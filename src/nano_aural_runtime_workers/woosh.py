"""Durable binding from verified jobs to a Woosh V2A invocation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from nano_aural_runtime import InvocationRejectedError, ModelDeployment, ModelInvocation
from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime.durable.materialization import MaterializedInput
from nano_aural_runtime_woosh.adapter import WooshV2AAdapter
from nano_aural_runtime_woosh.baseline import (
    ADAPTER_ID,
    RUNTIME_ENVIRONMENT_ID,
    SUPPORTED_BACKENDS,
    SUPPORTED_SOURCE_REVISION,
    WINDOW_END_SECONDS,
)
from nano_aural_runtime_woosh.tasks import WooshV2ALocalRequest
from nano_aural_runtime_workers.capabilities import reject_operator_owned_job_fields

_OPERATIONS = frozenset({"audio.video_to_sfx"})
_OPERATOR_CONFIGURATION_KEYS = frozenset(
    (
        "manifest_path",
        "deployment_manifest_sha256",
        "source_dir",
        "weights_dir",
        "synchformer_path",
        "source_revision",
        "adapter_id",
        "backend_id",
        "runtime_environment_id",
    )
)
_DURABLE_IDENTITY_KEYS = frozenset(
    (
        "deployment_manifest_sha256",
        "source_revision",
        "adapter_id",
        "backend_id",
        "runtime_environment_id",
    )
)
_REQUIRED_REQUEST_KEYS = frozenset(("seed",))
_OPTIONAL_REQUEST_KEYS = frozenset(("prompt", "operation"))
_FORBIDDEN_REQUEST_KEYS = frozenset(
    (
        "duration_seconds",
        "cfg",
        "guidance_scale",
        "solver",
        "solver_method",
        "renoise",
        "num_steps",
        "backend",
        "backend_id",
        "source_dir",
        "weights_dir",
        "checkpoint_path",
        "synchformer_path",
        "video_path",
        "reference_audio",
        "audio",
    )
)


class WooshV2ADurableBindingError(InvocationRejectedError):
    """The durable deployment or request is not sealed."""


@dataclass(frozen=True)
class WooshV2ADurableInvocationBuilder:
    adapter: WooshV2AAdapter
    operator_configuration: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, WooshV2AAdapter):
            raise TypeError("adapter must be a WooshV2AAdapter")
        configuration = dict(self.operator_configuration)
        if set(configuration) != _OPERATOR_CONFIGURATION_KEYS or not all(
            isinstance(value, str) and value.strip() for value in configuration.values()
        ):
            raise WooshV2ADurableBindingError("operator configuration is not sealed")
        if (
            configuration["source_revision"] != SUPPORTED_SOURCE_REVISION
            or configuration["adapter_id"] != ADAPTER_ID
            or configuration["backend_id"] not in SUPPORTED_BACKENDS
            or configuration["runtime_environment_id"] != RUNTIME_ENVIRONMENT_ID
        ):
            raise WooshV2ADurableBindingError("operator configuration does not match V1 lock")
        digest = configuration["deployment_manifest_sha256"]
        if len(digest) != 64 or digest != digest.lower():
            raise WooshV2ADurableBindingError("operator configuration has an invalid fingerprint")
        object.__setattr__(self, "operator_configuration", MappingProxyType(configuration))

    @property
    def adapter_id(self) -> str:
        return ADAPTER_ID

    @property
    def operations(self) -> frozenset:
        return _OPERATIONS

    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
        configuration = self.operator_configuration
        identity = {name: configuration[name] for name in sorted(_DURABLE_IDENTITY_KEYS)}
        if (
            deployment.adapter_id != self.adapter_id
            or deployment.fingerprint != configuration["deployment_manifest_sha256"]
            or dict(deployment.manifest) != identity
        ):
            raise WooshV2ADurableBindingError(
                "durable deployment identity does not match operator seal"
            )
        return ModelDeployment(
            deployment_id=deployment.deployment_id,
            descriptor=self.adapter.descriptor,
            fingerprint=deployment.fingerprint,
            configuration=configuration,
        )

    def build(
        self,
        deployment: DeploymentRecord,
        job: JobRecord,
        inputs: Sequence[MaterializedInput],
    ) -> ModelInvocation:
        if job.deployment_id != deployment.deployment_id:
            raise WooshV2ADurableBindingError("job deployment does not match claimed deployment")
        video = self._video_input(inputs)
        request = job.request
        if not isinstance(request, Mapping):
            raise WooshV2ADurableBindingError("durable request has missing or unexpected fields")
        keys = set(request)
        if keys & _FORBIDDEN_REQUEST_KEYS:
            raise WooshV2ADurableBindingError("durable request contains operator-owned fields")
        if not _REQUIRED_REQUEST_KEYS.issubset(keys) or not keys.issubset(
            _REQUIRED_REQUEST_KEYS | _OPTIONAL_REQUEST_KEYS
        ):
            raise WooshV2ADurableBindingError("durable request has missing or unexpected fields")
        reject_operator_owned_job_fields(request)
        operation = request.get("operation")
        if operation is not None and operation not in _OPERATIONS:
            raise WooshV2ADurableBindingError("durable request operation is not supported")
        prompt = request.get("prompt")
        try:
            local = WooshV2ALocalRequest(
                video_path=Path(video.path),
                seed=int(request["seed"]),
                prompt=None if prompt is None else str(prompt),
            )
        except (TypeError, ValueError) as error:
            raise WooshV2ADurableBindingError("durable request is invalid") from error
        return local.to_invocation("durable:" + job.job_id)

    @staticmethod
    def _video_input(inputs: Sequence[MaterializedInput]) -> MaterializedInput:
        values = tuple(inputs)
        if len(values) != 1 or values[0].role != "video":
            raise WooshV2ADurableBindingError("video-to-sfx jobs require exactly one video input")
        item = values[0]
        if not isinstance(item, MaterializedInput):
            raise WooshV2ADurableBindingError("durable input is not materialized evidence")
        if item.media is None or item.media.media_type != "video/mp4":
            raise WooshV2ADurableBindingError("video input media is not verified video/mp4")
        if item.media.duration_seconds < WINDOW_END_SECONDS:
            raise WooshV2ADurableBindingError("video shorter than 8 seconds is not supported")
        return item
