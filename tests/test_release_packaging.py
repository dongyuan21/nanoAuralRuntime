"""Offline release artifact, fresh-install, and optional-frontend contracts."""

from __future__ import annotations

import hashlib
import io
import json
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Mapping, Optional, Tuple

import pytest  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import release_artifacts as release_artifacts_module  # noqa: E402
from tools import release_comfyui_archives as comfyui_archives_module  # noqa: E402
from tools.release_artifacts import (  # noqa: E402
    ReleaseArtifactError,
    build_release_artifacts,
)
from tools.release_comfyui_archives import (  # noqa: E402
    ComfyUIArchiveError,
    build_comfyui_archives,
)


@dataclass(frozen=True)
class _ReleaseFixture:
    wheel: Path
    sdist: Path
    archives: Tuple[Path, ...]


@pytest.fixture(scope="module")
def release_fixture(tmp_path_factory: pytest.TempPathFactory) -> _ReleaseFixture:
    root = tmp_path_factory.mktemp("release-artifacts")
    python_output = root / "python"
    comfy_output = root / "comfyui"
    python_output.mkdir()
    comfy_output.mkdir()
    built = build_release_artifacts(python_output)
    optional = build_comfyui_archives(comfy_output)
    wheel = python_output / next(item.filename for item in built if item.filename.endswith(".whl"))
    sdist = python_output / next(
        item.filename for item in built if item.filename.endswith(".tar.gz")
    )
    return _ReleaseFixture(
        wheel,
        sdist,
        tuple(comfy_output / item.filename for item in optional),
    )


def _file_names_in_sdist(path: Path) -> Iterator[str]:
    with tarfile.open(path, "r:gz") as archive:
        for member in archive.getmembers():
            if member.isfile():
                yield member.name


def _release_contract() -> Tuple[release_artifacts_module._ProjectContract, dict[str, bytes]]:
    contract = release_artifacts_module._project_contract(ROOT)
    sources = dict(release_artifacts_module._source_files(ROOT))
    return contract, sources


def _wheel_metadata(path: Path) -> bytes:
    with zipfile.ZipFile(path) as archive:
        name = next(item for item in archive.namelist() if item.endswith(".dist-info/METADATA"))
        return archive.read(name)


def _rewrite_wheel(source: Path, destination: Path, case: str) -> None:
    with zipfile.ZipFile(source) as original:
        items = [(item, original.read(item.filename)) for item in original.infolist()]
    record_name = next(
        item.filename for item, _content in items if item.filename.endswith("/RECORD")
    )
    rewritten = []
    for information, content in items:
        if information.filename == record_name:
            rows = content.decode("utf-8").splitlines()
            if case == "record-missing":
                rows.pop(0)
            elif case == "record-duplicate":
                rows.insert(1, rows[0])
            elif case == "record-hash":
                columns = rows[0].split(",")
                columns[1] = "sha256=" + "A" * 43
                rows[0] = ",".join(columns)
            elif case == "record-size":
                columns = rows[0].split(",")
                columns[2] = str(int(columns[2]) + 1)
                rows[0] = ",".join(columns)
            elif case == "record-self":
                rows[-1] = record_name + ",sha256=" + "A" * 43 + ",1"
            content = ("\n".join(rows) + "\n").encode("utf-8")
        elif information.filename.endswith("/entry_points.txt") and case == "entry-extra":
            content += b"unexpected = package.module:main\n"
        elif information.filename.endswith("/METADATA") and case == "metadata-dependency":
            content = content.replace(b"psycopg[binary]<4,>=3.1", b"malicious-package>=1", 1)
        elif information.filename.endswith("/METADATA") and case == "metadata-name":
            content = content.replace(b"Name: nano-aural-runtime", b"Name: malicious-runtime", 1)
        rewritten.append((information, content))
    with zipfile.ZipFile(destination, "w") as target:
        for information, content in rewritten:
            target.writestr(information, content)


def _rewrite_sdist(source: Path, destination: Path, case: str) -> None:
    with tarfile.open(source, "r:gz") as original, tarfile.open(destination, "w:gz") as target:
        duplicate: Optional[Tuple[tarfile.TarInfo, bytes]] = None
        for information in original.getmembers():
            extracted = original.extractfile(information) if information.isfile() else None
            content = extracted.read() if extracted is not None else b""
            if information.name.endswith("/README.md") and case == "root-readme":
                content += b"\ntampered\n"
            elif information.name.endswith("/LICENSE") and case == "root-license":
                content += b"\ntampered\n"
            elif information.name.endswith("/NOTICE") and case == "root-notice":
                content += b"\ntampered\n"
            elif information.name.endswith("/pyproject.toml") and case == "root-pyproject":
                content = content.replace(b"nano-aural-runtime", b"wrong-runtime", 1)
            elif information.name.count("/") == 1 and information.name.endswith("/PKG-INFO"):
                if case == "root-pkg-info":
                    content = content.replace(
                        b"Requires-Python: <3.13,>=3.9", b"Requires-Python: >=3"
                    )
            elif information.name.endswith(".egg-info/PKG-INFO") and case == "egg-pkg-info":
                content = content.replace(b"Requires-Python: <3.13,>=3.9", b"Requires-Python: >=3")
            elif information.name.endswith(".egg-info/entry_points.txt") and case == "egg-entry":
                content += b"unexpected = package.module:main\n"
            elif information.name.endswith(".egg-info/requires.txt") and case == "egg-requires":
                content += b"malicious-package>=1\n"
            information.size = len(content)
            target.addfile(information, io.BytesIO(content) if information.isfile() else None)
            if information.name.endswith("/README.md") and case == "duplicate":
                duplicate = information, content
        if duplicate is not None:
            information, content = duplicate
            target.addfile(information, io.BytesIO(content))


def test_wheel_and_sdist_are_strict_headless_distributions(
    release_fixture: _ReleaseFixture, tmp_path: Path
) -> None:
    prohibited = {
        ".cache",
        ".env",
        ".git",
        ".pytest_cache",
        ".ruff_cache",
        ".venv",
        "artifacts",
        "benchmarks",
        "checkpoints",
        "evidence",
        "hf_cache",
        "integrations",
        "media",
        "model_weights",
        "outputs",
        "secrets",
        "tests",
        "weights",
    }
    with zipfile.ZipFile(release_fixture.wheel) as wheel:
        wheel_names = wheel.namelist()
        assert any(name.endswith(".dist-info/licenses/LICENSE") for name in wheel_names)
        assert any(name.endswith(".dist-info/licenses/NOTICE") for name in wheel_names)
        notice_name = next(
            name for name in wheel_names if name.endswith(".dist-info/licenses/NOTICE")
        )
        notice = wheel.read(notice_name).decode("utf-8")
        assert "https://github.com/xiaomi-research/controlfoley" in notice
        assert "https://huggingface.co/YJX-Xiaomi/ControlFoley/blob/main/README.md" in notice
        assert "CC BY-NC 4.0 includes a NonCommercial restriction" in notice
        assert "https://github.com/Stability-AI/stable-audio-3" in notice
        assert "https://github.com/SonyResearch/Woosh" in notice
        assert "https://huggingface.co/hkchengrex/MMAudio" in notice
        assert {name.split("/", 1)[0] for name in wheel_names if ".dist-info/" not in name} == {
            "nano_aural_runtime",
            "nano_aural_runtime_cli",
            "nano_aural_runtime_controlfoley",
            "nano_aural_runtime_remote",
            "nano_aural_runtime_stable_audio_3",
            "nano_aural_runtime_woosh",
            "nano_aural_runtime_workflows",
            "nano_aural_runtime_workers",
        }
    all_names = wheel_names + list(_file_names_in_sdist(release_fixture.sdist))
    for raw in all_names:
        name = PurePosixPath(raw)
        assert name.is_absolute() is False
        assert ".." not in name.parts
        assert prohibited.isdisjoint(part.casefold() for part in name.parts)

    assert 'requires = ["setuptools==82.0.1"]' in (ROOT / "pyproject.toml").read_text(
        encoding="utf-8"
    )

    second = tmp_path / "second-python-build"
    second.mkdir()
    build_release_artifacts(second)
    first_bytes = {
        path.name: path.read_bytes() for path in (release_fixture.wheel, release_fixture.sdist)
    }
    second_bytes = {path.name: path.read_bytes() for path in second.iterdir()}
    assert first_bytes == second_bytes


@pytest.mark.parametrize(
    "case",
    (
        "record-missing",
        "record-duplicate",
        "record-hash",
        "record-size",
        "record-self",
        "entry-extra",
        "metadata-dependency",
        "metadata-name",
    ),
)
def test_wheel_audit_rejects_record_entry_point_and_metadata_tampering(
    release_fixture: _ReleaseFixture, tmp_path: Path, case: str
) -> None:
    destination = tmp_path / (case + ".whl")
    _rewrite_wheel(release_fixture.wheel, destination, case)
    contract, sources = _release_contract()
    with pytest.raises(ReleaseArtifactError):
        release_artifacts_module._audit_wheel(destination, contract, sources, ROOT)


@pytest.mark.parametrize(
    "case",
    (
        "duplicate",
        "root-readme",
        "root-license",
        "root-notice",
        "root-pyproject",
        "root-pkg-info",
        "egg-pkg-info",
        "egg-entry",
        "egg-requires",
    ),
)
def test_sdist_audit_rejects_duplicate_bound_file_and_metadata_tampering(
    release_fixture: _ReleaseFixture, tmp_path: Path, case: str
) -> None:
    destination = tmp_path / (case + ".tar.gz")
    _rewrite_sdist(release_fixture.sdist, destination, case)
    contract, sources = _release_contract()
    with pytest.raises(ReleaseArtifactError):
        release_artifacts_module._audit_sdist(
            destination,
            contract,
            sources,
            ROOT,
            _wheel_metadata(release_fixture.wheel),
        )


def _isolated_environment() -> dict[str, str]:
    environment = dict(os.environ)
    for name in ("PYTHONHOME", "PYTHONPATH", "PYTHONSTARTUP"):
        environment.pop(name, None)
    return environment


def _run(
    command: list[str], *, cwd: Path, environment: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_fresh_venv_installs_wheel_scripts_headless_imports_and_sql_resources(
    release_fixture: _ReleaseFixture, tmp_path: Path
) -> None:
    environment = _isolated_environment()
    work = tmp_path / "detached-cwd"
    work.mkdir()
    venv = tmp_path / "venv"
    created = _run([sys.executable, "-m", "venv", str(venv)], cwd=work, environment=environment)
    assert created.returncode == 0, created.stdout + created.stderr
    binary = "Scripts" if os.name == "nt" else "bin"
    python = venv / binary / ("python.exe" if os.name == "nt" else "python")
    installed = _run(
        [
            str(python),
            "-m",
            "pip",
            "install",
            "--no-index",
            "--no-deps",
            str(release_fixture.wheel),
        ],
        cwd=work,
        environment=environment,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr

    commands = (
        (str(venv / binary / "nano-aural"), "--help"),
        (str(venv / binary / "nano-aural-controlfoley"), "--help"),
        (str(venv / binary / "nano-aural-remote"), "--help"),
        (str(python), "-m", "nano_aural_runtime_cli", "--help"),
        (str(python), "-m", "nano_aural_runtime_controlfoley.cli", "--help"),
        (str(python), "-m", "nano_aural_runtime_remote", "--help"),
        (str(python), "-m", "nano_aural_runtime.durable.service", "--help"),
        (str(python), "-m", "nano_aural_runtime.durable.reference_worker", "--help"),
        (str(python), "-m", "nano_aural_runtime.durable.recovery", "--help"),
    )
    for command in commands:
        completed = _run(list(command), cwd=work, environment=environment)
        assert completed.returncode == 0, completed.stdout + completed.stderr
        assert "usage:" in completed.stdout
        assert str(ROOT) not in completed.stdout + completed.stderr

    archives = [str(path) for path in release_fixture.archives]
    code = """
import importlib
import importlib.metadata
import importlib.resources
import json
import sys

for name in (
    "nano_aural_runtime",
    "nano_aural_runtime_cli",
    "nano_aural_runtime_controlfoley",
    "nano_aural_runtime_remote",
    "nano_aural_runtime_stable_audio_3",
    "nano_aural_runtime_woosh",
    "nano_aural_runtime_workflows",
    "nano_aural_runtime_workers",
):
    importlib.import_module(name)
sql = importlib.resources.files("nano_aural_runtime.durable").joinpath("sql")
assert sorted(item.name for item in sql.iterdir() if item.name.endswith(".sql")) == [
    "0001_durable_foundation.sql",
    "0002_verified_uploads.sql",
    "0003_queue_leases.sql",
    "0004_artifact_publications.sql",
    "0005_upload_staging_cleanups.sql",
]
distribution = importlib.metadata.distribution("nano-aural-runtime")
entries = {(item.name, item.value) for item in distribution.entry_points if item.group == "console_scripts"}
assert entries == {
    ("nano-aural", "nano_aural_runtime_cli.main:main"),
    ("nano-aural-controlfoley", "nano_aural_runtime_cli.main:controlfoley_alias"),
    ("nano-aural-remote", "nano_aural_runtime_remote.cli:main"),
}
assert any(str(item).endswith("licenses/LICENSE") for item in distribution.files or ())
assert any(str(item).endswith("licenses/NOTICE") for item in distribution.files or ())
try:
    importlib.import_module("integrations.comfyui")
except ModuleNotFoundError:
    pass
else:
    raise AssertionError("headless wheel unexpectedly includes integrations")
for archive in json.loads(sys.argv[1]):
    sys.path.insert(0, archive)
embedded = importlib.import_module("nano_aural_comfyui_embedded")
remote = importlib.import_module("nano_aural_comfyui_remote")
compat = importlib.import_module("nano_aural_comfyui_compat")
assert set(embedded.NODE_CLASS_MAPPINGS).isdisjoint(remote.NODE_CLASS_MAPPINGS)
assert compat.OFFICIAL_CONTROLFOLEY_NODE_NAMES
assert not any(name == "comfy" or name.startswith("comfy.") for name in sys.modules)
print("FRESH_WHEEL_AND_OPTIONAL_ARCHIVES_OK")
"""
    verified = _run(
        [str(python), "-I", "-c", code, json.dumps(archives)],
        cwd=work,
        environment=environment,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr
    assert verified.stdout.strip() == "FRESH_WHEEL_AND_OPTIONAL_ARCHIVES_OK"
    assert str(ROOT) not in verified.stdout + verified.stderr


def _archive_map(paths: Tuple[Path, ...]) -> dict[str, bytes]:
    return {path.name: path.read_bytes() for path in paths}


def test_comfyui_archives_are_deterministic_independent_and_manifested(
    release_fixture: _ReleaseFixture, tmp_path: Path
) -> None:
    second = tmp_path / "second"
    second.mkdir()
    second_evidence = build_comfyui_archives(second)
    second_paths = tuple(second / item.filename for item in second_evidence)
    assert _archive_map(release_fixture.archives) == _archive_map(second_paths)
    expected_packages = {
        "nano_aural_comfyui_embedded",
        "nano_aural_comfyui_remote",
        "nano_aural_comfyui_compat",
    }
    observed_packages = set()
    for path in release_fixture.archives:
        with zipfile.ZipFile(path) as archive:
            assert archive.testzip() is None
            names = archive.namelist()
            roots = {PurePosixPath(name).parts[0] for name in names}
            assert len(roots) == 1
            package = next(iter(roots))
            observed_packages.add(package)
            manifest_name = package + "/RELEASE-MANIFEST.json"
            manifest = json.loads(archive.read(manifest_name))
            assert manifest["package_name"] == package
            assert manifest["contains_controlfoley_source"] is False
            assert manifest["contains_model_weights"] is False
            assert manifest["hardware_evidence"] == "not_included"
            assert package + "/LICENSE" in names
            assert package + "/NOTICE" in names
            described = {item["path"]: item for item in manifest["files"]}
            assert set(described) == set(names) - {manifest_name}
            for name, item in described.items():
                content = archive.read(name)
                assert item["size_bytes"] == len(content)
                assert item["sha256"] == hashlib.sha256(content).hexdigest()
                info = archive.getinfo(name)
                assert info.date_time == (1980, 1, 1, 0, 0, 0)
                assert info.compress_type == zipfile.ZIP_STORED
    assert observed_packages == expected_packages


def test_release_publish_fsyncs_output_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_fsync = os.fsync
    directory_syncs = 0

    def recording_fsync(descriptor: int) -> None:
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(descriptor).st_mode):
            directory_syncs += 1
        original_fsync(descriptor)

    monkeypatch.setattr(os, "fsync", recording_fsync)
    python_output = tmp_path / "python"
    comfy_output = tmp_path / "comfyui"
    python_output.mkdir()
    comfy_output.mkdir()
    build_release_artifacts(python_output)
    build_comfyui_archives(comfy_output)
    assert directory_syncs == 2


def _assert_only_expected_output(output: Path, expected: Mapping[str, bytes]) -> None:
    observed = {path.name: path.read_bytes() for path in output.iterdir()}
    assert observed == expected


def test_python_publication_preflights_the_complete_target_set(tmp_path: Path) -> None:
    output = tmp_path / "python"
    output.mkdir()
    sentinel = b"preexisting-sdist"
    existing = output / "nano_aural_runtime-0.1.0.dev0.tar.gz"
    existing.write_bytes(sentinel)
    unrelated = output / "operator-note"
    unrelated.write_bytes(b"keep")
    with pytest.raises(ReleaseArtifactError, match="refusing to overwrite"):
        build_release_artifacts(output)
    _assert_only_expected_output(output, {existing.name: sentinel, unrelated.name: b"keep"})


@pytest.mark.parametrize(
    "existing_name",
    (
        "nano-aural-comfyui-remote-0.1.0.dev0.zip",
        "nano-aural-comfyui-compat-0.1.0.dev0.zip",
    ),
)
def test_comfyui_publication_preflights_the_complete_target_set(
    tmp_path: Path, existing_name: str
) -> None:
    output = tmp_path / "comfyui"
    output.mkdir()
    existing = output / existing_name
    existing.write_bytes(b"preexisting-archive")
    unrelated = output / "operator-note"
    unrelated.write_bytes(b"keep")
    with pytest.raises(ComfyUIArchiveError, match="refusing to overwrite"):
        build_comfyui_archives(output)
    _assert_only_expected_output(
        output, {existing.name: b"preexisting-archive", unrelated.name: b"keep"}
    )


def test_python_publication_rolls_back_only_its_targets_after_link_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "python"
    output.mkdir()
    unrelated = output / "operator-note"
    unrelated.write_bytes(b"keep")
    original_link = os.link
    calls = 0

    def failing_link(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("injected link failure")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(OSError, match="injected link failure"):
        build_release_artifacts(output)
    assert calls == 2
    _assert_only_expected_output(output, {unrelated.name: b"keep"})


def test_comfyui_publication_rolls_back_only_its_targets_after_link_failure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "comfyui"
    output.mkdir()
    unrelated = output / "operator-note"
    unrelated.write_bytes(b"keep")
    original_link = os.link
    calls = 0

    def failing_link(source: os.PathLike[str], destination: os.PathLike[str]) -> None:
        nonlocal calls
        calls += 1
        if calls == 3:
            raise OSError("injected link failure")
        original_link(source, destination)

    monkeypatch.setattr(os, "link", failing_link)
    with pytest.raises(OSError, match="injected link failure"):
        build_comfyui_archives(output)
    assert calls == 3
    _assert_only_expected_output(output, {unrelated.name: b"keep"})


@pytest.mark.parametrize(
    ("builder", "module"),
    (
        (build_release_artifacts, release_artifacts_module),
        (build_comfyui_archives, comfyui_archives_module),
    ),
)
def test_publication_rolls_back_and_resyncs_after_directory_fsync_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builder: object,
    module: object,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    unrelated = output / "operator-note"
    unrelated.write_bytes(b"keep")
    original_fsync = module._fsync_directory  # type: ignore[attr-defined]
    calls = 0

    def failing_once(directory: Path) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError("injected directory fsync failure")
        original_fsync(directory)

    monkeypatch.setattr(module, "_fsync_directory", failing_once)
    with pytest.raises(OSError, match="injected directory fsync failure"):
        builder(output)  # type: ignore[operator]
    assert calls == 2
    _assert_only_expected_output(output, {unrelated.name: b"keep"})


@pytest.mark.parametrize(
    ("builder", "module"),
    (
        (build_release_artifacts, release_artifacts_module),
        (build_comfyui_archives, comfyui_archives_module),
    ),
    ids=("wheel-sdist", "comfyui-archives"),
)
def test_post_link_identity_failure_is_registered_rolled_back_and_resynced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    builder: object,
    module: object,
) -> None:
    output = tmp_path / "output"
    output.mkdir()
    unrelated = output / "preexisting-operator-note"
    unrelated.write_bytes(b"keep")
    original_fsync = module._fsync_directory  # type: ignore[attr-defined]
    verify_calls = 0
    directory_syncs = 0

    def failing_verification(destination: Path, expected_device: int, expected_inode: int) -> None:
        del destination, expected_device, expected_inode
        nonlocal verify_calls
        verify_calls += 1
        raise OSError("injected post-link identity failure")

    def recording_fsync(directory: Path) -> None:
        nonlocal directory_syncs
        directory_syncs += 1
        original_fsync(directory)

    monkeypatch.setattr(module, "_verify_published_identity", failing_verification)
    monkeypatch.setattr(module, "_fsync_directory", recording_fsync)
    with pytest.raises(OSError, match="injected post-link identity failure"):
        builder(output)  # type: ignore[operator]
    assert verify_calls == 1
    assert directory_syncs == 1
    _assert_only_expected_output(output, {unrelated.name: b"keep"})


def test_reference_container_source_and_dependency_match_wheel_contract(
    release_fixture: _ReleaseFixture,
) -> None:
    dockerfile = (ROOT / "ops/Dockerfile.api").read_text(encoding="utf-8")
    assert "COPY src/nano_aural_runtime /opt/nano-aural/src/nano_aural_runtime" in dockerfile
    assert "COPY src/nano_aural_runtime_controlfoley" not in dockerfile
    requirements = (ROOT / "ops/requirements-api.txt").read_text(encoding="utf-8")
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    assert 'durable-postgres = [\n    "psycopg[binary]>=3.1,<4",\n]' in pyproject
    assert "psycopg[binary]==3.2.13" in requirements
    assert "psycopg-binary==3.2.13" in requirements
    assert "typing_extensions==4.15.0" in requirements
    assert "--require-hashes" in requirements
    assert "--only-binary=:all:" in requirements
    with zipfile.ZipFile(release_fixture.wheel) as archive:
        for source in sorted((ROOT / "src/nano_aural_runtime").rglob("*")):
            if source.is_file() and "__pycache__" not in source.parts:
                relative = source.relative_to(ROOT / "src").as_posix()
                assert archive.read(relative) == source.read_bytes()


def test_static_tooling_excludes_archived_source_plans() -> None:
    pyproject = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    ruff_section = pyproject.split("[tool.ruff]", 1)[1].split("[tool.ruff.lint]", 1)[0]
    assert 'exclude = ["docs/source-plans/**"]' in ruff_section
