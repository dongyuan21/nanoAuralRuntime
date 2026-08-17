"""Durable binding from verified jobs to a Stable Audio 3 Small-SFX invocation."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Any, Mapping, Sequence

from nano_aural_runtime import InvocationRejectedError, ModelDeployment, ModelInvocation
from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime.durable.materialization import MaterializedInput
from nano_aural_runtime_stable_audio_3.adapter import StableAudio3Adapter
from nano_aural_runtime_stable_audio_3.baseline import (
    RUNTIME_ENVIRONMENT_ID,
    SUPPORTED_HF_REPOSITORY,
    SUPPORTED_MODEL_ID,
    SUPPORTED_SOURCE_REVISION,
)
from nano_aural_runtime_stable_audio_3.tasks import StableAudio3LocalRequest
from nano_aural_runtime_workers.capabilities import reject_operator_owned_job_fields

_OPERATIONS = frozenset({"audio.text_to_sfx"})
_OPERATOR_CONFIGURATION_KEYS = frozenset(
    (
        "manifest_path",
        "deployment_manifest_sha256",
        "source_dir",
        "weights_dir",
        "source_revision",
        "model_id",
        "hf_repository",
        "runtime_environment_id",
    )
)
_DURABLE_IDENTITY_KEYS = frozenset(
    (
        "deployment_manifest_sha256",
        "source_revision",
        "model_id",
        "hf_repository",
        "runtime_environment_id",
    )
)
_REQUEST_KEYS = frozenset(("prompt", "duration_seconds", "seed"))


class StableAudio3DurableBindingError(InvocationRejectedError):
    """The durable deployment or request is not sealed."""


@dataclass(frozen=True)
class StableAudio3DurableInvocationBuilder:
    adapter: StableAudio3Adapter
    operator_configuration: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, StableAudio3Adapter):
            raise TypeError("adapter must be a StableAudio3Adapter")
        configuration = dict(self.operator_configuration)
        if set(configuration) != _OPERATOR_CONFIGURATION_KEYS or not all(
            isinstance(value, str) and value.strip() for value in configuration.values()
        ):
            raise StableAudio3DurableBindingError("operator configuration is not sealed")
        if (
            configuration["source_revision"] != SUPPORTED_SOURCE_REVISION
            or configuration["model_id"] != SUPPORTED_MODEL_ID
            or configuration["hf_repository"] != SUPPORTED_HF_REPOSITORY
            or configuration["runtime_environment_id"] != RUNTIME_ENVIRONMENT_ID
        ):
            raise StableAudio3DurableBindingError("operator configuration does not match V1 lock")
        digest = configuration["deployment_manifest_sha256"]
        if len(digest) != 64 or digest != digest.lower():
            raise StableAudio3DurableBindingError(
                "operator configuration has an invalid fingerprint"
            )
        object.__setattr__(self, "operator_configuration", MappingProxyType(configuration))

    @property
    def adapter_id(self) -> str:
        return "stable-audio-3-small-sfx"

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
            raise StableAudio3DurableBindingError(
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
            raise StableAudio3DurableBindingError(
                "job deployment does not match claimed deployment"
            )
        if tuple(inputs):
            raise StableAudio3DurableBindingError("text-to-sfx jobs must not include binary inputs")
        request = job.request
        if not isinstance(request, Mapping) or set(request) != _REQUEST_KEYS:
            raise StableAudio3DurableBindingError(
                "durable request has missing or unexpected fields"
            )
        reject_operator_owned_job_fields(request)
        try:
            local = StableAudio3LocalRequest(
                prompt=str(request["prompt"]),
                duration_seconds=float(request["duration_seconds"]),
                seed=int(request["seed"]),
            )
        except (TypeError, ValueError) as error:
            raise StableAudio3DurableBindingError("durable request is invalid") from error
        return local.to_invocation("durable:" + job.job_id)
