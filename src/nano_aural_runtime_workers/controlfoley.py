"""Worker/application binding from fenced durable inputs to a ControlFoley invocation.

This integration layer, not the ControlFoley adapter package, depends on both
Durable and the adapter.  It accepts only a durable job record plus
server-created :class:`MaterializedInput` values and produces the existing
ControlFoley local request/``ModelInvocation``.  It never claims a lease,
invokes an adapter, publishes an artifact, or changes durable state.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Protocol, Sequence

from nano_aural_runtime import InvocationRejectedError, ModelDeployment, ModelInvocation
from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime.durable.materialization import MaterializedInput
from nano_aural_runtime_controlfoley.adapter import ControlFoleyAdapter
from nano_aural_runtime_controlfoley.tasks import ControlFoleyLocalRequest, ControlFoleyTaskKind


class ControlFoleyDurableBindingError(InvocationRejectedError):
    """The durable deployment, request, or materialized inputs are not sealed."""


class InvocationBuilder(Protocol):
    """Application-side input to the generic P3D ``DurableRuntimeWorker``."""

    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment: ...

    def build(
        self,
        deployment: DeploymentRecord,
        job: JobRecord,
        inputs: Sequence[MaterializedInput],
    ) -> ModelInvocation: ...


_OPERATOR_CONFIGURATION_KEYS = frozenset(
    (
        "manifest_path",
        "deployment_manifest_sha256",
        "source_dir",
        "weights_dir",
        "upstream_repository",
        "source_revision",
        "variant",
        "precision",
        "checkpoint_sha256",
    )
)
_DURABLE_IDENTITY_KEYS = frozenset(
    (
        "deployment_manifest_sha256",
        "upstream_repository",
        "source_revision",
        "variant",
        "precision",
        "checkpoint_sha256",
    )
)
_REQUEST_KEYS = frozenset(
    (
        "task",
        "prompt",
        "duration_seconds",
        "num_steps",
        "guidance_scale",
        "seed",
    )
)
_BINARY_ROLES = {
    ControlFoleyTaskKind.V2A: frozenset(("video",)),
    ControlFoleyTaskKind.TV2A: frozenset(("video",)),
    ControlFoleyTaskKind.TC_V2A: frozenset(("video",)),
    ControlFoleyTaskKind.AC_V2A: frozenset(("video", "reference_audio")),
    ControlFoleyTaskKind.T2A: frozenset(),
}


@dataclass(frozen=True)
class ControlFoleyDurableInvocationBuilder:
    """Sealed operator configuration and DB-to-adapter invocation binding.

    ``operator_configuration`` is a process-start, operator-only value.  Its
    source/weights paths never come from ``JobRecord.request`` and are omitted
    from the durable deployment manifest identity.
    """

    adapter: ControlFoleyAdapter
    operator_configuration: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.adapter, ControlFoleyAdapter):
            raise TypeError("adapter must be a ControlFoleyAdapter")
        configuration = dict(self.operator_configuration)
        if set(configuration) != _OPERATOR_CONFIGURATION_KEYS or not all(
            isinstance(value, str) and value.strip() for value in configuration.values()
        ):
            raise ControlFoleyDurableBindingError("operator configuration is not sealed")
        if configuration["variant"] != "large_44k" or configuration["precision"] != "fp32":
            raise ControlFoleyDurableBindingError(
                "only upstream_parity large_44k fp32 is supported"
            )
        for field in ("deployment_manifest_sha256", "checkpoint_sha256"):
            value = configuration[field]
            if len(value) != 64 or value != value.lower():
                raise ControlFoleyDurableBindingError(
                    "operator configuration has an invalid fingerprint"
                )
            try:
                int(value, 16)
            except ValueError as error:
                raise ControlFoleyDurableBindingError(
                    "operator configuration has an invalid fingerprint"
                ) from error
        object.__setattr__(self, "operator_configuration", MappingProxyType(configuration))

    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
        """Bind operator-only configuration exactly to a durable deployment row."""

        configuration = self.operator_configuration
        identity = {name: configuration[name] for name in sorted(_DURABLE_IDENTITY_KEYS)}
        if (
            deployment.adapter_id != self.adapter.descriptor.adapter_id
            or deployment.fingerprint != configuration["deployment_manifest_sha256"]
            or dict(deployment.manifest) != identity
        ):
            raise ControlFoleyDurableBindingError(
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
        """Build exactly one adapter-owned invocation from DB/request evidence."""

        self._assert_job_deployment(deployment, job)
        values = self._request_values(job.request)
        task = self._task(values["task"])
        materialized = self._materialized_roles(inputs, _BINARY_ROLES[task])
        request = ControlFoleyLocalRequest(
            task=task,
            video_path=self._role_path(materialized, "video"),
            reference_audio_path=self._role_path(materialized, "reference_audio"),
            prompt=values["prompt"],
            duration_seconds=values["duration_seconds"],
            num_steps=values["num_steps"],
            guidance_scale=values["guidance_scale"],
            seed=values["seed"],
        )
        return request.to_invocation("durable:" + job.job_id)

    @staticmethod
    def _assert_job_deployment(deployment: DeploymentRecord, job: JobRecord) -> None:
        if job.deployment_id != deployment.deployment_id:
            raise ControlFoleyDurableBindingError(
                "job deployment does not match claimed deployment"
            )

    @staticmethod
    def _request_values(request: Mapping[str, Any]) -> Mapping[str, Any]:
        if not isinstance(request, Mapping) or set(request) != _REQUEST_KEYS:
            raise ControlFoleyDurableBindingError(
                "durable request has missing or unexpected fields"
            )
        # Paths, source/weight locations, ETags, and any adapter process controls
        # are consequently rejected before a local request can be constructed.
        return request

    @staticmethod
    def _task(value: object) -> ControlFoleyTaskKind:
        try:
            return ControlFoleyTaskKind(value)
        except (TypeError, ValueError) as error:
            raise ControlFoleyDurableBindingError("durable request task is invalid") from error

    @classmethod
    def _materialized_roles(
        cls, inputs: Sequence[MaterializedInput], expected_roles: frozenset[str]
    ) -> Mapping[str, MaterializedInput]:
        values = tuple(inputs)
        by_role = {item.role: item for item in values}
        if len(by_role) != len(values) or set(by_role) != expected_roles:
            raise ControlFoleyDurableBindingError("durable materialized roles do not match task")
        for role, item in by_role.items():
            # ``MaterializedInput`` is an internal server-produced value from
            # the P3C materializer. This integration does not accept request
            # paths or re-interpret paths.
            if not isinstance(item, MaterializedInput):
                raise ControlFoleyDurableBindingError("durable input is not materialized evidence")
            if role == "video" and (item.media is None or item.media.media_type != "video/mp4"):
                raise ControlFoleyDurableBindingError("video input media is not verified video/mp4")
            if role == "reference_audio" and (
                item.media is None or not item.media.media_type.startswith("audio/")
            ):
                raise ControlFoleyDurableBindingError("reference input media is not verified audio")
        return by_role

    @staticmethod
    def _role_path(values: Mapping[str, MaterializedInput], role: str) -> Path | None:
        item = values.get(role)
        return None if item is None else item.path
