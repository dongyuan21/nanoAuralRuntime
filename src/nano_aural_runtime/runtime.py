"""Runtime orchestration for adapter-owned model sessions."""

from __future__ import annotations

from typing import Optional

from .adapters import AdapterRegistry
from .errors import (
    AdapterContractError,
    AdapterExecutionError,
    InvocationCancelledError,
    InvocationRejectedError,
)
from .models import ExecutionContext, InvocationResult, ModelDeployment, ModelInvocation
from .session import ModelSession


class Runtime:
    """Owns adapter lookup and enforces session lifecycle invariants.

    ``invoke`` uses serialized single-flight semantics for a session: concurrent
    callers wait, then execute one at a time.  A caller whose cancellation token
    is cancelled while waiting raises ``InvocationCancelledError`` and does not
    call the adapter.  Different sessions are intentionally independent.
    """

    def __init__(self, registry: Optional[AdapterRegistry] = None) -> None:
        self._registry = registry or AdapterRegistry()

    @property
    def registry(self) -> AdapterRegistry:
        return self._registry

    def load(self, deployment: ModelDeployment) -> ModelSession:
        if not isinstance(deployment, ModelDeployment):
            raise TypeError("deployment must be a ModelDeployment")
        adapter = self._registry.get(deployment.descriptor.adapter_id)
        if adapter.descriptor.adapter_id != deployment.descriptor.adapter_id:
            raise AdapterContractError("registered adapter id does not match deployment descriptor")

        session = ModelSession(deployment)
        session._bind(self, adapter)
        session._begin_loading()
        try:
            adapter.load(session)
        except BaseException:
            # A process interruption must not strand a session in LOADING.  The
            # original exception (including KeyboardInterrupt/SystemExit) is
            # deliberately re-raised unchanged after making the state safe.
            session._fail_loading()
            raise
        session._finish_loading()
        return session

    def invoke(
        self,
        session: ModelSession,
        invocation: ModelInvocation,
        context: Optional[ExecutionContext] = None,
    ) -> InvocationResult:
        if not isinstance(session, ModelSession):
            raise TypeError("session must be a ModelSession")
        if not isinstance(invocation, ModelInvocation):
            raise TypeError("invocation must be a ModelInvocation")
        execution_context = context or ExecutionContext()
        if not isinstance(execution_context, ExecutionContext):
            raise TypeError("context must be an ExecutionContext")

        adapter = session._require_runtime(self)
        session._acquire_invocation(execution_context.cancellation_token)
        try:
            result = adapter.invoke(session, invocation, execution_context)
            execution_context.cancellation_token.raise_if_cancelled()
            if not isinstance(result, InvocationResult):
                raise AdapterContractError("adapter.invoke must return InvocationResult")
            if result.invocation_id != invocation.invocation_id:
                raise AdapterContractError("adapter result invocation_id does not match invocation")
        except (InvocationCancelledError, InvocationRejectedError):
            session._finish_invocation(failed=False)
            raise
        except (AdapterContractError, AdapterExecutionError):
            session._finish_invocation(failed=True)
            raise
        except Exception as error:
            session._finish_invocation(failed=True)
            raise AdapterExecutionError("adapter invocation failed") from error
        except BaseException:
            # BaseException is intentional here: even an interruption must
            # release the single-flight lock and prevent reuse of an execution
            # whose adapter work may have been interrupted mid-flight.
            session._finish_invocation(failed=True)
            raise
        session._finish_invocation(failed=False)
        return result

    def unload(self, session: ModelSession) -> None:
        if not isinstance(session, ModelSession):
            raise TypeError("session must be a ModelSession")
        adapter = session._require_runtime(self)
        session._begin_unloading()
        try:
            adapter.unload(session)
        except BaseException:
            # Preserve an interruption while recording that resources may only
            # have been partially released.
            session._fail_unloading()
            raise
        session._finish_unloading()
