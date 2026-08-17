"""Woosh V2A provenance and local adapter. No torch, no Flow/DFlow/T2A paths."""

from .adapter import OfficialWooshV2ARunner, WooshV2AAdapter, woosh_v2a_local_deployment
from .baseline import (
    ADAPTER_ID,
    BACKEND_DVFLOW,
    BACKEND_VFLOW,
    OPERATION,
    RUNTIME_ENVIRONMENT_ID,
    SUPPORTED_SOURCE_REPOSITORY,
    SUPPORTED_SOURCE_REVISION,
    SUPPORTED_SOURCE_TAG,
    Fingerprint,
    GpuPrerequisitesUnavailable,
    SchemaValidationError,
    WooshV2ADeploymentManifest,
    WooshV2AFixtureManifest,
    collect_sanitized_environment,
    detect_gpu_prerequisites,
    manifest_sha256,
    require_gpu_prerequisites,
)
from .tasks import WooshV2ALocalRequest

__all__ = [
    "ADAPTER_ID",
    "BACKEND_DVFLOW",
    "BACKEND_VFLOW",
    "OPERATION",
    "RUNTIME_ENVIRONMENT_ID",
    "SUPPORTED_SOURCE_REPOSITORY",
    "SUPPORTED_SOURCE_REVISION",
    "SUPPORTED_SOURCE_TAG",
    "Fingerprint",
    "GpuPrerequisitesUnavailable",
    "OfficialWooshV2ARunner",
    "SchemaValidationError",
    "WooshV2AAdapter",
    "WooshV2ADeploymentManifest",
    "WooshV2AFixtureManifest",
    "WooshV2ALocalRequest",
    "collect_sanitized_environment",
    "detect_gpu_prerequisites",
    "manifest_sha256",
    "require_gpu_prerequisites",
    "woosh_v2a_local_deployment",
]
