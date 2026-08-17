"""Strict, dependency-free manifests for the Stable Audio 3 Small-SFX baseline.

Phase 8A records provenance only. It never downloads gated weights, never
imports torch, and never claims parity.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import platform
import re
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

SUPPORTED_SOURCE_REPOSITORY = "https://github.com/Stability-AI/stable-audio-3"
SUPPORTED_SOURCE_REVISION = "a0b57f5483c4588f827f3552b7d5c6ca2a9687be"
SUPPORTED_MODEL_ID = "small-sfx"
SUPPORTED_HF_REPOSITORY = "stabilityai/stable-audio-3-small-sfx"
SUPPORTED_BACKEND = "pytorch"
SUPPORTED_STEPS = 8
SUPPORTED_CFG_SCALE = 1.0
SUPPORTED_SAMPLER = "pingpong"
SUPPORTED_BATCH_SIZE = 1
SUPPORTED_SAMPLE_RATE = 44100
SUPPORTED_CHANNELS = 2
SUPPORTED_DURATIONS = (5.0, 30.0, 120.0)
RUNTIME_ENVIRONMENT_ID = "stable-audio-3-pytorch-2.7.1-cu126"
SCHEMA_VERSION = 1
OPERATION = "audio.text_to_sfx"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class SchemaValidationError(ValueError):
    """Raised when a baseline manifest is incomplete, ambiguous, or unsafe."""


class GpuPrerequisitesUnavailable(RuntimeError):
    """Raised by callers that choose to require an optional GPU setup."""


def _mapping(value: Any, name: str, keys: Tuple[str, ...]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError("{0} must be an object".format(name))
    actual = set(value)
    expected = set(keys)
    if actual != expected:
        raise SchemaValidationError(
            "{0} keys must be exactly {1}; missing={2}, extra={3}".format(
                name, sorted(expected), sorted(expected - actual), sorted(actual - expected)
            )
        )
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SchemaValidationError("{0} must be a non-empty string".format(name))
    return value


def _integer(value: Any, name: str, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise SchemaValidationError("{0} must be an integer >= {1}".format(name, minimum))
    return value


def _finite(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaValidationError("{0} must be a number".format(name))
    number = float(value)
    if not math.isfinite(number):
        raise SchemaValidationError("{0} must be finite".format(name))
    return number


def manifest_sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path) -> Dict[str, Any]:
    with path.open(encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict):
        raise SchemaValidationError("JSON manifest must be an object")
    return payload


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        raise SchemaValidationError("refusing to overwrite {0}".format(path))
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )


@dataclass(frozen=True)
class Fingerprint:
    status: str
    sha256: Optional[str]
    size_bytes: Optional[int]

    @classmethod
    def from_dict(cls, value: Any, name: str) -> "Fingerprint":
        data = _mapping(value, name, ("status", "sha256", "size_bytes"))
        status = _string(data["status"], "{0}.status".format(name))
        if status == "pending":
            if data["sha256"] is not None or data["size_bytes"] is not None:
                raise SchemaValidationError(
                    "{0} pending fingerprint must not contain a digest or size".format(name)
                )
            return cls(status=status, sha256=None, size_bytes=None)
        if status != "verified":
            raise SchemaValidationError("{0}.status must be pending or verified".format(name))
        digest = data["sha256"]
        if not isinstance(digest, str) or not _SHA256.fullmatch(digest) or len(set(digest)) == 1:
            raise SchemaValidationError("{0}.sha256 must be a lowercase full SHA-256".format(name))
        return cls(
            status=status,
            sha256=digest,
            size_bytes=_integer(data["size_bytes"], "{0}.size_bytes".format(name)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StableAudio3DeploymentManifest:
    schema_version: int
    deployment_id: str
    upstream_repository: str
    source_revision: str
    model_id: str
    hf_repository: str
    backend: str
    steps: int
    cfg_scale: float
    sampler: str
    batch_size: int
    sample_rate: int
    channels: int
    runtime_environment_id: str
    hf_revision: Fingerprint
    weights: Fingerprint

    @classmethod
    def from_dict(cls, value: Any) -> "StableAudio3DeploymentManifest":
        data = _mapping(
            value,
            "deployment",
            (
                "schema_version",
                "deployment_id",
                "upstream_repository",
                "source_revision",
                "model_id",
                "hf_repository",
                "backend",
                "steps",
                "cfg_scale",
                "sampler",
                "batch_size",
                "sample_rate",
                "channels",
                "runtime_environment_id",
                "hf_revision",
                "weights",
            ),
        )
        if _integer(data["schema_version"], "schema_version", 1) != SCHEMA_VERSION:
            raise SchemaValidationError("unsupported deployment schema_version")
        if (
            _string(data["upstream_repository"], "upstream_repository")
            != SUPPORTED_SOURCE_REPOSITORY
        ):
            raise SchemaValidationError("unsupported Stable Audio 3 source repository")
        if _string(data["source_revision"], "source_revision") != SUPPORTED_SOURCE_REVISION:
            raise SchemaValidationError("unsupported Stable Audio 3 source revision")
        if _string(data["model_id"], "model_id") != SUPPORTED_MODEL_ID:
            raise SchemaValidationError("V1 supports only small-sfx")
        if _string(data["hf_repository"], "hf_repository") != SUPPORTED_HF_REPOSITORY:
            raise SchemaValidationError("unsupported Hugging Face repository")
        if _string(data["backend"], "backend") != SUPPORTED_BACKEND:
            raise SchemaValidationError("V1 supports only the official PyTorch backend")
        if _integer(data["steps"], "steps", 1) != SUPPORTED_STEPS:
            raise SchemaValidationError("steps must be the official post-trained default 8")
        if _finite(data["cfg_scale"], "cfg_scale") != SUPPORTED_CFG_SCALE:
            raise SchemaValidationError("cfg_scale must be the official post-trained default 1.0")
        if _string(data["sampler"], "sampler") != SUPPORTED_SAMPLER:
            raise SchemaValidationError("sampler must be the official pingpong path")
        if _integer(data["batch_size"], "batch_size", 1) != SUPPORTED_BATCH_SIZE:
            raise SchemaValidationError("batch_size must be 1")
        if _integer(data["sample_rate"], "sample_rate", 1) != SUPPORTED_SAMPLE_RATE:
            raise SchemaValidationError("sample_rate must be 44100")
        if _integer(data["channels"], "channels", 1) != SUPPORTED_CHANNELS:
            raise SchemaValidationError("channels must be 2")
        if (
            _string(data["runtime_environment_id"], "runtime_environment_id")
            != RUNTIME_ENVIRONMENT_ID
        ):
            raise SchemaValidationError("runtime_environment_id does not match the isolated lock")
        return cls(
            schema_version=SCHEMA_VERSION,
            deployment_id=_string(data["deployment_id"], "deployment_id"),
            upstream_repository=SUPPORTED_SOURCE_REPOSITORY,
            source_revision=SUPPORTED_SOURCE_REVISION,
            model_id=SUPPORTED_MODEL_ID,
            hf_repository=SUPPORTED_HF_REPOSITORY,
            backend=SUPPORTED_BACKEND,
            steps=SUPPORTED_STEPS,
            cfg_scale=SUPPORTED_CFG_SCALE,
            sampler=SUPPORTED_SAMPLER,
            batch_size=SUPPORTED_BATCH_SIZE,
            sample_rate=SUPPORTED_SAMPLE_RATE,
            channels=SUPPORTED_CHANNELS,
            runtime_environment_id=RUNTIME_ENVIRONMENT_ID,
            hf_revision=Fingerprint.from_dict(data["hf_revision"], "hf_revision"),
            weights=Fingerprint.from_dict(data["weights"], "weights"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["hf_revision"] = self.hf_revision.to_dict()
        payload["weights"] = self.weights.to_dict()
        return payload


@dataclass(frozen=True)
class StableAudio3FixtureManifest:
    schema_version: int
    fixture_id: str
    operation: str
    duration_seconds: float
    seed: int
    prompt: Fingerprint

    @classmethod
    def from_dict(cls, value: Any) -> "StableAudio3FixtureManifest":
        data = _mapping(
            value,
            "fixture",
            (
                "schema_version",
                "fixture_id",
                "operation",
                "duration_seconds",
                "seed",
                "prompt",
            ),
        )
        if _integer(data["schema_version"], "schema_version", 1) != SCHEMA_VERSION:
            raise SchemaValidationError("unsupported fixture schema_version")
        if _string(data["operation"], "operation") != OPERATION:
            raise SchemaValidationError("fixture operation must be audio.text_to_sfx")
        duration = _finite(data["duration_seconds"], "duration_seconds")
        if duration not in SUPPORTED_DURATIONS:
            raise SchemaValidationError("duration_seconds must be 5, 30, or 120")
        return cls(
            schema_version=SCHEMA_VERSION,
            fixture_id=_string(data["fixture_id"], "fixture_id"),
            operation=OPERATION,
            duration_seconds=duration,
            seed=_integer(data["seed"], "seed"),
            prompt=Fingerprint.from_dict(data["prompt"], "prompt"),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["prompt"] = self.prompt.to_dict()
        return payload


@dataclass(frozen=True)
class GpuPrerequisites:
    available: bool
    reasons: Tuple[str, ...]


def detect_gpu_prerequisites(
    source_dir: Optional[str], weights_dir: Optional[str]
) -> GpuPrerequisites:
    reasons = []
    if not source_dir:
        reasons.append("STABLE_AUDIO_3_SOURCE_DIR is not set")
    if not weights_dir:
        reasons.append("STABLE_AUDIO_3_WEIGHTS_DIR is not set")
    if not os.environ.get("HF_HOME"):
        reasons.append("HF_HOME is not set")
    return GpuPrerequisites(available=not reasons, reasons=tuple(reasons))


def require_gpu_prerequisites(
    source_dir: Optional[str], weights_dir: Optional[str]
) -> GpuPrerequisites:
    detected = detect_gpu_prerequisites(source_dir, weights_dir)
    if not detected.available:
        raise GpuPrerequisitesUnavailable("; ".join(detected.reasons))
    return detected


def collect_sanitized_environment() -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "python_version": platform.python_version(),
        "platform": platform.system().lower(),
        "torch": None,
        "cuda": None,
    }


def planned_result(
    deployment: StableAudio3DeploymentManifest,
    fixture: StableAudio3FixtureManifest,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "planned",
        "operation": OPERATION,
        "deployment_id": deployment.deployment_id,
        "fixture_id": fixture.fixture_id,
        "deployment_manifest_sha256": manifest_sha256(deployment.to_dict()),
        "fixture_manifest_sha256": manifest_sha256(fixture.to_dict()),
        "source_revision": deployment.source_revision,
        "duration_seconds": fixture.duration_seconds,
        "seed": fixture.seed,
        "sample_rate": deployment.sample_rate,
        "channels": deployment.channels,
        "wall_time_seconds": None,
        "peak_allocated_bytes": None,
        "peak_reserved_bytes": None,
        "environment": collect_sanitized_environment(),
    }
