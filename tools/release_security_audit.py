#!/usr/bin/env python3
"""Deterministic, stdlib-only internal release security evidence.

This is intentionally not a CycloneDX generator and does not perform a
vulnerability lookup.  It inventories declared/installed dependency metadata,
hashes release inputs, validates supplied Python archives, and records whether
external scanners are available without invoking them or using the network.
"""

from __future__ import annotations

import argparse
import base64
import configparser
import csv
import hashlib
import importlib.metadata
import io
import json
import os
import re
import secrets
import shlex
import shutil
import stat
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from email import policy
from email.message import Message
from email.parser import BytesParser
from pathlib import Path, PurePath, PurePosixPath
from typing import Collection, Iterable, Mapping, Optional, Sequence, Tuple, cast

_SCHEMA = "nano-aural-internal-release-security-evidence"
_SPDX_LICENSES = frozenset(
    ("Apache-2.0", "BSD-2-Clause", "BSD-3-Clause", "LGPL-3.0-only", "MIT", "PSF-2.0")
)
_SPDX_ID = re.compile(r"^SPDXRef-[A-Za-z0-9.-]+$")
_DENIED_DIRECTORY_NAMES = frozenset(
    (
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        ".tox",
        ".venv",
        "__pycache__",
        "checkpoints",
        "hf_cache",
        "model_weights",
        "secrets",
        "weights",
    )
)
_DENIED_FILE_NAMES = frozenset((".env", ".pypirc", "credentials", "id_rsa", "id_ed25519"))
_DENIED_SUFFIXES = frozenset(
    (
        ".ckpt",
        ".flac",
        ".key",
        ".mp3",
        ".mp4",
        ".pem",
        ".pth",
        ".pt",
        ".pyc",
        ".pyo",
        ".safetensors",
        ".wav",
    )
)
_TEXT_LIMIT_BYTES = 2 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 512 * 1024 * 1024
_MAX_ARCHIVE_MEMBER_BYTES = 128 * 1024 * 1024
_MAX_ARCHIVE_MEMBERS = 10_000
_SDIST_TOP_FILES = frozenset(
    ("LICENSE", "NOTICE", "PKG-INFO", "README.md", "pyproject.toml", "setup.cfg")
)
_PACKAGE_ROOTS = frozenset(
    (
        "nano_aural_runtime",
        "nano_aural_runtime_cli",
        "nano_aural_runtime_controlfoley",
        "nano_aural_runtime_remote",
        "nano_aural_runtime_stable_audio_3",
        "nano_aural_runtime_workers",
    )
)
_SDIST_EGG_INFO_FILES = frozenset(
    (
        "PKG-INFO",
        "SOURCES.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "requires.txt",
        "top_level.txt",
    )
)
_WHEEL_METADATA_FILES = frozenset(
    (
        "INSTALLER",
        "LICENSE",
        "METADATA",
        "NOTICE",
        "RECORD",
        "REQUESTED",
        "WHEEL",
        "direct_url.json",
        "entry_points.txt",
        "licenses",
        "top_level.txt",
    )
)


@dataclass(frozen=True, order=True)
class Finding:
    """A non-sensitive scanner result.

    Findings deliberately contain no matched value, line, environment value,
    or archive payload.  This makes scanner output safe to retain as evidence.
    """

    file: str
    rule: str

    def to_dict(self) -> Mapping[str, str]:
        return {"file": self.file, "rule": self.rule}


class ArtifactValidationError(ValueError):
    def __init__(self, file: str, rule: str) -> None:
        super().__init__("{0}: {1}".format(file, rule))
        self.finding = Finding(file, rule)


@dataclass(frozen=True)
class _Requirement:
    group: str
    requirement: str
    name: str
    archive_hashes: Tuple[str, ...] = ()


@dataclass(frozen=True)
class _ReleaseContract:
    name: str
    version: str
    description: str
    requires_python: str
    license_expression: str
    author: str
    classifiers: Tuple[str, ...]
    urls: Tuple[Tuple[str, str], ...]
    scripts: Tuple[Tuple[str, str], ...]
    optional_dependencies: Tuple[Tuple[str, Tuple[str, ...]], ...]
    build_backend_version: str


_SECRET_RULES = (
    (
        "secret.private_key",
        re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
    ("secret.aws_access_key", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("secret.github_token", re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b")),
    ("secret.provider_key", re.compile(r"\bsk-[A-Za-z0-9]{20,}\b")),
    (
        "secret.jwt",
        re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b"),
    ),
    (
        "secret.credential_url",
        re.compile(r"\b[a-z][a-z0-9+.-]*://[^\s/:@]+:[^\s/@]{8,}@", re.IGNORECASE),
    ),
)
_ASSIGNMENT = re.compile(
    r"(?i)\b(?:api[_-]?key|dsn|password|secret|token)\b\s*[:=]\s*[\"']([^\"'\r\n]{16,})[\"']"
)
_REPOSITORY_SECRET_ALLOWLIST = frozenset(
    (
        (
            "tests/test_controlfoley_profiler.py",
            "secret.hardcoded_assignment",
            "db495e0afe6760d861e992f49548a01e8ac1594ba9a92a3aed5eede515859a76",
        ),
        (
            "tests/test_ops_reference.py",
            "secret.credential_url",
            "ad0c9dcb6bd7ce235f7e265bf04995bd2e6321728d4d935a8f86ce34418cf65d",
        ),
        (
            "tests/test_durable_service.py",
            "secret.hardcoded_assignment",
            "8ab817b57342c26ffe488f3496c34d72b47ac4140f5dbcf16e9cb38c3390a2ba",
        ),
        (
            "tests/test_ops_reference.py",
            "secret.hardcoded_assignment",
            "b9899270295552d1188d6c153bb3107b582387fc8d1c29b0175f6f2139277c7b",
        ),
        (
            "tests/test_ops_reference.py",
            "secret.hardcoded_assignment",
            "834d43ffd9cc9e28d74e5d7b53c939b06829ca172d1c0496b1f0bb428bf5aa0c",
        ),
        (
            "tests/test_release_migration_recovery.py",
            "secret.hardcoded_assignment",
            "abb548089e6e7febc573d9f8edf00bf87c2797fc81e0a7435ed032ea4d742110",
        ),
    )
)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def scan_secrets(
    root: Path,
    *,
    allowlist: Collection[Tuple[str, str, str]] = (),
) -> Tuple[Finding, ...]:
    """Recursively scan text without returning the matched secret value."""

    root = root.resolve(strict=True)
    findings = set(_secret_path_findings(root))
    for path in _regular_files(root):
        relative = path.relative_to(root).as_posix()
        try:
            if path.stat().st_size > _TEXT_LIMIT_BYTES:
                continue
            raw = path.read_bytes()
        except OSError:
            findings.add(Finding(relative, "secret.unreadable"))
            continue
        if b"\x00" in raw:
            continue
        try:
            text = raw.decode("utf-8")
        except UnicodeDecodeError:
            continue
        permitted = frozenset(allowlist)
        for rule, pattern in _SECRET_RULES:
            for matched in pattern.finditer(text):
                fingerprint = hashlib.sha256(matched.group(0).encode("utf-8")).hexdigest()
                if (relative, rule, fingerprint) not in permitted:
                    findings.add(Finding(relative, rule))
        for matched in _ASSIGNMENT.finditer(text):
            rule = "secret.hardcoded_assignment"
            fingerprint = hashlib.sha256(matched.group(1).encode("utf-8")).hexdigest()
            if (relative, rule, fingerprint) not in permitted:
                findings.add(Finding(relative, rule))
    return tuple(sorted(findings))


def _secret_path_findings(root: Path) -> Tuple[Finding, ...]:
    findings = set()
    for directory, names, filenames in os.walk(root, followlinks=False):
        base = Path(directory)
        kept = []
        for name in sorted(names):
            path = base / name
            relative = path.relative_to(root).as_posix()
            if name == "secrets":
                findings.add(Finding(relative, "secret.denied_directory"))
            if name not in _DENIED_DIRECTORY_NAMES and not path.is_symlink():
                kept.append(name)
        names[:] = kept
        for name in sorted(filenames):
            lower = name.lower()
            if lower in _DENIED_FILE_NAMES or lower.startswith(".env."):
                relative = (base / name).relative_to(root).as_posix()
                findings.add(Finding(relative, "secret.sensitive_filename"))
    return tuple(sorted(findings))


def scan_artifact_tree(
    root: Path,
    *,
    allowed_top_levels: Collection[str],
) -> Tuple[Finding, ...]:
    """Apply recursive explicit allow and deny rules to an unpacked artifact."""

    root = root.resolve(strict=True)
    allowed = frozenset(allowed_top_levels)
    findings = set()
    for path in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
        relative = path.relative_to(root)
        display = relative.as_posix()
        if path.is_symlink():
            findings.add(Finding(display, "artifact.symlink"))
            continue
        if relative.parts and relative.parts[0] not in allowed:
            findings.add(Finding(display, "artifact.not_allowlisted"))
        denied = _denied_path(relative)
        if denied is not None:
            findings.add(Finding(display, denied))
    return tuple(sorted(findings))


def validate_wheel(path: Path, *, source_root: Path) -> Mapping[str, object]:
    display = path.name
    if not path.is_file() or path.suffix != ".whl" or path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ArtifactValidationError(display, "wheel.invalid_file")
    filename = re.fullmatch(
        r"nano_aural_runtime-([A-Za-z0-9][A-Za-z0-9._+]*)-py3-none-any\.whl", display
    )
    if filename is None:
        raise ArtifactValidationError(display, "wheel.filename_identity")
    version = filename.group(1)
    expected_dist_info = "nano_aural_runtime-{0}.dist-info".format(version)
    contract = _release_contract(source_root.resolve(strict=True))
    try:
        with zipfile.ZipFile(path) as archive:
            infos = archive.infolist()
            names = _validated_zip_names(display, infos)
            records = [name for name in names if name.endswith(".dist-info/RECORD")]
            if len(records) != 1:
                raise ArtifactValidationError(display, "wheel.record_count")
            record_name = records[0]
            dist_infos = {
                PurePosixPath(name).parts[0]
                for name in names
                if PurePosixPath(name).parts[0].endswith(".dist-info")
            }
            if len(dist_infos) != 1:
                raise ArtifactValidationError(display, "wheel.dist_info_count")
            dist_info = next(iter(dist_infos))
            if dist_info != expected_dist_info:
                raise ArtifactValidationError(display, "wheel.dist_info_identity")
            required_metadata = {
                dist_info + "/METADATA",
                dist_info + "/WHEEL",
                dist_info + "/RECORD",
                dist_info + "/entry_points.txt",
                dist_info + "/top_level.txt",
                dist_info + "/licenses/LICENSE",
                dist_info + "/licenses/NOTICE",
            }
            required = required_metadata | {root + "/__init__.py" for root in _PACKAGE_ROOTS}
            if not required.issubset(names):
                raise ArtifactValidationError(display, "wheel.required_metadata")
            metadata = _archive_metadata(display, archive.read(dist_info + "/METADATA"), "wheel")
            _validate_project_metadata(display, metadata, version, "wheel")
            license_files = tuple(metadata.get_all("License-File", ()))
            if license_files != ("LICENSE", "NOTICE"):
                raise ArtifactValidationError(display, "wheel.license_metadata")
            wheel_metadata = _archive_metadata(display, archive.read(dist_info + "/WHEEL"), "wheel")
            if (
                wheel_metadata.get("Wheel-Version") != "1.0"
                or wheel_metadata.get("Root-Is-Purelib", "").lower() != "true"
                or tuple(wheel_metadata.get_all("Tag", ())) != ("py3-none-any",)
            ):
                raise ArtifactValidationError(display, "wheel.wheel_metadata")
            _validate_entry_points(display, archive.read(dist_info + "/entry_points.txt"))
            top_levels = tuple(
                sorted(
                    line.strip()
                    for line in archive.read(dist_info + "/top_level.txt")
                    .decode("utf-8")
                    .splitlines()
                    if line.strip()
                )
            )
            if top_levels != tuple(sorted(_PACKAGE_ROOTS)):
                raise ArtifactValidationError(display, "wheel.top_level_metadata")
            expected = _wheel_record(display, archive.read(record_name))
            files = {name for name in names if not name.endswith("/")}
            if {name for name in files if name.startswith(dist_info + "/")} != required_metadata:
                raise ArtifactValidationError(display, "wheel.metadata_membership")
            if set(expected) != files:
                raise ArtifactValidationError(display, "wheel.record_membership")
            for name in sorted(files):
                _validate_wheel_member(display, name)
                recorded_hash, recorded_size = expected[name]
                data = archive.read(name)
                if name == record_name:
                    if recorded_hash or recorded_size:
                        raise ArtifactValidationError(display, "wheel.record_self_hash")
                    continue
                actual = "sha256=" + base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(
                    b"="
                ).decode("ascii")
                if recorded_hash != actual or recorded_size != str(len(data)):
                    raise ArtifactValidationError(display, "wheel.record_hash")
            _validate_wheel_source_binding(
                display, archive, files, dist_info, source_root, contract
            )
    except (OSError, zipfile.BadZipFile):
        raise ArtifactValidationError(display, "wheel.invalid_zip") from None
    return {
        "file": display,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "member_count": len(names),
        "validation": "VALID",
    }


def validate_sdist(path: Path, *, source_root: Path) -> Mapping[str, object]:
    display = path.name
    if not path.is_file() or path.stat().st_size > _MAX_ARCHIVE_BYTES:
        raise ArtifactValidationError(display, "sdist.invalid_file")
    filename = re.fullmatch(r"nano_aural_runtime-([A-Za-z0-9][A-Za-z0-9._+]*)\.tar\.gz", display)
    if filename is None:
        raise ArtifactValidationError(display, "sdist.filename_identity")
    version = filename.group(1)
    expected_root = "nano_aural_runtime-{0}".format(version)
    contract = _release_contract(source_root.resolve(strict=True))
    try:
        with tarfile.open(path, mode="r:*") as archive:
            members = []
            for member in archive:
                members.append(member)
                if len(members) > _MAX_ARCHIVE_MEMBERS:
                    raise ArtifactValidationError(display, "sdist.member_limit")
            names = []
            roots = set()
            total_size = 0
            for member in members:
                normalized = _archive_path(display, member.name, "sdist")
                if normalized in names:
                    raise ArtifactValidationError(display, "sdist.duplicate_member")
                names.append(normalized)
                roots.add(PurePosixPath(normalized).parts[0])
                if member.issym() or member.islnk():
                    raise ArtifactValidationError(display, "sdist.link")
                if not (member.isfile() or member.isdir()):
                    raise ArtifactValidationError(display, "sdist.special_file")
                if member.size < 0 or member.size > _MAX_ARCHIVE_MEMBER_BYTES:
                    raise ArtifactValidationError(display, "sdist.member_size_limit")
                total_size += member.size
                if total_size > _MAX_ARCHIVE_BYTES:
                    raise ArtifactValidationError(display, "sdist.total_size_limit")
            if len(roots) != 1:
                raise ArtifactValidationError(display, "sdist.root_count")
            root = next(iter(roots))
            if root != expected_root:
                raise ArtifactValidationError(display, "sdist.root_identity")
            required = (
                {root + "/" + name for name in _SDIST_TOP_FILES}
                | {root + "/src/" + name + "/__init__.py" for name in _PACKAGE_ROOTS}
                | {
                    root + "/src/nano_aural_runtime.egg-info/" + name
                    for name in _SDIST_EGG_INFO_FILES
                }
            )
            if not required.issubset(names):
                raise ArtifactValidationError(display, "sdist.required_metadata")
            for member, name in zip(members, names):
                relative = PurePosixPath(name).relative_to(root)
                if not relative.parts:
                    continue
                denied = _denied_path(relative)
                if denied is not None:
                    raise ArtifactValidationError(display, "sdist." + denied)
                _validate_sdist_member(display, relative, member.isdir())
            member_by_name = {member.name.rstrip("/"): member for member in members}
            pkg_info = archive.extractfile(member_by_name[root + "/PKG-INFO"])
            if pkg_info is None:
                raise ArtifactValidationError(display, "sdist.required_metadata")
            metadata = _archive_metadata(display, pkg_info.read(), "sdist")
            _validate_project_metadata(display, metadata, version, "sdist")
            _validate_sdist_source_binding(
                display,
                archive,
                member_by_name,
                root,
                source_root.resolve(strict=True),
                contract,
            )
    except (OSError, tarfile.TarError):
        raise ArtifactValidationError(display, "sdist.invalid_tar") from None
    return {
        "file": display,
        "sha256": sha256_file(path),
        "size_bytes": path.stat().st_size,
        "member_count": len(names),
        "validation": "VALID",
    }


def dependency_inventory(root: Path) -> Tuple[Mapping[str, object], ...]:
    requirements = _declared_requirements(root)
    inventory = []
    for requirement in requirements:
        version: Optional[str] = None
        license_name: Optional[str] = None
        metadata_hash: Optional[str] = None
        record_hash: Optional[str] = None
        try:
            distribution = importlib.metadata.distribution(requirement.name)
        except importlib.metadata.PackageNotFoundError:
            status = "NOT_INSTALLED"
        else:
            status = "INSTALLED"
            version = distribution.version
            metadata = distribution.read_text("METADATA")
            record = distribution.read_text("RECORD")
            if metadata is not None:
                metadata_hash = hashlib.sha256(metadata.encode("utf-8")).hexdigest()
            if record is not None:
                record_hash = hashlib.sha256(record.encode("utf-8")).hexdigest()
            license_name = _distribution_license(cast(Mapping[str, str], distribution.metadata))
        inventory.append(
            {
                "group": requirement.group,
                "name": requirement.name,
                "requirement": requirement.requirement,
                "status": status,
                "installed_version": version,
                "license": license_name,
                "metadata_sha256": metadata_hash,
                "record_sha256": record_hash,
                "distribution_archive_hash": (
                    requirement.archive_hashes[0] if len(requirement.archive_hashes) == 1 else None
                ),
            }
        )
    return tuple(inventory)


def release_input_manifest(root: Path) -> Mapping[str, object]:
    root = root.resolve(strict=True)
    project = _project_metadata(root / "pyproject.toml")
    wheel_paths = [root / name for name in ("LICENSE", "NOTICE", "README.md", "pyproject.toml")]
    wheel_paths.extend(_regular_files(root / "src"))
    sdist_paths = tuple(wheel_paths)
    container = _container_manifest(root, root / "ops" / "Dockerfile.api")
    return {
        "project": {
            **project,
            "license_file_sha256": sha256_file(root / "LICENSE"),
            "notice_file_sha256": sha256_file(root / "NOTICE"),
        },
        "wheel_inputs": _file_records(root, wheel_paths),
        "sdist_inputs": _file_records(root, sdist_paths),
        "container": container,
    }


def external_capabilities() -> Mapping[str, object]:
    cyclonedx = tuple(name for name in ("cyclonedx-py", "cyclonedx-bom") if shutil.which(name))
    vulnerability = tuple(name for name in ("pip-audit", "trivy", "grype") if shutil.which(name))
    return {
        "spdx_validation": {
            "status": "UNRUN",
            "available_tools": tuple(
                name for name in ("pyspdxtools", "spdx-tools") if shutil.which(name)
            ),
            "reason": "external SPDX validation was not invoked",
        },
        "cyclonedx_sbom": {
            "status": "UNRUN",
            "available_tools": cyclonedx,
            "reason": "formal external SBOM generation is outside this stdlib-only audit",
        },
        "vulnerability_scan": {
            "status": "UNRUN",
            "available_tools": vulnerability,
            "reason": "external vulnerability scanners were detected but never invoked",
        },
    }


def audit_release(
    root: Path,
    *,
    wheels: Sequence[Path] = (),
    sdists: Sequence[Path] = (),
    source_date_epoch: Optional[int] = None,
) -> Mapping[str, object]:
    root = root.resolve(strict=True)
    secrets_found = scan_secrets(root, allowlist=_REPOSITORY_SECRET_ALLOWLIST)
    release_inputs = release_input_manifest(root)
    if source_date_epoch is None:
        standard_sbom: Mapping[str, object] = {
            "format": "SPDX-2.3-JSON",
            "status": "UNRUN",
            "reason": "source_date_epoch was not supplied",
            "external_validation": "UNRUN",
        }
    else:
        spdx = build_spdx_sbom(root, source_date_epoch=source_date_epoch)
        # Bind the exact standalone artifact bytes emitted by ``main`` rather
        # than only the JSON value.  The evidence file contract includes one
        # trailing newline, so the recorded digest must include it as well.
        spdx_payload = _canonical_json(spdx) + b"\n"
        standard_sbom = {
            "format": "SPDX-2.3-JSON",
            "status": "GENERATED",
            "validation": "BUILTIN_STRICT_STRUCTURAL_VALIDATION",
            "external_validation": "UNRUN",
            "sha256": hashlib.sha256(spdx_payload).hexdigest(),
            "document": spdx,
        }
    wheel_results = tuple(validate_wheel(path, source_root=root) for path in sorted(wheels))
    sdist_results = tuple(validate_sdist(path, source_root=root) for path in sorted(sdists))
    dependencies = dependency_inventory(root)
    supply_chain_findings = set(_dependency_lock_findings(root))
    for dependency in dependencies:
        if dependency["distribution_archive_hash"] is None:
            source = (
                "ops/requirements-api.txt"
                if str(dependency["group"]).startswith("container:")
                else "pyproject.toml"
            )
            supply_chain_findings.add(
                Finding(source, "dependency.distribution_archive_hash_unavailable")
            )
    container = cast(Mapping[str, object], release_inputs["container"])
    container_findings = cast(Sequence[Mapping[str, str]], container["findings"])
    supply_chain_findings.update(Finding(item["file"], item["rule"]) for item in container_findings)
    return {
        "schema": _SCHEMA,
        "schema_version": 1,
        "evidence_kind": "internal_inventory_not_cyclonedx",
        "release_inputs": release_inputs,
        "dependencies": dependencies,
        "standard_sbom": standard_sbom,
        "artifacts": {
            "wheel_validation_status": "VALIDATED" if wheel_results else "UNRUN",
            "wheels": wheel_results,
            "sdist_validation_status": "VALIDATED" if sdist_results else "UNRUN",
            "sdists": sdist_results,
        },
        "supply_chain_findings": tuple(item.to_dict() for item in sorted(supply_chain_findings)),
        "secret_scan": {
            "status": "CLEAN" if not secrets_found else "FINDINGS",
            "findings": tuple(item.to_dict() for item in secrets_found),
        },
        "external_materials": (
            {
                "name": "ControlFoley source checkout",
                "distribution": "EXCLUDED_OPERATOR_SUPPLIED",
                "version": None,
                "license": "operator-must-resolve-upstream-terms",
                "sha256": None,
            },
            {
                "name": "ControlFoley main checkpoint",
                "distribution": "EXCLUDED_OPERATOR_SUPPLIED",
                "version": None,
                "license": "operator-must-resolve-model-dataset-and-weight-terms",
                "sha256": None,
            },
            {
                "name": "ControlFoley external weights",
                "distribution": "EXCLUDED_OPERATOR_SUPPLIED",
                "version": None,
                "license": "operator-must-resolve-model-dataset-and-weight-terms",
                "sha256": None,
            },
            {
                "name": "Hugging Face cache and snapshots",
                "distribution": "EXCLUDED_OPERATOR_SUPPLIED",
                "version": None,
                "license": "operator-must-resolve-each-upstream-artifact-term",
                "sha256": None,
            },
            {
                "name": "Model input/output media",
                "distribution": "EXCLUDED_OPERATOR_SUPPLIED",
                "version": None,
                "license": "operator-must-resolve-media-and-dataset-terms",
                "sha256": None,
            },
            {
                "name": "Private test and benchmark fixtures",
                "distribution": "EXCLUDED_OPERATOR_SUPPLIED",
                "version": None,
                "license": "operator-must-resolve-fixture-terms",
                "sha256": None,
            },
        ),
        "capabilities": external_capabilities(),
        "release_gate": "NOT_EVALUATED",
    }


def build_spdx_sbom(root: Path, *, source_date_epoch: int) -> Mapping[str, object]:
    """Build a deterministic SPDX 2.3 JSON document for declared Python components."""

    created = _spdx_created(source_date_epoch)
    project = _project_metadata(root / "pyproject.toml")
    dependencies = dependency_inventory(root)
    project_id = "SPDXRef-Package-nano-aural-runtime"
    container_id = "SPDXRef-Package-nano-aural-runtime-api-container"
    packages = [
        {
            "name": project["name"],
            "SPDXID": project_id,
            "versionInfo": project["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": project["license"],
            "licenseDeclared": project["license"],
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "APPLICATION",
        },
        {
            "name": "nano-aural-runtime-api-container",
            "SPDXID": container_id,
            "versionInfo": project["version"],
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": project["license"],
            "licenseDeclared": project["license"],
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "CONTAINER",
            "comment": "nanoAural-container=api",
        },
    ]
    grouped: dict[Tuple[str, Optional[str]], list[Mapping[str, object]]] = {}
    for dependency in dependencies:
        identity = (str(dependency["name"]), _spdx_dependency_version(dependency))
        grouped.setdefault(identity, []).append(dependency)
    for name, version in sorted(grouped, key=lambda item: (item[0], item[1] or "")):
        rows = grouped[(name, version)]
        licenses = sorted(
            {
                str(row["license"])
                for row in rows
                if row["license"] and row["installed_version"] == version
            }
        )
        declared_license = _spdx_license(licenses[0] if len(licenses) == 1 else None)
        package: dict[str, object] = {
            "name": name,
            "SPDXID": _spdx_package_id(name, version),
            "downloadLocation": "NOASSERTION",
            "filesAnalyzed": False,
            "licenseConcluded": "NOASSERTION",
            "licenseDeclared": declared_license,
            "copyrightText": "NOASSERTION",
            "primaryPackagePurpose": "LIBRARY",
            "comment": "nanoAural-declarations="
            + _canonical_json(
                {
                    "groups": tuple(sorted({str(row["group"]) for row in rows})),
                    "requirements": tuple(sorted({str(row["requirement"]) for row in rows})),
                }
            ).decode("ascii"),
        }
        if version is not None:
            package["versionInfo"] = version
            package["externalRefs"] = (
                {
                    "referenceCategory": "PACKAGE-MANAGER",
                    "referenceType": "purl",
                    "referenceLocator": "pkg:pypi/{0}@{1}".format(name, version),
                },
            )
        archive_hashes = sorted(
            {
                str(row["distribution_archive_hash"])
                for row in rows
                if row["distribution_archive_hash"]
            }
        )
        if archive_hashes:
            package["checksums"] = tuple(
                {"algorithm": "SHA256", "checksumValue": archive_hash}
                for archive_hash in archive_hashes
            )
        packages.append(package)
    relationship_values = {
        ("SPDXRef-DOCUMENT", "DESCRIBES", project_id),
        ("SPDXRef-DOCUMENT", "DESCRIBES", container_id),
    }
    for name, version in sorted(grouped, key=lambda item: (item[0], item[1] or "")):
        dependency_id = _spdx_package_id(name, version)
        for group in sorted({str(row["group"]) for row in grouped[(name, version)]}):
            relationship_values.add(
                _spdx_group_relationship(project_id, container_id, dependency_id, group)
            )
    relationships = tuple(
        {
            "spdxElementId": source,
            "relationshipType": relationship,
            "relatedSpdxElement": target,
        }
        for source, relationship, target in sorted(relationship_values)
    )
    release_content_sha256 = hashlib.sha256(
        _canonical_json(release_input_manifest(root))
    ).hexdigest()
    document_comment = "nanoAural-release-inputs-sha256=" + release_content_sha256
    namespace_seed = {
        "created": created,
        "document_comment": document_comment,
        "name": "{0}-{1}".format(project["name"], project["version"]),
        "packages": tuple(packages),
        "relationships": relationships,
    }
    namespace_hash = hashlib.sha256(_canonical_json(namespace_seed)).hexdigest()
    document = {
        "spdxVersion": "SPDX-2.3",
        "dataLicense": "CC0-1.0",
        "SPDXID": "SPDXRef-DOCUMENT",
        "name": "{0}-{1}".format(project["name"], project["version"]),
        "comment": document_comment,
        "documentNamespace": (
            "https://github.com/dongyuan21/nanoAuralRuntime/spdx/" + namespace_hash
        ),
        "creationInfo": {
            "created": created,
            "creators": ("Tool: nanoAuralRuntime-release-security-audit-1",),
        },
        "packages": tuple(packages),
        "relationships": relationships,
    }
    validate_spdx_sbom(document)
    return document


def validate_spdx_sbom(document: Mapping[str, object]) -> None:
    """Validate the strict SPDX 2.3 subset emitted by this audit."""

    required = {
        "spdxVersion",
        "dataLicense",
        "SPDXID",
        "name",
        "comment",
        "documentNamespace",
        "creationInfo",
        "packages",
        "relationships",
    }
    if set(document) != required:
        raise ValueError("SPDX document fields are invalid")
    if (
        document["spdxVersion"] != "SPDX-2.3"
        or document["dataLicense"] != "CC0-1.0"
        or document["SPDXID"] != "SPDXRef-DOCUMENT"
    ):
        raise ValueError("SPDX document identity is invalid")
    if not isinstance(document["name"], str) or not document["name"]:
        raise ValueError("SPDX document name is invalid")
    document_comment = document["comment"]
    if (
        not isinstance(document_comment, str)
        or re.fullmatch(r"nanoAural-release-inputs-sha256=[0-9a-f]{64}", document_comment) is None
    ):
        raise ValueError("SPDX document content binding is invalid")
    namespace = document["documentNamespace"]
    if (
        not isinstance(namespace, str)
        or not namespace.startswith("https://")
        or any(character.isspace() for character in namespace)
    ):
        raise ValueError("SPDX document namespace is invalid")
    creation = document["creationInfo"]
    if not isinstance(creation, Mapping) or set(creation) != {"created", "creators"}:
        raise ValueError("SPDX creationInfo is invalid")
    _validate_spdx_timestamp(creation["created"])
    creators = creation["creators"]
    if not isinstance(creators, (tuple, list)) or tuple(creators) != (
        "Tool: nanoAuralRuntime-release-security-audit-1",
    ):
        raise ValueError("SPDX creator is invalid")
    packages = document["packages"]
    if not isinstance(packages, (tuple, list)) or not packages:
        raise ValueError("SPDX packages are invalid")
    identifiers = set()
    declarations: dict[str, Tuple[Tuple[str, ...], Tuple[str, ...]]] = {}
    for package in packages:
        if not isinstance(package, Mapping):
            raise ValueError("SPDX package is invalid")
        required_package = {
            "name",
            "SPDXID",
            "downloadLocation",
            "filesAnalyzed",
            "licenseConcluded",
            "licenseDeclared",
            "copyrightText",
            "primaryPackagePurpose",
        }
        allowed_package = required_package | {
            "checksums",
            "comment",
            "externalRefs",
            "versionInfo",
        }
        if not required_package.issubset(package) or not set(package).issubset(allowed_package):
            raise ValueError("SPDX package fields are incomplete")
        identifier = package["SPDXID"]
        if not isinstance(identifier, str) or _SPDX_ID.fullmatch(identifier) is None:
            raise ValueError("SPDX package identifier is invalid")
        if identifier in identifiers:
            raise ValueError("SPDX package identifiers are not unique")
        identifiers.add(identifier)
        if (
            not isinstance(package["name"], str)
            or not package["name"]
            or package["downloadLocation"] != "NOASSERTION"
            or package["filesAnalyzed"] is not False
            or package["copyrightText"] != "NOASSERTION"
            or package["primaryPackagePurpose"] not in ("APPLICATION", "CONTAINER", "LIBRARY")
        ):
            raise ValueError("SPDX package values are invalid")
        for field in ("licenseConcluded", "licenseDeclared"):
            if package[field] != "NOASSERTION" and package[field] not in _SPDX_LICENSES:
                raise ValueError("SPDX package license is not in the audited allowlist")
        if "versionInfo" in package and (
            not isinstance(package["versionInfo"], str) or not package["versionInfo"]
        ):
            raise ValueError("SPDX package version is invalid")
        if "comment" in package and (
            not isinstance(package["comment"], str)
            or not package["comment"]
            or len(package["comment"]) > 2048
        ):
            raise ValueError("SPDX package comment is invalid")
        if package["primaryPackagePurpose"] == "LIBRARY":
            declarations[identifier] = _spdx_declarations(package.get("comment"))
        if "checksums" in package:
            checksums = package["checksums"]
            if not isinstance(checksums, (tuple, list)) or not checksums:
                raise ValueError("SPDX package checksums are invalid")
            checksum_values = []
            for checksum in checksums:
                if (
                    not isinstance(checksum, Mapping)
                    or set(checksum) != {"algorithm", "checksumValue"}
                    or checksum["algorithm"] != "SHA256"
                    or not isinstance(checksum["checksumValue"], str)
                    or re.fullmatch(r"[0-9a-f]{64}", checksum["checksumValue"]) is None
                ):
                    raise ValueError("SPDX package checksum is invalid")
                checksum_values.append(checksum["checksumValue"])
            if checksum_values != sorted(set(checksum_values)):
                raise ValueError("SPDX package checksums are not canonical")
        if "externalRefs" in package:
            references = package["externalRefs"]
            if not isinstance(references, (tuple, list)) or len(references) != 1:
                raise ValueError("SPDX package external references are invalid")
            reference = references[0]
            if not isinstance(reference, Mapping) or set(reference) != {
                "referenceCategory",
                "referenceType",
                "referenceLocator",
            }:
                raise ValueError("SPDX package external reference fields are invalid")
            locator = reference["referenceLocator"]
            if (
                reference["referenceCategory"] != "PACKAGE-MANAGER"
                or reference["referenceType"] != "purl"
                or not isinstance(locator, str)
                or not locator.startswith("pkg:pypi/")
                or any(character.isspace() for character in locator)
            ):
                raise ValueError("SPDX package external reference is invalid")
            if "versionInfo" not in package or locator != "pkg:pypi/{0}@{1}".format(
                package["name"], package["versionInfo"]
            ):
                raise ValueError("SPDX package external reference version is invalid")
    relationships = document["relationships"]
    if not isinstance(relationships, (tuple, list)) or not relationships:
        raise ValueError("SPDX relationships are invalid")
    known = identifiers | {"SPDXRef-DOCUMENT"}
    actual = set()
    for relationship in relationships:
        if not isinstance(relationship, Mapping) or set(relationship) != {
            "spdxElementId",
            "relationshipType",
            "relatedSpdxElement",
        }:
            raise ValueError("SPDX relationship fields are invalid")
        values = (
            relationship["spdxElementId"],
            relationship["relationshipType"],
            relationship["relatedSpdxElement"],
        )
        if values[0] not in known or values[2] not in known:
            raise ValueError("SPDX relationship reference is invalid")
        if values[1] not in (
            "BUILD_DEPENDENCY_OF",
            "DEPENDS_ON",
            "DESCRIBES",
            "DEV_DEPENDENCY_OF",
            "OPTIONAL_DEPENDENCY_OF",
            "TEST_DEPENDENCY_OF",
        ):
            raise ValueError("SPDX relationship type is invalid")
        actual.add(values)
    if len(actual) != len(relationships):
        raise ValueError("SPDX relationships are duplicated")
    applications = [
        package for package in packages if package["primaryPackagePurpose"] == "APPLICATION"
    ]
    if len(applications) != 1:
        raise ValueError("SPDX application package is invalid")
    root = applications[0]["SPDXID"]
    containers = [
        package for package in packages if package["primaryPackagePurpose"] == "CONTAINER"
    ]
    if (
        len(containers) != 1
        or containers[0]["name"] != "nano-aural-runtime-api-container"
        or containers[0].get("comment") != "nanoAural-container=api"
    ):
        raise ValueError("SPDX container package is invalid")
    container = containers[0]["SPDXID"]
    expected = {
        ("SPDXRef-DOCUMENT", "DESCRIBES", root),
        ("SPDXRef-DOCUMENT", "DESCRIBES", container),
    }
    for dependency_id, (groups, requirements) in declarations.items():
        if any(group.startswith("container:") for group in groups):
            package = next(item for item in packages if item["SPDXID"] == dependency_id)
            locked_versions = {
                version
                for requirement in requirements
                if (version := _exact_requirement_version(requirement)) is not None
            }
            if package.get("versionInfo") not in locked_versions or "checksums" not in package:
                raise ValueError("SPDX container dependency is not sealed")
        expected.update(
            _spdx_group_relationship(root, container, dependency_id, group) for group in groups
        )
    if actual != expected:
        raise ValueError("SPDX relationship graph is incomplete")
    namespace_seed = {
        "created": creation["created"],
        "document_comment": document_comment,
        "name": document["name"],
        "packages": tuple(packages),
        "relationships": tuple(relationships),
    }
    expected_namespace = (
        "https://github.com/dongyuan21/nanoAuralRuntime/spdx/"
        + hashlib.sha256(_canonical_json(namespace_seed)).hexdigest()
    )
    if namespace != expected_namespace:
        raise ValueError("SPDX document namespace is not bound to document content")


def _spdx_created(source_date_epoch: int) -> str:
    if (
        isinstance(source_date_epoch, bool)
        or not isinstance(source_date_epoch, int)
        or source_date_epoch < 0
    ):
        raise ValueError("source_date_epoch must be a nonnegative integer")
    try:
        value = datetime.fromtimestamp(source_date_epoch, timezone.utc)
    except (OSError, OverflowError, ValueError):
        raise ValueError("source_date_epoch is outside the supported range") from None
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _validate_spdx_timestamp(value: object) -> None:
    if not isinstance(value, str) or not value.endswith("Z"):
        raise ValueError("SPDX creation timestamp is invalid")
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ")
    except ValueError:
        raise ValueError("SPDX creation timestamp is invalid") from None


def _spdx_license(value: Optional[str]) -> str:
    aliases = {
        "Apache Software License": "Apache-2.0",
        "MIT License": "MIT",
        "GNU Lesser General Public License v3 (LGPLv3)": "LGPL-3.0-only",
    }
    normalized = aliases.get(value or "", value)
    return normalized if normalized in _SPDX_LICENSES else "NOASSERTION"


def _spdx_dependency_version(dependency: Mapping[str, object]) -> Optional[str]:
    group = str(dependency["group"])
    if group.startswith("container:"):
        version = _exact_requirement_version(str(dependency["requirement"]))
        archive_hash = dependency["distribution_archive_hash"]
        if version is None or not isinstance(archive_hash, str):
            raise ValueError("container dependency is not sealed by an exact version and hash")
        if re.fullmatch(r"[0-9a-f]{64}", archive_hash) is None:
            raise ValueError("container dependency archive hash is invalid")
        return version
    installed = dependency["installed_version"]
    return str(installed) if installed else None


def _exact_requirement_version(requirement: str) -> Optional[str]:
    matched = re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?==([^,;*\s]+)",
        requirement,
    )
    return matched.group(1) if matched is not None else None


def _spdx_package_id(name: str, version: Optional[str]) -> str:
    normalized_name = re.sub(r"[^A-Za-z0-9.-]+", "-", name).strip("-.")
    normalized_version = re.sub(r"[^A-Za-z0-9.-]+", "-", version or "unversioned").strip("-.")
    if not normalized_name or not normalized_version:
        raise ValueError("dependency name cannot form an SPDX identifier")
    identity_hash = hashlib.sha256((name + "\0" + (version or "")).encode("utf-8")).hexdigest()[:12]
    return "SPDXRef-Dependency-{0}-{1}-{2}".format(
        normalized_name,
        normalized_version,
        identity_hash,
    )


def _spdx_group_relationship(
    project_id: str, container_id: str, dependency_id: str, group: str
) -> Tuple[str, str, str]:
    if group == "build":
        return dependency_id, "BUILD_DEPENDENCY_OF", project_id
    if group == "runtime":
        return project_id, "DEPENDS_ON", dependency_id
    if group.startswith("container:"):
        return container_id, "DEPENDS_ON", dependency_id
    if group == "optional:dev":
        return dependency_id, "DEV_DEPENDENCY_OF", project_id
    if group.startswith("optional:") and "test" in group.split(":", 1)[1].split("-"):
        return dependency_id, "TEST_DEPENDENCY_OF", project_id
    if group.startswith("optional:"):
        return dependency_id, "OPTIONAL_DEPENDENCY_OF", project_id
    raise ValueError("dependency group has no SPDX relationship mapping")


def _spdx_declarations(comment: object) -> Tuple[Tuple[str, ...], Tuple[str, ...]]:
    prefix = "nanoAural-declarations="
    if not isinstance(comment, str) or not comment.startswith(prefix):
        raise ValueError("SPDX dependency declarations are missing")
    try:
        value = json.loads(comment[len(prefix) :])
    except (json.JSONDecodeError, UnicodeError):
        raise ValueError("SPDX dependency declarations are invalid") from None
    if not isinstance(value, Mapping) or set(value) != {"groups", "requirements"}:
        raise ValueError("SPDX dependency declarations are invalid")
    groups = value["groups"]
    requirements = value["requirements"]
    if (
        not isinstance(groups, list)
        or not groups
        or not all(isinstance(group, str) and group for group in groups)
        or groups != sorted(set(groups))
        or not isinstance(requirements, list)
        or not requirements
        or not all(isinstance(requirement, str) and requirement for requirement in requirements)
        or requirements != sorted(set(requirements))
    ):
        raise ValueError("SPDX dependency declarations are invalid")
    return tuple(groups), tuple(requirements)


def _spdx_declared_groups(comment: object) -> Tuple[str, ...]:
    groups, _requirements = _spdx_declarations(comment)
    return groups


def _regular_files(root: Path) -> Tuple[Path, ...]:
    if not root.exists():
        return ()
    if root.is_file():
        return (root,)
    files = []
    for directory, names, filenames in os.walk(root, followlinks=False):
        names[:] = sorted(name for name in names if name not in _DENIED_DIRECTORY_NAMES)
        base = Path(directory)
        for name in sorted(filenames):
            path = base / name
            if not path.is_symlink() and path.is_file():
                files.append(path)
    return tuple(files)


def _denied_path(path: PurePath) -> Optional[str]:
    if any(part in _DENIED_DIRECTORY_NAMES for part in path.parts):
        return "artifact.denied_directory"
    name = path.name.lower()
    if name in _DENIED_FILE_NAMES or name.startswith(".env."):
        return "artifact.denied_secret_file"
    if path.suffix.lower() in _DENIED_SUFFIXES:
        return "artifact.denied_binary_or_model_file"
    return None


def _archive_path(display: str, raw: str, kind: str) -> str:
    if not raw or raw.startswith(("/", "\\")) or "\\" in raw:
        raise ArtifactValidationError(display, kind + ".unsafe_path")
    directory = raw.endswith("/")
    value = raw[:-1] if directory else raw
    parts = value.split("/")
    if (
        not value
        or len(value) > 4096
        or any(part in ("", ".", "..") or len(part) > 255 for part in parts)
    ):
        raise ArtifactValidationError(display, kind + ".unsafe_path")
    normalized = PurePosixPath(*parts).as_posix()
    return normalized + ("/" if directory else "")


def _validated_zip_names(display: str, infos: Sequence[zipfile.ZipInfo]) -> Tuple[str, ...]:
    if len(infos) > _MAX_ARCHIVE_MEMBERS:
        raise ArtifactValidationError(display, "wheel.member_limit")
    names = []
    total_size = 0
    for info in infos:
        name = _archive_path(display, info.filename, "wheel")
        if name in names:
            raise ArtifactValidationError(display, "wheel.duplicate_member")
        mode = (info.external_attr >> 16) & 0o170000
        if info.flag_bits & 0x1:
            raise ArtifactValidationError(display, "wheel.encrypted_member")
        if mode == stat.S_IFLNK:
            raise ArtifactValidationError(display, "wheel.symlink")
        if mode not in (0, stat.S_IFREG, stat.S_IFDIR):
            raise ArtifactValidationError(display, "wheel.special_file")
        if info.file_size < 0 or info.file_size > _MAX_ARCHIVE_MEMBER_BYTES:
            raise ArtifactValidationError(display, "wheel.member_size_limit")
        total_size += info.file_size
        if total_size > _MAX_ARCHIVE_BYTES:
            raise ArtifactValidationError(display, "wheel.total_size_limit")
        names.append(name)
    return tuple(names)


def _wheel_record(display: str, payload: bytes) -> Mapping[str, Tuple[str, str]]:
    try:
        rows = csv.reader(io.StringIO(payload.decode("utf-8"), newline=""))
        result = {}
        for row in rows:
            if len(row) != 3:
                raise ArtifactValidationError(display, "wheel.record_schema")
            name = _archive_path(display, row[0], "wheel")
            if name in result:
                raise ArtifactValidationError(display, "wheel.record_duplicate")
            result[name] = (row[1], row[2])
    except UnicodeDecodeError:
        raise ArtifactValidationError(display, "wheel.record_encoding") from None
    return result


def _archive_metadata(display: str, payload: bytes, kind: str) -> Message:
    try:
        metadata = BytesParser().parsebytes(payload, headersonly=True)
    except (UnicodeError, ValueError):
        raise ArtifactValidationError(display, kind + ".metadata_encoding") from None
    if metadata.defects:
        raise ArtifactValidationError(display, kind + ".metadata_schema")
    return metadata


def _validate_project_metadata(display: str, metadata: Message, version: str, kind: str) -> None:
    required = {
        "Metadata-Version": None,
        "Name": "nano-aural-runtime",
        "Version": version,
        "License-Expression": "Apache-2.0",
    }
    for field, expected in required.items():
        values = tuple(metadata.get_all(field, ()))
        if len(values) != 1 or (expected is not None and values[0] != expected):
            raise ArtifactValidationError(display, kind + ".project_identity")


def _validate_entry_points(display: str, payload: bytes) -> None:
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        parser.read_string(payload.decode("utf-8"))
    except (configparser.Error, UnicodeDecodeError):
        raise ArtifactValidationError(display, "wheel.entry_points") from None
    expected = {
        "nano-aural": "nano_aural_runtime_cli.main:main",
        "nano-aural-controlfoley": "nano_aural_runtime_cli.main:controlfoley_alias",
        "nano-aural-remote": "nano_aural_runtime_remote.cli:main",
    }
    if (
        parser.sections() != ["console_scripts"]
        or dict(parser.items("console_scripts")) != expected
    ):
        raise ArtifactValidationError(display, "wheel.entry_points")


def _validate_wheel_member(display: str, name: str) -> None:
    path = PurePosixPath(name)
    denied = _denied_path(path)
    if denied is not None:
        raise ArtifactValidationError(display, "wheel." + denied)
    first = path.parts[0]
    if first.endswith(".dist-info") and len(path.parts) >= 2:
        if path.parts[1] not in _WHEEL_METADATA_FILES:
            raise ArtifactValidationError(display, "wheel.metadata_member_not_allowlisted")
        if path.parts[1] == "licenses":
            if len(path.parts) != 3 or path.parts[2] not in ("LICENSE", "NOTICE"):
                raise ArtifactValidationError(display, "wheel.metadata_member_not_allowlisted")
        elif len(path.parts) != 2:
            raise ArtifactValidationError(display, "wheel.metadata_member_not_allowlisted")
        return
    if first in _PACKAGE_ROOTS:
        if path.suffix not in (".py", ".pyi", ".sql") and path.name != "py.typed":
            raise ArtifactValidationError(display, "wheel.package_member_not_allowlisted")
        return
    raise ArtifactValidationError(display, "wheel.member_not_allowlisted")


def _source_package_files(source_root: Path) -> Mapping[str, Path]:
    result = {}
    for package in sorted(_PACKAGE_ROOTS):
        package_root = source_root / "src" / package
        if not package_root.is_dir() or package_root.is_symlink():
            raise ValueError("release package source root is missing")
        for path in _regular_files(package_root):
            if path.suffix not in (".py", ".pyi", ".sql") and path.name != "py.typed":
                continue
            relative = path.relative_to(source_root / "src").as_posix()
            result[relative] = path
    return result


def _validate_wheel_source_binding(
    display: str,
    archive: zipfile.ZipFile,
    files: Collection[str],
    dist_info: str,
    source_root: Path,
    contract: _ReleaseContract,
) -> None:
    source_root = source_root.resolve(strict=True)
    metadata_payload = archive.read(dist_info + "/METADATA")
    _validate_release_metadata(display, metadata_payload, contract, source_root, "wheel")
    if archive.read(dist_info + "/entry_points.txt") != _expected_entry_points(contract):
        raise ArtifactValidationError(display, "wheel.entry_points")
    if archive.read(dist_info + "/top_level.txt") != _expected_top_level():
        raise ArtifactValidationError(display, "wheel.top_level_metadata")
    expected_wheel = (
        "Wheel-Version: 1.0\n"
        "Generator: setuptools ({0})\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n\n".format(contract.build_backend_version)
    ).encode("utf-8")
    if archive.read(dist_info + "/WHEEL") != expected_wheel:
        raise ArtifactValidationError(display, "wheel.wheel_metadata")
    expected = _source_package_files(source_root)
    actual = {name for name in files if PurePosixPath(name).parts[0] in _PACKAGE_ROOTS}
    if actual != set(expected):
        raise ArtifactValidationError(display, "wheel.source_membership")
    for name, source in expected.items():
        if archive.read(name) != source.read_bytes():
            raise ArtifactValidationError(display, "wheel.source_hash")
    for name in ("LICENSE", "NOTICE"):
        if archive.read(dist_info + "/licenses/" + name) != (source_root / name).read_bytes():
            raise ArtifactValidationError(display, "wheel.source_hash")


def _validate_sdist_member(display: str, relative: PurePosixPath, directory: bool) -> None:
    if len(relative.parts) == 1:
        if directory and relative.parts[0] == "src":
            return
        if not directory and relative.as_posix() in _SDIST_TOP_FILES:
            return
        raise ArtifactValidationError(display, "sdist.not_allowlisted")
    if relative.parts[0] != "src":
        raise ArtifactValidationError(display, "sdist.not_allowlisted")
    package = relative.parts[1]
    if package == "nano_aural_runtime.egg-info":
        if directory and len(relative.parts) == 2:
            return
        if (
            not directory
            and len(relative.parts) == 3
            and relative.parts[2] in _SDIST_EGG_INFO_FILES
        ):
            return
        raise ArtifactValidationError(display, "sdist.not_allowlisted")
    if package not in _PACKAGE_ROOTS:
        raise ArtifactValidationError(display, "sdist.not_allowlisted")
    if directory:
        return
    if relative.suffix not in (".py", ".pyi", ".sql") and relative.name != "py.typed":
        raise ArtifactValidationError(display, "sdist.package_member_not_allowlisted")


def _validate_sdist_source_binding(
    display: str,
    archive: tarfile.TarFile,
    members: Mapping[str, tarfile.TarInfo],
    root: str,
    source_root: Path,
    contract: _ReleaseContract,
) -> None:
    metadata_member = archive.extractfile(members[root + "/PKG-INFO"])
    if metadata_member is None:
        raise ArtifactValidationError(display, "sdist.required_metadata")
    metadata_payload = metadata_member.read()
    _validate_release_metadata(display, metadata_payload, contract, source_root, "sdist")
    expected = _source_package_files(source_root)
    actual = {
        name[len(root + "/src/") :]
        for name, member in members.items()
        if name.startswith(root + "/src/")
        and not member.isdir()
        and PurePosixPath(name[len(root + "/src/") :]).parts[0] in _PACKAGE_ROOTS
    }
    if actual != set(expected):
        raise ArtifactValidationError(display, "sdist.source_membership")
    for name, source in expected.items():
        member = archive.extractfile(members[root + "/src/" + name])
        if member is None or member.read() != source.read_bytes():
            raise ArtifactValidationError(display, "sdist.source_hash")
    for name in ("LICENSE", "NOTICE", "README.md", "pyproject.toml"):
        member = archive.extractfile(members[root + "/" + name])
        if member is None or member.read() != (source_root / name).read_bytes():
            raise ArtifactValidationError(display, "sdist.source_hash")
    setup = archive.extractfile(members[root + "/setup.cfg"])
    if setup is None or setup.read() != b"[egg_info]\ntag_build = \ntag_date = 0\n\n":
        raise ArtifactValidationError(display, "sdist.setup_metadata")
    egg_root = root + "/src/nano_aural_runtime.egg-info/"
    egg_payloads = {}
    for name in _SDIST_EGG_INFO_FILES:
        member = archive.extractfile(members[egg_root + name])
        if member is None:
            raise ArtifactValidationError(display, "sdist.egg_info_membership")
        egg_payloads[name] = member.read()
    if egg_payloads["PKG-INFO"] != metadata_payload:
        raise ArtifactValidationError(display, "sdist.egg_info_metadata")
    if egg_payloads["entry_points.txt"] != _expected_entry_points(contract):
        raise ArtifactValidationError(display, "sdist.egg_info_entry_points")
    if egg_payloads["requires.txt"] != _expected_requires(contract):
        raise ArtifactValidationError(display, "sdist.egg_info_requires")
    if egg_payloads["top_level.txt"] != _expected_top_level():
        raise ArtifactValidationError(display, "sdist.egg_info_top_level")
    if egg_payloads["dependency_links.txt"] != b"\n":
        raise ArtifactValidationError(display, "sdist.egg_info_dependency_links")
    try:
        sources = tuple(egg_payloads["SOURCES.txt"].decode("utf-8").splitlines())
    except UnicodeDecodeError:
        raise ArtifactValidationError(display, "sdist.egg_info_sources") from None
    expected_sources = {"LICENSE", "NOTICE", "README.md", "pyproject.toml"}
    expected_sources.update("src/" + name for name in expected)
    expected_sources.update(
        "src/nano_aural_runtime.egg-info/" + name for name in _SDIST_EGG_INFO_FILES
    )
    if len(sources) != len(set(sources)) or set(sources) != expected_sources:
        raise ArtifactValidationError(display, "sdist.egg_info_sources")
    expected_files = {
        root + "/LICENSE",
        root + "/NOTICE",
        root + "/PKG-INFO",
        root + "/README.md",
        root + "/pyproject.toml",
        root + "/setup.cfg",
        *(root + "/src/" + name for name in expected),
        *(egg_root + name for name in _SDIST_EGG_INFO_FILES),
    }
    actual_files = {name for name, member in members.items() if member.isfile()}
    if actual_files != expected_files:
        raise ArtifactValidationError(display, "sdist.source_membership")


def _declared_requirements(root: Path) -> Tuple[_Requirement, ...]:
    pyproject = root / "pyproject.toml"
    requirements = []
    for group, key in (
        ("build", ("build-system", "requires")),
        ("runtime", ("project", "dependencies")),
    ):
        for raw in _toml_array(pyproject, *key):
            requirements.append(_requirement(group, raw))
    for group, raw in _toml_optional_arrays(pyproject):
        requirements.append(_requirement("optional:" + group, raw))
    container_requirements = root / "ops" / "requirements-api.txt"
    for raw in _requirements_lines(container_requirements):
        if raw and not raw.startswith("-"):
            declaration, archive_hashes = _locked_requirement(raw)
            requirements.append(
                _requirement("container:api", declaration, archive_hashes=archive_hashes)
            )
    return tuple(sorted(requirements, key=lambda item: (item.group, item.name, item.requirement)))


def _dependency_lock_findings(root: Path) -> Tuple[Finding, ...]:
    path = root / "ops" / "requirements-api.txt"
    findings = set()
    requirements = _requirements_lines(path)
    if "--require-hashes" not in requirements:
        findings.add(Finding("ops/requirements-api.txt", "dependency.require_hashes_missing"))
    if "--only-binary=:all:" not in requirements:
        findings.add(Finding("ops/requirements-api.txt", "dependency.only_binary_missing"))
    for requirement in requirements:
        if not requirement:
            continue
        if requirement.startswith("-"):
            if requirement not in ("--only-binary=:all:", "--require-hashes"):
                findings.add(Finding("ops/requirements-api.txt", "dependency.unsupported_option"))
            continue
        declaration, archive_hashes = _locked_requirement(requirement)
        if (
            re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]*(?:\[[A-Za-z0-9._,-]+\])?==[^,;*\s]+",
                declaration,
            )
            is None
        ):
            findings.add(Finding("ops/requirements-api.txt", "dependency.not_exactly_pinned"))
        if not archive_hashes:
            findings.add(Finding("ops/requirements-api.txt", "dependency.archive_hash_missing"))
        option_tokens = shlex.split(requirement)[1:]
        hash_tokens = [token for token in option_tokens if token.startswith("--hash=")]
        if len(hash_tokens) != len(archive_hashes):
            findings.add(Finding("ops/requirements-api.txt", "dependency.archive_hash_invalid"))
        if len(hash_tokens) != len(option_tokens):
            findings.add(Finding("ops/requirements-api.txt", "dependency.unsupported_option"))
    return tuple(sorted(findings))


def _requirements_lines(path: Path) -> Tuple[str, ...]:
    values = []
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.split("#", 1)[0].strip()
        if not stripped and not current:
            continue
        continued = stripped.endswith("\\")
        part = stripped[:-1].strip() if continued else stripped
        current = (current + " " + part).strip()
        if not continued:
            values.append(current)
            current = ""
    if current:
        raise ValueError("requirements file ends in an incomplete continuation")
    return tuple(values)


def _locked_requirement(raw: str) -> Tuple[str, Tuple[str, ...]]:
    tokens = shlex.split(raw)
    if not tokens:
        raise ValueError("locked requirement is empty")
    hashes = []
    for token in tokens[1:]:
        matched = re.fullmatch(r"--hash=sha256:([0-9a-f]{64})", token)
        if matched is not None:
            hashes.append(matched.group(1))
    return tokens[0], tuple(sorted(set(hashes)))


def _requirement(
    group: str,
    raw: str,
    *,
    archive_hashes: Tuple[str, ...] = (),
) -> _Requirement:
    matched = re.match(r"\s*([A-Za-z0-9][A-Za-z0-9._-]*)", raw)
    if matched is None:
        raise ValueError("declared requirement name is invalid")
    name = matched.group(1).lower().replace("_", "-").replace(".", "-")
    return _Requirement(group, raw, name, archive_hashes)


def _toml_array(path: Path, section: str, key: str) -> Tuple[str, ...]:
    return tuple(_toml_arrays(path).get((section, key), ()))


def _toml_optional_arrays(path: Path) -> Tuple[Tuple[str, str], ...]:
    result = []
    for (section, key), values in _toml_arrays(path).items():
        if section == "project.optional-dependencies":
            result.extend((key, value) for value in values)
    return tuple(result)


def _toml_arrays(path: Path) -> Mapping[Tuple[str, str], Tuple[str, ...]]:
    section = ""
    active: Optional[Tuple[str, str]] = None
    values: dict[Tuple[str, str], list[str]] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line.startswith("[") and line.endswith("]"):
            section = line[1:-1]
            active = None
            continue
        if active is None:
            matched = re.match(r"([A-Za-z0-9_-]+)\s*=\s*\[(.*)$", line)
            if matched is None:
                continue
            active = (section, matched.group(1))
            values.setdefault(active, [])
            remainder = matched.group(2)
        else:
            remainder = line
        for encoded in re.findall(r'"(?:[^"\\]|\\.)*"', remainder):
            decoded = json.loads(encoded)
            if not isinstance(decoded, str):
                raise ValueError("TOML requirement value must be a string")
            values[active].append(decoded)
        if "]" in remainder:
            active = None
    return {key: tuple(items) for key, items in values.items()}


def _toml_section_lines(text: str, name: str) -> Tuple[str, ...]:
    marker = "[{0}]".format(name)
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == marker) + 1
    except StopIteration:
        raise ValueError("release project section is missing") from None
    result = []
    for line in lines[start:]:
        if line.strip().startswith("["):
            break
        if line.strip() and not line.lstrip().startswith("#"):
            result.append(line.strip())
    return tuple(result)


def _literal_string(lines: Sequence[str], key: str) -> str:
    pattern = re.compile(r'{0}\s*=\s*"([^"\\]*)"'.format(re.escape(key)))
    matches = [matched.group(1) for line in lines if (matched := pattern.fullmatch(line))]
    if len(matches) != 1:
        raise ValueError("release project string is missing or ambiguous")
    return matches[0]


def _literal_mapping(lines: Sequence[str]) -> Tuple[Tuple[str, str], ...]:
    result = []
    for line in lines:
        matched = re.fullmatch(r'([A-Za-z0-9_-]+)\s*=\s*"([^"\\]*)"', line)
        if matched is None:
            raise ValueError("release project mapping is invalid")
        result.append((matched.group(1), matched.group(2)))
    if len(result) != len({name for name, _value in result}):
        raise ValueError("release project mapping is duplicated")
    return tuple(result)


def _release_contract(root: Path) -> _ReleaseContract:
    pyproject = root / "pyproject.toml"
    text = pyproject.read_text(encoding="utf-8")
    project = _toml_section_lines(text, "project")
    author_line = next((line for line in project if line.startswith("authors = ")), "")
    author = re.fullmatch(r'authors\s*=\s*\[\{name\s*=\s*"([^"\\]*)"\}\]', author_line)
    build = _toml_array(pyproject, "build-system", "requires")
    if len(build) != 1:
        raise ValueError("release build dependency contract is invalid")
    build_match = re.fullmatch(r"setuptools==([A-Za-z0-9][A-Za-z0-9._+]*)", build[0])
    if build_match is None or author is None:
        raise ValueError("release project contract is invalid")
    if _toml_array(pyproject, "project", "dependencies"):
        raise ValueError("base release dependency contract must be empty")
    optional: dict[str, list[str]] = {}
    for group, requirement in _toml_optional_arrays(pyproject):
        optional.setdefault(group, []).append(requirement)
    return _ReleaseContract(
        name=_literal_string(project, "name"),
        version=_literal_string(project, "version"),
        description=_literal_string(project, "description"),
        requires_python=_literal_string(project, "requires-python"),
        license_expression=_literal_string(project, "license"),
        author=author.group(1),
        classifiers=_toml_array(pyproject, "project", "classifiers"),
        urls=_literal_mapping(_toml_section_lines(text, "project.urls")),
        scripts=_literal_mapping(_toml_section_lines(text, "project.scripts")),
        optional_dependencies=tuple(
            (group, tuple(requirements)) for group, requirements in optional.items()
        ),
        build_backend_version=build_match.group(1),
    )


def _canonical_specifier(value: str) -> str:
    return ",".join(sorted(part.strip() for part in value.split(",") if part.strip()))


def _canonical_dependency(value: str) -> str:
    matched = re.fullmatch(r"([A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?)(.*)", value)
    if matched is None:
        raise ValueError("release dependency form is unsupported")
    return matched.group(1) + _canonical_specifier(matched.group(2))


def _expected_metadata_headers(contract: _ReleaseContract) -> Mapping[str, Tuple[str, ...]]:
    requires_dist = []
    for extra, dependencies in contract.optional_dependencies:
        for dependency in dependencies:
            requires_dist.append(
                '{0}; extra == "{1}"'.format(_canonical_dependency(dependency), extra)
            )
    return {
        "Metadata-Version": ("2.4",),
        "Name": (contract.name,),
        "Version": (contract.version,),
        "Summary": (contract.description,),
        "Author": (contract.author,),
        "License-Expression": (contract.license_expression,),
        "Project-URL": tuple("{0}, {1}".format(name, url) for name, url in contract.urls),
        "Classifier": contract.classifiers,
        "Requires-Python": (_canonical_specifier(contract.requires_python),),
        "Description-Content-Type": ("text/markdown",),
        "License-File": ("LICENSE", "NOTICE"),
        "Provides-Extra": tuple(name for name, _dependencies in contract.optional_dependencies),
        "Requires-Dist": tuple(requires_dist),
        "Dynamic": ("license-file",),
    }


def _validate_release_metadata(
    display: str,
    payload: bytes,
    contract: _ReleaseContract,
    source_root: Path,
    kind: str,
) -> None:
    _headers, separator, body = payload.partition(b"\n\n")
    if not separator or body != (source_root / "README.md").read_bytes():
        raise ArtifactValidationError(display, kind + ".readme_metadata")
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(payload)
    except (TypeError, ValueError):
        raise ArtifactValidationError(display, kind + ".metadata_schema") from None
    observed: dict[str, list[str]] = {}
    for name, value in message.raw_items():
        observed.setdefault(name, []).append(value)
    if {name: tuple(values) for name, values in observed.items()} != _expected_metadata_headers(
        contract
    ):
        raise ArtifactValidationError(display, kind + ".release_metadata")


def _expected_entry_points(contract: _ReleaseContract) -> bytes:
    lines = ["[console_scripts]"]
    lines.extend("{0} = {1}".format(name, target) for name, target in contract.scripts)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _expected_top_level() -> bytes:
    return ("\n".join(sorted(_PACKAGE_ROOTS)) + "\n").encode("utf-8")


def _expected_requires(contract: _ReleaseContract) -> bytes:
    sections = []
    for extra, dependencies in sorted(contract.optional_dependencies):
        values = "\n".join(_canonical_dependency(dependency) for dependency in dependencies)
        sections.append("[{0}]\n{1}\n".format(extra, values))
    return ("\n" + "\n".join(sections)).encode("utf-8")


def _project_metadata(path: Path) -> Mapping[str, str]:
    text = path.read_text(encoding="utf-8")
    project = text.split("[project]", 1)[1].split("\n[", 1)[0]
    result = {}
    for key in ("name", "version"):
        matched = re.search(r"^" + key + r'\s*=\s*"([^"]+)"', project, re.MULTILINE)
        if matched is None:
            raise ValueError("project metadata is incomplete")
        result[key] = matched.group(1)
    license_match = re.search(r'license\s*=\s*\{\s*text\s*=\s*"([^"]+)"\s*\}', project)
    license_file = re.search(r'license\s*=\s*\{\s*file\s*=\s*"([^"]+)"\s*\}', project)
    license_string = re.search(r'^license\s*=\s*"([^"]+)"', project, re.MULTILINE)
    if license_string is not None:
        result["license"] = license_string.group(1)
    elif license_match is not None:
        result["license"] = license_match.group(1)
    elif license_file is not None and "Apache Software License" in project:
        result["license"] = "Apache-2.0"
    else:
        raise ValueError("project license metadata is incomplete")
    return result


def _distribution_license(metadata: Mapping[str, str]) -> Optional[str]:
    value = metadata.get("License-Expression") or metadata.get("License")
    if value and value.strip() and value.strip().upper() != "UNKNOWN":
        return value.strip()
    get_all = getattr(metadata, "get_all", None)
    candidate = get_all("Classifier") if callable(get_all) else ()
    classifiers = (
        tuple(item for item in candidate if isinstance(item, str))
        if isinstance(candidate, (tuple, list))
        else ()
    )
    licenses = sorted(
        item.split(" :: ")[-1] for item in classifiers or () if item.startswith("License :: ")
    )
    return "; ".join(licenses) or None


def _file_records(root: Path, paths: Iterable[Path]) -> Tuple[Mapping[str, object], ...]:
    records = []
    for path in sorted(set(paths), key=lambda item: item.as_posix()):
        if path.is_symlink() or not path.is_file():
            continue
        records.append(
            {
                "file": path.relative_to(root).as_posix(),
                "sha256": sha256_file(path),
                "size_bytes": path.stat().st_size,
            }
        )
    return tuple(records)


def _container_manifest(root: Path, dockerfile: Path) -> Mapping[str, object]:
    instructions = _docker_instructions(dockerfile)
    base_images = []
    copy_sources = []
    declared_images, image_findings = _declared_image_references(root)
    findings = set(image_findings)
    dockerfile_display = dockerfile.relative_to(root).as_posix()
    for instruction, arguments in instructions:
        if instruction == "FROM":
            reference = arguments.split()[0]
            name, tag = _container_reference(reference)
            digest = reference.split("@sha256:", 1)[1] if "@sha256:" in reference else None
            if digest is None:
                findings.add(Finding(dockerfile_display, "container.base_image_not_digest_pinned"))
            elif re.fullmatch(r"[0-9a-f]{64}", digest) is None:
                findings.add(Finding(dockerfile_display, "container.base_image_digest_invalid"))
            base_images.append(
                {
                    "name": name,
                    "version": tag,
                    "digest": digest,
                    "license": None,
                }
            )
        elif instruction in ("COPY", "ADD"):
            sources = _copy_sources(arguments)
            for source in sources:
                if "://" in source:
                    findings.add(Finding(dockerfile_display, "container.remote_add"))
                    continue
                source_path = PurePosixPath(source)
                source_parts = source.split("/")
                if (
                    source_path.is_absolute()
                    or "\\" in source
                    or any(part in ("", ".", "..") for part in source_parts)
                ):
                    findings.add(Finding(dockerfile_display, "container.unsafe_copy_source"))
                    continue
                candidate = root / source
                if candidate.is_symlink():
                    findings.add(Finding(source_path.as_posix(), "container.symlink_input"))
                    continue
                if candidate.is_dir():
                    copy_sources.extend(_regular_files(candidate))
                elif candidate.is_file():
                    copy_sources.append(candidate)
                else:
                    findings.add(Finding(dockerfile_display, "container.missing_copy_source"))
                    continue
                checked = (candidate,) if candidate.is_file() else tuple(candidate.rglob("*"))
                for item in checked:
                    relative = item.relative_to(root)
                    display = relative.as_posix()
                    if item.is_symlink():
                        findings.add(Finding(display, "container.symlink_input"))
                        continue
                    denied = _denied_path(relative)
                    if denied is not None:
                        findings.add(Finding(display, "container." + denied))
    return {
        "dockerfile": {
            "file": dockerfile_display,
            "sha256": sha256_file(dockerfile),
        },
        "base_images": tuple(base_images),
        "declared_images": declared_images,
        "copied_inputs": _file_records(root, copy_sources),
        "findings": tuple(item.to_dict() for item in sorted(findings)),
        "build_validation": "UNRUN",
    }


def _declared_image_references(
    root: Path,
) -> Tuple[Tuple[Mapping[str, object], ...], Tuple[Finding, ...]]:
    records = []
    findings = set()
    compose_path = root / "compose.yaml"
    if compose_path.is_file():
        try:
            compose = json.loads(compose_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            findings.add(Finding("compose.yaml", "container.image_manifest_invalid"))
        else:
            services = compose.get("services") if isinstance(compose, Mapping) else None
            if not isinstance(services, Mapping):
                findings.add(Finding("compose.yaml", "container.image_manifest_invalid"))
            else:
                for service_name in sorted(services):
                    service = services[service_name]
                    if isinstance(service, Mapping) and "image" in service:
                        reference = service["image"]
                        if not isinstance(reference, str) or not reference:
                            findings.add(
                                Finding("compose.yaml", "container.image_reference_invalid")
                            )
                            continue
                        records.append(
                            _declared_image_record(
                                "compose.yaml",
                                "services.{0}.image".format(service_name),
                                reference,
                                findings,
                            )
                        )

    workflow_path = root / ".github" / "workflows" / "ci.yml"
    if workflow_path.is_file():
        try:
            workflow = workflow_path.read_text(encoding="utf-8")
        except OSError:
            findings.add(Finding(".github/workflows/ci.yml", "container.image_manifest_invalid"))
        else:
            for index, matched in enumerate(
                re.finditer(r"(?m)^[ \t]+image:[ \t]*([^#\r\n]+)", workflow)
            ):
                try:
                    values = shlex.split(matched.group(1).strip())
                except ValueError:
                    findings.add(
                        Finding(".github/workflows/ci.yml", "container.image_reference_invalid")
                    )
                    continue
                if len(values) != 1:
                    findings.add(
                        Finding(".github/workflows/ci.yml", "container.image_reference_invalid")
                    )
                    continue
                records.append(
                    _declared_image_record(
                        ".github/workflows/ci.yml",
                        "image[{0}]".format(index),
                        values[0],
                        findings,
                    )
                )
    return (
        tuple(sorted(records, key=lambda item: (str(item["file"]), str(item["location"])))),
        tuple(sorted(findings)),
    )


def _declared_image_record(
    file: str,
    location: str,
    reference: str,
    findings: set[Finding],
) -> Mapping[str, object]:
    name, tag = _container_reference(reference)
    digest = reference.split("@sha256:", 1)[1] if "@sha256:" in reference else None
    if digest is None:
        findings.add(Finding(file, "container.image_not_digest_pinned"))
    elif re.fullmatch(r"[0-9a-f]{64}", digest) is None:
        findings.add(Finding(file, "container.image_digest_invalid"))
    return {
        "file": file,
        "location": location,
        "name": name,
        "version": tag,
        "digest": digest,
    }


def _docker_instructions(path: Path) -> Tuple[Tuple[str, str], ...]:
    logical = []
    current = ""
    for raw in path.read_text(encoding="utf-8").splitlines():
        stripped = raw.strip()
        if not stripped or stripped.startswith("#"):
            continue
        current += (" " if current else "") + stripped.rstrip("\\").strip()
        if stripped.endswith("\\"):
            continue
        instruction, _, arguments = current.partition(" ")
        logical.append((instruction.upper(), arguments.strip()))
        current = ""
    if current:
        raise ValueError("Dockerfile ends in an incomplete instruction")
    return tuple(logical)


def _copy_sources(arguments: str) -> Tuple[str, ...]:
    remaining = arguments
    while remaining.startswith("--"):
        _flag, _separator, remaining = remaining.partition(" ")
        remaining = remaining.lstrip()
    if remaining.startswith("["):
        parsed = json.loads(remaining)
        if not isinstance(parsed, list) or not all(isinstance(item, str) for item in parsed):
            raise ValueError("Docker COPY JSON form is invalid")
        values = parsed
    else:
        values = shlex.split(remaining)
    if len(values) < 2:
        raise ValueError("Docker COPY requires source and destination")
    return tuple(values[:-1])


def _container_reference(reference: str) -> Tuple[str, Optional[str]]:
    without_digest = reference.split("@", 1)[0]
    final = without_digest.rsplit("/", 1)[-1]
    if ":" in final:
        name, tag = without_digest.rsplit(":", 1)
        return name, tag
    return without_digest, None


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        _json_value(value), sort_keys=True, separators=(",", ":"), ensure_ascii=True
    ).encode("utf-8")


def _fsync_directory(descriptor: int) -> None:
    os.fsync(descriptor)


def _publish_evidence_set(outputs: Sequence[Tuple[Path, bytes]]) -> None:
    """Publish a complete evidence set privately, exclusively, and atomically as a set."""

    if not outputs:
        return
    prepared = []
    target_keys = set()
    directories: dict[Path, int] = {}
    temporaries: list[Tuple[int, str]] = []
    created: list[Tuple[int, str]] = []
    try:
        # Preflight every final target before creating any temporary or final
        # file, so a known conflict cannot leave half of the evidence set.
        for raw_path, payload in outputs:
            if not raw_path.is_absolute() or raw_path.name in ("", ".", ".."):
                raise ValueError("release security evidence output must be an absolute file")
            parent = raw_path.parent.resolve(strict=True)
            if raw_path.parent != parent:
                raise ValueError("release security evidence output parent must be canonical")
            target = (parent, raw_path.name)
            if target in target_keys:
                raise ValueError("release security evidence outputs must be different")
            target_keys.add(target)
            if parent not in directories:
                directories[parent] = os.open(
                    parent,
                    os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
                )
            directory = directories[parent]
            try:
                os.stat(raw_path.name, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise ValueError("release security evidence output already exists")
            prepared.append((directory, raw_path.name, payload))

        for directory, _name, payload in prepared:
            temporary = ".nano-aural-release-security-{0}".format(secrets.token_hex(12))
            descriptor = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory,
            )
            temporaries.append((directory, temporary))
            try:
                with os.fdopen(descriptor, "wb", closefd=False) as handle:
                    handle.write(payload)
                    handle.flush()
                    os.fsync(handle.fileno())
            finally:
                os.close(descriptor)

        for (directory, name, _payload), (_temp_directory, temporary) in zip(prepared, temporaries):
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError:
                raise ValueError("release security evidence output already exists") from None
            created.append((directory, name))
        for directory in directories.values():
            _fsync_directory(directory)

        for directory, temporary in temporaries:
            os.unlink(temporary, dir_fd=directory)
        temporaries.clear()
        for directory in directories.values():
            _fsync_directory(directory)
    except Exception:
        rollback_failed = False
        for directory, name in reversed(created):
            try:
                os.unlink(name, dir_fd=directory)
            except FileNotFoundError:
                pass
            except OSError:
                rollback_failed = True
        for directory, temporary in temporaries:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            except OSError:
                rollback_failed = True
        for directory in directories.values():
            try:
                _fsync_directory(directory)
            except OSError:
                rollback_failed = True
        if rollback_failed:
            raise OSError("release security evidence rollback failed") from None
        raise
    finally:
        for directory in directories.values():
            os.close(directory)


def _write_exclusive(path: Path, payload: bytes) -> None:
    _publish_evidence_set(((path, payload),))


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--wheel", action="append", type=Path, default=[])
    parser.add_argument("--sdist", action="append", type=Path, default=[])
    parser.add_argument("--output", type=Path)
    parser.add_argument(
        "--source-date-epoch",
        type=int,
        help="explicit reproducible creation time required to generate SPDX",
    )
    parser.add_argument("--spdx-output", type=Path)
    options = parser.parse_args(argv)
    try:
        evidence = audit_release(
            options.root,
            wheels=options.wheel,
            sdists=options.sdist,
            source_date_epoch=options.source_date_epoch,
        )
        encoded = _canonical_json(evidence) + b"\n"
        spdx_encoded: Optional[bytes] = None
        if options.spdx_output is not None:
            if options.output is None:
                raise ValueError("SPDX output requires an internal evidence output")
            standard = evidence["standard_sbom"]
            if not isinstance(standard, Mapping) or standard.get("status") != "GENERATED":
                raise ValueError("SPDX output requires source_date_epoch")
            spdx_encoded = _canonical_json(standard["document"]) + b"\n"
            if (
                options.output is not None
                and options.output.resolve() == options.spdx_output.resolve()
            ):
                raise ValueError("internal and SPDX evidence outputs must be different")
        if options.output is None:
            sys.stdout.buffer.write(encoded)
        else:
            outputs = [(options.output, encoded)]
            if options.spdx_output is not None and spdx_encoded is not None:
                outputs.append((options.spdx_output, spdx_encoded))
            _publish_evidence_set(outputs)
    except (ArtifactValidationError, OSError, TypeError, ValueError):
        # Never echo parser payloads, filesystem paths, archive member values,
        # or secret scanner matches at this public boundary.
        sys.stderr.write("release security audit failed; inspect non-sensitive test diagnostics\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
