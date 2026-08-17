# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import io
import os
import wave
from dataclasses import FrozenInstanceError, dataclass
from pathlib import Path
from uuid import uuid4

import pytest

from nano_aural_runtime.durable.domain import (
    AssetRecord,
    AttemptRecord,
    BlobRecord,
    BlobState,
    JobInput,
    JobRecord,
    JobState,
)
from nano_aural_runtime.durable.errors import StateTransitionError
from nano_aural_runtime.durable.materialization import (
    AttemptInputMaterializer,
    AttemptLease,
    EvidenceDriftError,
    MaterializationIntegrityError,
    MaterializationLeaseLostError,
    MaterializationMediaError,
    VerifiedInputEvidence,
)
from nano_aural_runtime.durable.storage import BlobObject, LocalBlobStore
from nano_aural_runtime.durable.uploads import WaveMediaProbe


@dataclass(frozen=True)
class Lease:
    job_id: str
    attempt_id: str
    worker_id: str
    lease_epoch: int


class LeaseAuthority:
    def __init__(self, fail_after: int | None = None) -> None:
        self.calls = 0
        self._fail_after = fail_after

    def assert_current(self, lease: AttemptLease) -> None:
        self.calls += 1
        if self._fail_after is not None and self.calls > self._fail_after:
            raise StateTransitionError("lease is stale")


class CorruptCanonicalStore:
    def __init__(self, expected: BlobObject, content: bytes) -> None:
        self._expected = expected
        self._content = content

    def put_stream(self, chunks: object) -> BlobObject:
        raise AssertionError("materialization must not promote input bytes")

    def open_reader(self, storage_key: str) -> io.BytesIO:
        assert storage_key == self._expected.storage_key
        return io.BytesIO(self._content)

    def stat(self, storage_key: str) -> BlobObject:
        assert storage_key == self._expected.storage_key
        return self._expected


def _wav_bytes() -> bytes:
    stream = io.BytesIO()
    with wave.open(stream, "wb") as source:
        source.setnchannels(1)
        source.setsampwidth(2)
        source.setframerate(8000)
        source.writeframes(b"\x00\x00" * 80)
    return stream.getvalue()


def _evidence(
    tmp_path: Path,
) -> tuple[JobRecord, AttemptRecord, Lease, VerifiedInputEvidence, bytes]:
    content = _wav_bytes()
    store = LocalBlobStore(tmp_path / "blob-root")
    stored = store.put_stream((content[:17], content[17:]))
    probe = WaveMediaProbe().probe(io.BytesIO(content))
    job_id, attempt_id, worker_id, asset_id, blob_id = (str(uuid4()) for _ in range(5))
    attempt = AttemptRecord(attempt_id, job_id, worker_id, 1, 1)
    job = JobRecord(
        job_id,
        "tenant-a",
        "materialize-key",
        "a" * 64,
        {"task": "test"},
        str(uuid4()),
        (JobInput("reference", asset_id),),
        state=JobState.RUNNING,
        current_attempt_id=attempt_id,
        lease_epoch=1,
    )
    blob = BlobRecord(
        blob_id,
        stored.sha256,
        stored.size_bytes,
        stored.storage_key,
        BlobState.VERIFIED,
        "audio/wav",
    )
    asset = AssetRecord(
        asset_id,
        blob_id,
        "tenant-a",
        metadata={"media_type": probe.media_type, "duration_seconds": probe.duration_seconds},
    )
    return (
        job,
        attempt,
        Lease(job_id, attempt_id, worker_id, 1),
        VerifiedInputEvidence("reference", asset, blob),
        content,
    )


def test_materializes_verified_canonical_media_and_cleans_only_its_workspace(
    tmp_path: Path,
) -> None:
    job, attempt, lease, evidence, content = _evidence(tmp_path)
    canonical = LocalBlobStore(tmp_path / "blob-root")
    root = tmp_path / "attempt-workspaces"
    sentinel = tmp_path / "must-survive"
    sentinel.write_text("safe")
    authority = LeaseAuthority()
    with AttemptInputMaterializer(
        job,
        attempt,
        lease,
        (evidence,),
        canonical,
        authority,
        root,
        WaveMediaProbe(),
        chunk_size=7,
    ) as materialization:
        workspace = materialization.workspace
        item = materialization.inputs[0]
        assert item.path.parent == workspace
        assert item.path.name.startswith("input-0000-")
        assert item.path.read_bytes() == content
        assert item.media is not None and item.media.media_type == "audio/wav"
        with pytest.raises(FrozenInstanceError):
            item.path = tmp_path  # type: ignore[misc]
        os.symlink(sentinel, workspace / "untrusted-link")
    assert not workspace.exists()
    assert sentinel.read_text() == "safe"
    assert authority.calls == 2


def test_rejects_staging_or_noncurrent_database_evidence_before_reading(tmp_path: Path) -> None:
    job, attempt, lease, evidence, _ = _evidence(tmp_path)
    staging_blob = BlobRecord(
        evidence.blob.blob_id,
        evidence.blob.sha256,
        evidence.blob.size_bytes,
        "staging/" + str(uuid4()),
        BlobState.VERIFIED,
    )
    with pytest.raises(EvidenceDriftError):
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (VerifiedInputEvidence("reference", evidence.asset, staging_blob),),
            LocalBlobStore(tmp_path / "blob-root"),
            LeaseAuthority(),
            tmp_path / "workspaces",
            WaveMediaProbe(),
        ):
            pass


def test_rejects_job_epoch_and_media_type_evidence_drift(tmp_path: Path) -> None:
    job, attempt, lease, evidence, _ = _evidence(tmp_path)
    stale_job = JobRecord(
        job.job_id,
        job.namespace_id,
        job.idempotency_key,
        job.request_sha256,
        job.request,
        job.deployment_id,
        job.inputs,
        state=job.state,
        current_attempt_id=job.current_attempt_id,
        lease_epoch=job.lease_epoch + 1,
    )
    with pytest.raises(EvidenceDriftError):
        with AttemptInputMaterializer(
            stale_job,
            attempt,
            lease,
            (evidence,),
            LocalBlobStore(tmp_path / "blob-root"),
            LeaseAuthority(),
            tmp_path / "epoch-workspaces",
            WaveMediaProbe(),
        ):
            pass
    conflicting_asset = AssetRecord(
        evidence.asset.asset_id,
        evidence.asset.blob_id,
        evidence.asset.namespace_id,
        metadata={"media_type": "audio/mpeg", "duration_seconds": 1.0},
    )
    with pytest.raises(EvidenceDriftError):
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (VerifiedInputEvidence("reference", conflicting_asset, evidence.blob),),
            LocalBlobStore(tmp_path / "blob-root"),
            LeaseAuthority(),
            tmp_path / "type-workspaces",
            WaveMediaProbe(),
        ):
            pass


def test_requires_complete_media_metadata_and_a_probe(tmp_path: Path) -> None:
    job, attempt, lease, evidence, _ = _evidence(tmp_path)
    metadata_missing = AssetRecord(
        evidence.asset.asset_id,
        evidence.asset.blob_id,
        evidence.asset.namespace_id,
        metadata={},
    )
    with pytest.raises(EvidenceDriftError):
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (VerifiedInputEvidence("reference", metadata_missing, evidence.blob),),
            LocalBlobStore(tmp_path / "blob-root"),
            LeaseAuthority(),
            tmp_path / "metadata-workspaces",
            WaveMediaProbe(),
        ):
            pass
    with pytest.raises(ValueError, match="media probe"):
        AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (evidence,),
            LocalBlobStore(tmp_path / "blob-root"),
            LeaseAuthority(),
            tmp_path / "probe-workspaces",
            None,  # type: ignore[arg-type]
        )


def test_detects_canonical_byte_drift_and_removes_partial_workspace(tmp_path: Path) -> None:
    job, attempt, lease, evidence, content = _evidence(tmp_path)
    expected = BlobObject(evidence.blob.sha256, evidence.blob.size_bytes, evidence.blob.storage_key)
    with pytest.raises(MaterializationIntegrityError):
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (evidence,),
            CorruptCanonicalStore(expected, content + b"changed"),
            LeaseAuthority(),
            tmp_path / "workspaces",
            WaveMediaProbe(),
        ):
            pass
    assert not list((tmp_path / "workspaces").glob("attempt-*"))


def test_reprobes_media_and_rechecks_fence_after_streaming(tmp_path: Path) -> None:
    job, attempt, lease, evidence, _ = _evidence(tmp_path)
    asset_with_bad_duration = AssetRecord(
        evidence.asset.asset_id,
        evidence.asset.blob_id,
        evidence.asset.namespace_id,
        metadata={"media_type": "audio/wav", "duration_seconds": 999.0},
    )
    canonical = LocalBlobStore(tmp_path / "blob-root")
    with pytest.raises(MaterializationMediaError):
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (VerifiedInputEvidence("reference", asset_with_bad_duration, evidence.blob),),
            canonical,
            LeaseAuthority(),
            tmp_path / "media-workspaces",
            WaveMediaProbe(),
        ):
            pass
    with pytest.raises(MaterializationLeaseLostError):
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (evidence,),
            canonical,
            LeaseAuthority(fail_after=1),
            tmp_path / "lease-workspaces",
            WaveMediaProbe(),
        ):
            pass
    assert not list((tmp_path / "lease-workspaces").glob("attempt-*"))


def test_materializer_does_not_accept_sha_or_size_drift(tmp_path: Path) -> None:
    job, attempt, lease, evidence, content = _evidence(tmp_path)
    wrong_sha = hashlib.sha256(b"another object").hexdigest()
    drifted_blob = BlobRecord(
        evidence.blob.blob_id,
        wrong_sha,
        evidence.blob.size_bytes,
        LocalBlobStore.canonical_key(wrong_sha),
        BlobState.VERIFIED,
    )
    expected = BlobObject(wrong_sha, len(content), drifted_blob.storage_key)
    with pytest.raises(MaterializationIntegrityError):
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            (VerifiedInputEvidence("reference", evidence.asset, drifted_blob),),
            CorruptCanonicalStore(expected, content),
            LeaseAuthority(),
            tmp_path / "hash-workspaces",
            WaveMediaProbe(),
        ):
            pass
