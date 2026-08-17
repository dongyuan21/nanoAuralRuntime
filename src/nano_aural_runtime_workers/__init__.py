"""Application integration packages that may depend on Durable, Core, and adapters."""

from .capabilities import WorkerCapability, WorkerCapabilityError, reject_operator_owned_job_fields
from .plugins import (
    CONTROLFOLEY_PLUGIN,
    DEFAULT_PLUGIN_CATALOG,
    STABLE_AUDIO_3_PLUGIN,
    WOOSH_V2A_PLUGIN,
    AdapterPluginCatalog,
    AdapterPluginMetadata,
)
from .registry import (
    BuilderRegistryError,
    DurableInvocationBuilder,
    DurableInvocationBuilderRegistry,
)

__all__ = [
    "CONTROLFOLEY_PLUGIN",
    "DEFAULT_PLUGIN_CATALOG",
    "STABLE_AUDIO_3_PLUGIN",
    "WOOSH_V2A_PLUGIN",
    "AdapterPluginCatalog",
    "AdapterPluginMetadata",
    "BuilderRegistryError",
    "DurableInvocationBuilder",
    "DurableInvocationBuilderRegistry",
    "WorkerCapability",
    "WorkerCapabilityError",
    "reject_operator_owned_job_fields",
]
