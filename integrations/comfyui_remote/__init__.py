"""Removable remote-only ComfyUI frontend; no model or ComfyUI dependency."""

from .nodes import (
    NODE_CLASS_MAPPINGS,
    NODE_DISPLAY_NAME_MAPPINGS,
    RemoteArtifactCollection,
    RemoteArtifactRef,
    RemoteAssetBinding,
    RemoteAssetBundle,
    RemoteDownloadRef,
    RemoteEventSummary,
    RemoteJobRef,
    RemoteNodeCancelled,
    RemoteNodeError,
    RemoteNodeExecutionError,
    RemoteNodeValidationError,
    configure_remote_client_for_host,
    teardown_remote_client,
)

__all__ = [
    "NODE_CLASS_MAPPINGS",
    "NODE_DISPLAY_NAME_MAPPINGS",
    "RemoteArtifactCollection",
    "RemoteArtifactRef",
    "RemoteAssetBinding",
    "RemoteAssetBundle",
    "RemoteDownloadRef",
    "RemoteEventSummary",
    "RemoteJobRef",
    "RemoteNodeCancelled",
    "RemoteNodeError",
    "RemoteNodeExecutionError",
    "RemoteNodeValidationError",
    "configure_remote_client_for_host",
    "teardown_remote_client",
]
