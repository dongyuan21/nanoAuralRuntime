"""CPU contracts for the Phase 3D fenced Runtime worker.

These tests deliberately use a genuine :class:`Runtime` with a CPU fake
adapter.  The queue and canonical store are small structural doubles: this
module tests the worker's composition boundary, while PostgreSQL fencing and
input materialization have their own Phase 3C suites.
"""

from __future__ import annotations

import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from shutil import rmtree
from typing import Callable, Optional, cast
from unittest.mock import patch

from nano_aural_runtime import (
    AdapterExecutionError,
    AdapterRegistry,
    CancellationToken,
    ExecutionContext,
    FakeAudioAdapter,
    InvocationCancelledError,
    InvocationRejectedError,
    InvocationResult,
    ModelDeployment,
    ModelDescriptor,
    ModelInvocation,
    Runtime,
)
from nano_aural_runtime.durable.domain import AttemptRecord, DeploymentRecord, JobRecord, JobState
from nano_aural_runtime.durable.errors import StateTransitionError
from nano_aural_runtime.durable.queue import Lease, PostgresLeaseQueue
from nano_aural_runtime.durable.runtime_worker import (
    ClaimGuard,
    DurableRuntimeWorker,
    RuntimeCandidate,
    WorkerProcessFatal,
)
from nano_aural_runtime.durable.upload_transports import CanonicalBlobStore
from nano_aural_runtime.durable.uploads import WaveMediaProbe


def _lease(attempt: str = "attempt-1") -> Lease:
    return Lease("job-1", attempt, "worker-1", 1, datetime.now(timezone.utc))


def _durable_deployment() -> DeploymentRecord:
    return DeploymentRecord("deployment-1", "CPU fake", "fake", "fingerprint-1")


def _job(lease: Lease) -> JobRecord:
    return JobRecord(
        lease.job_id,
        "namespace-1",
        "key-" + lease.attempt_id,
        "a" * 64,
        {"task": "zero-input"},
        "deployment-1",
        (),
        state=JobState.RUNNING,
        current_attempt_id=lease.attempt_id,
        lease_epoch=lease.lease_epoch,
    )


class _Builder:
    def __init__(self) -> None:
        self.core_calls: list[DeploymentRecord] = []
        self.build_calls: list[tuple[DeploymentRecord, JobRecord, tuple[object, ...]]] = []

    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
        self.core_calls.append(deployment)
        return ModelDeployment(
            deployment.deployment_id,
            ModelDescriptor(deployment.adapter_id, "CPU fake", "1"),
            deployment.fingerprint,
        )

    def build(
        self, deployment: DeploymentRecord, job: JobRecord, inputs: tuple[object, ...]
    ) -> ModelInvocation:
        self.build_calls.append((deployment, job, inputs))
        assert job.current_attempt_id is not None
        return ModelInvocation("invoke-" + job.current_attempt_id, "fake", {"inputs": len(inputs)})


class _MismatchedBuilder(_Builder):
    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
        core = super().core_deployment(deployment)
        return ModelDeployment(
            "wrong-deployment",
            core.descriptor,
            "wrong-fingerprint",
        )


class _WrongAdapterBuilder(_Builder):
    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
        core = super().core_deployment(deployment)
        return ModelDeployment(
            core.deployment_id,
            ModelDescriptor("other-adapter", "wrong", "1"),
            core.fingerprint,
        )


class _BlockingLoadAdapter(FakeAudioAdapter):
    def __init__(self, entered: threading.Event, release: threading.Event) -> None:
        super().__init__()
        self._entered, self._release = entered, release

    def load(self, session: object) -> None:
        del session
        self.load_calls += 1
        self._entered.set()
        if not self._release.wait(2):
            raise TimeoutError("test did not release Runtime.load")


class _FailingLoadAdapter(FakeAudioAdapter):
    def load(self, session: object) -> None:
        del session
        self.load_calls += 1
        raise RuntimeError("model load failed")


class _Queue:
    """Queue double that makes the worker's legal side effects observable."""

    def __init__(self, leases: list[Lease]) -> None:
        self._leases = list(leases)
        self.assert_calls = 0
        self.fail_calls: list[tuple[Lease, dict[str, int]]] = []
        self.cancel_calls: list[Lease] = []
        self.heartbeat_calls = 0
        self.cancel_requested = False
        self.fail_assert_on: Optional[int] = None

    def claim_next(self, worker_id: str, lease_seconds: int) -> Optional[Lease]:
        assert worker_id == "worker-1"
        assert lease_seconds == 2
        return self._leases.pop(0) if self._leases else None

    def load_ready_deployment(self, lease: Lease) -> DeploymentRecord:
        return _durable_deployment()

    def load_verified_input_evidence(self, lease: Lease):
        return (
            _job(lease),
            AttemptRecord(lease.attempt_id, lease.job_id, lease.worker_id, 1, lease.lease_epoch),
            (),
        )

    def assert_current(self, lease: Lease) -> None:
        self.assert_calls += 1
        if self.fail_assert_on is not None and self.assert_calls >= self.fail_assert_on:
            raise StateTransitionError("lease was fenced")

    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        assert lease_seconds == 2
        self.heartbeat_calls += 1
        return lease

    def cancellation_requested(self, lease: Lease) -> bool:
        return self.cancel_requested

    def cancel_current(self, lease: Lease) -> None:
        self.cancel_calls.append(lease)

    def fail_retryable(self, lease: Lease, **kwargs: int) -> None:
        self.fail_calls.append((lease, kwargs))


class _Monitor:
    def __init__(self, owner: _Queue, failure: Optional[BaseException] = None) -> None:
        self.owner, self.failure = owner, failure
        self.closed = False

    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        if self.failure is not None:
            raise self.failure
        return self.owner.heartbeat(lease, lease_seconds)

    def close(self) -> None:
        self.closed = True


class _RefreshingMonitor(_Monitor):
    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        super().heartbeat(lease, lease_seconds)
        return Lease(
            lease.job_id,
            lease.attempt_id,
            lease.worker_id,
            lease.lease_epoch,
            datetime(2030, 1, 1, tzinfo=timezone.utc),
        )


class _BrokenCloseMonitor(_Monitor):
    def close(self) -> None:
        super().close()
        raise RuntimeError("monitor close failed")


class _LeaseLossMonitor(_Monitor):
    """Tell the owner that a heartbeat observed a lease loss after renewing once."""

    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        self.owner.heartbeat(lease, lease_seconds)
        self.owner.fail_assert_on = self.owner.assert_calls + 1
        raise StateTransitionError("lease lost while a blocking stage was running")


class _CancellationMonitor(_Monitor):
    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        self.owner.heartbeat(lease, lease_seconds)
        self.owner.cancel_requested = True
        self.owner.fail_assert_on = self.owner.assert_calls + 1
        raise StateTransitionError("cancellation requested while a blocking stage was running")


def _blocking_materializer(
    entered: threading.Event,
    release: threading.Event,
    cleaned: threading.Event,
):
    """A structural materializer double which owns an attempt-local directory."""

    class BlockingMaterializer:
        def __init__(self, *args: object) -> None:
            self._workspace = Path(cast(Path, args[6])) / "blocked-materialization"

        @property
        def inputs(self) -> tuple[object, ...]:
            return ()

        def __enter__(self) -> "BlockingMaterializer":
            self._workspace.mkdir(parents=True, exist_ok=False)
            entered.set()
            if not release.wait(2):
                raise TimeoutError("test did not release materialization")
            return self

        def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
            rmtree(self._workspace)
            cleaned.set()
            return False

    return BlockingMaterializer


class DurableRuntimeWorkerTests(unittest.TestCase):
    def _worker(
        self,
        queue: _Queue,
        adapter: FakeAudioAdapter,
        factory: Optional[Callable[[], object]] = None,
        builder: Optional[_Builder] = None,
        guard: Optional[ClaimGuard] = None,
    ) -> tuple[DurableRuntimeWorker, _Builder, list[object]]:
        registry = AdapterRegistry()
        registry.register(adapter)
        monitors: list[object] = []

        def monitor_factory() -> object:
            if factory is not None:
                monitor = factory()
            else:
                monitor = _Monitor(queue)
            monitors.append(monitor)
            return monitor

        temporary_workspace = tempfile.TemporaryDirectory(prefix="runtime-worker-test-")
        self.addCleanup(temporary_workspace.cleanup)
        workspace = Path(temporary_workspace.name)
        actual_builder = builder or _Builder()
        return (
            DurableRuntimeWorker(
                cast(PostgresLeaseQueue, queue),
                "worker-1",
                2,
                Runtime(registry),
                actual_builder,
                cast(CanonicalBlobStore, object()),
                workspace,
                WaveMediaProbe(),
                cast(Callable[[], PostgresLeaseQueue], monitor_factory),
                guard=guard,
            ),
            actual_builder,
            monitors,
        )

    def test_cpu_zero_input_job_uses_runtime_and_keeps_only_memory_candidate(self) -> None:
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, builder, monitors = self._worker(queue, adapter)

        candidate = worker.run_once()

        self.assertIsNotNone(candidate)
        assert candidate is not None
        self.assertEqual("invoke-attempt-1", candidate.result.invocation_id)
        self.assertEqual(1, adapter.load_calls)
        self.assertEqual(1, adapter.invoke_calls)
        self.assertEqual(0, adapter.unload_calls)
        self.assertEqual((), builder.build_calls[0][2])
        self.assertGreaterEqual(queue.assert_calls, 4)
        self.assertEqual([], queue.fail_calls)
        self.assertEqual([], queue.cancel_calls)
        self.assertEqual(1, len(monitors))
        self.assertTrue(cast(_Monitor, monitors[0]).closed)
        # AttemptInputMaterializer must remove the server-created workspace.
        workspace_root = worker._workspace_root  # type: ignore[attr-defined]
        self.assertEqual([], list(workspace_root.iterdir()))

    def test_claim_guard_rejects_before_runtime_load(self) -> None:
        class _Reject:
            def validate(
                self, deployment: DeploymentRecord, job: Optional[JobRecord] = None
            ) -> None:
                raise InvocationRejectedError("worker capability does not match deployment")

        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, _builder, _monitors = self._worker(queue, adapter, guard=_Reject())

        with self.assertRaisesRegex(InvocationRejectedError, "capability"):
            worker.run_once()

        self.assertEqual(0, adapter.load_calls)
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual(1, len(queue.fail_calls))

    def test_durable_deployment_identity_is_checked_before_runtime_load(self) -> None:
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, _builder, _monitors = self._worker(queue, adapter, builder=_MismatchedBuilder())

        with self.assertRaisesRegex(InvocationRejectedError, "does not match"):
            worker.run_once()

        self.assertEqual(0, adapter.load_calls)
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual(1, len(queue.fail_calls))

    def test_durable_adapter_identity_is_checked_before_runtime_load(self) -> None:
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, _builder, _monitors = self._worker(queue, adapter, builder=_WrongAdapterBuilder())

        with self.assertRaisesRegex(InvocationRejectedError, "does not match"):
            worker.run_once()

        self.assertEqual(0, adapter.load_calls)
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual(1, len(queue.fail_calls))

    def test_runtime_session_is_single_flight_even_when_two_callers_arrive(self) -> None:
        entered, release = threading.Event(), threading.Event()
        active = 0
        maximum_active = 0
        lock = threading.Lock()

        def serialized(session, invocation, context):
            nonlocal active, maximum_active
            del session, context
            with lock:
                active += 1
                maximum_active = max(maximum_active, active)
                entered.set()
            release.wait(2)
            with lock:
                active -= 1
            return InvocationResult(invocation.invocation_id)

        adapter = FakeAudioAdapter(result_factory=serialized)
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(_Builder().core_deployment(_durable_deployment()))
        errors: list[BaseException] = []

        def invoke(identifier: str) -> None:
            try:
                runtime.invoke(session, ModelInvocation(identifier, "fake"))
            except BaseException as error:  # pragma: no cover - assertion below checks it
                errors.append(error)

        first = threading.Thread(target=invoke, args=("one",))
        second = threading.Thread(target=invoke, args=("two",))
        first.start()
        self.assertTrue(entered.wait(1))
        second.start()
        threading.Event().wait(0.05)
        self.assertEqual(1, adapter.invoke_calls)
        release.set()
        first.join(2)
        second.join(2)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(1, maximum_active)

    def test_successful_attempts_reuse_one_runtime_session(self) -> None:
        queue = _Queue([_lease("attempt-1"), _lease("attempt-2")])
        adapter = FakeAudioAdapter()
        worker, _builder, _monitors = self._worker(queue, adapter)

        self.assertIsNotNone(worker.run_once())
        self.assertIsNotNone(worker.run_once())

        self.assertEqual(1, adapter.load_calls)
        self.assertEqual(2, adapter.invoke_calls)
        self.assertEqual(0, adapter.unload_calls)

    def test_worker_close_unloads_the_reusable_session_once(self) -> None:
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, _builder, _monitors = self._worker(queue, adapter)
        self.assertIsNotNone(worker.run_once())

        worker.close()
        worker.close()

        self.assertEqual(1, adapter.unload_calls)

    def test_candidate_carries_the_monitor_refreshed_lease(self) -> None:
        started, release = threading.Event(), threading.Event()

        def blocking(session, invocation, context) -> InvocationResult:
            del session, invocation, context
            started.set()
            self.assertTrue(release.wait(2))
            return InvocationResult("invoke-attempt-1")

        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter(result_factory=blocking)
        worker, _builder, _monitors = self._worker(
            queue, adapter, lambda: _RefreshingMonitor(queue)
        )
        result: list[object] = []
        thread = threading.Thread(target=lambda: result.append(worker.run_once()))
        thread.start()
        self.assertTrue(started.wait(1))
        for _ in range(30):
            if queue.heartbeat_calls:
                break
            threading.Event().wait(0.02)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        candidate = cast(RuntimeCandidate, result[0])
        self.assertEqual(datetime(2030, 1, 1, tzinfo=timezone.utc), candidate.lease.expires_at)

    def test_materialization_is_heartbeat_monitored_and_carries_refreshed_lease(self) -> None:
        entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, _builder, _monitors = self._worker(
            queue, adapter, lambda: _RefreshingMonitor(queue)
        )
        outcome: list[object] = []
        thread = threading.Thread(target=lambda: outcome.append(worker.run_once()))

        with patch(
            "nano_aural_runtime.durable.runtime_worker.AttemptInputMaterializer",
            _blocking_materializer(entered, release, cleaned),
        ):
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                for _ in range(30):
                    if queue.heartbeat_calls:
                        break
                    threading.Event().wait(0.02)
                self.assertGreaterEqual(queue.heartbeat_calls, 1)
            finally:
                release.set()
                thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(outcome))
        candidate = cast(RuntimeCandidate, outcome[0])
        self.assertEqual(datetime(2030, 1, 1, tzinfo=timezone.utc), candidate.lease.expires_at)
        self.assertTrue(cleaned.is_set())
        self.assertEqual([], list(worker._workspace_root.iterdir()))  # type: ignore[attr-defined]

    def test_lease_loss_during_materialization_stops_load_invoke_and_candidate(self) -> None:
        entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, builder, _monitors = self._worker(queue, adapter, lambda: _LeaseLossMonitor(queue))
        outcome: list[BaseException | object] = []

        def run() -> None:
            try:
                outcome.append(worker.run_once())
            except BaseException as error:
                outcome.append(error)

        thread = threading.Thread(target=run)
        with patch(
            "nano_aural_runtime.durable.runtime_worker.AttemptInputMaterializer",
            _blocking_materializer(entered, release, cleaned),
        ):
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                for _ in range(30):
                    if queue.heartbeat_calls:
                        break
                    threading.Event().wait(0.02)
                self.assertGreaterEqual(queue.heartbeat_calls, 1)
            finally:
                release.set()
                thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(outcome))
        self.assertIsInstance(outcome[0], (InvocationCancelledError, StateTransitionError))
        self.assertEqual(0, adapter.load_calls)
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual([], builder.build_calls)
        self.assertTrue(cleaned.is_set())
        self.assertEqual([], list(worker._workspace_root.iterdir()))  # type: ignore[attr-defined]

    def test_db_cancellation_during_materialization_stops_later_stages_and_cleans(self) -> None:
        entered, release, cleaned = threading.Event(), threading.Event(), threading.Event()
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, builder, _monitors = self._worker(
            queue, adapter, lambda: _CancellationMonitor(queue)
        )
        outcome: list[BaseException | object] = []

        def run() -> None:
            try:
                outcome.append(worker.run_once())
            except BaseException as error:
                outcome.append(error)

        thread = threading.Thread(target=run)
        with patch(
            "nano_aural_runtime.durable.runtime_worker.AttemptInputMaterializer",
            _blocking_materializer(entered, release, cleaned),
        ):
            thread.start()
            try:
                self.assertTrue(entered.wait(1))
                for _ in range(30):
                    if queue.heartbeat_calls:
                        break
                    threading.Event().wait(0.02)
                self.assertGreaterEqual(queue.heartbeat_calls, 1)
            finally:
                release.set()
                thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(outcome))
        self.assertIsInstance(outcome[0], (InvocationCancelledError, StateTransitionError))
        self.assertEqual(["attempt-1"], [lease.attempt_id for lease in queue.cancel_calls])
        self.assertEqual(0, adapter.load_calls)
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual([], builder.build_calls)
        self.assertTrue(cleaned.is_set())
        self.assertEqual([], list(worker._workspace_root.iterdir()))  # type: ignore[attr-defined]

    def test_runtime_load_is_heartbeat_monitored_before_first_invoke(self) -> None:
        entered, release = threading.Event(), threading.Event()
        queue = _Queue([_lease()])
        adapter = _BlockingLoadAdapter(entered, release)
        worker, _builder, _monitors = self._worker(
            queue, adapter, lambda: _RefreshingMonitor(queue)
        )
        outcome: list[object] = []
        thread = threading.Thread(target=lambda: outcome.append(worker.run_once()))
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            for _ in range(30):
                if queue.heartbeat_calls:
                    break
                threading.Event().wait(0.02)
            self.assertGreaterEqual(queue.heartbeat_calls, 1)
        finally:
            release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(outcome))
        candidate = cast(RuntimeCandidate, outcome[0])
        self.assertEqual(datetime(2030, 1, 1, tzinfo=timezone.utc), candidate.lease.expires_at)
        self.assertEqual(1, adapter.invoke_calls)
        self.assertEqual([], list(worker._workspace_root.iterdir()))  # type: ignore[attr-defined]

    def test_lease_loss_while_runtime_load_is_blocked_never_invokes_or_returns_candidate(
        self,
    ) -> None:
        entered, release = threading.Event(), threading.Event()
        queue = _Queue([_lease(), _lease("attempt-2")])
        adapter = _BlockingLoadAdapter(entered, release)
        worker, builder, _monitors = self._worker(queue, adapter, lambda: _LeaseLossMonitor(queue))
        outcome: list[BaseException | object] = []

        def run() -> None:
            try:
                outcome.append(worker.run_once())
            except BaseException as error:
                outcome.append(error)

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            for _ in range(30):
                if queue.heartbeat_calls:
                    break
                threading.Event().wait(0.02)
            self.assertGreaterEqual(queue.heartbeat_calls, 1)
        finally:
            release.set()
            thread.join(2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(outcome))
        self.assertIsInstance(outcome[0], (InvocationCancelledError, StateTransitionError))
        self.assertEqual(1, adapter.load_calls)
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual([], builder.build_calls)
        self.assertEqual([], list(worker._workspace_root.iterdir()))  # type: ignore[attr-defined]

    def test_runtime_load_fault_is_process_fatal_and_workspace_is_cleaned(self) -> None:
        queue = _Queue([_lease(), _lease("attempt-2")])
        adapter = _FailingLoadAdapter()
        worker, builder, _monitors = self._worker(queue, adapter)

        with self.assertRaisesRegex(WorkerProcessFatal, "Runtime.load"):
            worker.run_once()

        self.assertEqual(1, adapter.load_calls)
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual([], builder.build_calls)
        with self.assertRaises(WorkerProcessFatal):
            worker.run_once()
        self.assertEqual([], list(worker._workspace_root.iterdir()))  # type: ignore[attr-defined]

    def test_monitor_has_its_own_connection_and_heartbeats_during_runtime_invoke(self) -> None:
        started, release = threading.Event(), threading.Event()

        def blocking(session, invocation, context: ExecutionContext) -> InvocationResult:
            del session, invocation
            started.set()
            self.assertTrue(release.wait(2))
            context.cancellation_token.raise_if_cancelled()
            return InvocationResult("invoke-attempt-1")

        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter(result_factory=blocking)
        worker, _builder, monitors = self._worker(queue, adapter)
        result: list[object] = []
        thread = threading.Thread(target=lambda: result.append(worker.run_once()))
        thread.start()
        self.assertTrue(started.wait(1))
        for _ in range(30):
            if queue.heartbeat_calls:
                break
            threading.Event().wait(0.02)
        self.assertGreaterEqual(queue.heartbeat_calls, 1)
        release.set()
        thread.join(2)
        self.assertFalse(thread.is_alive())
        self.assertEqual(1, len(result))
        self.assertTrue(cast(_Monitor, monitors[0]).closed)

    def test_monitor_factory_failure_prevents_adapter_thread_from_starting(self) -> None:
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()

        def unavailable() -> object:
            raise RuntimeError("monitor database unavailable")

        worker, _builder, _monitors = self._worker(queue, adapter, unavailable)
        with self.assertRaisesRegex(RuntimeError, "monitor database unavailable"):
            worker.run_once()
        self.assertEqual(0, adapter.invoke_calls)
        self.assertEqual(0, adapter.load_calls)
        self.assertEqual(0, adapter.unload_calls)
        self.assertEqual(1, len(queue.fail_calls))

    def test_monitor_close_failure_never_exposes_a_candidate(self) -> None:
        queue = _Queue([_lease()])
        adapter = FakeAudioAdapter()
        worker, _builder, monitors = self._worker(
            queue, adapter, lambda: _BrokenCloseMonitor(queue)
        )

        with self.assertRaisesRegex(WorkerProcessFatal, "could not close safely"):
            worker.run_once()

        self.assertEqual(1, adapter.invoke_calls)
        self.assertEqual([], queue.fail_calls)
        with self.assertRaises(WorkerProcessFatal):
            worker.run_once()
        self.assertTrue(cast(_BrokenCloseMonitor, monitors[0]).closed)

    def test_rejected_adapter_is_terminal_but_ready_session_can_be_reused(self) -> None:
        responses = [InvocationRejectedError("unsupported"), InvocationResult("invoke-attempt-2")]

        def outcomes(session, invocation, context):
            del session, invocation, context
            response = responses.pop(0)
            if isinstance(response, BaseException):
                raise response
            return response

        queue = _Queue([_lease("attempt-1"), _lease("attempt-2")])
        adapter = FakeAudioAdapter(result_factory=outcomes)
        worker, _builder, _monitors = self._worker(queue, adapter)
        with self.assertRaises(InvocationRejectedError):
            worker.run_once()
        self.assertEqual(
            [("attempt-1", {"max_attempts": 1})],
            [(item.attempt_id, kwargs) for item, kwargs in queue.fail_calls],
        )
        self.assertEqual(0, adapter.unload_calls)
        self.assertIsNotNone(worker.run_once())
        self.assertEqual(1, adapter.load_calls)

    def test_cancelled_lease_cancels_same_runtime_token_and_has_no_candidate(self) -> None:
        observed_tokens: list[CancellationToken] = []

        def cooperative(session, invocation, context: ExecutionContext) -> InvocationResult:
            del session, invocation
            observed_tokens.append(context.cancellation_token)
            while not context.cancellation_token.is_cancelled():
                threading.Event().wait(0.01)
            context.cancellation_token.raise_if_cancelled()
            raise AssertionError("unreachable")

        queue = _Queue([_lease()])
        queue.cancel_requested = True
        adapter = FakeAudioAdapter(result_factory=cooperative)
        worker, _builder, monitors = self._worker(
            queue, adapter, lambda: _Monitor(queue, StateTransitionError("cancel requested"))
        )
        with self.assertRaises(InvocationCancelledError):
            worker.run_once()
        self.assertEqual(1, len(observed_tokens))
        self.assertTrue(observed_tokens[0].is_cancelled())
        self.assertEqual(["attempt-1"], [lease.attempt_id for lease in queue.cancel_calls])
        self.assertEqual([], queue.fail_calls)
        self.assertTrue(cast(_Monitor, monitors[0]).closed)

    def test_monitor_driver_failure_cancels_work_and_is_process_fatal(self) -> None:
        observed_tokens: list[CancellationToken] = []

        def cooperative(session, invocation, context: ExecutionContext) -> InvocationResult:
            del session, invocation
            observed_tokens.append(context.cancellation_token)
            while not context.cancellation_token.is_cancelled():
                threading.Event().wait(0.01)
            context.cancellation_token.raise_if_cancelled()
            raise AssertionError("unreachable")

        queue = _Queue([_lease(), _lease("attempt-2")])
        adapter = FakeAudioAdapter(result_factory=cooperative)
        worker, _builder, monitors = self._worker(
            queue, adapter, lambda: _Monitor(queue, RuntimeError("database disconnected"))
        )
        with self.assertRaisesRegex(WorkerProcessFatal, "monitor heartbeat failed"):
            worker.run_once()
        self.assertEqual(1, len(observed_tokens))
        self.assertTrue(observed_tokens[0].is_cancelled())
        self.assertEqual([], queue.fail_calls)
        self.assertEqual([], queue.cancel_calls)
        self.assertTrue(cast(_Monitor, monitors[0]).closed)
        with self.assertRaises(WorkerProcessFatal):
            worker.run_once()

    def test_stale_post_invoke_fence_never_returns_candidate_and_discards_session(self) -> None:
        queue = _Queue([_lease()])
        # enter materializer, worker pre-invoke fence, materializer post-I/O fence,
        # then the post-invocation fence loses the attempt.
        queue.fail_assert_on = 4
        adapter = FakeAudioAdapter()
        worker, _builder, _monitors = self._worker(queue, adapter)
        with self.assertRaisesRegex(StateTransitionError, "fenced"):
            worker.run_once()
        self.assertEqual(1, adapter.invoke_calls)
        self.assertEqual(1, adapter.unload_calls)
        self.assertEqual(1, len(queue.fail_calls))

    def test_execution_fault_discards_failed_session_then_reloads_for_new_attempt(self) -> None:
        failures = [True, False]

        def fail_once(session, invocation, context):
            del session, context
            if failures.pop(0):
                raise AdapterExecutionError("backend died")
            return InvocationResult(invocation.invocation_id)

        queue = _Queue([_lease("attempt-1"), _lease("attempt-2")])
        adapter = FakeAudioAdapter(result_factory=fail_once)
        worker, _builder, _monitors = self._worker(queue, adapter)
        with self.assertRaises(AdapterExecutionError):
            worker.run_once()
        self.assertEqual(1, adapter.unload_calls)
        self.assertIsNotNone(worker.run_once())
        self.assertEqual(2, adapter.load_calls)
        self.assertEqual(2, adapter.invoke_calls)

    def test_base_exception_is_mapped_to_fault_and_worker_does_not_reuse_failed_session(
        self,
    ) -> None:
        failures = [True, False]

        def interrupt_once(session, invocation, context):
            del session, context
            if failures.pop(0):
                raise KeyboardInterrupt("interrupted")
            return InvocationResult(invocation.invocation_id)

        queue = _Queue([_lease("attempt-1"), _lease("attempt-2")])
        adapter = FakeAudioAdapter(result_factory=interrupt_once)
        worker, _builder, _monitors = self._worker(queue, adapter)
        with self.assertRaises(AdapterExecutionError):
            worker.run_once()
        self.assertEqual(1, adapter.unload_calls)
        self.assertIsNotNone(worker.run_once())
        self.assertEqual(2, adapter.load_calls)

    def test_adapter_ignoring_cancel_is_permanently_fatal_and_not_reused(self) -> None:
        started, release = threading.Event(), threading.Event()

        def ignores_cancel(session, invocation, context) -> InvocationResult:
            del session, invocation, context
            started.set()
            release.wait(3)
            return InvocationResult("invoke-attempt-1")

        queue = _Queue([_lease(), _lease("attempt-2")])
        adapter = FakeAudioAdapter(result_factory=ignores_cancel)
        worker, _builder, _monitors = self._worker(
            queue, adapter, lambda: _Monitor(queue, StateTransitionError("lease lost"))
        )
        try:
            with self.assertRaises(WorkerProcessFatal):
                worker.run_once()
            self.assertTrue(started.is_set())
            with self.assertRaises(WorkerProcessFatal):
                worker.run_once()
            self.assertEqual(1, adapter.invoke_calls)
            self.assertEqual([], queue.fail_calls)
        finally:
            release.set()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
