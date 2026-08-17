"""Experimental, adapter-owned ControlFoley L0/L1 in-memory cache contracts."""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

from nano_aural_runtime import CacheReport

from .baseline import fingerprint_file, fingerprint_text, manifest_sha256
from .staged import validate_staged_backend_id
from .tasks import (
    EXPERIMENTAL_STAGED_OPERATION,
    ControlFoleyLocalRequest,
    ControlFoleyTaskKind,
)

EXPERIMENTAL_L0_L1_CACHE_MODE = "experimental_l0_l1"
CONTROLFOLEY_CACHE_LEVELS = ("l0_metadata", "l1_preprocess")
_SAFE_VERSION = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9._-]{0,63})")
_PYTHON_MODULE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*){0,15}")
_CACHE_CONFIGURATION_KEYS = {
    "cache_mode",
    "cache_preprocess_version",
    "cache_code_version",
    "cache_policy_fingerprint",
}


class ControlFoleyCacheInputDrift(ValueError):
    """Materialized input bytes changed between key capture and cache commit."""


@dataclass(frozen=True)
class ControlFoleyGpuCacheConfiguration:
    """Strict operator-only conditional-GPU cache equivalence configuration."""

    deployment: Path
    fixture: Path
    source_dir: Path
    weights_dir: Path
    backend_module: str
    task: ControlFoleyTaskKind
    preprocess_version: str
    code_version: str
    evidence_output: Path
    video: Optional[Path] = None
    audio: Optional[Path] = None
    prompt: Optional[str] = None

    @classmethod
    def from_dict(cls, value: object) -> "ControlFoleyGpuCacheConfiguration":
        if not isinstance(value, Mapping):
            raise ValueError("GPU cache configuration must be an object")
        try:
            task = ControlFoleyTaskKind(value["task"])
        except (KeyError, TypeError, ValueError) as error:
            raise ValueError("GPU cache task is invalid") from error
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
            "preprocess_version",
            "code_version",
            "evidence_output",
            *task_keys,
        }
        if set(value) != expected:
            raise ValueError("GPU cache configuration has missing or unexpected fields")
        backend_module = value["backend_module"]
        if not isinstance(backend_module, str) or _PYTHON_MODULE.fullmatch(backend_module) is None:
            raise ValueError("backend_module must be a safe dotted Python module name")
        preprocess_version = _safe_version(value["preprocess_version"], "preprocess_version")
        code_version = _safe_version(value["code_version"], "code_version")
        prompt = value["prompt"] if "prompt" in task_keys else None
        if prompt is not None and (
            not isinstance(prompt, str)
            or not prompt.strip()
            or any(character in prompt for character in ("\x00", "\r"))
        ):
            raise ValueError("prompt must be non-empty text without control delimiters")
        return cls(
            deployment=_absolute_existing_path(value["deployment"], "deployment", False),
            fixture=_absolute_existing_path(value["fixture"], "fixture", False),
            source_dir=_absolute_existing_path(value["source_dir"], "source_dir", True),
            weights_dir=_absolute_existing_path(value["weights_dir"], "weights_dir", True),
            backend_module=backend_module,
            task=task,
            preprocess_version=preprocess_version,
            code_version=code_version,
            evidence_output=_absolute_output_path(value["evidence_output"]),
            video=(
                _absolute_existing_path(value["video"], "video", False)
                if "video" in task_keys
                else None
            ),
            audio=(
                _absolute_existing_path(value["audio"], "audio", False)
                if "audio" in task_keys
                else None
            ),
            prompt=prompt,
        )


@dataclass(frozen=True)
class ControlFoleyCachePolicy:
    preprocess_version: str
    code_version: str
    max_entries: int = 128
    max_entry_bytes: int = 8 * 1024 * 1024
    max_total_bytes: int = 64 * 1024 * 1024
    ttl_seconds: float = 900.0
    mode: str = EXPERIMENTAL_L0_L1_CACHE_MODE

    def __post_init__(self) -> None:
        if self.mode != EXPERIMENTAL_L0_L1_CACHE_MODE:
            raise ValueError("unsupported ControlFoley cache mode")
        _safe_version(self.preprocess_version, "preprocess_version")
        _safe_version(self.code_version, "code_version")
        for name, value in (
            ("max_entries", self.max_entries),
            ("max_entry_bytes", self.max_entry_bytes),
            ("max_total_bytes", self.max_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("{0} must be a positive integer".format(name))
        if self.max_entry_bytes > self.max_total_bytes:
            raise ValueError("max_entry_bytes cannot exceed max_total_bytes")
        if (
            isinstance(self.ttl_seconds, bool)
            or not isinstance(self.ttl_seconds, (int, float))
            or not math.isfinite(self.ttl_seconds)
            or self.ttl_seconds <= 0
        ):
            raise ValueError("ttl_seconds must be finite and positive")

    @property
    def fingerprint(self) -> str:
        return manifest_sha256(
            {
                "schema_version": 1,
                "mode": self.mode,
                "preprocess_version": self.preprocess_version,
                "code_version": self.code_version,
            }
        )

    def configuration(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "cache_mode": self.mode,
                "cache_preprocess_version": self.preprocess_version,
                "cache_code_version": self.code_version,
                "cache_policy_fingerprint": self.fingerprint,
            }
        )

    def assert_configuration(self, configuration: Mapping[str, Any]) -> None:
        if {key for key in configuration if key.startswith("cache_")} != (
            _CACHE_CONFIGURATION_KEYS
        ) or any(configuration.get(name) != value for name, value in self.configuration().items()):
            raise ValueError("cached deployment does not match the adapter cache policy")


@dataclass(frozen=True)
class ControlFoleyCacheInput:
    role: str
    sha256: str
    size_bytes: int

    def __post_init__(self) -> None:
        if self.role not in ("video", "reference_audio", "prompt"):
            raise ValueError("unsupported cache input role")
        _sha256(self.sha256, "input sha256")
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise TypeError("input size_bytes must be an integer")
        if self.size_bytes < 0:
            raise ValueError("input size_bytes must be nonnegative")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {"role": self.role, "sha256": self.sha256, "size_bytes": self.size_bytes}
        )


@dataclass(frozen=True)
class ControlFoleyCacheKey:
    inputs: Tuple[ControlFoleyCacheInput, ...]
    task: str
    duration_seconds: float
    num_steps: int
    guidance_scale: float
    seed: int
    preprocess_version: str
    code_version: str
    source_revision: str
    deployment_fingerprint: str
    deployment_manifest_sha256: str
    checkpoint_sha256: str
    variant: str
    precision: str
    backend_id: str
    operation: str = EXPERIMENTAL_STAGED_OPERATION
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported cache key schema_version")
        if self.operation != EXPERIMENTAL_STAGED_OPERATION:
            raise ValueError("L0/L1 cache is available only for the experimental staged path")
        if not isinstance(self.inputs, tuple) or not all(
            isinstance(value, ControlFoleyCacheInput) for value in self.inputs
        ):
            raise TypeError("cache inputs must be an immutable tuple of cache inputs")
        roles = tuple(value.role for value in self.inputs)
        if len(set(roles)) != len(roles) or roles != tuple(sorted(roles)):
            raise ValueError("cache input roles must be unique and canonical")
        _safe_version(self.task, "task")
        expected_roles = {
            "V2A": ("video",),
            "TV2A": ("prompt", "video"),
            "TC-V2A": ("prompt", "video"),
            "AC-V2A": ("reference_audio", "video"),
            "T2A": ("prompt",),
        }.get(self.task)
        if expected_roles is None or roles != expected_roles:
            raise ValueError("cache input roles do not match the exact task contract")
        _safe_version(self.preprocess_version, "preprocess_version")
        _safe_version(self.code_version, "code_version")
        _git_revision(self.source_revision)
        _sha256(self.deployment_fingerprint, "deployment_fingerprint")
        _sha256(self.deployment_manifest_sha256, "deployment_manifest_sha256")
        _sha256(self.checkpoint_sha256, "checkpoint_sha256")
        _safe_version(self.variant, "variant")
        _safe_version(self.precision, "precision")
        validate_staged_backend_id(self.backend_id)
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
            or isinstance(self.guidance_scale, bool)
            or not isinstance(self.guidance_scale, (int, float))
            or not math.isfinite(self.guidance_scale)
        ):
            raise ValueError("cache parameters must be finite and valid")
        if (
            isinstance(self.num_steps, bool)
            or not isinstance(self.num_steps, int)
            or self.num_steps <= 0
        ):
            raise ValueError("cache num_steps must be a positive integer")
        if isinstance(self.seed, bool) or not isinstance(self.seed, int):
            raise TypeError("cache seed must be an integer")

    @classmethod
    def from_request(
        cls,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
        policy: ControlFoleyCachePolicy,
    ) -> "ControlFoleyCacheKey":
        if not isinstance(request, ControlFoleyLocalRequest):
            raise TypeError("request must be ControlFoleyLocalRequest")
        policy.assert_configuration(configuration)
        inputs = []
        if request.video_path is not None:
            value = fingerprint_file(request.video_path)
            if value.sha256 is None or value.size_bytes is None:
                raise ValueError("video input fingerprint is unavailable")
            inputs.append(ControlFoleyCacheInput("video", value.sha256, value.size_bytes))
        if request.reference_audio_path is not None:
            value = fingerprint_file(request.reference_audio_path)
            if value.sha256 is None or value.size_bytes is None:
                raise ValueError("reference input fingerprint is unavailable")
            inputs.append(ControlFoleyCacheInput("reference_audio", value.sha256, value.size_bytes))
        if request.prompt is not None:
            value = fingerprint_text(request.prompt)
            if value.sha256 is None or value.size_bytes is None:
                raise ValueError("prompt fingerprint is unavailable")
            inputs.append(ControlFoleyCacheInput("prompt", value.sha256, value.size_bytes))
        return cls(
            inputs=tuple(sorted(inputs, key=lambda item: item.role)),
            task=request.task.value,
            duration_seconds=float(request.duration_seconds),
            num_steps=request.num_steps,
            guidance_scale=float(request.guidance_scale),
            seed=request.seed,
            preprocess_version=policy.preprocess_version,
            code_version=policy.code_version,
            source_revision=_configuration_text(configuration, "source_revision"),
            deployment_fingerprint=deployment_fingerprint,
            deployment_manifest_sha256=_configuration_text(
                configuration, "deployment_manifest_sha256"
            ),
            checkpoint_sha256=_configuration_text(configuration, "checkpoint_sha256"),
            variant=_configuration_text(configuration, "variant"),
            precision=_configuration_text(configuration, "precision"),
            backend_id=_configuration_text(configuration, "staged_backend_id"),
        )

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            _json_value(self.to_dict()),
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")

    def to_dict(self) -> Mapping[str, object]:
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "operation": self.operation,
                "inputs": tuple(value.to_dict() for value in self.inputs),
                "task": self.task,
                "parameters": MappingProxyType(
                    {
                        "duration_seconds": float(self.duration_seconds),
                        "num_steps": self.num_steps,
                        "guidance_scale": float(self.guidance_scale),
                        "seed": self.seed,
                    }
                ),
                "preprocess_version": self.preprocess_version,
                "code_version": self.code_version,
                "source_revision": self.source_revision,
                "deployment_fingerprint": self.deployment_fingerprint,
                "deployment_manifest_sha256": self.deployment_manifest_sha256,
                "checkpoint_sha256": self.checkpoint_sha256,
                "variant": self.variant,
                "precision": self.precision,
                "backend_id": self.backend_id,
            }
        )


@dataclass(frozen=True)
class ControlFoleyCacheEntry:
    tier: str
    key_digest: str
    deployment_fingerprint: str
    payload: bytes
    payload_sha256: str
    expires_at: float

    def __post_init__(self) -> None:
        if self.tier not in CONTROLFOLEY_CACHE_LEVELS:
            raise ValueError("unsupported cache tier")
        _sha256(self.key_digest, "key_digest")
        _sha256(self.deployment_fingerprint, "deployment_fingerprint")
        if not isinstance(self.payload, bytes) or not self.payload:
            raise ValueError("cache payload must be non-empty immutable bytes")
        _sha256(self.payload_sha256, "payload_sha256")
        if (
            isinstance(self.expires_at, bool)
            or not isinstance(self.expires_at, (int, float))
            or not math.isfinite(self.expires_at)
            or self.expires_at < 0
        ):
            raise ValueError("cache expiry must be finite and nonnegative")


@dataclass(frozen=True)
class ControlFoleyCacheSnapshot:
    entries: int
    bytes_used: int
    evictions: int

    def __post_init__(self) -> None:
        for name, value in (
            ("entries", self.entries),
            ("bytes_used", self.bytes_used),
            ("evictions", self.evictions),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{0} must be a nonnegative integer".format(name))


class ControlFoleyCacheStore(Protocol):
    def get_entry(self, tier: str, key_digest: str) -> Optional[ControlFoleyCacheEntry]: ...

    def put_if_absent(
        self, tier: str, key_digest: str, deployment_fingerprint: str, payload: bytes
    ) -> str: ...

    def delete(self, tier: str, key_digest: str) -> None: ...

    def invalidate_deployment(self, deployment_fingerprint: str) -> int: ...

    def invalidate_all(self) -> int: ...

    def snapshot(self) -> ControlFoleyCacheSnapshot: ...

    def close(self) -> None: ...


class ControlFoleyMemoryCacheStore:
    """Thread-safe byte/item-bounded LRU with monotonic TTL and immutable puts."""

    def __init__(
        self,
        policy: ControlFoleyCachePolicy,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(policy, ControlFoleyCachePolicy):
            raise TypeError("policy must be ControlFoleyCachePolicy")
        if not callable(monotonic):
            raise TypeError("monotonic must be callable")
        self._policy = policy
        self._clock = monotonic
        self._entries: OrderedDict[tuple[str, str], ControlFoleyCacheEntry] = OrderedDict()
        self._bytes_used = 0
        self._evictions = 0
        self._closed = False
        self._last_now: Optional[float] = None
        self._lock = threading.RLock()

    def get_entry(self, tier: str, key_digest: str) -> Optional[ControlFoleyCacheEntry]:
        _tier(tier)
        _sha256(key_digest, "key_digest")
        with self._lock:
            self._ensure_open()
            identity = (tier, key_digest)
            entry = self._entries.get(identity)
            if entry is None:
                return None
            if entry.expires_at <= self._now():
                self._remove(identity)
                return None
            self._entries.move_to_end(identity)
            return entry

    def put_if_absent(
        self, tier: str, key_digest: str, deployment_fingerprint: str, payload: bytes
    ) -> str:
        _tier(tier)
        _sha256(key_digest, "key_digest")
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("cache payload must be non-empty immutable bytes")
        if len(payload) > self._policy.max_entry_bytes:
            return "rejected"
        with self._lock:
            self._ensure_open()
            now = self._now()
            identity = (tier, key_digest)
            existing = self._entries.get(identity)
            if existing is not None and existing.expires_at > now:
                self._entries.move_to_end(identity)
                return "present"
            if existing is not None:
                self._remove(identity)
            entry = ControlFoleyCacheEntry(
                tier=tier,
                key_digest=key_digest,
                deployment_fingerprint=deployment_fingerprint,
                payload=payload,
                payload_sha256=hashlib.sha256(payload).hexdigest(),
                expires_at=now + float(self._policy.ttl_seconds),
            )
            self._entries[identity] = entry
            self._bytes_used += len(payload)
            self._evict_to_limits()
            return "stored" if identity in self._entries else "rejected"

    def delete(self, tier: str, key_digest: str) -> None:
        _tier(tier)
        _sha256(key_digest, "key_digest")
        with self._lock:
            self._ensure_open()
            self._remove((tier, key_digest))

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            self._ensure_open()
            identities = [
                identity
                for identity, entry in self._entries.items()
                if entry.deployment_fingerprint == deployment_fingerprint
            ]
            for identity in identities:
                self._remove(identity)
            return len(identities)

    def invalidate_all(self) -> int:
        with self._lock:
            self._ensure_open()
            count = len(self._entries)
            self._entries.clear()
            self._bytes_used = 0
            return count

    def snapshot(self) -> ControlFoleyCacheSnapshot:
        with self._lock:
            self._ensure_open()
            now = self._now()
            for identity, entry in tuple(self._entries.items()):
                if entry.expires_at <= now:
                    self._remove(identity)
            return ControlFoleyCacheSnapshot(
                entries=len(self._entries),
                bytes_used=self._bytes_used,
                evictions=self._evictions,
            )

    def close(self) -> None:
        with self._lock:
            self._entries.clear()
            self._bytes_used = 0
            self._closed = True

    def _remove(self, identity: tuple[str, str]) -> None:
        entry = self._entries.pop(identity, None)
        if entry is not None:
            self._bytes_used -= len(entry.payload)

    def _evict_to_limits(self) -> None:
        while (
            len(self._entries) > self._policy.max_entries
            or self._bytes_used > self._policy.max_total_bytes
        ):
            _, entry = self._entries.popitem(last=False)
            self._bytes_used -= len(entry.payload)
            self._evictions += 1

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("cache store is closed")

    def _now(self) -> float:
        value = self._clock()
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
            or value < 0
            or (self._last_now is not None and value < self._last_now)
        ):
            raise RuntimeError("cache monotonic clock is invalid")
        self._last_now = float(value)
        return self._last_now


class ControlFoleyCacheTransaction:
    """Invocation-local read set and delayed writes; commit only after success."""

    def __init__(
        self,
        cache: "ControlFoleyL0L1Cache",
        key: Optional[ControlFoleyCacheKey],
        generation: int,
        reason: Optional[str] = None,
        *,
        store_disabled: bool = False,
    ) -> None:
        self._cache = cache
        self.key = key
        self.generation = generation
        self._states = {tier: "unavailable" for tier in CONTROLFOLEY_CACHE_LEVELS}
        self._payloads: dict[str, bytes] = {}
        self._pending: dict[str, bytes] = {}
        self._faults = 0 if reason is None else 1
        self._writes = 0
        self._reason = reason
        self._store_disabled = store_disabled
        self._complete = False
        if key is not None:
            self._read("l0_metadata", expected_payload=key.canonical_bytes)
            self._read("l1_preprocess")
            if self._states["l0_metadata"] != "hit":
                self._pending["l0_metadata"] = key.canonical_bytes

    @property
    def cached_preprocess(self) -> Optional[bytes]:
        return self._payloads.get("l1_preprocess")

    def reject_cached_preprocess(self) -> None:
        if self.key is None:
            return
        self._states["l1_preprocess"] = "corrupt"
        self._payloads.pop("l1_preprocess", None)
        self._faults += 1
        self._cache._safe_delete("l1_preprocess", self.key.digest, self.key.deployment_fingerprint)

    def stage_preprocess(self, payload: bytes) -> None:
        if self.key is None or self._complete:
            return
        if not isinstance(payload, bytes) or not payload:
            self.record_fault("codec_fault")
            return
        self._pending["l1_preprocess"] = payload

    def record_fault(self, reason: str) -> None:
        if reason not in (
            "codec_fault",
            "store_fault",
            "key_unavailable",
            "limit_rejected",
        ):
            reason = "store_fault"
        self._faults += 1
        self._reason = reason

    def commit(self, current_key: Optional[ControlFoleyCacheKey]) -> CacheReport:
        if self._complete:
            return self.report()
        if self.key is None:
            self._complete = True
            return self.report()
        if current_key != self.key:
            self.abort()
            raise ControlFoleyCacheInputDrift("ControlFoley cache inputs changed before commit")
        for tier, payload in self._pending.items():
            outcome = self._cache._safe_put(tier, self.key, payload)
            if outcome == "stored":
                self._writes += 1
            elif outcome == "fault":
                self.record_fault("store_fault")
            elif outcome == "rejected":
                self.record_fault("limit_rejected")
        self._pending.clear()
        self._complete = True
        return self.report()

    def abort(self) -> None:
        self._pending.clear()
        self._complete = True

    def invalidated(self) -> CacheReport:
        self._pending.clear()
        self._reason = "invalidated"
        self._complete = True
        return self.report()

    def report(self) -> CacheReport:
        snapshot = None if self._store_disabled else self._cache._safe_snapshot()
        if snapshot is None:
            if self._faults == 0:
                self.record_fault("store_fault")
            snapshot = ControlFoleyCacheSnapshot(0, 0, 0)
        hits = sum(state == "hit" for state in self._states.values())
        misses = sum(state in ("miss", "corrupt") for state in self._states.values())
        return CacheReport(
            hits=hits,
            misses=misses,
            bytes_used=snapshot.bytes_used,
            metadata={
                "schema_version": 1,
                "namespace": "controlfoley",
                "status": "ok" if self._faults == 0 else "degraded",
                "mode": EXPERIMENTAL_L0_L1_CACHE_MODE,
                "levels": dict(self._states),
                "writes": self._writes,
                "faults": self._faults,
                "resident_entries": snapshot.entries,
                "evictions": snapshot.evictions,
                "reason": self._reason,
            },
        )

    def _read(self, tier: str, expected_payload: Optional[bytes] = None) -> None:
        if self.key is None:
            return
        try:
            entry = self._cache._get_entry(tier, self.key)
        except Exception:
            self._states[tier] = "miss"
            self.record_fault("store_fault")
            return
        if entry is None:
            self._states[tier] = "miss"
            return
        try:
            valid = (
                isinstance(entry, ControlFoleyCacheEntry)
                and entry.tier == tier
                and entry.key_digest == self.key.digest
                and entry.deployment_fingerprint == self.key.deployment_fingerprint
                and hashlib.sha256(entry.payload).hexdigest() == entry.payload_sha256
                and (expected_payload is None or entry.payload == expected_payload)
            )
        except Exception:
            valid = False
        if not valid:
            self._states[tier] = "corrupt"
            self._faults += 1
            self._cache._safe_delete(tier, self.key.digest, self.key.deployment_fingerprint)
            return
        self._states[tier] = "hit"
        self._payloads[tier] = entry.payload


class ControlFoleyL0L1Cache:
    """Failure-isolating adapter facade over a bounded cache store."""

    def __init__(
        self,
        policy: ControlFoleyCachePolicy,
        store: Optional[ControlFoleyCacheStore] = None,
    ) -> None:
        if not isinstance(policy, ControlFoleyCachePolicy):
            raise TypeError("policy must be ControlFoleyCachePolicy")
        self.policy = policy
        self._store: ControlFoleyCacheStore = store or ControlFoleyMemoryCacheStore(policy)
        self._generation = 0
        self._globally_disabled = False
        self._disabled_deployments: set[str] = set()
        self._lock = threading.RLock()

    def begin(
        self,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
    ) -> ControlFoleyCacheTransaction:
        with self._lock:
            if self._is_disabled(deployment_fingerprint):
                return ControlFoleyCacheTransaction(
                    self,
                    None,
                    self._generation,
                    "store_fault",
                    store_disabled=True,
                )
        try:
            key = self.key_for(request, configuration, deployment_fingerprint)
            with self._lock:
                if self._is_disabled(deployment_fingerprint):
                    return ControlFoleyCacheTransaction(
                        self,
                        None,
                        self._generation,
                        "store_fault",
                        store_disabled=True,
                    )
                return ControlFoleyCacheTransaction(self, key, self._generation)
        except Exception:
            with self._lock:
                return ControlFoleyCacheTransaction(self, None, self._generation, "key_unavailable")

    def key_for(
        self,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
    ) -> ControlFoleyCacheKey:
        return ControlFoleyCacheKey.from_request(
            request, configuration, deployment_fingerprint, self.policy
        )

    def commit(
        self,
        transaction: ControlFoleyCacheTransaction,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
    ) -> CacheReport:
        if transaction.key is None:
            return transaction.commit(None)
        try:
            current_key = self.key_for(request, configuration, deployment_fingerprint)
        except Exception as error:
            transaction.abort()
            raise ControlFoleyCacheInputDrift(
                "ControlFoley cache inputs could not be revalidated"
            ) from error
        with self._lock:
            if transaction.generation != self._generation:
                return transaction.invalidated()
            return transaction.commit(current_key)

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            self._generation += 1
            if self._globally_disabled:
                return 0
            try:
                return self._store.invalidate_deployment(deployment_fingerprint)
            except Exception:
                self._disabled_deployments.add(deployment_fingerprint)
                return 0

    def invalidate_all(self) -> int:
        with self._lock:
            self._generation += 1
            if self._globally_disabled:
                return 0
            try:
                return self._store.invalidate_all()
            except Exception:
                self._globally_disabled = True
                return 0

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            self._globally_disabled = True
            try:
                self._store.close()
            except Exception:
                pass

    def _is_disabled(self, deployment_fingerprint: str) -> bool:
        with self._lock:
            return self._globally_disabled or deployment_fingerprint in self._disabled_deployments

    def _get_entry(self, tier: str, key: ControlFoleyCacheKey) -> Optional[ControlFoleyCacheEntry]:
        with self._lock:
            if self._is_disabled(key.deployment_fingerprint):
                raise RuntimeError("cache store is unavailable")
            return self._store.get_entry(tier, key.digest)

    def _safe_put(self, tier: str, key: ControlFoleyCacheKey, payload: bytes) -> str:
        with self._lock:
            if self._is_disabled(key.deployment_fingerprint):
                return "fault"
            try:
                outcome = self._store.put_if_absent(
                    tier, key.digest, key.deployment_fingerprint, payload
                )
                return outcome if outcome in ("stored", "present", "rejected") else "fault"
            except Exception:
                return "fault"

    def _safe_delete(self, tier: str, key_digest: str, deployment_fingerprint: str) -> None:
        with self._lock:
            if self._is_disabled(deployment_fingerprint):
                return
            try:
                self._store.delete(tier, key_digest)
            except Exception:
                pass

    def _safe_snapshot(self) -> Optional[ControlFoleyCacheSnapshot]:
        with self._lock:
            if self._globally_disabled:
                return None
            try:
                snapshot = self._store.snapshot()
                return snapshot if isinstance(snapshot, ControlFoleyCacheSnapshot) else None
            except Exception:
                return None


def cached_configuration_keys() -> frozenset[str]:
    return frozenset(_CACHE_CONFIGURATION_KEYS)


def _configuration_text(configuration: Mapping[str, Any], name: str) -> str:
    value = configuration.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError("cache deployment field {0} is invalid".format(name))
    return value


def _safe_version(value: object, name: str) -> str:
    if not isinstance(value, str) or _SAFE_VERSION.fullmatch(value) is None:
        raise ValueError("{0} must be a safe short version identifier".format(name))
    return value


def _sha256(value: object, name: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ValueError("{0} must be a lowercase full SHA-256".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be hexadecimal".format(name)) from error


def _git_revision(value: object) -> None:
    if not isinstance(value, str) or len(value) != 40 or value != value.lower():
        raise ValueError("source_revision must be a lowercase full git revision")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("source_revision must be hexadecimal") from error


def _tier(value: str) -> None:
    if value not in CONTROLFOLEY_CACHE_LEVELS:
        raise ValueError("unsupported cache tier")


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
        raise ValueError("evidence_output must be an absolute new path")
    path = Path(value)
    if not path.is_absolute() or path.exists() or not path.parent.is_dir():
        raise ValueError("evidence_output must be an absolute new path in an existing directory")
    return path.parent.resolve(strict=True) / path.name


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value
