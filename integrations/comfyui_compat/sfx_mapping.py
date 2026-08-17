"""Optional ComfyUI display names for unified SFX workflows.

This mapping is not a job-state authority. Omitting `integrations/` must not
break Core, CLI, API, or Worker paths. No Woosh T2A nodes are declared.
"""

from __future__ import annotations

from types import MappingProxyType
from typing import Mapping

from nano_aural_runtime_workflows import GENERATE_AND_MUX, TEXT_GENERATE, VIDEO_GENERATE

SFX_COMFYUI_NODE_NAMES: Mapping[str, str] = MappingProxyType(
    {
        TEXT_GENERATE: "NanoAuralStableAudio3TextToSfx",
        VIDEO_GENERATE: "NanoAuralVideoToSfx",
        GENERATE_AND_MUX: "NanoAuralGenerateAndMux",
    }
)
EXISTING_CONTROLFOLEY_NODES = (
    "NanoAuralControlFoleyEmbedded",
    "NanoAuralAudioOutput",
)
FORBIDDEN_NODE_NAMES = (
    "NanoAuralWooshTextToSfx",
    "NanoAuralWooshFlow",
    "NanoAuralWooshDFlow",
)


def display_name(workflow_id: str) -> str:
    try:
        return SFX_COMFYUI_NODE_NAMES[workflow_id]
    except KeyError as error:
        raise KeyError("no ComfyUI mapping for workflow") from error
