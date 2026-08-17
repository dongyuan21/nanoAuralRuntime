"""Strict, dependency-free manifests for the ControlFoley upstream baseline.

Phase 2A records reproducibility evidence only.  It never synthesizes media,
hashes, CUDA measurements, or acceptance thresholds, and does not import the
upstream project.  A later Phase owns the real adapter invocation.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import math
import platform
import re
import subprocess
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

SUPPORTED_SOURCE_REPOSITORY = "https://github.com/xiaomi-research/controlfoley"
SUPPORTED_SOURCE_REVISION = "6858cd12a48d141201e3266e7abe1f38357a133e"
SUPPORTED_VARIANT = "large_44k"
SUPPORTED_PRECISION = "fp32"
SCHEMA_VERSION = 1
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_TASKS = frozenset(("V2A", "TV2A", "TC-V2A", "AC-V2A", "T2A"))
_TASK_INPUTS = {
    "V2A": (("video", "video"),),
    "TV2A": (("video", "video"), ("prompt", "text")),
    "TC-V2A": (("video", "video"), ("prompt", "text")),
    "AC-V2A": (("video", "video"), ("reference_audio", "audio")),
    "T2A": (("prompt", "text"),),
}
_EXTERNAL_WEIGHT_PATHS = (
    "ext_weights/cav_mae_st.pth",
    "ext_weights/music_speech_audioset_epoch_15_esc_89.98.pt",
    "ext_weights/synchformer_state_dict.pth",
    "ext_weights/v1-44.pth",
)
_METRICS = (
    "peak",
    "rms",
    "mae",
    "max_absolute_error",
    "waveform_cosine_similarity",
    "mel_spectrogram_distance",
)


class SchemaValidationError(ValueError):
    """Raised when a baseline manifest is incomplete, ambiguous, or unsafe."""


class GpuPrerequisitesUnavailable(RuntimeError):
    """Raised by callers that choose to require an optional GPU setup."""


def _mapping(value: Any, name: str, keys: Iterable[str]) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise SchemaValidationError("{0} must be an object".format(name))
    actual = set(value)
    expected = set(keys)
    missing = expected - actual
    extra = actual - expected
    if missing or extra:
        raise SchemaValidationError(
            "{0} keys must be exactly {1}; missing={2}, extra={3}".format(
                name, sorted(expected), sorted(missing), sorted(extra)
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


def _number_or_none(value: Any, name: str) -> Optional[float]:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SchemaValidationError("{0} must be a number or null".format(name))
    number = float(value)
    if not math.isfinite(number):
        raise SchemaValidationError("{0} must be finite".format(name))
    return number


def manifest_sha256(value: Mapping[str, Any]) -> str:
    """Return a real, canonical JSON SHA-256 for a deployment/result binding."""

    encoded = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True)
class Fingerprint:
    """A digest whose missing state is explicit rather than represented by a fake."""

    status: str
    sha256: Optional[str]
    size_bytes: Optional[int]

    @classmethod
    def from_dict(cls, value: Any, name: str) -> "Fingerprint":
        data = _mapping(value, name, ("status", "sha256", "size_bytes"))
        status = _string(data["status"], "{0}.status".format(name))
        sha256 = data["sha256"]
        size = data["size_bytes"]
        if status == "pending":
            if sha256 is not None or size is not None:
                raise SchemaValidationError(
                    "{0} pending fingerprint must not contain a digest or size".format(name)
                )
            return cls(status=status, sha256=None, size_bytes=None)
        if status != "verified":
            raise SchemaValidationError("{0}.status must be pending or verified".format(name))
        if not isinstance(sha256, str) or not _SHA256.fullmatch(sha256) or len(set(sha256)) == 1:
            raise SchemaValidationError("{0}.sha256 must be a lowercase full SHA-256".format(name))
        return cls(
            status=status, sha256=sha256, size_bytes=_integer(size, "{0}.size_bytes".format(name))
        )

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class ExternalWeight:
    relative_path: str
    fingerprint: Fingerprint

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "ExternalWeight":
        data = _mapping(
            value,
            "deployment.external_weights[{0}]".format(index),
            ("relative_path", "fingerprint"),
        )
        relative_path = _string(data["relative_path"], "external weight relative_path")
        path = Path(relative_path)
        if path.is_absolute() or ".." in path.parts:
            raise SchemaValidationError("external weight paths must be safe relative paths")
        return cls(
            relative_path=relative_path,
            fingerprint=Fingerprint.from_dict(data["fingerprint"], "external weight fingerprint"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"relative_path": self.relative_path, "fingerprint": self.fingerprint.to_dict()}


@dataclass(frozen=True)
class ControlFoleyDeploymentManifest:
    schema_version: int
    deployment_id: str
    upstream_repository: str
    source_revision: str
    variant: str
    precision: str
    checkpoint_relative_path: str
    checkpoint: Fingerprint
    external_weights: Tuple[ExternalWeight, ...]

    @classmethod
    def from_dict(cls, value: Any) -> "ControlFoleyDeploymentManifest":
        data = _mapping(
            value,
            "deployment",
            (
                "schema_version",
                "deployment_id",
                "upstream_repository",
                "source_revision",
                "variant",
                "precision",
                "checkpoint_relative_path",
                "checkpoint",
                "external_weights",
            ),
        )
        schema_version = _integer(data["schema_version"], "deployment.schema_version", 1)
        if schema_version != SCHEMA_VERSION:
            raise SchemaValidationError("unsupported deployment schema_version")
        repository = _string(data["upstream_repository"], "deployment.upstream_repository")
        revision = _string(data["source_revision"], "deployment.source_revision")
        if repository != SUPPORTED_SOURCE_REPOSITORY:
            raise SchemaValidationError("deployment uses an unsupported upstream repository")
        if revision != SUPPORTED_SOURCE_REVISION:
            raise SchemaValidationError("deployment source revision must equal the supported lock")
        variant = _string(data["variant"], "deployment.variant")
        precision = _string(data["precision"], "deployment.precision")
        if variant != SUPPORTED_VARIANT or precision != SUPPORTED_PRECISION:
            raise SchemaValidationError(
                "deployment variant/precision must equal the locked upstream defaults"
            )
        deployment_id = _string(data["deployment_id"], "deployment.deployment_id")
        if deployment_id != "controlfoley-large44k-fp32-v1":
            raise SchemaValidationError("deployment id must identify the locked FP32 baseline")
        checkpoint_relative_path = _string(
            data["checkpoint_relative_path"], "deployment.checkpoint_relative_path"
        )
        if checkpoint_relative_path != "weights/controlfoley.pth":
            raise SchemaValidationError(
                "deployment checkpoint path must be weights/controlfoley.pth"
            )
        weights_value = data["external_weights"]
        if not isinstance(weights_value, list):
            raise SchemaValidationError("deployment.external_weights must be an array")
        external_weights = tuple(
            ExternalWeight.from_dict(item, index) for index, item in enumerate(weights_value)
        )
        if tuple(sorted(weight.relative_path for weight in external_weights)) != tuple(
            sorted(_EXTERNAL_WEIGHT_PATHS)
        ):
            raise SchemaValidationError(
                "deployment must declare exactly the four official external weight paths"
            )
        if len({weight.relative_path for weight in external_weights}) != len(external_weights):
            raise SchemaValidationError("deployment external weight paths must be unique")
        return cls(
            schema_version=schema_version,
            deployment_id=deployment_id,
            upstream_repository=repository,
            source_revision=revision,
            variant=variant,
            precision=precision,
            checkpoint_relative_path=checkpoint_relative_path,
            checkpoint=Fingerprint.from_dict(data["checkpoint"], "deployment.checkpoint"),
            external_weights=external_weights,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["checkpoint"] = self.checkpoint.to_dict()
        data["external_weights"] = [weight.to_dict() for weight in self.external_weights]
        return data


@dataclass(frozen=True)
class FixtureInput:
    role: str
    kind: str
    fingerprint: Fingerprint

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "FixtureInput":
        data = _mapping(value, "fixture.inputs[{0}]".format(index), ("role", "kind", "fingerprint"))
        return cls(
            role=_string(data["role"], "fixture input role"),
            kind=_string(data["kind"], "fixture input kind"),
            fingerprint=Fingerprint.from_dict(data["fingerprint"], "fixture input fingerprint"),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {"role": self.role, "kind": self.kind, "fingerprint": self.fingerprint.to_dict()}


@dataclass(frozen=True)
class FixtureManifest:
    schema_version: int
    fixture_id: str
    task: str
    duration_seconds: float
    seed: int
    num_steps: int
    guidance_scale: float
    inputs: Tuple[FixtureInput, ...]

    @classmethod
    def from_dict(cls, value: Any) -> "FixtureManifest":
        data = _mapping(
            value,
            "fixture",
            (
                "schema_version",
                "fixture_id",
                "task",
                "duration_seconds",
                "seed",
                "num_steps",
                "guidance_scale",
                "inputs",
            ),
        )
        schema_version = _integer(data["schema_version"], "fixture.schema_version", 1)
        if schema_version != SCHEMA_VERSION:
            raise SchemaValidationError("unsupported fixture schema_version")
        task = _string(data["task"], "fixture.task")
        if task not in _TASKS:
            raise SchemaValidationError("fixture.task is not one of the supported baseline tasks")
        duration = _number_or_none(data["duration_seconds"], "fixture.duration_seconds")
        guidance = _number_or_none(data["guidance_scale"], "fixture.guidance_scale")
        if duration is None or duration <= 0:
            raise SchemaValidationError("fixture.duration_seconds must be a positive number")
        if guidance is None or guidance < 0:
            raise SchemaValidationError("fixture.guidance_scale must be a non-negative number")
        inputs_value = data["inputs"]
        if not isinstance(inputs_value, list) or not inputs_value:
            raise SchemaValidationError("fixture.inputs must be a non-empty array")
        inputs = tuple(
            FixtureInput.from_dict(item, index) for index, item in enumerate(inputs_value)
        )
        if len({item.role for item in inputs}) != len(inputs):
            raise SchemaValidationError("fixture input roles must be unique")
        expected_inputs = _TASK_INPUTS[task]
        if tuple((item.role, item.kind) for item in inputs) != expected_inputs:
            raise SchemaValidationError(
                "fixture input roles/kinds must match {0} exactly".format(task)
            )
        return cls(
            schema_version=schema_version,
            fixture_id=_string(data["fixture_id"], "fixture.fixture_id"),
            task=task,
            duration_seconds=duration,
            seed=_integer(data["seed"], "fixture.seed"),
            num_steps=_integer(data["num_steps"], "fixture.num_steps", 1),
            guidance_scale=guidance,
            inputs=inputs,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "fixture_id": self.fixture_id,
            "task": self.task,
            "duration_seconds": self.duration_seconds,
            "seed": self.seed,
            "num_steps": self.num_steps,
            "guidance_scale": self.guidance_scale,
            "inputs": [item.to_dict() for item in self.inputs],
        }


def _metrics_from_dict(value: Any, name: str) -> Dict[str, Optional[float]]:
    data = _mapping(value, name, _METRICS)
    return {
        metric: _number_or_none(data[metric], "{0}.{1}".format(name, metric)) for metric in _METRICS
    }


@dataclass(frozen=True)
class RepeatEvidence:
    output: Fingerprint
    waveform: Mapping[str, Optional[int]]
    wall_time_seconds: Optional[float]
    peak_allocated_bytes: Optional[int]
    peak_reserved_bytes: Optional[int]

    @classmethod
    def from_dict(cls, value: Any, index: int) -> "RepeatEvidence":
        data = _mapping(
            value,
            "result.repeat_evidence[{0}]".format(index),
            (
                "output",
                "waveform",
                "wall_time_seconds",
                "peak_allocated_bytes",
                "peak_reserved_bytes",
            ),
        )
        waveform_data = _mapping(
            data["waveform"], "repeat waveform", ("channels", "samples", "sample_rate")
        )
        waveform = {
            key: None
            if waveform_data[key] is None
            else _integer(waveform_data[key], "repeat waveform.{0}".format(key), 1)
            for key in waveform_data
        }
        wall_time = _number_or_none(data["wall_time_seconds"], "repeat wall_time_seconds")
        allocated = data["peak_allocated_bytes"]
        reserved = data["peak_reserved_bytes"]
        if allocated is not None:
            allocated = _integer(allocated, "repeat peak_allocated_bytes")
        if reserved is not None:
            reserved = _integer(reserved, "repeat peak_reserved_bytes")
        return cls(
            output=Fingerprint.from_dict(data["output"], "repeat output"),
            waveform=waveform,
            wall_time_seconds=wall_time,
            peak_allocated_bytes=allocated,
            peak_reserved_bytes=reserved,
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "output": self.output.to_dict(),
            "waveform": dict(self.waveform),
            "wall_time_seconds": self.wall_time_seconds,
            "peak_allocated_bytes": self.peak_allocated_bytes,
            "peak_reserved_bytes": self.peak_reserved_bytes,
        }


@dataclass(frozen=True)
class BaselineResultManifest:
    """Result schema that keeps absent measurement and threshold states explicit."""

    schema_version: int
    run_id: str
    fixture_id: str
    deployment_id: str
    deployment_manifest_sha256: str
    fixture_manifest_sha256: str
    source_revision: str
    checkpoint: Fingerprint
    input_fingerprints: Mapping[str, Fingerprint]
    parameters: Mapping[str, Any]
    state: str
    environment: Mapping[str, Any]
    waveform: Mapping[str, Optional[int]]
    wall_time_seconds: Optional[float]
    peak_allocated_bytes: Optional[int]
    peak_reserved_bytes: Optional[int]
    self_repeat_metrics: Mapping[str, Optional[float]]
    self_repeat_thresholds: Mapping[str, Optional[float]]
    repeat_evidence: Tuple[RepeatEvidence, ...]

    @classmethod
    def from_dict(cls, value: Any) -> "BaselineResultManifest":
        data = _mapping(
            value,
            "result",
            (
                "schema_version",
                "run_id",
                "fixture_id",
                "deployment_id",
                "deployment_manifest_sha256",
                "fixture_manifest_sha256",
                "source_revision",
                "checkpoint",
                "input_fingerprints",
                "parameters",
                "state",
                "environment",
                "waveform",
                "wall_time_seconds",
                "peak_allocated_bytes",
                "peak_reserved_bytes",
                "self_repeat_metrics",
                "self_repeat_thresholds",
                "repeat_evidence",
            ),
        )
        schema_version = _integer(data["schema_version"], "result.schema_version", 1)
        if schema_version != SCHEMA_VERSION:
            raise SchemaValidationError("unsupported result schema_version")
        state = _string(data["state"], "result.state")
        if state not in ("planned", "completed", "failed"):
            raise SchemaValidationError("result.state must be planned, completed, or failed")
        source_revision = _string(data["source_revision"], "result.source_revision")
        if source_revision != SUPPORTED_SOURCE_REVISION:
            raise SchemaValidationError("result source revision must equal the supported lock")
        deployment_manifest_sha256 = _string(
            data["deployment_manifest_sha256"], "result.deployment_manifest_sha256"
        )
        if not _SHA256.fullmatch(deployment_manifest_sha256):
            raise SchemaValidationError("result deployment_manifest_sha256 must be a full SHA-256")
        fixture_manifest_sha256 = _string(
            data["fixture_manifest_sha256"], "result.fixture_manifest_sha256"
        )
        if not _SHA256.fullmatch(fixture_manifest_sha256):
            raise SchemaValidationError("result fixture_manifest_sha256 must be a full SHA-256")
        checkpoint = Fingerprint.from_dict(data["checkpoint"], "result.checkpoint")
        input_value = data["input_fingerprints"]
        if not isinstance(input_value, Mapping) or not input_value:
            raise SchemaValidationError("result.input_fingerprints must be a non-empty object")
        input_fingerprints = {
            _string(role, "result input role"): Fingerprint.from_dict(
                fingerprint, "result input fingerprint"
            )
            for role, fingerprint in input_value.items()
        }
        parameters = data["parameters"]
        if not isinstance(parameters, Mapping):
            raise SchemaValidationError("result.parameters must be a sanitized object")
        if any("/" in str(item) or "\\" in str(item) for item in parameters.values()):
            raise SchemaValidationError("result.parameters must not contain file paths")
        environment = validate_environment_manifest(data["environment"])
        waveform_data = _mapping(
            data["waveform"], "result.waveform", ("channels", "samples", "sample_rate")
        )
        waveform = {
            key: None
            if waveform_data[key] is None
            else _integer(waveform_data[key], "waveform.{0}".format(key), 1)
            for key in waveform_data
        }
        wall_time = _number_or_none(data["wall_time_seconds"], "result.wall_time_seconds")
        allocated = data["peak_allocated_bytes"]
        reserved = data["peak_reserved_bytes"]
        if allocated is not None:
            allocated = _integer(allocated, "result.peak_allocated_bytes")
        if reserved is not None:
            reserved = _integer(reserved, "result.peak_reserved_bytes")
        metrics = _metrics_from_dict(data["self_repeat_metrics"], "result.self_repeat_metrics")
        thresholds = _metrics_from_dict(
            data["self_repeat_thresholds"], "result.self_repeat_thresholds"
        )
        evidence_value = data["repeat_evidence"]
        if not isinstance(evidence_value, list):
            raise SchemaValidationError("result.repeat_evidence must be an array")
        repeat_evidence = tuple(
            RepeatEvidence.from_dict(item, index) for index, item in enumerate(evidence_value)
        )
        if state == "planned" and any(
            item is not None
            for item in (
                wall_time,
                allocated,
                reserved,
                *waveform.values(),
                *metrics.values(),
                *thresholds.values(),
            )
        ):
            raise SchemaValidationError("planned results must not contain fabricated measurements")
        if state == "planned" and repeat_evidence:
            raise SchemaValidationError("planned results must not contain repeat evidence")
        if state == "completed" and any(
            item is None
            for item in (
                wall_time,
                allocated,
                reserved,
                *waveform.values(),
                *metrics.values(),
            )
        ):
            raise SchemaValidationError(
                "completed results require waveform, timing, VRAM, and raw self-repeat metrics"
            )
        if state == "completed":
            if checkpoint.status != "verified" or any(
                fingerprint.status != "verified" for fingerprint in input_fingerprints.values()
            ):
                raise SchemaValidationError(
                    "completed results require verified checkpoint and input fingerprints"
                )
            if len(repeat_evidence) != 2:
                raise SchemaValidationError(
                    "completed results require exactly two repeat evidence records"
                )
            for evidence in repeat_evidence:
                if (
                    evidence.output.status != "verified"
                    or evidence.wall_time_seconds is None
                    or evidence.wall_time_seconds <= 0
                    or evidence.peak_allocated_bytes is None
                    or evidence.peak_reserved_bytes is None
                    or any(value is None for value in evidence.waveform.values())
                ):
                    raise SchemaValidationError("completed repeat evidence is incomplete")
        return cls(
            schema_version=schema_version,
            run_id=_string(data["run_id"], "result.run_id"),
            fixture_id=_string(data["fixture_id"], "result.fixture_id"),
            deployment_id=_string(data["deployment_id"], "result.deployment_id"),
            deployment_manifest_sha256=deployment_manifest_sha256,
            fixture_manifest_sha256=fixture_manifest_sha256,
            source_revision=source_revision,
            checkpoint=checkpoint,
            input_fingerprints=input_fingerprints,
            parameters=dict(parameters),
            state=state,
            environment=environment,
            waveform=waveform,
            wall_time_seconds=wall_time,
            peak_allocated_bytes=allocated,
            peak_reserved_bytes=reserved,
            self_repeat_metrics=metrics,
            self_repeat_thresholds=thresholds,
            repeat_evidence=repeat_evidence,
        )

    def to_dict(self) -> Dict[str, Any]:
        data = asdict(self)
        data["checkpoint"] = self.checkpoint.to_dict()
        data["input_fingerprints"] = {
            role: fingerprint.to_dict() for role, fingerprint in self.input_fingerprints.items()
        }
        data["repeat_evidence"] = [evidence.to_dict() for evidence in self.repeat_evidence]
        return data


def validate_environment_manifest(value: Any) -> Dict[str, Any]:
    data = _mapping(
        value,
        "environment",
        ("schema_version", "captured_at", "python_version", "platform", "torch", "cuda"),
    )
    if _integer(data["schema_version"], "environment.schema_version", 1) != SCHEMA_VERSION:
        raise SchemaValidationError("unsupported environment schema_version")
    _string(data["captured_at"], "environment.captured_at")
    _string(data["python_version"], "environment.python_version")
    platform_data = _mapping(
        data["platform"], "environment.platform", ("system", "release", "machine")
    )
    torch_data = _mapping(data["torch"], "environment.torch", ("installed", "version"))
    cuda_data = _mapping(data["cuda"], "environment.cuda", ("available", "device_count"))
    if not isinstance(torch_data["installed"], bool):
        raise SchemaValidationError("environment.torch.installed must be boolean")
    if torch_data["version"] is not None and not isinstance(torch_data["version"], str):
        raise SchemaValidationError("environment.torch.version must be string or null")
    if torch_data["installed"] and torch_data["version"] is None:
        raise SchemaValidationError("installed torch must include its version")
    if not isinstance(cuda_data["available"], bool):
        raise SchemaValidationError("environment.cuda.available must be boolean")
    _integer(cuda_data["device_count"], "environment.cuda.device_count")
    if cuda_data["available"] and cuda_data["device_count"] < 1:
        raise SchemaValidationError("available CUDA must report at least one device")
    return {
        "schema_version": data["schema_version"],
        "captured_at": data["captured_at"],
        "python_version": data["python_version"],
        "platform": {
            key: _string(platform_data[key], "environment.platform.{0}".format(key))
            for key in platform_data
        },
        "torch": dict(torch_data),
        "cuda": dict(cuda_data),
    }


def collect_sanitized_environment() -> Dict[str, Any]:
    """Collect portable runtime facts without hostname, paths, or environment values."""

    torch_installed = importlib.util.find_spec("torch") is not None
    torch_version: Optional[str] = None
    cuda_available = False
    device_count = 0
    if torch_installed:
        try:
            import torch  # type: ignore[import-not-found]

            torch_version = str(torch.__version__)
            cuda_available = bool(torch.cuda.is_available())
            device_count = int(torch.cuda.device_count()) if cuda_available else 0
        except Exception:
            # A broken optional torch install is a captured absence, not a reason
            # to invent an environment value or make CPU validation unavailable.
            torch_installed = False
    return {
        "schema_version": SCHEMA_VERSION,
        "captured_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "python_version": platform.python_version(),
        "platform": {
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "torch": {"installed": torch_installed, "version": torch_version},
        "cuda": {"available": cuda_available, "device_count": device_count},
    }


@dataclass(frozen=True)
class GpuPrerequisites:
    available: bool
    reasons: Tuple[str, ...]
    diagnostics: Tuple[str, ...] = ()


def _git_revision(source_dir: Path) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _git_output(source_dir: Path, arguments: Sequence[str]) -> Optional[str]:
    try:
        result = subprocess.run(
            ["git", "-C", str(source_dir), *arguments],
            check=True,
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return result.stdout.strip()


def _is_supported_origin(origin: str) -> bool:
    return origin.rstrip("/") in (
        "https://github.com/xiaomi-research/controlfoley",
        "https://github.com/xiaomi-research/controlfoley.git",
        "git@github.com:xiaomi-research/controlfoley",
        "git@github.com:xiaomi-research/controlfoley.git",
        "ssh://git@github.com/xiaomi-research/controlfoley",
        "ssh://git@github.com/xiaomi-research/controlfoley.git",
    )


def fingerprint_file(path: Path) -> Fingerprint:
    """Calculate a real full-file SHA-256; never return a placeholder digest."""

    digest = hashlib.sha256()
    size_bytes = 0
    with path.open("rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            size_bytes += len(chunk)
    return Fingerprint(status="verified", sha256=digest.hexdigest(), size_bytes=size_bytes)


def fingerprint_text(value: str) -> Fingerprint:
    encoded = value.encode("utf-8")
    return Fingerprint(
        status="verified", sha256=hashlib.sha256(encoded).hexdigest(), size_bytes=len(encoded)
    )


def _fingerprint_matches(expected: Fingerprint, actual: Fingerprint, label: str) -> Optional[str]:
    if expected.status != "verified":
        return "{0} fingerprint is pending".format(label)
    if expected != actual:
        return "{0} fingerprint does not match the manifest".format(label)
    return None


def validate_materialized_weights(
    deployment: ControlFoleyDeploymentManifest, weights_dir: Path
) -> Tuple[str, ...]:
    """Verify all official external files and the main checkpoint before execution."""

    reasons: List[str] = []
    required = [(deployment.checkpoint_relative_path, deployment.checkpoint)] + [
        (weight.relative_path, weight.fingerprint) for weight in deployment.external_weights
    ]
    for relative_path, expected in required:
        candidate = weights_dir / relative_path
        if not candidate.is_file():
            reasons.append("required weight is missing: {0}".format(relative_path))
            continue
        actual = fingerprint_file(candidate)
        mismatch = _fingerprint_matches(expected, actual, "weight {0}".format(relative_path))
        if mismatch is not None:
            reasons.append(mismatch)
    return tuple(reasons)


def detect_gpu_prerequisites(
    source_dir: Optional[Path],
    weights_dir: Optional[Path],
    deployment: Optional[ControlFoleyDeploymentManifest] = None,
) -> GpuPrerequisites:
    """Report every missing prerequisite; this function never downloads anything."""

    reasons: List[str] = []
    diagnostics: List[str] = []
    if source_dir is None:
        reasons.append("CONTROLFOLEY_SOURCE_DIR is not set")
    elif not source_dir.is_dir():
        reasons.append("ControlFoley source directory does not exist")
    else:
        if not (source_dir / "demo.py").is_file():
            reasons.append("ControlFoley source directory is missing demo.py")
        revision = _git_revision(source_dir)
        if revision is None:
            reasons.append("ControlFoley source revision cannot be read with git")
        elif revision != SUPPORTED_SOURCE_REVISION:
            reasons.append("ControlFoley source revision does not match the supported lock")
        dirty = _git_output(source_dir, ("status", "--porcelain"))
        if dirty is None:
            reasons.append("ControlFoley source dirty state cannot be read with git")
        elif dirty:
            reasons.append("ControlFoley source tree is dirty")
        origin = _git_output(source_dir, ("remote", "get-url", "origin"))
        if origin is None:
            reasons.append("ControlFoley source origin is unavailable")
        else:
            diagnostics.append(
                "ControlFoley source origin matches supported repository: {0}".format(
                    _is_supported_origin(origin)
                )
            )
            if not _is_supported_origin(origin):
                reasons.append("ControlFoley source origin does not match the supported repository")
    if weights_dir is None:
        reasons.append("CONTROLFOLEY_WEIGHTS_DIR is not set")
    elif not weights_dir.is_dir():
        reasons.append("ControlFoley weights directory does not exist")
    elif deployment is not None:
        reasons.extend(validate_materialized_weights(deployment, weights_dir))
    environment = collect_sanitized_environment()
    if not environment["cuda"]["available"]:
        reasons.append("CUDA is unavailable")
    return GpuPrerequisites(
        available=not reasons, reasons=tuple(reasons), diagnostics=tuple(diagnostics)
    )


def require_gpu_prerequisites(
    source_dir: Optional[Path],
    weights_dir: Optional[Path],
    deployment: Optional[ControlFoleyDeploymentManifest] = None,
) -> GpuPrerequisites:
    """Raise an actionable diagnostic for callers that require the GPU setup."""

    prerequisites = detect_gpu_prerequisites(source_dir, weights_dir, deployment)
    if not prerequisites.available:
        raise GpuPrerequisitesUnavailable(
            "ControlFoley GPU prerequisites unavailable: {0}".format(
                "; ".join(prerequisites.reasons)
            )
        )
    return prerequisites


def compare_waveforms(
    first_path: Path, second_path: Path
) -> Tuple[Dict[str, float], Dict[str, int]]:
    """Calculate raw self-repeat evidence with delayed torch/torchaudio imports."""

    try:
        import torch  # type: ignore[import-not-found]
        import torchaudio  # type: ignore[import-not-found]
    except ImportError as error:
        raise GpuPrerequisitesUnavailable(
            "torch and torchaudio are required only to capture a completed baseline"
        ) from error
    first, sample_rate = torchaudio.load(str(first_path))
    second, second_rate = torchaudio.load(str(second_path))
    if sample_rate != second_rate or tuple(first.shape) != tuple(second.shape):
        raise SchemaValidationError(
            "self-repeat outputs have different sample rate or waveform shape"
        )
    if not bool(torch.isfinite(first).all()) or not bool(torch.isfinite(second).all()):
        raise SchemaValidationError("self-repeat output contains non-finite samples")
    difference = first - second
    first_flat = first.reshape(-1)
    second_flat = second.reshape(-1)
    first_norm = torch.linalg.vector_norm(first_flat)
    second_norm = torch.linalg.vector_norm(second_flat)
    if float(first_norm) == 0.0 or float(second_norm) == 0.0:
        raise SchemaValidationError("self-repeat output has zero waveform norm")
    mel_transform = torchaudio.transforms.MelSpectrogram(sample_rate=sample_rate)
    mel_distance = torch.mean(
        torch.abs(torch.log1p(mel_transform(first)) - torch.log1p(mel_transform(second)))
    )
    metrics = {
        "peak": float(torch.max(torch.abs(first))),
        "rms": float(torch.sqrt(torch.mean(first.square()))),
        "mae": float(torch.mean(torch.abs(difference))),
        "max_absolute_error": float(torch.max(torch.abs(difference))),
        "waveform_cosine_similarity": float(
            torch.dot(first_flat, second_flat) / (first_norm * second_norm)
        ),
        "mel_spectrogram_distance": float(mel_distance),
    }
    waveform = {
        "channels": int(first.shape[0]),
        "samples": int(first.shape[1]),
        "sample_rate": int(sample_rate),
    }
    return metrics, waveform


def verify_result_bindings(
    deployment: ControlFoleyDeploymentManifest,
    fixture: FixtureManifest,
    result: BaselineResultManifest,
) -> None:
    """Offline audit that binds a completed result to sealed evidence."""

    if result.state != "completed":
        raise SchemaValidationError("only completed baseline results can be binding-verified")
    if result.deployment_id != deployment.deployment_id or result.fixture_id != fixture.fixture_id:
        raise SchemaValidationError("result identity does not match deployment/fixture")
    if result.source_revision != deployment.source_revision:
        raise SchemaValidationError("result source revision does not match deployment")
    if result.deployment_manifest_sha256 != manifest_sha256(deployment.to_dict()):
        raise SchemaValidationError("result deployment manifest fingerprint does not match")
    if result.fixture_manifest_sha256 != manifest_sha256(fixture.to_dict()):
        raise SchemaValidationError("result fixture manifest fingerprint does not match")
    if deployment.checkpoint.status != "verified" or result.checkpoint != deployment.checkpoint:
        raise SchemaValidationError(
            "result checkpoint fingerprint does not match sealed deployment"
        )
    fixture_inputs = {item.role: item.fingerprint for item in fixture.inputs}
    if any(item.status != "verified" for item in fixture_inputs.values()):
        raise SchemaValidationError("fixture is not sealed")
    if result.input_fingerprints != fixture_inputs:
        raise SchemaValidationError("result inputs do not match sealed fixture")
    expected = {
        "task": fixture.task,
        "variant": deployment.variant,
        "precision": deployment.precision,
        "video": None if "video" not in fixture_inputs else fixture_inputs["video"].to_dict(),
        "audio": (
            None
            if "reference_audio" not in fixture_inputs
            else fixture_inputs["reference_audio"].to_dict()
        ),
        "duration_seconds": fixture.duration_seconds,
        "cfg_strength": fixture.guidance_scale,
        "num_steps": fixture.num_steps,
        "seed": fixture.seed,
        "skip_video_composite": False,
        "mask_away_clip": False,
        "output": {"path_redacted": True, "repeat_specific": True},
        "prompt": None if "prompt" not in fixture_inputs else fixture_inputs["prompt"].to_dict(),
        "negative_prompt": None,
    }
    if set(result.parameters) != set(expected):
        raise SchemaValidationError("result parameters contain missing or unexpected fields")
    for name, value in expected.items():
        if result.parameters.get(name) != value:
            raise SchemaValidationError(
                "result parameter {0} does not match sealed evidence".format(name)
            )


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
