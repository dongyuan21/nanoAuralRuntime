"""CPU-only fake durable worker used to prove Phase 3A state invariants."""

from __future__ import annotations

import hashlib
from uuid import NAMESPACE_URL, uuid5

from .domain import ArtifactRecord, BlobRecord, BlobState, JobState
from .repository import DurableRepository
from .storage import LocalBlobStore


class FakeRuntimeWorker:
    """Publishes a deterministic validated artifact without importing a model.

    It is not a queue consumer and does not model Phase 3C leases. Its role is
    to verify the Phase 3A path from already VERIFIED input assets through a
    ready artifact and one winning attempt.
    """

    def __init__(self, repository: DurableRepository, blob_store: LocalBlobStore) -> None:
        self._repository = repository
        self._blob_store = blob_store

    def execute(
        self,
        job_id: str,
        worker_id: str,
        fail: bool = False,
        retryable_fail: bool = False,
        cancel_before_publish: bool = False,
    ) -> JobState:
        attempt = self._repository.start_attempt(job_id, worker_id)
        if fail:
            return self._repository.fail_attempt(job_id, attempt.attempt_id).state
        if retryable_fail:
            return self._repository.retry_attempt(job_id, attempt.attempt_id).state
        if cancel_before_publish:
            self._repository.request_cancel(job_id)
            return self._repository.cancel_attempt(job_id, attempt.attempt_id).state
        job = self._repository.get_job(job_id)
        source = "|".join(sorted(item.asset_id for item in job.inputs)).encode("utf-8")
        content = b"fake-runtime-artifact:" + hashlib.sha256(source).digest()
        if not content:
            return self._repository.fail_attempt(job_id, attempt.attempt_id).state
        stored = self._blob_store.put_immutable(content)
        validated = self._blob_store.stat(stored.storage_key)
        if validated != stored:
            return self._repository.fail_attempt(job_id, attempt.attempt_id).state
        blob = self._repository.register_blob(
            BlobRecord(
                blob_id=str(uuid5(NAMESPACE_URL, "blob:" + stored.sha256)),
                sha256=stored.sha256,
                size_bytes=stored.size_bytes,
                storage_key=stored.storage_key,
                state=BlobState.VERIFIED,
            )
        )
        artifacts = tuple(
            ArtifactRecord(
                artifact_id=str(
                    uuid5(NAMESPACE_URL, "artifact:{0}:{1}".format(attempt.attempt_id, kind.value))
                ),
                job_id=job_id,
                attempt_id=attempt.attempt_id,
                blob_id=blob.blob_id,
                kind=kind,
            )
            for kind in job.required_artifact_kinds
        )
        return self._repository.publish_success(job_id, attempt.attempt_id, artifacts).state
