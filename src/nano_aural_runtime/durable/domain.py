"""Immutable durable-domain value objects and canonical request hashing."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import Any, Mapping, Optional, Tuple


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, tuple):
        return tuple(_freeze(item) for item in value)
    if isinstance(value, set):
        return frozenset(_freeze(item) for item in value)
    return value


def _require_text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(name))


def _require_sha256(value: str, name: str = "sha256") -> None:
    if not isinstance(value, str) or len(value) != 64:
        raise ValueError("{0} must be a 64-character SHA-256 hex string".format(name))
    if value != value.lower():
        raise ValueError("{0} must be lowercase hexadecimal".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be a SHA-256 hex string".format(name)) from error


def _canonical_json_value(value: Any) -> Any:
    """Return JSON-safe data while rejecting ambiguous/non-deterministic values."""

    if value is None or isinstance(value, (bool, str, int)):
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("canonical request cannot contain non-finite floats")
        return value
    if isinstance(value, Mapping):
        result = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("canonical request mapping keys must be strings")
            result[key] = _canonical_json_value(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    raise TypeError(
        "canonical request contains unsupported value: {0}".format(type(value).__name__)
    )


def canonical_request_sha256(
    request: Mapping[str, Any],
    deployment_id: Optional[str] = None,
    inputs: Tuple["JobInput", ...] = (),
    required_artifact_kinds: Tuple["ArtifactKind", ...] = (),
) -> str:
    """Hash a canonical JSON request for durable idempotency.

    This deliberately hashes parsed request data, not Python object identity or
    untrusted formatting. Callers should apply API defaults before this point.
    """

    if not isinstance(request, Mapping):
        raise TypeError("request must be a mapping")
    normalized = _canonical_json_value(request)
    if deployment_id is not None:
        _require_text(deployment_id, "deployment_id")
        normalized = {
            "deployment_id": deployment_id,
            "request": normalized,
            "inputs": [
                {"role": item.role, "asset_id": item.asset_id}
                for item in sorted(inputs, key=lambda item: item.role)
            ],
            "required_artifact_kinds": sorted(kind.value for kind in required_artifact_kinds),
        }
    payload = json.dumps(
        normalized,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


class DeploymentState(str, Enum):
    REGISTERED = "registered"
    READY = "ready"
    UNHEALTHY = "unhealthy"
    RETIRED = "retired"


class BlobState(str, Enum):
    VERIFIED = "verified"
    QUARANTINED = "quarantined"
    DELETED = "deleted"


class AssetState(str, Enum):
    VERIFIED = "verified"
    REJECTED = "rejected"
    DELETED = "deleted"


class JobState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class AttemptState(str, Enum):
    ACTIVE = "active"
    SUCCEEDED = "succeeded"
    FAILED_TERMINAL = "failed_terminal"
    FAILED_RETRYABLE = "failed_retryable"
    CANCELLED = "cancelled"


class ArtifactState(str, Enum):
    READY = "ready"
    REJECTED = "rejected"


class WorkerState(str, Enum):
    READY = "ready"
    BUSY = "busy"
    UNHEALTHY = "unhealthy"


class EventType(str, Enum):
    JOB_CREATED = "job_created"
    ATTEMPT_STARTED = "attempt_started"
    CANCEL_REQUESTED = "cancel_requested"
    ATTEMPT_CANCELLED = "attempt_cancelled"
    ATTEMPT_FAILED = "attempt_failed"
    ATTEMPT_REQUEUED = "attempt_requeued"
    JOB_SUCCEEDED = "job_succeeded"


class AssetKind(str, Enum):
    INPUT = "input"
    GENERATED = "generated"


class ArtifactKind(str, Enum):
    OUTPUT = "output"
    MANIFEST = "manifest"


@dataclass(frozen=True)
class DeploymentRecord:
    deployment_id: str
    name: str
    adapter_id: str
    fingerprint: str
    state: DeploymentState = DeploymentState.READY
    manifest: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("deployment_id", "name", "adapter_id", "fingerprint"):
            _require_text(getattr(self, name), name)
        object.__setattr__(self, "manifest", _freeze(self.manifest))


@dataclass(frozen=True)
class BlobRecord:
    blob_id: str
    sha256: str
    size_bytes: int
    storage_key: str
    state: BlobState = BlobState.VERIFIED
    content_type: str = "application/octet-stream"

    def __post_init__(self) -> None:
        _require_text(self.blob_id, "blob_id")
        _require_sha256(self.sha256)
        _require_text(self.storage_key, "storage_key")
        if not isinstance(self.size_bytes, int) or self.size_bytes < 0:
            raise ValueError("size_bytes must be a non-negative integer")


@dataclass(frozen=True)
class AssetRecord:
    asset_id: str
    blob_id: str
    namespace_id: str
    kind: AssetKind = AssetKind.INPUT
    state: AssetState = AssetState.VERIFIED
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("asset_id", "blob_id", "namespace_id"):
            _require_text(getattr(self, name), name)
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class JobInput:
    role: str
    asset_id: str

    def __post_init__(self) -> None:
        _require_text(self.role, "role")
        _require_text(self.asset_id, "asset_id")


@dataclass(frozen=True)
class JobRecord:
    job_id: str
    namespace_id: str
    idempotency_key: str
    request_sha256: str
    request: Mapping[str, Any]
    deployment_id: str
    inputs: Tuple[JobInput, ...]
    required_artifact_kinds: Tuple[ArtifactKind, ...] = (ArtifactKind.OUTPUT,)
    state: JobState = JobState.QUEUED
    current_attempt_id: Optional[str] = None
    winning_attempt_id: Optional[str] = None
    cancel_requested: bool = False
    lease_epoch: int = 0

    def __post_init__(self) -> None:
        for name in ("job_id", "namespace_id", "idempotency_key", "deployment_id"):
            _require_text(getattr(self, name), name)
        _require_sha256(self.request_sha256, "request_sha256")
        if not isinstance(self.request, Mapping):
            raise TypeError("request must be a mapping")
        inputs = tuple(self.inputs)
        if not all(isinstance(item, JobInput) for item in inputs):
            raise ValueError("inputs must contain JobInput values")
        if len({item.role for item in inputs}) != len(inputs):
            raise ValueError("job input roles must be unique")
        required = tuple(self.required_artifact_kinds)
        if not required or not all(isinstance(kind, ArtifactKind) for kind in required):
            raise ValueError("required_artifact_kinds must contain ArtifactKind values")
        if len(set(required)) != len(required):
            raise ValueError("required_artifact_kinds must be unique")
        if not isinstance(self.lease_epoch, int) or self.lease_epoch < 0:
            raise ValueError("lease_epoch must be a non-negative integer")
        object.__setattr__(self, "request", _freeze(self.request))
        object.__setattr__(self, "inputs", inputs)
        object.__setattr__(self, "required_artifact_kinds", required)


@dataclass(frozen=True)
class AttemptRecord:
    attempt_id: str
    job_id: str
    worker_id: str
    attempt_no: int
    lease_epoch: int
    state: AttemptState = AttemptState.ACTIVE

    def __post_init__(self) -> None:
        for name in ("attempt_id", "job_id", "worker_id"):
            _require_text(getattr(self, name), name)
        if not isinstance(self.attempt_no, int) or self.attempt_no < 1:
            raise ValueError("attempt_no must be a positive integer")
        if not isinstance(self.lease_epoch, int) or self.lease_epoch < 1:
            raise ValueError("lease_epoch must be a positive integer")


@dataclass(frozen=True)
class ArtifactRecord:
    artifact_id: str
    job_id: str
    attempt_id: str
    blob_id: str
    kind: ArtifactKind = ArtifactKind.OUTPUT
    state: ArtifactState = ArtifactState.READY
    metadata: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("artifact_id", "job_id", "attempt_id", "blob_id"):
            _require_text(getattr(self, name), name)
        object.__setattr__(self, "metadata", _freeze(self.metadata))


@dataclass(frozen=True)
class WorkerRecord:
    worker_id: str
    deployment_id: str
    state: WorkerState = WorkerState.READY

    def __post_init__(self) -> None:
        _require_text(self.worker_id, "worker_id")
        _require_text(self.deployment_id, "deployment_id")


@dataclass(frozen=True)
class JobEventRecord:
    event_id: str
    job_id: str
    event_type: EventType
    attempt_id: Optional[str] = None
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _require_text(self.event_id, "event_id")
        _require_text(self.job_id, "job_id")
        if self.attempt_id is not None:
            _require_text(self.attempt_id, "attempt_id")
        object.__setattr__(self, "payload", _freeze(self.payload))
