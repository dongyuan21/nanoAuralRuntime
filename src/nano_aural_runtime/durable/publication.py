"""Fenced PostgreSQL artifact publication and visible-winner authority.

Publication object I/O is deliberately outside this module.  This repository
records immutable evidence and finalizes only after a caller has written,
re-read, validated, and promoted bytes.  Every worker-originated mutation uses
the lock order ``workers -> jobs -> job_attempts -> artifact_publications ->
blobs -> artifacts`` and PostgreSQL's clock for the lease fence.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Sequence, cast
from uuid import uuid4

from .domain import ArtifactKind, BlobRecord, BlobState
from .errors import DurableInvariantError, NotFoundError, StateTransitionError
from .queue import Lease
from .storage import LocalBlobStore


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(name))


def _sha256(value: Optional[str], name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ValueError("{0} must be a lowercase full SHA-256".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be hexadecimal".format(name)) from error


def _size(value: Optional[int], name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("{0} must be a non-negative integer".format(name))


def _positive_size(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("{0} must be a positive integer".format(name))


class PublicationState(str, Enum):
    RESERVED = "reserved"
    OBJECT_WRITTEN = "object_written"
    VALIDATED = "validated"
    FINALIZED = "finalized"
    REJECTED = "rejected"
    ABANDONED = "abandoned"


@dataclass(frozen=True)
class PublicationSpec:
    kind: ArtifactKind
    content_type: str
    max_size_bytes: int
    expected_sha256: Optional[str] = None
    expected_size_bytes: Optional[int] = None
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind must be an ArtifactKind")
        _text(self.content_type, "content_type")
        _positive_size(self.max_size_bytes, "max_size_bytes")
        _sha256(self.expected_sha256, "expected_sha256")
        _size(self.expected_size_bytes, "expected_size_bytes")
        if self.expected_size_bytes is not None and self.expected_size_bytes > self.max_size_bytes:
            raise ValueError("expected_size_bytes exceeds max_size_bytes")
        if not isinstance(self.metadata, Mapping):
            raise TypeError("metadata must be a mapping")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


@dataclass(frozen=True)
class PublicationRecord:
    publication_id: str
    job_id: str
    attempt_id: str
    worker_id: str
    lease_epoch: int
    kind: ArtifactKind
    content_type: str
    max_size_bytes: int
    state: PublicationState
    version: int
    expected_sha256: Optional[str] = None
    expected_size_bytes: Optional[int] = None
    attempt_object_key: Optional[str] = None
    observed_sha256: Optional[str] = None
    observed_size_bytes: Optional[int] = None
    observed_content_type: Optional[str] = None
    canonical_blob_id: Optional[str] = None
    metadata: Mapping[str, object] = field(default_factory=dict)
    validator_metadata: Mapping[str, object] = field(default_factory=dict)
    created_at: Optional[datetime] = None
    updated_at: Optional[datetime] = None
    terminal_reason: Optional[str] = None
    attempt_object_deleted_at: Optional[datetime] = None
    stale_since: Optional[datetime] = None

    def __post_init__(self) -> None:
        for name in ("publication_id", "job_id", "attempt_id", "worker_id", "content_type"):
            _text(str(getattr(self, name)), name)
        if not isinstance(self.kind, ArtifactKind):
            raise TypeError("kind must be an ArtifactKind")
        if not isinstance(self.state, PublicationState):
            raise TypeError("state must be a PublicationState")
        _positive_size(self.max_size_bytes, "max_size_bytes")
        if isinstance(self.lease_epoch, bool) or self.lease_epoch < 1:
            raise ValueError("lease_epoch must be positive")
        if isinstance(self.version, bool) or self.version < 0:
            raise ValueError("version must be non-negative")
        _sha256(self.expected_sha256, "expected_sha256")
        _sha256(self.observed_sha256, "observed_sha256")
        _size(self.expected_size_bytes, "expected_size_bytes")
        if self.expected_size_bytes is not None and self.expected_size_bytes > self.max_size_bytes:
            raise ValueError("expected_size_bytes exceeds max_size_bytes")
        _size(self.observed_size_bytes, "observed_size_bytes")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))
        object.__setattr__(
            self, "validator_metadata", MappingProxyType(dict(self.validator_metadata))
        )


@dataclass(frozen=True)
class VisibleArtifact:
    artifact_id: str
    publication_id: str
    job_id: str
    attempt_id: str
    kind: ArtifactKind
    blob_id: str
    sha256: str
    size_bytes: int
    storage_key: str
    content_type: str
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in (
            "artifact_id",
            "publication_id",
            "job_id",
            "attempt_id",
            "blob_id",
            "storage_key",
            "content_type",
        ):
            _text(getattr(self, name), name)
        _sha256(self.sha256, "sha256")
        _size(self.size_bytes, "size_bytes")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class PostgresPublicationRepository:
    """Versioned publication CAS plus exactly-one visible winner finalization."""

    _SELECT = """SELECT id,job_id,attempt_id,worker_id,lease_epoch,kind,
        expected_content_type,max_size_bytes,state,version,expected_sha256,expected_size_bytes,
        attempt_object_key,observed_sha256,observed_size_bytes,observed_content_type,
        canonical_blob_id,metadata,validator_metadata,created_at,updated_at,terminal_reason,
        attempt_object_deleted_at,stale_since
        FROM artifact_publications"""

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def get(self, publication_id: str) -> PublicationRecord:
        with self._connection.transaction():
            row = self._connection.execute(
                self._SELECT + " WHERE id=%s", (publication_id,)
            ).fetchone()
        if row is None:
            raise NotFoundError("publication not found: {0}".format(publication_id))
        return self._from_row(row)

    def reserve(self, lease: Lease, spec: PublicationSpec) -> PublicationRecord:
        if not isinstance(spec, PublicationSpec):
            raise TypeError("spec must be a PublicationSpec")
        publication_id = str(uuid4())
        with self._connection.transaction():
            self._lock_lease(lease)
            row = self._connection.execute(
                """INSERT INTO artifact_publications
                   (id,job_id,attempt_id,worker_id,lease_epoch,kind,expected_sha256,
                    expected_size_bytes,expected_content_type,max_size_bytes,metadata)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb)
                   ON CONFLICT (attempt_id,kind) DO NOTHING
                   RETURNING id""",
                (
                    publication_id,
                    lease.job_id,
                    lease.attempt_id,
                    lease.worker_id,
                    lease.lease_epoch,
                    spec.kind.value,
                    spec.expected_sha256,
                    spec.expected_size_bytes,
                    spec.content_type,
                    spec.max_size_bytes,
                    json.dumps(dict(spec.metadata), sort_keys=True),
                ),
            ).fetchone()
            if row is None:
                existing = self._connection.execute(
                    self._SELECT + " WHERE attempt_id=%s AND kind=%s FOR UPDATE",
                    (lease.attempt_id, spec.kind.value),
                ).fetchone()
                if existing is None:
                    raise StateTransitionError("publication reservation disappeared")
                record = self._from_row(existing)
                if not self._matches_spec(record, lease, spec):
                    raise StateTransitionError("publication kind was reserved with other identity")
                return record
            result = self._select_record(publication_id)
        return result

    def record_object(
        self,
        lease: Lease,
        publication_id: str,
        expected_version: int,
        observed_sha256: str,
        observed_size_bytes: int,
    ) -> PublicationRecord:
        _sha256(observed_sha256, "observed_sha256")
        _size(observed_size_bytes, "observed_size_bytes")
        with self._connection.transaction():
            self._lock_lease(lease)
            current = self._lock_publication(lease, publication_id)
            if (
                current.state is not PublicationState.RESERVED
                or current.version != expected_version
            ):
                raise StateTransitionError("publication object CAS lost")
            if observed_size_bytes > current.max_size_bytes:
                raise DurableInvariantError("publication object exceeds sealed size limit")
            key = self.attempt_object_key(current)
            row = self._connection.execute(
                """UPDATE artifact_publications SET state='object_written',version=version+1,
                   attempt_object_key=%s,observed_sha256=%s,observed_size_bytes=%s,
                   updated_at=clock_timestamp()
                   WHERE id=%s AND state='reserved' AND version=%s RETURNING id""",
                (key, observed_sha256, observed_size_bytes, publication_id, expected_version),
            ).fetchone()
            if row is None:
                raise StateTransitionError("publication object CAS lost")
            result = self._select_record(publication_id)
        return result

    def record_validated(
        self,
        lease: Lease,
        publication_id: str,
        expected_version: int,
        blob: BlobRecord,
        validator_metadata: Mapping[str, object],
    ) -> PublicationRecord:
        if not isinstance(blob, BlobRecord) or blob.state is not BlobState.VERIFIED:
            raise DurableInvariantError("validated publication requires a VERIFIED blob")
        if blob.storage_key != LocalBlobStore.canonical_key(blob.sha256):
            raise DurableInvariantError("validated publication blob key is not canonical")
        if not isinstance(validator_metadata, Mapping):
            raise TypeError("validator_metadata must be a mapping")
        with self._connection.transaction():
            self._lock_lease(lease)
            current = self._lock_publication(lease, publication_id)
            if (
                current.state is not PublicationState.OBJECT_WRITTEN
                or current.version != expected_version
            ):
                raise StateTransitionError("publication validation CAS lost")
            if (
                current.observed_sha256 != blob.sha256
                or current.observed_size_bytes != blob.size_bytes
                or current.content_type != blob.content_type
            ):
                raise DurableInvariantError("validated blob differs from recorded object evidence")
            row = self._connection.execute(
                """INSERT INTO blobs (id,sha256,size_bytes,storage_key,content_type,state)
                   VALUES (%s,%s,%s,%s,%s,'verified') ON CONFLICT DO NOTHING
                   RETURNING id,size_bytes,storage_key,content_type,state""",
                (
                    blob.blob_id,
                    blob.sha256,
                    blob.size_bytes,
                    blob.storage_key,
                    blob.content_type,
                ),
            ).fetchone()
            if row is None:
                row = self._connection.execute(
                    """SELECT id,size_bytes,storage_key,content_type,state FROM blobs
                       WHERE sha256=%s FOR UPDATE""",
                    (blob.sha256,),
                ).fetchone()
            if row is None or tuple(row[1:]) != (
                blob.size_bytes,
                blob.storage_key,
                blob.content_type,
                BlobState.VERIFIED.value,
            ):
                raise DurableInvariantError("canonical SHA maps to conflicting blob metadata")
            updated = self._connection.execute(
                """UPDATE artifact_publications SET state='validated',version=version+1,
                   canonical_blob_id=%s,observed_content_type=%s,validator_metadata=%s::jsonb,
                   updated_at=clock_timestamp()
                   WHERE id=%s AND state='object_written' AND version=%s RETURNING id""",
                (
                    row[0],
                    blob.content_type,
                    json.dumps(dict(validator_metadata), sort_keys=True),
                    publication_id,
                    expected_version,
                ),
            ).fetchone()
            if updated is None:
                raise StateTransitionError("publication validation CAS lost")
            result = self._select_record(publication_id)
        return result

    def reject(
        self, lease: Lease, publication_id: str, expected_version: int, reason: str
    ) -> PublicationRecord:
        return self._terminalize(
            lease, publication_id, expected_version, PublicationState.REJECTED, reason
        )

    def abandon(self, publication_id: str, expected_version: int, reason: str) -> PublicationRecord:
        """Fence a stale orphan after the migration's DB-clock grace period."""

        _text(reason, "reason")
        grace_started = False
        with self._connection.transaction():
            row = self._connection.execute(
                self._SELECT + " WHERE id=%s FOR UPDATE", (publication_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("publication not found: {0}".format(publication_id))
            current = self._from_row(row)
            if current.version != expected_version or current.state in (
                PublicationState.FINALIZED,
                PublicationState.REJECTED,
                PublicationState.ABANDONED,
            ):
                raise StateTransitionError("publication abandon CAS lost")
            if current.stale_since is None:
                changed = self._connection.execute(
                    """UPDATE artifact_publications SET version=version+1,
                       stale_since=clock_timestamp(),updated_at=clock_timestamp()
                       WHERE id=%s AND version=%s AND stale_since IS NULL
                         AND state IN ('reserved','object_written','validated')""",
                    (publication_id, expected_version),
                ).rowcount
                if changed != 1:
                    raise StateTransitionError("publication stale grace CAS lost")
                result = self._select_record(publication_id)
                grace_started = True
            else:
                grace_elapsed = bool(
                    self._connection.execute(
                        """SELECT clock_timestamp() >= stale_since + interval '5 minutes'
                           FROM artifact_publications WHERE id=%s AND version=%s""",
                        (publication_id, expected_version),
                    ).fetchone()[0]
                )
                if not grace_elapsed:
                    raise StateTransitionError("publication stale grace has not elapsed")
                changed = self._connection.execute(
                    """UPDATE artifact_publications SET state='abandoned',version=version+1,
                       terminal_reason=%s,terminal_at=clock_timestamp(),updated_at=clock_timestamp()
                       WHERE id=%s AND version=%s AND state IN ('reserved','object_written','validated')""",
                    (reason, publication_id, expected_version),
                ).rowcount
                if changed != 1:
                    raise StateTransitionError("publication abandon CAS lost")
                result = self._select_record(publication_id)
        if grace_started:
            raise StateTransitionError("publication stale grace has started")
        return result

    def finalize(
        self, lease: Lease, publications: Sequence[PublicationRecord]
    ) -> tuple[VisibleArtifact, ...]:
        requested = tuple(publications)
        if not requested:
            raise ValueError("publications must not be empty")
        if len({item.publication_id for item in requested}) != len(requested):
            raise ValueError("publication ids must be unique")
        with self._connection.transaction():
            job = self._lock_lease(lease)
            attempt = self._connection.execute(
                """SELECT attempt_no FROM job_attempts WHERE id=%s AND job_id=%s
                   AND worker_id=%s AND lease_epoch=%s AND state='active'
                   AND lease_expires_at>clock_timestamp() FOR UPDATE""",
                (lease.attempt_id, lease.job_id, lease.worker_id, lease.lease_epoch),
            ).fetchone()
            if attempt is None:
                raise StateTransitionError("lease is no longer current")
            ids = sorted(item.publication_id for item in requested)
            rows = self._connection.execute(
                self._SELECT + " WHERE id=ANY(%s::uuid[]) ORDER BY id FOR UPDATE",
                (ids,),
            ).fetchall()
            current = tuple(self._from_row(row) for row in rows)
            if len(current) != len(requested):
                raise StateTransitionError("publication set is incomplete")
            expected_versions = {item.publication_id: item.version for item in requested}
            if any(
                item.job_id != lease.job_id
                or item.attempt_id != lease.attempt_id
                or item.worker_id != lease.worker_id
                or item.lease_epoch != lease.lease_epoch
                or item.state is not PublicationState.VALIDATED
                or expected_versions[item.publication_id] != item.version
                for item in current
            ):
                raise StateTransitionError("publication set is stale or not validated")
            required = tuple(ArtifactKind(str(kind)) for kind in cast(Sequence[object], job[0]))
            if {item.kind for item in current} != set(required) or len(current) != len(required):
                raise DurableInvariantError(
                    "finalization requires the exact required artifact kinds"
                )
            blob_ids = sorted({str(item.canonical_blob_id) for item in current})
            blobs = self._connection.execute(
                """SELECT id FROM blobs WHERE id=ANY(%s::uuid[]) AND state='verified'
                   ORDER BY id FOR UPDATE""",
                (blob_ids,),
            ).fetchall()
            if len(blobs) != len(blob_ids):
                raise DurableInvariantError("finalization requires VERIFIED canonical blobs")
            self._connection.execute(
                "SELECT id FROM artifacts WHERE job_id=%s ORDER BY id FOR UPDATE", (lease.job_id,)
            ).fetchall()
            for item in current:
                artifact_id = str(uuid4())
                self._connection.execute(
                    """INSERT INTO artifacts
                       (id,job_id,attempt_id,blob_id,kind,state,metadata,publication_id)
                       VALUES (%s,%s,%s,%s,%s,'ready',%s::jsonb,%s)""",
                    (
                        artifact_id,
                        lease.job_id,
                        lease.attempt_id,
                        item.canonical_blob_id,
                        item.kind.value,
                        json.dumps(dict(item.metadata), sort_keys=True),
                        item.publication_id,
                    ),
                )
                changed = self._connection.execute(
                    """UPDATE artifact_publications SET state='finalized',version=version+1,
                       terminal_at=clock_timestamp(),updated_at=clock_timestamp()
                       WHERE id=%s AND state='validated' AND version=%s""",
                    (item.publication_id, item.version),
                ).rowcount
                if changed != 1:
                    raise StateTransitionError("publication finalization CAS lost")
            attempt_changed = self._connection.execute(
                """UPDATE job_attempts a SET state='succeeded',finished_at=clock_timestamp(),
                   heartbeat_at=NULL,lease_expires_at=NULL
                   WHERE a.id=%s AND a.job_id=%s AND a.worker_id=%s
                     AND a.lease_epoch=%s AND a.state='active'
                     AND a.lease_expires_at>clock_timestamp()
                     AND EXISTS (SELECT 1 FROM workers w WHERE w.id=a.worker_id AND w.state='busy')
                     AND EXISTS (SELECT 1 FROM jobs j WHERE j.id=a.job_id AND j.state='running'
                       AND j.current_attempt_id=a.id AND j.lease_epoch=a.lease_epoch
                       AND j.cancel_requested_at IS NULL)""",
                (lease.attempt_id, lease.job_id, lease.worker_id, lease.lease_epoch),
            ).rowcount
            if attempt_changed != 1:
                raise StateTransitionError("winner lease expired before finalization")
            self._connection.execute(
                "UPDATE workers SET state='ready' WHERE id=%s", (lease.worker_id,)
            )
            self._connection.execute(
                """INSERT INTO job_events (job_id,attempt_id,event_type,payload)
                   VALUES (%s,%s,'job_succeeded','{}'::jsonb)""",
                (lease.job_id, lease.attempt_id),
            )
            changed = self._connection.execute(
                """UPDATE jobs SET state='succeeded',current_attempt_id=NULL,
                   winning_attempt_id=%s,updated_at=clock_timestamp()
                   WHERE id=%s AND state='running' AND current_attempt_id=%s
                     AND lease_epoch=%s AND cancel_requested_at IS NULL""",
                (lease.attempt_id, lease.job_id, lease.attempt_id, lease.lease_epoch),
            ).rowcount
            if changed != 1:
                raise StateTransitionError("job winner CAS lost")
        return self.visible_winner(lease.job_id)

    def visible_winner(
        self, job_id: str, namespace_id: Optional[str] = None
    ) -> tuple[VisibleArtifact, ...]:
        namespace_clause = " AND j.namespace_id=%s" if namespace_id is not None else ""
        params: tuple[object, ...] = (
            (job_id, namespace_id) if namespace_id is not None else (job_id,)
        )
        with self._connection.transaction():
            rows = self._connection.execute(
                """SELECT a.id,a.publication_id,a.job_id,a.attempt_id,a.kind,b.id,b.sha256,
                          b.size_bytes,b.storage_key,b.content_type,a.metadata
                   FROM jobs j JOIN artifacts a ON a.attempt_id=j.winning_attempt_id AND a.job_id=j.id
                   JOIN artifact_publications p ON p.id=a.publication_id
                   JOIN blobs b ON b.id=a.blob_id
                   WHERE j.id=%s"""
                + namespace_clause
                + """ AND j.state='succeeded' AND a.state='ready' AND p.state='finalized'
                     AND p.job_id=j.id AND p.attempt_id=j.winning_attempt_id
                     AND p.canonical_blob_id=a.blob_id AND b.state='verified'
                   ORDER BY a.kind""",
                params,
            ).fetchall()
        return tuple(
            VisibleArtifact(
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                ArtifactKind(str(row[4])),
                str(row[5]),
                str(row[6]),
                int(row[7]),
                str(row[8]),
                str(row[9]),
                dict(row[10]),
            )
            for row in rows
        )

    def cleanup_candidates(
        self, before: datetime, limit: int = 100
    ) -> tuple[PublicationRecord, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be positive")
        with self._connection.transaction():
            rows = self._connection.execute(
                self._SELECT
                + """ WHERE state IN ('finalized','rejected','abandoned')
                     AND attempt_object_deleted_at IS NULL AND terminal_at < %s
                     ORDER BY terminal_at,id LIMIT %s""",
                (before, limit),
            ).fetchall()
        return tuple(self._from_row(row) for row in rows)

    def record_cleanup(self, publication_id: str, expected_version: int) -> PublicationRecord:
        """Record successful deletion of the immutable attempt-specific object."""

        with self._connection.transaction():
            row = self._connection.execute(
                self._SELECT + " WHERE id=%s FOR UPDATE", (publication_id,)
            ).fetchone()
            if row is None:
                raise NotFoundError("publication not found: {0}".format(publication_id))
            current = self._from_row(row)
            if (
                current.version != expected_version
                or current.state
                not in (
                    PublicationState.FINALIZED,
                    PublicationState.REJECTED,
                    PublicationState.ABANDONED,
                )
                or current.attempt_object_deleted_at is not None
            ):
                raise StateTransitionError("publication cleanup CAS lost")
            changed = self._connection.execute(
                """UPDATE artifact_publications SET version=version+1,
                   attempt_object_deleted_at=clock_timestamp(),updated_at=clock_timestamp()
                   WHERE id=%s AND version=%s AND attempt_object_deleted_at IS NULL
                     AND state IN ('finalized','rejected','abandoned')""",
                (publication_id, expected_version),
            ).rowcount
            if changed != 1:
                raise StateTransitionError("publication cleanup CAS lost")
            result = self._select_record(publication_id)
        return result

    def known_attempt_keys(self, keys: Sequence[str]) -> tuple[str, ...]:
        """Return only exact attempt-object keys present in the immutable ledger."""

        values = tuple(keys)
        if not values:
            return ()
        if not all(isinstance(key, str) and key for key in values):
            raise ValueError("keys must be non-empty strings")
        with self._connection.transaction():
            rows = self._connection.execute(
                """SELECT attempt_object_key FROM artifact_publications
                   WHERE attempt_object_key=ANY(%s::text[]) ORDER BY attempt_object_key""",
                (list(values),),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    def orphan_keys_before(self, before: datetime, limit: int = 100) -> tuple[str, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
            raise ValueError("limit must be positive")
        with self._connection.transaction():
            rows = self._connection.execute(
                """SELECT p.attempt_object_key FROM artifact_publications p
                   LEFT JOIN jobs j ON j.id=p.job_id
                   LEFT JOIN job_attempts a ON a.id=p.attempt_id
                   WHERE p.attempt_object_key IS NOT NULL AND p.updated_at < %s
                     AND p.state <> 'finalized'
                     AND (p.state IN ('rejected','abandoned') OR j.state <> 'running'
                          OR j.current_attempt_id IS DISTINCT FROM p.attempt_id
                          OR a.state <> 'active' OR a.lease_epoch <> p.lease_epoch)
                   ORDER BY p.updated_at,p.id LIMIT %s""",
                (before, limit),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)

    @staticmethod
    def attempt_object_key(publication: PublicationRecord) -> str:
        return "attempts/{0}/{1}/epoch-{2}/{3}/{4}".format(
            publication.job_id,
            publication.attempt_id,
            publication.lease_epoch,
            publication.kind.value,
            publication.publication_id,
        )

    def _terminalize(
        self,
        lease: Lease,
        publication_id: str,
        expected_version: int,
        state: PublicationState,
        reason: str,
    ) -> PublicationRecord:
        _text(reason, "reason")
        with self._connection.transaction():
            self._lock_lease(lease)
            current = self._lock_publication(lease, publication_id)
            if current.version != expected_version or current.state in (
                PublicationState.FINALIZED,
                PublicationState.REJECTED,
                PublicationState.ABANDONED,
            ):
                raise StateTransitionError("publication terminal CAS lost")
            changed = self._connection.execute(
                """UPDATE artifact_publications SET state=%s,version=version+1,
                   terminal_reason=%s,terminal_at=clock_timestamp(),updated_at=clock_timestamp()
                   WHERE id=%s AND version=%s AND state IN ('reserved','object_written','validated')""",
                (state.value, reason, publication_id, expected_version),
            ).rowcount
            if changed != 1:
                raise StateTransitionError("publication terminal CAS lost")
            result = self._select_record(publication_id)
        return result

    def _select_record(self, publication_id: str) -> PublicationRecord:
        row = self._connection.execute(self._SELECT + " WHERE id=%s", (publication_id,)).fetchone()
        if row is None:
            raise NotFoundError("publication not found: {0}".format(publication_id))
        return self._from_row(row)

    def _lock_lease(self, lease: Lease) -> tuple[Sequence[object]]:
        worker = self._connection.execute(
            "SELECT id FROM workers WHERE id=%s AND state='busy' FOR UPDATE",
            (lease.worker_id,),
        ).fetchone()
        if worker is None:
            raise StateTransitionError("lease is no longer current")
        job = self._connection.execute(
            """SELECT required_artifact_kinds FROM jobs
               WHERE id=%s AND state='running' AND current_attempt_id=%s
                 AND lease_epoch=%s AND cancel_requested_at IS NULL FOR UPDATE""",
            (lease.job_id, lease.attempt_id, lease.lease_epoch),
        ).fetchone()
        if job is None:
            raise StateTransitionError("lease is no longer current")
        attempt = self._connection.execute(
            """SELECT attempt_no FROM job_attempts WHERE id=%s AND job_id=%s
               AND worker_id=%s AND lease_epoch=%s AND state='active'
               AND lease_expires_at>clock_timestamp() FOR UPDATE""",
            (lease.attempt_id, lease.job_id, lease.worker_id, lease.lease_epoch),
        ).fetchone()
        if attempt is None:
            raise StateTransitionError("lease is no longer current")
        return (cast(Sequence[object], job[0]),)

    def _lock_publication(self, lease: Lease, publication_id: str) -> PublicationRecord:
        row = self._connection.execute(
            self._SELECT + " WHERE id=%s FOR UPDATE", (publication_id,)
        ).fetchone()
        if row is None:
            raise NotFoundError("publication not found: {0}".format(publication_id))
        record = self._from_row(row)
        if (
            record.job_id,
            record.attempt_id,
            record.worker_id,
            record.lease_epoch,
        ) != (lease.job_id, lease.attempt_id, lease.worker_id, lease.lease_epoch):
            raise StateTransitionError("publication is owned by another lease")
        return record

    @staticmethod
    def _matches_spec(record: PublicationRecord, lease: Lease, spec: PublicationSpec) -> bool:
        return (
            record.job_id == lease.job_id
            and record.attempt_id == lease.attempt_id
            and record.worker_id == lease.worker_id
            and record.lease_epoch == lease.lease_epoch
            and record.kind is spec.kind
            and record.content_type == spec.content_type
            and record.max_size_bytes == spec.max_size_bytes
            and record.expected_sha256 == spec.expected_sha256
            and record.expected_size_bytes == spec.expected_size_bytes
            and dict(record.metadata) == dict(spec.metadata)
        )

    @staticmethod
    def _from_row(row: Sequence[object]) -> PublicationRecord:
        return PublicationRecord(
            publication_id=str(row[0]),
            job_id=str(row[1]),
            attempt_id=str(row[2]),
            worker_id=str(row[3]),
            lease_epoch=int(cast(Any, row[4])),
            kind=ArtifactKind(str(row[5])),
            content_type=str(row[6]),
            max_size_bytes=int(cast(Any, row[7])),
            state=PublicationState(str(row[8])),
            version=int(cast(Any, row[9])),
            expected_sha256=str(row[10]) if row[10] is not None else None,
            expected_size_bytes=(int(cast(Any, row[11])) if row[11] is not None else None),
            attempt_object_key=str(row[12]) if row[12] is not None else None,
            observed_sha256=str(row[13]) if row[13] is not None else None,
            observed_size_bytes=(int(cast(Any, row[14])) if row[14] is not None else None),
            observed_content_type=str(row[15]) if row[15] is not None else None,
            canonical_blob_id=str(row[16]) if row[16] is not None else None,
            metadata=cast(Mapping[str, object], row[17]),
            validator_metadata=(cast(Mapping[str, object], row[18]) if row[18] is not None else {}),
            created_at=row[19] if isinstance(row[19], datetime) else None,
            updated_at=row[20] if isinstance(row[20], datetime) else None,
            terminal_reason=str(row[21]) if row[21] is not None else None,
            attempt_object_deleted_at=(row[22] if isinstance(row[22], datetime) else None),
            stale_since=(row[23] if isinstance(row[23], datetime) else None),
        )
