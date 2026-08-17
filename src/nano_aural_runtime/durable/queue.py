"""Phase 3C fenced PostgreSQL queue primitives; no model or artifact publication.

All mutating paths use the lock order ``workers -> jobs -> job_attempts``.
The database clock, rather than a worker's wall clock, decides lease validity.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any, Optional
from uuid import uuid4

from .domain import (
    ArtifactKind,
    AssetKind,
    AssetRecord,
    AssetState,
    AttemptRecord,
    AttemptState,
    BlobRecord,
    BlobState,
    DeploymentRecord,
    DeploymentState,
    JobInput,
    JobRecord,
    JobState,
)
from .errors import StateTransitionError
from .materialization import VerifiedInputEvidence


@dataclass(frozen=True)
class Lease:
    job_id: str
    attempt_id: str
    worker_id: str
    lease_epoch: int
    expires_at: datetime


def _positive_int(value: int, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("{0} must be a positive integer".format(name))


class PostgresLeaseQueue:
    """Atomic claim, fence, cancellation and recovery for a single worker lease.

    This deliberately has no successful-finalization operation.  Phase 3C can
    retain an in-process candidate only; Phase 3E owns artifacts and success.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    def close(self) -> None:
        """Close only a worker-owned monitor connection when the driver supports it."""
        close = getattr(self._connection, "close", None)
        if callable(close):
            close()

    def claim_next(self, worker_id: str, lease_seconds: int) -> Optional[Lease]:
        _positive_int(lease_seconds, "lease_seconds")
        with self._connection.transaction():
            worker = self._connection.execute(
                """SELECT model_deployment_id FROM workers
                   WHERE id=%s AND state='ready' FOR UPDATE SKIP LOCKED""",
                (worker_id,),
            ).fetchone()
            if worker is None:
                return None
            job = self._connection.execute(
                """SELECT id,lease_epoch FROM jobs
                   WHERE state='queued' AND cancel_requested_at IS NULL
                     AND model_deployment_id=%s
                     AND (retry_not_before IS NULL OR retry_not_before <= clock_timestamp())
                   ORDER BY created_at,id FOR UPDATE SKIP LOCKED LIMIT 1""",
                (worker[0],),
            ).fetchone()
            if job is None:
                return None
            attempt_id, epoch = str(uuid4()), int(job[1]) + 1
            row = self._connection.execute(
                """INSERT INTO job_attempts
                   (id,job_id,worker_id,attempt_no,lease_epoch,state,heartbeat_at,lease_expires_at)
                   SELECT %s,%s,%s,COALESCE(max(attempt_no),0)+1,%s,'active',
                     clock_timestamp(),clock_timestamp()+(%s * interval '1 second')
                   FROM job_attempts WHERE job_id=%s RETURNING lease_expires_at""",
                (attempt_id, job[0], worker_id, epoch, lease_seconds, job[0]),
            ).fetchone()
            self._connection.execute(
                """UPDATE jobs SET state='running',current_attempt_id=%s,lease_epoch=%s,
                   retry_not_before=NULL,updated_at=clock_timestamp() WHERE id=%s""",
                (attempt_id, epoch, job[0]),
            )
            self._connection.execute(
                "UPDATE workers SET state='busy',last_heartbeat_at=clock_timestamp() WHERE id=%s",
                (worker_id,),
            )
            self._event(str(job[0]), attempt_id, "attempt_started")
            return Lease(str(job[0]), attempt_id, worker_id, epoch, row[0])

    def assert_current(self, lease: Lease) -> None:
        """Raise when a lease lost its epoch, ownership, cancellation or expiry."""

        # This is frequently called between object-I/O stages.  Commit the
        # read immediately so a subsequent repository transaction does not
        # become a savepoint inside an accidental psycopg implicit transaction
        # and retain publication locks across slow I/O.
        with self._connection.transaction():
            row = self._connection.execute(
                """SELECT 1 FROM job_attempts a JOIN jobs j ON j.id=a.job_id
                   JOIN workers w ON w.id=a.worker_id
                   WHERE a.id=%s AND a.job_id=%s AND a.worker_id=%s AND a.lease_epoch=%s
                     AND a.state='active' AND a.lease_expires_at > clock_timestamp()
                     AND j.state='running' AND j.current_attempt_id=a.id
                     AND j.lease_epoch=a.lease_epoch AND j.cancel_requested_at IS NULL
                     AND w.state='busy'""",
                (lease.attempt_id, lease.job_id, lease.worker_id, lease.lease_epoch),
            ).fetchone()
        if row is None:
            raise StateTransitionError("lease is no longer current")

    def load_verified_input_evidence(
        self, lease: Lease
    ) -> tuple[JobRecord, AttemptRecord, tuple[VerifiedInputEvidence, ...]]:
        """Read DB-owned, verified canonical input evidence under the same fence.

        No path, staging key, ETag, or caller-selected asset enters this API.
        The materializer must fence again immediately before and after byte I/O.
        """

        with self._connection.transaction():
            self._lock_current(lease, require_unexpired=True)
            input_count = self._connection.execute(
                "SELECT count(*) FROM job_inputs WHERE job_id=%s", (lease.job_id,)
            ).fetchone()[0]
            rows = self._connection.execute(
                """SELECT j.namespace_id,j.idempotency_key,j.request_sha256,j.request_json,
                     j.model_deployment_id,j.required_artifact_kinds,j.lease_epoch,
                     a.attempt_no,i.role,asset.id,asset.blob_id,asset.kind,asset.metadata,
                     blob.sha256,blob.size_bytes,blob.storage_key,blob.content_type
                   FROM jobs j JOIN job_attempts a ON a.id=j.current_attempt_id
                   JOIN job_inputs i ON i.job_id=j.id JOIN assets asset ON asset.id=i.asset_id
                   JOIN blobs blob ON blob.id=asset.blob_id
                   WHERE j.id=%s AND a.id=%s AND a.lease_epoch=%s
                     AND asset.namespace_id=j.namespace_id AND asset.state='verified'
                     AND blob.state='verified'
                   ORDER BY i.role""",
                (lease.job_id, lease.attempt_id, lease.lease_epoch),
            ).fetchall()
            if len(rows) != input_count:
                raise StateTransitionError("job inputs are not all verified canonical assets")
            if not rows:
                header = self._connection.execute(
                    """SELECT j.namespace_id,j.idempotency_key,j.request_sha256,j.request_json,
                       j.model_deployment_id,j.required_artifact_kinds,j.lease_epoch,a.attempt_no
                       FROM jobs j JOIN job_attempts a ON a.id=j.current_attempt_id
                       WHERE j.id=%s AND a.id=%s AND a.lease_epoch=%s""",
                    (lease.job_id, lease.attempt_id, lease.lease_epoch),
                ).fetchone()
                if header is None:
                    raise StateTransitionError("current lease disappeared")
                job = JobRecord(
                    lease.job_id,
                    str(header[0]),
                    str(header[1]),
                    str(header[2]),
                    dict(header[3]),
                    str(header[4]),
                    (),
                    tuple(ArtifactKind(str(kind)) for kind in header[5]),
                    JobState.RUNNING,
                    lease.attempt_id,
                    None,
                    False,
                    int(header[6]),
                )
                attempt = AttemptRecord(
                    lease.attempt_id,
                    lease.job_id,
                    lease.worker_id,
                    int(header[7]),
                    lease.lease_epoch,
                    AttemptState.ACTIVE,
                )
                self.assert_current(lease)
                return job, attempt, ()
            first = rows[0]
            inputs = tuple(JobInput(str(row[9 - 1]), str(row[9])) for row in rows)
            job = JobRecord(
                lease.job_id,
                str(first[0]),
                str(first[1]),
                str(first[2]),
                dict(first[3]),
                str(first[4]),
                inputs,
                tuple(ArtifactKind(str(kind)) for kind in first[5]),
                JobState.RUNNING,
                lease.attempt_id,
                None,
                False,
                int(first[6]),
            )
            attempt = AttemptRecord(
                lease.attempt_id,
                lease.job_id,
                lease.worker_id,
                int(first[7]),
                lease.lease_epoch,
                AttemptState.ACTIVE,
            )
            evidence = tuple(
                VerifiedInputEvidence(
                    str(row[8]),
                    AssetRecord(
                        str(row[9]),
                        str(row[10]),
                        str(first[0]),
                        kind=AssetKind(str(row[11])),
                        state=AssetState.VERIFIED,
                        metadata=dict(row[12]),
                    ),
                    BlobRecord(
                        str(row[10]),
                        str(row[13]),
                        int(row[14]),
                        str(row[15]),
                        BlobState.VERIFIED,
                        str(row[16]),
                    ),
                )
                for row in rows
            )
            self.assert_current(lease)
            return job, attempt, evidence

    def cancellation_requested(self, lease: Lease) -> bool:
        # A monitor owns a dedicated psycopg connection.  Bound this read in
        # its own transaction so it cannot leave an implicit outer transaction
        # around the next heartbeat savepoint and retain lease row locks.
        with self._connection.transaction():
            row = self._connection.execute(
                """SELECT j.cancel_requested_at IS NOT NULL
                   FROM jobs j JOIN job_attempts a ON a.id=j.current_attempt_id
                   JOIN workers w ON w.id=a.worker_id
                   WHERE j.id=%s AND a.id=%s AND a.worker_id=%s AND a.lease_epoch=%s
                     AND j.lease_epoch=a.lease_epoch AND j.state='running' AND a.state='active'
                     AND w.state='busy' AND a.lease_expires_at>clock_timestamp()""",
                (lease.job_id, lease.attempt_id, lease.worker_id, lease.lease_epoch),
            ).fetchone()
        if row is None:
            raise StateTransitionError("lease is no longer current")
        return bool(row[0])

    def load_ready_deployment(self, lease: Lease) -> DeploymentRecord:
        """Fenced DB deployment proof for one worker/job lease."""
        with self._connection.transaction():
            self._lock_current(lease, require_unexpired=True)
            row = self._connection.execute(
                """SELECT d.id,d.name,d.adapter_id,d.fingerprint,d.manifest,d.state
                   FROM jobs j JOIN workers w ON w.id=%s
                   JOIN model_deployments d ON d.id=j.model_deployment_id
                   WHERE j.id=%s AND j.current_attempt_id=%s
                     AND w.model_deployment_id=j.model_deployment_id AND d.state='ready'""",
                (lease.worker_id, lease.job_id, lease.attempt_id),
            ).fetchone()
            if row is None:
                raise StateTransitionError("worker/job deployment is not READY and identical")
            self.assert_current(lease)
            return DeploymentRecord(
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
                DeploymentState.READY,
                dict(row[4]),
            )

    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        _positive_int(lease_seconds, "lease_seconds")
        with self._connection.transaction():
            # A heartbeat already owns one exact lease identity.  It must wait
            # behind a short publication CAS in the same lock order, not treat
            # transient SKIP LOCKED contention as permanent lease loss.
            self._lock_current(lease, require_unexpired=True, skip_locked=False)
            row = self._connection.execute(
                """UPDATE job_attempts a SET heartbeat_at=clock_timestamp(),
                   lease_expires_at=clock_timestamp()+(%s * interval '1 second')
                   FROM jobs j, workers w
                   WHERE a.id=%s AND a.job_id=%s AND a.worker_id=%s AND a.lease_epoch=%s
                     AND a.job_id=j.id AND a.worker_id=w.id AND a.state='active'
                     AND a.lease_expires_at>clock_timestamp() AND j.state='running'
                     AND j.current_attempt_id=a.id AND j.lease_epoch=a.lease_epoch
                     AND j.cancel_requested_at IS NULL AND w.state='busy'
                   RETURNING a.lease_expires_at""",
                (lease_seconds, lease.attempt_id, lease.job_id, lease.worker_id, lease.lease_epoch),
            ).fetchone()
            if row is None:
                raise StateTransitionError("lease is no longer current")
            self._connection.execute(
                "UPDATE workers SET last_heartbeat_at=clock_timestamp() WHERE id=%s AND state='busy'",
                (lease.worker_id,),
            )
            return Lease(lease.job_id, lease.attempt_id, lease.worker_id, lease.lease_epoch, row[0])

    def request_cancel(self, job_id: str) -> bool:
        """Request cancellation once. Queued jobs become terminal; running jobs fence I/O."""

        with self._connection.transaction():
            row = self._connection.execute(
                "SELECT state,current_attempt_id FROM jobs WHERE id=%s FOR UPDATE", (job_id,)
            ).fetchone()
            if row is None or row[0] not in ("queued", "running"):
                return False
            changed = self._connection.execute(
                """UPDATE jobs SET state=CASE WHEN state='queued' THEN 'cancelled'::job_state ELSE state END,
                   cancel_requested_at=clock_timestamp(),updated_at=clock_timestamp()
                   WHERE id=%s AND cancel_requested_at IS NULL""",
                (job_id,),
            ).rowcount
            if changed:
                self._event(job_id, str(row[1]) if row[1] else None, "cancel_requested")
            return bool(changed)

    def cancel_current(self, lease: Lease) -> None:
        """Terminalize a current cancellation; a stale worker cannot change anything."""

        self._finish_lease(lease, cancelled=True, retry_delay_seconds=0, max_attempts=1)

    def fail_retryable(
        self, lease: Lease, retry_delay_seconds: int = 1, max_attempts: int = 3
    ) -> None:
        _positive_int(retry_delay_seconds, "retry_delay_seconds")
        _positive_int(max_attempts, "max_attempts")
        self._finish_lease(
            lease,
            cancelled=False,
            retry_delay_seconds=retry_delay_seconds,
            max_attempts=max_attempts,
        )

    def _finish_lease(
        self, lease: Lease, *, cancelled: bool, retry_delay_seconds: int, max_attempts: int
    ) -> None:
        with self._connection.transaction():
            attempt = self._lock_current(lease, require_unexpired=True)
            requested = self._connection.execute(
                "SELECT cancel_requested_at FROM jobs WHERE id=%s", (lease.job_id,)
            ).fetchone()[0]
            if cancelled and requested is None:
                raise StateTransitionError("cancellation was not requested")
            terminal = (
                "cancelled"
                if cancelled or requested is not None
                else ("failed_terminal" if int(attempt[0]) >= max_attempts else "failed_retryable")
            )
            job_state = (
                "cancelled"
                if terminal == "cancelled"
                else ("failed" if terminal == "failed_terminal" else "queued")
            )
            self._connection.execute(
                """UPDATE job_attempts SET state=%s,finished_at=clock_timestamp(),
                   heartbeat_at=NULL,lease_expires_at=NULL,failure_reason=%s WHERE id=%s""",
                (
                    terminal,
                    "cancelled" if terminal == "cancelled" else "worker_failure",
                    lease.attempt_id,
                ),
            )
            self._connection.execute(
                "UPDATE workers SET state='ready' WHERE id=%s", (lease.worker_id,)
            )
            if job_state == "queued":
                self._connection.execute(
                    """UPDATE jobs SET state='queued',current_attempt_id=NULL,
                       retry_not_before=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp()
                       WHERE id=%s AND current_attempt_id=%s""",
                    (retry_delay_seconds, lease.job_id, lease.attempt_id),
                )
                self._event(lease.job_id, lease.attempt_id, "attempt_requeued")
            else:
                self._connection.execute(
                    """UPDATE jobs SET state=%s,current_attempt_id=NULL,updated_at=clock_timestamp()
                       WHERE id=%s AND current_attempt_id=%s""",
                    (job_state, lease.job_id, lease.attempt_id),
                )
                self._event(
                    lease.job_id,
                    lease.attempt_id,
                    "attempt_cancelled" if terminal == "cancelled" else "attempt_failed",
                )

    def reap_expired(
        self, limit: int = 100, retry_delay_seconds: int = 1, max_attempts: int = 3
    ) -> int:
        _positive_int(limit, "limit")
        _positive_int(retry_delay_seconds, "retry_delay_seconds")
        _positive_int(max_attempts, "max_attempts")
        reaped = 0
        with self._connection.transaction():
            rows = self._connection.execute(
                """SELECT a.id,a.job_id,a.worker_id,a.lease_epoch,a.attempt_no
                   FROM job_attempts a JOIN jobs j ON j.id=a.job_id
                   WHERE a.state='active' AND a.lease_expires_at <= clock_timestamp()
                     AND j.current_attempt_id=a.id
                   ORDER BY a.lease_expires_at LIMIT %s""",
                (limit,),
            ).fetchall()
            for attempt_id, job_id, worker_id, epoch, _attempt_no in rows:
                lease = Lease(
                    str(job_id), str(attempt_id), str(worker_id), int(epoch), datetime.min
                )
                # A second reaper may have selected this candidate before this
                # transaction acquired the worker-first lock sequence. It must
                # simply observe the new epoch/terminal state and move on.
                try:
                    current = self._lock_current(lease, require_unexpired=False)
                except StateTransitionError:
                    continue
                still_expired = self._connection.execute(
                    "SELECT 1 FROM job_attempts WHERE id=%s AND lease_expires_at <= clock_timestamp()",
                    (attempt_id,),
                ).fetchone()
                if still_expired is None:
                    continue
                cancelled = (
                    self._connection.execute(
                        "SELECT cancel_requested_at FROM jobs WHERE id=%s", (job_id,)
                    ).fetchone()[0]
                    is not None
                )
                terminal = (
                    "cancelled"
                    if cancelled
                    else (
                        "failed_terminal" if int(current[0]) >= max_attempts else "failed_retryable"
                    )
                )
                job_state = (
                    "cancelled"
                    if cancelled
                    else ("failed" if terminal == "failed_terminal" else "queued")
                )
                self._connection.execute(
                    """UPDATE job_attempts SET state=%s,finished_at=clock_timestamp(),heartbeat_at=NULL,
                       lease_expires_at=NULL,failure_reason='lease_expired' WHERE id=%s AND state='active'""",
                    (terminal, attempt_id),
                )
                self._connection.execute(
                    "UPDATE workers SET state='ready' WHERE id=%s", (worker_id,)
                )
                if job_state == "queued":
                    self._connection.execute(
                        """UPDATE jobs SET state='queued',current_attempt_id=NULL,
                           retry_not_before=clock_timestamp()+(%s * interval '1 second'),updated_at=clock_timestamp()
                           WHERE id=%s AND current_attempt_id=%s""",
                        (retry_delay_seconds, job_id, attempt_id),
                    )
                    self._event(str(job_id), str(attempt_id), "attempt_requeued")
                else:
                    self._connection.execute(
                        "UPDATE jobs SET state=%s,current_attempt_id=NULL,updated_at=clock_timestamp() WHERE id=%s AND current_attempt_id=%s",
                        (job_state, job_id, attempt_id),
                    )
                    self._event(
                        str(job_id),
                        str(attempt_id),
                        "attempt_cancelled" if cancelled else "attempt_failed",
                    )
                reaped += 1
        return reaped

    def _lock_current(
        self, lease: Lease, *, require_unexpired: bool, skip_locked: bool = True
    ) -> Any:
        # Keep all leased mutation paths in worker -> job -> attempt order.
        lock = "FOR UPDATE SKIP LOCKED" if skip_locked else "FOR UPDATE"
        worker = self._connection.execute(
            "SELECT id FROM workers WHERE id=%s AND state='busy' " + lock,
            (lease.worker_id,),
        ).fetchone()
        if worker is None:
            raise StateTransitionError("lease is no longer current")
        job = self._connection.execute(
            "SELECT id FROM jobs WHERE id=%s AND state='running' " + lock,
            (lease.job_id,),
        ).fetchone()
        if job is None:
            raise StateTransitionError("lease is no longer current")
        extra = "AND a.lease_expires_at>clock_timestamp()" if require_unexpired else ""
        row = self._connection.execute(
            """SELECT a.attempt_no FROM job_attempts a JOIN jobs j ON j.id=a.job_id
               JOIN workers w ON w.id=a.worker_id
               WHERE a.id=%s AND a.job_id=%s AND a.worker_id=%s AND a.lease_epoch=%s
                 AND a.state='active' AND j.state='running' AND j.current_attempt_id=a.id
                 AND j.lease_epoch=a.lease_epoch AND w.state='busy' {0}
               FOR UPDATE OF a {1}""".format(extra, "SKIP LOCKED" if skip_locked else ""),
            (lease.attempt_id, lease.job_id, lease.worker_id, lease.lease_epoch),
        ).fetchone()
        if row is None:
            raise StateTransitionError("lease is no longer current")
        return row

    def _event(self, job_id: str, attempt_id: Optional[str], event_type: str) -> None:
        self._connection.execute(
            "INSERT INTO job_events(job_id,attempt_id,event_type) VALUES (%s,%s,%s)",
            (job_id, attempt_id, event_type),
        )
