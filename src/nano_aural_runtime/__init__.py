"""nanoAuralRuntime's dependency-free, model-agnostic Runtime Core."""

from .adapters import AdapterRegistry, AudioModelAdapter, EchoAdapter, FakeAudioAdapter
from .errors import (
    AdapterContractError,
    AdapterExecutionError,
    AdapterNotFoundError,
    AdapterRegistrationError,
    InvocationCancelledError,
    InvocationError,
    InvocationRejectedError,
    NanoAuralRuntimeError,
    SessionStateError,
)
from .models import (
    CacheReport,
    CancellationToken,
    ExecutionContext,
    InvocationResult,
    ModelDeployment,
    ModelDescriptor,
    ModelInvocation,
    ProducedArtifact,
    ProfileReport,
)
from .runtime import Runtime
from .session import ModelSession, SessionState

__all__ = [
    "AdapterContractError",
    "AdapterExecutionError",
    "AdapterNotFoundError",
    "AdapterRegistrationError",
    "AdapterRegistry",
    "AudioModelAdapter",
    "CacheReport",
    "CancellationToken",
    "EchoAdapter",
    "ExecutionContext",
    "FakeAudioAdapter",
    "InvocationCancelledError",
    "InvocationError",
    "InvocationRejectedError",
    "InvocationResult",
    "ModelDeployment",
    "ModelDescriptor",
    "ModelInvocation",
    "ModelSession",
    "NanoAuralRuntimeError",
    "ProducedArtifact",
    "ProfileReport",
    "Runtime",
    "SessionState",
    "SessionStateError",
]
