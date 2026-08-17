"""Strict, dependency-free manifests for the Woosh V2A VFlow/DVFlow baseline.

Phase 9A records provenance only. It never downloads weights, never inspects
Woosh-Flow / Woosh-DFlow / TextConditionerA, never imports torch, and never
claims parity.
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
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

SUPPORTED_SOURCE_REPOSITORY = "https://github.com/SonyResearch/Woosh"
SUPPORTED_SOURCE_TAG = "v1.0.0"
SUPPORTED_SOURCE_REVISION = "f6ff658efc6d63dee9959964cd75c63415910a19"
ADAPTER_ID = "woosh-v2a"
OPERATION = "audio.video_to_sfx"
RUNTIME_ENVIRONMENT_ID = "woosh-v2a-pytorch-2.8.0-cu128"
SCHEMA_VERSION = 1
WINDOW_START_SECONDS = 0.0
WINDOW_END_SECONDS = 8.0
VIDEO_FPS = 24
SAMPLE_RATE = 48000
CHANNELS = 1
LATENT_CHANNELS = 128
LATENT_FRAMES = 801
SYNCHFORMER_HF_REPOSITORY = "hkchengrex/MMAudio"
SYNCHFORMER_FILENAME = "ext_weights/synchformer_state_dict.pth"
BACKEND_DVFLOW = "dvflow-8s"
BACKEND_VFLOW = "vflow-8s"
SUPPORTED_BACKENDS = (BACKEND_DVFLOW, BACKEND_VFLOW)
OUT_OF_SCOPE_BACKENDS = frozenset(("flow", "dflow", "t2a", "woosh-flow", "woosh-dflow"))
_SHA256 = re.compile(r"^[0-9a-f]{64}$")

IN_SCOPE_ARCHIVES = {
    "Woosh-AE.zip": (
        "d6f77e3792ee43c21da580f39d6576e0da3e4b46b949223259adf36036c1f9af",
        822991075,
    ),
    "TextConditionerV.zip": (
        "64d8ba0d647d3e685365b37526c2c95790110823623b58fa1642dd8f2139f6ac",
        1298488076,
    ),
    "Woosh-VFlow-8s.zip": (
        "b1d9193d611d33471c39a878c205d4fb52ca380c28abd3557d610439d23b583a",
        1537130885,
    ),
    "Woosh-DVFlow-8s.zip": (
        "c6f4b60d1cbc88a49ddd1ffa704a251570c0d7dafa8fdd1b4af7d8ba90d61d79",
        1564739862,
    ),
}
OUT_OF_SCOPE_ARCHIVES = frozenset(
    ("TextConditionerA.zip", "Woosh-Flow.zip", "Woosh-DFlow.zip", "Woosh-CLAP.zip")
)


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
    def from_dict(cls, value: Any, name: str, *, allow_omitted: bool = False) -> "Fingerprint":
        data = _mapping(value, name, ("status", "sha256", "size_bytes"))
        status = _string(data["status"], "{0}.status".format(name))
        if status == "omitted":
            if not allow_omitted:
                raise SchemaValidationError("{0} cannot be omitted".format(name))
            if data["sha256"] is not None or data["size_bytes"] is not None:
                raise SchemaValidationError(
                    "{0} omitted fingerprint must not contain a digest or size".format(name)
                )
            return cls(status=status, sha256=None, size_bytes=None)
        if status == "pending":
            if data["sha256"] is not None or data["size_bytes"] is not None:
                raise SchemaValidationError(
                    "{0} pending fingerprint must not contain a digest or size".format(name)
                )
            return cls(status=status, sha256=None, size_bytes=None)
        if status != "verified":
            raise SchemaValidationError(
                "{0}.status must be pending, verified{1}".format(
                    name, ", or omitted" if allow_omitted else ""
                )
            )
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
class ReleaseArchive:
    filename: str
    sha256: str
    size_bytes: int

    @classmethod
    def from_dict(cls, value: Any, name: str, expected_filename: str) -> "ReleaseArchive":
        data = _mapping(value, name, ("filename", "sha256", "size_bytes"))
        filename = _string(data["filename"], "{0}.filename".format(name))
        if filename != expected_filename:
            raise SchemaValidationError("{0} must be {1}".format(name, expected_filename))
        if filename in OUT_OF_SCOPE_ARCHIVES:
            raise SchemaValidationError("{0} is out of V2A scope".format(filename))
        expected = IN_SCOPE_ARCHIVES.get(filename)
        if expected is None:
            raise SchemaValidationError("{0} is not an in-scope V2A archive".format(filename))
        digest = data["sha256"]
        if not isinstance(digest, str) or digest != expected[0]:
            raise SchemaValidationError(
                "{0} SHA-256 does not match the v1.0.0 release".format(name)
            )
        size = _integer(data["size_bytes"], "{0}.size_bytes".format(name), 1)
        if size != expected[1]:
            raise SchemaValidationError("{0} size does not match the v1.0.0 release".format(name))
        return cls(filename=filename, sha256=digest, size_bytes=size)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SamplerPolicy:
    model_class: str
    sampler: str
    num_steps: Optional[int]
    renoise: Optional[Tuple[float, float, float, float]]
    cfg: float
    solver_method: Optional[str]
    atol: Optional[float]
    rtol: Optional[float]

    @classmethod
    def from_dict(cls, value: Any, backend_id: str) -> "SamplerPolicy":
        data = _mapping(
            value,
            "sampler_policy",
            (
                "model_class",
                "sampler",
                "num_steps",
                "renoise",
                "cfg",
                "solver_method",
                "atol",
                "rtol",
            ),
        )
        if backend_id == BACKEND_DVFLOW:
            if _string(data["model_class"], "model_class") != "FlowMapFromPretrained":
                raise SchemaValidationError("dvflow-8s model_class must be FlowMapFromPretrained")
            if _string(data["sampler"], "sampler") != "sample_euler":
                raise SchemaValidationError("dvflow-8s sampler must be sample_euler")
            if _integer(data["num_steps"], "num_steps", 1) != 4:
                raise SchemaValidationError("dvflow-8s num_steps must be 4")
            if not isinstance(data["renoise"], Sequence) or len(data["renoise"]) != 4:
                raise SchemaValidationError("dvflow-8s renoise must be four numbers")
            first, second, third, fourth = (_finite(item, "renoise") for item in data["renoise"])
            renoise = (first, second, third, fourth)
            if renoise != (0.0, 0.5, 0.5, 0.3):
                raise SchemaValidationError("dvflow-8s renoise must be [0, 0.5, 0.5, 0.3]")
            if _finite(data["cfg"], "cfg") != 3.0:
                raise SchemaValidationError("dvflow-8s cfg must be the official default 3")
            if (
                data["solver_method"] is not None
                or data["atol"] is not None
                or data["rtol"] is not None
            ):
                raise SchemaValidationError("dvflow-8s must not set an ODE solver policy")
            return cls(
                model_class="FlowMapFromPretrained",
                sampler="sample_euler",
                num_steps=4,
                renoise=renoise,
                cfg=3.0,
                solver_method=None,
                atol=None,
                rtol=None,
            )
        if backend_id == BACKEND_VFLOW:
            if _string(data["model_class"], "model_class") != "VideoKontext":
                raise SchemaValidationError("vflow-8s model_class must be VideoKontext")
            if _string(data["sampler"], "sampler") != "flowmatching_integrate":
                raise SchemaValidationError("vflow-8s sampler must be flowmatching_integrate")
            if data["num_steps"] is not None or data["renoise"] is not None:
                raise SchemaValidationError("vflow-8s must not set distilled Euler fields")
            if _string(data["solver_method"], "solver_method") != "dopri5":
                raise SchemaValidationError("vflow-8s solver_method must be dopri5")
            if _finite(data["atol"], "atol") != 1e-3 or _finite(data["rtol"], "rtol") != 1e-3:
                raise SchemaValidationError("vflow-8s atol/rtol must be 1e-3")
            if _finite(data["cfg"], "cfg") != 4.5:
                raise SchemaValidationError("vflow-8s cfg must be the official default 4.5")
            return cls(
                model_class="VideoKontext",
                sampler="flowmatching_integrate",
                num_steps=None,
                renoise=None,
                cfg=4.5,
                solver_method="dopri5",
                atol=1e-3,
                rtol=1e-3,
            )
        raise SchemaValidationError("unsupported Woosh backend")

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["renoise"] = None if self.renoise is None else list(self.renoise)
        return payload


def _backend_archive_name(backend_id: str) -> str:
    if backend_id == BACKEND_DVFLOW:
        return "Woosh-DVFlow-8s.zip"
    if backend_id == BACKEND_VFLOW:
        return "Woosh-VFlow-8s.zip"
    raise SchemaValidationError("unsupported Woosh backend")


@dataclass(frozen=True)
class WooshV2ADeploymentManifest:
    schema_version: int
    deployment_id: str
    adapter_id: str
    backend_id: str
    upstream_repository: str
    source_tag: str
    source_revision: str
    runtime_environment_id: str
    window_start_seconds: float
    window_end_seconds: float
    video_fps: int
    sample_rate: int
    channels: int
    latent_channels: int
    latent_frames: int
    sampler_policy: SamplerPolicy
    woosh_ae_archive: ReleaseArchive
    text_conditioner_v_archive: ReleaseArchive
    backend_archive: ReleaseArchive
    woosh_ae_config: Fingerprint
    woosh_ae_weights: Fingerprint
    text_conditioner_v_config: Fingerprint
    text_conditioner_v_weights: Fingerprint
    backend_config: Fingerprint
    backend_weights: Fingerprint
    synchformer_repository: str
    synchformer_filename: str
    synchformer_revision: Fingerprint
    synchformer_weights: Fingerprint

    @classmethod
    def from_dict(cls, value: Any) -> "WooshV2ADeploymentManifest":
        data = _mapping(
            value,
            "deployment",
            (
                "schema_version",
                "deployment_id",
                "adapter_id",
                "backend_id",
                "upstream_repository",
                "source_tag",
                "source_revision",
                "runtime_environment_id",
                "window_start_seconds",
                "window_end_seconds",
                "video_fps",
                "sample_rate",
                "channels",
                "latent_channels",
                "latent_frames",
                "sampler_policy",
                "woosh_ae_archive",
                "text_conditioner_v_archive",
                "backend_archive",
                "woosh_ae_config",
                "woosh_ae_weights",
                "text_conditioner_v_config",
                "text_conditioner_v_weights",
                "backend_config",
                "backend_weights",
                "synchformer_repository",
                "synchformer_filename",
                "synchformer_revision",
                "synchformer_weights",
            ),
        )
        if _integer(data["schema_version"], "schema_version", 1) != SCHEMA_VERSION:
            raise SchemaValidationError("unsupported deployment schema_version")
        if _string(data["adapter_id"], "adapter_id") != ADAPTER_ID:
            raise SchemaValidationError("adapter_id must be woosh-v2a")
        backend_id = _string(data["backend_id"], "backend_id")
        if backend_id in OUT_OF_SCOPE_BACKENDS:
            raise SchemaValidationError("backend is out of V2A scope")
        if backend_id not in SUPPORTED_BACKENDS:
            raise SchemaValidationError("V1 supports only dvflow-8s and vflow-8s")
        if (
            _string(data["upstream_repository"], "upstream_repository")
            != SUPPORTED_SOURCE_REPOSITORY
        ):
            raise SchemaValidationError("unsupported Woosh source repository")
        if _string(data["source_tag"], "source_tag") != SUPPORTED_SOURCE_TAG:
            raise SchemaValidationError("unsupported Woosh source tag")
        if _string(data["source_revision"], "source_revision") != SUPPORTED_SOURCE_REVISION:
            raise SchemaValidationError("unsupported Woosh source revision")
        if (
            _string(data["runtime_environment_id"], "runtime_environment_id")
            != RUNTIME_ENVIRONMENT_ID
        ):
            raise SchemaValidationError("runtime_environment_id does not match the isolated lock")
        if _finite(data["window_start_seconds"], "window_start_seconds") != WINDOW_START_SECONDS:
            raise SchemaValidationError("window_start_seconds must be 0")
        if _finite(data["window_end_seconds"], "window_end_seconds") != WINDOW_END_SECONDS:
            raise SchemaValidationError("window_end_seconds must be 8")
        if _integer(data["video_fps"], "video_fps", 1) != VIDEO_FPS:
            raise SchemaValidationError("video_fps must be 24")
        if _integer(data["sample_rate"], "sample_rate", 1) != SAMPLE_RATE:
            raise SchemaValidationError("sample_rate must be 48000")
        if _integer(data["channels"], "channels", 1) != CHANNELS:
            raise SchemaValidationError("channels must be 1")
        if _integer(data["latent_channels"], "latent_channels", 1) != LATENT_CHANNELS:
            raise SchemaValidationError("latent_channels must be 128")
        if _integer(data["latent_frames"], "latent_frames", 1) != LATENT_FRAMES:
            raise SchemaValidationError("latent_frames must be 801")
        if (
            _string(data["synchformer_repository"], "synchformer_repository")
            != SYNCHFORMER_HF_REPOSITORY
        ):
            raise SchemaValidationError("synchformer_repository must be hkchengrex/MMAudio")
        if _string(data["synchformer_filename"], "synchformer_filename") != SYNCHFORMER_FILENAME:
            raise SchemaValidationError("synchformer_filename must be the MMAudio ext_weights file")
        sampler_policy = SamplerPolicy.from_dict(data["sampler_policy"], backend_id)
        return cls(
            schema_version=SCHEMA_VERSION,
            deployment_id=_string(data["deployment_id"], "deployment_id"),
            adapter_id=ADAPTER_ID,
            backend_id=backend_id,
            upstream_repository=SUPPORTED_SOURCE_REPOSITORY,
            source_tag=SUPPORTED_SOURCE_TAG,
            source_revision=SUPPORTED_SOURCE_REVISION,
            runtime_environment_id=RUNTIME_ENVIRONMENT_ID,
            window_start_seconds=WINDOW_START_SECONDS,
            window_end_seconds=WINDOW_END_SECONDS,
            video_fps=VIDEO_FPS,
            sample_rate=SAMPLE_RATE,
            channels=CHANNELS,
            latent_channels=LATENT_CHANNELS,
            latent_frames=LATENT_FRAMES,
            sampler_policy=sampler_policy,
            woosh_ae_archive=ReleaseArchive.from_dict(
                data["woosh_ae_archive"], "woosh_ae_archive", "Woosh-AE.zip"
            ),
            text_conditioner_v_archive=ReleaseArchive.from_dict(
                data["text_conditioner_v_archive"],
                "text_conditioner_v_archive",
                "TextConditionerV.zip",
            ),
            backend_archive=ReleaseArchive.from_dict(
                data["backend_archive"], "backend_archive", _backend_archive_name(backend_id)
            ),
            woosh_ae_config=Fingerprint.from_dict(data["woosh_ae_config"], "woosh_ae_config"),
            woosh_ae_weights=Fingerprint.from_dict(data["woosh_ae_weights"], "woosh_ae_weights"),
            text_conditioner_v_config=Fingerprint.from_dict(
                data["text_conditioner_v_config"], "text_conditioner_v_config"
            ),
            text_conditioner_v_weights=Fingerprint.from_dict(
                data["text_conditioner_v_weights"], "text_conditioner_v_weights"
            ),
            backend_config=Fingerprint.from_dict(data["backend_config"], "backend_config"),
            backend_weights=Fingerprint.from_dict(data["backend_weights"], "backend_weights"),
            synchformer_repository=SYNCHFORMER_HF_REPOSITORY,
            synchformer_filename=SYNCHFORMER_FILENAME,
            synchformer_revision=Fingerprint.from_dict(
                data["synchformer_revision"], "synchformer_revision"
            ),
            synchformer_weights=Fingerprint.from_dict(
                data["synchformer_weights"], "synchformer_weights"
            ),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["sampler_policy"] = self.sampler_policy.to_dict()
        payload["woosh_ae_archive"] = self.woosh_ae_archive.to_dict()
        payload["text_conditioner_v_archive"] = self.text_conditioner_v_archive.to_dict()
        payload["backend_archive"] = self.backend_archive.to_dict()
        for name in (
            "woosh_ae_config",
            "woosh_ae_weights",
            "text_conditioner_v_config",
            "text_conditioner_v_weights",
            "backend_config",
            "backend_weights",
            "synchformer_revision",
            "synchformer_weights",
        ):
            payload[name] = getattr(self, name).to_dict()
        return payload


@dataclass(frozen=True)
class WooshV2AFixtureManifest:
    schema_version: int
    fixture_id: str
    operation: str
    seed: int
    window_start_seconds: float
    window_end_seconds: float
    video: Fingerprint
    prompt: Fingerprint

    @classmethod
    def from_dict(cls, value: Any) -> "WooshV2AFixtureManifest":
        data = _mapping(
            value,
            "fixture",
            (
                "schema_version",
                "fixture_id",
                "operation",
                "seed",
                "window_start_seconds",
                "window_end_seconds",
                "video",
                "prompt",
            ),
        )
        if _integer(data["schema_version"], "schema_version", 1) != SCHEMA_VERSION:
            raise SchemaValidationError("unsupported fixture schema_version")
        if _string(data["operation"], "operation") != OPERATION:
            raise SchemaValidationError("fixture operation must be audio.video_to_sfx")
        if _finite(data["window_start_seconds"], "window_start_seconds") != WINDOW_START_SECONDS:
            raise SchemaValidationError("window_start_seconds must be 0")
        if _finite(data["window_end_seconds"], "window_end_seconds") != WINDOW_END_SECONDS:
            raise SchemaValidationError("window_end_seconds must be 8")
        return cls(
            schema_version=SCHEMA_VERSION,
            fixture_id=_string(data["fixture_id"], "fixture_id"),
            operation=OPERATION,
            seed=_integer(data["seed"], "seed"),
            window_start_seconds=WINDOW_START_SECONDS,
            window_end_seconds=WINDOW_END_SECONDS,
            video=Fingerprint.from_dict(data["video"], "video"),
            prompt=Fingerprint.from_dict(data["prompt"], "prompt", allow_omitted=True),
        )

    def to_dict(self) -> Dict[str, Any]:
        payload = asdict(self)
        payload["video"] = self.video.to_dict()
        payload["prompt"] = self.prompt.to_dict()
        return payload


@dataclass(frozen=True)
class GpuPrerequisites:
    available: bool
    reasons: Tuple[str, ...]


def detect_gpu_prerequisites(
    source_dir: Optional[str],
    weights_dir: Optional[str],
    synchformer_path: Optional[str],
) -> GpuPrerequisites:
    reasons = []
    if not source_dir:
        reasons.append("WOOSH_SOURCE_DIR is not set")
    if not weights_dir:
        reasons.append("WOOSH_WEIGHTS_DIR is not set")
    if not synchformer_path:
        reasons.append("WOOSH_SYNCHFORMER_PATH is not set")
    if not os.environ.get("HF_HOME"):
        reasons.append("HF_HOME is not set")
    return GpuPrerequisites(available=not reasons, reasons=tuple(reasons))


def require_gpu_prerequisites(
    source_dir: Optional[str],
    weights_dir: Optional[str],
    synchformer_path: Optional[str],
) -> GpuPrerequisites:
    detected = detect_gpu_prerequisites(source_dir, weights_dir, synchformer_path)
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
    deployment: WooshV2ADeploymentManifest,
    fixture: WooshV2AFixtureManifest,
    *,
    self_repeat: bool = False,
) -> Dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "state": "planned",
        "operation": OPERATION,
        "adapter_id": ADAPTER_ID,
        "backend_id": deployment.backend_id,
        "deployment_id": deployment.deployment_id,
        "fixture_id": fixture.fixture_id,
        "comparison": "self_repeat" if self_repeat else "single",
        "deployment_manifest_sha256": manifest_sha256(deployment.to_dict()),
        "fixture_manifest_sha256": manifest_sha256(fixture.to_dict()),
        "source_revision": deployment.source_revision,
        "seed": fixture.seed,
        "window_start_seconds": fixture.window_start_seconds,
        "window_end_seconds": fixture.window_end_seconds,
        "prompt_present": fixture.prompt.status != "omitted",
        "sample_rate": deployment.sample_rate,
        "channels": deployment.channels,
        "wall_time_seconds": None,
        "model_load_seconds": None,
        "video_decode_seconds": None,
        "synchformer_seconds": None,
        "sampler_seconds": None,
        "ae_decode_seconds": None,
        "peak_allocated_bytes": None,
        "peak_reserved_bytes": None,
        "environment": collect_sanitized_environment(),
    }


def baseline_matrix() -> Tuple[Tuple[str, str, bool], ...]:
    """Checked-in pairing of deployments, fixtures, and self-repeat intent."""
    return (
        ("dvflow-8s.lock.json", "v2sfx-8s-video-only.json", False),
        ("dvflow-8s.lock.json", "v2sfx-8s-video-prompt.json", False),
        ("vflow-8s.lock.json", "v2sfx-8s-video-only.json", False),
        ("vflow-8s.lock.json", "v2sfx-8s-video-prompt.json", False),
        ("dvflow-8s.lock.json", "v2sfx-8s-video-only.json", True),
        ("vflow-8s.lock.json", "v2sfx-8s-video-only.json", True),
    )
