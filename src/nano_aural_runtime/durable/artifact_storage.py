"""Attempt-scoped immutable object storage for Phase 3E publication.

These objects are publication intermediates, not durable visible artifacts and
not content identity.  Only an application-streamed full-file SHA-256 may be
promoted into the canonical BlobStore.  Provider ETags remain diagnostics.
"""

from __future__ import annotations

import hashlib
import io
import os
import stat as stat_module
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from threading import RLock
from typing import BinaryIO, Callable, Dict, Iterable, Optional, Protocol, Tuple
from uuid import UUID, uuid4

from .domain import ArtifactKind
from .errors import StateTransitionError

_BUFFER_SIZE = 1024 * 1024


@dataclass(frozen=True)
class AttemptObject:
    storage_key: str
    size_bytes: int
    sha256: str
    created_at: datetime
    transport_etag: Optional[str] = None

    def __post_init__(self) -> None:
        parse_attempt_key(self.storage_key)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise TypeError("size_bytes must be an integer")
        if self.size_bytes < 0:
            raise ValueError("size_bytes must be non-negative")
        _validate_sha256(self.sha256)
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")
        if self.transport_etag is not None and (
            not isinstance(self.transport_etag, str) or not self.transport_etag
        ):
            raise ValueError("transport_etag must be non-empty when supplied")


@dataclass(frozen=True)
class AttemptInventoryPage:
    """One strictly bounded persistent inventory scan page."""

    objects: Tuple[AttemptObject, ...]
    scanned: int
    next_cursor: int
    wrapped: bool

    def __post_init__(self) -> None:
        if self.scanned < 0 or self.next_cursor < 0:
            raise ValueError("inventory page counters must be non-negative")
        if len(self.objects) > self.scanned:
            raise ValueError("inventory page cannot contain more objects than scanned entries")


class AttemptArtifactStore(Protocol):
    """Immutable attempt-object boundary shared by Local and S3 transports."""

    def put_stream(self, storage_key: str, chunks: Iterable[bytes]) -> AttemptObject: ...
    def open_reader(self, storage_key: str) -> BinaryIO: ...
    def stat(self, storage_key: str) -> AttemptObject: ...
    def delete(self, storage_key: str) -> None: ...
    def list_before(self, cutoff: datetime) -> Tuple[AttemptObject, ...]: ...
    def inventory_page_before(
        self, cutoff: datetime, limit: int, scan_id: str
    ) -> AttemptInventoryPage: ...


def attempt_key(
    job_id: str,
    attempt_id: str,
    lease_epoch: int,
    kind: ArtifactKind,
    publication_id: str,
) -> str:
    """Return the only accepted server-generated publication object key."""

    job = str(UUID(job_id))
    attempt = str(UUID(attempt_id))
    publication = str(UUID(publication_id))
    if isinstance(lease_epoch, bool) or not isinstance(lease_epoch, int) or lease_epoch < 1:
        raise ValueError("lease_epoch must be a positive integer")
    if not isinstance(kind, ArtifactKind):
        raise TypeError("kind must be ArtifactKind")
    return "attempts/{0}/{1}/epoch-{2}/{3}/{4}".format(
        job, attempt, lease_epoch, kind.value, publication
    )


def parse_attempt_key(storage_key: str) -> Tuple[str, str, int, ArtifactKind, str]:
    """Reject every spelling except the exact lowercase canonical key."""

    if (
        not isinstance(storage_key, str)
        or not storage_key
        or "\x00" in storage_key
        or "\\" in storage_key
        or storage_key.startswith("/")
    ):
        raise ValueError("storage_key must be an exact attempt key")
    parts = tuple(storage_key.split("/"))
    if len(parts) != 6 or parts[0] != "attempts" or any(part in ("", ".", "..") for part in parts):
        raise ValueError("storage_key must be an exact attempt key")
    try:
        job = str(UUID(parts[1]))
        attempt = str(UUID(parts[2]))
        publication = str(UUID(parts[5]))
        if not parts[3].startswith("epoch-"):
            raise ValueError
        epoch_text = parts[3][len("epoch-") :]
        epoch = int(epoch_text)
        if epoch < 1 or epoch_text != str(epoch):
            raise ValueError
        kind = ArtifactKind(parts[4])
    except (ValueError, TypeError) as error:
        raise ValueError("storage_key must be an exact attempt key") from error
    expected = attempt_key(job, attempt, epoch, kind, publication)
    if storage_key != expected:
        raise ValueError("storage_key must be an exact attempt key")
    return job, attempt, epoch, kind, publication


class LocalAttemptArtifactStore:
    """Symlink-safe local attempt storage with conditional immutable writes."""

    _INVENTORY = ".attempt-inventory-v1"
    _MAX_INVENTORY_LINE = 512

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._root_fd = os.open(str(self._root), os.O_RDONLY | os.O_DIRECTORY)

    def close(self) -> None:
        if self._root_fd >= 0:
            os.close(self._root_fd)
            self._root_fd = -1

    def __del__(self) -> None:
        self.close()

    def put_stream(self, storage_key: str, chunks: Iterable[bytes]) -> AttemptObject:
        parts = self._components(storage_key)
        # Record intent before object creation.  A crash can therefore leave a
        # harmless missing journal entry, but never an unindexed object in the
        # write-after-link crash window that inventory recovery exists to find.
        self._record_inventory(storage_key)
        parent_fd = self._parent_fd(parts[:-1], create=True)
        name = parts[-1]
        temporary = ".publish-" + uuid4().hex
        digest = hashlib.sha256()
        size = 0
        created_at = datetime.now(timezone.utc)
        try:
            temporary_fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                with os.fdopen(temporary_fd, "wb") as stream:
                    for chunk in chunks:
                        if not isinstance(chunk, bytes):
                            raise TypeError("attempt object chunks must be bytes")
                        digest.update(chunk)
                        size += len(chunk)
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary,
                        name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    existing = self._stat_open(parent_fd, name, storage_key)
                    if existing.sha256 != digest.hexdigest() or existing.size_bytes != size:
                        raise StateTransitionError(
                            "attempt object key already contains different bytes"
                        ) from None
                    return existing
                os.fsync(parent_fd)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)
        return AttemptObject(storage_key, size, digest.hexdigest(), created_at)

    def open_reader(self, storage_key: str) -> BinaryIO:
        parts = self._components(storage_key)
        parent_fd = self._parent_fd(parts[:-1], create=False)
        try:
            fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            os.close(fd)
            raise RuntimeError("attempt object is not a regular file")
        return os.fdopen(fd, "rb")

    def stat(self, storage_key: str) -> AttemptObject:
        parts = self._components(storage_key)
        parent_fd = self._parent_fd(parts[:-1], create=False)
        try:
            return self._stat_open(parent_fd, parts[-1], storage_key)
        finally:
            os.close(parent_fd)

    def delete(self, storage_key: str) -> None:
        parts = self._components(storage_key)
        parent_fd = self._parent_fd(parts[:-1], create=False)
        try:
            try:
                info = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat_module.S_ISREG(info.st_mode):
                raise RuntimeError("attempt object is not a regular file")
            os.unlink(parts[-1], dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)

    def list_before(self, cutoff: datetime) -> Tuple[AttemptObject, ...]:
        """Compatibility full inventory; operator recovery uses bounded pages."""

        _aware(cutoff, "cutoff")
        found: Dict[str, AttemptObject] = {}
        cursor = 0
        while True:
            page = self._inventory_page_from(cutoff, 100, cursor)
            for object_ in page.objects:
                found[object_.storage_key] = object_
            if page.wrapped:
                break
            cursor = page.next_cursor
        return tuple(found[key] for key in sorted(found))

    def inventory_page_before(
        self, cutoff: datetime, limit: int, scan_id: str
    ) -> AttemptInventoryPage:
        """Scan/hash at most ``limit`` journal entries and persist fairness."""

        _aware(cutoff, "cutoff")
        _positive_limit(limit)
        self._scan_id(scan_id)
        cursor = self._read_inventory_cursor(scan_id)
        page = self._inventory_page_from(cutoff, limit, cursor)
        self._write_inventory_cursor(scan_id, page.next_cursor)
        return page

    def _inventory_page_from(
        self, cutoff: datetime, limit: int, cursor: int
    ) -> AttemptInventoryPage:
        try:
            descriptor = os.open(
                self._INVENTORY,
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            return AttemptInventoryPage((), 0, 0, True)
        information = os.fstat(descriptor)
        if not stat_module.S_ISREG(information.st_mode):
            os.close(descriptor)
            raise RuntimeError("attempt inventory is not a regular file")
        objects = []
        seen = set()
        scanned = 0
        wrapped = False
        next_cursor = cursor
        with os.fdopen(descriptor, "rb") as source:
            source.seek(cursor)
            while scanned < limit:
                line = source.readline(self._MAX_INVENTORY_LINE + 1)
                if not line:
                    wrapped = True
                    next_cursor = 0
                    break
                if len(line) > self._MAX_INVENTORY_LINE or not line.endswith(b"\n"):
                    raise RuntimeError("attempt inventory contains an invalid record")
                scanned += 1
                next_cursor = source.tell()
                try:
                    key = line[:-1].decode("ascii")
                    parse_attempt_key(key)
                    if key in seen:
                        continue
                    seen.add(key)
                    created_at = self._created_at(key)
                    if created_at >= cutoff:
                        continue
                    current = self.stat(key)
                    if current.created_at < cutoff:
                        objects.append(current)
                except (OSError, ValueError, RuntimeError):
                    continue
        return AttemptInventoryPage(tuple(objects), scanned, next_cursor, wrapped)

    def _record_inventory(self, storage_key: str) -> None:
        payload = storage_key.encode("ascii") + b"\n"
        if len(payload) > self._MAX_INVENTORY_LINE:
            raise ValueError("attempt key exceeds inventory record bound")
        descriptor = os.open(
            self._INVENTORY,
            os.O_WRONLY | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._root_fd,
        )
        try:
            if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("attempt inventory is not a regular file")
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short attempt inventory write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    def _created_at(self, storage_key: str) -> datetime:
        parts = self._components(storage_key)
        parent_fd = self._parent_fd(parts[:-1], create=False)
        try:
            information = os.stat(parts[-1], dir_fd=parent_fd, follow_symlinks=False)
        finally:
            os.close(parent_fd)
        if stat_module.S_ISLNK(information.st_mode) or not stat_module.S_ISREG(information.st_mode):
            raise RuntimeError("attempt object is not a regular file")
        return datetime.fromtimestamp(information.st_mtime, timezone.utc)

    @staticmethod
    def _scan_id(scan_id: str) -> None:
        if (
            not isinstance(scan_id, str)
            or not scan_id
            or any(character not in "abcdefghijklmnopqrstuvwxyz_" for character in scan_id)
        ):
            raise ValueError("scan_id must contain only lowercase letters and underscores")

    def _cursor_name(self, scan_id: str) -> str:
        self._scan_id(scan_id)
        return ".attempt-inventory-{0}.cursor".format(scan_id)

    def _read_inventory_cursor(self, scan_id: str) -> int:
        try:
            descriptor = os.open(
                self._cursor_name(scan_id),
                os.O_RDONLY | os.O_NONBLOCK | os.O_NOFOLLOW,
                dir_fd=self._root_fd,
            )
        except FileNotFoundError:
            return 0
        try:
            if not stat_module.S_ISREG(os.fstat(descriptor).st_mode):
                raise RuntimeError("attempt inventory cursor is not a regular file")
            payload = os.read(descriptor, 65)
        finally:
            os.close(descriptor)
        if not payload or len(payload) > 64 or not payload.isdigit():
            raise RuntimeError("attempt inventory cursor is invalid")
        return int(payload)

    def _write_inventory_cursor(self, scan_id: str, cursor: int) -> None:
        name = self._cursor_name(scan_id)
        temporary = name + ".tmp-" + uuid4().hex
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=self._root_fd,
        )
        try:
            payload = str(cursor).encode("ascii")
            if os.write(descriptor, payload) != len(payload):
                raise OSError("short attempt inventory cursor write")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(
                temporary,
                name,
                src_dir_fd=self._root_fd,
                dst_dir_fd=self._root_fd,
            )
            os.fsync(self._root_fd)
        finally:
            try:
                os.unlink(temporary, dir_fd=self._root_fd)
            except FileNotFoundError:
                pass

    @staticmethod
    def _components(storage_key: str) -> Tuple[str, ...]:
        parse_attempt_key(storage_key)
        return tuple(storage_key.split("/"))

    def _parent_fd(self, components: Tuple[str, ...], create: bool) -> int:
        fd = os.dup(self._root_fd)
        try:
            for component in components:
                if create:
                    try:
                        os.mkdir(component, mode=0o700, dir_fd=fd)
                    except FileExistsError:
                        pass
                next_fd = os.open(
                    component,
                    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=fd,
                )
                os.close(fd)
                fd = next_fd
            return fd
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _stat_open(parent_fd: int, name: str, storage_key: str) -> AttemptObject:
        fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            os.close(fd)
            raise RuntimeError("attempt object is not a regular file")
        digest = hashlib.sha256()
        size = 0
        with os.fdopen(fd, "rb") as stream:
            while True:
                chunk = stream.read(_BUFFER_SIZE)
                if not chunk:
                    break
                digest.update(chunk)
                size += len(chunk)
        created_at = datetime.fromtimestamp(info.st_mtime, timezone.utc)
        return AttemptObject(storage_key, size, digest.hexdigest(), created_at)


class InMemoryS3CompatibleAttemptStore:
    """CPU S3 contract double; ETags are deliberately non-authoritative."""

    def __init__(self, etag_factory: Optional[Callable[[str, bytes], str]] = None) -> None:
        self._objects: Dict[str, Tuple[bytes, datetime, str]] = {}
        self._inventory: list[str] = []
        self._inventory_cursors: Dict[str, int] = {}
        self._lock = RLock()
        self._etag_factory = etag_factory or self._default_etag

    def put_stream(self, storage_key: str, chunks: Iterable[bytes]) -> AttemptObject:
        parse_attempt_key(storage_key)
        parts = []
        for chunk in chunks:
            if not isinstance(chunk, bytes):
                raise TypeError("attempt object chunks must be bytes")
            parts.append(chunk)
        data = b"".join(parts)
        with self._lock:
            self._inventory.append(storage_key)
            existing = self._objects.get(storage_key)
            if existing is not None:
                if existing[0] != data:
                    raise StateTransitionError(
                        "attempt object key already contains different bytes"
                    )
                return self._object(storage_key, existing)
            created = datetime.now(timezone.utc)
            etag = self._etag_factory(storage_key, data)
            if not isinstance(etag, str) or not etag:
                raise ValueError("etag factory must return a non-empty string")
            stored = (data, created, etag)
            self._objects[storage_key] = stored
            return self._object(storage_key, stored)

    def open_reader(self, storage_key: str) -> BinaryIO:
        parse_attempt_key(storage_key)
        with self._lock:
            return io.BytesIO(self._objects[storage_key][0])

    def stat(self, storage_key: str) -> AttemptObject:
        parse_attempt_key(storage_key)
        with self._lock:
            return self._object(storage_key, self._objects[storage_key])

    def delete(self, storage_key: str) -> None:
        parse_attempt_key(storage_key)
        with self._lock:
            self._objects.pop(storage_key, None)

    def list_before(self, cutoff: datetime) -> Tuple[AttemptObject, ...]:
        _aware(cutoff, "cutoff")
        with self._lock:
            return tuple(
                sorted(
                    (
                        self._object(key, value)
                        for key, value in self._objects.items()
                        if value[1] < cutoff
                    ),
                    key=lambda item: item.storage_key,
                )
            )

    def inventory_page_before(
        self, cutoff: datetime, limit: int, scan_id: str
    ) -> AttemptInventoryPage:
        _aware(cutoff, "cutoff")
        _positive_limit(limit)
        LocalAttemptArtifactStore._scan_id(scan_id)
        with self._lock:
            cursor = self._inventory_cursors.get(scan_id, 0)
            keys = self._inventory[cursor : cursor + limit]
            next_cursor = cursor + len(keys)
            wrapped = next_cursor >= len(self._inventory)
            if wrapped:
                next_cursor = 0
            self._inventory_cursors[scan_id] = next_cursor
            seen = set()
            objects = []
            for key in keys:
                if key in seen:
                    continue
                seen.add(key)
                stored = self._objects.get(key)
                if stored is not None and stored[1] < cutoff:
                    objects.append(self._object(key, stored))
            return AttemptInventoryPage(tuple(objects), len(keys), next_cursor, wrapped)

    @staticmethod
    def _object(storage_key: str, stored: Tuple[bytes, datetime, str]) -> AttemptObject:
        data, created_at, etag = stored
        return AttemptObject(
            storage_key,
            len(data),
            hashlib.sha256(data).hexdigest(),
            created_at,
            etag,
        )

    @staticmethod
    def _default_etag(_storage_key: str, data: bytes) -> str:
        # The suffix emphasizes that this diagnostic is not a full-file identity.
        return hashlib.md5(data).hexdigest() + "-2"  # nosec B303: S3 diagnostic double


def _validate_sha256(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ValueError("sha256 must be lowercase 64-character hexadecimal")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("sha256 must be lowercase 64-character hexadecimal") from error


def _aware(value: datetime, name: str) -> None:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("{0} must be timezone-aware".format(name))


def _positive_limit(limit: int) -> None:
    if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1:
        raise ValueError("limit must be positive")
