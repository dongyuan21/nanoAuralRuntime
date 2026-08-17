"""Adapter protocol, registry, and dependency-free test adapters."""

from __future__ import annotations

from threading import RLock
from typing import Callable, Dict, Optional, Protocol, Tuple, runtime_checkable

from .errors import AdapterNotFoundError, AdapterRegistrationError
from .models import (
    ExecutionContext,
    InvocationResult,
    ModelDescriptor,
    ModelInvocation,
    ProducedArtifact,
)
from .session import ModelSession


@runtime_checkable
class AudioModelAdapter(Protocol):
    """Model-specific implementation behind the generic runtime core."""

    @property
    def descriptor(self) -> ModelDescriptor:
        """Describe the adapter identity and supported capabilities."""

        ...

    def load(self, session: ModelSession) -> None:
        """Allocate resources for ``session.deployment`` without changing its state."""

        ...

    def invoke(
        self,
        session: ModelSession,
        invocation: ModelInvocation,
        context: ExecutionContext,
    ) -> InvocationResult:
        """Interpret one adapter-owned invocation and return artifacts."""

        ...

    def unload(self, session: ModelSession) -> None:
        """Release resources for a non-running session."""

        ...


class AdapterRegistry:
    """Thread-safe mapping from adapter id to a concrete adapter instance."""

    def __init__(self) -> None:
        self._adapters: Dict[str, AudioModelAdapter] = {}
        self._lock = RLock()

    def register(self, adapter: AudioModelAdapter, replace: bool = False) -> None:
        if not isinstance(adapter, AudioModelAdapter):
            raise AdapterRegistrationError("adapter does not implement AudioModelAdapter")
        descriptor = adapter.descriptor
        if not isinstance(descriptor, ModelDescriptor):
            raise AdapterRegistrationError("adapter descriptor must be a ModelDescriptor")
        with self._lock:
            if descriptor.adapter_id in self._adapters and not replace:
                raise AdapterRegistrationError(
                    "adapter id is already registered: {0}".format(descriptor.adapter_id)
                )
            self._adapters[descriptor.adapter_id] = adapter

    def get(self, adapter_id: str) -> AudioModelAdapter:
        with self._lock:
            try:
                return self._adapters[adapter_id]
            except KeyError as error:
                raise AdapterNotFoundError(
                    "no adapter registered: {0}".format(adapter_id)
                ) from error

    def registered_adapter_ids(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._adapters))


class EchoAdapter:
    """A CPU-only adapter that returns a text representation of invocation input."""

    _descriptor = ModelDescriptor(
        adapter_id="echo",
        model_id="echo",
        version="1",
        capabilities={"test_double": True},
    )

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    def load(self, session: ModelSession) -> None:
        return None

    def invoke(
        self,
        session: ModelSession,
        invocation: ModelInvocation,
        context: ExecutionContext,
    ) -> InvocationResult:
        context.cancellation_token.raise_if_cancelled()
        value = invocation.inputs.get("value", invocation.inputs)
        content = str(value).encode("utf-8")
        return InvocationResult(
            invocation_id=invocation.invocation_id,
            artifacts=(
                ProducedArtifact(
                    name="echo.txt",
                    media_type="text/plain",
                    content=content,
                ),
            ),
        )

    def unload(self, session: ModelSession) -> None:
        return None


class FakeAudioAdapter:
    """Configurable CPU test adapter that records lifecycle calls."""

    _descriptor = ModelDescriptor(
        adapter_id="fake",
        model_id="fake",
        version="1",
        capabilities={"test_double": True},
    )

    def __init__(
        self,
        result_factory: Optional[
            Callable[[ModelSession, ModelInvocation, ExecutionContext], InvocationResult]
        ] = None,
    ) -> None:
        self._result_factory = result_factory
        self.load_calls = 0
        self.invoke_calls = 0
        self.unload_calls = 0

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    def load(self, session: ModelSession) -> None:
        self.load_calls += 1

    def invoke(
        self,
        session: ModelSession,
        invocation: ModelInvocation,
        context: ExecutionContext,
    ) -> InvocationResult:
        self.invoke_calls += 1
        context.cancellation_token.raise_if_cancelled()
        if self._result_factory is not None:
            return self._result_factory(session, invocation, context)
        return InvocationResult(
            invocation_id=invocation.invocation_id,
            artifacts=(
                ProducedArtifact(
                    name="fake.bin",
                    media_type="application/octet-stream",
                    content=invocation.invocation_id.encode("utf-8"),
                ),
            ),
        )

    def unload(self, session: ModelSession) -> None:
        self.unload_calls += 1
