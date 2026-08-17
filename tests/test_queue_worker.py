from __future__ import annotations

import unittest
from datetime import datetime, timezone
from typing import Optional

from nano_aural_runtime.durable.errors import StateTransitionError
from nano_aural_runtime.durable.queue import Lease
from nano_aural_runtime.durable.queue_worker import FakeLeaseWorker


class _Queue:
    def __init__(self, lease: Optional[Lease]) -> None:
        self.lease = lease
        self.calls: list[str] = []
        self.reject_after_execute = False

    def claim_next(self, worker_id: str, lease_seconds: int) -> Optional[Lease]:
        self.calls.append("claim:{0}:{1}".format(worker_id, lease_seconds))
        return self.lease

    def assert_current(self, lease: Lease) -> None:
        self.calls.append("assert")
        if self.reject_after_execute and self.calls.count("assert") == 2:
            raise StateTransitionError("expired")

    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        self.calls.append("heartbeat:{0}".format(lease_seconds))
        return lease


def _lease() -> Lease:
    return Lease("job", "attempt", "worker", 1, datetime.now(timezone.utc))


class FakeLeaseWorkerTests(unittest.TestCase):
    def test_success_stays_as_memory_only_candidate(self) -> None:
        queue = _Queue(_lease())
        worker = FakeLeaseWorker(queue, "worker", 30, lambda lease: {"epoch": lease.lease_epoch})  # type: ignore[arg-type]
        candidate = worker.run_once()
        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual({"epoch": 1}, candidate.value)
        self.assertEqual(["claim:worker:30", "assert", "assert"], queue.calls)

    def test_returns_none_without_claim(self) -> None:
        queue = _Queue(None)
        worker = FakeLeaseWorker(queue, "worker", 30, lambda lease: lease)  # type: ignore[arg-type]
        self.assertIsNone(worker.run_once())
        self.assertEqual(["claim:worker:30"], queue.calls)

    def test_rejects_candidate_after_fence_loss(self) -> None:
        queue = _Queue(_lease())
        queue.reject_after_execute = True
        worker = FakeLeaseWorker(queue, "worker", 30, lambda lease: b"candidate")  # type: ignore[arg-type]
        with self.assertRaisesRegex(StateTransitionError, "expired"):
            worker.run_once()

    def test_cooperative_executor_can_renew_its_fenced_lease(self) -> None:
        queue = _Queue(_lease())
        worker = FakeLeaseWorker(queue, "worker", 30, lambda lease: b"unused")  # type: ignore[arg-type]
        result = worker.run_renewing_once(lambda lease, renew: (renew(), b"renewed")[1])
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(b"renewed", result.value)
        self.assertIn("heartbeat:30", queue.calls)
