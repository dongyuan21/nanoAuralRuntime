"""Typed errors exposed by the model-agnostic runtime core."""


class NanoAuralRuntimeError(Exception):
    """Base class for all errors raised by this package."""


class AdapterRegistrationError(NanoAuralRuntimeError):
    """Raised when an adapter cannot be registered safely."""


class AdapterNotFoundError(NanoAuralRuntimeError):
    """Raised when no adapter is registered for a deployment."""


class AdapterContractError(NanoAuralRuntimeError):
    """Raised when an adapter violates the runtime contract."""


class SessionStateError(NanoAuralRuntimeError):
    """Raised when a lifecycle operation is invalid for a session state."""


class InvocationError(NanoAuralRuntimeError):
    """Base class for errors which occur while processing an invocation."""


class InvocationRejectedError(InvocationError):
    """An invocation was invalid or unsupported, but the session remains usable."""


class InvocationCancelledError(InvocationError):
    """An invocation was cancelled cooperatively."""


class AdapterExecutionError(InvocationError):
    """An adapter encountered a non-recoverable execution failure."""
