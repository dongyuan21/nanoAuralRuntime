"""Stable Audio 3 Small-SFX provenance. No torch or gated-weight download."""

from .adapter import StableAudio3Adapter, stable_audio_3_local_deployment
from .baseline import (
    OPERATION,
    RUNTIME_ENVIRONMENT_ID,
    SUPPORTED_HF_REPOSITORY,
    SUPPORTED_MODEL_ID,
    SUPPORTED_SOURCE_REPOSITORY,
    SUPPORTED_SOURCE_REVISION,
    Fingerprint,
    GpuPrerequisitesUnavailable,
    SchemaValidationError,
    StableAudio3DeploymentManifest,
    StableAudio3FixtureManifest,
    collect_sanitized_environment,
    detect_gpu_prerequisites,
    manifest_sha256,
    require_gpu_prerequisites,
)

__all__ = [
    "StableAudio3Adapter",
    "stable_audio_3_local_deployment",
    "OPERATION",
    "RUNTIME_ENVIRONMENT_ID",
    "SUPPORTED_HF_REPOSITORY",
    "SUPPORTED_MODEL_ID",
    "SUPPORTED_SOURCE_REPOSITORY",
    "SUPPORTED_SOURCE_REVISION",
    "Fingerprint",
    "GpuPrerequisitesUnavailable",
    "SchemaValidationError",
    "StableAudio3DeploymentManifest",
    "StableAudio3FixtureManifest",
    "collect_sanitized_environment",
    "detect_gpu_prerequisites",
    "manifest_sha256",
    "require_gpu_prerequisites",
]
