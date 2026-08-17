"""Experimental ControlFoley L2 condition-feature cache and safe disk store."""

from __future__ import annotations

import hashlib
import json
import math
import os
import secrets
import stat
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple

from nano_aural_runtime import CacheReport

from .baseline import manifest_sha256
from .cache import ControlFoleyCacheInputDrift, ControlFoleyCacheKey, ControlFoleyCachePolicy
from .safe_tensor import validate_safe_tensor_bundle
from .staged import controlfoley_condition_feature_roles
from .tasks import ControlFoleyLocalRequest

EXPERIMENTAL_L2_CACHE_MODE = "experimental_l2_condition_features"
L2_CACHE_LEVEL = "l2_condition"
_L2_CONFIGURATION_KEYS = frozenset(
    {
        "l2_cache_mode",
        "l2_cache_codec_version",
        "l2_cache_schema_version",
        "l2_cache_policy_fingerprint",
    }
)
_SAFE_ID = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-")
_DATA_SUFFIX = ".safetensors"
_META_SUFFIX = ".json"
_GLOBAL_FUSE = ".nano-aural-l2-global-fuse-v1"
_DEPLOYMENT_FUSE_PREFIX = ".nano-aural-l2-deployment-fuse-v1-"
_FUSE_PAYLOAD = b"nano-aural-controlfoley-l2-fail-closed-v1\n"


@dataclass(frozen=True)
class ControlFoleyConditionCachePolicy:
    root: Path
    codec_version: str
    schema_version: str
    max_entries: int = 128
    max_entry_bytes: int = 64 * 1024 * 1024
    max_total_bytes: int = 2 * 1024 * 1024 * 1024
    ttl_seconds: float = 3600.0
    cleanup_grace_seconds: float = 60.0
    mode: str = EXPERIMENTAL_L2_CACHE_MODE

    def __post_init__(self) -> None:
        if self.mode != EXPERIMENTAL_L2_CACHE_MODE:
            raise ValueError("unsupported condition cache mode")
        _safe_id(self.codec_version, "codec_version")
        _safe_id(self.schema_version, "schema_version")
        if not isinstance(self.root, Path) or not self.root.is_absolute():
            raise ValueError("condition cache root must be an absolute operator path")
        try:
            if self.root.resolve(strict=True) != self.root:
                raise ValueError("condition cache root must not traverse symlink components")
            root_stat = self.root.lstat()
        except OSError as error:
            raise ValueError("condition cache root is unavailable") from error
        if not stat.S_ISDIR(root_stat.st_mode) or stat.S_ISLNK(root_stat.st_mode):
            raise ValueError("condition cache root must be a real directory")
        if root_stat.st_mode & 0o077:
            raise ValueError("condition cache root must not be group/world accessible")
        for name, value in (
            ("max_entries", self.max_entries),
            ("max_entry_bytes", self.max_entry_bytes),
            ("max_total_bytes", self.max_total_bytes),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise ValueError("{0} must be a positive integer".format(name))
        if self.max_entry_bytes > self.max_total_bytes:
            raise ValueError("max_entry_bytes cannot exceed max_total_bytes")
        for name, value in (
            ("ttl_seconds", self.ttl_seconds),
            ("cleanup_grace_seconds", self.cleanup_grace_seconds),
        ):
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise ValueError("{0} must be finite and positive".format(name))

    @property
    def fingerprint(self) -> str:
        return manifest_sha256(
            {
                "schema_version": 1,
                "mode": self.mode,
                "codec_version": self.codec_version,
                "condition_schema_version": self.schema_version,
            }
        )

    def configuration(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "l2_cache_mode": self.mode,
                "l2_cache_codec_version": self.codec_version,
                "l2_cache_schema_version": self.schema_version,
                "l2_cache_policy_fingerprint": self.fingerprint,
            }
        )

    def assert_configuration(self, configuration: Mapping[str, Any]) -> None:
        if {key for key in configuration if key.startswith("l2_cache_")} != set(
            _L2_CONFIGURATION_KEYS
        ) or any(configuration.get(key) != value for key, value in self.configuration().items()):
            raise ValueError("L2 deployment does not match the adapter condition cache policy")

    def assert_operator_separation(self, source_dir: Path, weights_dir: Path) -> None:
        for name, protected in (("source_dir", source_dir), ("weights_dir", weights_dir)):
            if not isinstance(protected, Path) or not protected.is_absolute():
                raise ValueError("{0} must be an absolute operator directory".format(name))
            try:
                canonical = protected.resolve(strict=True)
            except OSError as error:
                raise ValueError("{0} is unavailable".format(name)) from error
            if canonical != protected or not canonical.is_dir():
                raise ValueError("{0} must be a canonical real directory".format(name))
            if _paths_overlap(self.root, canonical):
                raise ValueError("condition cache root must be disjoint from source and weights")


@dataclass(frozen=True)
class ControlFoleyConditionCacheKey:
    parent: ControlFoleyCacheKey
    role: str
    codec_version: str
    condition_schema_version: str
    schema_version: int = 1

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported L2 cache key schema_version")
        if not isinstance(self.parent, ControlFoleyCacheKey):
            raise TypeError("L2 parent must be a ControlFoley cache key")
        _safe_id(self.codec_version, "codec_version")
        _safe_id(self.condition_schema_version, "condition_schema_version")
        expected = {
            "V2A": ("video",),
            "TV2A": ("video", "text"),
            "TC-V2A": ("video", "text"),
            "AC-V2A": ("video", "reference_audio"),
            "T2A": ("text",),
        }[self.parent.task]
        if self.role not in expected:
            raise ValueError("L2 feature role does not match the task")

    @property
    def digest(self) -> str:
        return hashlib.sha256(self.canonical_bytes).hexdigest()

    @property
    def canonical_bytes(self) -> bytes:
        return json.dumps(
            {
                "schema_version": self.schema_version,
                "parent": _json_value(self.parent.to_dict()),
                "feature_stage": "condition_encode_projection",
                "feature_role": self.role,
                "codec_version": self.codec_version,
                "condition_schema_version": self.condition_schema_version,
            },
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=True,
        ).encode("utf-8")


@dataclass(frozen=True)
class ControlFoleyConditionCacheEntry:
    key_digest: str
    deployment_fingerprint: str
    payload: bytes
    payload_sha256: str
    created_at: float
    expires_at: float

    def __post_init__(self) -> None:
        _sha256(self.key_digest, "key_digest")
        _sha256(self.deployment_fingerprint, "deployment_fingerprint")
        if not isinstance(self.payload, bytes) or not self.payload:
            raise ValueError("L2 payload must be non-empty immutable bytes")
        _sha256(self.payload_sha256, "payload_sha256")
        if (
            any(
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value < 0
                for value in (self.created_at, self.expires_at)
            )
            or self.expires_at <= self.created_at
        ):
            raise ValueError("L2 entry timestamps are invalid")


@dataclass(frozen=True)
class ControlFoleyConditionCacheSnapshot:
    entries: int
    bytes_used: int
    evictions: int
    corruptions: int

    def __post_init__(self) -> None:
        for name, value in (
            ("entries", self.entries),
            ("bytes_used", self.bytes_used),
            ("evictions", self.evictions),
            ("corruptions", self.corruptions),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("{0} must be a nonnegative integer".format(name))


class ControlFoleyConditionCacheStore(Protocol):
    def get_entry(
        self, key: ControlFoleyConditionCacheKey
    ) -> Optional[ControlFoleyConditionCacheEntry]: ...

    def put_if_absent(self, key: ControlFoleyConditionCacheKey, payload: bytes) -> str: ...

    def delete(self, key: ControlFoleyConditionCacheKey) -> None: ...

    def invalidate_deployment(self, deployment_fingerprint: str) -> int: ...

    def invalidate_all(self) -> int: ...

    def cleanup(self) -> int: ...

    def snapshot(self) -> ControlFoleyConditionCacheSnapshot: ...

    def close(self) -> None: ...


class _PersistentFuseAuthority:
    """Root-confined durable authority preventing stale-entry resurrection."""

    def __init__(self, root: Path) -> None:
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        self._fd = os.open(root, flags)
        info = os.fstat(self._fd)
        if not stat.S_ISDIR(info.st_mode) or info.st_mode & 0o077:
            os.close(self._fd)
            raise ValueError("condition cache fuse root is not a private directory")
        self._closed = False
        self._lock = threading.RLock()

    def is_fused(self, deployment_fingerprint: str) -> bool:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            self._ensure_open()
            return self._exists(_GLOBAL_FUSE) or self._exists(
                self._deployment_name(deployment_fingerprint)
            )

    def global_fused(self) -> bool:
        with self._lock:
            self._ensure_open()
            return self._exists(_GLOBAL_FUSE)

    def deployment_fused(self, deployment_fingerprint: str) -> bool:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            self._ensure_open()
            return self._exists(self._deployment_name(deployment_fingerprint))

    def fuse_deployment(self, deployment_fingerprint: str) -> None:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        self._write_once(self._deployment_name(deployment_fingerprint))

    def fuse_all(self) -> None:
        self._write_once(_GLOBAL_FUSE)

    def clear_deployment(self, deployment_fingerprint: str) -> None:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            self._ensure_open()
            self._unlink(self._deployment_name(deployment_fingerprint))
            os.fsync(self._fd)

    def clear_all(self) -> None:
        with self._lock:
            self._ensure_open()
            for name in tuple(os.listdir(self._fd)):
                if name == _GLOBAL_FUSE or name.startswith(_DEPLOYMENT_FUSE_PREFIX):
                    self._unlink(name)
            os.fsync(self._fd)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            os.close(self._fd)
            self._closed = True

    def _write_once(self, name: str) -> None:
        with self._lock:
            self._ensure_open()
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            try:
                fd = os.open(name, flags, 0o600, dir_fd=self._fd)
            except FileExistsError:
                return
            try:
                view = memoryview(_FUSE_PAYLOAD)
                while view:
                    written = os.write(fd, view)
                    if written <= 0:
                        raise OSError("condition cache fuse write made no progress")
                    view = view[written:]
                os.fsync(fd)
            finally:
                os.close(fd)
            os.fsync(self._fd)

    def _exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _unlink(self, name: str) -> None:
        try:
            os.unlink(name, dir_fd=self._fd)
        except FileNotFoundError:
            pass

    @staticmethod
    def _deployment_name(deployment_fingerprint: str) -> str:
        return _DEPLOYMENT_FUSE_PREFIX + deployment_fingerprint

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("condition cache fuse authority is closed")


class ControlFoleyLocalConditionCacheStore:
    """Immutable, dirfd-confined safetensors persistence with bounded reads."""

    def __init__(
        self,
        policy: ControlFoleyConditionCachePolicy,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        if not isinstance(policy, ControlFoleyConditionCachePolicy):
            raise TypeError("policy must be ControlFoleyConditionCachePolicy")
        if not callable(wall_clock):
            raise TypeError("wall_clock must be callable")
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        try:
            self._root_fd = os.open(policy.root, flags)
        except OSError as error:
            raise ValueError("condition cache root could not be opened safely") from error
        opened_root = os.fstat(self._root_fd)
        if not stat.S_ISDIR(opened_root.st_mode) or opened_root.st_mode & 0o077:
            os.close(self._root_fd)
            raise ValueError("opened condition cache root is not a private directory")
        self.policy = policy
        self._clock = wall_clock
        self._closed = False
        self._evictions = 0
        self._corruptions = 0
        self._access: dict[str, int] = {}
        self._lock = threading.RLock()
        self._fuses = _PersistentFuseAuthority(policy.root)
        self.cleanup()

    def get_entry(
        self, key: ControlFoleyConditionCacheKey
    ) -> Optional[ControlFoleyConditionCacheEntry]:
        with self._lock:
            self._ensure_open()
            if self._fuses.is_fused(key.parent.deployment_fingerprint):
                return None
            try:
                entry = self._read_entry(key)
            except (OSError, ValueError):
                self._quarantine(key.digest)
                self._corruptions += 1
                return None
            if entry is None:
                return None
            now = self._now()
            if entry.created_at > now or entry.expires_at <= now:
                self._delete_digest(key.digest)
                return None
            self._access[key.digest] = time.monotonic_ns()
            return entry

    def put_if_absent(self, key: ControlFoleyConditionCacheKey, payload: bytes) -> str:
        if not isinstance(payload, bytes) or not payload:
            raise ValueError("L2 payload must be non-empty immutable bytes")
        if len(payload) > self.policy.max_entry_bytes:
            return "rejected"
        validate_safe_tensor_bundle(
            payload,
            codec_version=self.policy.codec_version,
            schema_version=self.policy.schema_version,
            max_size_bytes=self.policy.max_entry_bytes,
        )
        with self._lock:
            self._ensure_open()
            if self._fuses.is_fused(key.parent.deployment_fingerprint):
                return "fault"
            existing = self.get_entry(key)
            payload_sha = hashlib.sha256(payload).hexdigest()
            if existing is not None:
                return "present" if existing.payload_sha256 == payload_sha else "conflict"
            now = self._now()
            metadata = {
                "schema_version": 1,
                "key_digest": key.digest,
                "deployment_fingerprint": key.parent.deployment_fingerprint,
                "payload_sha256": payload_sha,
                "size_bytes": len(payload),
                "created_at": now,
                "expires_at": now + float(self.policy.ttl_seconds),
                "codec_version": self.policy.codec_version,
                "condition_schema_version": self.policy.schema_version,
            }
            data_name = self._data_name(key.digest)
            meta_name = self._meta_name(key.digest)
            data_stored = self._publish(data_name, payload)
            if not data_stored:
                deadline = time.monotonic() + 0.5
                while True:
                    concurrent = self._read_entry(key)
                    if concurrent is not None:
                        return "present" if concurrent.payload_sha256 == payload_sha else "conflict"
                    if time.monotonic() >= deadline:
                        return "fault"
                    time.sleep(0.005)
            try:
                meta_stored = self._publish(
                    meta_name,
                    json.dumps(
                        metadata, sort_keys=True, separators=(",", ":"), ensure_ascii=True
                    ).encode("utf-8"),
                )
            except Exception:
                self._unlink(data_name)
                raise
            if not meta_stored:
                self._unlink(data_name)
                return "fault"
            self._access[key.digest] = time.monotonic_ns()
            self._enforce_limits()
            self._fsync_root()
            return "stored" if self._exists(data_name) and self._exists(meta_name) else "rejected"

    def delete(self, key: ControlFoleyConditionCacheKey) -> None:
        with self._lock:
            self._ensure_open()
            self._delete_digest(key.digest)
            self._fsync_root()

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            self._ensure_open()
            count = 0
            for digest, metadata in self._metadata_records():
                if metadata.get("deployment_fingerprint") == deployment_fingerprint:
                    self._delete_digest(digest)
                    count += 1
            self._fsync_root()
            self._fuses.clear_deployment(deployment_fingerprint)
            return count

    def invalidate_all(self) -> int:
        with self._lock:
            self._ensure_open()
            digests = {digest for digest, _ in self._metadata_records()}
            for digest in digests:
                self._delete_digest(digest)
            self._fsync_root()
            self._fuses.clear_all()
            return len(digests)

    def cleanup(self) -> int:
        with self._lock:
            self._ensure_open()
            removed = 0
            now = self._now()
            global_fused = self._fuses.global_fused()
            names = self._names()
            for name in names:
                if name.startswith(".tmp-") and self._older_than_grace(name, now):
                    self._unlink(name)
                    removed += 1
            data_digests = {
                name[: -len(_DATA_SUFFIX)]
                for name in names
                if name.endswith(_DATA_SUFFIX) and _is_sha256(name[: -len(_DATA_SUFFIX)])
            }
            meta_digests = {
                name[: -len(_META_SUFFIX)]
                for name in names
                if name.endswith(_META_SUFFIX) and _is_sha256(name[: -len(_META_SUFFIX)])
            }
            for digest in data_digests ^ meta_digests:
                name = (
                    self._data_name(digest) if digest in data_digests else self._meta_name(digest)
                )
                if self._older_than_grace(name, now):
                    self._delete_digest(digest)
                    removed += 1
            for digest in data_digests & meta_digests:
                try:
                    metadata = self._read_metadata(digest)
                    deployment = metadata.get("deployment_fingerprint")
                    if not isinstance(deployment, str):
                        raise ValueError("L2 metadata deployment fingerprint is invalid")
                    if global_fused or self._fuses.deployment_fused(deployment):
                        self._delete_digest(digest)
                        removed += 1
                        continue
                    size = _positive_int(metadata.get("size_bytes"), "size_bytes")
                    if size > self.policy.max_entry_bytes:
                        self._delete_digest(digest)
                        removed += 1
                        continue
                    created_at = _finite_number(metadata.get("created_at"), "created_at")
                    expires_at = _finite_number(metadata.get("expires_at"), "expires_at")
                    if (
                        created_at > now
                        or expires_at <= created_at
                        or expires_at > created_at + float(self.policy.ttl_seconds)
                        or expires_at <= now
                    ):
                        self._delete_digest(digest)
                        removed += 1
                except (OSError, ValueError):
                    self._quarantine(digest)
                    self._corruptions += 1
                    removed += 1
            self._enforce_limits()
            self._fsync_root()
            return removed

    def snapshot(self) -> ControlFoleyConditionCacheSnapshot:
        with self._lock:
            self._ensure_open()
            self.cleanup()
            entries = 0
            bytes_used = 0
            for digest, metadata in self._metadata_records():
                try:
                    deployment = metadata.get("deployment_fingerprint")
                    if not isinstance(deployment, str) or self._fuses.is_fused(deployment):
                        continue
                    size = _positive_int(metadata.get("size_bytes"), "size_bytes")
                    if self._exists(self._data_name(digest)):
                        entries += 1
                        bytes_used += size
                except ValueError:
                    continue
            return ControlFoleyConditionCacheSnapshot(
                entries, bytes_used, self._evictions, self._corruptions
            )

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self.cleanup()
            self._fuses.close()
            os.close(self._root_fd)
            self._closed = True

    def _read_entry(
        self, key: ControlFoleyConditionCacheKey
    ) -> Optional[ControlFoleyConditionCacheEntry]:
        data_name = self._data_name(key.digest)
        meta_name = self._meta_name(key.digest)
        data_exists = self._exists(data_name)
        meta_exists = self._exists(meta_name)
        if not data_exists and not meta_exists:
            return None
        if not data_exists or not meta_exists:
            present_name = data_name if data_exists else meta_name
            if self._older_than_grace(present_name, self._now()):
                self._delete_digest(key.digest)
            return None
        metadata = self._read_metadata(key.digest)
        expected = {
            "schema_version": 1,
            "key_digest": key.digest,
            "deployment_fingerprint": key.parent.deployment_fingerprint,
            "codec_version": self.policy.codec_version,
            "condition_schema_version": self.policy.schema_version,
        }
        if any(metadata.get(name) != value for name, value in expected.items()):
            raise ValueError("L2 metadata does not bind the cache key")
        size = _positive_int(metadata.get("size_bytes"), "size_bytes")
        if size > self.policy.max_entry_bytes:
            raise ValueError("L2 metadata size exceeds the sealed limit")
        payload = self._read_file(self._data_name(key.digest), self.policy.max_entry_bytes)
        if len(payload) != size or hashlib.sha256(payload).hexdigest() != metadata.get(
            "payload_sha256"
        ):
            raise ValueError("L2 payload integrity validation failed")
        validate_safe_tensor_bundle(
            payload,
            codec_version=self.policy.codec_version,
            schema_version=self.policy.schema_version,
            max_size_bytes=self.policy.max_entry_bytes,
        )
        created_at = _finite_number(metadata.get("created_at"), "created_at")
        expires_at = _finite_number(metadata.get("expires_at"), "expires_at")
        if expires_at > created_at + float(self.policy.ttl_seconds):
            raise ValueError("L2 entry expiry exceeds the current operator TTL cap")
        return ControlFoleyConditionCacheEntry(
            key.digest,
            key.parent.deployment_fingerprint,
            payload,
            str(metadata["payload_sha256"]),
            created_at,
            expires_at,
        )

    def _read_metadata(self, digest: str) -> Mapping[str, object]:
        payload = self._read_file(self._meta_name(digest), 4096)
        try:
            value = json.loads(payload.decode("utf-8"), object_pairs_hook=_unique_object)
        except (UnicodeDecodeError, ValueError) as error:
            raise ValueError("L2 metadata JSON is invalid") from error
        expected = {
            "schema_version",
            "key_digest",
            "deployment_fingerprint",
            "payload_sha256",
            "size_bytes",
            "created_at",
            "expires_at",
            "codec_version",
            "condition_schema_version",
        }
        if not isinstance(value, dict) or set(value) != expected:
            raise ValueError("L2 metadata has missing or unexpected fields")
        _sha256(value["key_digest"], "key_digest")
        _sha256(value["deployment_fingerprint"], "deployment_fingerprint")
        _sha256(value["payload_sha256"], "payload_sha256")
        _positive_int(value["size_bytes"], "size_bytes")
        _finite_number(value["created_at"], "created_at")
        _finite_number(value["expires_at"], "expires_at")
        return value

    def _read_file(self, name: str, limit: int) -> bytes:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=self._root_fd)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_mode & 0o077
                or info.st_size <= 0
                or info.st_size > limit
            ):
                raise ValueError("L2 cache file shape is unsafe")
            chunks = []
            remaining = info.st_size
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    raise ValueError("L2 cache file is truncated")
                chunks.append(chunk)
                remaining -= len(chunk)
            if os.read(fd, 1):
                raise ValueError("L2 cache file grew during the bounded read")
            return b"".join(chunks)
        finally:
            os.close(fd)

    def _publish(self, final_name: str, payload: bytes) -> bool:
        temp_name = ".tmp-{0}-{1}".format(os.getpid(), secrets.token_hex(12))
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(temp_name, flags, 0o600, dir_fd=self._root_fd)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("L2 cache write made no progress")
                view = view[written:]
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            os.link(
                temp_name,
                final_name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
                follow_symlinks=False,
            )
            os.fsync(self._root_fd)
            return True
        except FileExistsError:
            return False
        finally:
            self._unlink(temp_name)

    def _enforce_limits(self) -> None:
        records = []
        for digest, metadata in self._metadata_records():
            try:
                size = _positive_int(metadata.get("size_bytes"), "size_bytes")
                created = _finite_number(metadata.get("created_at"), "created_at")
            except ValueError:
                self._quarantine(digest)
                self._corruptions += 1
                continue
            access = self._access.get(digest, int(created * 1_000_000_000))
            records.append((access, digest, size))
        total = sum(item[2] for item in records)
        records.sort()
        while len(records) > self.policy.max_entries or total > self.policy.max_total_bytes:
            _, digest, size = records.pop(0)
            self._delete_digest(digest)
            total -= size
            self._evictions += 1

    def _metadata_records(self) -> Tuple[Tuple[str, Mapping[str, object]], ...]:
        records = []
        for name in self._names():
            if not name.endswith(_META_SUFFIX):
                continue
            digest = name[: -len(_META_SUFFIX)]
            if not _is_sha256(digest):
                continue
            try:
                records.append((digest, self._read_metadata(digest)))
            except (OSError, ValueError):
                self._quarantine(digest)
                self._corruptions += 1
        return tuple(records)

    def _names(self) -> Tuple[str, ...]:
        return tuple(os.listdir(self._root_fd))

    def _older_than_grace(self, name: str, now: float) -> bool:
        try:
            info = os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
        except FileNotFoundError:
            return False
        return now - info.st_mtime >= float(self.policy.cleanup_grace_seconds)

    def _exists(self, name: str) -> bool:
        try:
            os.stat(name, dir_fd=self._root_fd, follow_symlinks=False)
            return True
        except FileNotFoundError:
            return False

    def _quarantine(self, digest: str) -> None:
        self._delete_digest(digest)

    def _delete_digest(self, digest: str) -> None:
        self._unlink(self._data_name(digest))
        self._unlink(self._meta_name(digest))
        self._access.pop(digest, None)

    def _unlink(self, name: str) -> None:
        try:
            os.unlink(name, dir_fd=self._root_fd)
        except FileNotFoundError:
            pass

    @staticmethod
    def _data_name(digest: str) -> str:
        _sha256(digest, "key_digest")
        return digest + _DATA_SUFFIX

    @staticmethod
    def _meta_name(digest: str) -> str:
        _sha256(digest, "key_digest")
        return digest + _META_SUFFIX

    def _ensure_open(self) -> None:
        if self._closed:
            raise RuntimeError("condition cache store is closed")

    def _fsync_root(self) -> None:
        os.fsync(self._root_fd)

    def _now(self) -> float:
        value = self._clock()
        return _finite_number(value, "wall clock")


class ControlFoleyConditionCacheTransaction:
    def __init__(
        self,
        cache: "ControlFoleyConditionCache",
        parent: Optional[ControlFoleyCacheKey],
        roles: Tuple[str, ...],
        generation: int,
        reason: Optional[str] = None,
        *,
        store_disabled: bool = False,
    ) -> None:
        self._cache = cache
        self.parent = parent
        self.roles = roles
        self.generation = generation
        self._states = {role: "unavailable" for role in roles}
        self._payloads: dict[str, bytes] = {}
        self._pending: dict[str, bytes] = {}
        self._faults = 0 if reason is None else 1
        self._reason = reason
        self._writes = 0
        self._newly_stored: list[ControlFoleyConditionCacheKey] = []
        self._complete = False
        self._store_disabled = store_disabled
        if parent is not None:
            for role in roles:
                self._read(role)

    @property
    def cached_payloads(self) -> Mapping[str, bytes]:
        return MappingProxyType(dict(self._payloads))

    def reject(self, role: str) -> None:
        if role not in self._states or self.parent is None:
            return
        self._states[role] = "corrupt"
        self._payloads.pop(role, None)
        self._faults += 1
        self._cache._safe_delete(self._cache.key_for_parent(self.parent, role))

    def stage(self, role: str, payload: bytes) -> None:
        if role not in self._states or self.parent is None or self._complete:
            return
        if not isinstance(payload, bytes) or not payload:
            self.record_fault("codec_fault")
            return
        self._pending[role] = payload

    def record_fault(self, reason: str) -> None:
        if reason not in ("codec_fault", "store_fault", "limit_rejected", "conflict"):
            reason = "store_fault"
        self._faults += 1
        self._reason = reason

    def commit(
        self,
        current_parent: Optional[ControlFoleyCacheKey],
        cancellation_check: Optional[Callable[[], None]] = None,
        revalidate: Optional[Callable[[], ControlFoleyCacheKey]] = None,
    ) -> CacheReport:
        if self._complete:
            return self.report()
        if self.parent is None:
            self._complete = True
            return self.report()
        if current_parent != self.parent:
            self.abort()
            raise ControlFoleyCacheInputDrift("ControlFoley L2 inputs changed before commit")
        try:
            for role, payload in self._pending.items():
                if cancellation_check is not None:
                    cancellation_check()
                key = self._cache.key_for_parent(self.parent, role)
                outcome = self._cache._safe_put(key, payload)
                if outcome == "stored":
                    self._writes += 1
                    self._newly_stored.append(key)
                elif outcome in ("rejected", "conflict"):
                    self.record_fault("limit_rejected" if outcome == "rejected" else "conflict")
                elif outcome == "fault":
                    self.record_fault("store_fault")
            if cancellation_check is not None:
                cancellation_check()
            if revalidate is not None and revalidate() != self.parent:
                raise ControlFoleyCacheInputDrift(
                    "ControlFoley L2 inputs changed during persistence"
                )
        except BaseException:
            self.rollback_new_writes()
            self.abort()
            raise
        self._pending.clear()
        self._complete = True
        return self.report()

    def abort(self) -> None:
        self._pending.clear()
        self._complete = True

    def rollback_new_writes(self) -> None:
        for key in reversed(self._newly_stored):
            self._cache._safe_delete(key)
        self._newly_stored.clear()
        self._writes = 0

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
            snapshot = ControlFoleyConditionCacheSnapshot(0, 0, 0, 0)
        hits = sum(value == "hit" for value in self._states.values())
        misses = sum(value in ("miss", "corrupt") for value in self._states.values())
        return CacheReport(
            hits=hits,
            misses=misses,
            bytes_used=snapshot.bytes_used,
            metadata={
                "schema_version": 1,
                "namespace": "controlfoley",
                "status": "ok" if self._faults == 0 else "degraded",
                "mode": EXPERIMENTAL_L2_CACHE_MODE,
                "levels": {
                    "l2_condition_{0}".format(role): state for role, state in self._states.items()
                },
                "writes": self._writes,
                "faults": self._faults,
                "resident_entries": snapshot.entries,
                "evictions": snapshot.evictions,
                "corruptions": snapshot.corruptions,
                "reason": self._reason,
            },
        )

    def _read(self, role: str) -> None:
        if self.parent is None:
            return
        key = self._cache.key_for_parent(self.parent, role)
        try:
            entry = self._cache._get_entry(key)
        except Exception:
            self._states[role] = "miss"
            self.record_fault("store_fault")
            return
        if entry is None:
            self._states[role] = "miss"
            return
        self._states[role] = "hit"
        self._payloads[role] = entry.payload


class ControlFoleyConditionCache:
    """Failure-isolating L2 facade with generation fencing and input rehash."""

    def __init__(
        self,
        base_policy: ControlFoleyCachePolicy,
        policy: ControlFoleyConditionCachePolicy,
        store: Optional[ControlFoleyConditionCacheStore] = None,
    ) -> None:
        if not isinstance(base_policy, ControlFoleyCachePolicy):
            raise TypeError("base_policy must be ControlFoleyCachePolicy")
        if not isinstance(policy, ControlFoleyConditionCachePolicy):
            raise TypeError("policy must be ControlFoleyConditionCachePolicy")
        self.base_policy = base_policy
        self.policy = policy
        self._store: ControlFoleyConditionCacheStore = (
            store or ControlFoleyLocalConditionCacheStore(policy)
        )
        self._fuses = _PersistentFuseAuthority(policy.root)
        self._generation = 0
        self._globally_disabled = self._fuses.global_fused()
        self._disabled_deployments: set[str] = set()
        self._lock = threading.RLock()

    def begin(
        self,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
    ) -> ControlFoleyConditionCacheTransaction:
        roles = controlfoley_condition_feature_roles(request)
        with self._lock:
            if self._is_disabled(deployment_fingerprint):
                return ControlFoleyConditionCacheTransaction(
                    self,
                    None,
                    roles,
                    self._generation,
                    "store_fault",
                    store_disabled=True,
                )
        try:
            parent = self.parent_key(request, configuration, deployment_fingerprint)
            with self._lock:
                if self._is_disabled(deployment_fingerprint):
                    return ControlFoleyConditionCacheTransaction(
                        self,
                        None,
                        roles,
                        self._generation,
                        "store_fault",
                        store_disabled=True,
                    )
                return ControlFoleyConditionCacheTransaction(self, parent, roles, self._generation)
        except Exception:
            with self._lock:
                return ControlFoleyConditionCacheTransaction(
                    self, None, roles, self._generation, "key_unavailable"
                )

    def parent_key(
        self,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
    ) -> ControlFoleyCacheKey:
        self.policy.assert_configuration(configuration)
        return ControlFoleyCacheKey.from_request(
            request, configuration, deployment_fingerprint, self.base_policy
        )

    def key_for_parent(
        self, parent: ControlFoleyCacheKey, role: str
    ) -> ControlFoleyConditionCacheKey:
        return ControlFoleyConditionCacheKey(
            parent,
            role,
            self.policy.codec_version,
            self.policy.schema_version,
        )

    def commit(
        self,
        transaction: ControlFoleyConditionCacheTransaction,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
        cancellation_check: Optional[Callable[[], None]] = None,
    ) -> CacheReport:
        if transaction.parent is None:
            return transaction.commit(None, cancellation_check)
        try:
            current = self.parent_key(request, configuration, deployment_fingerprint)
        except Exception as error:
            transaction.abort()
            raise ControlFoleyCacheInputDrift(
                "ControlFoley L2 inputs could not be revalidated"
            ) from error
        with self._lock:
            if transaction.generation != self._generation:
                return transaction.invalidated()
            return transaction.commit(
                current,
                cancellation_check,
                lambda: self.parent_key(request, configuration, deployment_fingerprint),
            )

    def assert_current(
        self,
        transaction: ControlFoleyConditionCacheTransaction,
        request: ControlFoleyLocalRequest,
        configuration: Mapping[str, Any],
        deployment_fingerprint: str,
    ) -> None:
        if transaction.parent is None:
            return
        try:
            current = self.parent_key(request, configuration, deployment_fingerprint)
        except Exception as error:
            transaction.rollback_new_writes()
            raise ControlFoleyCacheInputDrift(
                "ControlFoley L2 inputs could not be revalidated"
            ) from error
        if current != transaction.parent:
            transaction.rollback_new_writes()
            raise ControlFoleyCacheInputDrift("ControlFoley L2 inputs changed after persistence")

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            self._generation += 1
            try:
                count = self._store.invalidate_deployment(deployment_fingerprint)
                self._fuses.clear_deployment(deployment_fingerprint)
                self._disabled_deployments.discard(deployment_fingerprint)
                return count
            except Exception:
                self._fuse_deployment(deployment_fingerprint)
                return 0

    def release_deployment(self, deployment_fingerprint: str) -> None:
        _sha256(deployment_fingerprint, "deployment_fingerprint")
        with self._lock:
            if self._globally_disabled:
                return
            try:
                self._store.cleanup()
            except Exception:
                self._generation += 1
                self._fuse_deployment(deployment_fingerprint)

    def invalidate_all(self) -> int:
        with self._lock:
            self._generation += 1
            try:
                count = self._store.invalidate_all()
                self._fuses.clear_all()
                self._globally_disabled = False
                self._disabled_deployments.clear()
                return count
            except Exception:
                self._fuse_all()
                return 0

    def close(self) -> None:
        with self._lock:
            self._generation += 1
            self._globally_disabled = True
            try:
                self._store.close()
            except Exception:
                self._fuse_all()
            finally:
                self._fuses.close()

    def _is_disabled(self, deployment_fingerprint: str) -> bool:
        if self._globally_disabled or deployment_fingerprint in self._disabled_deployments:
            return True
        try:
            return self._fuses.is_fused(deployment_fingerprint)
        except Exception:
            self._globally_disabled = True
            return True

    def _get_entry(
        self, key: ControlFoleyConditionCacheKey
    ) -> Optional[ControlFoleyConditionCacheEntry]:
        with self._lock:
            if self._is_disabled(key.parent.deployment_fingerprint):
                raise RuntimeError("condition cache is unavailable")
            return self._store.get_entry(key)

    def _safe_put(self, key: ControlFoleyConditionCacheKey, payload: bytes) -> str:
        with self._lock:
            if self._is_disabled(key.parent.deployment_fingerprint):
                return "fault"
            try:
                outcome = self._store.put_if_absent(key, payload)
                if outcome == "conflict":
                    self._fuse_deployment(key.parent.deployment_fingerprint)
                    return outcome
                if outcome in ("stored", "present", "rejected"):
                    return outcome
                self._fuse_deployment(key.parent.deployment_fingerprint)
                return "fault"
            except Exception:
                # Publication may have succeeded before an fsync/index/limit
                # failure.  Ownership is uncertain, so future reads must fuse.
                self._fuse_deployment(key.parent.deployment_fingerprint)
                return "fault"

    def _safe_delete(self, key: ControlFoleyConditionCacheKey) -> bool:
        with self._lock:
            if self._is_disabled(key.parent.deployment_fingerprint):
                return False
            try:
                self._store.delete(key)
                return True
            except Exception:
                self._fuse_deployment(key.parent.deployment_fingerprint)
                return False

    def _safe_snapshot(self) -> Optional[ControlFoleyConditionCacheSnapshot]:
        with self._lock:
            if self._globally_disabled:
                return None
            try:
                snapshot = self._store.snapshot()
                return (
                    snapshot if isinstance(snapshot, ControlFoleyConditionCacheSnapshot) else None
                )
            except Exception:
                return None

    def _fuse_deployment(self, deployment_fingerprint: str) -> None:
        self._disabled_deployments.add(deployment_fingerprint)
        try:
            self._fuses.fuse_deployment(deployment_fingerprint)
        except Exception:
            self._globally_disabled = True
            try:
                self._fuses.fuse_all()
            except Exception:
                pass

    def _fuse_all(self) -> None:
        self._globally_disabled = True
        try:
            self._fuses.fuse_all()
        except Exception:
            pass


def l2_cached_configuration_keys() -> frozenset[str]:
    return _L2_CONFIGURATION_KEYS


def merge_controlfoley_cache_reports(
    base: CacheReport,
    condition: CacheReport,
    *,
    encoder_calls: int,
    projection_calls: int,
) -> CacheReport:
    for name, value in (("encoder_calls", encoder_calls), ("projection_calls", projection_calls)):
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("{0} must be a nonnegative integer".format(name))
    base_metadata = dict(base.metadata)
    condition_metadata = dict(condition.metadata)
    levels = dict(base_metadata.get("levels", {}))
    levels.update(dict(condition_metadata.get("levels", {})))
    status = (
        "degraded"
        if "degraded" in (base_metadata.get("status"), condition_metadata.get("status"))
        else "ok"
    )
    return CacheReport(
        hits=base.hits + condition.hits,
        misses=base.misses + condition.misses,
        bytes_used=base.bytes_used + condition.bytes_used,
        metadata={
            "schema_version": 2,
            "namespace": "controlfoley",
            "status": status,
            "mode": EXPERIMENTAL_L2_CACHE_MODE,
            "levels": levels,
            "writes": int(base_metadata.get("writes", 0))
            + int(condition_metadata.get("writes", 0)),
            "faults": int(base_metadata.get("faults", 0))
            + int(condition_metadata.get("faults", 0)),
            "resident_entries": int(base_metadata.get("resident_entries", 0))
            + int(condition_metadata.get("resident_entries", 0)),
            "evictions": int(base_metadata.get("evictions", 0))
            + int(condition_metadata.get("evictions", 0)),
            "encoder_calls": encoder_calls,
            "projection_calls": projection_calls,
            "tier_bytes": {
                "l0_l1": base.bytes_used,
                "l2_condition": condition.bytes_used,
            },
            "reason": condition_metadata.get("reason") or base_metadata.get("reason"),
        },
    )


def _json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_value(item) for item in value]
    return value


def _paths_overlap(first: Path, second: Path) -> bool:
    try:
        first.relative_to(second)
        return True
    except ValueError:
        pass
    try:
        second.relative_to(first)
        return True
    except ValueError:
        return False


def _unique_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("L2 metadata contains duplicate keys")
        result[key] = value
    return result


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError("{0} must be a positive integer".format(name))
    return value


def _finite_number(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
    ):
        raise ValueError("{0} must be finite and nonnegative".format(name))
    return float(value)


def _safe_id(value: object, name: str) -> str:
    if (
        not isinstance(value, str)
        or not 1 <= len(value) <= 64
        or not value.isascii()
        or any(character not in _SAFE_ID for character in value)
    ):
        raise ValueError("{0} must be a safe short ASCII identifier".format(name))
    return value


def _is_sha256(value: object) -> bool:
    try:
        _sha256(value, "sha256")
    except ValueError:
        return False
    return True


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
