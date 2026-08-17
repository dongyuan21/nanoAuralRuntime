"""Worker capability descriptors and deployment-aware claim matching."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Mapping, Optional, Tuple

from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime.errors import InvocationRejectedError

from .plugins import DEFAULT_PLUGIN_CATALOG


def _non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(name))
    return value


def _sha256(value: str, name: str) -> str:
    _non_empty(value, name)
    if len(value) != 64 or value != value.lower():
        raise ValueError("{0} must be a lowercase SHA-256 hex string".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be a SHA-256 hex string".format(name)) from error
    return value


class WorkerCapabilityError(InvocationRejectedError):
    """The Worker cannot legally execute the claimed Deployment."""


@dataclass(frozen=True)
class WorkerCapability:
    """Operator-owned readiness document for one isolated Worker process."""

    runtime_environment_id: str
    adapter_id: str
    supported_operations: frozenset
    supported_backends: Tuple[str, ...]
    source_revision: str
    checkpoint_manifest_sha256: str
    device: str
    max_concurrency: int = 1
    synchformer_sha256: Optional[str] = None

    def __post_init__(self) -> None:
        _non_empty(self.runtime_environment_id, "runtime_environment_id")
        _non_empty(self.adapter_id, "adapter_id")
        _non_empty(self.source_revision, "source_revision")
        _sha256(self.checkpoint_manifest_sha256, "checkpoint_manifest_sha256")
        _non_empty(self.device, "device")
        if not isinstance(self.supported_operations, frozenset) or not self.supported_operations:
            raise ValueError("supported_operations must be a non-empty frozenset")
        for operation in self.supported_operations:
            _non_empty(operation, "operation")
        if not isinstance(self.supported_backends, tuple) or not self.supported_backends:
            raise ValueError("supported_backends must be a non-empty tuple")
        for backend in self.supported_backends:
            _non_empty(backend, "backend")
        if (
            isinstance(self.max_concurrency, bool)
            or not isinstance(self.max_concurrency, int)
            or self.max_concurrency != 1
        ):
            raise ValueError("max_concurrency must be 1")
        if self.synchformer_sha256 is not None:
            _sha256(self.synchformer_sha256, "synchformer_sha256")

    def validate(self, deployment: DeploymentRecord, job: Optional[JobRecord] = None) -> None:
        """Fail closed when the Worker cannot execute this Deployment/Job."""

        plugin = DEFAULT_PLUGIN_CATALOG.get(self.adapter_id)
        if deployment.adapter_id != self.adapter_id:
            raise WorkerCapabilityError("worker adapter_id does not match deployment")
        if deployment.fingerprint != self.checkpoint_manifest_sha256:
            raise WorkerCapabilityError("worker fingerprint does not match deployment")
        manifest = dict(deployment.manifest)
        environment = manifest.get("runtime_environment_id")
        if environment is not None and environment != self.runtime_environment_id:
            raise WorkerCapabilityError("worker environment does not match deployment")
        backend = manifest.get("backend_id")
        if backend is None:
            if (
                len(self.supported_backends) != 1
                or self.supported_backends[0] != plugin.default_backend
            ):
                raise WorkerCapabilityError("deployment backend_id is required for this worker")
        elif backend not in self.supported_backends:
            raise WorkerCapabilityError("worker does not support deployment backend")
        if job is not None:
            self.validate_job(job)

    def validate_job(self, job: JobRecord) -> None:
        request = dict(job.request)
        reject_operator_owned_job_fields(request)
        operation = request.get("operation")
        task = request.get("task")
        selected = operation if isinstance(operation, str) else task
        if not isinstance(selected, str) or selected not in self.supported_operations:
            raise WorkerCapabilityError("job operation is not supported by this worker")


_OPERATOR_OWNED_JOB_FIELDS = frozenset(
    (
        "source_dir",
        "weights_dir",
        "checkpoint_path",
        "backend_path",
        "python_module",
        "solver",
        "rtol",
        "atol",
        "renoise",
        "runtime_environment_id",
        "backend_id",
        "synchformer_sha256",
    )
)


def reject_operator_owned_job_fields(request: Mapping[str, object]) -> None:
    """Remote Jobs cannot carry operator-owned paths or sampler policy."""

    leaked = _OPERATOR_OWNED_JOB_FIELDS.intersection(request)
    if leaked:
        raise WorkerCapabilityError("job request contains operator-owned fields")
