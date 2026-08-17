# pyright: reportMissingImports=false
from __future__ import annotations

import base64
import contextlib
import hashlib
import importlib.util
import io
import json
import os
import re
import stat
import sys
import tarfile
import traceback
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from types import ModuleType
from typing import Callable, Mapping, Sequence, cast

import pytest

from nano_aural_runtime.durable import recovery, reference_worker, service
from nano_aural_runtime.durable.observability import (
    DurableHttpObserver,
    DurableMetrics,
    StructuredEventLogger,
)
from nano_aural_runtime_controlfoley import cli as local_cli
from nano_aural_runtime_remote import cli as remote_cli
from nano_aural_runtime_remote.client import RemoteClient

ROOT = Path(__file__).resolve().parents[1]
AUDIT_PATH = ROOT / "tools" / "release_security_audit.py"
RELEASE_ARTIFACTS_PATH = ROOT / "tools" / "release_artifacts.py"
CANARY = "NAR_RELEASE_CANARY_7f2d9b31"

_release_spec = importlib.util.spec_from_file_location(
    "release_artifacts_contract", RELEASE_ARTIFACTS_PATH
)
assert _release_spec is not None and _release_spec.loader is not None
release_artifacts_module = importlib.util.module_from_spec(_release_spec)
sys.modules[_release_spec.name] = release_artifacts_module
_release_spec.loader.exec_module(release_artifacts_module)


@pytest.fixture(scope="module")
def audit() -> ModuleType:
    spec = importlib.util.spec_from_file_location("release_security_audit", AUDIT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _capture_entrypoint(call: Callable[[], int]) -> tuple[int, str, str, str]:
    stdout = io.StringIO()
    stderr = io.StringIO()
    rendered_traceback = ""
    try:
        with contextlib.redirect_stdout(stdout), contextlib.redirect_stderr(stderr):
            result = call()
    except BaseException as error:  # pragma: no cover - an escaped canary is the test failure
        result = -1
        rendered_traceback = "".join(
            traceback.format_exception(type(error), error, error.__traceback__)
        )
    return result, stdout.getvalue(), stderr.getvalue(), rendered_traceback


def _chained_canary_error() -> LookupError:
    try:
        raise OSError(CANARY)
    except OSError as cause:
        try:
            raise LookupError(CANARY) from cause
        except LookupError as error:
            return error


def test_canary_is_redacted_from_cli_service_worker_and_recovery_surfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    error = _chained_canary_error()

    def fail(*_args: object, **_kwargs: object) -> object:
        raise error

    monkeypatch.setattr(local_cli, "_safe_output", fail)
    local = _capture_entrypoint(
        lambda: local_cli.main(
            (
                "controlfoley",
                "local",
                "--manifest",
                "manifest.json",
                "--source-dir",
                "source",
                "--weights-dir",
                "weights",
                "--task",
                "T2A",
                "--prompt",
                CANARY,
                "--output",
                "output.flac",
            )
        )
    )

    monkeypatch.setattr(service.DurableServiceConfig, "from_environment", fail)
    durable_service = _capture_entrypoint(lambda: service.main(()))

    monkeypatch.setattr(reference_worker.ReferenceWorkerConfig, "from_environment", fail)
    worker = _capture_entrypoint(lambda: reference_worker.main(("--run-once",)))

    monkeypatch.setenv("NANO_AURAL_DATABASE_DSN", CANARY)
    monkeypatch.setattr(recovery, "_connect", fail)
    recovery_result = _capture_entrypoint(lambda: recovery.main(("--reap-expired",)))

    class CanaryRemoteClient:
        def status(self, _job_id: str) -> Mapping[str, object]:
            raise error

    remote_stdout, remote_stderr = io.StringIO(), io.StringIO()
    remote_result = _capture_entrypoint(
        lambda: remote_cli.run(
            ("status", "job"),
            cast(RemoteClient, CanaryRemoteClient()),
            remote_stdout,
            remote_stderr,
        )
    )
    remote_result = (
        remote_result[0],
        remote_result[1] + remote_stdout.getvalue(),
        remote_result[2] + remote_stderr.getvalue(),
        remote_result[3],
    )

    for result, stdout, stderr, full_traceback in (
        local,
        durable_service,
        worker,
        recovery_result,
        remote_result,
    ):
        assert result == 2
        assert stdout == ""
        assert stderr
        assert CANARY not in stdout + stderr + full_traceback
        assert "Traceback" not in stdout + stderr + full_traceback


@pytest.mark.parametrize("stage", ("create", "serve", "close"))
def test_durable_service_runtime_and_close_canary_never_escape(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    error = _chained_canary_error()

    class CanaryServer:
        def __enter__(self) -> "CanaryServer":
            return self

        def __exit__(self, *_args: object) -> None:
            return None

        def serve_forever(self) -> None:
            if stage == "serve":
                raise error

    class CanaryService:
        def __enter__(self) -> "CanaryService":
            return self

        def __exit__(self, *_args: object) -> None:
            if stage == "close":
                raise error

        def create_server(self) -> CanaryServer:
            if stage == "create":
                raise error
            return CanaryServer()

    fake_service = CanaryService()
    monkeypatch.setattr(
        service.DurableServiceConfig,
        "from_environment",
        classmethod(lambda _cls: object()),
    )
    monkeypatch.setattr(
        service.DurableService,
        "connect",
        classmethod(lambda _cls, _config: fake_service),
    )
    result, stdout, stderr, full_traceback = _capture_entrypoint(lambda: service.main(()))
    assert result == 1
    assert stdout == ""
    assert stderr == "nano-aural durable service failed; check operator logs\n"
    assert CANARY not in stdout + stderr + full_traceback
    assert "Traceback" not in stdout + stderr + full_traceback


@pytest.mark.parametrize("stage", ("apply", "close"))
def test_durable_service_migrate_only_canary_never_escapes(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    error = _chained_canary_error()

    class Connection:
        def close(self) -> None:
            if stage == "close":
                raise error

    monkeypatch.setenv("NANO_AURAL_DATABASE_DSN", CANARY)
    monkeypatch.setattr(service, "_connect_postgres", lambda _dsn: Connection())

    def apply(_connection: object) -> None:
        if stage == "apply":
            raise error

    monkeypatch.setattr(service, "apply_postgres_migrations", apply)
    result, stdout, stderr, full_traceback = _capture_entrypoint(
        lambda: service.main(("--migrate-only",))
    )
    assert result == 2
    assert stdout == ""
    assert stderr == "nano-aural durable migration failed; check operator logs\n"
    assert CANARY not in stdout + stderr + full_traceback
    assert "Traceback" not in stdout + stderr + full_traceback


@pytest.mark.parametrize("stage", ("action", "events", "close"))
def test_durable_recovery_action_event_and_close_canary_never_escape(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    error = _chained_canary_error()

    class Connection:
        def close(self) -> None:
            if stage == "close":
                raise error

    class Queue:
        def __init__(self, _connection: object) -> None:
            pass

        def reap_expired(self, *, limit: int) -> int:
            assert limit == 100
            if stage == "action":
                raise error
            return 0

    class Events:
        def __init__(self, _output: object) -> None:
            pass

        def emit(self, **_values: object) -> None:
            if stage == "events":
                raise error

    monkeypatch.setenv("NANO_AURAL_DATABASE_DSN", CANARY)
    monkeypatch.setattr(recovery, "_connect", lambda _dsn: Connection())
    monkeypatch.setattr(recovery, "PostgresLeaseQueue", Queue)
    monkeypatch.setattr(recovery, "StructuredEventLogger", Events)
    result, stdout, stderr, full_traceback = _capture_entrypoint(
        lambda: recovery.main(("--reap-expired",))
    )
    assert result == 1
    assert stdout == ""
    assert stderr == "durable recovery failed; check operator logs\n"
    assert CANARY not in stdout + stderr + full_traceback
    assert "Traceback" not in stdout + stderr + full_traceback


def test_canary_is_reduced_out_of_structured_observer_logs() -> None:
    output = io.StringIO()
    observer = DurableHttpObserver(
        DurableMetrics(),
        StructuredEventLogger(
            output,
            clock=lambda: datetime(2026, 8, 17, tzinfo=timezone.utc),
        ),
    )
    observer.record("GET", "/v1/jobs/{0}/artifacts/{0}/content".format(CANARY), 500, 1.0)
    assert CANARY not in output.getvalue()
    assert json.loads(output.getvalue()) == {
        "component": "api",
        "duration_ms": 1.0,
        "outcome": "failed",
        "timestamp": "2026-08-17T00:00:00Z",
    }


def test_remote_cli_configuration_canary_is_redacted(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("NANO_AURAL_API_URL", CANARY)
    monkeypatch.setenv("NANO_AURAL_API_TOKEN", CANARY)
    result, stdout, stderr, full_traceback = _capture_entrypoint(
        lambda: remote_cli.main(("status", "job"))
    )
    assert result == 2
    assert stdout == ""
    assert stderr == "nano-aural-remote: configuration failed\n"
    assert CANARY not in stdout + stderr + full_traceback


def test_secret_scanner_reports_only_file_and_rule_with_allowlist(
    audit: ModuleType, tmp_path: Path
) -> None:
    key_header = "-----BEGIN " + "PRIVATE KEY-----"
    access_key = "AK" + "IA" + "A" * 16
    provider_key = "s" + "k-" + "B" * 24
    (tmp_path / "credentials.txt").write_text(
        "\n".join((key_header, access_key, provider_key)), encoding="utf-8"
    )
    placeholder = "placeholder-not-a-real-credential"
    (tmp_path / "placeholder.txt").write_text(
        "tok" + "en=" + json.dumps(placeholder) + "\n", encoding="utf-8"
    )
    (tmp_path / ".env").write_text("PLACEHOLDER=not-a-real-credential\n", encoding="utf-8")

    findings = audit.scan_secrets(tmp_path)
    assert [finding.to_dict() for finding in findings] == [
        {"file": ".env", "rule": "secret.sensitive_filename"},
        {"file": "credentials.txt", "rule": "secret.aws_access_key"},
        {"file": "credentials.txt", "rule": "secret.private_key"},
        {"file": "credentials.txt", "rule": "secret.provider_key"},
        {"file": "placeholder.txt", "rule": "secret.hardcoded_assignment"},
    ]
    encoded = json.dumps([finding.to_dict() for finding in findings], sort_keys=True)
    for sensitive in (key_header, access_key, provider_key):
        assert sensitive not in encoded

    allowed = audit.scan_secrets(
        tmp_path,
        allowlist={
            (
                "credentials.txt",
                "secret.aws_access_key",
                hashlib.sha256(access_key.encode()).hexdigest(),
            ),
            (
                "credentials.txt",
                "secret.private_key",
                hashlib.sha256(key_header.encode()).hexdigest(),
            ),
            (
                "credentials.txt",
                "secret.provider_key",
                hashlib.sha256(provider_key.encode()).hexdigest(),
            ),
        },
    )
    assert [finding.to_dict() for finding in allowed] == [
        {"file": ".env", "rule": "secret.sensitive_filename"},
        {"file": "placeholder.txt", "rule": "secret.hardcoded_assignment"},
    ]

    second_access_key = "AK" + "IA" + "Z" * 16
    (tmp_path / "credentials.txt").write_text(
        access_key + "\n" + second_access_key + "\n", encoding="utf-8"
    )
    same_rule = audit.scan_secrets(
        tmp_path,
        allowlist={
            (
                "credentials.txt",
                "secret.aws_access_key",
                hashlib.sha256(access_key.encode()).hexdigest(),
            )
        },
    )
    assert any(
        item.file == "credentials.txt" and item.rule == "secret.aws_access_key"
        for item in same_rule
    )


def test_recursive_artifact_allow_and_deny_scanner(audit: ModuleType, tmp_path: Path) -> None:
    source = tmp_path / "src"
    source.mkdir()
    (source / "safe.py").write_text("VALUE = 1\n", encoding="utf-8")
    (source / "model.pth").write_bytes(b"not-a-real-weight")
    (tmp_path / ".env").write_text("not-a-real-secret", encoding="utf-8")
    (tmp_path / "unexpected").mkdir()
    (tmp_path / "unexpected" / "file.txt").write_text("x", encoding="utf-8")
    os.symlink(source / "safe.py", tmp_path / "linked.py")

    findings = {
        tuple(item.to_dict().values())
        for item in audit.scan_artifact_tree(tmp_path, allowed_top_levels=("src",))
    }
    assert ("src/model.pth", "artifact.denied_binary_or_model_file") in findings
    assert (".env", "artifact.denied_secret_file") in findings
    assert ("linked.py", "artifact.symlink") in findings
    assert ("unexpected", "artifact.not_allowlisted") in findings
    assert ("src/safe.py", "artifact.not_allowlisted") not in findings


def _record_row(name: str, payload: bytes) -> tuple[str, str, str]:
    encoded = base64.urlsafe_b64encode(hashlib.sha256(payload).digest()).rstrip(b"=").decode()
    return name, "sha256=" + encoded, str(len(payload))


def _project_version(root: Path) -> str:
    matched = re.search(
        r'^version\s*=\s*"([^"]+)"',
        root.joinpath("pyproject.toml").read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    assert matched is not None
    return matched.group(1)


def _package_payloads(source_root: Path | None = None) -> dict[str, bytes]:
    roots = (
        "nano_aural_runtime",
        "nano_aural_runtime_cli",
        "nano_aural_runtime_controlfoley",
        "nano_aural_runtime_remote",
        "nano_aural_runtime_stable_audio_3",
        "nano_aural_runtime_woosh",
        "nano_aural_runtime_workflows",
        "nano_aural_runtime_workers",
    )
    if source_root is None:
        return {root + "/__init__.py": b"" for root in roots}
    payloads = {}
    for root in roots:
        for path in sorted((source_root / "src" / root).rglob("*")):
            if "__pycache__" in path.parts or not path.is_file():
                continue
            if path.suffix in (".py", ".pyi", ".sql") or path.name == "py.typed":
                payloads[path.relative_to(source_root / "src").as_posix()] = path.read_bytes()
    return payloads


def _release_metadata(source_root: Path, *, version: str | None = None) -> bytes:
    contract = release_artifacts_module._project_contract(source_root)
    lines = []
    for name, values in release_artifacts_module._expected_metadata_headers(contract).items():
        lines.extend("{0}: {1}".format(name, value) for value in values)
    payload = ("\n".join(lines) + "\n\n").encode() + (source_root / "README.md").read_bytes()
    if version is not None:
        payload = payload.replace(
            ("Version: " + contract.version).encode(), ("Version: " + version).encode(), 1
        )
    return payload


def _wheel(
    path: Path,
    *,
    source_root: Path | None = None,
    version: str | None = None,
    metadata_version: str | None = None,
    dist_info_name: str | None = None,
    malicious_requires_dist: bool = False,
    mutate_record: bool = False,
    extra: Sequence[tuple[zipfile.ZipInfo | str, bytes]] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    version = version or (_project_version(source_root) if source_root else "0.1.0")
    dist_info = dist_info_name or "nano_aural_runtime-{0}.dist-info".format(version)
    assert source_root is not None
    contract = release_artifacts_module._project_contract(source_root)
    metadata_payload = _release_metadata(source_root, version=metadata_version)
    if malicious_requires_dist:
        metadata_payload = metadata_payload.replace(
            b"Requires-Dist: psycopg[binary]<4,>=3.1",
            b"Requires-Dist: malicious-package>=1",
            1,
        )
    files = _package_payloads(source_root)
    files.update(
        {
            dist_info + "/METADATA": metadata_payload,
            dist_info + "/WHEEL": (
                "Wheel-Version: 1.0\n"
                "Generator: setuptools (82.0.1)\n"
                "Root-Is-Purelib: true\n"
                "Tag: py3-none-any\n\n"
            ).encode(),
            dist_info + "/entry_points.txt": release_artifacts_module._expected_entry_points(
                contract
            ),
            dist_info + "/top_level.txt": release_artifacts_module._expected_top_level(),
            dist_info + "/licenses/LICENSE": (source_root / "LICENSE").read_bytes(),
            dist_info + "/licenses/NOTICE": (source_root / "NOTICE").read_bytes(),
        }
    )
    special: dict[str, zipfile.ZipInfo] = {}
    for name, payload in extra:
        if isinstance(name, str):
            files[name] = payload
        else:
            files[name.filename] = payload
            special[name.filename] = name
    rows = [_record_row(name, payload) for name, payload in sorted(files.items())]
    if mutate_record:
        rows[0] = (rows[0][0], "sha256=" + "A" * 43, rows[0][2])
    rows.append((dist_info + "/RECORD", "", ""))
    buffer = io.StringIO(newline="")
    import csv

    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerows(rows)
    files[dist_info + "/RECORD"] = buffer.getvalue().encode()
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, payload in sorted(files.items()):
            archive.writestr(special.get(name, name), payload)


def test_wheel_record_hash_path_and_symlink_validation(audit: ModuleType, tmp_path: Path) -> None:
    version = _project_version(ROOT)
    valid = tmp_path / "nano_aural_runtime-{0}-py3-none-any.whl".format(version)
    _wheel(valid, source_root=ROOT)
    result = audit.validate_wheel(valid, source_root=ROOT)
    assert result["validation"] == "VALID"
    assert result["sha256"] == audit.sha256_file(valid)

    tampered = tmp_path / "tampered" / valid.name
    _wheel(tampered, source_root=ROOT, mutate_record=True)
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_wheel(tampered, source_root=ROOT)
    assert caught.value.finding.rule == "wheel.record_hash"

    traversal = tmp_path / "traversal" / valid.name
    _wheel(traversal, source_root=ROOT, extra=(("../outside.py", b"x"),))
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_wheel(traversal, source_root=ROOT)
    assert caught.value.finding.rule == "wheel.unsafe_path"

    link = zipfile.ZipInfo("nano_aural_runtime/link.py")
    link.create_system = 3
    link.external_attr = (stat.S_IFLNK | 0o777) << 16
    symlink_wheel = tmp_path / "symlink" / valid.name
    _wheel(symlink_wheel, source_root=ROOT, extra=((link, b"target"),))
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_wheel(symlink_wheel, source_root=ROOT)
    assert caught.value.finding.rule == "wheel.symlink"

    evil = tmp_path / "evil" / valid.name
    _wheel(evil, source_root=ROOT, extra=(("nano_aural_runtime_evil/__init__.py", b""),))
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_wheel(evil, source_root=ROOT)
    assert caught.value.finding.rule == "wheel.member_not_allowlisted"

    mismatched = tmp_path / "identity" / valid.name
    _wheel(mismatched, source_root=ROOT, metadata_version="9.9.9")
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_wheel(mismatched, source_root=ROOT)
    assert caught.value.finding.rule == "wheel.project_identity"


def test_wheel_rejects_malicious_requires_dist(audit: ModuleType, tmp_path: Path) -> None:
    version = _project_version(ROOT)
    candidate = tmp_path / "nano_aural_runtime-{0}-py3-none-any.whl".format(version)
    _wheel(candidate, source_root=ROOT, malicious_requires_dist=True)
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_wheel(candidate, source_root=ROOT)
    assert caught.value.finding.rule == "wheel.release_metadata"


def test_wheel_rejects_uncontracted_installer_member(audit: ModuleType, tmp_path: Path) -> None:
    version = _project_version(ROOT)
    candidate = tmp_path / "nano_aural_runtime-{0}-py3-none-any.whl".format(version)
    dist_info = "nano_aural_runtime-{0}.dist-info".format(version)
    _wheel(candidate, source_root=ROOT, extra=((dist_info + "/INSTALLER", b"malicious\n"),))
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_wheel(candidate, source_root=ROOT)
    assert caught.value.finding.rule == "wheel.metadata_membership"


def _sdist(
    path: Path,
    *,
    source_root: Path | None = None,
    version: str | None = None,
    metadata_version: str | None = None,
    root_name: str | None = None,
    unsafe: bool = False,
    symlink: bool = False,
    malicious_egg_entrypoint: bool = False,
    extra: Sequence[tuple[str, bytes]] = (),
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    version = version or (_project_version(source_root) if source_root else "0.1.0")
    root = root_name or "nano_aural_runtime-{0}".format(version)
    assert source_root is not None
    contract = release_artifacts_module._project_contract(source_root)
    metadata_payload = _release_metadata(source_root, version=metadata_version)
    package_payloads = _package_payloads(source_root)
    egg_root = root + "/src/nano_aural_runtime.egg-info/"
    entry_points = release_artifacts_module._expected_entry_points(contract)
    if malicious_egg_entrypoint:
        entry_points += b"malicious = package.module:main\n"
    egg_names = (
        "PKG-INFO",
        "SOURCES.txt",
        "dependency_links.txt",
        "entry_points.txt",
        "requires.txt",
        "top_level.txt",
    )
    source_entries = {"LICENSE", "NOTICE", "README.md", "pyproject.toml"}
    source_entries.update("src/" + name for name in package_payloads)
    source_entries.update("src/nano_aural_runtime.egg-info/" + name for name in egg_names)
    with tarfile.open(path, "w:gz") as archive:
        files = {
            root + "/PKG-INFO": metadata_payload,
            root + "/pyproject.toml": (source_root / "pyproject.toml").read_bytes(),
            root + "/README.md": (source_root / "README.md").read_bytes(),
            root + "/LICENSE": (source_root / "LICENSE").read_bytes(),
            root + "/NOTICE": (source_root / "NOTICE").read_bytes(),
            root + "/setup.cfg": b"[egg_info]\ntag_build = \ntag_date = 0\n\n",
            egg_root + "PKG-INFO": metadata_payload,
            egg_root + "SOURCES.txt": ("\n".join(sorted(source_entries)) + "\n").encode(),
            egg_root + "dependency_links.txt": b"\n",
            egg_root + "entry_points.txt": entry_points,
            egg_root + "requires.txt": release_artifacts_module._expected_requires(contract),
            egg_root + "top_level.txt": release_artifacts_module._expected_top_level(),
        }
        files.update({root + "/src/" + name: payload for name, payload in package_payloads.items()})
        files.update({root + "/" + name: payload for name, payload in extra})
        if unsafe:
            files["../outside"] = b"x"
        for name, payload in files.items():
            info = tarfile.TarInfo(name)
            info.size = len(payload)
            archive.addfile(info, io.BytesIO(payload))
        if symlink:
            info = tarfile.TarInfo(root + "/src/nano_aural_runtime/link.py")
            info.type = tarfile.SYMTYPE
            info.linkname = "../../outside"
            archive.addfile(info)


def test_sdist_member_path_and_link_validation(audit: ModuleType, tmp_path: Path) -> None:
    version = _project_version(ROOT)
    valid = tmp_path / "nano_aural_runtime-{0}.tar.gz".format(version)
    _sdist(valid, source_root=ROOT)
    assert audit.validate_sdist(valid, source_root=ROOT)["validation"] == "VALID"

    traversal = tmp_path / "traversal" / valid.name
    _sdist(traversal, source_root=ROOT, unsafe=True)
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_sdist(traversal, source_root=ROOT)
    assert caught.value.finding.rule == "sdist.unsafe_path"

    symlink = tmp_path / "symlink" / valid.name
    _sdist(symlink, source_root=ROOT, symlink=True)
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_sdist(symlink, source_root=ROOT)
    assert caught.value.finding.rule == "sdist.link"

    for member in ("tests/test_evil.py", "integrations/evil.py", "setup.py"):
        adversarial = tmp_path / member.replace("/", "-") / valid.name
        _sdist(adversarial, source_root=ROOT, extra=((member, b"malicious"),))
        with pytest.raises(audit.ArtifactValidationError) as caught:
            audit.validate_sdist(adversarial, source_root=ROOT)
        assert caught.value.finding.rule == "sdist.not_allowlisted"

    mismatched = tmp_path / "identity" / valid.name
    _sdist(mismatched, source_root=ROOT, metadata_version="9.9.9")
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_sdist(mismatched, source_root=ROOT)
    assert caught.value.finding.rule == "sdist.project_identity"


def test_sdist_rejects_malicious_egg_info_entrypoint(audit: ModuleType, tmp_path: Path) -> None:
    version = _project_version(ROOT)
    candidate = tmp_path / "nano_aural_runtime-{0}.tar.gz".format(version)
    _sdist(candidate, source_root=ROOT, malicious_egg_entrypoint=True)
    with pytest.raises(audit.ArtifactValidationError) as caught:
        audit.validate_sdist(candidate, source_root=ROOT)
    assert caught.value.finding.rule == "sdist.egg_info_entry_points"


def test_candidate_artifact_status_changes_only_after_both_archives_validate(
    audit: ModuleType, tmp_path: Path
) -> None:
    version = _project_version(ROOT)
    wheel = tmp_path / "nano_aural_runtime-{0}-py3-none-any.whl".format(version)
    sdist = tmp_path / "nano_aural_runtime-{0}.tar.gz".format(version)
    _wheel(wheel, source_root=ROOT)
    _sdist(sdist, source_root=ROOT)
    evidence = audit.audit_release(
        ROOT,
        wheels=(wheel,),
        sdists=(sdist,),
        source_date_epoch=1786896000,
    )
    assert evidence["artifacts"]["wheel_validation_status"] == "VALIDATED"
    assert evidence["artifacts"]["sdist_validation_status"] == "VALIDATED"
    assert evidence["artifacts"]["wheels"][0]["sha256"] == audit.sha256_file(wheel)
    assert evidence["artifacts"]["sdists"][0]["sha256"] == audit.sha256_file(sdist)


def test_real_release_candidates_pass_security_validation_from_stable_snapshot(
    audit: ModuleType, tmp_path: Path
) -> None:
    snapshot = tmp_path / "snapshot"
    snapshot.mkdir()
    for name in ("pyproject.toml", "README.md", "LICENSE", "NOTICE"):
        (snapshot / name).write_bytes((ROOT / name).read_bytes())
    for relative, payload in release_artifacts_module._source_files(ROOT).items():
        target = snapshot / "src" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)

    output = tmp_path / "built"
    output.mkdir()
    evidence = release_artifacts_module.build_release_artifacts(output, project_root=snapshot)
    wheel = output / next(item.filename for item in evidence if item.filename.endswith(".whl"))
    sdist = output / next(item.filename for item in evidence if item.filename.endswith(".tar.gz"))
    assert audit.validate_wheel(wheel, source_root=snapshot)["validation"] == "VALID"
    assert audit.validate_sdist(sdist, source_root=snapshot)["validation"] == "VALID"


def test_container_requirement_lock_findings_require_pin_and_hash(
    audit: ModuleType, tmp_path: Path
) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    (tmp_path / "pyproject.toml").write_text("", encoding="utf-8")
    requirements = ops / "requirements-api.txt"
    requirements.write_text("package>=1,<2\n", encoding="utf-8")
    assert {item.rule for item in audit._dependency_lock_findings(tmp_path)} == {
        "dependency.archive_hash_missing",
        "dependency.not_exactly_pinned",
        "dependency.only_binary_missing",
        "dependency.require_hashes_missing",
    }
    requirements.write_text(
        "--require-hashes\n"
        "--only-binary=:all:\n"
        "package==1.2.3 \\\n    --hash=sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    assert audit._dependency_lock_findings(tmp_path) == ()
    declarations = audit._declared_requirements(tmp_path)
    assert declarations == (
        audit._Requirement(
            "container:api",
            "package==1.2.3",
            "package",
            ("a" * 64,),
        ),
    )

    requirements.write_text(
        "--require-hashes\n--only-binary=:all:\npackage==1.2.3 --hash=sha256:not-a-sha256\n",
        encoding="utf-8",
    )
    assert {item.rule for item in audit._dependency_lock_findings(tmp_path)} == {
        "dependency.archive_hash_invalid",
        "dependency.archive_hash_missing",
    }

    requirements.write_text(
        "--require-hashes\n"
        "--only-binary=:all:\n"
        "--index-url=https://packages.invalid/simple\n"
        "package==1.2.3 --hash=sha256:" + "a" * 64 + " --trusted-host=packages.invalid\n",
        encoding="utf-8",
    )
    assert {item.rule for item in audit._dependency_lock_findings(tmp_path)} == {
        "dependency.unsupported_option",
    }


def test_container_base_image_digest_tamper_is_reported(audit: ModuleType, tmp_path: Path) -> None:
    ops = tmp_path / "ops"
    ops.mkdir()
    dockerfile = ops / "Dockerfile.api"
    dockerfile.write_text(
        "FROM python:3.12-slim-bookworm@sha256:" + "a" * 64 + "\n",
        encoding="utf-8",
    )
    assert audit._container_manifest(tmp_path, dockerfile)["findings"] == ()

    dockerfile.write_text("FROM python:3.12-slim-bookworm\n", encoding="utf-8")
    assert audit._container_manifest(tmp_path, dockerfile)["findings"] == (
        {
            "file": "ops/Dockerfile.api",
            "rule": "container.base_image_not_digest_pinned",
        },
    )

    dockerfile.write_text(
        "FROM python:3.12-slim-bookworm@sha256:not-a-digest\n",
        encoding="utf-8",
    )
    assert audit._container_manifest(tmp_path, dockerfile)["findings"] == (
        {
            "file": "ops/Dockerfile.api",
            "rule": "container.base_image_digest_invalid",
        },
    )


def test_compose_and_ci_images_require_exact_digest_and_detect_tamper(
    audit: ModuleType, tmp_path: Path
) -> None:
    digest = "d0f363f8366fbc3f52d172c6e76bc27151c3d643b870e1062b4e8bfe65baf609"
    compose = tmp_path / "compose.yaml"
    workflow = tmp_path / ".github" / "workflows" / "ci.yml"
    workflow.parent.mkdir(parents=True)
    compose.write_text(
        json.dumps(
            {
                "services": {
                    "postgres": {"image": "postgres:16.3-bookworm@sha256:" + digest},
                    "built-locally": {"build": {"context": "."}},
                }
            }
        ),
        encoding="utf-8",
    )
    workflow.write_text(
        "jobs:\n  postgres:\n    services:\n      postgres:\n"
        "        image: postgres:16.3-bookworm@sha256:" + digest + "\n",
        encoding="utf-8",
    )
    records, findings = audit._declared_image_references(tmp_path)
    assert findings == ()
    assert {
        (record["file"], record["name"], record["version"], record["digest"]) for record in records
    } == {
        ("compose.yaml", "postgres", "16.3-bookworm", digest),
        (".github/workflows/ci.yml", "postgres", "16.3-bookworm", digest),
    }

    compose.write_text(
        json.dumps({"services": {"postgres": {"image": "postgres:16.3-bookworm"}}}),
        encoding="utf-8",
    )
    workflow.write_text(
        "jobs:\n  postgres:\n    services:\n      postgres:\n"
        "        image: postgres:16.3-bookworm\n",
        encoding="utf-8",
    )
    _records, findings = audit._declared_image_references(tmp_path)
    assert {(finding.file, finding.rule) for finding in findings} == {
        ("compose.yaml", "container.image_not_digest_pinned"),
        (".github/workflows/ci.yml", "container.image_not_digest_pinned"),
    }

    compose.write_text(
        json.dumps(
            {"services": {"postgres": {"image": "postgres:16.3-bookworm@sha256:not-a-digest"}}}
        ),
        encoding="utf-8",
    )
    workflow.write_text(
        "jobs:\n  postgres:\n    services:\n      postgres:\n"
        "        image: postgres:16.3-bookworm@sha256:abc123\n",
        encoding="utf-8",
    )
    _records, findings = audit._declared_image_references(tmp_path)
    assert {(finding.file, finding.rule) for finding in findings} == {
        ("compose.yaml", "container.image_digest_invalid"),
        (".github/workflows/ci.yml", "container.image_digest_invalid"),
    }

    compose.write_text("{not-json", encoding="utf-8")
    workflow.write_text(
        "jobs:\n  postgres:\n    services:\n      postgres:\n"
        '        image: "postgres:16.3-bookworm\n',
        encoding="utf-8",
    )
    _records, findings = audit._declared_image_references(tmp_path)
    assert {(finding.file, finding.rule) for finding in findings} == {
        ("compose.yaml", "container.image_manifest_invalid"),
        (".github/workflows/ci.yml", "container.image_reference_invalid"),
    }


def test_internal_manifest_is_deterministic_truthful_and_excludes_external_models(
    audit: ModuleType,
) -> None:
    source_date_epoch = 1786896000
    first = audit.audit_release(ROOT, source_date_epoch=source_date_epoch)
    second = audit.audit_release(ROOT, source_date_epoch=source_date_epoch)
    assert json.dumps(audit._json_value(first), sort_keys=True) == json.dumps(
        audit._json_value(second), sort_keys=True
    )
    assert first["schema"] == "nano-aural-internal-release-security-evidence"
    assert first["evidence_kind"] == "internal_inventory_not_cyclonedx"
    assert first["release_gate"] == "NOT_EVALUATED"
    assert first["secret_scan"] == {"status": "CLEAN", "findings": ()}
    assert first["artifacts"]["wheel_validation_status"] == "UNRUN"
    assert first["artifacts"]["sdist_validation_status"] == "UNRUN"
    assert all(set(item) == {"file", "rule"} for item in first["supply_chain_findings"])
    assert first["standard_sbom"]["status"] == "GENERATED"
    assert first["standard_sbom"]["external_validation"] == "UNRUN"

    project = first["release_inputs"]["project"]
    assert project["name"] == "nano-aural-runtime"
    assert project["version"]
    assert project["license"] == "Apache-2.0"
    assert len(project["license_file_sha256"]) == 64
    dependencies = first["dependencies"]
    names = {item["name"] for item in dependencies}
    assert {"psycopg", "pyright", "pytest", "ruff", "setuptools"}.issubset(names)
    assert all(
        set(item)
        == {
            "distribution_archive_hash",
            "group",
            "installed_version",
            "license",
            "metadata_sha256",
            "name",
            "record_sha256",
            "requirement",
            "status",
        }
        for item in dependencies
    )
    supply_chain_findings = {
        (item["file"], item["rule"]) for item in first["supply_chain_findings"]
    }
    assert (
        "ops/Dockerfile.api",
        "container.base_image_not_digest_pinned",
    ) not in supply_chain_findings
    assert (
        "ops/Dockerfile.api",
        "container.base_image_digest_invalid",
    ) not in supply_chain_findings
    assert not any(
        file == "ops/requirements-api.txt"
        and rule
        in {
            "dependency.archive_hash_invalid",
            "dependency.archive_hash_missing",
            "dependency.distribution_archive_hash_unavailable",
            "dependency.not_exactly_pinned",
            "dependency.only_binary_missing",
            "dependency.require_hashes_missing",
            "dependency.unsupported_option",
        }
        for file, rule in supply_chain_findings
    )

    container = first["release_inputs"]["container"]
    copied = {item["file"] for item in container["copied_inputs"]}
    assert "ops/requirements-api.txt" in copied
    assert "ops/load_secrets.py" in copied
    assert any(name.startswith("src/nano_aural_runtime/") for name in copied)
    assert not any("controlfoley" in name or name.endswith((".pth", ".pt")) for name in copied)
    base_image = container["base_images"][0]
    assert base_image == {
        "name": "python",
        "version": "3.12-slim-bookworm",
        "digest": "d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b",
        "license": None,
    }
    assert container["findings"] == ()
    postgres_digest = "d0f363f8366fbc3f52d172c6e76bc27151c3d643b870e1062b4e8bfe65baf609"
    assert {
        (image["file"], image["name"], image["version"], image["digest"])
        for image in container["declared_images"]
    } == {
        ("compose.yaml", "postgres", "16.3-bookworm", postgres_digest),
        (".github/workflows/ci.yml", "postgres", "16.3-bookworm", postgres_digest),
    }

    container_dependencies = {
        item["name"]: item for item in dependencies if item["group"] == "container:api"
    }
    assert {
        name: (
            item["requirement"],
            item["distribution_archive_hash"],
        )
        for name, item in container_dependencies.items()
    } == {
        "psycopg": (
            "psycopg[binary]==3.2.13",
            "a481374514f2da627157f767a9336705ebefe93ea7a0522a6cbacba165da179a",
        ),
        "psycopg-binary": (
            "psycopg-binary==3.2.13",
            "8f1189dc78553ef4b2e55d9e116fc74870191bc6a9a5f4442412a703c4cc6c3b",
        ),
        "typing-extensions": (
            "typing_extensions==4.15.0",
            "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548",
        ),
    }

    assert {item["distribution"] for item in first["external_materials"]} == {
        "EXCLUDED_OPERATOR_SUPPLIED"
    }
    assert {item["name"] for item in first["external_materials"]} == {
        "ControlFoley source checkout",
        "ControlFoley main checkpoint",
        "ControlFoley external weights",
        "Hugging Face cache and snapshots",
        "Model input/output media",
        "Private test and benchmark fixtures",
    }
    for capability in first["capabilities"].values():
        assert capability["status"] == "UNRUN"


def test_spdx_2_3_sbom_is_deterministic_strict_and_marks_unknowns(
    audit: ModuleType,
) -> None:
    epoch = 1786896000
    first = audit.build_spdx_sbom(ROOT, source_date_epoch=epoch)
    second = audit.build_spdx_sbom(ROOT, source_date_epoch=epoch)
    assert audit._canonical_json(first) == audit._canonical_json(second)
    audit.validate_spdx_sbom(first)
    audit.validate_spdx_sbom(json.loads(audit._canonical_json(first)))
    assert first["spdxVersion"] == "SPDX-2.3"
    assert first["dataLicense"] == "CC0-1.0"
    assert first["creationInfo"] == {
        "created": "2026-08-16T16:00:00Z",
        "creators": ("Tool: nanoAuralRuntime-release-security-audit-1",),
    }
    packages = first["packages"]
    assert packages[0]["name"] == "nano-aural-runtime"
    assert packages[0]["licenseDeclared"] == "Apache-2.0"
    dependency_packages = [
        package for package in packages if package["primaryPackagePurpose"] == "LIBRARY"
    ]
    dependency_names = {package["name"] for package in dependency_packages}
    assert {"psycopg", "pyright", "pytest", "ruff", "setuptools"}.issubset(dependency_names)
    assert all(package["licenseConcluded"] == "NOASSERTION" for package in dependency_packages)
    known_ids = {package["SPDXID"] for package in packages}
    assert all(
        relationship["relatedSpdxElement"] in known_ids for relationship in first["relationships"]
    )
    relationships = {
        (
            relationship["spdxElementId"],
            relationship["relationshipType"],
            relationship["relatedSpdxElement"],
        )
        for relationship in first["relationships"]
    }
    project_id = next(
        package["SPDXID"] for package in packages if package["name"] == "nano-aural-runtime"
    )
    container_id = next(
        package["SPDXID"]
        for package in packages
        if package["name"] == "nano-aural-runtime-api-container"
    )

    def declared_groups(package: Mapping[str, object]) -> set[str]:
        comment = cast(str, package["comment"])
        return set(json.loads(comment.split("=", 1)[1])["groups"])

    setuptools = next(package for package in packages if package["name"] == "setuptools")
    pytest_package = next(package for package in packages if package["name"] == "pytest")
    psycopg_project = next(
        package
        for package in packages
        if package["name"] == "psycopg" and "optional:postgres-test" in declared_groups(package)
    )
    psycopg_container = next(
        package
        for package in packages
        if package["name"] == "psycopg" and "container:api" in declared_groups(package)
    )
    assert (setuptools["SPDXID"], "BUILD_DEPENDENCY_OF", project_id) in relationships
    assert (pytest_package["SPDXID"], "DEV_DEPENDENCY_OF", project_id) in relationships
    assert (psycopg_project["SPDXID"], "TEST_DEPENDENCY_OF", project_id) in relationships
    assert (psycopg_project["SPDXID"], "OPTIONAL_DEPENDENCY_OF", project_id) in relationships
    assert (container_id, "DEPENDS_ON", psycopg_container["SPDXID"]) in relationships
    assert not any(
        source == project_id and relationship == "DEPENDS_ON"
        for source, relationship, _target in relationships
    )
    assert not all(
        relationship["relationshipType"] in ("DESCRIBES", "DEPENDS_ON")
        for relationship in first["relationships"]
    )

    mutated = dict(first)
    mutated["relationships"] = first["relationships"][:-1]
    with pytest.raises(ValueError, match="graph"):
        audit.validate_spdx_sbom(mutated)

    namespace_mutation = json.loads(audit._canonical_json(first))
    dependency = next(
        package
        for package in namespace_mutation["packages"]
        if package["primaryPackagePurpose"] == "LIBRARY"
        and "container:api" not in json.loads(package["comment"].split("=", 1)[1])["groups"]
    )
    declarations = json.loads(dependency["comment"].split("=", 1)[1])
    declarations["requirements"][0] += "; python_version >= '3.9'"
    dependency["comment"] = "nanoAural-declarations=" + json.dumps(
        declarations, sort_keys=True, separators=(",", ":")
    )
    with pytest.raises(ValueError, match="namespace"):
        audit.validate_spdx_sbom(namespace_mutation)

    unrun = audit.audit_release(ROOT)
    assert unrun["standard_sbom"] == {
        "format": "SPDX-2.3-JSON",
        "status": "UNRUN",
        "reason": "source_date_epoch was not supplied",
        "external_validation": "UNRUN",
    }


def test_spdx_container_lock_uses_sealed_version_when_installed_version_differs(
    audit: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    typing_hash = "f0fa19c6845758ab08074a0cfa8b7aecb71c999ca73d62883bc25cc018c4e548"
    psycopg_hash = "a481374514f2da627157f767a9336705ebefe93ea7a0522a6cbacba165da179a"
    declared = (
        audit._Requirement(
            "container:api",
            "psycopg[binary]==3.2.13",
            "psycopg",
            (psycopg_hash,),
        ),
        audit._Requirement("optional:dev", "psycopg[binary]>=3.1,<4", "psycopg"),
        audit._Requirement(
            "container:api",
            "typing_extensions==4.15.0",
            "typing-extensions",
            (typing_hash,),
        ),
        audit._Requirement("optional:dev", "typing_extensions>=4", "typing-extensions"),
    )

    class FakeDistribution:
        def __init__(self, version: str) -> None:
            self.version = version
            self.metadata = {"License-Expression": "MIT"}

        def read_text(self, filename: str) -> str:
            return "fake-" + filename

    installed = {"psycopg": "3.2.13", "typing-extensions": "4.16.0"}
    monkeypatch.setattr(audit, "_declared_requirements", lambda _root: declared)
    monkeypatch.setattr(
        audit.importlib.metadata,
        "distribution",
        lambda name: FakeDistribution(installed[name]),
    )

    inventory = audit.dependency_inventory(ROOT)
    locked_inventory = next(
        item
        for item in inventory
        if item["name"] == "typing-extensions" and item["group"] == "container:api"
    )
    assert locked_inventory["requirement"] == "typing_extensions==4.15.0"
    assert locked_inventory["installed_version"] == "4.16.0"
    assert locked_inventory["distribution_archive_hash"] == typing_hash

    first = audit.build_spdx_sbom(ROOT, source_date_epoch=1786896000)
    second = audit.build_spdx_sbom(ROOT, source_date_epoch=1786896000)
    assert audit._canonical_json(first) == audit._canonical_json(second)
    audit.validate_spdx_sbom(first)
    audit.validate_spdx_sbom(json.loads(audit._canonical_json(first)))

    packages = first["packages"]
    project_id = next(
        package["SPDXID"]
        for package in packages
        if package["primaryPackagePurpose"] == "APPLICATION"
    )
    container_id = next(
        package["SPDXID"] for package in packages if package["primaryPackagePurpose"] == "CONTAINER"
    )
    typing_packages = [package for package in packages if package["name"] == "typing-extensions"]
    assert {package["versionInfo"] for package in typing_packages} == {"4.15.0", "4.16.0"}
    assert len({package["SPDXID"] for package in typing_packages}) == 2
    locked_typing = next(
        package for package in typing_packages if package["versionInfo"] == "4.15.0"
    )
    installed_typing = next(
        package for package in typing_packages if package["versionInfo"] == "4.16.0"
    )
    assert locked_typing["checksums"] == ({"algorithm": "SHA256", "checksumValue": typing_hash},)
    assert locked_typing["externalRefs"] == (
        {
            "referenceCategory": "PACKAGE-MANAGER",
            "referenceType": "purl",
            "referenceLocator": "pkg:pypi/typing-extensions@4.15.0",
        },
    )
    assert locked_typing["licenseDeclared"] == "NOASSERTION"
    assert installed_typing["licenseDeclared"] == "MIT"

    relationships = {
        (
            relationship["spdxElementId"],
            relationship["relationshipType"],
            relationship["relatedSpdxElement"],
        )
        for relationship in first["relationships"]
    }
    assert (container_id, "DEPENDS_ON", locked_typing["SPDXID"]) in relationships
    assert (container_id, "DEPENDS_ON", installed_typing["SPDXID"]) not in relationships
    assert (installed_typing["SPDXID"], "DEV_DEPENDENCY_OF", project_id) in relationships

    psycopg_packages = [package for package in packages if package["name"] == "psycopg"]
    assert len(psycopg_packages) == 1
    psycopg = psycopg_packages[0]
    assert psycopg["versionInfo"] == "3.2.13"
    assert psycopg["checksums"] == ({"algorithm": "SHA256", "checksumValue": psycopg_hash},)
    assert (container_id, "DEPENDS_ON", psycopg["SPDXID"]) in relationships
    assert (psycopg["SPDXID"], "DEV_DEPENDENCY_OF", project_id) in relationships

    tampered = json.loads(audit._canonical_json(first))
    locked = next(
        package
        for package in tampered["packages"]
        if package["name"] == "typing-extensions" and package["versionInfo"] == "4.15.0"
    )
    locked["versionInfo"] = "4.16.0"
    locked["externalRefs"][0]["referenceLocator"] = "pkg:pypi/typing-extensions@4.16.0"
    with pytest.raises(ValueError, match="not sealed"):
        audit.validate_spdx_sbom(tampered)


def test_evidence_output_is_private_exclusive_and_contains_no_gate_claim(
    audit: ModuleType, tmp_path: Path
) -> None:
    output = tmp_path / "release-security.json"
    spdx_output = tmp_path / "release-security.spdx.json"
    arguments = (
        "--root",
        str(ROOT),
        "--source-date-epoch",
        "1786896000",
        "--output",
        str(output),
        "--spdx-output",
        str(spdx_output),
    )
    assert audit.main(arguments) == 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert stat.S_IMODE(spdx_output.stat().st_mode) == 0o600
    evidence = json.loads(output.read_text(encoding="utf-8"))
    assert evidence["release_gate"] == "NOT_EVALUATED"
    assert evidence["capabilities"]["cyclonedx_sbom"]["status"] == "UNRUN"
    spdx = json.loads(spdx_output.read_text(encoding="utf-8"))
    assert spdx["spdxVersion"] == "SPDX-2.3"
    assert evidence["standard_sbom"]["sha256"] == audit.sha256_file(spdx_output)
    assert audit.main(arguments) == 2
    assert not tuple(tmp_path.glob(".nano-aural-release-security-*"))


def test_evidence_set_preflights_all_targets_before_any_publish(
    audit: ModuleType, tmp_path: Path
) -> None:
    output = tmp_path / "internal.json"
    spdx_output = tmp_path / "spdx.json"
    spdx_output.write_text("operator-owned\n", encoding="utf-8")
    assert (
        audit.main(
            (
                "--root",
                str(ROOT),
                "--source-date-epoch",
                "1786896000",
                "--output",
                str(output),
                "--spdx-output",
                str(spdx_output),
            )
        )
        == 2
    )
    assert not output.exists()
    assert spdx_output.read_text(encoding="utf-8") == "operator-owned\n"
    assert not tuple(tmp_path.glob(".nano-aural-release-security-*"))


def test_evidence_set_fsync_failure_rolls_back_all_outputs_and_fsyncs_rollback(
    audit: ModuleType, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    output = tmp_path / "internal.json"
    spdx_output = tmp_path / "spdx.json"
    original = audit._fsync_directory
    calls = 0

    def fail_first(descriptor: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(CANARY)
        original(descriptor)

    monkeypatch.setattr(audit, "_fsync_directory", fail_first)
    assert (
        audit.main(
            (
                "--root",
                str(ROOT),
                "--source-date-epoch",
                "1786896000",
                "--output",
                str(output),
                "--spdx-output",
                str(spdx_output),
            )
        )
        == 2
    )
    assert calls >= 2
    assert not output.exists()
    assert not spdx_output.exists()
    assert not tuple(tmp_path.glob(".nano-aural-release-security-*"))
