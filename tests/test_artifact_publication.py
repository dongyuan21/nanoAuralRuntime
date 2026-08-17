# pyright: reportMissingImports=false
"""CPU fault-injection tests for Phase 3E publication application logic."""

from __future__ import annotations

import hashlib
import io
import wave
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import BinaryIO, Mapping, Optional, Sequence
from uuid import uuid4

import pytest

from nano_aural_runtime.durable.artifact_publication import (
    ArtifactOrphanSweeper,
    ArtifactPublicationService,
    ArtifactRejectedError,
    PublicationCandidate,
    PublicationFaultInjector,
    PublicationFaultPoint,
    PublicationProgressHook,
    PublicationProgressPoint,
)
from nano_aural_runtime.durable.artifact_storage import (
    AttemptArtifactStore,
    InMemoryS3CompatibleAttemptStore,
    LocalAttemptArtifactStore,
    attempt_key,
)
from nano_aural_runtime.durable.artifact_validation import StreamingMediaArtifactValidator
from nano_aural_runtime.durable.domain import ArtifactKind, BlobRecord
from nano_aural_runtime.durable.errors import NotFoundError, StateTransitionError
from nano_aural_runtime.durable.publication import (
    PublicationRecord,
    PublicationSpec,
    PublicationState,
    VisibleArtifact,
)
from nano_aural_runtime.durable.queue import Lease
from nano_aural_runtime.durable.storage import LocalBlobStore
from nano_aural_runtime.durable.uploads import WaveMediaProbe


def _wav(frames: int = 400) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def _lease() -> Lease:
    return Lease(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        7,
        datetime.now(timezone.utc) + timedelta(minutes=1),
    )


def _candidate(content: bytes) -> PublicationCandidate:
    return PublicationCandidate(
        ArtifactKind.OUTPUT,
        "audio/wav",
        lambda: (content[:17], content[17:]),
        len(content) + 1,
        hashlib.sha256(content).hexdigest(),
        len(content),
        {"producer": "cpu-fake"},
    )


def _missing(store: InMemoryS3CompatibleAttemptStore, key: str) -> bool:
    try:
        store.stat(key)
    except KeyError:
        return True
    return False


class FakeFence:
    def __init__(self) -> None:
        self.current = True

    def assert_current(self, lease: Lease) -> None:
        assert isinstance(lease, Lease)
        if not self.current:
            raise StateTransitionError("lease is no longer current")


class FakePublicationRepository:
    """In-memory ledger double; SQL authority is tested by slice A."""

    def __init__(self, lease: Lease, fence: FakeFence) -> None:
        self.lease = lease
        self.fence = fence
        self.records: dict[str, PublicationRecord] = {}
        self.by_kind: dict[ArtifactKind, str] = {}
        self.blobs: dict[str, BlobRecord] = {}
        self.visible: tuple[VisibleArtifact, ...] = ()
        self.fail_record_validated_once = False
        self.orphan_keys: tuple[str, ...] = ()
        self.known_query_sizes: list[int] = []

    def get(self, publication_id: str) -> PublicationRecord:
        try:
            return self.records[publication_id]
        except KeyError as error:
            raise NotFoundError("publication not found") from error

    def reserve(self, lease: Lease, spec: PublicationSpec) -> PublicationRecord:
        self._fence(lease)
        existing_id = self.by_kind.get(spec.kind)
        if existing_id is not None:
            current = self.records[existing_id]
            if (
                current.content_type != spec.content_type
                or current.max_size_bytes != spec.max_size_bytes
                or current.expected_sha256 != spec.expected_sha256
                or current.expected_size_bytes != spec.expected_size_bytes
                or dict(current.metadata) != dict(spec.metadata)
            ):
                raise StateTransitionError("publication identity changed")
            return current
        publication_id = str(uuid4())
        record = PublicationRecord(
            publication_id,
            lease.job_id,
            lease.attempt_id,
            lease.worker_id,
            lease.lease_epoch,
            spec.kind,
            spec.content_type,
            spec.max_size_bytes,
            PublicationState.RESERVED,
            0,
            spec.expected_sha256,
            spec.expected_size_bytes,
            metadata=spec.metadata,
            created_at=datetime.now(timezone.utc),
            updated_at=datetime.now(timezone.utc),
        )
        self.records[publication_id] = record
        self.by_kind[spec.kind] = publication_id
        return record

    def record_object(
        self,
        lease: Lease,
        publication_id: str,
        expected_version: int,
        observed_sha256: str,
        observed_size_bytes: int,
    ) -> PublicationRecord:
        self._fence(lease)
        current = self.get(publication_id)
        self._cas(current, PublicationState.RESERVED, expected_version)
        updated = replace(
            current,
            state=PublicationState.OBJECT_WRITTEN,
            version=current.version + 1,
            attempt_object_key=attempt_key(
                current.job_id,
                current.attempt_id,
                current.lease_epoch,
                current.kind,
                current.publication_id,
            ),
            observed_sha256=observed_sha256,
            observed_size_bytes=observed_size_bytes,
            updated_at=datetime.now(timezone.utc),
        )
        self.records[publication_id] = updated
        return updated

    def record_validated(
        self,
        lease: Lease,
        publication_id: str,
        expected_version: int,
        blob: BlobRecord,
        validator_metadata: Mapping[str, object],
    ) -> PublicationRecord:
        self._fence(lease)
        current = self.get(publication_id)
        self._cas(current, PublicationState.OBJECT_WRITTEN, expected_version)
        if self.fail_record_validated_once:
            self.fail_record_validated_once = False
            raise RuntimeError("injected database failure after canonical promotion")
        self.blobs[blob.blob_id] = blob
        updated = replace(
            current,
            state=PublicationState.VALIDATED,
            version=current.version + 1,
            canonical_blob_id=blob.blob_id,
            observed_content_type=blob.content_type,
            validator_metadata=validator_metadata,
            updated_at=datetime.now(timezone.utc),
        )
        self.records[publication_id] = updated
        return updated

    def finalize(
        self, lease: Lease, publications: Sequence[PublicationRecord]
    ) -> tuple[VisibleArtifact, ...]:
        self._fence(lease)
        output = []
        for requested in publications:
            current = self.get(requested.publication_id)
            self._cas(current, PublicationState.VALIDATED, requested.version)
            blob = self.blobs[str(current.canonical_blob_id)]
            updated = replace(
                current,
                state=PublicationState.FINALIZED,
                version=current.version + 1,
                updated_at=datetime.now(timezone.utc),
            )
            self.records[current.publication_id] = updated
            output.append(
                VisibleArtifact(
                    str(uuid4()),
                    current.publication_id,
                    lease.job_id,
                    lease.attempt_id,
                    current.kind,
                    blob.blob_id,
                    blob.sha256,
                    blob.size_bytes,
                    blob.storage_key,
                    blob.content_type,
                    current.metadata,
                )
            )
        self.visible = tuple(output)
        return self.visible

    def reject(
        self, lease: Lease, publication_id: str, expected_version: int, reason: str
    ) -> PublicationRecord:
        self._fence(lease)
        current = self.get(publication_id)
        if current.version != expected_version:
            raise StateTransitionError("CAS lost")
        updated = replace(
            current,
            state=PublicationState.REJECTED,
            version=current.version + 1,
            terminal_reason=reason,
            updated_at=datetime.now(timezone.utc),
        )
        self.records[publication_id] = updated
        return updated

    def abandon(self, publication_id: str, expected_version: int, reason: str) -> PublicationRecord:
        current = self.get(publication_id)
        if current.version != expected_version or self.fence.current:
            raise StateTransitionError("publication is not stale")
        updated = replace(
            current,
            state=PublicationState.ABANDONED,
            version=current.version + 1,
            terminal_reason=reason,
            updated_at=datetime.now(timezone.utc),
        )
        self.records[publication_id] = updated
        return updated

    def cleanup_candidates(
        self, before: datetime, limit: int = 100
    ) -> tuple[PublicationRecord, ...]:
        assert isinstance(before, datetime)
        return tuple(
            record
            for record in self.records.values()
            if record.state
            in (
                PublicationState.FINALIZED,
                PublicationState.REJECTED,
                PublicationState.ABANDONED,
            )
            and record.attempt_object_deleted_at is None
        )[:limit]

    def record_cleanup(self, publication_id: str, expected_version: int) -> PublicationRecord:
        current = self.get(publication_id)
        if current.version != expected_version or current.attempt_object_deleted_at is not None:
            raise StateTransitionError("cleanup CAS lost")
        updated = replace(
            current,
            version=current.version + 1,
            attempt_object_deleted_at=datetime.now(timezone.utc),
        )
        self.records[publication_id] = updated
        return updated

    def orphan_keys_before(self, before: datetime, limit: int = 100) -> tuple[str, ...]:
        assert isinstance(before, datetime)
        return self.orphan_keys[:limit]

    def known_attempt_keys(self, keys: Sequence[str]) -> tuple[str, ...]:
        self.known_query_sizes.append(len(keys))
        requested = set(keys)
        return tuple(
            sorted(
                record.attempt_object_key
                for record in self.records.values()
                if record.attempt_object_key in requested and record.attempt_object_key is not None
            )
        )

    def _fence(self, lease: Lease) -> None:
        assert lease == self.lease
        self.fence.assert_current(lease)

    @staticmethod
    def _cas(current: PublicationRecord, state: PublicationState, expected_version: int) -> None:
        if current.state is not state or current.version != expected_version:
            raise StateTransitionError("CAS lost")


class InjectedCrash(RuntimeError):
    pass


class CrashOnce:
    def __init__(self, point: PublicationFaultPoint) -> None:
        self.point = point
        self.fired = False

    def __call__(self, point: PublicationFaultPoint, publication: PublicationRecord) -> None:
        assert isinstance(publication, PublicationRecord)
        if point is self.point and not self.fired:
            self.fired = True
            raise InjectedCrash(point.value)


def _service(
    repository: FakePublicationRepository,
    attempt_store: AttemptArtifactStore,
    canonical: LocalBlobStore,
    fence: FakeFence,
    fault: Optional[PublicationFaultInjector] = None,
    progress: Optional[PublicationProgressHook] = None,
) -> ArtifactPublicationService:
    return ArtifactPublicationService(
        repository,
        attempt_store,
        canonical,
        StreamingMediaArtifactValidator(WaveMediaProbe()),
        fence,
        fault,
        progress,
    )


def test_complete_publish_re_reads_promotes_finalizes_and_cleans(tmp_path: Path) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempt_store = LocalAttemptArtifactStore(tmp_path / "attempts")
    canonical = LocalBlobStore(tmp_path / "canonical")
    try:
        visible = _service(repository, attempt_store, canonical, fence).publish(
            lease, (_candidate(content),)
        )
        assert len(visible) == 1
        assert visible[0].sha256 == hashlib.sha256(content).hexdigest()
        assert visible[0].content_type == "audio/wav"
        record = next(iter(repository.records.values()))
        assert record.state is PublicationState.FINALIZED
        assert record.attempt_object_deleted_at is not None
        assert attempt_store.list_before(datetime.now(timezone.utc)) == ()
        assert canonical.stat(visible[0].storage_key).sha256 == visible[0].sha256
    finally:
        attempt_store.close()
        canonical.close()


@pytest.mark.parametrize(
    "point",
    (
        PublicationFaultPoint.AFTER_ATTEMPT_WRITE,
        PublicationFaultPoint.AFTER_OBJECT_RECORDED,
        PublicationFaultPoint.AFTER_VALIDATION,
        PublicationFaultPoint.AFTER_CANONICAL_PROMOTION,
        PublicationFaultPoint.AFTER_VALIDATED_RECORDED,
    ),
)
def test_each_precommit_crash_resumes_without_duplicate_visible_result(
    tmp_path: Path, point: PublicationFaultPoint
) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / point.value)
    try:
        with pytest.raises(InjectedCrash):
            _service(repository, attempts, canonical, fence, CrashOnce(point)).publish(
                lease, (_candidate(content),)
            )
        visible = _service(repository, attempts, canonical, fence).publish(
            lease, (_candidate(content),)
        )
        assert len(visible) == 1
        assert len(repository.records) == 1
        assert repository.get(visible[0].publication_id).state is PublicationState.FINALIZED
    finally:
        canonical.close()


def test_crash_resume_cannot_relax_sealed_max_size(tmp_path: Path) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    original = _candidate(content)
    try:
        with pytest.raises(InjectedCrash):
            _service(
                repository,
                attempts,
                canonical,
                fence,
                CrashOnce(PublicationFaultPoint.AFTER_ATTEMPT_WRITE),
            ).publish(lease, (original,))
        relaxed = PublicationCandidate(
            original.kind,
            original.content_type,
            original.chunks_factory,
            original.max_size_bytes + 1,
            original.expected_sha256,
            original.expected_size_bytes,
            original.metadata,
        )
        with pytest.raises(StateTransitionError, match="identity changed"):
            _service(repository, attempts, canonical, fence).publish(lease, (relaxed,))
        record = next(iter(repository.records.values()))
        assert record.max_size_bytes == original.max_size_bytes
        assert record.state is PublicationState.RESERVED
    finally:
        canonical.close()


def test_canonical_promotion_then_database_failure_is_reused_on_retry(tmp_path: Path) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    repository.fail_record_validated_once = True
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    try:
        with pytest.raises(RuntimeError, match="database failure"):
            _service(repository, attempts, canonical, fence).publish(lease, (_candidate(content),))
        digest = hashlib.sha256(content).hexdigest()
        assert canonical.stat(LocalBlobStore.canonical_key(digest)).sha256 == digest
        visible = _service(repository, attempts, canonical, fence).publish(
            lease, (_candidate(content),)
        )
        assert visible[0].sha256 == digest
    finally:
        canonical.close()


def test_lost_after_canonical_promotion_keeps_global_object_conservatively(
    tmp_path: Path,
) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    repository.fail_record_validated_once = True
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    try:
        with pytest.raises(RuntimeError, match="database failure"):
            _service(repository, attempts, canonical, fence).publish(lease, (_candidate(content),))
        record = next(iter(repository.records.values()))
        assert record.state is PublicationState.OBJECT_WRITTEN
        assert record.attempt_object_key is not None
        repository.orphan_keys = (record.attempt_object_key,)
        fence.current = False
        assert (
            ArtifactOrphanSweeper(repository, attempts).sweep(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )
            == 1
        )
        digest = hashlib.sha256(content).hexdigest()
        # No attempt-ledger fact is enough to prove a global canonical blob is
        # unreferenced.  This slice intentionally leaves it for conservative GC.
        assert canonical.stat(LocalBlobStore.canonical_key(digest)).sha256 == digest
        assert repository.get(record.publication_id).state is PublicationState.ABANDONED
    finally:
        canonical.close()


def test_stale_lease_cannot_record_or_finalize_and_orphan_is_swept(tmp_path: Path) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")

    def lose_fence(point: PublicationFaultPoint, publication: PublicationRecord) -> None:
        if point is PublicationFaultPoint.AFTER_ATTEMPT_WRITE:
            fence.current = False
            repository.orphan_keys = (
                attempt_key(
                    publication.job_id,
                    publication.attempt_id,
                    publication.lease_epoch,
                    publication.kind,
                    publication.publication_id,
                ),
            )

    try:
        with pytest.raises(StateTransitionError):
            _service(repository, attempts, canonical, fence, lose_fence).publish(
                lease, (_candidate(content),)
            )
        assert repository.visible == ()
        assert len(attempts.list_before(datetime.now(timezone.utc))) == 1
        assert (
            ArtifactOrphanSweeper(repository, attempts).sweep(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )
            == 1
        )
        assert attempts.list_before(datetime.now(timezone.utc)) == ()
        assert next(iter(repository.records.values())).state is PublicationState.ABANDONED
    finally:
        canonical.close()


def test_crash_after_attempt_write_without_resume_is_found_by_inventory(tmp_path: Path) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    try:
        with pytest.raises(InjectedCrash):
            _service(
                repository,
                attempts,
                canonical,
                fence,
                CrashOnce(PublicationFaultPoint.AFTER_ATTEMPT_WRITE),
            ).publish(lease, (_candidate(content),))
        record = next(iter(repository.records.values()))
        assert record.state is PublicationState.RESERVED
        assert record.attempt_object_key is None
        fence.current = False
        assert (
            ArtifactOrphanSweeper(repository, attempts).sweep(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )
            == 1
        )
        cleaned = repository.get(record.publication_id)
        assert cleaned.state is PublicationState.ABANDONED
        assert cleaned.attempt_object_deleted_at is not None
        assert attempts.list_before(datetime.now(timezone.utc)) == ()
    finally:
        canonical.close()


def test_inventory_deletes_old_unknown_key_but_preserves_current_reserved(tmp_path: Path) -> None:
    lease, fence = _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    unknown = attempt_key(str(uuid4()), str(uuid4()), 1, ArtifactKind.OUTPUT, str(uuid4()))
    attempts.put_stream(unknown, (b"unknown-old-object",))
    candidate = _candidate(_wav())
    current = repository.reserve(
        lease,
        PublicationSpec(
            candidate.kind,
            candidate.content_type,
            candidate.max_size_bytes,
            candidate.expected_sha256,
            candidate.expected_size_bytes,
            candidate.metadata,
        ),
    )
    current_key = attempt_key(
        current.job_id,
        current.attempt_id,
        current.lease_epoch,
        current.kind,
        current.publication_id,
    )
    attempts.put_stream(current_key, candidate.chunks_factory())
    try:
        assert (
            ArtifactOrphanSweeper(repository, attempts).sweep(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )
            == 1
        )
        with pytest.raises(KeyError):
            attempts.stat(unknown)
        assert attempts.stat(current_key).storage_key == current_key
        assert repository.get(current.publication_id).state is PublicationState.RESERVED
    finally:
        canonical.close()


def test_bounded_inventory_cursor_advances_past_large_known_active_prefix(
    tmp_path: Path,
) -> None:
    class CountingStore(InMemoryS3CompatibleAttemptStore):
        def __init__(self) -> None:
            super().__init__()
            self.scanned_pages: list[int] = []

        def inventory_page_before(self, cutoff: datetime, limit: int, scan_id: str):
            page = super().inventory_page_before(cutoff, limit, scan_id)
            self.scanned_pages.append(page.scanned)
            return page

    lease, fence = _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = CountingStore()
    known_keys = []
    for _ in range(7):
        job_id, attempt_id, publication_id = (str(uuid4()) for _ in range(3))
        key = attempt_key(job_id, attempt_id, 1, ArtifactKind.OUTPUT, publication_id)
        stored = attempts.put_stream(key, (b"known-active",))
        repository.records[publication_id] = PublicationRecord(
            publication_id,
            job_id,
            attempt_id,
            lease.worker_id,
            1,
            ArtifactKind.OUTPUT,
            "audio/wav",
            64,
            PublicationState.OBJECT_WRITTEN,
            1,
            attempt_object_key=key,
            observed_sha256=stored.sha256,
            observed_size_bytes=stored.size_bytes,
            observed_content_type="audio/wav",
        )
        known_keys.append(key)
    unknown_keys = []
    for _ in range(4):
        key = attempt_key(str(uuid4()), str(uuid4()), 1, ArtifactKind.OUTPUT, str(uuid4()))
        attempts.put_stream(key, (b"unknown-old",))
        unknown_keys.append(key)

    canonical_store = LocalBlobStore(tmp_path / "canonical")
    canonical = canonical_store.put_immutable(b"canonical-is-not-in-attempt-inventory")
    try:
        cleaned = 0
        for _ in range(6):
            cleaned += ArtifactOrphanSweeper(repository, attempts).sweep(
                datetime.now(timezone.utc) + timedelta(seconds=1), limit=3
            )
            if all(_missing(attempts, key) for key in unknown_keys):
                break
        assert cleaned == len(unknown_keys)
        assert all(attempts.stat(key).storage_key == key for key in known_keys)
        assert attempts.scanned_pages
        assert all(scanned <= 3 for scanned in attempts.scanned_pages)
        assert repository.known_query_sizes
        assert all(size <= 3 for size in repository.known_query_sizes)
        with canonical_store.open_reader(canonical.storage_key) as source:
            assert source.read() == b"canonical-is-not-in-attempt-inventory"
    finally:
        canonical_store.close()


def test_validation_failure_is_rejected_and_never_promoted(tmp_path: Path) -> None:
    content, lease, fence = _wav()[:44], _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    try:
        with pytest.raises(ArtifactRejectedError):
            _service(repository, attempts, canonical, fence).publish(lease, (_candidate(content),))
        record = next(iter(repository.records.values()))
        assert record.state is PublicationState.REJECTED
        assert repository.visible == ()
        assert record.observed_sha256 is not None
        with pytest.raises(FileNotFoundError):
            canonical.stat(LocalBlobStore.canonical_key(record.observed_sha256))
    finally:
        canonical.close()


def test_attempt_cleanup_failure_is_retried_without_touching_canonical(tmp_path: Path) -> None:
    class FailDeleteOnce(InMemoryS3CompatibleAttemptStore):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def delete(self, storage_key: str) -> None:
            if not self.failed:
                self.failed = True
                raise OSError("injected attempt cleanup failure")
            super().delete(storage_key)

    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = FailDeleteOnce()
    canonical = LocalBlobStore(tmp_path / "canonical")
    try:
        visible = _service(repository, attempts, canonical, fence).publish(
            lease, (_candidate(content),)
        )
        record = repository.get(visible[0].publication_id)
        assert record.attempt_object_deleted_at is None
        assert len(attempts.list_before(datetime.now(timezone.utc))) == 1
        assert (
            ArtifactOrphanSweeper(repository, attempts).sweep(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )
            == 1
        )
        assert canonical.stat(visible[0].storage_key).sha256 == visible[0].sha256
        assert repository.get(record.publication_id).attempt_object_deleted_at is not None
    finally:
        canonical.close()


def test_finalize_commit_then_crash_leaves_visible_winner_for_sweeper(tmp_path: Path) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    try:
        with pytest.raises(InjectedCrash):
            _service(
                repository,
                attempts,
                canonical,
                fence,
                CrashOnce(PublicationFaultPoint.AFTER_FINALIZE),
            ).publish(lease, (_candidate(content),))
        record = next(iter(repository.records.values()))
        assert record.state is PublicationState.FINALIZED
        assert len(repository.visible) == 1
        assert len(attempts.list_before(datetime.now(timezone.utc))) == 1
        assert (
            ArtifactOrphanSweeper(repository, attempts).sweep(
                datetime.now(timezone.utc) + timedelta(seconds=1)
            )
            == 1
        )
        assert record.canonical_blob_id in repository.blobs
        assert (
            canonical.stat(repository.visible[0].storage_key).sha256 == repository.visible[0].sha256
        )
    finally:
        canonical.close()


def test_candidate_size_limit_rejects_before_full_stream_is_written(tmp_path: Path) -> None:
    lease, fence = _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    yielded = []

    def chunks():
        for chunk in (b"1234", b"5678", b"must-not-be-consumed"):
            yielded.append(chunk)
            yield chunk

    candidate = PublicationCandidate(
        ArtifactKind.OUTPUT, "audio/wav", chunks, 6, expected_size_bytes=None
    )
    try:
        with pytest.raises(ArtifactRejectedError):
            _service(repository, attempts, canonical, fence).publish(lease, (candidate,))
        assert yielded == [b"1234", b"5678"]
        assert next(iter(repository.records.values())).state is PublicationState.REJECTED
    finally:
        canonical.close()


def test_unsupported_mime_is_rejected_before_reservation() -> None:
    lease, fence = _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    with pytest.raises(ValueError, match="allowed audio/video"):
        PublicationCandidate(
            ArtifactKind.OUTPUT,
            "application/octet-stream",
            lambda: (b"bytes",),
            10,
        )
    assert repository.records == {}


def test_progress_hook_covers_chunks_probe_and_canonical_and_can_cancel(
    tmp_path: Path,
) -> None:
    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = InMemoryS3CompatibleAttemptStore()
    canonical = LocalBlobStore(tmp_path / "canonical")
    observed: list[PublicationProgressPoint] = []
    expected_lease = lease

    def progress(
        lease: Lease,
        point: PublicationProgressPoint,
        bytes_processed: int,
    ) -> None:
        assert lease == expected_lease
        if point is PublicationProgressPoint.BEFORE_FINALIZE:
            assert bytes_processed == 0
        else:
            assert bytes_processed > 0
        observed.append(point)

    try:
        _service(repository, attempts, canonical, fence, progress=progress).publish(
            lease, (_candidate(content),)
        )
        assert {
            PublicationProgressPoint.ATTEMPT_WRITE,
            PublicationProgressPoint.VALIDATION_READ,
            PublicationProgressPoint.BEFORE_PROBE,
            PublicationProgressPoint.AFTER_PROBE,
            PublicationProgressPoint.CANONICAL_PROMOTION,
            PublicationProgressPoint.CANONICAL_VERIFY,
            PublicationProgressPoint.BEFORE_FINALIZE,
        }.issubset(observed)
    finally:
        canonical.close()

    second_lease, second_fence = _lease(), FakeFence()
    second_repository = FakePublicationRepository(second_lease, second_fence)
    second_attempts = InMemoryS3CompatibleAttemptStore()
    second_canonical = LocalBlobStore(tmp_path / "cancelled-canonical")

    def cancel(
        lease: Lease,
        point: PublicationProgressPoint,
        bytes_processed: int,
    ) -> None:
        assert isinstance(lease, Lease)
        assert bytes_processed > 0
        if point is PublicationProgressPoint.CANONICAL_PROMOTION:
            raise StateTransitionError("publication cancelled")

    try:
        with pytest.raises(StateTransitionError, match="cancelled"):
            _service(
                second_repository,
                second_attempts,
                second_canonical,
                second_fence,
                progress=cancel,
            ).publish(second_lease, (_candidate(content),))
        assert second_repository.visible == ()
        assert (
            next(iter(second_repository.records.values())).state is PublicationState.OBJECT_WRITTEN
        )
    finally:
        second_canonical.close()


@pytest.mark.parametrize("suffix", (b"extra", b""))
def test_second_read_is_bounded_by_validated_size_and_eof(tmp_path: Path, suffix: bytes) -> None:
    class ChangedOnSecondRead(InMemoryS3CompatibleAttemptStore):
        def __init__(self) -> None:
            super().__init__()
            self.reads = 0

        def open_reader(self, storage_key: str) -> BinaryIO:
            self.reads += 1
            original = super().open_reader(storage_key).read()
            if self.reads == 2:
                changed = original + suffix if suffix else original[:-1]
                return io.BytesIO(changed)
            return io.BytesIO(original)

    content, lease, fence = _wav(), _lease(), FakeFence()
    repository = FakePublicationRepository(lease, fence)
    attempts = ChangedOnSecondRead()
    canonical = LocalBlobStore(tmp_path / ("append" if suffix else "truncate"))
    try:
        with pytest.raises(ArtifactRejectedError, match="validated size"):
            _service(repository, attempts, canonical, fence).publish(lease, (_candidate(content),))
        assert repository.visible == ()
        assert next(iter(repository.records.values())).state is PublicationState.REJECTED
    finally:
        canonical.close()
