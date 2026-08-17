"""Phase 3C fake queue worker, deliberately without Runtime or publication."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Generic, Optional, TypeVar, cast

from .materialization import LeaseAuthority, MaterializedInput
from .queue import Lease, PostgresLeaseQueue
from .upload_transports import CanonicalBlobStore
from .uploads import MediaProbe

Candidate = TypeVar("Candidate")


@dataclass(frozen=True)
class InMemoryCandidate(Generic[Candidate]):
    """A fenced, process-local result.  It is not a durable artifact."""

    lease: Lease
    value: Candidate


class FakeLeaseWorker(Generic[Candidate]):
    """Run injected CPU work only while the lease remains fenced/current.

    The executor has no durable repository and this class intentionally leaves
    the attempt ACTIVE after a result.  If the process exits before Phase 3E,
    the reaper makes the attempt retryable.
    """

    def __init__(
        self,
        queue: PostgresLeaseQueue,
        worker_id: str,
        lease_seconds: int,
        executor: Callable[[Lease], Candidate],
    ) -> None:
        self._queue = queue
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._executor = executor

    def run_once(self) -> Optional[InMemoryCandidate[Candidate]]:
        lease = self._queue.claim_next(self._worker_id, self._lease_seconds)
        if lease is None:
            return None
        self._queue.assert_current(lease)
        value = self._executor(lease)
        self._queue.assert_current(lease)
        return InMemoryCandidate(lease, cast(Candidate, value))

    def run_renewing_once(
        self, executor: Callable[[Lease, Callable[[], Lease]], Candidate]
    ) -> Optional[InMemoryCandidate[Candidate]]:
        """Run cooperative CPU work with an injected, fenced heartbeat cadence.

        The executor decides its safe polling cadence; each renewal uses the
        DB clock and raises immediately if cancellation or reaping won the race.
        """

        lease = self._queue.claim_next(self._worker_id, self._lease_seconds)
        if lease is None:
            return None
        current = lease

        def renew() -> Lease:
            nonlocal current
            current = self._queue.heartbeat(current, self._lease_seconds)
            return current

        self._queue.assert_current(current)
        value = executor(current, renew)
        self._queue.assert_current(current)
        return InMemoryCandidate(current, value)

    def run_materialized_once(
        self,
        canonical: CanonicalBlobStore,
        workspace_root: Path,
        executor: Callable[[Lease, tuple[MaterializedInput, ...]], Candidate],
        probe: MediaProbe,
    ) -> Optional[InMemoryCandidate[Candidate]]:
        """Materialize DB-owned canonical inputs in a 0700 attempt workspace.

        The result still remains in memory; no artifact, winner, or job success
        is persisted by this Phase 3C helper.
        """

        from .materialization import AttemptInputMaterializer

        lease = self._queue.claim_next(self._worker_id, self._lease_seconds)
        if lease is None:
            return None
        job, attempt, evidence = self._queue.load_verified_input_evidence(lease)
        value: Candidate
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            evidence,
            canonical,
            cast(LeaseAuthority, self._queue),
            workspace_root,
            probe,
        ) as materializer:
            value = executor(lease, materializer.inputs)
        self._queue.assert_current(lease)
        return InMemoryCandidate(lease, cast(Candidate, value))
