"""Dependency-free public value objects for the runtime core."""

from __future__ import annotations

from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple

from .errors import InvocationCancelledError


def _freeze(value: Any) -> Any:
    """Copy common container values into immutable equivalents.

    Deployment and invocation metadata often become cache, audit, or idempotency
    inputs in higher layers.  Copying them here prevents callers from mutating a
    value object after it has been accepted by the core.
    """

    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _non_empty(value: str, field_name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(field_name))


@dataclass(frozen=True)
class ModelDescriptor:
    """Static identity and capabilities of a model adapter."""

    adapter_id: str
    model_id: str
    version: str
    capabilities: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty(self.adapter_id, "adapter_id")
        _non_empty(self.model_id, "model_id")
        _non_empty(self.version, "version")
        object.__setattr__(self, "capabilities", _freeze(self.capabilities))


@dataclass(frozen=True)
class ModelDeployment:
    """Immutable configuration selecting one concrete model deployment."""

    deployment_id: str
    descriptor: ModelDescriptor
    fingerprint: str
    configuration: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty(self.deployment_id, "deployment_id")
        _non_empty(self.fingerprint, "fingerprint")
        if not isinstance(self.descriptor, ModelDescriptor):
            raise TypeError("descriptor must be a ModelDescriptor")
        object.__setattr__(self, "configuration", _freeze(self.configuration))


@dataclass(frozen=True)
class ModelInvocation:
    """A model-neutral request passed to an adapter.

    The core deliberately gives the payload no generation-specific shape.  Each
    adapter owns validation and interpretation of its inputs.
    """

    invocation_id: str
    operation: str
    inputs: Mapping[str, Any] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty(self.invocation_id, "invocation_id")
        _non_empty(self.operation, "operation")
        object.__setattr__(self, "inputs", _freeze(self.inputs))
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class ProducedArtifact:
    """One immutable byte payload produced by an invocation."""

    name: str
    media_type: str
    content: bytes
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _non_empty(self.name, "name")
        _non_empty(self.media_type, "media_type")
        if not isinstance(self.content, bytes):
            raise TypeError("content must be bytes")
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class ProfileReport:
    """Optional, model-neutral measurements collected during one invocation.

    The Runtime Core only carries this immutable value.  It neither measures
    stages nor imposes a timing backend; adapters may attach namespaced metrics
    when an observability layer is introduced.
    """

    metrics: Mapping[str, float] = field(default_factory=dict)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in self.metrics.items():
            _non_empty(name, "profile metric name")
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise TypeError("profile metrics must be numeric")
        object.__setattr__(self, "metrics", _freeze(self.metrics))
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class CacheReport:
    """Optional, model-neutral cache observations for one invocation.

    Cache implementation and policy intentionally remain outside the Runtime
    Core.  The counters have zero defaults so every invocation has a stable,
    empty report before cache work starts in a later phase.
    """

    hits: int = 0
    misses: int = 0
    bytes_used: int = 0
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name, value in (
            ("hits", self.hits),
            ("misses", self.misses),
            ("bytes_used", self.bytes_used),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{0} must be a non-negative integer".format(name))
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class InvocationResult:
    """The completed result of one invocation, including zero or more artifacts."""

    invocation_id: str
    artifacts: Tuple[ProducedArtifact, ...] = field(default_factory=tuple)
    metadata: Mapping[str, Any] = field(default_factory=dict)
    warnings: Tuple[str, ...] = field(default_factory=tuple)
    profile: ProfileReport = field(default_factory=ProfileReport)
    cache: CacheReport = field(default_factory=CacheReport)

    def __post_init__(self) -> None:
        _non_empty(self.invocation_id, "invocation_id")
        artifacts = tuple(self.artifacts)
        if not all(isinstance(artifact, ProducedArtifact) for artifact in artifacts):
            raise TypeError("artifacts must contain ProducedArtifact values")
        if not all(isinstance(warning, str) for warning in self.warnings):
            raise TypeError("warnings must contain strings")
        if not isinstance(self.profile, ProfileReport):
            raise TypeError("profile must be a ProfileReport")
        if not isinstance(self.cache, CacheReport):
            raise TypeError("cache must be a CacheReport")
        object.__setattr__(self, "artifacts", artifacts)
        object.__setattr__(self, "metadata", _freeze(self.metadata))
        object.__setattr__(self, "warnings", tuple(self.warnings))


class CancellationToken:
    """A thread-safe, cooperative cancellation signal.

    Adapters should call :meth:`raise_if_cancelled` at safe interruption points.
    """

    def __init__(self) -> None:
        import threading

        self._event = threading.Event()
        self._reason: Optional[str] = None
        self._lock = threading.Lock()

    def cancel(self, reason: Optional[str] = None) -> bool:
        """Cancel once and return whether this call changed the token."""

        with self._lock:
            if self._event.is_set():
                return False
            self._reason = reason
            self._event.set()
            return True

    @property
    def reason(self) -> Optional[str]:
        with self._lock:
            return self._reason

    def is_cancelled(self) -> bool:
        return self._event.is_set()

    def raise_if_cancelled(self) -> None:
        if self.is_cancelled():
            detail = "invocation cancelled"
            if self.reason:
                detail = "{0}: {1}".format(detail, self.reason)
            raise InvocationCancelledError(detail)


@dataclass(frozen=True)
class ExecutionContext:
    """Execution-scoped cross-cutting data, kept independent of any frontend."""

    request_id: Optional[str] = None
    cancellation_token: CancellationToken = field(default_factory=CancellationToken)
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.request_id is not None:
            _non_empty(self.request_id, "request_id")
        if not isinstance(self.cancellation_token, CancellationToken):
            raise TypeError("cancellation_token must be a CancellationToken")
        object.__setattr__(self, "metadata", _freeze(self.metadata))
