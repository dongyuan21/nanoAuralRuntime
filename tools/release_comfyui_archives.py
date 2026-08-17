"""Build deterministic, independently removable ComfyUI source archives."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import re
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_FIXED_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


class ComfyUIArchiveError(RuntimeError):
    """An optional frontend source tree or archive is unsafe to publish."""


@dataclass(frozen=True)
class _ArchiveSpec:
    distribution: str
    package_name: str
    source_dir: str
    files: Tuple[str, ...]


@dataclass(frozen=True)
class ArchiveEvidence:
    filename: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> Mapping[str, object]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


_SPECS = (
    _ArchiveSpec(
        "nano-aural-comfyui-embedded",
        "nano_aural_comfyui_embedded",
        "integrations/comfyui",
        (
            "README.md",
            "__init__.py",
            "bootstrap.py",
            "embedded.py",
            "examples/embedded_controlfoley_t2a.json",
        ),
    ),
    _ArchiveSpec(
        "nano-aural-comfyui-remote",
        "nano_aural_comfyui_remote",
        "integrations/comfyui_remote",
        (
            "README.md",
            "__init__.py",
            "bootstrap.py",
            "examples/remote_controlfoley_v2a.json",
            "nodes.py",
        ),
    ),
    _ArchiveSpec(
        "nano-aural-comfyui-compat",
        "nano_aural_comfyui_compat",
        "integrations/comfyui_compat",
        (
            "__init__.py",
            "coexistence.py",
            "examples/official_controlfoley_t2a_ab.json",
        ),
    ),
)


def _project_version(root: Path) -> str:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    in_project = False
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("["):
            in_project = stripped == "[project]"
            continue
        if in_project:
            match = re.fullmatch(r'version\s*=\s*"([0-9A-Za-z.+!-]+)"', stripped)
            if match is not None:
                return match.group(1)
    raise ComfyUIArchiveError("pyproject.toml must declare one literal project version")


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _source_payload(root: Path, spec: _ArchiveSpec) -> Mapping[str, bytes]:
    source = root / spec.source_dir
    if not source.is_dir() or source.is_symlink():
        raise ComfyUIArchiveError("optional frontend source directory is absent or unsafe")
    actual = set()
    for path in source.rglob("*"):
        if path.is_dir() and not path.is_symlink():
            continue
        if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
            continue
        if path.is_symlink() or not path.is_file():
            raise ComfyUIArchiveError("optional frontend may contain regular files only")
        actual.add(path.relative_to(source).as_posix())
    if actual != set(spec.files):
        raise ComfyUIArchiveError("optional frontend files differ from the release allowlist")
    payload: dict[str, bytes] = {}
    for name in spec.files:
        archive_name = PurePosixPath(spec.package_name, name).as_posix()
        payload[archive_name] = (source / name).read_bytes()
    for name in ("LICENSE", "NOTICE"):
        path = root / name
        if not path.is_file() or path.is_symlink():
            raise ComfyUIArchiveError("release license metadata is absent or unsafe")
        payload[PurePosixPath(spec.package_name, name).as_posix()] = path.read_bytes()
    return payload


def _manifest(spec: _ArchiveSpec, version: str, payload: Mapping[str, bytes]) -> bytes:
    files = [
        {
            "path": name,
            "sha256": _sha256(content),
            "size_bytes": len(content),
        }
        for name, content in sorted(payload.items())
    ]
    return (
        json.dumps(
            {
                "contains_controlfoley_source": False,
                "contains_model_weights": False,
                "distribution": spec.distribution,
                "files": files,
                "hardware_evidence": "not_included",
                "package_name": spec.package_name,
                "schema_version": 1,
                "source_dir": spec.source_dir,
                "version": version,
            },
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, _FIXED_TIMESTAMP)
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    return info


def _archive_bytes(root: Path, spec: _ArchiveSpec, version: str) -> Tuple[str, bytes]:
    payload = dict(_source_payload(root, spec))
    manifest_name = PurePosixPath(spec.package_name, "RELEASE-MANIFEST.json").as_posix()
    payload[manifest_name] = _manifest(spec, version, payload)
    raw = io.BytesIO()
    with zipfile.ZipFile(raw, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, content in sorted(payload.items()):
            archive.writestr(_zip_info(name), content)
    return "{0}-{1}.zip".format(spec.distribution, version), raw.getvalue()


def _rollback_publication(
    output: Path,
    temporary_paths: Sequence[Path],
    created: Sequence[Tuple[Path, int, int]],
) -> None:
    mutated = False
    for temporary in temporary_paths:
        try:
            temporary.unlink()
            mutated = True
        except FileNotFoundError:
            pass
    for destination, expected_device, expected_inode in reversed(created):
        try:
            information = os.stat(str(destination), follow_symlinks=False)
            if (information.st_dev, information.st_ino) == (expected_device, expected_inode):
                destination.unlink()
                mutated = True
        except FileNotFoundError:
            pass
    if mutated:
        try:
            _fsync_directory(output)
        except OSError:
            pass


def _publish_collection(
    output: Path, artifacts: Sequence[Tuple[str, bytes]]
) -> Tuple[ArchiveEvidence, ...]:
    if len(artifacts) != len({name for name, _content in artifacts}):
        raise ComfyUIArchiveError("optional frontend archive names must be unique")
    destinations = tuple((output / name, content) for name, content in artifacts)
    if any(os.path.lexists(destination) for destination, _content in destinations):
        raise ComfyUIArchiveError("refusing to overwrite an existing optional frontend archive")
    temporary_paths: list[Path] = []
    staged: list[Tuple[Path, int, int]] = []
    created: list[Tuple[Path, int, int]] = []
    try:
        for _destination, content in destinations:
            descriptor, raw_temporary = tempfile.mkstemp(
                dir=str(output), prefix=".nano-aural-comfyui-publish-"
            )
            temporary = Path(raw_temporary)
            temporary_paths.append(temporary)
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
                os.fchmod(stream.fileno(), 0o644)
                information = os.fstat(stream.fileno())
            staged.append((temporary, information.st_dev, information.st_ino))
        for (destination, _content), (temporary, expected_device, expected_inode) in zip(
            destinations, staged
        ):
            os.link(temporary, destination)
            created.append((destination, expected_device, expected_inode))
            _verify_published_identity(destination, expected_device, expected_inode)
        for temporary, _device, _inode in staged:
            temporary.unlink()
        _fsync_directory(output)
    except FileExistsError as error:
        _rollback_publication(output, temporary_paths, created)
        raise ComfyUIArchiveError(
            "refusing to overwrite an existing optional frontend archive"
        ) from error
    except Exception:
        _rollback_publication(output, temporary_paths, created)
        raise
    return tuple(
        ArchiveEvidence(destination.name, _sha256(content), len(content))
        for destination, content in destinations
    )


def _verify_published_identity(
    destination: Path, expected_device: int, expected_inode: int
) -> None:
    information = os.stat(str(destination), follow_symlinks=False)
    if (information.st_dev, information.st_ino) != (expected_device, expected_inode):
        raise ComfyUIArchiveError("published optional frontend identity changed unexpectedly")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_comfyui_archives(
    output_dir: Path, *, project_root: Path = PROJECT_ROOT
) -> Tuple[ArchiveEvidence, ...]:
    """Build three byte-deterministic source archives from exact file manifests."""

    root = Path(project_root).resolve()
    output = Path(output_dir).resolve()
    if not output.is_dir() or output.is_symlink():
        raise ComfyUIArchiveError("output directory must be an existing regular directory")
    version = _project_version(root)
    artifacts = tuple(_archive_bytes(root, spec, version) for spec in _SPECS)
    return _publish_collection(output, artifacts)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build deterministic removable NanoAural ComfyUI source archives."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        artifacts = build_comfyui_archives(arguments.output_dir)
    except (ComfyUIArchiveError, OSError, ValueError, zipfile.BadZipFile):
        sys.stderr.write(
            "optional frontend archive build failed; inspect the source allowlist and output\n"
        )
        return 2
    print(
        json.dumps(
            {
                "artifacts": [artifact.to_dict() for artifact in artifacts],
                "hardware_evidence": "not_run",
                "release_gate": "blocked",
                "schema_version": 1,
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
