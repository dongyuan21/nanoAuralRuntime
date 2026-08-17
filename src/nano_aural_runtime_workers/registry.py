"""Durable invocation builder registry. Lives above Core and below frontends."""

from __future__ import annotations

from threading import RLock
from typing import Dict, Optional, Protocol, Sequence, Tuple

from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime.durable.materialization import MaterializedInput
from nano_aural_runtime.errors import InvocationRejectedError
from nano_aural_runtime.models import ModelDeployment, ModelInvocation

from .capabilities import WorkerCapability, reject_operator_owned_job_fields
from .plugins import DEFAULT_PLUGIN_CATALOG


class DurableInvocationBuilder(Protocol):
    """Adapter-owned binder from durable records to a Core invocation."""

    @property
    def adapter_id(self) -> str: ...

    @property
    def operations(self) -> frozenset: ...

    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment: ...

    def build(
        self,
        deployment: DeploymentRecord,
        job: JobRecord,
        inputs: Sequence[MaterializedInput],
    ) -> ModelInvocation: ...


class BuilderRegistryError(InvocationRejectedError):
    """No legal builder exists for the claimed adapter."""


class DurableInvocationBuilderRegistry:
    """Thread-safe mapping from adapter id to one invocation builder."""

    def __init__(self) -> None:
        self._builders: Dict[str, DurableInvocationBuilder] = {}
        self._lock = RLock()

    def register(self, builder: DurableInvocationBuilder, replace: bool = False) -> None:
        adapter_id = builder.adapter_id
        if not isinstance(adapter_id, str) or not adapter_id.strip():
            raise BuilderRegistryError("builder adapter_id must be a non-empty string")
        operations = builder.operations
        if not isinstance(operations, frozenset) or not operations:
            raise BuilderRegistryError("builder operations must be a non-empty frozenset")
        plugin = DEFAULT_PLUGIN_CATALOG.get(adapter_id)
        if not plugin.implemented:
            raise BuilderRegistryError("adapter frontend is not installed: {0}".format(adapter_id))
        if not operations.issubset(plugin.operations):
            raise BuilderRegistryError("builder operations are not declared by the plugin catalog")
        with self._lock:
            if adapter_id in self._builders and not replace:
                raise BuilderRegistryError("builder already registered: {0}".format(adapter_id))
            self._builders[adapter_id] = builder

    def get(self, adapter_id: str) -> DurableInvocationBuilder:
        with self._lock:
            try:
                return self._builders[adapter_id]
            except KeyError as error:
                raise BuilderRegistryError(
                    "no invocation builder registered: {0}".format(adapter_id)
                ) from error

    def registered_adapter_ids(self) -> Tuple[str, ...]:
        with self._lock:
            return tuple(sorted(self._builders))

    def builder_for(
        self,
        deployment: DeploymentRecord,
        job: Optional[JobRecord] = None,
        capability: Optional[WorkerCapability] = None,
    ) -> DurableInvocationBuilder:
        if capability is not None:
            capability.validate(deployment, job)
        builder = self.get(deployment.adapter_id)
        if job is not None:
            reject_operator_owned_job_fields(job.request)
        return builder
