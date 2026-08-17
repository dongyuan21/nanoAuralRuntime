"""Optional embedded ComfyUI frontend for the local ControlFoley route.

This package deliberately does not import ComfyUI.  ComfyUI discovers this
directory as a custom-node package, while headless nanoAuralRuntime packages
remain usable when the directory is omitted altogether.
"""

from .embedded import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    ComfyUIExecutionCancelled,
    ComfyUIExecutionError,
    ComfyUIOriginConflictError,
    ComfyUIValidationError,
    ControlFoleyEmbeddedNode,
    EmbeddedAudio,
    EmbeddedAudioOutputNode,
    EmbeddedRuntimeOwner,
    LocalControlFoleyRuntime,
    assert_controlfoley_module_origins,
    configure_embedded_runtime,
    teardown_embedded_runtime,
)

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "ComfyUIExecutionCancelled",
    "ComfyUIExecutionError",
    "ComfyUIOriginConflictError",
    "ComfyUIValidationError",
    "ControlFoleyEmbeddedNode",
    "EmbeddedAudio",
    "EmbeddedAudioOutputNode",
    "EmbeddedRuntimeOwner",
    "LocalControlFoleyRuntime",
    "assert_controlfoley_module_origins",
    "configure_embedded_runtime",
    "teardown_embedded_runtime",
]
