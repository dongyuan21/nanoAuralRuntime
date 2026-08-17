"""Phase 3D fenced Runtime worker; publication remains exclusively Phase 3E."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Optional, Protocol, Tuple, cast

from nano_aural_runtime.errors import (
    AdapterExecutionError,
    InvocationCancelledError,
    InvocationRejectedError,
)
from nano_aural_runtime.models import (
    CancellationToken,
    ExecutionContext,
    InvocationResult,
    ModelDeployment,
    ModelInvocation,
)
from nano_aural_runtime.runtime import Runtime
from nano_aural_runtime.session import ModelSession

from .domain import DeploymentRecord, JobRecord
from .errors import StateTransitionError
from .materialization import AttemptInputMaterializer, LeaseAuthority, MaterializedInput
from .queue import Lease, PostgresLeaseQueue
from .upload_transports import CanonicalBlobStore
from .uploads import MediaProbe


class InvocationBuilder(Protocol):
    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment: ...
    def build(
        self, deployment: DeploymentRecord, job: JobRecord, inputs: Tuple[MaterializedInput, ...]
    ) -> ModelInvocation: ...


class ClaimGuard(Protocol):
    """Optional deployment/job filter; implementations live above Durable."""

    def validate(self, deployment: DeploymentRecord, job: Optional[JobRecord] = None) -> None: ...


@dataclass(frozen=True)
class RuntimeCandidate:
    lease: Lease
    result: InvocationResult


class WorkerProcessFatal(AdapterExecutionError):
    """Supervisor must replace this worker; a Python thread ignored cancellation."""


class DurableRuntimeWorker:
    """One deployment/session worker. Successful calls stay process-local."""

    def __init__(
        self,
        queue: PostgresLeaseQueue,
        worker_id: str,
        lease_seconds: int,
        runtime: Runtime,
        builder: InvocationBuilder,
        canonical: CanonicalBlobStore,
        workspace_root: Path,
        probe: MediaProbe,
        monitor_queue_factory: Callable[[], PostgresLeaseQueue],
        guard: Optional[ClaimGuard] = None,
    ) -> None:
        self._queue, self._worker_id, self._lease_seconds = queue, worker_id, lease_seconds
        self._runtime, self._builder = runtime, builder
        self._canonical, self._workspace_root, self._probe = canonical, Path(workspace_root), probe
        self._monitor_queue_factory = monitor_queue_factory
        self._guard = guard
        self._session = None
        self._fatal = False

    def run_once(self) -> Optional[RuntimeCandidate]:
        if self._fatal:
            raise WorkerProcessFatal("worker is permanently fatal and must be replaced")
        lease = self._queue.claim_next(self._worker_id, self._lease_seconds)
        if lease is None:
            return None
        claimed_lease = lease
        token = ExecutionContext().cancellation_token
        try:
            result, lease = self._run_monitored(
                claimed_lease, token, lambda: self._execute_claimed(claimed_lease, token)
            )
            self._queue.assert_current(lease)
            return RuntimeCandidate(lease, result)
        except WorkerProcessFatal:
            raise
        except InvocationRejectedError:
            token.cancel("runtime fault")
            try:
                if self._queue.cancellation_requested(lease):
                    self._queue.cancel_current(lease)
                else:
                    self._queue.fail_retryable(lease, max_attempts=1)
            except StateTransitionError:
                pass
            raise
        except InvocationCancelledError:
            try:
                if self._queue.cancellation_requested(lease):
                    self._queue.cancel_current(lease)
            except StateTransitionError:
                pass
            raise
        except Exception:
            token.cancel("runtime fault")
            self._discard_session()
            try:
                self._queue.fail_retryable(lease)
            except StateTransitionError:
                pass
            raise

    def _execute_claimed(self, lease: Lease, token: CancellationToken) -> InvocationResult:
        """Perform deployment, materialization, load, binding and invoke under monitoring."""
        durable_deployment = self._queue.load_ready_deployment(lease)
        token.raise_if_cancelled()
        if self._guard is not None:
            self._guard.validate(durable_deployment)
        job, attempt, evidence = self._queue.load_verified_input_evidence(lease)
        token.raise_if_cancelled()
        if self._guard is not None:
            self._guard.validate(durable_deployment, job)
        with AttemptInputMaterializer(
            job,
            attempt,
            lease,
            evidence,
            self._canonical,
            cast(LeaseAuthority, self._queue),
            self._workspace_root,
            self._probe,
        ) as materializer:
            token.raise_if_cancelled()
            self._queue.assert_current(lease)
            try:
                deployment = self._builder.core_deployment(durable_deployment)
            except InvocationRejectedError:
                raise
            except (TypeError, ValueError) as error:
                raise InvocationRejectedError("Core deployment binding rejected") from error
            if (
                deployment.deployment_id != durable_deployment.deployment_id
                or deployment.fingerprint != durable_deployment.fingerprint
                or deployment.descriptor.adapter_id != durable_deployment.adapter_id
            ):
                raise InvocationRejectedError(
                    "builder Core deployment does not match fenced durable deployment"
                )
            if self._session is None:
                try:
                    self._session = self._runtime.load(deployment)
                except BaseException as error:
                    self._fatal = True
                    raise WorkerProcessFatal("Runtime.load left process safety unknown") from error
            token.raise_if_cancelled()
            try:
                invocation = self._builder.build(durable_deployment, job, materializer.inputs)
            except InvocationRejectedError:
                raise
            except (TypeError, ValueError) as error:
                raise InvocationRejectedError("invocation binding rejected") from error
            token.raise_if_cancelled()
            return self._runtime.invoke(
                cast(ModelSession, self._session),
                invocation,
                ExecutionContext(cancellation_token=token),
            )
        raise AdapterExecutionError("materialization ended without an invocation")

    def _run_monitored(
        self,
        lease: Lease,
        token: CancellationToken,
        operation: Callable[[], InvocationResult],
    ) -> tuple[InvocationResult, Lease]:
        """Run every post-claim stage while a separate connection owns monitoring."""
        monitor = self._monitor_queue_factory()
        done, outcome = Event(), []

        def execute() -> None:
            try:
                outcome.append(operation())
            except BaseException as error:
                outcome.append(error)
            finally:
                done.set()

        thread = Thread(target=execute, daemon=True)
        thread.start()
        current = lease
        try:
            while not done.wait(0.05):
                try:
                    current = monitor.heartbeat(current, self._lease_seconds)
                except StateTransitionError:
                    token.cancel("lease lost or cancellation requested")
                    if not done.wait(1.0):
                        self._fatal = True
                        raise WorkerProcessFatal(
                            "adapter ignored cancellation; worker process is unsafe"
                        ) from None
                    break
                except BaseException as error:
                    token.cancel("monitor heartbeat failed")
                    done.wait(1.0)
                    self._fatal = True
                    raise WorkerProcessFatal(
                        "monitor heartbeat failed; worker is unsafe"
                    ) from error
        finally:
            try:
                monitor.close()
            except BaseException as error:
                self._fatal = True
                raise WorkerProcessFatal("monitor connection could not close safely") from error
        thread.join()
        if not outcome:
            raise AdapterExecutionError("runtime invocation ended without result")
        if isinstance(outcome[0], BaseException):
            if isinstance(outcome[0], (InvocationRejectedError, InvocationCancelledError)):
                raise outcome[0]
            self._discard_session()
            if isinstance(outcome[0], Exception):
                raise outcome[0]
            raise AdapterExecutionError("runtime worker interrupted") from outcome[0]
        return cast(InvocationResult, outcome[0]), current

    def _discard_session(self) -> None:
        if self._session is None:
            return
        try:
            self._runtime.unload(self._session)
        except BaseException as error:
            self._fatal = True
            raise WorkerProcessFatal("Runtime.unload failed; worker is unsafe") from error
        self._session = None

    def close(self) -> None:
        """Unload a reusable session; failure permanently poisons the worker."""
        self._discard_session()
