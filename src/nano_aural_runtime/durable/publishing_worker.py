"""Phase 3E application glue from Runtime candidates to fenced publication.

The Runtime invocation and publication are deliberately separate phases.  A
fresh cancellation token and an independent PostgreSQL queue connection guard
every object/probe/finalization checkpoint after the Runtime has returned.
"""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from threading import Event, Lock, Thread
from typing import Callable, Iterable, Optional, Protocol, Sequence, Tuple

from nano_aural_runtime.errors import InvocationCancelledError, InvocationRejectedError
from nano_aural_runtime.models import CancellationToken, ProducedArtifact

from .artifact_publication import (
    ArtifactPublicationService,
    ArtifactRejectedError,
    LeaseFence,
    PublicationCandidate,
    PublicationProgressHook,
    PublicationProgressPoint,
    PublicationRepository,
)
from .artifact_storage import AttemptArtifactStore
from .artifact_validation import ArtifactValidator
from .domain import ArtifactKind
from .errors import StateTransitionError
from .publication import VisibleArtifact
from .queue import Lease
from .runtime_worker import RuntimeCandidate
from .upload_transports import CanonicalBlobStore


class RuntimeCandidateSource(Protocol):
    def run_once(self) -> Optional[RuntimeCandidate]: ...


class PublicationService(Protocol):
    def publish(
        self, lease: Lease, candidates: Sequence[PublicationCandidate]
    ) -> Tuple[VisibleArtifact, ...]: ...


class PublicationServiceFactory(Protocol):
    def __call__(self, progress_hook: PublicationProgressHook, /) -> PublicationService: ...


class PublicationQueue(Protocol):
    def cancellation_requested(self, lease: Lease) -> bool: ...
    def cancel_current(self, lease: Lease) -> None: ...
    def fail_retryable(
        self, lease: Lease, retry_delay_seconds: int = 1, max_attempts: int = 3
    ) -> None: ...


class PublicationLeaseMonitor(Protocol):
    def cancellation_requested(self, lease: Lease) -> bool: ...
    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease: ...
    def close(self) -> None: ...


class PublicationPlanningError(InvocationRejectedError):
    """A Runtime result cannot satisfy this deployment's publication contract."""


class PublicationMonitorError(RuntimeError):
    """The independent lease monitor failed before publication committed."""


@dataclass(frozen=True)
class PublishedRuntimeCandidate:
    runtime: RuntimeCandidate
    visible: Tuple[VisibleArtifact, ...]


class SingleOutputArtifactPlanner:
    """Map exactly one model-neutral Runtime artifact to required OUTPUT."""

    def __init__(self, max_output_bytes: int, chunk_size: int = 1024 * 1024) -> None:
        for value, name in (
            (max_output_bytes, "max_output_bytes"),
            (chunk_size, "chunk_size"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("{0} must be a positive integer".format(name))
        self._max_output_bytes = max_output_bytes
        self._chunk_size = chunk_size

    def plan(self, candidate: RuntimeCandidate) -> Tuple[PublicationCandidate, ...]:
        if not isinstance(candidate, RuntimeCandidate):
            raise TypeError("candidate must be a RuntimeCandidate")
        artifacts = candidate.result.artifacts
        if len(artifacts) != 1:
            raise PublicationPlanningError(
                "the initial durable binding requires exactly one Runtime artifact"
            )
        artifact = artifacts[0]
        if not isinstance(artifact, ProducedArtifact):
            raise PublicationPlanningError("Runtime result contains an invalid artifact")
        content = artifact.content
        if len(content) > self._max_output_bytes:
            raise PublicationPlanningError("Runtime artifact exceeds the output size limit")

        def replay() -> Iterable[bytes]:
            for offset in range(0, len(content), self._chunk_size):
                yield content[offset : offset + self._chunk_size]

        try:
            publication = PublicationCandidate(
                kind=ArtifactKind.OUTPUT,
                content_type=artifact.media_type,
                chunks_factory=replay,
                max_size_bytes=self._max_output_bytes,
                expected_sha256=hashlib.sha256(content).hexdigest(),
                expected_size_bytes=len(content),
                # Adapter metadata and names are not automatically durable: they
                # may contain private paths or backend diagnostics.  A later
                # explicit metadata policy can expose an allowlisted subset.
                metadata={},
            )
        except (TypeError, ValueError) as error:
            raise PublicationPlanningError("Runtime artifact is not publishable") from error
        return (publication,)


@dataclass
class _MonitorOutcome:
    lock: Lock
    fatal: Optional[BaseException] = None
    cancel_requested: bool = False
    lease_lost: bool = False

    @classmethod
    def create(cls) -> "_MonitorOutcome":
        return cls(Lock())

    def record_fatal(self, error: BaseException) -> None:
        with self.lock:
            if self.fatal is None:
                self.fatal = error

    def record_cancel(self) -> None:
        with self.lock:
            self.cancel_requested = True

    def record_lost(self) -> None:
        with self.lock:
            self.lease_lost = True

    def snapshot(self) -> tuple[Optional[BaseException], bool, bool]:
        with self.lock:
            return self.fatal, self.cancel_requested, self.lease_lost


class DurablePublishingWorker:
    """Publish one successful Runtime result under a fresh fenced monitor.

    ``monitor_queue_factory`` must create a queue with its own PostgreSQL
    connection.  Sharing the command connection with the monitor thread is not
    supported.
    """

    def __init__(
        self,
        runtime_worker: RuntimeCandidateSource,
        queue: PublicationQueue,
        monitor_queue_factory: Callable[[], PublicationLeaseMonitor],
        publication_service_factory: PublicationServiceFactory,
        planner: SingleOutputArtifactPlanner,
        *,
        lease_seconds: int,
        heartbeat_interval_seconds: float = 0.05,
        monitor_start_timeout_seconds: float = 5.0,
        retry_delay_seconds: int = 1,
        max_attempts: int = 3,
    ) -> None:
        if (
            isinstance(lease_seconds, bool)
            or not isinstance(lease_seconds, int)
            or lease_seconds < 1
        ):
            raise ValueError("lease_seconds must be a positive integer")
        for value, name in (
            (heartbeat_interval_seconds, "heartbeat_interval_seconds"),
            (monitor_start_timeout_seconds, "monitor_start_timeout_seconds"),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(float(value))
                or value <= 0
            ):
                raise ValueError("{0} must be finite and positive".format(name))
        for value, name in (
            (retry_delay_seconds, "retry_delay_seconds"),
            (max_attempts, "max_attempts"),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise ValueError("{0} must be a positive integer".format(name))
        self._runtime_worker = runtime_worker
        self._queue = queue
        self._monitor_queue_factory = monitor_queue_factory
        self._publication_service_factory = publication_service_factory
        self._planner = planner
        self._lease_seconds = lease_seconds
        self._heartbeat_interval_seconds = float(heartbeat_interval_seconds)
        self._monitor_start_timeout_seconds = float(monitor_start_timeout_seconds)
        self._retry_delay_seconds = retry_delay_seconds
        self._max_attempts = max_attempts

    def run_once(self) -> Optional[PublishedRuntimeCandidate]:
        runtime_candidate = self._runtime_worker.run_once()
        if runtime_candidate is None:
            return None
        lease = runtime_candidate.lease
        token = CancellationToken()
        done, ready = Event(), Event()
        outcome = _MonitorOutcome.create()
        try:
            monitor = self._monitor_queue_factory()
        except BaseException as error:
            self._fail_if_current(lease, deterministic=False)
            raise PublicationMonitorError(
                "publication monitor could not be created; no publication was started"
            ) from error

        def monitor_lease() -> None:
            current = lease
            initialized = False
            try:
                if monitor.cancellation_requested(current):
                    outcome.record_cancel()
                    token.cancel("durable cancellation requested")
                    return
                current = monitor.heartbeat(current, self._lease_seconds)
                initialized = True
                ready.set()
                while not done.wait(self._heartbeat_interval_seconds):
                    if monitor.cancellation_requested(current):
                        outcome.record_cancel()
                        token.cancel("durable cancellation requested")
                        return
                    current = monitor.heartbeat(current, self._lease_seconds)
            except StateTransitionError:
                outcome.record_lost()
                token.cancel("publication lease lost")
            except BaseException as error:
                outcome.record_fatal(error)
                token.cancel("publication monitor failed")
            finally:
                if not initialized:
                    ready.set()
                try:
                    monitor.close()
                except BaseException as error:
                    outcome.record_fatal(error)
                    token.cancel("publication monitor close failed")

        monitor_thread = Thread(
            target=monitor_lease,
            name="durable-publication-monitor",
            daemon=True,
        )
        try:
            monitor_thread.start()
        except BaseException as error:
            try:
                monitor.close()
            except BaseException:
                pass
            self._fail_if_current(lease, deterministic=False)
            raise PublicationMonitorError(
                "publication monitor could not start; no publication was started"
            ) from error
        if not ready.wait(self._monitor_start_timeout_seconds):
            token.cancel("publication monitor startup timed out")
            outcome.record_fatal(TimeoutError("publication monitor startup timed out"))

        visible: Optional[Tuple[VisibleArtifact, ...]] = None
        try:
            self._raise_monitor_error(token, outcome)
            candidates = self._planner.plan(runtime_candidate)
            self._raise_monitor_error(token, outcome)

            def progress(
                lease: Lease,
                point: PublicationProgressPoint,
                bytes_processed: int,
            ) -> None:
                del point, bytes_processed
                if _lease_identity(lease) != _lease_identity(runtime_candidate.lease):
                    raise StateTransitionError("publication progress crossed lease identity")
                self._raise_monitor_error(token, outcome)

            service = self._publication_service_factory(progress)
            visible = service.publish(lease, candidates)
            return PublishedRuntimeCandidate(runtime_candidate, tuple(visible))
        except (PublicationPlanningError, ArtifactRejectedError):
            self._stop_monitor(done, monitor_thread)
            self._finish_failure(lease, outcome, deterministic=True)
            raise
        except InvocationCancelledError:
            self._stop_monitor(done, monitor_thread)
            self._finish_failure(lease, outcome, deterministic=False)
            raise
        except StateTransitionError:
            # A stale lease or a concurrent publisher/finalizer already owns
            # the relevant CAS.  This delivery must never terminalize their
            # still-current attempt.
            self._stop_monitor(done, monitor_thread)
            raise
        except BaseException:
            self._stop_monitor(done, monitor_thread)
            self._finish_failure(lease, outcome, deterministic=False)
            raise
        finally:
            done.set()
            monitor_thread.join(self._monitor_start_timeout_seconds)
            # A committed winner is the database authority.  A heartbeat can
            # observe that terminal transition just before ``done`` is set;
            # such an expected post-commit fence loss cannot undo visibility.
            if visible is None and monitor_thread.is_alive():
                token.cancel("publication monitor did not stop")

    def _stop_monitor(self, done: Event, monitor_thread: Thread) -> None:
        done.set()
        monitor_thread.join(self._monitor_start_timeout_seconds)

    @staticmethod
    def _raise_monitor_error(token: CancellationToken, outcome: _MonitorOutcome) -> None:
        fatal, _cancel_requested, _lease_lost = outcome.snapshot()
        if fatal is not None:
            raise PublicationMonitorError("publication monitor failed") from fatal
        token.raise_if_cancelled()

    def _finish_failure(
        self, lease: Lease, outcome: _MonitorOutcome, *, deterministic: bool
    ) -> None:
        _fatal, cancel_requested, lease_lost = outcome.snapshot()
        if lease_lost:
            return
        try:
            if cancel_requested or self._queue.cancellation_requested(lease):
                self._queue.cancel_current(lease)
            else:
                self._fail_if_current(lease, deterministic=deterministic)
        except StateTransitionError:
            # Reaper/finalizer/current-attempt CAS is authoritative.
            return

    def _fail_if_current(self, lease: Lease, *, deterministic: bool) -> None:
        try:
            self._queue.fail_retryable(
                lease,
                retry_delay_seconds=self._retry_delay_seconds,
                max_attempts=1 if deterministic else self._max_attempts,
            )
        except StateTransitionError:
            return


def _lease_identity(lease: Lease) -> tuple[str, str, str, int]:
    return lease.job_id, lease.attempt_id, lease.worker_id, lease.lease_epoch


def postgres_publication_service_factory(
    repository: PublicationRepository,
    attempt_store: AttemptArtifactStore,
    canonical_store: CanonicalBlobStore,
    validator: ArtifactValidator,
    lease_fence: LeaseFence,
) -> PublicationServiceFactory:
    """Return a small production composition without widening Core contracts."""

    def build(progress_hook: PublicationProgressHook) -> PublicationService:
        return ArtifactPublicationService(
            repository,
            attempt_store,
            canonical_store,
            validator,
            lease_fence,
            progress_hook=progress_hook,
        )

    return build
