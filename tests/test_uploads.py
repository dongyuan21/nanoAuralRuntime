# pyright: reportMissingImports=false
"""CPU contract tests for Phase 3B staging and content verification."""

from __future__ import annotations

import hashlib
import io
import sys
import wave
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from nano_aural_runtime.durable.errors import StateTransitionError
from nano_aural_runtime.durable.storage import LocalBlobStore
from nano_aural_runtime.durable.upload_transports import InMemoryS3CompatibleCanonical
from nano_aural_runtime.durable.uploads import (
    CommandMediaProbe,
    InMemoryS3CompatibleStaging,
    InMemoryUploadRepository,
    LocalStagingBlobStore,
    MediaInfo,
    UploadCliState,
    UploadMode,
    UploadSession,
    UploadState,
    UploadVerifier,
    WaveMediaProbe,
)


def _wav() -> bytes:
    result = io.BytesIO()
    with wave.open(result, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * 80)
    return result.getvalue()


def test_staging_stream_round_trip_and_traversal_rejection(tmp_path: Path) -> None:
    store = LocalStagingBlobStore(tmp_path)
    session = "00000000-0000-0000-0000-000000000001"
    key = store.write_stream(session, (part for part in (b"one", b"two")))
    with store.open_reader(key) as source:
        assert source.read() == b"onetwo"
    with pytest.raises(ValueError):
        store.open_reader("staging/../outside")
    with pytest.raises(StateTransitionError):
        store.write_stream(session, (b"replacement",))
    store.delete(key)
    assert not store.exists(key)


def test_media_probe_rejects_bad_media_and_never_uses_multipart_etag_as_identity() -> None:
    content = _wav()
    session = UploadSession(
        "00000000-0000-0000-0000-000000000002",
        "ns",
        UploadMode.MULTIPART,
        len(content),
        "staging/00000000-0000-0000-0000-000000000002",
        datetime.now(timezone.utc) + timedelta(minutes=1),
        hashlib.sha256(content).hexdigest(),
    )
    assert session.expected_sha256 != "multipart-etag-not-a-sha256"
    assert WaveMediaProbe().probe(io.BytesIO(content)).media_type == "audio/wav"
    with pytest.raises(ValueError):
        WaveMediaProbe().probe(io.BytesIO(b"not media"))
    with pytest.raises(ValueError):
        WaveMediaProbe().probe(io.BytesIO(content[:44]))
    s3 = InMemoryS3CompatibleStaging()
    key = s3.write_stream("00000000-0000-0000-0000-000000000009", (content,))
    assert s3.multipart_etag(key).endswith("-2")
    assert s3.multipart_etag(key) != hashlib.sha256(content).hexdigest()


def test_upload_contract_values_and_cli_state_are_strict_and_atomic(tmp_path: Path) -> None:
    identifier = "00000000-0000-0000-0000-000000000025"
    with pytest.raises(ValueError):
        UploadSession(identifier, "ns", UploadMode.SINGLE, 1, "staging/wrong", datetime.now())
    state = UploadCliState(identifier, "staging/" + identifier, 0)
    path = tmp_path / "state.json"
    state.save(path)
    assert UploadCliState.load(path) == state
    path.chmod(0o644)
    with pytest.raises(ValueError):
        UploadCliState.load(path)
    with pytest.raises(ValueError):
        CommandMediaProbe(("probe",), timeout_seconds=float("nan"))


@pytest.mark.parametrize(
    ("program", "limit", "timeout", "works"),
    (
        (
            "import json; print(json.dumps({'media_type':'audio/wav','duration_seconds':1}))",
            128,
            1,
            True,
        ),
        ("print('x' * 4096)", 32, 1, False),
        ("import sys; sys.stderr.write('x' * 4096)", 32, 1, False),
        ("import time; time.sleep(2)", 128, 0.05, False),
    ),
)
def test_command_probe_limits_real_child_output_and_timeout(
    tmp_path: Path, program: str, limit: int, timeout: float, works: bool
) -> None:
    probe = CommandMediaProbe((sys.executable, "-c", program), timeout, limit)
    if works:
        assert probe.probe_path(tmp_path / "server-temp") == MediaInfo("audio/wav", 1.0)
    else:
        with pytest.raises(ValueError):
            probe.probe_path(tmp_path / "server-temp")


def test_verifier_rejects_bad_inputs_and_deduplicates_wav(tmp_path: Path) -> None:
    staging = LocalStagingBlobStore(tmp_path / "staging")
    canonical = LocalBlobStore(tmp_path / "canonical")
    repository = InMemoryUploadRepository()
    verifier = UploadVerifier(repository, staging, canonical, WaveMediaProbe())
    now = datetime.now(timezone.utc)

    def session(identifier: str, payload: bytes, digest: str) -> UploadSession:
        value = UploadSession(
            identifier,
            "ns",
            UploadMode.MULTIPART,
            len(payload),
            staging.key_for(identifier),
            now + timedelta(minutes=1),
            digest,
        )
        repository.create_session(value)
        staging.write_stream(identifier, (payload,))
        return repository.mark_uploaded(identifier, value.version)

    bad_hash = session("00000000-0000-0000-0000-000000000003", _wav(), "0" * 64)
    assert verifier.finalize(bad_hash.session_id, bad_hash.version).state == UploadState.REJECTED
    assert not staging.exists(bad_hash.staging_key)
    bad_media = session(
        "00000000-0000-0000-0000-000000000004", b"bad", hashlib.sha256(b"bad").hexdigest()
    )
    assert verifier.finalize(bad_media.session_id, bad_media.version).state == UploadState.REJECTED
    payload = _wav()
    first = session(
        "00000000-0000-0000-0000-000000000005", payload, hashlib.sha256(payload).hexdigest()
    )
    second = session(
        "00000000-0000-0000-0000-000000000006", payload, hashlib.sha256(payload).hexdigest()
    )
    first_done = verifier.finalize(first.session_id, first.version)
    second_done = verifier.finalize(second.session_id, second.version)
    assert first_done.state == second_done.state == UploadState.VERIFIED
    assert first_done.verified_blob_id == second_done.verified_blob_id


def test_s3_compatible_transports_run_the_full_verifier_contract() -> None:
    staging = InMemoryS3CompatibleStaging()
    canonical = InMemoryS3CompatibleCanonical()
    repository = InMemoryUploadRepository()
    payload = _wav()
    digest = hashlib.sha256(payload).hexdigest()
    completed = []
    for suffix in ("031", "032"):
        identifier = "00000000-0000-0000-0000-000000000" + suffix
        session = UploadSession(
            identifier,
            "s3-ns",
            UploadMode.MULTIPART,
            len(payload),
            "staging/" + identifier,
            datetime.now(timezone.utc) + timedelta(minutes=1),
            digest,
        )
        repository.create_session(session)
        staging.write_stream(identifier, (payload[:17], payload[17:]))
        uploaded = repository.mark_uploaded(identifier, 0)
        completed.append(
            UploadVerifier(repository, staging, canonical, WaveMediaProbe()).finalize(
                identifier, uploaded.version
            )
        )
    assert all(item.state == UploadState.VERIFIED for item in completed)
    assert completed[0].verified_blob_id == completed[1].verified_blob_id


def test_local_staging_rejects_symlinked_storage_directory(tmp_path: Path) -> None:
    root = tmp_path / "root"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "staging").symlink_to(outside, target_is_directory=True)
    store = LocalStagingBlobStore(root)
    with pytest.raises(OSError):
        store.write_stream("00000000-0000-0000-0000-000000000033", (b"x",))


def test_inmemory_upload_expiry_matches_postgres_cas() -> None:
    repository = InMemoryUploadRepository()
    identifier = "00000000-0000-0000-0000-000000000034"
    session = UploadSession(
        identifier,
        "expiry-ns",
        UploadMode.SINGLE,
        1,
        "staging/" + identifier,
        datetime.now(timezone.utc) - timedelta(seconds=1),
    )
    repository.create_session(session)
    with pytest.raises(StateTransitionError):
        repository.mark_uploaded(identifier, 0)
    assert repository.expire_before(datetime.now(timezone.utc))[0].state == UploadState.EXPIRED


def test_promotion_orphan_is_reused_after_database_failure(tmp_path: Path) -> None:
    class FailOnceRepository(InMemoryUploadRepository):
        failed = False

        def finalize_verified(self, *args, **kwargs):  # type: ignore[no-untyped-def]
            if not self.failed:
                self.failed = True
                raise RuntimeError("simulated database rollback after promotion")
            return super().finalize_verified(*args, **kwargs)

    staging = LocalStagingBlobStore(tmp_path / "staging")
    canonical = LocalBlobStore(tmp_path / "canonical")
    repository = FailOnceRepository()
    payload = _wav()
    identifier = "00000000-0000-0000-0000-000000000010"
    session = UploadSession(
        identifier,
        "ns",
        UploadMode.SINGLE,
        len(payload),
        staging.key_for(identifier),
        datetime.now(timezone.utc) + timedelta(minutes=1),
        hashlib.sha256(payload).hexdigest(),
    )
    repository.create_session(session)
    staging.write_stream(identifier, (payload,))
    uploaded = repository.mark_uploaded(identifier, 0)
    verifier = UploadVerifier(repository, staging, canonical, WaveMediaProbe())
    with pytest.raises(RuntimeError, match="database rollback"):
        verifier.finalize(identifier, uploaded.version)
    verifying = repository.get_session(identifier)
    assert verifying.state == UploadState.VERIFYING
    assert verifier.finalize(identifier, verifying.version).state == UploadState.VERIFIED
    assert not staging.exists(staging.key_for(identifier))


def test_janitor_deletes_only_expired_staging(tmp_path: Path) -> None:
    staging = LocalStagingBlobStore(tmp_path / "staging")
    repository = InMemoryUploadRepository()
    now = datetime.now(timezone.utc)
    expired_id, live_id = (
        "00000000-0000-0000-0000-000000000007",
        "00000000-0000-0000-0000-000000000008",
    )
    for identifier, expiry in (
        (expired_id, now - timedelta(seconds=1)),
        (live_id, now + timedelta(minutes=1)),
    ):
        value = UploadSession(
            identifier, "ns", UploadMode.SINGLE, 1, staging.key_for(identifier), expiry
        )
        repository.create_session(value)
        staging.write_stream(identifier, (b"x",))
    verifier = UploadVerifier(
        repository, staging, LocalBlobStore(tmp_path / "canonical"), WaveMediaProbe()
    )
    assert verifier.janitor(now) == 1
    assert not staging.exists(staging.key_for(expired_id))
    assert staging.exists(staging.key_for(live_id))


def test_cli_state_atomic_save_and_load(tmp_path: Path) -> None:
    state = UploadCliState(
        "00000000-0000-0000-0000-000000000011", "staging/00000000-0000-0000-0000-000000000011", 3
    )
    path = tmp_path / "upload-state.json"
    state.save(path)
    assert UploadCliState.load(path) == state
