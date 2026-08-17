"""PostgreSQL Phase 3A durable repository.

This is deliberately an explicit-command repository, not a queue claimer:
there is no ``SKIP LOCKED``, heartbeat, lease-expiry reaper, or recovery loop
here.  Those distributed-worker concerns belong to Phase 3C.
"""

from __future__ import annotations

import json
from typing import Any, Mapping, Optional, Sequence, Tuple, cast
from uuid import uuid4

from .domain import (
    ArtifactKind,
    ArtifactRecord,
    ArtifactState,
    AssetRecord,
    AssetState,
    AttemptRecord,
    AttemptState,
    BlobRecord,
    BlobState,
    DeploymentRecord,
    DeploymentState,
    EventType,
    JobInput,
    JobRecord,
    JobState,
    WorkerRecord,
    canonical_request_sha256,
)
from .errors import (
    DurableInvariantError,
    IdempotencyConflictError,
    NotFoundError,
    StateTransitionError,
)


class PostgresDurableRepository:
    """Production SQL implementation of the Phase 3A repository boundary.

    The caller supplies an already-migrated psycopg3 connection.  Each command
    commits its own database transaction, so rows and their domain event are
    atomically visible.  This class intentionally never creates schema.
    """

    def __init__(self, connection: Any) -> None:
        self._connection = connection

    @staticmethod
    def _id() -> str:
        return str(uuid4())

    def register_deployment(self, deployment: DeploymentRecord) -> DeploymentRecord:
        with self._connection.transaction():
            self._connection.execute(
                """INSERT INTO model_deployments
                   (id,name,adapter_id,fingerprint,manifest,state) VALUES (%s,%s,%s,%s,%s::jsonb,%s)""",
                (
                    deployment.deployment_id,
                    deployment.name,
                    deployment.adapter_id,
                    deployment.fingerprint,
                    json.dumps(dict(deployment.manifest), sort_keys=True),
                    deployment.state.value,
                ),
            )
        return deployment

    def register_worker(self, worker: WorkerRecord) -> WorkerRecord:
        with self._connection.transaction():
            self._connection.execute(
                "INSERT INTO workers (id,model_deployment_id,state) VALUES (%s,%s,%s)",
                (worker.worker_id, worker.deployment_id, worker.state.value),
            )
        return worker

    def register_blob(self, blob: BlobRecord) -> BlobRecord:
        with self._connection.transaction():
            row = self._connection.execute(
                """INSERT INTO blobs (id,sha256,size_bytes,storage_key,content_type,state)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (sha256) DO NOTHING
                   RETURNING id,sha256,size_bytes,storage_key,content_type,state""",
                (
                    blob.blob_id,
                    blob.sha256,
                    blob.size_bytes,
                    blob.storage_key,
                    blob.content_type,
                    blob.state.value,
                ),
            ).fetchone()
            if row is None:
                row = self._connection.execute(
                    "SELECT id,sha256,size_bytes,storage_key,content_type,state FROM blobs WHERE sha256=%s",
                    (blob.sha256,),
                ).fetchone()
                if row is None or tuple(row[2:]) != (
                    blob.size_bytes,
                    blob.storage_key,
                    blob.content_type,
                    blob.state.value,
                ):
                    raise DurableInvariantError(
                        "SHA-256 identity cannot map to different blob metadata"
                    )
        return self._blob_from_row(row)

    def create_asset(self, asset: AssetRecord) -> AssetRecord:
        with self._connection.transaction():
            blob = self._connection.execute(
                "SELECT state FROM blobs WHERE id=%s", (asset.blob_id,)
            ).fetchone()
            if blob is None:
                raise NotFoundError("blob not found: {0}".format(asset.blob_id))
            if asset.state == AssetState.VERIFIED and blob[0] != BlobState.VERIFIED.value:
                raise DurableInvariantError("a VERIFIED asset requires a VERIFIED blob")
            self._connection.execute(
                """INSERT INTO assets (id,namespace_id,blob_id,kind,metadata,state)
                   VALUES (%s,%s,%s,%s,%s::jsonb,%s)""",
                (
                    asset.asset_id,
                    asset.namespace_id,
                    asset.blob_id,
                    asset.kind.value,
                    json.dumps(dict(asset.metadata), sort_keys=True),
                    asset.state.value,
                ),
            )
        return asset

    def create_job(
        self,
        namespace_id: str,
        idempotency_key: str,
        request: Mapping[str, object],
        deployment_id: str,
        inputs: Sequence[JobInput],
        required_artifact_kinds: Sequence[ArtifactKind] = (ArtifactKind.OUTPUT,),
    ) -> JobRecord:
        if not isinstance(namespace_id, str) or not namespace_id.strip():
            raise ValueError("namespace_id must be non-empty")
        if not isinstance(idempotency_key, str) or not idempotency_key.strip():
            raise ValueError("idempotency_key must be non-empty")
        normalized_inputs = tuple(sorted(tuple(inputs), key=lambda item: item.role))
        requested_kinds = tuple(required_artifact_kinds)
        if not requested_kinds or not all(
            isinstance(kind, ArtifactKind) for kind in requested_kinds
        ):
            raise ValueError("required_artifact_kinds must contain ArtifactKind values")
        if len(set(requested_kinds)) != len(requested_kinds):
            raise ValueError("required_artifact_kinds must be unique")
        required = tuple(sorted(requested_kinds, key=lambda kind: kind.value))
        request_hash = canonical_request_sha256(
            request,
            deployment_id=deployment_id,
            inputs=normalized_inputs,
            required_artifact_kinds=required,
        )
        if len({item.role for item in normalized_inputs}) != len(normalized_inputs):
            raise DurableInvariantError("job input roles must be unique")
        job_id = self._id()
        with self._connection.transaction():
            deployment = self._connection.execute(
                "SELECT state FROM model_deployments WHERE id=%s", (deployment_id,)
            ).fetchone()
            if deployment is None:
                raise NotFoundError("deployment not found: {0}".format(deployment_id))
            if deployment[0] != DeploymentState.READY.value:
                raise StateTransitionError("jobs require a READY deployment")
            row = self._connection.execute(
                """INSERT INTO jobs (id,namespace_id,idempotency_key,request_sha256,request_json,
                       model_deployment_id,required_artifact_kinds)
                   VALUES (%s,%s,%s,%s,%s::jsonb,%s,%s)
                   ON CONFLICT (namespace_id,idempotency_key) DO NOTHING
                   RETURNING id""",
                (
                    job_id,
                    namespace_id,
                    idempotency_key,
                    request_hash,
                    json.dumps(request, sort_keys=True),
                    deployment_id,
                    [kind.value for kind in required],
                ),
            ).fetchone()
            if row is None:
                existing = self._job_by_key(namespace_id, idempotency_key)
                if existing.request_sha256 != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was already used for a different request"
                    )
                return existing
            for item in normalized_inputs:
                self._connection.execute(
                    "INSERT INTO job_inputs (job_id,role,asset_id) VALUES (%s,%s,%s)",
                    (job_id, item.role, item.asset_id),
                )
            self._event(job_id, EventType.JOB_CREATED)
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> JobRecord:
        row = self._connection.execute(
            """SELECT id,namespace_id,idempotency_key,request_sha256,request_json,model_deployment_id,
                      required_artifact_kinds,state,current_attempt_id,winning_attempt_id,cancel_requested_at,lease_epoch
               FROM jobs WHERE id=%s""",
            (job_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("job not found: {0}".format(job_id))
        inputs = self._connection.execute(
            "SELECT role,asset_id FROM job_inputs WHERE job_id=%s ORDER BY role", (job_id,)
        ).fetchall()
        return JobRecord(
            job_id=str(row[0]),
            namespace_id=row[1],
            idempotency_key=row[2],
            request_sha256=row[3],
            request=row[4],
            deployment_id=str(row[5]),
            inputs=tuple(JobInput(role=item[0], asset_id=str(item[1])) for item in inputs),
            required_artifact_kinds=tuple(ArtifactKind(item) for item in row[6]),
            state=JobState(row[7]),
            current_attempt_id=str(row[8]) if row[8] else None,
            winning_attempt_id=str(row[9]) if row[9] else None,
            cancel_requested=row[10] is not None,
            lease_epoch=row[11],
        )

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        row = self._connection.execute(
            "SELECT id,job_id,worker_id,attempt_no,lease_epoch,state FROM job_attempts WHERE id=%s",
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise NotFoundError("attempt not found: {0}".format(attempt_id))
        return AttemptRecord(
            str(row[0]), str(row[1]), str(row[2]), row[3], row[4], AttemptState(row[5])
        )

    def list_artifacts(self, job_id: str) -> Tuple[ArtifactRecord, ...]:
        rows = self._connection.execute(
            "SELECT id,job_id,attempt_id,blob_id,kind,state,metadata FROM artifacts WHERE job_id=%s",
            (job_id,),
        ).fetchall()
        return tuple(self._artifact_from_row(row) for row in rows)

    def start_attempt(self, job_id: str, worker_id: str) -> AttemptRecord:
        raise StateTransitionError("legacy execution mutators are disabled; use PostgresLeaseQueue")

    def publish_success(
        self, job_id: str, attempt_id: str, artifacts: Sequence[ArtifactRecord]
    ) -> JobRecord:
        raise StateTransitionError(
            "legacy execution mutators are disabled; Phase 3E owns publication"
        )

    def fail_attempt(self, job_id: str, attempt_id: str) -> JobRecord:
        raise StateTransitionError("legacy execution mutators are disabled; use PostgresLeaseQueue")

    def retry_attempt(self, job_id: str, attempt_id: str) -> JobRecord:
        """Record a retryable outcome and explicitly requeue; no 3C claim logic."""

        raise StateTransitionError("legacy execution mutators are disabled; use PostgresLeaseQueue")

    def request_cancel(self, job_id: str) -> JobRecord:
        with self._connection.transaction():
            job = self._locked_job(job_id)
            if job.state == JobState.QUEUED:
                self._connection.execute(
                    "UPDATE jobs SET state='cancelled',cancel_requested_at=now() WHERE id=%s",
                    (job_id,),
                )
                self._event(job_id, EventType.CANCEL_REQUESTED)
            elif job.state == JobState.RUNNING:
                self._connection.execute(
                    "UPDATE jobs SET cancel_requested_at=now() WHERE id=%s", (job_id,)
                )
                self._event(job_id, EventType.CANCEL_REQUESTED, job.current_attempt_id)
        return self.get_job(job_id)

    def cancel_attempt(self, job_id: str, attempt_id: str) -> JobRecord:
        raise StateTransitionError("legacy execution mutators are disabled; use PostgresLeaseQueue")

    def _finish_non_success(
        self,
        job_id: str,
        attempt_id: str,
        attempt_state: str,
        job_state: JobState,
        event: EventType,
    ) -> JobRecord:
        raise StateTransitionError("legacy execution mutators are disabled; use PostgresLeaseQueue")

    def _locked_job(self, job_id: str) -> JobRecord:
        self._connection.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (job_id,)).fetchone()
        return self.get_job(job_id)

    def _active_attempt(self, job_id: str, attempt_id: str) -> Tuple[JobRecord, AttemptRecord]:
        raise StateTransitionError("legacy execution mutators are disabled; use PostgresLeaseQueue")

    def _job_by_key(self, namespace_id: str, idempotency_key: str) -> JobRecord:
        row = self._connection.execute(
            "SELECT id FROM jobs WHERE namespace_id=%s AND idempotency_key=%s",
            (namespace_id, idempotency_key),
        ).fetchone()
        if row is None:
            raise NotFoundError("idempotent job disappeared")
        return self.get_job(str(row[0]))

    def _event(self, job_id: str, event: EventType, attempt_id: Optional[str] = None) -> None:
        self._connection.execute(
            "INSERT INTO job_events (job_id,attempt_id,event_type) VALUES (%s,%s,%s)",
            (job_id, attempt_id, event.value),
        )

    @staticmethod
    def _blob_from_row(row: Sequence[object]) -> BlobRecord:
        return BlobRecord(
            str(row[0]),
            str(row[1]),
            cast(int, row[2]),
            str(row[3]),
            BlobState(str(row[5])),
            str(row[4]),
        )

    @staticmethod
    def _artifact_from_row(row: Sequence[object]) -> ArtifactRecord:
        return ArtifactRecord(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            ArtifactKind(str(row[4])),
            ArtifactState(str(row[5])),
            cast(Mapping[str, object], row[6]),
        )
