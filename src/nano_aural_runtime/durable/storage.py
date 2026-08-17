"""Safe local, content-addressed BlobStore implementation for development/tests."""

from __future__ import annotations

import hashlib
import os
import stat as stat_module
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Iterable, Tuple
from uuid import uuid4


@dataclass(frozen=True)
class BlobObject:
    sha256: str
    size_bytes: int
    storage_key: str


class LocalBlobStore:
    """A canonical-key-only local BlobStore that never follows path symlinks.

    All filesystem operations resolve each trusted path component through a
    directory file descriptor with ``O_NOFOLLOW``. Callers cannot use filenames,
    dot paths, backslashes, NULs, or alternative digest spellings as blob keys.
    """

    _PREFIX = ("blobs", "sha256")

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

    @staticmethod
    def canonical_key(sha256: str) -> str:
        if not isinstance(sha256, str) or len(sha256) != 64 or sha256 != sha256.lower():
            raise ValueError("sha256 must be a lowercase 64-character hex digest")
        try:
            int(sha256, 16)
        except ValueError as error:
            raise ValueError("sha256 must be lowercase hexadecimal") from error
        return "blobs/sha256/{0}/{1}/{2}".format(sha256[:2], sha256[2:4], sha256)

    def _parse_key(self, storage_key: str) -> Tuple[str, str, str]:
        if (
            not isinstance(storage_key, str)
            or not storage_key
            or "\x00" in storage_key
            or "\\" in storage_key
            or storage_key.startswith("/")
        ):
            raise ValueError("storage_key must be an exact canonical key")
        parts = tuple(storage_key.split("/"))
        if (
            len(parts) != 5
            or parts[:2] != self._PREFIX
            or any(part in ("", ".", "..") for part in parts)
        ):
            raise ValueError("storage_key must be an exact canonical key")
        digest = parts[4]
        if (
            storage_key != self.canonical_key(digest)
            or parts[2] != digest[:2]
            or parts[3] != digest[2:4]
        ):
            raise ValueError("storage_key must be an exact canonical key")
        return parts[2], parts[3], digest

    def _parent_fd(self, storage_key: str, create: bool) -> Tuple[int, str]:
        first, second, digest = self._parse_key(storage_key)
        fd = os.dup(self._root_fd)
        try:
            for component in self._PREFIX + (first, second):
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
            return fd, digest
        except BaseException:
            os.close(fd)
            raise

    @staticmethod
    def _read_fd(fd: int) -> bytes:
        with os.fdopen(fd, "rb") as stream:
            return stream.read()

    def _read_existing(self, parent_fd: int, digest: str) -> bytes:
        fd = os.open(digest, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            os.close(fd)
            raise RuntimeError("canonical blob is not a regular file")
        return self._read_fd(fd)

    def put_immutable(self, data: bytes) -> BlobObject:
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        digest = hashlib.sha256(data).hexdigest()
        key = self.canonical_key(digest)
        parent_fd, filename = self._parent_fd(key, create=True)
        temporary_name = ".put-" + uuid4().hex
        try:
            try:
                existing = self._read_existing(parent_fd, filename)
            except FileNotFoundError:
                existing = None
            if existing is not None:
                if existing != data:
                    raise RuntimeError("immutable blob path contains unexpected bytes")
                return BlobObject(digest, len(data), key)
            temp_fd = os.open(
                temporary_name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=parent_fd,
            )
            try:
                with os.fdopen(temp_fd, "wb") as stream:
                    stream.write(data)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(
                        temporary_name,
                        filename,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    if self._read_existing(parent_fd, filename) != data:
                        raise RuntimeError(
                            "immutable blob path contains unexpected bytes"
                        ) from None
                os.fsync(parent_fd)
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_fd)
        return BlobObject(digest, len(data), key)

    def put_stream(self, chunks: Iterable[bytes]) -> BlobObject:
        """Promote a byte stream without retaining the full object in memory."""
        digest = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile(mode="w+b") as spool:
            for chunk in chunks:
                if not isinstance(chunk, bytes):
                    raise TypeError("blob stream chunks must be bytes")
                digest.update(chunk)
                size += len(chunk)
                spool.write(chunk)
            key = self.canonical_key(digest.hexdigest())
            parent_fd, filename = self._parent_fd(key, create=True)
            temporary_name = ".stream-" + uuid4().hex
            try:
                try:
                    existing_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                except FileNotFoundError:
                    existing_fd = -1
                if existing_fd >= 0:
                    existing_digest = hashlib.sha256()
                    existing_size = 0
                    with os.fdopen(existing_fd, "rb") as existing_stream:
                        while True:
                            existing_chunk = existing_stream.read(1024 * 1024)
                            if not existing_chunk:
                                break
                            existing_digest.update(existing_chunk)
                            existing_size += len(existing_chunk)
                    if existing_size != size or existing_digest.hexdigest() != digest.hexdigest():
                        raise RuntimeError("immutable blob path contains unexpected bytes")
                    return BlobObject(digest.hexdigest(), size, key)
                fd = os.open(
                    temporary_name,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                    0o600,
                    dir_fd=parent_fd,
                )
                with os.fdopen(fd, "wb") as target:
                    spool.seek(0)
                    while True:
                        chunk = spool.read(1024 * 1024)
                        if not chunk:
                            break
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
                try:
                    os.link(
                        temporary_name,
                        filename,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    existing_fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
                    existing_digest = hashlib.sha256()
                    existing_size = 0
                    with os.fdopen(existing_fd, "rb") as existing_stream:
                        while True:
                            existing_chunk = existing_stream.read(1024 * 1024)
                            if not existing_chunk:
                                break
                            existing_digest.update(existing_chunk)
                            existing_size += len(existing_chunk)
                    if existing_size != size or existing_digest.hexdigest() != digest.hexdigest():
                        raise RuntimeError(
                            "immutable blob path contains unexpected bytes"
                        ) from None
                os.fsync(parent_fd)
            finally:
                try:
                    os.unlink(temporary_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
                os.close(parent_fd)
        return BlobObject(digest.hexdigest(), size, key)

    def open_reader(self, storage_key: str) -> BinaryIO:
        parent_fd, filename = self._parent_fd(storage_key, create=False)
        try:
            fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=parent_fd)
        finally:
            os.close(parent_fd)
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            os.close(fd)
            raise RuntimeError("canonical blob is not a regular file")
        return os.fdopen(fd, "rb")

    def stat(self, storage_key: str) -> BlobObject:
        parent_fd, filename = self._parent_fd(storage_key, create=False)
        try:
            data = self._read_existing(parent_fd, filename)
        finally:
            os.close(parent_fd)
        digest = hashlib.sha256(data).hexdigest()
        if storage_key != self.canonical_key(digest):
            raise RuntimeError("stored blob does not match its canonical key")
        return BlobObject(digest, len(data), storage_key)

    def delete(self, storage_key: str) -> None:
        parent_fd, filename = self._parent_fd(storage_key, create=False)
        try:
            try:
                info = os.stat(filename, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return
            if not stat_module.S_ISREG(info.st_mode):
                raise RuntimeError("canonical blob is not a regular file")
            os.unlink(filename, dir_fd=parent_fd)
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
