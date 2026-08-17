# pyright: reportMissingImports=false
"""Phase 6 migration immutability and bounded recovery-set tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from importlib import resources
from importlib.util import find_spec
from pathlib import Path
from typing import BinaryIO, Iterator
from uuid import uuid4

import pytest

from nano_aural_runtime.durable import migration_admin, release_recovery
from nano_aural_runtime.durable.domain import (
    ArtifactKind,
    AssetKind,
    AssetRecord,
    AssetState,
    BlobRecord,
    BlobState,
    DeploymentRecord,
    JobInput,
    WorkerRecord,
)
from nano_aural_runtime.durable.migration_admin import _open_manifest
from nano_aural_runtime.durable.migrations import (
    MigrationIntegrityError,
    _migration_sources,
    adopt_legacy_postgres_migration_checksums,
    apply_postgres_migrations,
    grant_postgres_runtime_role,
    verify_postgres_migrations,
)
from nano_aural_runtime.durable.postgres_repository import PostgresDurableRepository
from nano_aural_runtime.durable.publication import (
    PostgresPublicationRepository,
    PublicationSpec,
)
from nano_aural_runtime.durable.queue import PostgresLeaseQueue
from nano_aural_runtime.durable.release_recovery import (
    _LEDGER_AUTHORITY_SQL,
    RecoveryEnvironmentUnavailable,
    RecoveryLimits,
    RecoverySetError,
    SubprocessPostgresRecoveryTools,
    create_recovery_set,
    restore_recovery_set,
)
from nano_aural_runtime.durable.release_recovery import (
    main as recovery_main,
)
from nano_aural_runtime.durable.storage import LocalBlobStore


class FakePostgresTools:
    def __init__(self, dump_bytes: bytes = b"PGDMP\x01bounded-test", *, empty: bool = True) -> None:
        self.dump_bytes = dump_bytes
        self.empty = empty
        self.restored = b""
        self.dump_calls = 0
        self.restore_calls = 0
        self.authoritative = True
        self.identity = "1" * 64

    def database_identity(self) -> str:
        return self.identity

    def dump(self, destination: Path, max_bytes: int) -> tuple[str, int]:
        self.dump_calls += 1
        if len(self.dump_bytes) > max_bytes:
            raise RecoverySetError("RECOVERY_BOUND_EXCEEDED")
        descriptor = os.open(str(destination), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(self.dump_bytes)
        return hashlib.sha256(self.dump_bytes).hexdigest(), len(self.dump_bytes)

    def target_database_is_empty(self) -> bool:
        return self.empty

    def target_database_has_sealed_migration_ledger(self) -> bool:
        return self.authoritative

    def restore(self, source: Path, expected_sha256: str, max_bytes: int) -> None:
        self.restore_calls += 1
        restored = source.read_bytes()
        assert len(restored) <= max_bytes
        assert hashlib.sha256(restored).hexdigest() == expected_sha256
        self.restored = restored
        self.empty = False
        self.authoritative = True


def _private(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


def _canonical(root: Path) -> tuple[bytes, bytes]:
    first = b"verified-input-content"
    second = b"visible-winning-artifact"
    store = LocalBlobStore(root)
    try:
        store.put_immutable(first)
        store.put_immutable(second)
    finally:
        store.close()
    root.chmod(0o700)
    return first, second


def _recovery_fixture(tmp_path: Path) -> tuple[FakePostgresTools, Path, tuple[bytes, bytes]]:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical-source")
    content = _canonical(canonical_root)
    tools = FakePostgresTools()
    recovery_set = tmp_path / "recovery-set"
    evidence = create_recovery_set(
        tools,
        canonical_root,
        recovery_set,
        RecoveryLimits(max_objects=4, max_total_blob_bytes=1024, max_database_dump_bytes=1024),
    )
    assert evidence.object_count == 2
    return tools, recovery_set, content


def test_recovery_set_is_manifest_last_redacted_and_round_trips_without_overwrite(
    tmp_path: Path,
) -> None:
    tools, recovery_set, content = _recovery_fixture(tmp_path)
    manifest = json.loads((recovery_set / "manifest.json").read_text(encoding="utf-8"))
    serialized = json.dumps(manifest, sort_keys=True)
    assert set(manifest) == {"schema", "database", "canonical"}
    assert "/" not in json.dumps(manifest["database"])
    assert "secret" not in serialized.lower()
    assert "namespace" not in serialized.lower()
    for path in (recovery_set, *recovery_set.rglob("*")):
        information = path.lstat()
        expected_mode = 0o700 if path.is_dir() else 0o600
        assert stat.S_IMODE(information.st_mode) == expected_mode

    restored_root = tmp_path / "canonical-restored"
    evidence = restore_recovery_set(
        tools,
        recovery_set,
        restored_root,
        RecoveryLimits(max_objects=4, max_total_blob_bytes=1024, max_database_dump_bytes=1024),
    )
    assert evidence.object_count == 2
    assert tools.restored == tools.dump_bytes
    restored = LocalBlobStore(restored_root)
    try:
        for value in content:
            digest = hashlib.sha256(value).hexdigest()
            with restored.open_reader(LocalBlobStore.canonical_key(digest)) as reader:
                assert reader.read() == value
    finally:
        restored.close()
    with pytest.raises(RecoverySetError, match="RECOVERY_TARGET_EXISTS"):
        restore_recovery_set(tools, recovery_set, restored_root)
    with pytest.raises(RecoverySetError, match="RECOVERY_TARGET_EXISTS"):
        create_recovery_set(tools, tmp_path / "canonical-source", recovery_set)


def test_backup_rejects_source_without_full_release_authority_before_dump(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical-source")
    _canonical(canonical_root)
    tools = FakePostgresTools()
    tools.authoritative = False
    with pytest.raises(RecoverySetError, match="RECOVERY_DATABASE_AUTHORITY_INVALID"):
        create_recovery_set(tools, canonical_root, tmp_path / "set")
    assert tools.dump_calls == 0


def test_restore_rejects_tampered_dump_blob_manifest_and_nonempty_database(
    tmp_path: Path,
) -> None:
    tools, recovery_set, _content = _recovery_fixture(tmp_path)
    dump = recovery_set / "database.dump"
    dump.write_bytes(dump.read_bytes() + b"tamper")
    with pytest.raises(RecoverySetError, match="RECOVERY_DATABASE_INTEGRITY_FAILED"):
        restore_recovery_set(tools, recovery_set, tmp_path / "target-dump")

    shutil.rmtree(recovery_set)
    _tools, recovery_set, _content = _recovery_fixture(tmp_path)
    blob = next((recovery_set / "canonical").rglob("?" * 64))
    blob.write_bytes(b"tamper")
    with pytest.raises(RecoverySetError, match="RECOVERY_CANONICAL_INTEGRITY_FAILED"):
        restore_recovery_set(tools, recovery_set, tmp_path / "target-blob")

    shutil.rmtree(recovery_set)
    _tools, recovery_set, _content = _recovery_fixture(tmp_path)
    manifest = recovery_set / "manifest.json"
    manifest.write_text('{"schema":"wrong"}\n', encoding="utf-8")
    with pytest.raises(RecoverySetError, match="RECOVERY_MANIFEST_INVALID"):
        restore_recovery_set(tools, recovery_set, tmp_path / "target-manifest")

    shutil.rmtree(recovery_set)
    _tools, recovery_set, _content = _recovery_fixture(tmp_path)
    tools.empty = False
    with pytest.raises(RecoverySetError, match="RECOVERY_DATABASE_NOT_EMPTY"):
        restore_recovery_set(tools, recovery_set, tmp_path / "target-nonempty")
    assert not (tmp_path / "target-nonempty").exists()


def test_backup_rejects_namespace_symlinks_bounds_and_incomplete_set(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical-source")
    _canonical(canonical_root)
    (canonical_root / "unexpected").write_bytes(b"not canonical")
    with pytest.raises(RecoverySetError, match="RECOVERY_CANONICAL_NAMESPACE_INVALID"):
        create_recovery_set(FakePostgresTools(), canonical_root, tmp_path / "bad-set")
    assert not (tmp_path / "bad-set" / "manifest.json").exists()

    canonical_root = _private(tmp_path / "bounded-source")
    _canonical(canonical_root)
    with pytest.raises(RecoverySetError, match="RECOVERY_BOUND_EXCEEDED"):
        create_recovery_set(
            FakePostgresTools(),
            canonical_root,
            tmp_path / "bounded-set",
            RecoveryLimits(max_objects=1, max_total_blob_bytes=1024, max_database_dump_bytes=1024),
        )
    assert not (tmp_path / "bounded-set" / "manifest.json").exists()

    symlink_root = _private(tmp_path / "symlink-source")
    (symlink_root / "blobs").symlink_to(canonical_root / "blobs", target_is_directory=True)
    with pytest.raises(RecoverySetError, match="RECOVERY_CANONICAL_NAMESPACE_INVALID"):
        create_recovery_set(FakePostgresTools(), symlink_root, tmp_path / "symlink-set")


@pytest.mark.parametrize(
    "fault_stage", ("after_dump", "after_canonical_copy", "after_manifest", "after_publish")
)
def test_backup_fault_checkpoints_restart_to_one_complete_set(
    tmp_path: Path, fault_stage: str
) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical")
    _canonical(canonical_root)
    tools = FakePostgresTools()
    recovery_set = tmp_path / "set"

    def fail(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError("simulated process fault")

    with pytest.raises(RuntimeError, match="simulated process fault"):
        create_recovery_set(tools, canonical_root, recovery_set, progress_hook=fail)
    evidence = create_recovery_set(tools, canonical_root, recovery_set)
    assert evidence.object_count == 2
    assert set(entry.name for entry in recovery_set.iterdir()) == {
        "database.dump",
        "canonical",
        "manifest.json",
    }
    assert not tuple(tmp_path.glob(".set.nano-backup-*"))


def test_backup_complete_build_resumes_without_second_dump_and_rejects_switched_database(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical")
    _canonical(canonical_root)
    tools = FakePostgresTools()
    recovery_set = tmp_path / "set"

    def fail_after_complete(stage: str) -> None:
        if stage == "after_manifest_complete":
            raise RuntimeError("simulated process fault")

    with pytest.raises(RuntimeError, match="simulated process fault"):
        create_recovery_set(tools, canonical_root, recovery_set, progress_hook=fail_after_complete)
    assert tools.dump_calls == 1
    assert create_recovery_set(tools, canonical_root, recovery_set).object_count == 2
    assert tools.dump_calls == 1

    second_set = tmp_path / "second-set"

    def fail_after_dump(stage: str) -> None:
        if stage == "after_dump":
            raise RuntimeError("simulated process fault")

    with pytest.raises(RuntimeError, match="simulated process fault"):
        create_recovery_set(tools, canonical_root, second_set, progress_hook=fail_after_dump)
    dump_calls = tools.dump_calls
    tools.identity = "2" * 64
    with pytest.raises(RecoverySetError, match="RECOVERY_DATABASE_IDENTITY_CHANGED"):
        create_recovery_set(tools, canonical_root, second_set)
    assert tools.dump_calls == dump_calls


def test_backup_complete_build_retries_fsync_before_state_advance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical")
    _canonical(canonical_root)
    tools = FakePostgresTools()
    recovery_set = tmp_path / "set"

    def fail_after_complete(stage: str) -> None:
        if stage == "after_manifest_complete":
            raise RuntimeError("simulated process fault")

    with pytest.raises(RuntimeError, match="simulated process fault"):
        create_recovery_set(tools, canonical_root, recovery_set, progress_hook=fail_after_complete)
    assert tools.dump_calls == 1

    real_fsync_tree = release_recovery._fsync_tree
    interrupted_calls = 0

    def interrupted_fsync(_root: Path) -> None:
        nonlocal interrupted_calls
        interrupted_calls += 1
        raise RuntimeError("simulated fsync interruption")

    monkeypatch.setattr(release_recovery, "_fsync_tree", interrupted_fsync)
    with pytest.raises(RuntimeError, match="simulated fsync interruption"):
        create_recovery_set(tools, canonical_root, recovery_set)
    assert interrupted_calls == 1
    assert tools.dump_calls == 1

    completed_syncs = 0

    def completed_fsync(root: Path) -> None:
        nonlocal completed_syncs
        completed_syncs += 1
        real_fsync_tree(root)

    monkeypatch.setattr(release_recovery, "_fsync_tree", completed_fsync)
    assert create_recovery_set(tools, canonical_root, recovery_set).object_count == 2
    assert completed_syncs == 1
    assert tools.dump_calls == 1


def test_backup_publication_is_atomic_no_replace_under_target_race(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical")
    _canonical(canonical_root)
    recovery_set = tmp_path / "set"

    def create_racing_target(stage: str) -> None:
        if stage == "after_manifest":
            recovery_set.mkdir(mode=0o700)
            marker = recovery_set / "other-operator"
            marker.write_bytes(b"preserve")
            marker.chmod(0o600)

    with pytest.raises(RecoverySetError, match="RECOVERY_TARGET_EXISTS"):
        create_recovery_set(
            FakePostgresTools(),
            canonical_root,
            recovery_set,
            progress_hook=create_racing_target,
        )
    assert (recovery_set / "other-operator").read_bytes() == b"preserve"


def test_backup_fsyncs_files_and_every_nested_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical")
    _canonical(canonical_root)
    modes: list[int] = []
    real_fsync = release_recovery.os.fsync

    def recording_fsync(descriptor: int) -> None:
        modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(release_recovery.os, "fsync", recording_fsync)
    create_recovery_set(FakePostgresTools(), canonical_root, tmp_path / "set")
    assert any(stat.S_ISREG(mode) for mode in modes)
    assert sum(1 for mode in modes if stat.S_ISDIR(mode)) >= 5


@pytest.mark.parametrize(
    "fault_stage",
    (
        "after_canonical_stage",
        "before_database_restore",
        "after_database_commit_before_marker",
        "after_database_restore",
        "after_canonical_publish",
    ),
)
def test_restore_fault_checkpoints_resume_without_second_database_restore(
    tmp_path: Path, fault_stage: str
) -> None:
    tools, recovery_set, _content = _recovery_fixture(tmp_path)
    target = tmp_path / "restored"

    def fail(stage: str) -> None:
        if stage == fault_stage:
            raise RuntimeError("simulated process fault")

    with pytest.raises(RuntimeError, match="simulated process fault"):
        restore_recovery_set(tools, recovery_set, target, progress_hook=fail)
    evidence = restore_recovery_set(tools, recovery_set, target)
    assert evidence.object_count == 2
    assert tools.restore_calls == 1
    assert tools.restored == tools.dump_bytes
    assert not tuple(tmp_path.glob(".restored.nano-restore-*"))


def test_restore_resume_rejects_switched_target_database_before_restore(tmp_path: Path) -> None:
    tools, recovery_set, _content = _recovery_fixture(tmp_path)
    target = tmp_path / "restored"

    def fail(stage: str) -> None:
        if stage == "before_database_restore":
            raise RuntimeError("simulated process fault")

    with pytest.raises(RuntimeError, match="simulated process fault"):
        restore_recovery_set(tools, recovery_set, target, progress_hook=fail)
    tools.identity = "3" * 64
    with pytest.raises(RecoverySetError, match="RECOVERY_DATABASE_IDENTITY_CHANGED"):
        restore_recovery_set(tools, recovery_set, target)
    assert tools.restore_calls == 0
    assert not target.exists()


def test_recovery_rejects_per_object_growth_temp_quota_and_empty_prefix_dirs(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    canonical_root = _private(tmp_path / "canonical")
    _canonical(canonical_root)
    with pytest.raises(RecoverySetError, match="RECOVERY_BOUND_EXCEEDED"):
        create_recovery_set(
            FakePostgresTools(),
            canonical_root,
            tmp_path / "single-bound",
            RecoveryLimits(max_single_blob_bytes=4, max_total_blob_bytes=1024),
        )
    with pytest.raises(RecoverySetError, match="RECOVERY_BOUND_EXCEEDED"):
        create_recovery_set(
            FakePostgresTools(),
            canonical_root,
            tmp_path / "temp-bound",
            RecoveryLimits(max_temporary_bytes=4),
        )

    _tools, recovery_set, _content = _recovery_fixture(tmp_path)
    extra = recovery_set / "canonical" / "blobs" / "sha256" / "aa" / "bb"
    extra.mkdir(parents=True, mode=0o700)
    for directory in (extra, extra.parent):
        directory.chmod(0o700)
    with pytest.raises(RecoverySetError, match="RECOVERY_CANONICAL_NAMESPACE_INVALID"):
        restore_recovery_set(FakePostgresTools(), recovery_set, tmp_path / "empty-prefix-target")


def test_missing_postgres_backup_toolchain_is_explicit_environment_unavailable(
    tmp_path: Path,
) -> None:
    tmp_path.chmod(0o700)
    binaries = _private(tmp_path / "bin")
    service_file = tmp_path / "pg_service.conf"
    service_file.write_text("[release]\nhost=/private/test\n", encoding="utf-8")
    service_file.chmod(0o600)
    with pytest.raises(RecoveryEnvironmentUnavailable, match="RECOVERY_TOOLCHAIN_UNAVAILABLE"):
        SubprocessPostgresRecoveryTools(binaries, service_file, "release")


def _fake_postgres_toolchain(
    tmp_path: Path, *, psql_body: str | None = None, dump_body: str | None = None
) -> tuple[Path, Path]:
    binaries = _private(tmp_path / ("bin-" + uuid4().hex))
    common = (
        "import os,sys\n"
        "assert os.environ['PGSERVICEFILE'].startswith('/dev/fd/')\n"
        "assert os.environ['PGOPTIONS']=='-c search_path=pg_catalog,public'\n"
        "assert open(os.environ['PGSERVICEFILE'],encoding='utf-8').read().startswith('[release]')\n"
        "assert any(a=='--dbname=service=release' for a in sys.argv)\n"
    )
    scripts = {
        "pg_dump": common
        + (dump_body or "sys.stdout.buffer.write(b'PGDMP\\x01descriptor-bound')\n"),
        "pg_restore": common + "assert sys.stdin.buffer.read().startswith(b'PGDMP')\n",
        "psql": common
        + (
            psql_body
            or "q=sys.argv[-1]\n"
            "print('cluster-system:database-oid:database-name' if 'pg_control_system' in q "
            "else ('1' if 'nano_aural_schema_migrations' in q else '0'))\n"
        ),
    }
    for name, body in scripts.items():
        path = binaries / name
        path.write_text("#!" + sys.executable + "\n" + body, encoding="utf-8")
        path.chmod(0o700)
    service_file = tmp_path / ("pg-service-" + uuid4().hex + ".conf")
    service_file.write_text("[release]\nhost=/private/fixed\n", encoding="utf-8")
    service_file.chmod(0o600)
    return binaries, service_file


def test_subprocess_tools_bind_service_and_binaries_and_bound_all_io(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tmp_path.chmod(0o700)
    binaries, service_file = _fake_postgres_toolchain(tmp_path)
    popen_calls: list[dict[str, object]] = []
    run_calls: list[dict[str, object]] = []
    real_popen = release_recovery.subprocess.Popen
    real_run = release_recovery.subprocess.run
    real_fsync = release_recovery.os.fsync
    fsync_modes: list[int] = []

    def recording_popen(*args: object, **kwargs: object) -> object:
        popen_calls.append(dict(kwargs))
        return real_popen(*args, **kwargs)  # type: ignore[arg-type,call-overload]

    def recording_run(*args: object, **kwargs: object) -> object:
        run_calls.append(dict(kwargs))
        return real_run(*args, **kwargs)  # type: ignore[arg-type,call-overload]

    def recording_fsync(descriptor: int) -> None:
        fsync_modes.append(os.fstat(descriptor).st_mode)
        real_fsync(descriptor)

    monkeypatch.setattr(release_recovery.subprocess, "Popen", recording_popen)
    monkeypatch.setattr(release_recovery.subprocess, "run", recording_run)
    monkeypatch.setattr(release_recovery.os, "fsync", recording_fsync)
    tools = SubprocessPostgresRecoveryTools(binaries, service_file, "release", timeout_seconds=5)
    try:
        assert len(tools.database_identity()) == 64
        dump = tmp_path / "database.dump"
        digest, size = tools.dump(dump, 1024)
        assert (digest, size) == (
            hashlib.sha256(dump.read_bytes()).hexdigest(),
            dump.stat().st_size,
        )
        assert tools.target_database_is_empty()
        assert tools.target_database_has_sealed_migration_ledger()
        tools.restore(dump, digest, 1024)
        assert any(stat.S_ISREG(mode) for mode in fsync_modes)
        assert popen_calls and run_calls
        for call in (*popen_calls, *run_calls):
            assert call["shell"] is False
            assert call["stderr"] is subprocess.DEVNULL
            assert call["env"] == tools._environment
            assert call["pass_fds"]

        service_file.write_text("[release]\nhost=/private/swap!\n", encoding="utf-8")
        with pytest.raises(RecoverySetError, match="RECOVERY_SERVICE_FILE_CHANGED"):
            tools.database_identity()
    finally:
        tools.close()

    binaries, service_file = _fake_postgres_toolchain(tmp_path)
    tools = SubprocessPostgresRecoveryTools(binaries, service_file, "release")
    try:
        dump = tmp_path / "mutable.dump"
        digest, _size = tools.dump(dump, 1024)
        original_run = tools._run

        def mutate_dump_during_restore(
            binary: str,
            arguments: tuple[str, ...],
            source: BinaryIO | None = None,
            *,
            capture_limit: int = 0,
        ) -> bytes:
            if binary == "pg_restore":
                with dump.open("ab") as stream:
                    stream.write(b"growth")
            return original_run(binary, arguments, source, capture_limit=capture_limit)

        monkeypatch.setattr(tools, "_run", mutate_dump_during_restore)
        with pytest.raises(RecoverySetError, match="RECOVERY_DATABASE_INTEGRITY_FAILED"):
            tools.restore(dump, digest, 1024)
    finally:
        tools.close()

    binaries, service_file = _fake_postgres_toolchain(
        tmp_path,
        psql_body="open(sys.argv[0],'a',encoding='utf-8').write('#changed')\n"
        "print('cluster-system:database-oid:database-name')\n",
    )
    tools = SubprocessPostgresRecoveryTools(binaries, service_file, "release")
    try:
        with pytest.raises(RecoverySetError, match="RECOVERY_TOOLCHAIN_CHANGED"):
            tools.database_identity()
    finally:
        tools.close()

    binaries, service_file = _fake_postgres_toolchain(tmp_path)
    tools = SubprocessPostgresRecoveryTools(binaries, service_file, "release")
    try:
        (binaries / "psql").write_text("changed", encoding="utf-8")
        with pytest.raises(RecoverySetError, match="RECOVERY_TOOLCHAIN_CHANGED"):
            tools.database_identity()
    finally:
        tools.close()


def test_subprocess_tools_enforce_dump_output_and_timeout_bounds(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    binaries, service_file = _fake_postgres_toolchain(
        tmp_path, dump_body="sys.stdout.buffer.write(b'x'*1025)\n"
    )
    tools = SubprocessPostgresRecoveryTools(binaries, service_file, "release")
    try:
        with pytest.raises(RecoverySetError, match="RECOVERY_BOUND_EXCEEDED"):
            tools.dump(tmp_path / "too-large.dump", 1024)
    finally:
        tools.close()

    binaries, service_file = _fake_postgres_toolchain(
        tmp_path, psql_body="import time\ntime.sleep(2)\nprint('late')\n"
    )
    tools = SubprocessPostgresRecoveryTools(binaries, service_file, "release", timeout_seconds=1)
    try:
        with pytest.raises(RecoverySetError, match="RECOVERY_DATABASE_TOOL_FAILED"):
            tools.database_identity()
    finally:
        tools.close()


def test_migration_adoption_manifest_is_owner_only_strict_and_bounded(tmp_path: Path) -> None:
    tmp_path.chmod(0o700)
    manifest = tmp_path / "trusted.json"
    manifest.write_text(
        json.dumps(
            {
                "schema": "nano-aural-migration-checksums/v1",
                "migrations": [{"filename": "0001_test.sql", "sha256": "a" * 64}],
            }
        ),
        encoding="utf-8",
    )
    manifest.chmod(0o600)
    assert _open_manifest(str(manifest)) == {"0001_test.sql": "a" * 64}
    manifest.chmod(0o644)
    with pytest.raises(ValueError, match="owner-only"):
        _open_manifest(str(manifest))
    manifest.chmod(0o600)
    link = tmp_path / "trusted-link.json"
    link.symlink_to(manifest)
    with pytest.raises(OSError):
        _open_manifest(str(link))


def test_migration_source_sequence_cannot_have_number_gaps(tmp_path: Path) -> None:
    (tmp_path / "0001_first.sql").write_text("SELECT 1;\n", encoding="utf-8")
    (tmp_path / "0003_gap.sql").write_text("SELECT 3;\n", encoding="utf-8")
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_SOURCE_SEQUENCE_INVALID"):
        _migration_sources(tmp_path)


def test_runtime_grant_rejects_every_nonfixed_role_before_database_access() -> None:
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_RUNTIME_ROLE_INVALID"):
        grant_postgres_runtime_role(object(), "attacker_controlled")  # type: ignore[arg-type]


def test_operator_cli_failures_never_echo_secret_or_private_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    secret = "release-secret-canary"
    monkeypatch.setenv("NANO_AURAL_DATABASE_DSN", secret)

    def fail_connect(_dsn: str) -> object:
        raise RuntimeError(secret)

    monkeypatch.setattr(migration_admin, "_connect", fail_connect)
    assert migration_admin.main(("--verify",)) == 2
    captured = capsys.readouterr()
    assert captured.err == "MIGRATION_ADMIN_DATABASE_UNAVAILABLE\n"
    assert secret not in captured.out + captured.err

    class AdminConnection:
        def __init__(self, *, fail_close: bool = False) -> None:
            self.fail_close = fail_close

        def close(self) -> None:
            if self.fail_close:
                raise RuntimeError(secret)

    connection = AdminConnection()
    monkeypatch.setattr(migration_admin, "_connect", lambda _dsn: connection)

    def fail_operation(_connection: object) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(migration_admin, "verify_postgres_migrations", fail_operation)
    assert migration_admin.main(("--verify",)) == 2
    captured = capsys.readouterr()
    assert captured.err == "MIGRATION_ADMIN_OPERATION_FAILED\n"
    assert secret not in captured.out + captured.err

    closing_connection = AdminConnection(fail_close=True)
    monkeypatch.setattr(migration_admin, "_connect", lambda _dsn: closing_connection)
    monkeypatch.setattr(migration_admin, "verify_postgres_migrations", lambda _connection: None)
    assert migration_admin.main(("--verify",)) == 2
    captured = capsys.readouterr()
    assert captured.err == "MIGRATION_ADMIN_CLOSE_FAILED\n"
    assert secret not in captured.out + captured.err

    granted_roles: list[str] = []
    successful_connection = AdminConnection()
    monkeypatch.setattr(migration_admin, "_connect", lambda _dsn: successful_connection)
    monkeypatch.setattr(
        migration_admin,
        "grant_postgres_runtime_role",
        lambda _connection, role: granted_roles.append(role),
    )
    assert migration_admin.main(("--grant-runtime-role", "nano_aural_runtime")) == 0
    captured = capsys.readouterr()
    assert captured.out == "MIGRATION_RUNTIME_ROLE_GRANTED\n"
    assert captured.err == ""
    assert granted_roles == ["nano_aural_runtime"]
    assert secret not in captured.out + captured.err

    tmp_path.chmod(0o700)
    service_file = tmp_path / "private-service.conf"
    service_file.write_text("[release]\npassword=" + secret + "\n", encoding="utf-8")
    service_file.chmod(0o600)
    private_path = "/absolute/" + secret
    assert (
        recovery_main(
            (
                "backup",
                "--postgres-bin-root",
                private_path,
                "--pg-service-file",
                str(service_file),
                "--service",
                "release",
                "--recovery-set",
                str(tmp_path / "set"),
                "--canonical-root",
                str(tmp_path / "canonical"),
            )
        )
        == 3
    )
    captured = capsys.readouterr()
    assert secret not in captured.out + captured.err
    assert private_path not in captured.out + captured.err


POSTGRES_BIN = os.environ.get("NANO_AURAL_POSTGRES_BIN")
_REQUIRED_CLUSTER_BINARIES = ("initdb", "postgres", "pg_ctl")
_POSTGRES_READY = bool(
    POSTGRES_BIN
    and all((Path(POSTGRES_BIN) / name).is_file() for name in _REQUIRED_CLUSTER_BINARIES)
    and find_spec("psycopg") is not None
)


@pytest.fixture(scope="module")
def postgres_cluster(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[tuple[object, object, Path, Path, int]]:
    del tmp_path_factory
    if not _POSTGRES_READY or POSTGRES_BIN is None:
        pytest.skip("isolated PostgreSQL 16 cluster prerequisites are unavailable")
    import psycopg

    cluster = tempfile.TemporaryDirectory(prefix="nar-p6-", dir="/private/tmp")
    root = Path(cluster.name)
    data_dir = root / "data"
    socket_dir = root / "socket"
    socket_dir.mkdir()
    postgres_bin = Path(str(POSTGRES_BIN))
    port = 55000 + (os.getpid() % 900)

    def run(binary: str, *arguments: str, check: bool = True) -> None:
        subprocess.run(
            [str(postgres_bin / binary), *arguments],
            check=check,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
        )

    run(
        "initdb",
        "-D",
        str(data_dir),
        "-A",
        "trust",
        "-U",
        "postgres",
        "--no-locale",
        "--encoding=UTF8",
    )
    run("pg_ctl", "-D", str(data_dir), "-o", f"-k {socket_dir} -p {port} -h ''", "start")

    def dsn(database: str) -> str:
        return f"dbname={database} user=postgres host={socket_dir} port={port}"

    for _ in range(40):
        try:
            maintenance = psycopg.connect(dsn("postgres"), autocommit=True, connect_timeout=1)
            break
        except psycopg.OperationalError:
            time.sleep(0.25)
    else:
        run("pg_ctl", "-D", str(data_dir), "-m", "immediate", "-w", "stop", check=False)
        raise RuntimeError("temporary PostgreSQL cluster did not become ready")
    try:
        yield maintenance, dsn, postgres_bin, socket_dir, port
    finally:
        maintenance.close()
        run("pg_ctl", "-D", str(data_dir), "-m", "immediate", "-w", "stop", check=False)
        cluster.cleanup()


@pytest.fixture
def postgres_database(
    postgres_cluster: tuple[object, object, Path, Path, int],
) -> Iterator[object]:
    import psycopg

    maintenance, dsn, _postgres_bin, _socket_dir, _port = postgres_cluster
    database = "p6_" + uuid4().hex
    maintenance.execute("CREATE DATABASE " + database)  # type: ignore[attr-defined]
    connection = psycopg.connect(dsn(database))  # type: ignore[operator]
    try:
        yield connection
    finally:
        connection.close()
        maintenance.execute("DROP DATABASE " + database)  # type: ignore[attr-defined]


def _migration_bytes() -> tuple[tuple[str, bytes], ...]:
    directory = resources.files("nano_aural_runtime.durable").joinpath("sql")
    return tuple(
        (entry.name, entry.read_bytes())
        for entry in sorted(directory.iterdir(), key=lambda item: item.name)
        if entry.name.endswith(".sql")
    )


def test_real_postgres_fresh_reapply_mirror_and_append_only_ledger(
    postgres_database: object,
) -> None:
    connection = postgres_database
    connection.execute("CREATE SCHEMA migration_shadow")  # type: ignore[attr-defined]
    connection.execute(  # type: ignore[attr-defined]
        "CREATE TABLE migration_shadow.nano_aural_schema_migrations(filename TEXT PRIMARY KEY)"
    )
    connection.execute("SET search_path TO migration_shadow,public")  # type: ignore[attr-defined]
    connection.commit()  # type: ignore[attr-defined]
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    assert (
        connection.execute(  # type: ignore[attr-defined]
            "SELECT count(*) FROM public.nano_aural_schema_migrations"
        ).fetchone()[0]
        == 5
    )
    assert (
        connection.execute(  # type: ignore[attr-defined]
            "SELECT count(*) FROM migration_shadow.nano_aural_schema_migrations"
        ).fetchone()[0]
        == 0
    )
    connection.execute("SET search_path TO public")  # type: ignore[attr-defined]
    connection.commit()  # type: ignore[attr-defined]
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    verify_postgres_migrations(connection)  # type: ignore[arg-type]
    assert connection.execute(_LEDGER_AUTHORITY_SQL).fetchone()[0] == 1  # type: ignore[attr-defined]
    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE public.nano_aural_schema_migrations "
        "ENABLE ALWAYS TRIGGER nano_aural_schema_migrations_guard"
    )
    connection.commit()  # type: ignore[attr-defined]
    assert connection.execute(_LEDGER_AUTHORITY_SQL).fetchone()[0] == 1  # type: ignore[attr-defined]
    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE public.nano_aural_schema_migrations "
        "ENABLE TRIGGER nano_aural_schema_migrations_guard"
    )
    connection.commit()  # type: ignore[attr-defined]
    rows = connection.execute(  # type: ignore[attr-defined]
        "SELECT filename,sha256 FROM nano_aural_schema_migrations ORDER BY filename"
    ).fetchall()
    expected = _migration_bytes()
    assert rows == [(name, hashlib.sha256(data).hexdigest()) for name, data in expected]
    for name, data in expected:
        root = Path(__file__).resolve().parents[1] / "migrations" / name
        assert root.read_bytes() == data

    with pytest.raises(Exception, match="require the migration runner"):
        connection.execute(  # type: ignore[attr-defined]
            "UPDATE nano_aural_schema_migrations SET sha256=%s WHERE filename=%s",
            ("f" * 64, expected[0][0]),
        )
        connection.commit()  # type: ignore[attr-defined]
    connection.rollback()  # type: ignore[attr-defined]
    with pytest.raises(Exception, match="require the migration runner"):
        connection.execute(  # type: ignore[attr-defined]
            "DELETE FROM nano_aural_schema_migrations WHERE filename=%s", (expected[-1][0],)
        )
        connection.commit()  # type: ignore[attr-defined]
    connection.rollback()  # type: ignore[attr-defined]


def test_real_postgres_upgrades_every_historical_prefix_to_latest(
    postgres_cluster: tuple[object, object, Path, Path, int], tmp_path: Path
) -> None:
    import psycopg

    maintenance, dsn, _postgres_bin, _socket_dir, _port = postgres_cluster
    migrations = _migration_bytes()
    for count in range(1, len(migrations) + 1):
        database = "p6_prefix_" + uuid4().hex
        maintenance.execute("CREATE DATABASE " + database)  # type: ignore[attr-defined]
        connection = psycopg.connect(dsn(database))  # type: ignore[operator]
        try:
            prefix = _private(tmp_path / f"prefix-{count}")
            for name, data in migrations[:count]:
                (prefix / name).write_bytes(data)
            apply_postgres_migrations(connection, prefix)
            apply_postgres_migrations(connection)
            verify_postgres_migrations(connection)
            assert connection.execute(  # type: ignore[attr-defined]
                "SELECT count(*) FROM nano_aural_schema_migrations"
            ).fetchone()[0] == len(migrations)
        finally:
            connection.close()
            maintenance.execute("DROP DATABASE " + database)  # type: ignore[attr-defined]


def test_real_postgres_raw_correct_pending_insert_is_rejected_then_runner_applies(
    postgres_database: object, tmp_path: Path
) -> None:
    connection = postgres_database
    migrations = _migration_bytes()
    prefix = _private(tmp_path / "raw-pending-prefix")
    for name, data in migrations[:-1]:
        (prefix / name).write_bytes(data)
    apply_postgres_migrations(connection, prefix)  # type: ignore[arg-type]
    pending_name, pending_data = migrations[-1]
    runtime_role = "p6_runtime_" + uuid4().hex
    connection.execute("CREATE ROLE " + runtime_role)  # type: ignore[attr-defined]
    connection.execute(  # type: ignore[attr-defined]
        "GRANT USAGE ON SCHEMA public TO " + runtime_role
    )
    connection.execute(  # type: ignore[attr-defined]
        "GRANT SELECT ON public.nano_aural_schema_migrations TO " + runtime_role
    )
    connection.commit()  # type: ignore[attr-defined]
    connection.execute("SET ROLE " + runtime_role)  # type: ignore[attr-defined]
    forged_token = "runtime-knows-the-guard-is-not-a-secret"
    connection.execute(  # type: ignore[attr-defined]
        """CREATE TEMPORARY TABLE nano_aural_migration_authorization (
             token TEXT PRIMARY KEY
           ) ON COMMIT DROP"""
    )
    connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO pg_temp.nano_aural_migration_authorization(token) VALUES (%s)",
        (forged_token,),
    )
    connection.execute(  # type: ignore[attr-defined]
        "SELECT pg_catalog.set_config('nano_aural.migration_runner_token',%s,true)",
        (forged_token,),
    )
    with pytest.raises(Exception, match="permission denied"):
        connection.execute(  # type: ignore[attr-defined]
            """INSERT INTO public.nano_aural_schema_migrations(filename,sha256)
               VALUES (%s,%s)""",
            (pending_name, hashlib.sha256(pending_data).hexdigest()),
        )
    connection.rollback()  # type: ignore[attr-defined]
    connection.execute("RESET ROLE")  # type: ignore[attr-defined]
    with pytest.raises(Exception, match="require the migration runner"):
        connection.execute(  # type: ignore[attr-defined]
            """INSERT INTO public.nano_aural_schema_migrations(filename,sha256)
               VALUES (%s,%s)""",
            (pending_name, hashlib.sha256(pending_data).hexdigest()),
        )
    connection.rollback()  # type: ignore[attr-defined]
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    verify_postgres_migrations(connection)  # type: ignore[arg-type]
    connection.execute("DROP OWNED BY " + runtime_role)  # type: ignore[attr-defined]
    connection.execute("DROP ROLE " + runtime_role)  # type: ignore[attr-defined]
    connection.commit()  # type: ignore[attr-defined]


def test_real_postgres_runtime_role_grant_is_idempotent_and_least_privilege(
    postgres_database: object,
) -> None:
    connection = postgres_database
    connection.execute(  # type: ignore[attr-defined]
        """CREATE ROLE nano_aural_runtime WITH
           LOGIN NOINHERIT NOSUPERUSER NOCREATEDB NOCREATEROLE
           NOREPLICATION NOBYPASSRLS"""
    )
    connection.commit()  # type: ignore[attr-defined]
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    verify_postgres_migrations(connection)  # type: ignore[arg-type]
    grant_postgres_runtime_role(connection, "nano_aural_runtime")  # type: ignore[arg-type]
    grant_postgres_runtime_role(connection, "nano_aural_runtime")  # type: ignore[arg-type]
    verify_postgres_migrations(connection)  # type: ignore[arg-type]

    connection.execute(  # type: ignore[attr-defined]
        """CREATE TABLE public.runtime_future_table (
             id BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
             value TEXT NOT NULL
           )"""
    )
    connection.commit()  # type: ignore[attr-defined]
    deployment_id = str(uuid4())
    connection.execute("SET ROLE nano_aural_runtime")  # type: ignore[attr-defined]
    assert connection.execute(  # type: ignore[attr-defined]
        "SELECT count(*) FROM public.nano_aural_schema_migrations"
    ).fetchone()[0] == len(_migration_bytes())
    connection.execute(  # type: ignore[attr-defined]
        """INSERT INTO public.model_deployments(id,name,adapter_id,fingerprint)
           VALUES (%s,%s,%s,%s)""",
        (deployment_id, "runtime-role-test", "fake", "9" * 64),
    )
    connection.execute(  # type: ignore[attr-defined]
        "UPDATE public.model_deployments SET state='ready' WHERE id=%s",
        (deployment_id,),
    )
    assert (
        connection.execute(  # type: ignore[attr-defined]
            "INSERT INTO public.runtime_future_table(value) VALUES ('future') RETURNING id"
        ).fetchone()[0]
        == 1
    )
    connection.execute(  # type: ignore[attr-defined]
        "DELETE FROM public.runtime_future_table WHERE id=1"
    )
    connection.execute(  # type: ignore[attr-defined]
        "DELETE FROM public.model_deployments WHERE id=%s", (deployment_id,)
    )
    connection.commit()  # type: ignore[attr-defined]

    with pytest.raises(Exception, match="permission denied for schema public"):
        connection.execute("CREATE TABLE public.runtime_forbidden(id INTEGER)")  # type: ignore[attr-defined]
    connection.rollback()  # type: ignore[attr-defined]

    forged_token = "runtime-role-knows-the-non-secret-guard"
    connection.execute(  # type: ignore[attr-defined]
        """CREATE TEMPORARY TABLE nano_aural_migration_authorization (
             token TEXT PRIMARY KEY
           ) ON COMMIT DROP"""
    )
    connection.execute(  # type: ignore[attr-defined]
        "INSERT INTO pg_temp.nano_aural_migration_authorization(token) VALUES (%s)",
        (forged_token,),
    )
    connection.execute(  # type: ignore[attr-defined]
        "SELECT pg_catalog.set_config('nano_aural.migration_runner_token',%s,true)",
        (forged_token,),
    )
    with pytest.raises(Exception, match="permission denied"):
        connection.execute(  # type: ignore[attr-defined]
            """INSERT INTO public.nano_aural_schema_migrations(filename,sha256)
               VALUES ('9999_forbidden.sql',%s)""",
            ("f" * 64,),
        )
    connection.rollback()  # type: ignore[attr-defined]
    with pytest.raises(Exception, match="permission denied"):
        connection.execute(  # type: ignore[attr-defined]
            "UPDATE public.nano_aural_schema_migrations SET sha256=sha256"
        )
    connection.rollback()  # type: ignore[attr-defined]
    with pytest.raises(Exception, match="permission denied"):
        connection.execute("DELETE FROM public.nano_aural_schema_migrations")  # type: ignore[attr-defined]
    connection.rollback()  # type: ignore[attr-defined]
    connection.execute("RESET ROLE")  # type: ignore[attr-defined]
    connection.commit()  # type: ignore[attr-defined]
    verify_postgres_migrations(connection)  # type: ignore[arg-type]


def test_real_postgres_legacy_adoption_is_explicit_and_trusted(postgres_database: object) -> None:
    connection = postgres_database
    migrations = _migration_bytes()
    with connection.transaction():  # type: ignore[attr-defined]
        connection.execute(  # type: ignore[attr-defined]
            """CREATE TABLE nano_aural_schema_migrations (
                filename TEXT PRIMARY KEY,
                applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
            )"""
        )
        for name, data in migrations[:2]:
            connection.execute(data.decode("utf-8"), prepare=False)  # type: ignore[attr-defined]
            connection.execute(  # type: ignore[attr-defined]
                "INSERT INTO nano_aural_schema_migrations(filename) VALUES (%s)", (name,)
            )
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_LEGACY_CHECKSUMS_REQUIRED"):
        apply_postgres_migrations(connection)  # type: ignore[arg-type]
    trusted = {name: hashlib.sha256(data).hexdigest() for name, data in migrations[:2]}
    wrong = dict(trusted)
    wrong[migrations[0][0]] = "f" * 64
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_ADOPTION_TRUST_MISMATCH"):
        adopt_legacy_postgres_migration_checksums(connection, wrong)  # type: ignore[arg-type]
    adopt_legacy_postgres_migration_checksums(connection, trusted)  # type: ignore[arg-type]
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    verify_postgres_migrations(connection)  # type: ignore[arg-type]


def test_real_postgres_rejects_tampered_sql_ledger_and_unknown_row(
    postgres_database: object, tmp_path: Path
) -> None:
    connection = postgres_database
    migrations = _migration_bytes()
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    tampered = _private(tmp_path / "tampered-migrations")
    for name, data in migrations:
        (tampered / name).write_bytes(
            data + (b"\n-- tampered\n" if name == migrations[1][0] else b"")
        )
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_CHECKSUM_MISMATCH"):
        apply_postgres_migrations(connection, tampered)  # type: ignore[arg-type]

    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE nano_aural_schema_migrations DISABLE TRIGGER nano_aural_schema_migrations_guard"
    )
    connection.execute(  # type: ignore[attr-defined]
        "UPDATE nano_aural_schema_migrations SET sha256=%s WHERE filename=%s",
        ("e" * 64, migrations[0][0]),
    )
    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE nano_aural_schema_migrations ENABLE TRIGGER nano_aural_schema_migrations_guard"
    )
    connection.commit()  # type: ignore[attr-defined]
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_CHECKSUM_MISMATCH"):
        apply_postgres_migrations(connection)  # type: ignore[arg-type]
    connection.rollback()  # type: ignore[attr-defined]
    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE nano_aural_schema_migrations DISABLE TRIGGER nano_aural_schema_migrations_guard"
    )
    connection.execute(  # type: ignore[attr-defined]
        "UPDATE nano_aural_schema_migrations SET sha256=%s WHERE filename=%s",
        (hashlib.sha256(migrations[0][1]).hexdigest(), migrations[0][0]),
    )
    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE nano_aural_schema_migrations ENABLE TRIGGER nano_aural_schema_migrations_guard"
    )
    with pytest.raises(Exception, match="require the migration runner"):
        connection.execute(  # type: ignore[attr-defined]
            "INSERT INTO nano_aural_schema_migrations(filename,sha256) VALUES (%s,%s)",
            ("9999_unknown.sql", "d" * 64),
        )
    connection.rollback()  # type: ignore[attr-defined]


@pytest.mark.parametrize(
    "mutation",
    ("fake_check", "noop_function", "disabled", "replica", "dml_grant", "column_dml_grant"),
)
def test_real_postgres_rejects_inexact_or_inactive_ledger_seal(
    postgres_database: object, mutation: str
) -> None:
    connection = postgres_database
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    if mutation == "fake_check":
        connection.execute(  # type: ignore[attr-defined]
            """ALTER TABLE public.nano_aural_schema_migrations
               DROP CONSTRAINT nano_aural_schema_migrations_sha256_format"""
        )
        connection.execute(  # type: ignore[attr-defined]
            """ALTER TABLE public.nano_aural_schema_migrations
               ADD CONSTRAINT nano_aural_schema_migrations_sha256_format CHECK (true)"""
        )
    elif mutation == "noop_function":
        connection.execute(  # type: ignore[attr-defined]
            """CREATE OR REPLACE FUNCTION public.nano_aural_guard_schema_migration_ledger()
               RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$""",
            prepare=False,
        )
    elif mutation == "disabled":
        connection.execute(  # type: ignore[attr-defined]
            """ALTER TABLE public.nano_aural_schema_migrations
               DISABLE TRIGGER nano_aural_schema_migrations_guard"""
        )
    elif mutation == "replica":
        connection.execute(  # type: ignore[attr-defined]
            """ALTER TABLE public.nano_aural_schema_migrations
               ENABLE REPLICA TRIGGER nano_aural_schema_migrations_guard"""
        )
    elif mutation == "dml_grant":
        runtime_role = "p6_bad_acl_" + uuid4().hex
        connection.execute("CREATE ROLE " + runtime_role)  # type: ignore[attr-defined]
        connection.execute(  # type: ignore[attr-defined]
            "GRANT INSERT ON public.nano_aural_schema_migrations TO " + runtime_role
        )
    else:
        runtime_role = "p6_bad_column_acl_" + uuid4().hex
        connection.execute("CREATE ROLE " + runtime_role)  # type: ignore[attr-defined]
        connection.execute(  # type: ignore[attr-defined]
            "GRANT INSERT(filename,sha256) "
            "ON public.nano_aural_schema_migrations TO " + runtime_role
        )
    connection.commit()  # type: ignore[attr-defined]
    assert connection.execute(_LEDGER_AUTHORITY_SQL).fetchone()[0] == 0  # type: ignore[attr-defined]
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_LEDGER_SEAL_INVALID"):
        verify_postgres_migrations(connection)  # type: ignore[arg-type]
    with pytest.raises(MigrationIntegrityError, match="MIGRATION_LEDGER_SEAL_INVALID"):
        apply_postgres_migrations(connection)  # type: ignore[arg-type]


@pytest.mark.parametrize("mutation", ("wrong_digest", "old_prefix", "unknown_row"))
def test_real_postgres_release_authority_requires_exact_packaged_migration_set(
    postgres_database: object, mutation: str
) -> None:
    connection = postgres_database
    migrations = _migration_bytes()
    apply_postgres_migrations(connection)  # type: ignore[arg-type]
    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE public.nano_aural_schema_migrations "
        "DISABLE TRIGGER nano_aural_schema_migrations_guard"
    )
    if mutation == "wrong_digest":
        connection.execute(  # type: ignore[attr-defined]
            "UPDATE public.nano_aural_schema_migrations SET sha256=%s WHERE filename=%s",
            ("f" * 64, migrations[0][0]),
        )
    elif mutation == "old_prefix":
        connection.execute(  # type: ignore[attr-defined]
            "DELETE FROM public.nano_aural_schema_migrations WHERE filename=%s",
            (migrations[-1][0],),
        )
    else:
        connection.execute(  # type: ignore[attr-defined]
            """INSERT INTO public.nano_aural_schema_migrations(filename,sha256)
               VALUES (%s,%s)""",
            ("9999_unknown.sql", "e" * 64),
        )
    connection.execute(  # type: ignore[attr-defined]
        "ALTER TABLE public.nano_aural_schema_migrations "
        "ENABLE TRIGGER nano_aural_schema_migrations_guard"
    )
    connection.commit()  # type: ignore[attr-defined]
    assert connection.execute(_LEDGER_AUTHORITY_SQL).fetchone()[0] == 0  # type: ignore[attr-defined]
    with pytest.raises(MigrationIntegrityError):
        verify_postgres_migrations(connection)  # type: ignore[arg-type]


def test_real_postgres_dump_restore_preserves_winner_catalog_and_blob_integrity(
    postgres_cluster: tuple[object, object, Path, Path, int], tmp_path: Path
) -> None:
    import psycopg

    maintenance, dsn, postgres_bin, socket_dir, port = postgres_cluster
    missing = [
        name for name in ("pg_dump", "pg_restore", "psql") if not (postgres_bin / name).is_file()
    ]
    if missing:
        pytest.skip("UNRUN_ENV: PostgreSQL dump/restore client tools are unavailable")
    source_database = "p6_backup_" + uuid4().hex
    target_database = "p6_restore_" + uuid4().hex
    maintenance.execute("CREATE DATABASE " + source_database)  # type: ignore[attr-defined]
    maintenance.execute("CREATE DATABASE " + target_database)  # type: ignore[attr-defined]
    source_connection = psycopg.connect(dsn(source_database), autocommit=True)  # type: ignore[operator]
    target_connection = None
    tmp_path.chmod(0o700)
    canonical_source = _private(tmp_path / "canonical-real-source")
    canonical_target = tmp_path / "canonical-real-target"
    recovery_set = tmp_path / "real-recovery-set"
    source_store = LocalBlobStore(canonical_source)
    try:
        apply_postgres_migrations(source_connection)
        repository = PostgresDurableRepository(source_connection)
        deployment = repository.register_deployment(
            DeploymentRecord(str(uuid4()), "release-drill", "fake", "f" * 64)
        )
        worker = repository.register_worker(WorkerRecord(str(uuid4()), deployment.deployment_id))
        input_bytes = b"verified-release-input"
        input_object = source_store.put_immutable(input_bytes)
        input_blob = repository.register_blob(
            BlobRecord(
                str(uuid4()),
                input_object.sha256,
                input_object.size_bytes,
                input_object.storage_key,
                BlobState.VERIFIED,
                "application/octet-stream",
            )
        )
        asset = repository.create_asset(
            AssetRecord(
                str(uuid4()),
                input_blob.blob_id,
                "release-namespace",
                AssetKind.INPUT,
                AssetState.VERIFIED,
            )
        )
        job = repository.create_job(
            "release-namespace",
            "release-recovery-drill",
            {"task": "fake-release-recovery"},
            deployment.deployment_id,
            (JobInput("input", asset.asset_id),),
            (ArtifactKind.OUTPUT,),
        )
        queue = PostgresLeaseQueue(source_connection)
        lease = queue.claim_next(worker.worker_id, 120)
        assert lease is not None
        output_bytes = b"verified-visible-winning-artifact"
        output_object = source_store.put_immutable(output_bytes)
        publications = PostgresPublicationRepository(source_connection)
        reserved = publications.reserve(
            lease,
            PublicationSpec(
                ArtifactKind.OUTPUT,
                "application/octet-stream",
                len(output_bytes),
                output_object.sha256,
                output_object.size_bytes,
            ),
        )
        written = publications.record_object(
            lease,
            reserved.publication_id,
            reserved.version,
            output_object.sha256,
            output_object.size_bytes,
        )
        validated = publications.record_validated(
            lease,
            written.publication_id,
            written.version,
            BlobRecord(
                str(uuid4()),
                output_object.sha256,
                output_object.size_bytes,
                output_object.storage_key,
                BlobState.VERIFIED,
                "application/octet-stream",
            ),
            {"release_drill": True},
        )
        winner = publications.finalize(lease, (validated,))
        assert len(winner) == 1
        source_store.close()
        service_file = tmp_path / "pg_service.conf"
        service_file.write_text(
            "[source]\n"
            f"host={socket_dir}\nport={port}\ndbname={source_database}\nuser=postgres\n"
            "[target]\n"
            f"host={socket_dir}\nport={port}\ndbname={target_database}\nuser=postgres\n",
            encoding="utf-8",
        )
        service_file.chmod(0o600)
        source_tools = SubprocessPostgresRecoveryTools(postgres_bin, service_file, "source")
        target_tools = SubprocessPostgresRecoveryTools(postgres_bin, service_file, "target")
        backup = create_recovery_set(source_tools, canonical_source, recovery_set)
        restored = restore_recovery_set(target_tools, recovery_set, canonical_target)
        assert restored == backup

        target_connection = psycopg.connect(dsn(target_database), autocommit=True)  # type: ignore[operator]
        verify_postgres_migrations(target_connection)
        events = target_connection.execute(
            "SELECT count(*) FROM job_events WHERE job_id=%s", (job.job_id,)
        ).fetchone()[0]
        assert events >= 3
        restored_winner = PostgresPublicationRepository(target_connection).visible_winner(
            job.job_id
        )
        assert len(restored_winner) == 1
        assert (restored_winner[0].sha256, restored_winner[0].size_bytes) == (
            output_object.sha256,
            output_object.size_bytes,
        )
        target_store = LocalBlobStore(canonical_target)
        try:
            with target_store.open_reader(restored_winner[0].storage_key) as reader:
                downloaded = reader.read()
            assert len(downloaded) == restored_winner[0].size_bytes
            assert hashlib.sha256(downloaded).hexdigest() == restored_winner[0].sha256
            with target_store.open_reader(input_object.storage_key) as reader:
                restored_input = reader.read()
            assert restored_input == input_bytes
        finally:
            target_store.close()
    finally:
        source_store.close()
        source_connection.close()
        if target_connection is not None:
            target_connection.close()
        maintenance.execute("DROP DATABASE " + source_database)  # type: ignore[attr-defined]
        maintenance.execute("DROP DATABASE " + target_database)  # type: ignore[attr-defined]
