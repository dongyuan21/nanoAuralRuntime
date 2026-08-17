"""Torch-free adapter plugin metadata. Discovery must not import model backends."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Optional, Tuple


def _non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(name))
    return value


@dataclass(frozen=True)
class AdapterPluginMetadata:
    """Public adapter identity used by CLI dispatch and Worker routing."""

    adapter_id: str
    frontend: str
    operations: frozenset
    backends: Tuple[str, ...]
    package: str
    implemented: bool
    default_backend: Optional[str] = None

    def __post_init__(self) -> None:
        _non_empty(self.adapter_id, "adapter_id")
        _non_empty(self.frontend, "frontend")
        _non_empty(self.package, "package")
        if not isinstance(self.operations, frozenset) or not self.operations:
            raise ValueError("operations must be a non-empty frozenset")
        for operation in self.operations:
            _non_empty(operation, "operation")
        if not isinstance(self.backends, tuple):
            raise TypeError("backends must be a tuple")
        for backend in self.backends:
            _non_empty(backend, "backend")
        if self.default_backend is not None:
            if self.default_backend not in self.backends:
                raise ValueError("default_backend must be one of backends")
        if not isinstance(self.implemented, bool):
            raise TypeError("implemented must be a bool")


CONTROLFOLEY_PLUGIN = AdapterPluginMetadata(
    adapter_id="controlfoley",
    frontend="controlfoley",
    operations=frozenset({"V2A", "TV2A", "TC-V2A", "AC-V2A", "T2A"}),
    backends=("upstream_parity",),
    package="nano_aural_runtime_controlfoley",
    implemented=True,
    default_backend="upstream_parity",
)

STABLE_AUDIO_3_PLUGIN = AdapterPluginMetadata(
    adapter_id="stable-audio-3-small-sfx",
    frontend="stable-audio-3",
    operations=frozenset({"audio.text_to_sfx"}),
    backends=("pytorch",),
    package="nano_aural_runtime_stable_audio_3",
    implemented=True,
    default_backend="pytorch",
)

WOOSH_V2A_PLUGIN = AdapterPluginMetadata(
    adapter_id="woosh-v2a",
    frontend="woosh",
    operations=frozenset({"audio.video_to_sfx"}),
    backends=("dvflow-8s", "vflow-8s"),
    package="nano_aural_runtime_woosh",
    implemented=True,
    default_backend="dvflow-8s",
)

_PLUGINS: Tuple[AdapterPluginMetadata, ...] = (
    CONTROLFOLEY_PLUGIN,
    STABLE_AUDIO_3_PLUGIN,
    WOOSH_V2A_PLUGIN,
)


class AdapterPluginCatalog:
    """Static catalog of declared adapters. Importing this module is torch-free."""

    def __init__(self, plugins: Tuple[AdapterPluginMetadata, ...] = _PLUGINS) -> None:
        by_id = {}
        by_frontend = {}
        for plugin in plugins:
            if plugin.adapter_id in by_id:
                raise ValueError("duplicate adapter id: {0}".format(plugin.adapter_id))
            if plugin.frontend in by_frontend:
                raise ValueError("duplicate frontend: {0}".format(plugin.frontend))
            by_id[plugin.adapter_id] = plugin
            by_frontend[plugin.frontend] = plugin
        self._by_id: Mapping[str, AdapterPluginMetadata] = MappingProxyType(by_id)
        self._by_frontend: Mapping[str, AdapterPluginMetadata] = MappingProxyType(by_frontend)

    def all_plugins(self) -> Tuple[AdapterPluginMetadata, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))

    def get(self, adapter_id: str) -> AdapterPluginMetadata:
        try:
            return self._by_id[adapter_id]
        except KeyError as error:
            raise KeyError("no adapter plugin: {0}".format(adapter_id)) from error

    def get_frontend(self, frontend: str) -> AdapterPluginMetadata:
        try:
            return self._by_frontend[frontend]
        except KeyError as error:
            raise KeyError("no adapter frontend: {0}".format(frontend)) from error

    def implemented_frontends(self) -> Tuple[str, ...]:
        return tuple(plugin.frontend for plugin in self.all_plugins() if plugin.implemented)


DEFAULT_PLUGIN_CATALOG = AdapterPluginCatalog()
