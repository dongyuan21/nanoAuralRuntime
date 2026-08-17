"""CPU-only Phase 3A durable-domain tests.

These exercise the deterministic in-memory repository. PostgreSQL DDL is
reviewed as the production authority; Phase 3C adds real claim/lease SQL tests.
"""

from __future__ import annotations

import os
import tempfile
import threading
import unittest
from pathlib import Path

from nano_aural_runtime.durable import (
    ArtifactKind,
    ArtifactRecord,
    AssetKind,
    AssetRecord,
    AssetState,
    AttemptState,
    BlobRecord,
    DeploymentRecord,
    DurableInvariantError,
    EventType,
    FakeRuntimeWorker,
    IdempotencyConflictError,
    InMemoryDurableRepository,
    JobInput,
    JobState,
    LocalBlobStore,
    StateTransitionError,
    WorkerRecord,
    canonical_request_sha256,
)


class DurableFoundationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.root = Path(self.tempdir.name) / "blobs"
        self.store = LocalBlobStore(self.root)
        self.repository = InMemoryDurableRepository()
        self.repository.register_deployment(DeploymentRecord("dep", "test", "fake", "fingerprint"))
        self.repository.register_worker(WorkerRecord("worker", "dep"))

    def tearDown(self) -> None:
        self.store.close()
        self.tempdir.cleanup()

    def _verified_asset(self, asset_id: str = "asset") -> AssetRecord:
        stored = self.store.put_immutable(b"verified input")
        blob = self.repository.register_blob(
            BlobRecord(
                "blob_" + stored.sha256, stored.sha256, stored.size_bytes, stored.storage_key
            )
        )
        asset = AssetRecord(asset_id, blob.blob_id, "ns", AssetKind.INPUT, AssetState.VERIFIED)
        return self.repository.create_asset(asset)

    def _job(self):
        asset = self._verified_asset()
        return self.repository.create_job(
            "ns",
            "same-key",
            {"task": "model-owned", "seed": 1},
            "dep",
            (JobInput("input", asset.asset_id),),
        )

    def test_canonical_hash_is_key_order_independent(self) -> None:
        self.assertEqual(
            canonical_request_sha256({"b": [2, 1], "a": {"x": True}}),
            canonical_request_sha256({"a": {"x": True}, "b": [2, 1]}),
        )

    def test_idempotency_returns_same_job_or_conflicts(self) -> None:
        job = self._job()
        same = self.repository.create_job(
            "ns", "same-key", {"seed": 1, "task": "model-owned"}, "dep", job.inputs
        )
        self.assertEqual(job.job_id, same.job_id)
        with self.assertRaises(IdempotencyConflictError):
            self.repository.create_job("ns", "same-key", {"task": "different"}, "dep", job.inputs)

    def test_zero_input_job_is_idempotent_and_prompt_changes_conflict(self) -> None:
        job = self.repository.create_job("ns", "text-key", {"prompt": "one"}, "dep", ())
        same = self.repository.create_job("ns", "text-key", {"prompt": "one"}, "dep", ())
        self.assertEqual((), job.inputs)
        self.assertEqual(job.job_id, same.job_id)
        with self.assertRaises(IdempotencyConflictError):
            self.repository.create_job("ns", "text-key", {"prompt": "two"}, "dep", ())

    def test_idempotency_covers_deployment_inputs_and_required_outputs(self) -> None:
        job = self._job()
        self.repository.register_deployment(
            DeploymentRecord("dep-two", "two", "fake", "fingerprint-two")
        )
        other_asset = self._verified_asset("other")
        variants = (
            ("dep-two", job.inputs, (ArtifactKind.OUTPUT,)),
            ("dep", (JobInput("input", other_asset.asset_id),), (ArtifactKind.OUTPUT,)),
            ("dep", job.inputs, (ArtifactKind.OUTPUT, ArtifactKind.MANIFEST)),
        )
        for deployment, inputs, required in variants:
            with self.assertRaises(IdempotencyConflictError):
                self.repository.create_job(
                    "ns",
                    "same-key",
                    {"task": "model-owned", "seed": 1},
                    deployment,
                    inputs,
                    required,
                )

    def test_idempotency_is_concurrent_and_input_order_independent(self) -> None:
        first = self._verified_asset("first")
        second = self._verified_asset("second")
        inputs = (JobInput("second", second.asset_id), JobInput("first", first.asset_id))
        created = []
        errors = []

        def submit(reverse: bool) -> None:
            try:
                selected = tuple(reversed(inputs)) if reverse else inputs
                created.append(
                    self.repository.create_job("ns", "concurrent", {"task": "x"}, "dep", selected)
                )
            except Exception as error:  # pragma: no cover - assertion below reports it
                errors.append(error)

        threads = [threading.Thread(target=submit, args=(index % 2 == 0,)) for index in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(1, len({job.job_id for job in created}))

    def test_only_verified_assets_may_enter_jobs(self) -> None:
        stored = self.store.put_immutable(b"unverified")
        blob = self.repository.register_blob(
            BlobRecord("blob_unverified", stored.sha256, stored.size_bytes, stored.storage_key)
        )
        rejected = self.repository.create_asset(
            AssetRecord("rejected", blob.blob_id, "ns", state=AssetState.REJECTED)
        )
        with self.assertRaises(DurableInvariantError):
            self.repository.create_job(
                "ns", "key", {"task": "x"}, "dep", (JobInput("input", rejected.asset_id),)
            )

    def test_only_one_current_attempt_is_allowed(self) -> None:
        job = self._job()
        first = self.repository.start_attempt(job.job_id, "worker")
        with self.assertRaises(StateTransitionError):
            self.repository.start_attempt(job.job_id, "worker")
        self.assertEqual(first.attempt_id, self.repository.get_job(job.job_id).current_attempt_id)

    def test_success_has_ready_winner_and_cannot_publish_twice(self) -> None:
        job = self._job()
        attempt = self.repository.start_attempt(job.job_id, "worker")
        stored = self.store.put_immutable(b"output")
        blob = self.repository.register_blob(
            BlobRecord("blob_output", stored.sha256, stored.size_bytes, stored.storage_key)
        )
        artifact = ArtifactRecord(
            "artifact", job.job_id, attempt.attempt_id, blob.blob_id, ArtifactKind.OUTPUT
        )
        result = self.repository.publish_success(job.job_id, attempt.attempt_id, (artifact,))
        self.assertEqual(JobState.SUCCEEDED, result.state)
        self.assertEqual(attempt.attempt_id, result.winning_attempt_id)
        self.assertIsNone(result.current_attempt_id)
        self.assertEqual(1, len(self.repository.list_artifacts(job.job_id)))
        with self.assertRaises(StateTransitionError):
            self.repository.publish_success(job.job_id, attempt.attempt_id, (artifact,))

    def test_success_rejects_non_ready_or_wrong_attempt_artifact(self) -> None:
        job = self._job()
        attempt = self.repository.start_attempt(job.job_id, "worker")
        stored = self.store.put_immutable(b"output")
        blob = self.repository.register_blob(
            BlobRecord("blob_output", stored.sha256, stored.size_bytes, stored.storage_key)
        )
        invalid = ArtifactRecord("artifact", job.job_id, "other", blob.blob_id)
        with self.assertRaises(DurableInvariantError):
            self.repository.publish_success(job.job_id, attempt.attempt_id, (invalid,))

    def test_success_requires_every_declared_artifact_kind(self) -> None:
        asset = self._verified_asset()
        job = self.repository.create_job(
            "ns",
            "two-artifacts",
            {"task": "x"},
            "dep",
            (JobInput("input", asset.asset_id),),
            (ArtifactKind.OUTPUT, ArtifactKind.MANIFEST),
        )
        attempt = self.repository.start_attempt(job.job_id, "worker")
        stored = self.store.put_immutable(b"output")
        blob = self.repository.register_blob(
            BlobRecord("blob_required", stored.sha256, stored.size_bytes, stored.storage_key)
        )
        output = ArtifactRecord("only-output", job.job_id, attempt.attempt_id, blob.blob_id)
        with self.assertRaises(DurableInvariantError):
            self.repository.publish_success(job.job_id, attempt.attempt_id, (output,))

    def test_fake_worker_success_failure_and_cancel_closed_loop(self) -> None:
        worker = FakeRuntimeWorker(self.repository, self.store)
        success = self._job()
        self.assertEqual(JobState.SUCCEEDED, worker.execute(success.job_id, "worker"))
        self.assertEqual(1, len(self.repository.list_artifacts(success.job_id)))

        failure = self._job_with_key("failure")
        self.assertEqual(JobState.FAILED, worker.execute(failure.job_id, "worker", fail=True))
        cancelled = self._job_with_key("cancel")
        self.assertEqual(
            JobState.CANCELLED,
            worker.execute(cancelled.job_id, "worker", cancel_before_publish=True),
        )

    def test_retryable_attempt_requeues_then_second_attempt_wins(self) -> None:
        job = self._job()
        first = self.repository.start_attempt(job.job_id, "worker")
        self.assertEqual(
            JobState.QUEUED, self.repository.retry_attempt(job.job_id, first.attempt_id).state
        )
        self.assertEqual(
            AttemptState.FAILED_RETRYABLE, self.repository.get_attempt(first.attempt_id).state
        )
        result = FakeRuntimeWorker(self.repository, self.store).execute(job.job_id, "worker")
        final = self.repository.get_job(job.job_id)
        self.assertEqual(JobState.SUCCEEDED, result)
        self.assertIsNotNone(final.winning_attempt_id)
        winning_attempt_id = final.winning_attempt_id
        assert winning_attempt_id is not None
        self.assertEqual(2, self.repository.get_attempt(winning_attempt_id).attempt_no)
        self.assertIn(
            EventType.ATTEMPT_REQUEUED, [event.event_type for event in self.repository.events]
        )

    def test_terminal_cancel_does_not_emit_misleading_event(self) -> None:
        job = self._job()
        FakeRuntimeWorker(self.repository, self.store).execute(job.job_id, "worker")
        event_count = len(self.repository.events)
        self.assertEqual(JobState.SUCCEEDED, self.repository.request_cancel(job.job_id).state)
        self.assertEqual(event_count, len(self.repository.events))

    def _job_with_key(self, key: str):
        asset = self._verified_asset("asset_" + key)
        return self.repository.create_job(
            "ns",
            key,
            {"task": "model-owned", "key": key},
            "dep",
            (JobInput("input", asset.asset_id),),
        )

    def test_blob_store_is_immutable_and_rejects_traversal(self) -> None:
        stored = self.store.put_immutable(b"same bytes")
        self.assertEqual(stored, self.store.put_immutable(b"same bytes"))
        with self.store.open_reader(stored.storage_key) as stream:
            self.assertEqual(b"same bytes", stream.read())
        self.assertEqual(stored, self.store.stat(stored.storage_key))
        for key in (
            "../outside",
            "/tmp/outside",
            "blobs/sha256/../outside",
            stored.storage_key.upper(),
            stored.storage_key.replace("/", "\\"),
            stored.storage_key + "/.",
            stored.storage_key + "\x00",
        ):
            with self.assertRaises(ValueError):
                self.store.open_reader(key)

    def test_blob_store_refuses_existing_symlink_escape_and_noncanonical_delete(self) -> None:
        stored = self.store.put_immutable(b"safe")
        target = self.root / stored.storage_key
        target.unlink()
        outside = Path(self.tempdir.name) / "outside"
        outside.write_bytes(b"outside")
        os.symlink(outside, target)
        with self.assertRaises(OSError):
            self.store.open_reader(stored.storage_key)
        with self.assertRaises(RuntimeError):
            self.store.delete(stored.storage_key)
        with self.assertRaises(ValueError):
            self.store.delete(stored.storage_key.upper())

    def test_concurrent_same_bytes_put_is_immutable(self) -> None:
        outputs = []
        errors = []

        def put() -> None:
            try:
                outputs.append(self.store.put_immutable(b"concurrent"))
            except Exception as error:  # pragma: no cover - assertion below reports it
                errors.append(error)

        threads = [threading.Thread(target=put) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertFalse(errors)
        self.assertEqual(1, len(set(outputs)))

    def test_sha_identity_cannot_be_rebound(self) -> None:
        stored = self.store.put_immutable(b"identity")
        self.repository.register_blob(
            BlobRecord("blob_one", stored.sha256, stored.size_bytes, stored.storage_key)
        )
        with self.assertRaises(DurableInvariantError):
            self.repository.register_blob(
                BlobRecord("blob_two", stored.sha256, stored.size_bytes + 1, stored.storage_key)
            )


if __name__ == "__main__":
    unittest.main()
