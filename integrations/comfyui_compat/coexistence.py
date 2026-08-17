"""Dependency-free coexistence and workflow diagnostics for Roadmap Phase 5C."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Mapping, Optional, Sequence, Tuple, cast


class ComfyUICoexistenceError(RuntimeError):
    """An actionable refusal to silently mix plugin or model origins."""


# Contract snapshot from the official ControlFoley ComfyUI plugin's public
# NODE_CLASS_MAPPINGS. The plugin itself is never imported by this package.
OFFICIAL_CONTROLFOLEY_NODE_NAMES = frozenset(
    (
        "LoadControlFoleyDependencies",
        "LoadControlFoleyModel",
        "ControlFoleyTorchCompile",
        "LoadControlFoleyVideo",
        "ControlFoleyGenerate",
        "ControlFoleyGenerateAdvanced",
        "ControlFoleySimpleGenerate",
        "SaveControlFoleyAudio",
        "MuxControlFoleyAudioToVideo",
        "UnloadControlFoleyModel",
    )
)


@dataclass(frozen=True)
class ModuleOrigin:
    module_name: str
    path: Path


@dataclass(frozen=True)
class CoexistenceReport:
    expected_source_dir: Path
    official_plugin_origins: Tuple[ModuleOrigin, ...]
    loaded_upstream_origins: Tuple[ModuleOrigin, ...]
    official_node_names: Tuple[str, ...]
    embedded_node_names: Tuple[str, ...]
    remote_node_names: Tuple[str, ...]


@dataclass(frozen=True)
class WorkflowReport:
    label: str
    node_types: Tuple[str, ...]
    output_node_types: Tuple[str, ...]
    link_count: int


def _module_origin(name: str, module: object, *, role: str) -> ModuleOrigin:
    if not isinstance(module, ModuleType):
        raise ComfyUICoexistenceError(
            "{0} module {1} is not a Python module. Restart ComfyUI and reinstall "
            "or disable the conflicting custom node.".format(role, name)
        )
    raw = getattr(module, "__file__", None)
    if not isinstance(raw, str) or not raw:
        raise ComfyUICoexistenceError(
            "{0} module {1} has no inspectable origin. Restart ComfyUI and load only "
            "one ControlFoley plugin/source pairing.".format(role, name)
        )
    try:
        path = Path(raw).resolve()
    except OSError:
        raise ComfyUICoexistenceError(
            "{0} module {1} has an unreadable origin. Restart ComfyUI and verify the "
            "custom-node installation.".format(role, name)
        ) from None
    return ModuleOrigin(name, path)


def _is_upstream_module(name: str) -> bool:
    return name in {"controlfoley", "lib.flow_matching"} or name.startswith("controlfoley.")


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _mapping_names(label: str, mapping: Mapping[str, object]) -> Tuple[str, ...]:
    if not isinstance(mapping, Mapping) or not mapping:
        raise ComfyUICoexistenceError("{0} NODE_CLASS_MAPPINGS is empty or invalid".format(label))
    if not all(
        isinstance(name, str) and name and isinstance(value, type)
        for name, value in mapping.items()
    ):
        raise ComfyUICoexistenceError(
            "{0} NODE_CLASS_MAPPINGS must contain non-empty names and node classes".format(label)
        )
    return tuple(sorted(mapping))


def inspect_controlfoley_coexistence(
    source_dir: Path,
    official_node_mappings: Mapping[str, object],
    embedded_node_mappings: Mapping[str, object],
    remote_node_mappings: Mapping[str, object],
    *,
    official_plugin_modules: Mapping[str, object],
    loaded_modules: Optional[Mapping[str, object]] = None,
) -> CoexistenceReport:
    """Validate one official/NanoAural host without importing either plugin.

    Official plugin wrapper modules may live under their own custom-node
    directory. Any already-loaded *upstream* ``controlfoley`` module must,
    however, resolve beneath the exact source tree sealed for the embedded
    route. A mismatch is refused before another model session can be loaded.
    """

    expected = Path(source_dir).resolve()
    if not expected.is_dir():
        raise ComfyUICoexistenceError(
            "configured ControlFoley source is unavailable; configure the official plugin "
            "and NanoAural Embedded to the same checkout before starting ComfyUI"
        )
    official_names = _mapping_names("official ControlFoley", official_node_mappings)
    embedded_names = _mapping_names("NanoAural Embedded", embedded_node_mappings)
    remote_names = _mapping_names("NanoAural Remote", remote_node_mappings)
    missing = sorted(OFFICIAL_CONTROLFOLEY_NODE_NAMES - set(official_names))
    if missing:
        raise ComfyUICoexistenceError(
            "official ControlFoley node contract is incomplete ({0}); verify the supported "
            "plugin revision before running the A/B workflow".format(", ".join(missing))
        )

    owners: dict[str, list[str]] = {}
    for label, names in (
        ("official ControlFoley", official_names),
        ("NanoAural Embedded", embedded_names),
        ("NanoAural Remote", remote_names),
    ):
        for name in names:
            owners.setdefault(name, []).append(label)
    collisions = tuple(sorted(name for name, labels in owners.items() if len(labels) > 1))
    if collisions:
        raise ComfyUICoexistenceError(
            "ComfyUI node-name conflict ({0}); disable or rename the conflicting custom "
            "node and restart ComfyUI. Silent NODE_CLASS_MAPPINGS replacement is refused.".format(
                ", ".join(collisions)
            )
        )

    if not isinstance(official_plugin_modules, Mapping) or not official_plugin_modules:
        raise ComfyUICoexistenceError(
            "official ControlFoley plugin origin is unavailable; verify its custom-node "
            "installation before running the A/B workflow"
        )
    official_origins = tuple(
        _module_origin(name, module, role="official plugin")
        for name, module in sorted(official_plugin_modules.items())
    )

    candidates = sys.modules if loaded_modules is None else loaded_modules
    upstream_origins = []
    for name, module in sorted(candidates.items()):
        if not _is_upstream_module(name):
            continue
        origin = _module_origin(name, module, role="loaded upstream")
        if not _is_within(origin.path, expected):
            raise ComfyUICoexistenceError(
                "ControlFoley module-origin conflict for {0}: loaded from {1}, expected "
                "beneath {2}. Restart ComfyUI, set CONTROLFOLEY_SOURCE_DIR for the official "
                "plugin, and set NanoAural's sealed source_dir to the same checkout before "
                "loading either node family.".format(name, origin.path, expected)
            )
        upstream_origins.append(origin)

    return CoexistenceReport(
        expected,
        official_origins,
        tuple(upstream_origins),
        official_names,
        embedded_names,
        remote_names,
    )


def _records(value: object, field: str) -> Sequence[Mapping[str, object]]:
    if not isinstance(value, list) or not all(isinstance(item, Mapping) for item in value):
        raise ComfyUICoexistenceError("workflow {0} must be a list of objects".format(field))
    return cast(Sequence[Mapping[str, object]], value)


def validate_workflow_schema(
    workflow: Mapping[str, object],
    node_mappings: Mapping[str, object],
    *,
    label: str,
) -> WorkflowReport:
    """Validate the shared ComfyUI workflow shape without importing ComfyUI."""

    if not isinstance(workflow, Mapping):
        raise ComfyUICoexistenceError("{0} workflow must be an object".format(label))
    _mapping_names(label, node_mappings)
    nodes = _records(workflow.get("nodes"), "nodes")
    raw_links = workflow.get("links")
    if not isinstance(raw_links, list) or not all(
        isinstance(link, (list, tuple)) and len(link) == 6 for link in raw_links
    ):
        raise ComfyUICoexistenceError("workflow links must use the standard six-field shape")

    node_ids: set[int] = set()
    node_types = []
    output_types = []
    for node in nodes:
        node_id, node_type = node.get("id"), node.get("type")
        if (
            isinstance(node_id, bool)
            or not isinstance(node_id, int)
            or node_id in node_ids
            or not isinstance(node_type, str)
            or node_type not in node_mappings
            or not isinstance(node.get("inputs"), list)
            or not isinstance(node.get("outputs"), list)
            or not isinstance(node.get("widgets_values"), list)
        ):
            raise ComfyUICoexistenceError(
                "{0} workflow has an invalid or undiscoverable node".format(label)
            )
        node_ids.add(node_id)
        node_types.append(node_type)
        if getattr(node_mappings[node_type], "OUTPUT_NODE", False) is True:
            output_types.append(node_type)

    link_ids: set[int] = set()
    for raw in raw_links:
        link = cast(Sequence[object], raw)
        link_id, source_id, target_id = link[0], link[1], link[3]
        if (
            isinstance(link_id, bool)
            or not isinstance(link_id, int)
            or link_id in link_ids
            or isinstance(source_id, bool)
            or not isinstance(source_id, int)
            or source_id not in node_ids
            or isinstance(target_id, bool)
            or not isinstance(target_id, int)
            or target_id not in node_ids
        ):
            raise ComfyUICoexistenceError(
                "{0} workflow has an invalid link endpoint or identifier".format(label)
            )
        link_ids.add(link_id)
    if not output_types:
        raise ComfyUICoexistenceError(
            "{0} workflow must terminate in a discovered OUTPUT_NODE".format(label)
        )
    return WorkflowReport(label, tuple(node_types), tuple(output_types), len(raw_links))
