"""Experimental ControlFoley profiling with truthful CPU/CUDA capability states."""

from __future__ import annotations

import math
import re
import time
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Dict, Iterator, Mapping, Optional, Protocol

from nano_aural_runtime import ProfileReport

from .baseline import (
    ControlFoleyDeploymentManifest,
    GpuPrerequisites,
    GpuPrerequisitesUnavailable,
    detect_gpu_prerequisites,
)
from .staged import (
    CONTROLFOLEY_STAGE_ORDER,
    ControlFoleyOracleDeploymentBinding,
    ControlFoleyStage,
    validate_staged_backend_id,
)
from .tasks import (
    EXPERIMENTAL_STAGED_OPERATION,
    UPSTREAM_PARITY_OPERATION,
    ControlFoleyTaskKind,
)

_PYTHON_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15}")


class ControlFoleyProfileLevel(str, Enum):
    OFF = "off"
    INVOCATION = "invocation"
    STAGES = "stages"


class ControlFoleyProfileSealError(ValueError):
    """A configured source, manifest, fixture, or weight seal is invalid."""


@dataclass(frozen=True)
class ControlFoleyGpuProfilePreflight:
    """Classify configured seal failures separately from host CUDA absence."""

    seal_reasons: tuple[str, ...]
    capability_reasons: tuple[str, ...]

    @classmethod
    def from_prerequisites(
        cls, prerequisites: GpuPrerequisites
    ) -> "ControlFoleyGpuProfilePreflight":
        if not isinstance(prerequisites, GpuPrerequisites):
            raise TypeError("prerequisites must be GpuPrerequisites")
        capability = tuple(
            reason for reason in prerequisites.reasons if reason == "CUDA is unavailable"
        )
        seals = tuple(reason for reason in prerequisites.reasons if reason != "CUDA is unavailable")
        return cls(seal_reasons=seals, capability_reasons=capability)

    def require(self) -> None:
        # Seal failures take precedence when both the configured material and
        # this host are invalid. A configured bad lock must never look like a
        # harmless hardware skip.
        if self.seal_reasons:
            raise ControlFoleyProfileSealError(
                "ControlFoley GPU profile seals are invalid: {0}".format(
                    "; ".join(self.seal_reasons)
                )
            )
        if self.capability_reasons:
            raise GpuPrerequisitesUnavailable(
                "ControlFoley GPU profile capability is unavailable: {0}".format(
                    "; ".join(self.capability_reasons)
                )
            )


def require_controlfoley_gpu_profile_preflight(
    source_dir: Path,
    weights_dir: Path,
    deployment: ControlFoleyDeploymentManifest,
) -> ControlFoleyGpuProfilePreflight:
    preflight = ControlFoleyGpuProfilePreflight.from_prerequisites(
        detect_gpu_prerequisites(source_dir, weights_dir, deployment)
    )
    preflight.require()
    return preflight


@dataclass(frozen=True)
class ControlFoleyProfileBinding:
    operation: str
    deployment_fingerprint: str
    deployment_manifest_sha256: str
    source_revision: str
    checkpoint_sha256: str
    canonical_invocation_sha256: str
    staged_backend_id: Optional[str]

    def __post_init__(self) -> None:
        if self.operation not in (UPSTREAM_PARITY_OPERATION, EXPERIMENTAL_STAGED_OPERATION):
            raise ValueError("unsupported profile binding operation")
        _sha256(self.deployment_fingerprint, "deployment_fingerprint")
        ControlFoleyOracleDeploymentBinding(
            self.deployment_manifest_sha256,
            self.source_revision,
            self.checkpoint_sha256,
        )
        _sha256(self.canonical_invocation_sha256, "canonical_invocation_sha256")
        if self.operation == EXPERIMENTAL_STAGED_OPERATION:
            validate_staged_backend_id(self.staged_backend_id)
        elif self.staged_backend_id is not None:
            raise ValueError("upstream profile binding cannot name a staged backend")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "operation": self.operation,
                "deployment_fingerprint": self.deployment_fingerprint,
                "deployment_manifest_sha256": self.deployment_manifest_sha256,
                "source_revision": self.source_revision,
                "checkpoint_sha256": self.checkpoint_sha256,
                "canonical_invocation_sha256": self.canonical_invocation_sha256,
                "staged_backend_id": self.staged_backend_id,
            }
        )


@dataclass(frozen=True)
class ControlFoleyGpuProfileEvidence:
    """Path-free evidence envelope emitted only by the conditional GPU harness."""

    fixture_manifest_sha256: str
    artifact_sha256: str
    binding: ControlFoleyProfileBinding
    profile: ProfileReport

    def __post_init__(self) -> None:
        _sha256(self.fixture_manifest_sha256, "fixture_manifest_sha256")
        _sha256(self.artifact_sha256, "artifact_sha256")
        if not isinstance(self.binding, ControlFoleyProfileBinding):
            raise TypeError("binding must be ControlFoleyProfileBinding")
        if not isinstance(self.profile, ProfileReport):
            raise TypeError("profile must be ProfileReport")
        metadata = self.profile.metadata
        report_binding = metadata.get("binding")
        expected_keys = {
            "schema_version",
            "namespace",
            "status",
            "level",
            "operation",
            "cpu_clock",
            "total_cpu_seconds",
            "cuda",
            "binding",
            "stages",
        }
        if (
            set(metadata) != expected_keys
            or metadata.get("status") != "ok"
            or metadata.get("namespace") != "controlfoley"
            or metadata.get("operation") != EXPERIMENTAL_STAGED_OPERATION
            or not isinstance(report_binding, Mapping)
            or dict(report_binding) != dict(self.binding.to_dict())
        ):
            raise ValueError("GPU profile report is not bound to the sealed staged execution")
        if any(
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            for value in self.profile.metrics.values()
        ):
            raise ValueError("GPU profile metrics must be finite and nonnegative")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": 1,
                "kind": "controlfoley-experimental-profile-evidence",
                "fixture_manifest_sha256": self.fixture_manifest_sha256,
                "artifact_sha256": self.artifact_sha256,
                "execution_binding": self.binding.to_dict(),
                "profile": {
                    "metrics": dict(self.profile.metrics),
                    "metadata": _json_value(self.profile.metadata),
                },
            }
        )


@dataclass(frozen=True)
class ControlFoleyGpuProfileConfiguration:
    """Strict operator-only conditional-GPU harness configuration."""

    deployment: Path
    fixture: Path
    source_dir: Path
    weights_dir: Path
    backend_module: str
    task: ControlFoleyTaskKind
    profile_output: Path
    video: Optional[Path] = None
    audio: Optional[Path] = None
    prompt: Optional[str] = None

    @classmethod
    def from_dict(cls, value: object) -> "ControlFoleyGpuProfileConfiguration":
        if not isinstance(value, Mapping):
            raise ValueError("GPU profile configuration must be an object")
        try:
            task = ControlFoleyTaskKind(value["task"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("GPU profile task is invalid") from error
        task_keys = {
            ControlFoleyTaskKind.V2A: {"video"},
            ControlFoleyTaskKind.TV2A: {"video", "prompt"},
            ControlFoleyTaskKind.TC_V2A: {"video", "prompt"},
            ControlFoleyTaskKind.AC_V2A: {"video", "audio"},
            ControlFoleyTaskKind.T2A: {"prompt"},
        }[task]
        expected = {
            "deployment",
            "fixture",
            "source_dir",
            "weights_dir",
            "backend_module",
            "task",
            "profile_output",
            *task_keys,
        }
        if set(value) != expected:
            raise ValueError("GPU profile configuration has missing or unexpected fields")
        deployment = _absolute_existing_path(value["deployment"], "deployment", directory=False)
        fixture = _absolute_existing_path(value["fixture"], "fixture", directory=False)
        source_dir = _absolute_existing_path(value["source_dir"], "source_dir", directory=True)
        weights_dir = _absolute_existing_path(value["weights_dir"], "weights_dir", directory=True)
        output = _absolute_output_path(value["profile_output"])
        backend_module = value["backend_module"]
        if not isinstance(backend_module, str) or _PYTHON_MODULE.fullmatch(backend_module) is None:
            raise ValueError("backend_module must be a safe dotted Python module name")
        video = (
            _absolute_existing_path(value["video"], "video", directory=False)
            if "video" in task_keys
            else None
        )
        audio = (
            _absolute_existing_path(value["audio"], "audio", directory=False)
            if "audio" in task_keys
            else None
        )
        prompt = value["prompt"] if "prompt" in task_keys else None
        if prompt is not None and (
            not isinstance(prompt, str)
            or not prompt.strip()
            or any(character in prompt for character in ("\x00", "\r"))
        ):
            raise ValueError("prompt must be non-empty text without control delimiters")
        return cls(
            deployment=deployment,
            fixture=fixture,
            source_dir=source_dir,
            weights_dir=weights_dir,
            backend_module=backend_module,
            task=task,
            profile_output=output,
            video=video,
            audio=audio,
            prompt=prompt,
        )


@dataclass(frozen=True)
class ControlFoleyCudaObservation:
    elapsed_milliseconds: float
    allocated_bytes: int
    reserved_bytes: int
    peak_allocated_bytes: int
    peak_reserved_bytes: int

    def __post_init__(self) -> None:
        if (
            isinstance(self.elapsed_milliseconds, bool)
            or not isinstance(self.elapsed_milliseconds, (int, float))
            or not math.isfinite(self.elapsed_milliseconds)
            or self.elapsed_milliseconds < 0
        ):
            raise ValueError("CUDA elapsed_milliseconds must be finite and nonnegative")
        for name, value in (
            ("allocated_bytes", self.allocated_bytes),
            ("reserved_bytes", self.reserved_bytes),
            ("peak_allocated_bytes", self.peak_allocated_bytes),
            ("peak_reserved_bytes", self.peak_reserved_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{0} must be a nonnegative integer".format(name))
        if self.peak_allocated_bytes < self.allocated_bytes:
            raise ValueError("peak_allocated_bytes cannot be below allocated_bytes")
        if self.peak_reserved_bytes < self.reserved_bytes:
            raise ValueError("peak_reserved_bytes cannot be below reserved_bytes")
        if self.reserved_bytes < self.allocated_bytes:
            raise ValueError("reserved_bytes cannot be below allocated_bytes")
        if self.peak_reserved_bytes < self.peak_allocated_bytes:
            raise ValueError("peak_reserved_bytes cannot be below peak_allocated_bytes")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "elapsed_milliseconds": float(self.elapsed_milliseconds),
                "allocated_bytes": self.allocated_bytes,
                "reserved_bytes": self.reserved_bytes,
                "peak_allocated_bytes": self.peak_allocated_bytes,
                "peak_reserved_bytes": self.peak_reserved_bytes,
            }
        )


class ControlFoleyCudaProfileBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    def is_available(self) -> bool: ...

    def begin(self, observation_name: str) -> object: ...

    def end(self, token: object) -> ControlFoleyCudaObservation: ...


class TorchCudaProfileBackend:
    """Lazy torch CUDA-event capability used only when explicitly injected."""

    backend_id = "torch-cuda-events-v1"

    def __init__(self) -> None:
        try:
            import torch  # type: ignore[import-not-found]
        except ImportError as error:
            raise RuntimeError("torch is unavailable for CUDA profiling") from error
        self._torch = torch

    def is_available(self) -> bool:
        return bool(self._torch.cuda.is_available())

    def device_name(self) -> str:
        if not self.is_available():
            raise RuntimeError("CUDA is unavailable")
        value = self._torch.cuda.get_device_name(self._torch.cuda.current_device())
        if not isinstance(value, str) or not value:
            raise RuntimeError("CUDA device identity is unavailable")
        return value

    def begin(self, observation_name: str) -> object:
        _profile_name(observation_name)
        if not self.is_available():
            raise RuntimeError("CUDA is unavailable")
        self._torch.cuda.reset_peak_memory_stats()
        event = self._torch.cuda.Event(enable_timing=True)
        event.record()
        return event

    def end(self, token: object) -> ControlFoleyCudaObservation:
        if not self.is_available():
            raise RuntimeError("CUDA became unavailable")
        end_event = self._torch.cuda.Event(enable_timing=True)
        end_event.record()
        end_event.synchronize()
        elapsed = float(token.elapsed_time(end_event))  # type: ignore[attr-defined]
        return ControlFoleyCudaObservation(
            elapsed_milliseconds=elapsed,
            allocated_bytes=int(self._torch.cuda.memory_allocated()),
            reserved_bytes=int(self._torch.cuda.memory_reserved()),
            peak_allocated_bytes=int(self._torch.cuda.max_memory_allocated()),
            peak_reserved_bytes=int(self._torch.cuda.max_memory_reserved()),
        )


@dataclass(frozen=True)
class ControlFoleyStageProfile:
    name: str
    cpu_seconds: float
    cuda: Optional[ControlFoleyCudaObservation]

    def __post_init__(self) -> None:
        _profile_name(self.name)
        if (
            isinstance(self.cpu_seconds, bool)
            or not isinstance(self.cpu_seconds, (int, float))
            or not math.isfinite(self.cpu_seconds)
            or self.cpu_seconds < 0
        ):
            raise ValueError("cpu_seconds must be finite and nonnegative")
        if self.cuda is not None and not isinstance(self.cuda, ControlFoleyCudaObservation):
            raise TypeError("cuda must be ControlFoleyCudaObservation or None")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "name": self.name,
                "cpu_seconds": float(self.cpu_seconds),
                "cuda": None if self.cuda is None else self.cuda.to_dict(),
            }
        )


class ControlFoleyProfiler:
    """Stateless factory for per-invocation, single-flight recorders."""

    def __init__(
        self,
        level: ControlFoleyProfileLevel = ControlFoleyProfileLevel.OFF,
        cuda_backend: Optional[ControlFoleyCudaProfileBackend] = None,
        monotonic_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if not isinstance(level, ControlFoleyProfileLevel):
            raise TypeError("level must be ControlFoleyProfileLevel")
        if not callable(monotonic_ns):
            raise TypeError("monotonic_ns must be callable")
        if cuda_backend is not None:
            validate_staged_backend_id(getattr(cuda_backend, "backend_id", None))
        self._level = level
        self._cuda_backend = cuda_backend
        self._monotonic_ns = monotonic_ns

    @property
    def level(self) -> ControlFoleyProfileLevel:
        return self._level

    def begin(
        self, operation: str, binding: Optional[ControlFoleyProfileBinding] = None
    ) -> "ControlFoleyProfileRecorder":
        return ControlFoleyProfileRecorder(
            self._level, operation, binding, self._cuda_backend, self._monotonic_ns
        )


class ControlFoleyProfileRecorder:
    """One invocation's observation state; it never owns execution semantics."""

    def __init__(
        self,
        level: ControlFoleyProfileLevel,
        operation: str,
        binding: Optional[ControlFoleyProfileBinding],
        cuda_backend: Optional[ControlFoleyCudaProfileBackend],
        monotonic_ns: Callable[[], int],
    ) -> None:
        if operation not in (UPSTREAM_PARITY_OPERATION, EXPERIMENTAL_STAGED_OPERATION):
            raise ValueError("unsupported profiled operation")
        if level is not ControlFoleyProfileLevel.OFF and (
            not isinstance(binding, ControlFoleyProfileBinding) or binding.operation != operation
        ):
            raise ValueError("enabled profile requires an exact execution binding")
        self._level = level
        self._operation = operation
        self._binding = binding
        self._cuda_backend = cuda_backend
        self._clock = monotonic_ns
        self._stages: list[ControlFoleyStageProfile] = []
        self._profile_error: Optional[str] = None
        self._finished = False
        self._last_clock_ns: Optional[int] = None
        self._start_ns = (
            None if self._level is ControlFoleyProfileLevel.OFF else self._clock_value()
        )
        self._invocation_cuda_token: Optional[object] = None
        self._cuda_status = "unavailable"
        self._cuda_backend_id: Optional[str] = None
        # The upstream oracle runs in an isolated child process. Parent-process
        # CUDA events cannot observe it, so CUDA is intentionally unavailable
        # there even if the host has a device. Only the in-process staged path
        # may activate an injected CUDA capability.
        if (
            self._level is not ControlFoleyProfileLevel.OFF
            and self._operation == EXPERIMENTAL_STAGED_OPERATION
            and cuda_backend is not None
        ):
            try:
                if cuda_backend.is_available():
                    self._cuda_status = "available"
                    self._cuda_backend_id = validate_staged_backend_id(cuda_backend.backend_id)
                    if self._level is ControlFoleyProfileLevel.INVOCATION:
                        self._invocation_cuda_token = cuda_backend.begin("invocation")
            except Exception:
                self._cuda_failure()

    @contextmanager
    def observe(self, stage: ControlFoleyStage) -> Iterator[None]:
        if not isinstance(stage, ControlFoleyStage):
            raise TypeError("stage must be ControlFoleyStage")
        with self._observe_name(stage.value):
            yield

    @contextmanager
    def observe_upstream(self) -> Iterator[None]:
        with self._observe_name(UPSTREAM_PARITY_OPERATION):
            yield

    @contextmanager
    def _observe_name(self, name: str) -> Iterator[None]:
        if self._level is not ControlFoleyProfileLevel.STAGES:
            yield
            return
        start_ns = self._clock_value()
        cuda_token: Optional[object] = None
        if self._cuda_status == "available" and self._cuda_backend is not None:
            try:
                cuda_token = self._cuda_backend.begin(name)
            except Exception:
                self._cuda_failure()
        try:
            yield
        finally:
            end_ns = self._clock_value()
            cuda_observation: Optional[ControlFoleyCudaObservation] = None
            if (
                cuda_token is not None
                and self._cuda_status == "available"
                and self._cuda_backend is not None
            ):
                try:
                    cuda_observation = self._cuda_backend.end(cuda_token)
                    if not isinstance(cuda_observation, ControlFoleyCudaObservation):
                        raise TypeError("CUDA backend returned an invalid observation")
                except Exception:
                    self._cuda_failure()
                    cuda_observation = None
            cpu_seconds = self._elapsed_seconds(start_ns, end_ns)
            if cpu_seconds is not None:
                self._stages.append(ControlFoleyStageProfile(name, cpu_seconds, cuda_observation))

    def finish(self) -> ProfileReport:
        if self._finished:
            return ProfileReport(
                metadata={
                    "schema_version": 1,
                    "namespace": "controlfoley",
                    "status": "error",
                    "reason": "recorder_reused",
                }
            )
        self._finished = True
        if self._level is ControlFoleyProfileLevel.OFF:
            return ProfileReport()
        end_ns = self._clock_value()
        total_seconds = self._elapsed_seconds(self._start_ns, end_ns)
        if total_seconds is None:
            return self._error_report()
        if self._level is ControlFoleyProfileLevel.INVOCATION:
            cuda = self._end_invocation_cuda()
            self._stages = [ControlFoleyStageProfile("invocation", total_seconds, cuda)]
        if self._cuda_status == "error":
            self._stages = [
                ControlFoleyStageProfile(stage.name, stage.cpu_seconds, None)
                for stage in self._stages
            ]
        if not self._stages_are_exact():
            self._profile_error = "stage_accounting_error"
        if self._profile_error is not None:
            return self._error_report()
        metrics: Dict[str, float] = {
            "controlfoley.cpu.total_seconds": total_seconds,
        }
        for stage in self._stages:
            metrics["controlfoley.cpu.{0}.seconds".format(stage.name)] = float(stage.cpu_seconds)
            if stage.cuda is not None:
                prefix = "controlfoley.cuda.{0}.".format(stage.name)
                metrics[prefix + "milliseconds"] = float(stage.cuda.elapsed_milliseconds)
                metrics[prefix + "allocated_bytes"] = float(stage.cuda.allocated_bytes)
                metrics[prefix + "reserved_bytes"] = float(stage.cuda.reserved_bytes)
                metrics[prefix + "peak_allocated_bytes"] = float(stage.cuda.peak_allocated_bytes)
                metrics[prefix + "peak_reserved_bytes"] = float(stage.cuda.peak_reserved_bytes)
        return ProfileReport(
            metrics=metrics,
            metadata={
                "schema_version": 1,
                "namespace": "controlfoley",
                "status": "ok",
                "level": self._level.value,
                "operation": self._operation,
                "cpu_clock": "monotonic_ns",
                "total_cpu_seconds": total_seconds,
                "cuda": {
                    "status": self._cuda_status,
                    "backend_id": self._cuda_backend_id,
                },
                "binding": self._binding.to_dict() if self._binding is not None else None,
                "stages": tuple(stage.to_dict() for stage in self._stages),
            },
        )

    def abandon(self) -> None:
        if self._finished:
            return
        self._finished = True
        if self._invocation_cuda_token is not None and self._cuda_backend is not None:
            try:
                self._cuda_backend.end(self._invocation_cuda_token)
            except Exception:
                pass

    def _end_invocation_cuda(self) -> Optional[ControlFoleyCudaObservation]:
        if self._invocation_cuda_token is None or self._cuda_backend is None:
            return None
        try:
            observation = self._cuda_backend.end(self._invocation_cuda_token)
            if not isinstance(observation, ControlFoleyCudaObservation):
                raise TypeError("CUDA backend returned an invalid observation")
            return observation
        except Exception:
            self._cuda_failure()
            return None

    def _clock_value(self) -> Optional[int]:
        try:
            value = self._clock()
        except Exception:
            self._profile_error = "cpu_clock_error"
            return None
        if (
            isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            or (self._last_clock_ns is not None and value < self._last_clock_ns)
        ):
            self._profile_error = "cpu_clock_error"
            return None
        self._last_clock_ns = value
        return value

    def _elapsed_seconds(self, start_ns: Optional[int], end_ns: Optional[int]) -> Optional[float]:
        if start_ns is None or end_ns is None or end_ns < start_ns:
            self._profile_error = "cpu_clock_error"
            return None
        try:
            seconds = (end_ns - start_ns) / 1_000_000_000.0
        except OverflowError:
            self._profile_error = "cpu_clock_error"
            return None
        if not math.isfinite(seconds):
            self._profile_error = "cpu_clock_error"
            return None
        return seconds

    def _cuda_failure(self) -> None:
        self._cuda_status = "error"
        self._cuda_backend_id = None

    def _stages_are_exact(self) -> bool:
        names = tuple(stage.name for stage in self._stages)
        if self._level is ControlFoleyProfileLevel.INVOCATION:
            return names == ("invocation",)
        if self._operation == UPSTREAM_PARITY_OPERATION:
            return names == (UPSTREAM_PARITY_OPERATION,)
        return names == tuple(stage.value for stage in CONTROLFOLEY_STAGE_ORDER)

    def _error_report(self) -> ProfileReport:
        return ProfileReport(
            metadata={
                "schema_version": 1,
                "namespace": "controlfoley",
                "status": "error",
                "reason": self._profile_error or "profile_error",
                "level": self._level.value,
                "operation": self._operation,
                "cuda": {"status": self._cuda_status, "backend_id": None},
                "binding": self._binding.to_dict() if self._binding is not None else None,
            }
        )


def _profile_name(value: str) -> None:
    allowed = {
        "invocation",
        UPSTREAM_PARITY_OPERATION,
        *(stage.value for stage in CONTROLFOLEY_STAGE_ORDER),
    }
    if value not in allowed:
        raise ValueError("unsupported profile observation name")


def _sha256(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or len(set(value)) == 1
    ):
        raise ValueError("{0} must be a lowercase full SHA-256".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be hexadecimal".format(name)) from error


def _absolute_existing_path(value: object, name: str, directory: bool) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("{0} must be an absolute path".format(name))
    path = Path(value)
    if not path.is_absolute():
        raise ValueError("{0} must be an absolute path".format(name))
    if directory and not path.is_dir():
        raise ValueError("{0} must be an existing directory".format(name))
    if not directory and not path.is_file():
        raise ValueError("{0} must be an existing regular file".format(name))
    return path.resolve(strict=True)


def _absolute_output_path(value: object) -> Path:
    if not isinstance(value, str) or not value:
        raise ValueError("profile_output must be an absolute new path")
    path = Path(value)
    if not path.is_absolute() or path.exists() or not path.parent.is_dir():
        raise ValueError("profile_output must be an absolute new path in an existing directory")
    return path.parent.resolve(strict=True) / path.name


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
