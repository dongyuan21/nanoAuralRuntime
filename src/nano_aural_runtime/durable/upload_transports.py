"""CPU transport contracts for Phase 3B; no HTTP or cloud SDK dependency."""

from __future__ import annotations

import hashlib
import io
from typing import BinaryIO, Dict, Iterable, Protocol, Tuple

from .errors import StateTransitionError
from .storage import BlobObject, LocalBlobStore


class CanonicalBlobStore(Protocol):
    """Content-addressed immutable bytes; provider ETags are never identity."""

    def put_stream(self, chunks: Iterable[bytes]) -> BlobObject: ...
    def open_reader(self, storage_key: str) -> BinaryIO: ...
    def stat(self, storage_key: str) -> BlobObject: ...


class InMemoryS3CompatibleCanonical:
    """S3-shaped CPU double with the exact LocalBlobStore canonical contract."""

    def __init__(self) -> None:
        self._objects: Dict[str, bytes] = {}

    def put_stream(self, chunks: Iterable[bytes]) -> BlobObject:
        data = b"".join(self._validated_chunks(chunks))
        digest = hashlib.sha256(data).hexdigest()
        key = LocalBlobStore.canonical_key(digest)
        existing = self._objects.setdefault(key, data)
        if existing != data:
            raise RuntimeError("immutable blob path contains unexpected bytes")
        return BlobObject(digest, len(data), key)

    def open_reader(self, storage_key: str) -> BinaryIO:
        self._validate_key(storage_key)
        return io.BytesIO(self._objects[storage_key])

    def stat(self, storage_key: str) -> BlobObject:
        self._validate_key(storage_key)
        data = self._objects[storage_key]
        digest = hashlib.sha256(data).hexdigest()
        if storage_key != LocalBlobStore.canonical_key(digest):
            raise RuntimeError("stored blob does not match its canonical key")
        return BlobObject(digest, len(data), storage_key)

    @staticmethod
    def _validated_chunks(chunks: Iterable[bytes]) -> Tuple[bytes, ...]:
        value = tuple(chunks)
        if not all(isinstance(chunk, bytes) for chunk in value):
            raise TypeError("blob stream chunks must be bytes")
        return value

    @staticmethod
    def _validate_key(storage_key: str) -> None:
        parts = storage_key.split("/") if isinstance(storage_key, str) else ()
        if len(parts) != 5 or storage_key != LocalBlobStore.canonical_key(parts[-1]):
            raise ValueError("storage_key must be an exact canonical key")


class MultipartUploadBuffer:
    """Strict in-memory multipart assembly contract for transport adapters."""

    def __init__(self) -> None:
        self._parts: Dict[int, bytes] = {}
        self._state = "open"

    def put_part(self, part_number: int, content: bytes) -> None:
        if self._state != "open":
            raise StateTransitionError("multipart upload is terminal")
        if isinstance(part_number, bool) or not isinstance(part_number, int) or part_number < 1:
            raise ValueError("part_number must be a positive integer")
        if not isinstance(content, bytes):
            raise TypeError("multipart part must be bytes")
        existing = self._parts.get(part_number)
        if existing is not None and existing != content:
            raise StateTransitionError("multipart part conflicts with prior content")
        self._parts[part_number] = content

    def complete(self) -> Iterable[bytes]:
        if self._state != "open":
            raise StateTransitionError("multipart upload is terminal")
        if not self._parts or tuple(sorted(self._parts)) != tuple(range(1, len(self._parts) + 1)):
            raise StateTransitionError("multipart upload has missing part numbers")
        self._state = "completed"
        return tuple(self._parts[number] for number in range(1, len(self._parts) + 1))

    def abort(self) -> None:
        if self._state != "open":
            raise StateTransitionError("multipart upload is terminal")
        self._state = "aborted"
        self._parts.clear()
