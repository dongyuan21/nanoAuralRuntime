"""Phase 3E publication application service and attempt-object recovery.

Object I/O is outside the database transaction by design.  The ledger CAS is
the authority: a stale worker may leave an attempt object, but only a current
lease can validate or finalize it, and only the repository can expose a winner.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import BinaryIO, Callable, Iterable, Mapping, Optional, Protocol, Sequence, Tuple
from uuid import uuid4

from .artifact_storage import AttemptArtifactStore, attempt_key, parse_attempt_key
from .artifact_validation import (
    ArtifactValidationError,
    ArtifactValidationPhase,
    ArtifactValidationSpec,
    ArtifactValidator,
)
from .domain import ArtifactKind, BlobRecord, BlobState
from .errors import DurableInvariantError, NotFoundError, StateTransitionError
from .publication import (
    PublicationRecord,
    PublicationSpec,
    PublicationState,
    VisibleArtifact,
)
from .queue import Lease
from .storage import LocalBlobStore
from .upload_transports import CanonicalBlobStore

_BUFFER_SIZE = 1024 * 1024


class ArtifactPublicationError(DurableInvariantError):
    """Publication cannot safely advance to a visible result."""


class ArtifactRejectedError(ArtifactPublicationError):
    """Immutable attempt bytes failed deterministic validation."""


class PublicationFaultPoint(str, Enum):
    AFTER_ATTEMPT_WRITE = "after_attempt_write"
    AFTER_OBJECT_RECORDED = "after_object_recorded"
    AFTER_VALIDATION = "after_validation"
    AFTER_CANONICAL_PROMOTION = "after_canonical_promotion"
    AFTER_VALIDATED_RECORDED = "after_validated_recorded"
    AFTER_FINALIZE = "after_finalize"


class PublicationFaultInjector(Protocol):
    def __call__(self, point: PublicationFaultPoint, publication: PublicationRecord) -> None: ...


class PublicationProgressPoint(str, Enum):
    ATTEMPT_WRITE = "attempt_write"
    VALIDATION_READ = "validation_read"
    BEFORE_PROBE = "before_probe"
    AFTER_PROBE = "after_probe"
    CANONICAL_PROMOTION = "canonical_promotion"
    CANONICAL_VERIFY = "canonical_verify"
    BEFORE_FINALIZE = "before_finalize"


class PublicationProgressHook(Protocol):
    def __call__(
        self, lease: Lease, point: PublicationProgressPoint, bytes_processed: int
    ) -> None: ...


class LeaseFence(Protocol):
    def assert_current(self, lease: Lease) -> None: ...


class PublicationRepository(Protocol):
    def get(self, publication_id: str) -> PublicationRecord: ...
    def reserve(self, lease: Lease, spec: PublicationSpec) -> PublicationRecord: ...
    def record_object(
        self,
        lease: Lease,
        publication_id: str,
        expected_version: int,
        observed_sha256: str,
        observed_size_bytes: int,
    ) -> PublicationRecord: ...
    def record_validated(
        self,
        lease: Lease,
        publication_id: str,
        expected_version: int,
        blob: BlobRecord,
        validator_metadata: Mapping[str, object],
    ) -> PublicationRecord: ...
    def finalize(
        self, lease: Lease, publications: Sequence[PublicationRecord]
    ) -> Tuple[VisibleArtifact, ...]: ...
    def reject(
        self, lease: Lease, publication_id: str, expected_version: int, reason: str
    ) -> PublicationRecord: ...
    def abandon(
        self, publication_id: str, expected_version: int, reason: str
    ) -> PublicationRecord: ...
    def cleanup_candidates(
        self, before: datetime, limit: int = 100
    ) -> Tuple[PublicationRecord, ...]: ...
    def record_cleanup(self, publication_id: str, expected_version: int) -> PublicationRecord: ...
    def known_attempt_keys(self, keys: Sequence[str]) -> Tuple[str, ...]: ...
    def orphan_keys_before(self, before: datetime, limit: int = 100) -> Tuple[str, ...]: ...


@dataclass(frozen=True)
class PublicationCandidate:
    """Replayable application-side output; it contains no user storage key."""

    kind: ArtifactKind
    content_type: str
    chunks_factory: Callable[[], Iterable[bytes]]
    max_size_bytes: int
    expected_sha256: Optional[str] = None
    expected_size_bytes: Optional[int] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind must be ArtifactKind")
        if not isinstance(self.content_type, str) or not self.content_type:
            raise ValueError("content_type must be non-empty")
        if not callable(self.chunks_factory):
            raise TypeError("chunks_factory must be callable and replayable")
        if (
            isinstance(self.max_size_bytes, bool)
            or not isinstance(self.max_size_bytes, int)
            or self.max_size_bytes < 1
        ):
            raise ValueError("max_size_bytes must be a positive integer")
        if self.expected_size_bytes is not None and (
            isinstance(self.expected_size_bytes, bool)
            or not isinstance(self.expected_size_bytes, int)
            or self.expected_size_bytes < 0
            or self.expected_size_bytes > self.max_size_bytes
        ):
            raise ValueError("expected_size_bytes must fit the configured limit")
        if self.expected_sha256 is not None:
            _sha256(self.expected_sha256)
        # The selected validator is media-only in this slice.  Reject an
        # unsupported MIME before creating any durable reservation.
        ArtifactValidationSpec(
            self.content_type,
            self.max_size_bytes,
            self.expected_sha256,
            self.expected_size_bytes,
        )
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class ArtifactPublicationService:
    """Advance replayable candidates through storage, validation and DB CAS."""

    def __init__(
        self,
        repository: PublicationRepository,
        attempt_store: AttemptArtifactStore,
        canonical_store: CanonicalBlobStore,
        validator: ArtifactValidator,
        lease_fence: LeaseFence,
        fault_injector: Optional[PublicationFaultInjector] = None,
        progress_hook: Optional[PublicationProgressHook] = None,
    ) -> None:
        self._repository = repository
        self._attempt_store = attempt_store
        self._canonical_store = canonical_store
        self._validator = validator
        self._lease_fence = lease_fence
        self._fault = fault_injector or (lambda _point, _record: None)
        self._progress = progress_hook

    def publish(
        self, lease: Lease, candidates: Sequence[PublicationCandidate]
    ) -> Tuple[VisibleArtifact, ...]:
        requested = tuple(candidates)
        if not requested:
            raise ValueError("publication candidates must not be empty")
        if len({item.kind for item in requested}) != len(requested):
            raise ValueError("publication candidate kinds must be unique")
        records = tuple(self._advance(lease, item) for item in requested)
        self._checkpoint(lease, PublicationProgressPoint.BEFORE_FINALIZE, 0)
        visible = self._repository.finalize(lease, records)
        for record in records:
            self._fault(
                PublicationFaultPoint.AFTER_FINALIZE,
                self._repository.get(record.publication_id),
            )
        self._cleanup_best_effort(records)
        return visible

    def _advance(self, lease: Lease, candidate: PublicationCandidate) -> PublicationRecord:
        spec = PublicationSpec(
            kind=candidate.kind,
            content_type=candidate.content_type,
            max_size_bytes=candidate.max_size_bytes,
            expected_sha256=candidate.expected_sha256,
            expected_size_bytes=candidate.expected_size_bytes,
            metadata=candidate.metadata,
        )
        record = self._repository.reserve(lease, spec)
        key = attempt_key(
            record.job_id,
            record.attempt_id,
            record.lease_epoch,
            record.kind,
            record.publication_id,
        )
        if record.state is PublicationState.RESERVED:
            self._lease_fence.assert_current(lease)
            try:
                object_ = self._attempt_store.put_stream(
                    key,
                    _bounded_candidate_chunks(
                        candidate.chunks_factory(),
                        record.max_size_bytes,
                        candidate.expected_size_bytes,
                        lambda size: self._checkpoint(
                            lease, PublicationProgressPoint.ATTEMPT_WRITE, size
                        ),
                    ),
                )
            except ArtifactRejectedError:
                self._repository.reject(
                    lease, record.publication_id, record.version, "object_size_limit"
                )
                raise
            self._lease_fence.assert_current(lease)
            try:
                _match_expected(
                    candidate,
                    record.max_size_bytes,
                    object_.sha256,
                    object_.size_bytes,
                )
            except ArtifactRejectedError:
                self._repository.reject(
                    lease, record.publication_id, record.version, "object_evidence_mismatch"
                )
                raise
            self._fault(PublicationFaultPoint.AFTER_ATTEMPT_WRITE, record)
            record = self._repository.record_object(
                lease,
                record.publication_id,
                record.version,
                object_.sha256,
                object_.size_bytes,
            )
            self._fault(PublicationFaultPoint.AFTER_OBJECT_RECORDED, record)
        if record.state is PublicationState.OBJECT_WRITTEN:
            record = self._validate_and_promote(lease, candidate, record, key)
        if record.state is PublicationState.VALIDATED:
            self._verify_validated_canonical(lease, record)
            return record
        if record.state is PublicationState.FINALIZED:
            raise ArtifactPublicationError(
                "publication already finalized; read the visible winner instead"
            )
        raise ArtifactPublicationError(
            "publication is terminal or cannot resume from its current state"
        )

    def _validate_and_promote(
        self,
        lease: Lease,
        candidate: PublicationCandidate,
        record: PublicationRecord,
        expected_key: str,
    ) -> PublicationRecord:
        if record.attempt_object_key != expected_key:
            raise ArtifactPublicationError("ledger attempt key differs from server identity")
        self._lease_fence.assert_current(lease)
        try:
            with self._attempt_store.open_reader(expected_key) as source:
                validated = self._validator.validate(
                    source,
                    ArtifactValidationSpec(
                        candidate.content_type,
                        record.max_size_bytes,
                        record.observed_sha256,
                        record.observed_size_bytes,
                    ),
                    lambda phase, bytes_processed: self._validation_checkpoint(
                        lease, phase, bytes_processed
                    ),
                )
        except (FileNotFoundError, KeyError) as error:
            self._reject_current(lease, record, "attempt_object_missing")
            raise ArtifactPublicationError("attempt object is unavailable") from error
        except ArtifactValidationError as error:
            self._reject_current(lease, record, "artifact_validation_failed")
            raise ArtifactRejectedError("attempt object failed validation") from error
        except ArtifactRejectedError:
            self._reject_current(lease, record, "object_evidence_drift")
            raise
        self._lease_fence.assert_current(lease)
        self._fault(PublicationFaultPoint.AFTER_VALIDATION, record)
        try:
            with self._attempt_store.open_reader(expected_key) as source:
                canonical = self._canonical_store.put_stream(
                    _iter_chunks(
                        source,
                        validated.size_bytes,
                        lambda size: self._checkpoint(
                            lease, PublicationProgressPoint.CANONICAL_PROMOTION, size
                        ),
                    )
                )
        except ArtifactRejectedError:
            self._reject_current(lease, record, "attempt_object_changed_after_validation")
            raise
        if canonical.sha256 != validated.sha256 or canonical.size_bytes != validated.size_bytes:
            self._reject_current(lease, record, "canonical_promotion_mismatch")
            raise ArtifactRejectedError("canonical promotion changed artifact identity")
        self._fault(PublicationFaultPoint.AFTER_CANONICAL_PROMOTION, record)
        self._lease_fence.assert_current(lease)
        blob = BlobRecord(
            str(uuid4()),
            canonical.sha256,
            canonical.size_bytes,
            canonical.storage_key,
            BlobState.VERIFIED,
            validated.content_type,
        )
        record = self._repository.record_validated(
            lease,
            record.publication_id,
            record.version,
            blob,
            dict(validated.metadata),
        )
        self._fault(PublicationFaultPoint.AFTER_VALIDATED_RECORDED, record)
        return record

    def _verify_validated_canonical(self, lease: Lease, record: PublicationRecord) -> None:
        if record.observed_sha256 is None or record.observed_size_bytes is None:
            raise ArtifactPublicationError("validated publication lacks content evidence")
        key = LocalBlobStore.canonical_key(record.observed_sha256)
        try:
            digest = hashlib.sha256()
            size = 0
            with self._canonical_store.open_reader(key) as source:
                for chunk in _iter_chunks(
                    source,
                    record.observed_size_bytes,
                    lambda current: self._checkpoint(
                        lease, PublicationProgressPoint.CANONICAL_VERIFY, current
                    ),
                ):
                    digest.update(chunk)
                    size += len(chunk)
        except (FileNotFoundError, KeyError) as error:
            raise ArtifactPublicationError("validated canonical object is unavailable") from error
        if digest.hexdigest() != record.observed_sha256 or size != record.observed_size_bytes:
            raise ArtifactPublicationError("validated canonical object differs from ledger")

    def _validation_checkpoint(
        self, lease: Lease, phase: ArtifactValidationPhase, size: int
    ) -> None:
        point = {
            ArtifactValidationPhase.STREAM: PublicationProgressPoint.VALIDATION_READ,
            ArtifactValidationPhase.BEFORE_PROBE: PublicationProgressPoint.BEFORE_PROBE,
            ArtifactValidationPhase.AFTER_PROBE: PublicationProgressPoint.AFTER_PROBE,
        }[phase]
        self._checkpoint(lease, point, size)

    def _checkpoint(
        self, lease: Lease, point: PublicationProgressPoint, bytes_processed: int
    ) -> None:
        self._lease_fence.assert_current(lease)
        if self._progress is not None:
            self._progress(lease, point, bytes_processed)

    def _reject_current(self, lease: Lease, record: PublicationRecord, reason: str) -> None:
        self._repository.reject(lease, record.publication_id, record.version, reason)

    def _cleanup_best_effort(self, records: Sequence[PublicationRecord]) -> None:
        for initial in records:
            try:
                current = self._repository.get(initial.publication_id)
                key = _record_key(current)
                self._attempt_store.delete(key)
                self._repository.record_cleanup(current.publication_id, current.version)
            except (OSError, RuntimeError, DurableInvariantError, NotFoundError):
                # Visibility is already committed.  The ledger-backed sweeper owns retry.
                continue


class ArtifactOrphanSweeper:
    """Delete only DB-authorized attempt objects; canonical blobs are untouchable."""

    def __init__(
        self, repository: PublicationRepository, attempt_store: AttemptArtifactStore
    ) -> None:
        self._repository = repository
        self._attempt_store = attempt_store

    def sweep(self, before: datetime, limit: int = 100) -> int:
        if not isinstance(before, datetime) or before.tzinfo is None or before.utcoffset() is None:
            raise ValueError("before must be timezone-aware")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be positive")
        cleaned = 0
        seen = set()
        for record in self._repository.cleanup_candidates(before, limit):
            seen.add(record.publication_id)
            try:
                if self._delete_record(record):
                    cleaned += 1
            except StateTransitionError:
                # Another sweeper won deletion/cleanup evidence.
                continue
        remaining = max(0, limit - len(seen))
        orphan_keys = self._repository.orphan_keys_before(before, remaining) if remaining else ()
        for key in orphan_keys:
            if len(seen) >= limit:
                break
            try:
                _job, _attempt, _epoch, _kind, publication_id = parse_attempt_key(key)
                if publication_id in seen:
                    continue
                record = self._repository.get(publication_id)
                seen.add(publication_id)
                if _record_key(record) != key:
                    raise ArtifactPublicationError("orphan key does not match publication identity")
                if record.state not in (
                    PublicationState.FINALIZED,
                    PublicationState.REJECTED,
                    PublicationState.ABANDONED,
                ):
                    record = self._repository.abandon(
                        record.publication_id, record.version, "orphan_sweep"
                    )
                if self._delete_record(record):
                    cleaned += 1
            except (NotFoundError, StateTransitionError):
                # Active leases are deliberately preserved; old unknown keys
                # are handled only by the age-bounded inventory below.
                continue
        # A process can crash after conditional object creation but before the
        # RESERVED->OBJECT_WRITTEN CAS records its key.  Inventory is therefore
        # required in addition to the DB's explicit key list.  Only objects
        # older than the caller's grace cutoff are considered; a still-current
        # ledger row is preserved when the DB-clock abandon CAS rejects it.
        remaining = max(0, limit - len(seen))
        if not remaining:
            return cleaned
        page = self._attempt_store.inventory_page_before(before, remaining, "orphan_sweep")
        inventory = page.objects
        known_inventory = set(
            self._repository.known_attempt_keys([object_.storage_key for object_ in inventory])
        )
        for object_ in inventory:
            if len(seen) >= limit:
                break
            key = object_.storage_key
            # Ledger-backed rows are handled by cleanup_candidates or
            # orphan_keys_before, both of which apply DB-owned terminal/stale
            # grace.  Inventory is only the crash window where an immutable
            # object exists but its key was never recorded.  Skipping known
            # keys also prevents an old mtime from probing an active lease.
            if key in known_inventory:
                continue
            _job, _attempt, _epoch, _kind, publication_id = parse_attempt_key(key)
            if publication_id in seen:
                continue
            seen.add(publication_id)
            try:
                record = self._repository.get(publication_id)
            except NotFoundError:
                self._attempt_store.delete(key)
                cleaned += 1
                continue
            if _record_key(record) != key:
                raise ArtifactPublicationError("inventoried key differs from ledger identity")
            try:
                if record.state not in (
                    PublicationState.FINALIZED,
                    PublicationState.REJECTED,
                    PublicationState.ABANDONED,
                ):
                    record = self._repository.abandon(
                        record.publication_id, record.version, "orphan_inventory_sweep"
                    )
                if self._delete_record(record):
                    cleaned += 1
            except StateTransitionError:
                continue
        return cleaned

    def _delete_record(self, record: PublicationRecord) -> bool:
        key = _record_key(record)
        try:
            self._attempt_store.stat(key)
            existed = True
        except (FileNotFoundError, KeyError):
            existed = False
        self._attempt_store.delete(key)
        self._repository.record_cleanup(record.publication_id, record.version)
        return existed


def _record_key(record: PublicationRecord) -> str:
    derived = attempt_key(
        record.job_id,
        record.attempt_id,
        record.lease_epoch,
        record.kind,
        record.publication_id,
    )
    if record.attempt_object_key is not None and record.attempt_object_key != derived:
        raise ArtifactPublicationError("ledger attempt key differs from server identity")
    return derived


def _match_expected(
    candidate: PublicationCandidate,
    sealed_max_size_bytes: int,
    sha256: str,
    size_bytes: int,
) -> None:
    if candidate.expected_sha256 is not None and sha256 != candidate.expected_sha256:
        raise ArtifactRejectedError("attempt object SHA-256 differs from declared evidence")
    if candidate.expected_size_bytes is not None and size_bytes != candidate.expected_size_bytes:
        raise ArtifactRejectedError("attempt object size differs from declared evidence")
    if size_bytes > sealed_max_size_bytes:
        raise ArtifactRejectedError("attempt object exceeds configured size limit")


def _iter_chunks(
    source: BinaryIO, expected_size_bytes: int, progress: Callable[[int], None]
) -> Iterable[bytes]:
    size = 0
    while True:
        chunk = source.read(_BUFFER_SIZE)
        if not chunk:
            break
        if not isinstance(chunk, bytes):
            raise TypeError("artifact reader must return bytes")
        size += len(chunk)
        if size > expected_size_bytes:
            raise ArtifactRejectedError("artifact changed beyond its validated size")
        progress(size)
        yield chunk
    if size != expected_size_bytes:
        raise ArtifactRejectedError("artifact ended before its validated size")


def _bounded_candidate_chunks(
    chunks: Iterable[bytes],
    max_size_bytes: int,
    expected_size_bytes: Optional[int],
    progress: Callable[[int], None],
) -> Iterable[bytes]:
    size = 0
    for chunk in chunks:
        if not isinstance(chunk, bytes):
            raise TypeError("publication candidate chunks must be bytes")
        size += len(chunk)
        if size > max_size_bytes or (
            expected_size_bytes is not None and size > expected_size_bytes
        ):
            raise ArtifactRejectedError("publication candidate exceeds its size bound")
        progress(size)
        yield chunk


def _sha256(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ValueError("sha256 must be lowercase 64-character hexadecimal")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("sha256 must be lowercase 64-character hexadecimal") from error
