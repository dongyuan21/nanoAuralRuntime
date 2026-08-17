"""Build and audit the headless wheel and sdist without network access."""

from __future__ import annotations

import argparse
import base64
import csv
import gzip
import hashlib
import importlib.metadata
import io
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import zipfile
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from pathlib import Path, PurePosixPath
from typing import Mapping, Optional, Sequence, Tuple

PROJECT_ROOT = Path(__file__).resolve().parents[1]
_BUILD_BACKEND_VERSION = "82.0.1"
_ROOT_INPUTS = ("pyproject.toml", "README.md", "LICENSE", "NOTICE")
_PACKAGE_ROOTS = (
    "nano_aural_runtime",
    "nano_aural_runtime_cli",
    "nano_aural_runtime_controlfoley",
    "nano_aural_runtime_remote",
    "nano_aural_runtime_stable_audio_3",
    "nano_aural_runtime_workers",
)
_FORBIDDEN_NAMES = frozenset(
    (
        ".cache",
        ".env",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "artifacts",
        "benchmarks",
        "checkpoints",
        "comfyui",
        "comfyui_compat",
        "comfyui_remote",
        "evidence",
        "hf_cache",
        "integrations",
        "media",
        "model_weights",
        "outputs",
        "secrets",
        "tests",
        "weights",
    )
)
_SQL_FILES = frozenset(
    (
        "0001_durable_foundation.sql",
        "0002_verified_uploads.sql",
        "0003_queue_leases.sql",
        "0004_artifact_publications.sql",
        "0005_upload_staging_cleanups.sql",
    )
)


class ReleaseArtifactError(RuntimeError):
    """A source or built distribution violates the release allowlist."""


@dataclass(frozen=True)
class ArtifactEvidence:
    filename: str
    sha256: str
    size_bytes: int

    def to_dict(self) -> Mapping[str, object]:
        return {
            "filename": self.filename,
            "sha256": self.sha256,
            "size_bytes": self.size_bytes,
        }


@dataclass(frozen=True)
class _ProjectContract:
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


def _toml_section(text: str, name: str) -> Tuple[str, ...]:
    marker = "[{0}]".format(name)
    lines = text.splitlines()
    try:
        start = next(index for index, line in enumerate(lines) if line.strip() == marker) + 1
    except StopIteration as error:
        raise ReleaseArtifactError("pyproject.toml is missing required release metadata") from error
    result = []
    for line in lines[start:]:
        if line.strip().startswith("["):
            break
        if line.strip() and not line.lstrip().startswith("#"):
            result.append(line.strip())
    return tuple(result)


def _toml_string(lines: Sequence[str], key: str) -> str:
    pattern = re.compile(r'{0}\s*=\s*"([^"\\]*)"'.format(re.escape(key)))
    matches = [match.group(1) for line in lines if (match := pattern.fullmatch(line))]
    if len(matches) != 1:
        raise ReleaseArtifactError("pyproject.toml release strings must be literal and unique")
    return matches[0]


def _toml_string_array(lines: Sequence[str], key: str) -> Tuple[str, ...]:
    start_pattern = re.compile(r"{0}\s*=\s*\[(.*)".format(re.escape(key)))
    fragments = []
    collecting = False
    for line in lines:
        if not collecting:
            match = start_pattern.fullmatch(line)
            if match is None:
                continue
            collecting = True
            fragment = match.group(1)
        else:
            fragment = line
        if fragment.rstrip().endswith("]"):
            before, after = fragment.rsplit("]", 1)
            if after.strip():
                raise ReleaseArtifactError("pyproject.toml release arrays must be literal")
            fragments.append(before)
            break
        fragments.append(fragment)
    else:
        raise ReleaseArtifactError("pyproject.toml release array is incomplete")
    joined = "\n".join(fragments)
    values = tuple(re.findall(r'"([^"\\]*)"', joined))
    residue = re.sub(r'"[^"\\]*"', "", joined).replace(",", "").strip()
    if residue:
        raise ReleaseArtifactError("pyproject.toml release arrays must contain strings only")
    return values


def _toml_mapping(lines: Sequence[str]) -> Tuple[Tuple[str, str], ...]:
    result = []
    for line in lines:
        match = re.fullmatch(r'([A-Za-z0-9_-]+)\s*=\s*"([^"\\]*)"', line)
        if match is None:
            raise ReleaseArtifactError("pyproject.toml release mapping must contain strings only")
        result.append((match.group(1), match.group(2)))
    if len(result) != len({key for key, _value in result}):
        raise ReleaseArtifactError("pyproject.toml release mapping contains duplicate keys")
    return tuple(result)


def _project_contract(root: Path) -> _ProjectContract:
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    build_system = _toml_section(text, "build-system")
    if (
        _toml_string_array(build_system, "requires")
        != ("setuptools=={0}".format(_BUILD_BACKEND_VERSION),)
        or _toml_string(build_system, "build-backend") != "setuptools.build_meta"
    ):
        raise ReleaseArtifactError("pyproject.toml build backend differs from the release pin")
    project = _toml_section(text, "project")
    if _toml_string(project, "readme") != "README.md":
        raise ReleaseArtifactError("release README must be the declared project description")
    if _toml_string_array(project, "license-files") != ("LICENSE", "NOTICE"):
        raise ReleaseArtifactError("release license files must match the distribution contract")
    if _toml_string_array(project, "dependencies"):
        raise ReleaseArtifactError("the headless base distribution must not have dependencies")
    author_line = next((line for line in project if line.startswith("authors = ")), "")
    author_match = re.fullmatch(r'authors\s*=\s*\[\{name\s*=\s*"([^"\\]*)"\}\]', author_line)
    if author_match is None:
        raise ReleaseArtifactError("pyproject.toml must declare one literal release author")
    optional_lines = _toml_section(text, "project.optional-dependencies")
    optional_names = []
    for line in optional_lines:
        match = re.match(r"([a-z0-9-]+)\s*=", line)
        if match is not None:
            optional_names.append(match.group(1))
    if len(optional_names) != len(set(optional_names)):
        raise ReleaseArtifactError("pyproject.toml optional dependency keys must be unique")
    optional = tuple((name, _toml_string_array(optional_lines, name)) for name in optional_names)
    return _ProjectContract(
        name=_toml_string(project, "name"),
        version=_toml_string(project, "version"),
        description=_toml_string(project, "description"),
        requires_python=_toml_string(project, "requires-python"),
        license_expression=_toml_string(project, "license"),
        author=author_match.group(1),
        classifiers=_toml_string_array(project, "classifiers"),
        urls=_toml_mapping(_toml_section(text, "project.urls")),
        scripts=_toml_mapping(_toml_section(text, "project.scripts")),
        optional_dependencies=optional,
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _require_build_backend() -> None:
    try:
        raw = importlib.metadata.version("setuptools")
    except importlib.metadata.PackageNotFoundError as error:
        raise ReleaseArtifactError("declared setuptools build backend is unavailable") from error
    if raw != _BUILD_BACKEND_VERSION:
        raise ReleaseArtifactError("setuptools build backend must match the exact release pin")


def _safe_archive_name(raw: str) -> PurePosixPath:
    if "\\" in raw:
        raise ReleaseArtifactError("distribution contains a non-portable archive path")
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ReleaseArtifactError("distribution contains an unsafe archive path")
    if any(part.casefold() in _FORBIDDEN_NAMES for part in path.parts):
        raise ReleaseArtifactError("distribution contains a forbidden release path")
    return path


def _source_files(root: Path) -> Mapping[str, bytes]:
    source = root / "src"
    expected: dict[str, bytes] = {}
    for package_name in _PACKAGE_ROOTS:
        package = source / package_name
        if not package.is_dir() or package.is_symlink():
            raise ReleaseArtifactError("required source package is absent or unsafe")
        for path in sorted(package.rglob("*")):
            if path.is_dir() and not path.is_symlink():
                continue
            relative = path.relative_to(source).as_posix()
            if path.is_symlink() or not path.is_file():
                raise ReleaseArtifactError("source packages may contain regular files only")
            if "__pycache__" in path.parts or path.suffix in {".pyc", ".pyo"}:
                continue
            is_sql = relative.startswith("nano_aural_runtime/durable/sql/")
            if path.suffix != ".py" and not (is_sql and path.suffix == ".sql"):
                raise ReleaseArtifactError("source package contains a non-allowlisted file")
            if is_sql and path.name not in _SQL_FILES:
                raise ReleaseArtifactError("source package contains an unknown migration resource")
            expected[relative] = path.read_bytes()
    observed_sql = {
        PurePosixPath(name).name
        for name in expected
        if name.startswith("nano_aural_runtime/durable/sql/")
    }
    if observed_sql != _SQL_FILES:
        raise ReleaseArtifactError("packaged migration resource set is incomplete")
    return expected


def _copy_snapshot(root: Path, destination: Path) -> Mapping[str, bytes]:
    for name in _ROOT_INPUTS:
        source = root / name
        if not source.is_file() or source.is_symlink():
            raise ReleaseArtifactError("required release metadata is absent or unsafe")
        shutil.copyfile(source, destination / name)
    files = _source_files(root)
    for relative, content in files.items():
        target = destination / "src" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)
    return files


def _backend_build(snapshot: Path, destination: Path, method: str) -> Path:
    code = (
        "from setuptools import build_meta; import sys; "
        "getattr(build_meta, sys.argv[1])(sys.argv[2])"
    )
    before = set(destination.iterdir())
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        environment.pop(name, None)
    environment["SOURCE_DATE_EPOCH"] = "315532800"
    completed = subprocess.run(
        [sys.executable, "-c", code, method, str(destination)],
        cwd=snapshot,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if completed.returncode != 0:
        raise ReleaseArtifactError("setuptools offline build failed")
    created = tuple(path for path in destination.iterdir() if path not in before and path.is_file())
    if len(created) != 1:
        raise ReleaseArtifactError("setuptools offline build returned an unexpected artifact set")
    return created[0]


def _canonical_specifier(value: str) -> str:
    return ",".join(sorted(part.strip() for part in value.split(",") if part.strip()))


def _canonical_dependency(value: str) -> str:
    match = re.fullmatch(r"([A-Za-z0-9_.-]+(?:\[[A-Za-z0-9_,.-]+\])?)(.*)", value)
    if match is None:
        raise ReleaseArtifactError("pyproject.toml contains an unsupported dependency form")
    return match.group(1) + _canonical_specifier(match.group(2))


def _expected_metadata_headers(contract: _ProjectContract) -> Mapping[str, Tuple[str, ...]]:
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


def _audit_metadata(content: bytes, contract: _ProjectContract, root: Path) -> None:
    _headers, separator, body = content.partition(b"\n\n")
    if not separator or body != (root / "README.md").read_bytes():
        raise ReleaseArtifactError("distribution metadata description differs from README.md")
    try:
        message = BytesParser(policy=policy.compat32).parsebytes(content)
    except (TypeError, ValueError) as error:
        raise ReleaseArtifactError("distribution metadata could not be parsed") from error
    observed: dict[str, list[str]] = {}
    for name, value in message.raw_items():
        observed.setdefault(name, []).append(value)
    expected = _expected_metadata_headers(contract)
    if {name: tuple(values) for name, values in observed.items()} != expected:
        raise ReleaseArtifactError("distribution metadata differs from pyproject.toml")


def _expected_entry_points(contract: _ProjectContract) -> bytes:
    lines = ["[console_scripts]"]
    lines.extend("{0} = {1}".format(name, target) for name, target in contract.scripts)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _expected_top_level() -> bytes:
    return ("\n".join(_PACKAGE_ROOTS) + "\n").encode("utf-8")


def _audit_record(members: Mapping[str, bytes], record_name: str) -> None:
    try:
        rows = tuple(csv.reader(io.StringIO(members[record_name].decode("utf-8"), newline="")))
    except (KeyError, UnicodeDecodeError, csv.Error) as error:
        raise ReleaseArtifactError("wheel RECORD could not be parsed") from error
    parsed: dict[str, Tuple[str, str]] = {}
    for row in rows:
        if len(row) != 3:
            raise ReleaseArtifactError("wheel RECORD rows must contain exactly three columns")
        raw_name, digest, size = row
        name = str(_safe_archive_name(raw_name))
        if name in parsed:
            raise ReleaseArtifactError("wheel RECORD contains duplicate paths")
        parsed[name] = (digest, size)
    if set(parsed) != set(members):
        raise ReleaseArtifactError("wheel RECORD does not describe every member exactly once")
    for name, content in members.items():
        digest, size = parsed[name]
        if name == record_name:
            if digest or size:
                raise ReleaseArtifactError("wheel RECORD self-entry must have empty hash and size")
            continue
        expected_digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=")
        if digest != "sha256=" + expected_digest.decode("ascii"):
            raise ReleaseArtifactError("wheel RECORD member hash is invalid")
        if size != str(len(content)):
            raise ReleaseArtifactError("wheel RECORD member size is invalid")


def _audit_wheel(
    path: Path, contract: _ProjectContract, sources: Mapping[str, bytes], root: Path
) -> bytes:
    dist_info = "nano_aural_runtime-{0}.dist-info".format(contract.version)
    allowed_metadata = frozenset(
        (
            "METADATA",
            "RECORD",
            "WHEEL",
            "entry_points.txt",
            "licenses/LICENSE",
            "licenses/NOTICE",
            "top_level.txt",
        )
    )
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        if len(names) != len(set(names)):
            raise ReleaseArtifactError("wheel contains duplicate members")
        members = {str(_safe_archive_name(name)): archive.read(name) for name in names}
    package_members = {
        name: content for name, content in members.items() if not name.startswith(dist_info + "/")
    }
    if package_members != sources:
        raise ReleaseArtifactError("wheel package files differ from the strict source allowlist")
    metadata_members = {
        name[len(dist_info) + 1 :]: content
        for name, content in members.items()
        if name.startswith(dist_info + "/")
    }
    if set(metadata_members) != allowed_metadata:
        raise ReleaseArtifactError(
            "wheel dist-info files differ from the strict metadata allowlist"
        )
    if metadata_members["entry_points.txt"] != _expected_entry_points(contract):
        raise ReleaseArtifactError("wheel console scripts differ from pyproject.toml")
    if metadata_members["top_level.txt"] != _expected_top_level():
        raise ReleaseArtifactError("wheel top-level packages differ from the release allowlist")
    expected_wheel = (
        "Wheel-Version: 1.0\n"
        "Generator: setuptools ({0})\n"
        "Root-Is-Purelib: true\n"
        "Tag: py3-none-any\n\n".format(_BUILD_BACKEND_VERSION)
    ).encode("utf-8")
    if metadata_members["WHEEL"] != expected_wheel:
        raise ReleaseArtifactError("wheel compatibility metadata differs from the release contract")
    metadata = metadata_members["METADATA"]
    _audit_metadata(metadata, contract, root)
    if metadata_members["licenses/LICENSE"] != (root / "LICENSE").read_bytes():
        raise ReleaseArtifactError("wheel LICENSE differs from the repository license")
    if metadata_members["licenses/NOTICE"] != (root / "NOTICE").read_bytes():
        raise ReleaseArtifactError("wheel NOTICE differs from the repository notice")
    _audit_record(members, dist_info + "/RECORD")
    return metadata


def _expected_requires(contract: _ProjectContract) -> bytes:
    sections = []
    for extra, dependencies in sorted(contract.optional_dependencies):
        values = "\n".join(_canonical_dependency(dependency) for dependency in dependencies)
        sections.append("[{0}]\n{1}\n".format(extra, values))
    return ("\n" + "\n".join(sections)).encode("utf-8")


def _audit_sdist(
    path: Path,
    contract: _ProjectContract,
    sources: Mapping[str, bytes],
    root: Path,
    wheel_metadata: bytes,
) -> None:
    prefix = "nano_aural_runtime-{0}".format(contract.version)
    allowed_root = frozenset(
        ("LICENSE", "NOTICE", "PKG-INFO", "README.md", "pyproject.toml", "setup.cfg")
    )
    allowed_egg_info = frozenset(
        (
            "PKG-INFO",
            "SOURCES.txt",
            "dependency_links.txt",
            "entry_points.txt",
            "requires.txt",
            "top_level.txt",
        )
    )
    files: dict[str, bytes] = {}
    seen = set()
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            name = str(_safe_archive_name(member.name))
            if name in seen:
                raise ReleaseArtifactError("sdist contains duplicate member names")
            seen.add(name)
            if member.isdir():
                continue
            if not member.isfile():
                raise ReleaseArtifactError("sdist may contain regular files only")
            extracted = archive.extractfile(member)
            if extracted is None:
                raise ReleaseArtifactError("sdist member could not be read")
            files[name] = extracted.read()
    if not files or any(not name.startswith(prefix + "/") for name in files):
        raise ReleaseArtifactError("sdist has an unexpected root directory")
    relative = {name[len(prefix) + 1 :]: content for name, content in files.items()}
    packaged = {
        name[len("src/") :]: content
        for name, content in relative.items()
        if name.startswith("src/") and ".egg-info/" not in name
    }
    if packaged != sources:
        raise ReleaseArtifactError("sdist package files differ from the strict source allowlist")
    for name in relative:
        if name in allowed_root:
            continue
        if name.startswith("src/nano_aural_runtime.egg-info/"):
            if name.split("/")[-1] in allowed_egg_info:
                continue
        if name.startswith("src/") and name[len("src/") :] in sources:
            continue
        raise ReleaseArtifactError("sdist contains a non-allowlisted file")
    for name in _ROOT_INPUTS:
        if relative.get(name) != (root / name).read_bytes():
            raise ReleaseArtifactError("sdist release metadata differs from repository bytes")
    if relative.get("setup.cfg") != b"[egg_info]\ntag_build = \ntag_date = 0\n\n":
        raise ReleaseArtifactError(
            "sdist generated configuration differs from the release contract"
        )
    egg_root = "src/nano_aural_runtime.egg-info/"
    if relative.get("PKG-INFO") != wheel_metadata:
        raise ReleaseArtifactError("sdist PKG-INFO differs from wheel METADATA")
    if relative.get(egg_root + "PKG-INFO") != wheel_metadata:
        raise ReleaseArtifactError("sdist egg-info metadata differs from wheel METADATA")
    _audit_metadata(wheel_metadata, contract, root)
    if relative.get(egg_root + "entry_points.txt") != _expected_entry_points(contract):
        raise ReleaseArtifactError("sdist console scripts differ from pyproject.toml")
    if relative.get(egg_root + "requires.txt") != _expected_requires(contract):
        raise ReleaseArtifactError("sdist dependencies differ from pyproject.toml")
    if relative.get(egg_root + "top_level.txt") != _expected_top_level():
        raise ReleaseArtifactError("sdist top-level packages differ from the release allowlist")
    if relative.get(egg_root + "dependency_links.txt") != b"\n":
        raise ReleaseArtifactError("sdist contains unexpected dependency links")
    sources_text = relative.get(egg_root + "SOURCES.txt", b"")
    try:
        source_entries = tuple(sources_text.decode("utf-8").splitlines())
    except UnicodeDecodeError as error:
        raise ReleaseArtifactError("sdist source manifest could not be parsed") from error
    expected_sources: set[str] = set(_ROOT_INPUTS)
    expected_sources.update("src/" + name for name in sources)
    expected_sources.update(
        egg_root + name
        for name in (
            "PKG-INFO",
            "SOURCES.txt",
            "dependency_links.txt",
            "entry_points.txt",
            "requires.txt",
            "top_level.txt",
        )
    )
    if len(source_entries) != len(set(source_entries)) or set(source_entries) != expected_sources:
        raise ReleaseArtifactError("sdist source manifest differs from the strict allowlist")
    for name in source_entries:
        _safe_archive_name(name)


def _normalize_sdist(path: Path) -> None:
    """Rewrite setuptools' content as a byte-reproducible source archive."""

    entries: list[tuple[str, bool, bytes]] = []
    seen = set()
    with tarfile.open(path, "r:gz") as source:
        for member in source.getmembers():
            name = str(_safe_archive_name(member.name))
            if name in seen:
                raise ReleaseArtifactError("sdist contains duplicate member names")
            seen.add(name)
            if member.isdir():
                entries.append((name, True, b""))
                continue
            if not member.isfile():
                raise ReleaseArtifactError("sdist may contain regular files only")
            extracted = source.extractfile(member)
            if extracted is None:
                raise ReleaseArtifactError("sdist member could not be read")
            entries.append((name, False, extracted.read()))
    temporary = path.with_name(path.name + ".normalized")
    with temporary.open("xb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=315532800) as compressed:
            with tarfile.open(fileobj=compressed, mode="w", format=tarfile.PAX_FORMAT) as target:
                for name, is_directory, content in sorted(entries):
                    information = tarfile.TarInfo(name)
                    information.gid = 0
                    information.gname = ""
                    information.mtime = 315532800
                    information.uid = 0
                    information.uname = ""
                    if is_directory:
                        information.mode = 0o755
                        information.type = tarfile.DIRTYPE
                        target.addfile(information)
                    else:
                        information.mode = 0o644
                        information.size = len(content)
                        information.type = tarfile.REGTYPE
                        target.addfile(information, io.BytesIO(content))
        raw.flush()
        os.fsync(raw.fileno())
    os.replace(temporary, path)


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
) -> Tuple[ArtifactEvidence, ...]:
    if len(artifacts) != len({name for name, _content in artifacts}):
        raise ReleaseArtifactError("release artifact names must be unique")
    destinations = tuple((output / name, content) for name, content in artifacts)
    if any(os.path.lexists(destination) for destination, _content in destinations):
        raise ReleaseArtifactError("refusing to overwrite an existing release artifact")
    temporary_paths: list[Path] = []
    staged: list[Tuple[Path, int, int]] = []
    created: list[Tuple[Path, int, int]] = []
    try:
        for _destination, content in destinations:
            descriptor, raw_temporary = tempfile.mkstemp(
                dir=str(output), prefix=".nano-aural-publish-"
            )
            temporary = Path(raw_temporary)
            temporary_paths.append(temporary)
            with os.fdopen(descriptor, "wb") as handle:
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
                os.fchmod(handle.fileno(), 0o644)
                information = os.fstat(handle.fileno())
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
        raise ReleaseArtifactError("refusing to overwrite an existing release artifact") from error
    except Exception:
        _rollback_publication(output, temporary_paths, created)
        raise
    return tuple(
        ArtifactEvidence(destination.name, _sha256(content), len(content))
        for destination, content in destinations
    )


def _verify_published_identity(
    destination: Path, expected_device: int, expected_inode: int
) -> None:
    information = os.stat(str(destination), follow_symlinks=False)
    if (information.st_dev, information.st_ino) != (expected_device, expected_inode):
        raise ReleaseArtifactError("published release artifact identity changed unexpectedly")


def _fsync_directory(directory: Path) -> None:
    descriptor = os.open(str(directory), os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def build_release_artifacts(
    output_dir: Path, *, project_root: Path = PROJECT_ROOT
) -> Tuple[ArtifactEvidence, ArtifactEvidence]:
    """Build audited distributions and publish them durably without overwrite."""

    root = Path(project_root).resolve()
    output = Path(output_dir).resolve()
    if not output.is_dir() or output.is_symlink():
        raise ReleaseArtifactError("output directory must be an existing regular directory")
    _require_build_backend()
    contract = _project_contract(root)
    with tempfile.TemporaryDirectory(prefix="nano-aural-release-") as directory:
        temporary = Path(directory)
        snapshot = temporary / "source"
        built = temporary / "built"
        snapshot.mkdir()
        built.mkdir()
        sources = _copy_snapshot(root, snapshot)
        wheel = _backend_build(snapshot, built, "build_wheel")
        sdist = _backend_build(snapshot, built, "build_sdist")
        _normalize_sdist(sdist)
        expected_names = {
            "nano_aural_runtime-{0}-py3-none-any.whl".format(contract.version),
            "nano_aural_runtime-{0}.tar.gz".format(contract.version),
        }
        if {wheel.name, sdist.name} != expected_names:
            raise ReleaseArtifactError("setuptools produced unexpected release artifact names")
        wheel_metadata = _audit_wheel(wheel, contract, sources, root)
        _audit_sdist(sdist, contract, sources, root, wheel_metadata)
        published = _publish_collection(
            output, ((wheel.name, wheel.read_bytes()), (sdist.name, sdist.read_bytes()))
        )
        if len(published) != 2:
            raise ReleaseArtifactError("release publication returned an unexpected artifact set")
        return published[0], published[1]


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Build audited nanoAuralRuntime wheel/sdist artifacts without network access."
    )
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    try:
        artifacts = build_release_artifacts(arguments.output_dir)
    except (OSError, ReleaseArtifactError, subprocess.SubprocessError):
        sys.stderr.write(
            "release artifact build failed; inspect the source allowlist and build environment\n"
        )
        return 2
    print(
        json.dumps(
            {
                "artifacts": [artifact.to_dict() for artifact in artifacts],
                "build_backend": "setuptools=={0}".format(_BUILD_BACKEND_VERSION),
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
