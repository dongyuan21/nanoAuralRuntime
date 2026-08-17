"""Roadmap Phase 5C official-plugin coexistence and A/B workflow checks."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Mapping

import pytest  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.comfyui import NODE_CLASS_MAPPINGS as EMBEDDED_MAPPINGS  # noqa: E402
from integrations.comfyui_compat import (  # noqa: E402
    OFFICIAL_CONTROLFOLEY_NODE_NAMES,
    ComfyUICoexistenceError,
    inspect_controlfoley_coexistence,
    validate_workflow_schema,
)
from integrations.comfyui_remote import NODE_CLASS_MAPPINGS as REMOTE_MAPPINGS  # noqa: E402


class _OfficialNode:
    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, object]:
        return {"required": {}}


class _OfficialGenerate(_OfficialNode):
    RETURN_TYPES = ("AUDIO", "INT", "FLOAT", "FLOAT", "STRING")


class _OfficialSave(_OfficialNode):
    OUTPUT_NODE = True
    RETURN_TYPES = ("AUDIO", "CONTROLFOLEY_AUDIO_FILE", "STRING")


def _official_mappings() -> dict[str, type]:
    mappings = {name: _OfficialNode for name in OFFICIAL_CONTROLFOLEY_NODE_NAMES}
    mappings["ControlFoleySimpleGenerate"] = _OfficialGenerate
    mappings["SaveControlFoleyAudio"] = _OfficialSave
    return mappings


def _module(name: str, path: Path) -> ModuleType:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch()
    module = ModuleType(name)
    module.__file__ = str(path)
    return module


def _workflow(path: str) -> Mapping[str, object]:
    value = json.loads((ROOT / path).read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_official_embedded_and_remote_mappings_share_one_host_without_collision() -> None:
    official = _official_mappings()
    assert set(official).isdisjoint(EMBEDDED_MAPPINGS)
    assert set(official).isdisjoint(REMOTE_MAPPINGS)
    assert set(EMBEDDED_MAPPINGS).isdisjoint(REMOTE_MAPPINGS)
    combined = {**official, **EMBEDDED_MAPPINGS, **REMOTE_MAPPINGS}
    assert len(combined) == len(official) + len(EMBEDDED_MAPPINGS) + len(REMOTE_MAPPINGS)
    assert "integrations.comfyui" in sys.modules
    assert "integrations.comfyui_remote" in sys.modules

    official_report = validate_workflow_schema(
        _workflow("integrations/comfyui_compat/examples/official_controlfoley_t2a_ab.json"),
        combined,
        label="official A/B",
    )
    embedded_report = validate_workflow_schema(
        _workflow("integrations/comfyui/examples/embedded_controlfoley_t2a.json"),
        combined,
        label="NanoAural Embedded",
    )
    remote_report = validate_workflow_schema(
        _workflow("integrations/comfyui_remote/examples/remote_controlfoley_v2a.json"),
        combined,
        label="NanoAural Remote",
    )
    assert official_report.output_node_types == ("SaveControlFoleyAudio",)
    assert embedded_report.output_node_types == ("NanoAuralAudioOutput",)
    assert remote_report.output_node_types == ("NanoAuralRemoteOutput",)
    assert official_report.link_count == 1
    assert embedded_report.link_count == 1
    assert remote_report.link_count == 6


def test_official_plugin_coexists_when_loaded_upstream_modules_share_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "controlfoley-source"
    source.mkdir()
    plugin = _module(
        "comfyui_controlfoley.nodes",
        tmp_path / "ComfyUI/custom_nodes/comfyui-controlfoley/nodes.py",
    )
    loaded = {
        "controlfoley": _module("controlfoley", source / "controlfoley/__init__.py"),
        "controlfoley.audio_model": _module(
            "controlfoley.audio_model", source / "controlfoley/audio_model.py"
        ),
        "lib.flow_matching": _module("lib.flow_matching", source / "lib/flow_matching.py"),
        "unrelated": _module("unrelated", tmp_path / "other/unrelated.py"),
    }

    report = inspect_controlfoley_coexistence(
        source,
        _official_mappings(),
        EMBEDDED_MAPPINGS,
        REMOTE_MAPPINGS,
        official_plugin_modules={plugin.__name__: plugin},
        loaded_modules=loaded,
    )
    assert report.expected_source_dir == source.resolve()
    assert tuple(item.module_name for item in report.loaded_upstream_origins) == (
        "controlfoley",
        "controlfoley.audio_model",
        "lib.flow_matching",
    )
    plugin_file = plugin.__file__
    assert isinstance(plugin_file, str)
    assert report.official_plugin_origins[0].path == Path(plugin_file).resolve()


def test_mixed_controlfoley_origin_is_rejected_with_actionable_diagnostics(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    source = tmp_path / "sealed-source"
    source.mkdir()
    plugin = _module("official.nodes", tmp_path / "official-plugin/nodes.py")
    conflict = _module("controlfoley", tmp_path / "other-source/controlfoley/__init__.py")
    monkeypatch.setitem(sys.modules, "controlfoley.p5c_conflict", conflict)

    with pytest.raises(ComfyUICoexistenceError) as caught:
        inspect_controlfoley_coexistence(
            source,
            _official_mappings(),
            EMBEDDED_MAPPINGS,
            REMOTE_MAPPINGS,
            official_plugin_modules={plugin.__name__: plugin},
        )
    message = str(caught.value)
    assert "module-origin conflict" in message
    conflict_file = conflict.__file__
    assert isinstance(conflict_file, str)
    assert str(Path(conflict_file).resolve()) in message
    assert str(source.resolve()) in message
    assert "Restart ComfyUI" in message
    assert "CONTROLFOLEY_SOURCE_DIR" in message
    assert "same checkout" in message


def test_unknown_origin_node_collision_and_incomplete_official_contract_refuse(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    source.mkdir()
    plugin = _module("official.nodes", tmp_path / "official/nodes.py")
    unknown = ModuleType("controlfoley")
    with pytest.raises(ComfyUICoexistenceError, match="no inspectable origin"):
        inspect_controlfoley_coexistence(
            source,
            _official_mappings(),
            EMBEDDED_MAPPINGS,
            REMOTE_MAPPINGS,
            official_plugin_modules={plugin.__name__: plugin},
            loaded_modules={"controlfoley": unknown},
        )

    colliding = _official_mappings()
    colliding["NanoAuralAudioOutput"] = _OfficialNode
    with pytest.raises(ComfyUICoexistenceError, match="Silent NODE_CLASS_MAPPINGS"):
        inspect_controlfoley_coexistence(
            source,
            colliding,
            EMBEDDED_MAPPINGS,
            REMOTE_MAPPINGS,
            official_plugin_modules={plugin.__name__: plugin},
        )

    incomplete = _official_mappings()
    del incomplete["ControlFoleySimpleGenerate"]
    with pytest.raises(ComfyUICoexistenceError, match="contract is incomplete"):
        inspect_controlfoley_coexistence(
            source,
            incomplete,
            EMBEDDED_MAPPINGS,
            REMOTE_MAPPINGS,
            official_plugin_modules={plugin.__name__: plugin},
        )
