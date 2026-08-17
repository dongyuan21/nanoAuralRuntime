"""Repository contract and deterministic in-memory implementation for Phase 3A.

The in-memory repository is deliberately narrow: it proves domain invariants in
CPU tests. It is not a replacement for PostgreSQL transactions or Phase 3C
claim/lease SQL.
"""

from __future__ import annotations

from dataclasses import replace
from threading import RLock
from typing import Dict, List, Mapping, Optional, Protocol, Sequence, Tuple
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
    JobEventRecord,
    JobInput,
    JobRecord,
    JobState,
    WorkerRecord,
    WorkerState,
    canonical_request_sha256,
)
from .errors import (
    DurableInvariantError,
    IdempotencyConflictError,
    NotFoundError,
    StateTransitionError,
)


class DurableRepository(Protocol):
    """Persistence boundary used by the Phase 3A fake worker."""

    def create_job(
        self,
        namespace_id: str,
        idempotency_key: str,
        request: Mapping[str, object],
        deployment_id: str,
        inputs: Sequence[JobInput],
        required_artifact_kinds: Sequence[ArtifactKind] = (ArtifactKind.OUTPUT,),
    ) -> JobRecord: ...

    def start_attempt(self, job_id: str, worker_id: str) -> AttemptRecord: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def register_blob(self, blob: BlobRecord) -> BlobRecord: ...

    def request_cancel(self, job_id: str) -> JobRecord: ...

    def cancel_attempt(self, job_id: str, attempt_id: str) -> JobRecord: ...

    def fail_attempt(self, job_id: str, attempt_id: str) -> JobRecord: ...

    def retry_attempt(self, job_id: str, attempt_id: str) -> JobRecord: ...

    def publish_success(
        self, job_id: str, attempt_id: str, artifacts: Sequence[ArtifactRecord]
    ) -> JobRecord: ...


class InMemoryDurableRepository:
    """Thread-safe invariant model used only by unit/integration tests.

    Production uses :class:`PostgresDurableRepository`; this deterministic
    implementation intentionally does not emulate
    `FOR UPDATE SKIP LOCKED`, heartbeat, lease expiry, or cross-process
    transactional guarantees; those belong to Phase 3C.
    """

    def __init__(self) -> None:
        self._lock = RLock()
        self._deployments: Dict[str, DeploymentRecord] = {}
        self._blobs: Dict[str, BlobRecord] = {}
        self._blob_by_digest: Dict[str, str] = {}
        self._assets: Dict[str, AssetRecord] = {}
        self._jobs: Dict[str, JobRecord] = {}
        self._job_by_idempotency: Dict[Tuple[str, str], str] = {}
        self._attempts: Dict[str, AttemptRecord] = {}
        self._attempts_by_job: Dict[str, Tuple[str, ...]] = {}
        self._artifacts: Dict[str, ArtifactRecord] = {}
        self._artifact_by_attempt_kind: Dict[Tuple[str, ArtifactKind], str] = {}
        self._workers: Dict[str, WorkerRecord] = {}
        self.events: List[JobEventRecord] = []

    @staticmethod
    def _id(prefix: str) -> str:
        return "{0}_{1}".format(prefix, uuid4().hex)

    def register_deployment(self, deployment: DeploymentRecord) -> DeploymentRecord:
        with self._lock:
            if deployment.deployment_id in self._deployments:
                raise DurableInvariantError("deployment already exists")
            self._deployments[deployment.deployment_id] = deployment
            return deployment

    def register_worker(self, worker: WorkerRecord) -> WorkerRecord:
        with self._lock:
            self._require_deployment(worker.deployment_id)
            if worker.worker_id in self._workers:
                raise DurableInvariantError("worker already exists")
            self._workers[worker.worker_id] = worker
            return worker

    def register_blob(self, blob: BlobRecord) -> BlobRecord:
        with self._lock:
            existing_id = self._blob_by_digest.get(blob.sha256)
            if existing_id is not None:
                existing = self._blobs[existing_id]
                if (
                    existing.size_bytes != blob.size_bytes
                    or existing.storage_key != blob.storage_key
                    or existing.state != blob.state
                    or existing.content_type != blob.content_type
                ):
                    raise DurableInvariantError(
                        "SHA-256 identity cannot map to different blob metadata"
                    )
                return existing
            if blob.blob_id in self._blobs:
                raise DurableInvariantError("blob id already exists")
            self._blobs[blob.blob_id] = blob
            self._blob_by_digest[blob.sha256] = blob.blob_id
            return blob

    def create_asset(self, asset: AssetRecord) -> AssetRecord:
        with self._lock:
            blob = self._require_blob(asset.blob_id)
            if asset.asset_id in self._assets:
                raise DurableInvariantError("asset already exists")
            if asset.state == AssetState.VERIFIED and blob.state != BlobState.VERIFIED:
                raise DurableInvariantError("a VERIFIED asset requires a VERIFIED blob")
            self._assets[asset.asset_id] = asset
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
        key = (namespace_id, idempotency_key)
        with self._lock:
            existing_id = self._job_by_idempotency.get(key)
            if existing_id is not None:
                existing = self._jobs[existing_id]
                if existing.request_sha256 != request_hash:
                    raise IdempotencyConflictError(
                        "idempotency key was already used for a different request"
                    )
                return existing
            deployment = self._require_deployment(deployment_id)
            if deployment.state != DeploymentState.READY:
                raise StateTransitionError("jobs require a READY deployment")
            if len({item.role for item in normalized_inputs}) != len(normalized_inputs):
                raise DurableInvariantError("job input roles must be unique")
            for item in normalized_inputs:
                asset = self._require_asset(item.asset_id)
                if asset.namespace_id != namespace_id:
                    raise DurableInvariantError("job input asset belongs to another namespace")
                if asset.state != AssetState.VERIFIED:
                    raise DurableInvariantError("only VERIFIED assets may enter a job")
                if self._require_blob(asset.blob_id).state != BlobState.VERIFIED:
                    raise DurableInvariantError("only verified blob content may enter a job")
            job = JobRecord(
                job_id=self._id("job"),
                namespace_id=namespace_id,
                idempotency_key=idempotency_key,
                request_sha256=request_hash,
                request=request,
                deployment_id=deployment_id,
                inputs=normalized_inputs,
                required_artifact_kinds=required,
            )
            self._jobs[job.job_id] = job
            self._job_by_idempotency[key] = job.job_id
            self._event(job.job_id, EventType.JOB_CREATED)
            return job

    def get_job(self, job_id: str) -> JobRecord:
        with self._lock:
            return self._require_job(job_id)

    def get_attempt(self, attempt_id: str) -> AttemptRecord:
        with self._lock:
            try:
                return self._attempts[attempt_id]
            except KeyError as error:
                raise NotFoundError("attempt not found: {0}".format(attempt_id)) from error

    def list_artifacts(self, job_id: str) -> Tuple[ArtifactRecord, ...]:
        with self._lock:
            self._require_job(job_id)
            return tuple(
                artifact for artifact in self._artifacts.values() if artifact.job_id == job_id
            )

    def start_attempt(self, job_id: str, worker_id: str) -> AttemptRecord:
        """Start one active attempt; Phase 3C replaces this with lease claim SQL."""

        with self._lock:
            job = self._require_job(job_id)
            worker = self._require_worker(worker_id)
            if worker.state != WorkerState.READY:
                raise StateTransitionError("worker is not READY")
            if worker.deployment_id != job.deployment_id:
                raise DurableInvariantError("worker deployment does not match job deployment")
            if job.state != JobState.QUEUED or job.current_attempt_id is not None:
                raise StateTransitionError("only a queued job without an attempt can start")
            if job.cancel_requested:
                raise StateTransitionError("cancelled job cannot start")
            attempt_no = len(self._attempts_by_job.get(job_id, ())) + 1
            attempt = AttemptRecord(
                attempt_id=self._id("attempt"),
                job_id=job_id,
                worker_id=worker_id,
                attempt_no=attempt_no,
                lease_epoch=job.lease_epoch + 1,
            )
            self._attempts[attempt.attempt_id] = attempt
            self._attempts_by_job[job_id] = self._attempts_by_job.get(job_id, ()) + (
                attempt.attempt_id,
            )
            self._jobs[job_id] = replace(
                job,
                state=JobState.RUNNING,
                current_attempt_id=attempt.attempt_id,
                lease_epoch=attempt.lease_epoch,
            )
            self._workers[worker_id] = replace(worker, state=WorkerState.BUSY)
            self._event(job_id, EventType.ATTEMPT_STARTED, attempt.attempt_id)
            return attempt

    def request_cancel(self, job_id: str) -> JobRecord:
        with self._lock:
            job = self._require_job(job_id)
            if job.state == JobState.QUEUED:
                job = replace(job, state=JobState.CANCELLED, cancel_requested=True)
                self._event(job_id, EventType.CANCEL_REQUESTED)
            elif job.state == JobState.RUNNING:
                job = replace(job, cancel_requested=True)
                self._event(job_id, EventType.CANCEL_REQUESTED, job.current_attempt_id)
            else:
                return job
            self._jobs[job_id] = job
            return job

    def cancel_attempt(self, job_id: str, attempt_id: str) -> JobRecord:
        with self._lock:
            job, attempt = self._active_attempt(job_id, attempt_id)
            self._attempts[attempt_id] = replace(attempt, state=AttemptState.CANCELLED)
            self._workers[attempt.worker_id] = replace(
                self._require_worker(attempt.worker_id), state=WorkerState.READY
            )
            updated = replace(
                job,
                state=JobState.CANCELLED,
                current_attempt_id=None,
                cancel_requested=True,
            )
            self._jobs[job_id] = updated
            self._event(job_id, EventType.ATTEMPT_CANCELLED, attempt_id)
            return updated

    def fail_attempt(self, job_id: str, attempt_id: str) -> JobRecord:
        with self._lock:
            job, attempt = self._active_attempt(job_id, attempt_id)
            self._attempts[attempt_id] = replace(attempt, state=AttemptState.FAILED_TERMINAL)
            self._workers[attempt.worker_id] = replace(
                self._require_worker(attempt.worker_id), state=WorkerState.READY
            )
            updated = replace(job, state=JobState.FAILED, current_attempt_id=None)
            self._jobs[job_id] = updated
            self._event(job_id, EventType.ATTEMPT_FAILED, attempt_id)
            return updated

    def retry_attempt(self, job_id: str, attempt_id: str) -> JobRecord:
        """Deterministically requeue a retryable failure without lease semantics.

        This is intentionally not Phase 3C claim/reaper logic: it records a
        terminal outcome for one attempt and permits a new explicit attempt.
        """

        with self._lock:
            job, attempt = self._active_attempt(job_id, attempt_id)
            self._attempts[attempt_id] = replace(attempt, state=AttemptState.FAILED_RETRYABLE)
            self._workers[attempt.worker_id] = replace(
                self._require_worker(attempt.worker_id), state=WorkerState.READY
            )
            updated = replace(job, state=JobState.QUEUED, current_attempt_id=None)
            self._jobs[job_id] = updated
            self._event(job_id, EventType.ATTEMPT_REQUEUED, attempt_id)
            return updated

    def publish_success(
        self, job_id: str, attempt_id: str, artifacts: Sequence[ArtifactRecord]
    ) -> JobRecord:
        with self._lock:
            job, attempt = self._active_attempt(job_id, attempt_id)
            if job.cancel_requested:
                raise StateTransitionError("cancelled job cannot publish success")
            proposed = tuple(artifacts)
            if not proposed:
                raise DurableInvariantError("successful job requires at least one artifact")
            kinds = set()
            for artifact in proposed:
                if not isinstance(artifact, ArtifactRecord):
                    raise TypeError("artifacts must contain ArtifactRecord")
                if artifact.job_id != job_id or artifact.attempt_id != attempt_id:
                    raise DurableInvariantError("artifact does not belong to active attempt")
                if artifact.state != ArtifactState.READY:
                    raise DurableInvariantError("successful job requires READY artifacts")
                if self._require_blob(artifact.blob_id).state != BlobState.VERIFIED:
                    raise DurableInvariantError("READY artifact requires a VERIFIED blob")
                if (
                    artifact.kind in kinds
                    or (attempt_id, artifact.kind) in self._artifact_by_attempt_kind
                ):
                    raise DurableInvariantError("attempt cannot publish duplicate artifact kinds")
                kinds.add(artifact.kind)
            missing = set(job.required_artifact_kinds) - kinds
            if missing:
                raise DurableInvariantError("successful job is missing required READY artifacts")
            for artifact in proposed:
                if artifact.artifact_id in self._artifacts:
                    raise DurableInvariantError("artifact id already exists")
                self._artifacts[artifact.artifact_id] = artifact
                self._artifact_by_attempt_kind[(attempt_id, artifact.kind)] = artifact.artifact_id
            self._attempts[attempt_id] = replace(attempt, state=AttemptState.SUCCEEDED)
            self._workers[attempt.worker_id] = replace(
                self._require_worker(attempt.worker_id), state=WorkerState.READY
            )
            updated = replace(
                job,
                state=JobState.SUCCEEDED,
                current_attempt_id=None,
                winning_attempt_id=attempt_id,
            )
            self._jobs[job_id] = updated
            self._event(job_id, EventType.JOB_SUCCEEDED, attempt_id)
            return updated

    def _active_attempt(self, job_id: str, attempt_id: str) -> Tuple[JobRecord, AttemptRecord]:
        job = self._require_job(job_id)
        attempt = self.get_attempt(attempt_id)
        if (
            job.state != JobState.RUNNING
            or job.current_attempt_id != attempt_id
            or attempt.job_id != job_id
            or attempt.state != AttemptState.ACTIVE
            or attempt.lease_epoch != job.lease_epoch
        ):
            raise StateTransitionError("attempt is not the job's current active attempt")
        return job, attempt

    def _event(self, job_id: str, event_type: EventType, attempt_id: Optional[str] = None) -> None:
        self.events.append(
            JobEventRecord(self._id("event"), job_id, event_type, attempt_id=attempt_id)
        )

    def _require_deployment(self, deployment_id: str) -> DeploymentRecord:
        try:
            return self._deployments[deployment_id]
        except KeyError as error:
            raise NotFoundError("deployment not found: {0}".format(deployment_id)) from error

    def _require_blob(self, blob_id: str) -> BlobRecord:
        try:
            return self._blobs[blob_id]
        except KeyError as error:
            raise NotFoundError("blob not found: {0}".format(blob_id)) from error

    def _require_asset(self, asset_id: str) -> AssetRecord:
        try:
            return self._assets[asset_id]
        except KeyError as error:
            raise NotFoundError("asset not found: {0}".format(asset_id)) from error

    def _require_job(self, job_id: str) -> JobRecord:
        try:
            return self._jobs[job_id]
        except KeyError as error:
            raise NotFoundError("job not found: {0}".format(job_id)) from error

    def _require_worker(self, worker_id: str) -> WorkerRecord:
        try:
            return self._workers[worker_id]
        except KeyError as error:
            raise NotFoundError("worker not found: {0}".format(worker_id)) from error
