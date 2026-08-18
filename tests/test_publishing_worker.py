# pyright: reportMissingImports=false
"""CPU tests for the Phase 3E Runtime-candidate publication worker."""

from __future__ import annotations

import hashlib
import io
import time
import wave
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional, Sequence
from uuid import uuid4

import pytest

from nano_aural_runtime import InvocationCancelledError, InvocationResult, ProducedArtifact
from nano_aural_runtime.durable.artifact_publication import (
    PublicationCandidate,
    PublicationProgressHook,
    PublicationProgressPoint,
)
from nano_aural_runtime.durable.domain import ArtifactKind
from nano_aural_runtime.durable.errors import StateTransitionError
from nano_aural_runtime.durable.publication import VisibleArtifact
from nano_aural_runtime.durable.publishing_worker import (
    DurablePublishingWorker,
    PublicationMonitorError,
    PublicationPlanningError,
    PublishedRuntimeCandidate,
    SingleOutputArtifactPlanner,
)
from nano_aural_runtime.durable.queue import Lease
from nano_aural_runtime.durable.runtime_worker import RuntimeCandidate


def _wav(frames: int = 400) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def _lease() -> Lease:
    return Lease(
        str(uuid4()),
        str(uuid4()),
        str(uuid4()),
        1,
        datetime.now(timezone.utc) + timedelta(seconds=5),
    )


def _runtime_candidate(
    lease: Lease, content: Optional[bytes] = None, *, count: int = 1
) -> RuntimeCandidate:
    payload = _wav() if content is None else content
    artifacts = tuple(
        ProducedArtifact(
            "private-/operator/path-{0}.wav".format(index),
            "audio/wav",
            payload,
            {"source_path": "/private/operator/model"},
        )
        for index in range(count)
    )
    return RuntimeCandidate(lease, InvocationResult(str(uuid4()), artifacts))


class OneShotSource:
    def __init__(self, candidate: Optional[RuntimeCandidate]) -> None:
        self.candidate = candidate

    def run_once(self) -> Optional[RuntimeCandidate]:
        candidate, self.candidate = self.candidate, None
        return candidate


class SharedLeaseState:
    def __init__(self) -> None:
        self.lock = Lock()
        self.current = True
        self.cancel_requested = False
        self.heartbeats = 0
        self.failed: list[tuple[int, int]] = []
        self.cancelled = 0
        self.monitor_closed = 0
        self.lose_after: Optional[int] = None
        self.cancel_after: Optional[int] = None
        self.fatal_after: Optional[int] = None


class FakeCommandQueue:
    def __init__(self, state: SharedLeaseState) -> None:
        self.state = state

    def cancellation_requested(self, lease: Lease) -> bool:
        assert isinstance(lease, Lease)
        with self.state.lock:
            if not self.state.current:
                raise StateTransitionError("lease lost")
            return self.state.cancel_requested

    def cancel_current(self, lease: Lease) -> None:
        assert isinstance(lease, Lease)
        with self.state.lock:
            if not self.state.current or not self.state.cancel_requested:
                raise StateTransitionError("not cancellable")
            self.state.cancelled += 1
            self.state.current = False

    def fail_retryable(
        self, lease: Lease, retry_delay_seconds: int = 1, max_attempts: int = 3
    ) -> None:
        assert isinstance(lease, Lease)
        with self.state.lock:
            if not self.state.current:
                raise StateTransitionError("lease lost")
            self.state.failed.append((retry_delay_seconds, max_attempts))
            self.state.current = False


class FakeMonitorQueue:
    def __init__(self, state: SharedLeaseState) -> None:
        self.state = state

    def cancellation_requested(self, lease: Lease) -> bool:
        assert isinstance(lease, Lease)
        with self.state.lock:
            if not self.state.current:
                raise StateTransitionError("lease lost")
            if (
                self.state.cancel_after is not None
                and self.state.heartbeats >= self.state.cancel_after
            ):
                self.state.cancel_requested = True
            return self.state.cancel_requested

    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        with self.state.lock:
            if not self.state.current or self.state.cancel_requested:
                raise StateTransitionError("lease lost")
            self.state.heartbeats += 1
            if (
                self.state.fatal_after is not None
                and self.state.heartbeats >= self.state.fatal_after
            ):
                raise RuntimeError("injected monitor connection failure")
            if self.state.lose_after is not None and self.state.heartbeats >= self.state.lose_after:
                self.state.current = False
                raise StateTransitionError("reaper won")
        return Lease(
            lease.job_id,
            lease.attempt_id,
            lease.worker_id,
            lease.lease_epoch,
            datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
        )

    def close(self) -> None:
        with self.state.lock:
            self.state.monitor_closed += 1


class RecordingPublicationService:
    def __init__(
        self,
        progress_hook: PublicationProgressHook,
        *,
        work_steps: int = 1,
        delay: float = 0.0,
    ) -> None:
        self.progress_hook = progress_hook
        self.work_steps = work_steps
        self.delay = delay
        self.published = False
        self.received: tuple[PublicationCandidate, ...] = ()

    def publish(
        self, lease: Lease, candidates: Sequence[PublicationCandidate]
    ) -> tuple[VisibleArtifact, ...]:
        self.received = tuple(candidates)
        for index in range(self.work_steps):
            self.progress_hook(
                lease,
                PublicationProgressPoint.ATTEMPT_WRITE,
                index + 1,
            )
            if self.delay:
                time.sleep(self.delay)
        self.progress_hook(lease, PublicationProgressPoint.BEFORE_FINALIZE, 0)
        candidate = self.received[0]
        assert candidate.expected_sha256 is not None
        assert candidate.expected_size_bytes is not None
        self.published = True
        return (
            VisibleArtifact(
                str(uuid4()),
                str(uuid4()),
                lease.job_id,
                lease.attempt_id,
                ArtifactKind.OUTPUT,
                str(uuid4()),
                candidate.expected_sha256,
                candidate.expected_size_bytes,
                "blobs/sha256/{0}/{1}".format(
                    candidate.expected_sha256[:2], candidate.expected_sha256
                ),
                candidate.content_type,
            ),
        )


class RecordingServiceFactory:
    def __init__(self, *, work_steps: int = 1, delay: float = 0.0) -> None:
        self.work_steps = work_steps
        self.delay = delay
        self.services: list[RecordingPublicationService] = []

    def __call__(self, progress_hook: PublicationProgressHook) -> RecordingPublicationService:
        service = RecordingPublicationService(
            progress_hook, work_steps=self.work_steps, delay=self.delay
        )
        self.services.append(service)
        return service


def _worker(
    candidate: RuntimeCandidate,
    state: SharedLeaseState,
    factory: RecordingServiceFactory,
) -> DurablePublishingWorker:
    return DurablePublishingWorker(
        OneShotSource(candidate),
        FakeCommandQueue(state),
        lambda: FakeMonitorQueue(state),
        factory,
        SingleOutputArtifactPlanner(4 * 1024 * 1024, chunk_size=31),
        lease_seconds=1,
        heartbeat_interval_seconds=0.002,
        monitor_start_timeout_seconds=1,
    )


def test_single_output_planner_seals_identity_and_drops_untrusted_metadata() -> None:
    candidate = _runtime_candidate(_lease())
    publication = SingleOutputArtifactPlanner(1_000_000, chunk_size=17).plan(candidate)[0]
    content = candidate.result.artifacts[0].content
    assert publication.kind is ArtifactKind.OUTPUT
    assert publication.expected_sha256 == hashlib.sha256(content).hexdigest()
    assert publication.expected_size_bytes == len(content)
    assert b"".join(publication.chunks_factory()) == content
    assert dict(publication.metadata) == {}
    assert "private" not in repr(publication)


@pytest.mark.parametrize("count", (0, 2))
def test_single_output_planner_rejects_missing_or_extra_artifact(count: int) -> None:
    with pytest.raises(PublicationPlanningError, match="exactly one"):
        SingleOutputArtifactPlanner(1_000_000).plan(_runtime_candidate(_lease(), count=count))


def test_long_publication_heartbeats_and_returns_one_visible_output() -> None:
    lease, state = _lease(), SharedLeaseState()
    factory = RecordingServiceFactory(work_steps=20, delay=0.003)
    result = _worker(_runtime_candidate(lease), state, factory).run_once()
    assert isinstance(result, PublishedRuntimeCandidate)
    assert len(result.visible) == 1
    assert result.visible[0].kind is ArtifactKind.OUTPUT
    assert state.heartbeats >= 2
    assert state.failed == []
    assert state.cancelled == 0
    assert state.monitor_closed == 1


def test_monitor_creation_failure_never_constructs_or_starts_publication() -> None:
    lease, state = _lease(), SharedLeaseState()
    factory = RecordingServiceFactory()

    def fail_monitor() -> FakeMonitorQueue:
        raise RuntimeError("postgres unavailable")

    worker = DurablePublishingWorker(
        OneShotSource(_runtime_candidate(lease)),
        FakeCommandQueue(state),
        fail_monitor,
        factory,
        SingleOutputArtifactPlanner(1_000_000),
        lease_seconds=1,
    )
    with pytest.raises(PublicationMonitorError, match="no publication"):
        worker.run_once()
    assert factory.services == []
    assert state.failed == [(1, 3)]


def test_db_cancel_propagates_to_progress_token_and_never_becomes_visible() -> None:
    lease, state = _lease(), SharedLeaseState()
    state.cancel_after = 2
    factory = RecordingServiceFactory(work_steps=100, delay=0.003)
    with pytest.raises(InvocationCancelledError, match="cancellation"):
        _worker(_runtime_candidate(lease), state, factory).run_once()
    assert state.cancelled == 1
    assert state.failed == []
    assert factory.services and not factory.services[0].published


def test_reaper_lease_loss_aborts_without_stale_state_mutation_or_visibility() -> None:
    lease, state = _lease(), SharedLeaseState()
    state.lose_after = 2
    factory = RecordingServiceFactory(work_steps=100, delay=0.003)
    with pytest.raises(InvocationCancelledError, match="lease lost"):
        _worker(_runtime_candidate(lease), state, factory).run_once()
    assert state.failed == []
    assert state.cancelled == 0
    assert factory.services and not factory.services[0].published


def test_cancel_probe_connection_failure_after_heartbeat_ste_is_fail_closed() -> None:
    lease, state = _lease(), SharedLeaseState()
    factory = RecordingServiceFactory(work_steps=100, delay=0.003)

    class ProbeFatalAfterHeartbeatMonitor(FakeMonitorQueue):
        def __init__(self, shared: SharedLeaseState) -> None:
            super().__init__(shared)
            self._probes = 0

        def cancellation_requested(self, lease: Lease) -> bool:
            assert isinstance(lease, Lease)
            self._probes += 1
            if self._probes == 1:
                return False
            raise RuntimeError("injected cancel probe connection failure")

        def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
            del lease, lease_seconds
            raise StateTransitionError("lease is no longer current")

    worker = DurablePublishingWorker(
        OneShotSource(_runtime_candidate(lease)),
        FakeCommandQueue(state),
        lambda: ProbeFatalAfterHeartbeatMonitor(state),
        factory,
        SingleOutputArtifactPlanner(4 * 1024 * 1024, chunk_size=31),
        lease_seconds=1,
        heartbeat_interval_seconds=0.002,
        monitor_start_timeout_seconds=1,
    )
    with pytest.raises(PublicationMonitorError, match="monitor failed"):
        worker.run_once()
    assert state.cancelled == 0
    assert state.failed == [(1, 3)]
    assert factory.services == [] or not factory.services[0].published


def test_heartbeat_blocked_by_cancel_flag_terminalizes_as_cancel() -> None:
    lease, state = _lease(), SharedLeaseState()
    factory = RecordingServiceFactory(work_steps=100, delay=0.003)

    class HeartbeatCancelMonitor(FakeMonitorQueue):
        def cancellation_requested(self, lease: Lease) -> bool:
            assert isinstance(lease, Lease)
            with self.state.lock:
                if not self.state.current:
                    raise StateTransitionError("lease lost")
                return self.state.cancel_requested

        def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
            with self.state.lock:
                if not self.state.current:
                    raise StateTransitionError("lease lost")
                self.state.heartbeats += 1
                if self.state.heartbeats >= 2:
                    self.state.cancel_requested = True
                    raise StateTransitionError("cancel_requested_at blocks heartbeat")
            return Lease(
                lease.job_id,
                lease.attempt_id,
                lease.worker_id,
                lease.lease_epoch,
                datetime.now(timezone.utc) + timedelta(seconds=lease_seconds),
            )

    worker = DurablePublishingWorker(
        OneShotSource(_runtime_candidate(lease)),
        FakeCommandQueue(state),
        lambda: HeartbeatCancelMonitor(state),
        factory,
        SingleOutputArtifactPlanner(4 * 1024 * 1024, chunk_size=31),
        lease_seconds=1,
        heartbeat_interval_seconds=0.002,
        monitor_start_timeout_seconds=1,
    )
    with pytest.raises(InvocationCancelledError, match="cancellation"):
        worker.run_once()
    assert state.cancelled == 1
    assert state.failed == []
    assert factory.services and not factory.services[0].published


def test_monitor_fatal_aborts_before_visibility_and_requeues_current_attempt() -> None:
    lease, state = _lease(), SharedLeaseState()
    state.fatal_after = 2
    factory = RecordingServiceFactory(work_steps=100, delay=0.003)
    with pytest.raises(PublicationMonitorError, match="monitor failed"):
        _worker(_runtime_candidate(lease), state, factory).run_once()
    assert state.failed == [(1, 3)]
    assert factory.services and not factory.services[0].published


def test_deterministic_runtime_result_contract_failure_is_terminal() -> None:
    lease, state = _lease(), SharedLeaseState()
    factory = RecordingServiceFactory()
    with pytest.raises(PublicationPlanningError):
        _worker(_runtime_candidate(lease, count=2), state, factory).run_once()
    assert state.failed == [(1, 1)]
    assert factory.services == []
