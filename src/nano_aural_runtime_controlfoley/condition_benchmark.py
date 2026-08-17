"""Reproducible raw benchmark-matrix evidence for experimental L2 caching."""

from __future__ import annotations

import json
import math
import os
import secrets
import stat
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Callable, Mapping, Optional, Tuple

from nano_aural_runtime import CacheReport, ProfileReport

from .cache import ControlFoleyGpuCacheConfiguration

_CELLS = ("off", "l2_cold", "l2_warm")
_RTX_4090_DEVICE_NAMES = frozenset({"NVIDIA GeForce RTX 4090"})


@dataclass(frozen=True)
class ControlFoleyGpuL2BenchmarkConfiguration:
    base: ControlFoleyGpuCacheConfiguration
    condition_cache_root: Path
    condition_codec_version: str
    condition_schema_version: str
    expected_device_name: str
    repeats: int

    @classmethod
    def from_dict(cls, value: object) -> "ControlFoleyGpuL2BenchmarkConfiguration":
        if not isinstance(value, Mapping):
            raise ValueError("GPU L2 benchmark configuration must be an object")
        l2_fields = {
            "condition_cache_root",
            "condition_codec_version",
            "condition_schema_version",
            "expected_device_name",
            "repeats",
        }
        if not l2_fields.issubset(value):
            raise ValueError("GPU L2 benchmark configuration has missing fields")
        base_values = {key: item for key, item in value.items() if key not in l2_fields}
        base = ControlFoleyGpuCacheConfiguration.from_dict(base_values)
        if set(value) != set(base_values) | l2_fields:
            raise ValueError("GPU L2 benchmark configuration has unexpected fields")
        root_value = value["condition_cache_root"]
        if not isinstance(root_value, str):
            raise ValueError("condition_cache_root must be an absolute operator path")
        root = Path(root_value)
        if not root.is_absolute():
            raise ValueError("condition_cache_root must be absolute")
        try:
            if root.resolve(strict=True) != root:
                raise ValueError("condition_cache_root must not traverse symlinks")
            root_stat = root.lstat()
        except OSError as error:
            raise ValueError("condition_cache_root is unavailable") from error
        if (
            not stat.S_ISDIR(root_stat.st_mode)
            or stat.S_ISLNK(root_stat.st_mode)
            or root_stat.st_mode & 0o077
        ):
            raise ValueError("condition_cache_root must be a private real directory")
        for protected in (base.source_dir, base.weights_dir):
            root_inside_protected = _is_relative_to(root, protected)
            protected_inside_root = _is_relative_to(protected, root)
            if root_inside_protected or protected_inside_root:
                raise ValueError("condition_cache_root must be outside locked source and weights")
        codec = value["condition_codec_version"]
        schema = value["condition_schema_version"]
        _safe_id(codec, "condition_codec_version")
        _safe_id(schema, "condition_schema_version")
        expected_device_name = _safe_device_name(value["expected_device_name"])
        evidence_output = base.evidence_output
        for protected in (base.source_dir, base.weights_dir, root):
            if _is_relative_to(evidence_output, protected) or _is_relative_to(
                protected, evidence_output
            ):
                raise ValueError(
                    "evidence_output must be outside locked source, weights, and cache root"
                )
        repeats = value["repeats"]
        if isinstance(repeats, bool) or not isinstance(repeats, int) or not 1 <= repeats <= 20:
            raise ValueError("repeats must be an integer between 1 and 20")
        return cls(base, root, codec, schema, expected_device_name, repeats)


def _is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class ControlFoleyL2BenchmarkPlan:
    deployment_manifest_sha256: str
    source_revision: str
    checkpoint_sha256: str
    fixture_manifest_sha256: str
    canonical_invocation_sha256: str
    backend_id: str
    uncached_deployment_fingerprint: str
    l2_deployment_fingerprint: str
    condition_codec_version: str
    condition_schema_version: str
    base_cache_policy_fingerprint: str
    condition_cache_policy_fingerprint: str
    repeats: int
    expected_device_name: Optional[str] = None
    cells: Tuple[str, ...] = _CELLS
    schema_version: int = 1
    state: str = "planned"

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.state != "planned":
            raise ValueError("benchmark plan schema/state is invalid")
        for name, value in (
            ("deployment_manifest_sha256", self.deployment_manifest_sha256),
            ("checkpoint_sha256", self.checkpoint_sha256),
            ("fixture_manifest_sha256", self.fixture_manifest_sha256),
            ("canonical_invocation_sha256", self.canonical_invocation_sha256),
            ("uncached_deployment_fingerprint", self.uncached_deployment_fingerprint),
            ("l2_deployment_fingerprint", self.l2_deployment_fingerprint),
            ("base_cache_policy_fingerprint", self.base_cache_policy_fingerprint),
            ("condition_cache_policy_fingerprint", self.condition_cache_policy_fingerprint),
        ):
            _sha256(value, name)
        _git_revision(self.source_revision)
        _safe_id(self.backend_id, "backend_id")
        _safe_id(self.condition_codec_version, "condition_codec_version")
        _safe_id(self.condition_schema_version, "condition_schema_version")
        if self.expected_device_name is not None:
            _safe_device_name(self.expected_device_name)
        if (
            isinstance(self.repeats, bool)
            or not isinstance(self.repeats, int)
            or not 1 <= self.repeats <= 20
        ):
            raise ValueError("benchmark repeats must be an integer between 1 and 20")
        if self.cells != _CELLS:
            raise ValueError("benchmark matrix cells must be exactly off/cold/warm")

    def to_dict(self) -> Mapping[str, object]:
        # Planned evidence intentionally contains no timing, memory, hit, or
        # derived speed/parity values.
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "state": self.state,
                "deployment_manifest_sha256": self.deployment_manifest_sha256,
                "source_revision": self.source_revision,
                "checkpoint_sha256": self.checkpoint_sha256,
                "fixture_manifest_sha256": self.fixture_manifest_sha256,
                "canonical_invocation_sha256": self.canonical_invocation_sha256,
                "backend_id": self.backend_id,
                "uncached_deployment_fingerprint": self.uncached_deployment_fingerprint,
                "l2_deployment_fingerprint": self.l2_deployment_fingerprint,
                "condition_codec_version": self.condition_codec_version,
                "condition_schema_version": self.condition_schema_version,
                "base_cache_policy_fingerprint": self.base_cache_policy_fingerprint,
                "condition_cache_policy_fingerprint": self.condition_cache_policy_fingerprint,
                "expected_device_name": self.expected_device_name,
                "repeats": self.repeats,
                "cells": self.cells,
                "measurements": None,
            }
        )

    def assert_cache_bindings(
        self,
        *,
        condition_codec_version: str,
        condition_schema_version: str,
        base_cache_policy_fingerprint: str,
        condition_cache_policy_fingerprint: str,
    ) -> None:
        actual = (
            condition_codec_version,
            condition_schema_version,
            base_cache_policy_fingerprint,
            condition_cache_policy_fingerprint,
        )
        expected = (
            self.condition_codec_version,
            self.condition_schema_version,
            self.base_cache_policy_fingerprint,
            self.condition_cache_policy_fingerprint,
        )
        if actual != expected:
            raise ValueError("benchmark cache policy or feature format binding drifted")


@dataclass(frozen=True)
class ControlFoleyL2BenchmarkObservation:
    output_sha256: str
    cache: CacheReport
    profile: ProfileReport
    encoder_calls: int
    projection_calls: int
    device_name: Optional[str] = None

    def __post_init__(self) -> None:
        _sha256(self.output_sha256, "output_sha256")
        if not isinstance(self.cache, CacheReport) or not isinstance(self.profile, ProfileReport):
            raise TypeError("benchmark observations require Core cache/profile reports")
        for name, value in (
            ("encoder_calls", self.encoder_calls),
            ("projection_calls", self.projection_calls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{0} must be a nonnegative integer".format(name))
        if self.device_name is not None:
            _safe_device_name(self.device_name)


@dataclass(frozen=True)
class ControlFoleyL2BenchmarkSample:
    cell: str
    repeat: int
    wall_time_seconds: float
    output_sha256: str
    cache_hits: int
    cache_misses: int
    cache_bytes: int
    encoder_calls: int
    projection_calls: int
    gpu_time_seconds: Optional[float]
    allocated_bytes: Optional[int]
    reserved_bytes: Optional[int]
    peak_allocated_bytes: Optional[int]
    peak_reserved_bytes: Optional[int]

    def __post_init__(self) -> None:
        if self.cell not in _CELLS:
            raise ValueError("benchmark cell is invalid")
        if isinstance(self.repeat, bool) or not isinstance(self.repeat, int) or self.repeat < 0:
            raise ValueError("benchmark repeat must be a nonnegative integer")
        if (
            isinstance(self.wall_time_seconds, bool)
            or not isinstance(self.wall_time_seconds, (int, float))
            or not math.isfinite(self.wall_time_seconds)
            or self.wall_time_seconds <= 0
        ):
            raise ValueError("benchmark wall time must be finite and positive")
        _sha256(self.output_sha256, "output_sha256")
        for name, value in (
            ("cache_hits", self.cache_hits),
            ("cache_misses", self.cache_misses),
            ("cache_bytes", self.cache_bytes),
            ("encoder_calls", self.encoder_calls),
            ("projection_calls", self.projection_calls),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{0} must be nonnegative".format(name))
        _optional_finite_nonnegative(self.gpu_time_seconds, "gpu_time_seconds")
        _optional_int(self.allocated_bytes, "allocated_bytes")
        _optional_int(self.reserved_bytes, "reserved_bytes")
        _optional_int(self.peak_allocated_bytes, "peak_allocated_bytes")
        _optional_int(self.peak_reserved_bytes, "peak_reserved_bytes")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(dict(self.__dict__))


@dataclass(frozen=True)
class ControlFoleyL2BenchmarkEvidence:
    plan: ControlFoleyL2BenchmarkPlan
    samples: Tuple[ControlFoleyL2BenchmarkSample, ...]
    schema_version: int = 1
    state: str = "measured_raw"

    def __post_init__(self) -> None:
        if self.schema_version != 1 or self.state != "measured_raw":
            raise ValueError("benchmark evidence schema/state is invalid")
        expected = tuple(
            (cell, repeat) for cell in self.plan.cells for repeat in range(self.plan.repeats)
        )
        actual = tuple((sample.cell, sample.repeat) for sample in self.samples)
        if actual != expected:
            raise ValueError("benchmark samples do not exactly fill the sealed matrix")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "state": self.state,
                "plan": dict(self.plan.to_dict()),
                "samples": tuple(dict(sample.to_dict()) for sample in self.samples),
            }
        )


class ControlFoleyL2BenchmarkRunner:
    """Runs the sealed cells in order and records only raw observations."""

    def __init__(self, monotonic: Callable[[], float]) -> None:
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._clock = monotonic

    def run(
        self,
        plan: ControlFoleyL2BenchmarkPlan,
        execute: Callable[[str, int], ControlFoleyL2BenchmarkObservation],
        prepare: Callable[[str, int], None],
    ) -> ControlFoleyL2BenchmarkEvidence:
        if not isinstance(plan, ControlFoleyL2BenchmarkPlan):
            raise TypeError("plan must be ControlFoleyL2BenchmarkPlan")
        if not callable(execute) or not callable(prepare):
            raise TypeError("benchmark callbacks must be callable")
        samples = []
        for cell in plan.cells:
            for repeat in range(plan.repeats):
                prepare(cell, repeat)
                started = self._clock()
                observation = execute(cell, repeat)
                ended = self._clock()
                if not isinstance(observation, ControlFoleyL2BenchmarkObservation):
                    raise TypeError("benchmark callback returned an invalid observation")
                gpu_time = _profile_gpu_seconds(observation.profile)
                allocated = _profile_int(observation.profile, "allocated_bytes")
                reserved = _profile_int(observation.profile, "reserved_bytes")
                peak_allocated = _profile_int(observation.profile, "peak_allocated_bytes")
                peak_reserved = _profile_int(observation.profile, "peak_reserved_bytes")
                if plan.expected_device_name is not None and (
                    observation.device_name != plan.expected_device_name
                    or not _cuda_profile_available(observation.profile)
                    or any(
                        value is None
                        for value in (
                            gpu_time,
                            allocated,
                            reserved,
                            peak_allocated,
                            peak_reserved,
                        )
                    )
                ):
                    raise ValueError("configured GPU benchmark requires complete CUDA observations")
                samples.append(
                    ControlFoleyL2BenchmarkSample(
                        cell=cell,
                        repeat=repeat,
                        wall_time_seconds=ended - started,
                        output_sha256=observation.output_sha256,
                        cache_hits=observation.cache.hits,
                        cache_misses=observation.cache.misses,
                        cache_bytes=observation.cache.bytes_used,
                        encoder_calls=observation.encoder_calls,
                        projection_calls=observation.projection_calls,
                        gpu_time_seconds=gpu_time,
                        allocated_bytes=allocated,
                        reserved_bytes=reserved,
                        peak_allocated_bytes=peak_allocated,
                        peak_reserved_bytes=peak_reserved,
                    )
                )
        return ControlFoleyL2BenchmarkEvidence(plan, tuple(samples))


def write_controlfoley_l2_benchmark_evidence(
    output: Path,
    evidence: ControlFoleyL2BenchmarkEvidence,
    pre_publish_check: Optional[Callable[[], None]] = None,
) -> None:
    """Atomically publish exclusive evidence without leaving a partial final file."""

    if not isinstance(evidence, ControlFoleyL2BenchmarkEvidence):
        raise TypeError("evidence must be ControlFoleyL2BenchmarkEvidence")
    if not isinstance(output, Path) or not output.is_absolute():
        raise ValueError("benchmark evidence output must be an absolute new path")
    if output.exists():
        raise ValueError("benchmark evidence output already exists")
    if pre_publish_check is not None and not callable(pre_publish_check):
        raise TypeError("pre_publish_check must be callable")
    parent = output.parent
    if parent.resolve(strict=True) != parent or not parent.is_dir() or not output.name:
        raise ValueError("benchmark evidence parent must be a canonical directory")
    payload = (
        json.dumps(
            _json_value(evidence.to_dict()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        )
        + "\n"
    ).encode("utf-8")
    if pre_publish_check is not None:
        pre_publish_check()
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, directory_flags)
    temp_name = ".nano-aural-p4d-evidence-{0}".format(secrets.token_hex(12))
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        temp_fd = os.open(temp_name, flags, 0o600, dir_fd=directory_fd)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(temp_fd, view)
                if written <= 0:
                    raise OSError("benchmark evidence write made no progress")
                view = view[written:]
            os.fsync(temp_fd)
        finally:
            os.close(temp_fd)
        try:
            os.link(
                temp_name,
                output.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise ValueError("benchmark evidence output already exists") from error
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temp_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _profile_int(report: ProfileReport, name: str) -> Optional[int]:
    values = [
        value
        for key, value in report.metrics.items()
        if key.startswith("controlfoley.cuda.") and key.endswith("." + name)
    ]
    if not values:
        return None
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or not float(value).is_integer()
        for value in values
    ):
        raise ValueError("profile CUDA memory evidence is invalid")
    return int(max(values))


def _profile_gpu_seconds(report: ProfileReport) -> Optional[float]:
    values = [
        value
        for key, value in report.metrics.items()
        if key.startswith("controlfoley.cuda.") and key.endswith(".milliseconds")
    ]
    if not values:
        return None
    if any(
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        for value in values
    ):
        raise ValueError("profile CUDA timing evidence is invalid")
    return float(sum(values)) / 1000.0


def _cuda_profile_available(report: ProfileReport) -> bool:
    cuda = report.metadata.get("cuda")
    return isinstance(cuda, Mapping) and cuda.get("status") == "available"


def _optional_finite_nonnegative(value: object, name: str) -> None:
    if value is None:
        return
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("{0} must be finite and nonnegative when present".format(name))


def _optional_int(value: object, name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("{0} must be a nonnegative integer when present".format(name))


def _safe_id(value: object, name: str) -> None:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 64
        or not value.isascii()
        or any(not (character.isalnum() or character in "._-") for character in value)
    ):
        raise ValueError("{0} must be a safe short identifier".format(name))


def _safe_device_name(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or len(value) > 128
        or not value.isascii()
        or value != value.strip()
        or any(ord(character) < 32 or character in "/\\" for character in value)
    ):
        raise ValueError("expected_device_name must be safe visible ASCII text")
    if value not in _RTX_4090_DEVICE_NAMES:
        raise ValueError("expected_device_name must identify the declared RTX 4090 Gate host")
    return value


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ValueError("{0} must be a full lowercase SHA-256".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be hexadecimal".format(name)) from error


def _git_revision(value: object) -> None:
    if not isinstance(value, str) or len(value) != 40 or value != value.lower():
        raise ValueError("source_revision must be a full lowercase git revision")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("source_revision must be hexadecimal") from error
