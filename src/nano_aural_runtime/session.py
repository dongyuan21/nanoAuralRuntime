"""Model session lifecycle and single-flight coordination."""

from __future__ import annotations

import threading
from enum import Enum
from typing import Any, Optional, Set
from uuid import uuid4

from .errors import SessionStateError
from .models import CancellationToken, ModelDeployment


class SessionState(str, Enum):
    UNLOADED = "unloaded"
    LOADING = "loading"
    READY = "ready"
    RUNNING = "running"
    FAILED = "failed"
    UNLOADING = "unloading"


class ModelSession:
    """Lifecycle container for one loaded model deployment.

    Single-flight semantics are *serialization*: invocations for the same
    session wait for the active invocation to finish.  Invocations for distinct
    sessions may run concurrently.  A cancelled caller leaves the wait queue
    without starting adapter work.

    Only :class:`~nano_aural_runtime.runtime.Runtime` performs the underscore
    lifecycle methods.  Adapters receive a session only to inspect its immutable
    deployment and to associate adapter-local resources externally.
    """

    def __init__(self, deployment: ModelDeployment) -> None:
        if not isinstance(deployment, ModelDeployment):
            raise TypeError("deployment must be a ModelDeployment")
        self._deployment = deployment
        self._session_id = str(uuid4())
        self._state = SessionState.UNLOADED
        self._lifecycle_lock = threading.RLock()
        self._execution_lock = threading.Lock()
        self._runtime: Optional[object] = None
        self._adapter: Optional[Any] = None

    @property
    def deployment(self) -> ModelDeployment:
        return self._deployment

    @property
    def session_id(self) -> str:
        return self._session_id

    @property
    def state(self) -> SessionState:
        with self._lifecycle_lock:
            return self._state

    def _bind(self, runtime: object, adapter: Any) -> None:
        with self._lifecycle_lock:
            if self._runtime is not None:
                raise SessionStateError("session is already bound to a runtime")
            self._runtime = runtime
            self._adapter = adapter

    def _require_runtime(self, runtime: object) -> Any:
        with self._lifecycle_lock:
            if self._runtime is not runtime or self._adapter is None:
                raise SessionStateError("session does not belong to this runtime")
            return self._adapter

    def _transition(self, expected: Set[SessionState], target: SessionState) -> None:
        with self._lifecycle_lock:
            if self._state not in expected:
                expected_values = ", ".join(sorted(state.value for state in expected))
                raise SessionStateError(
                    "cannot transition session from {0} to {1}; expected {2}".format(
                        self._state.value, target.value, expected_values
                    )
                )
            self._state = target

    def _begin_loading(self) -> None:
        self._transition({SessionState.UNLOADED}, SessionState.LOADING)

    def _finish_loading(self) -> None:
        self._transition({SessionState.LOADING}, SessionState.READY)

    def _fail_loading(self) -> None:
        self._transition({SessionState.LOADING}, SessionState.FAILED)

    def _begin_unloading(self) -> None:
        self._transition({SessionState.READY, SessionState.FAILED}, SessionState.UNLOADING)

    def _finish_unloading(self) -> None:
        self._transition({SessionState.UNLOADING}, SessionState.UNLOADED)

    def _fail_unloading(self) -> None:
        self._transition({SessionState.UNLOADING}, SessionState.FAILED)

    def _acquire_invocation(self, cancellation: CancellationToken) -> None:
        """Acquire serialized execution and transition a READY session to RUNNING."""

        cancellation.raise_if_cancelled()
        while not self._execution_lock.acquire(timeout=0.05):
            cancellation.raise_if_cancelled()
        try:
            cancellation.raise_if_cancelled()
            self._transition({SessionState.READY}, SessionState.RUNNING)
        except BaseException:
            self._execution_lock.release()
            raise

    def _finish_invocation(self, failed: bool) -> None:
        try:
            self._transition(
                {SessionState.RUNNING}, SessionState.FAILED if failed else SessionState.READY
            )
        finally:
            self._execution_lock.release()
