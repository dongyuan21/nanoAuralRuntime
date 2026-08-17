"""Phase 3B verified-upload contracts and CPU implementation.

Multipart ETags and provider checksums are transport diagnostics only.  The
only content identity accepted here is the application-streamed full-file
SHA-256 calculated while reading the staged object.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import selectors
import signal
import stat as stat_module
import subprocess
import tempfile
import time
import wave
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from threading import RLock
from typing import BinaryIO, Dict, Iterable, Optional, Protocol, Sequence, Tuple
from uuid import UUID, uuid4

from .domain import AssetKind, AssetRecord, AssetState, BlobRecord, BlobState
from .errors import DurableInvariantError, NotFoundError, StateTransitionError
from .upload_transports import CanonicalBlobStore


class UploadMode(str, Enum):
    SINGLE = "single"
    MULTIPART = "multipart"


class UploadState(str, Enum):
    INITIATED = "initiated"
    UPLOADED = "uploaded"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    REJECTED = "rejected"
    EXPIRED = "expired"


@dataclass(frozen=True)
class UploadSession:
    session_id: str
    namespace_id: str
    mode: UploadMode
    expected_size_bytes: int
    staging_key: str
    expires_at: datetime
    expected_sha256: Optional[str] = None
    state: UploadState = UploadState.INITIATED
    version: int = 0
    verified_blob_id: Optional[str] = None
    verified_asset_id: Optional[str] = None
    rejection_reason: Optional[str] = None

    def __post_init__(self) -> None:
        UUID(self.session_id)
        if not isinstance(self.namespace_id, str) or not self.namespace_id.strip():
            raise ValueError("namespace_id must be non-empty")
        if not isinstance(self.expected_size_bytes, int) or self.expected_size_bytes < 0:
            raise ValueError("expected_size_bytes must be non-negative")
        if not isinstance(self.mode, UploadMode) or not isinstance(self.state, UploadState):
            raise TypeError("mode and state must be upload enums")
        if self.staging_key != "staging/" + str(UUID(self.session_id)):
            raise ValueError("staging_key must exactly bind the upload session UUID")
        if self.expires_at.tzinfo is None or self.expires_at.utcoffset() is None:
            raise ValueError("expires_at must be timezone-aware")
        if self.expected_sha256 is not None:
            _sha256(self.expected_sha256)
        if not isinstance(self.version, int) or self.version < 0:
            raise ValueError("version must be non-negative")
        verified = self.state == UploadState.VERIFIED
        if verified != (self.verified_blob_id is not None and self.verified_asset_id is not None):
            raise ValueError("verified pointer shape does not match state")
        if not verified and (
            self.verified_blob_id is not None or self.verified_asset_id is not None
        ):
            raise ValueError("non-verified session cannot have verified pointers")
        if self.state == UploadState.REJECTED:
            if not isinstance(self.rejection_reason, str) or not self.rejection_reason:
                raise ValueError("rejected session requires a reason")
        elif self.rejection_reason is not None:
            raise ValueError("only a rejected session may have a rejection reason")


@dataclass(frozen=True)
class MediaInfo:
    media_type: str
    duration_seconds: float

    def __post_init__(self) -> None:
        if not isinstance(self.media_type, str) or not self.media_type:
            raise ValueError("media_type must be non-empty")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be finite and positive")


class MediaProbe(Protocol):
    def probe(self, stream: BinaryIO) -> MediaInfo: ...


class WaveMediaProbe:
    """Small CPU-only probe for tests and local administration."""

    def probe(self, stream: BinaryIO) -> MediaInfo:
        try:
            with wave.open(stream, "rb") as source:
                rate = source.getframerate()
                if rate <= 0:
                    raise ValueError("invalid WAV sample rate")
                expected = source.getnframes() * source.getnchannels() * source.getsampwidth()
                if len(source.readframes(source.getnframes())) != expected:
                    raise ValueError("truncated WAV frames")
                return MediaInfo("audio/wav", source.getnframes() / float(rate))
        except (wave.Error, EOFError, ValueError) as error:
            raise ValueError("invalid WAV media") from error


class CommandMediaProbe:
    """Injectable argv-only probe for production adapters; never invokes a shell."""

    _ALLOWED_TYPES = frozenset(("audio/wav", "audio/mpeg", "audio/flac", "video/mp4"))

    def __init__(
        self,
        argv_prefix: Sequence[str],
        timeout_seconds: float = 5.0,
        max_output_bytes: int = 65536,
    ) -> None:
        if not argv_prefix or not all(isinstance(item, str) and item for item in argv_prefix):
            raise ValueError("argv_prefix must be non-empty strings")
        self._argv_prefix = tuple(argv_prefix)
        if (
            isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("timeout_seconds must be finite and positive")
        if not isinstance(max_output_bytes, int) or max_output_bytes < 1:
            raise ValueError("max_output_bytes must be positive")
        self._timeout_seconds = timeout_seconds
        self._max_output_bytes = max_output_bytes

    def probe_path(self, path: Path) -> MediaInfo:
        try:
            stdout, stderr, returncode = self._run_limited(path)
        except (OSError, subprocess.TimeoutExpired, TimeoutError) as error:
            raise ValueError("media probe failed") from error
        if returncode != 0:
            raise ValueError("media probe rejected staged object")
        try:
            payload = json.loads(stdout.decode("utf-8", errors="strict"))
            if (
                not isinstance(payload, dict)
                or set(payload) != {"media_type", "duration_seconds"}
                or not isinstance(payload["media_type"], str)
                or isinstance(payload["duration_seconds"], bool)
            ):
                raise ValueError("media probe returned invalid metadata")
            media_type, duration = payload["media_type"], float(payload["duration_seconds"])
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
            raise ValueError("media probe returned invalid metadata") from error
        if media_type not in self._ALLOWED_TYPES or not math.isfinite(duration) or duration <= 0:
            raise ValueError("media probe returned invalid media")
        return MediaInfo(media_type, duration)

    def _run_limited(self, path: Path) -> Tuple[bytes, bytes, int]:
        process = subprocess.Popen(
            [*self._argv_prefix, str(path)],
            shell=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=os.name == "posix",
        )
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        streams = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
        for descriptor in streams:
            os.set_blocking(descriptor, False)
            selector.register(descriptor, selectors.EVENT_READ)
        deadline = time.monotonic() + float(self._timeout_seconds)
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError("media probe timed out")
                for key, _ in selector.select(remaining):
                    chunk = os.read(key.fd, 8192)
                    if not chunk:
                        selector.unregister(key.fd)
                        continue
                    target = streams[key.fd]
                    target.extend(chunk)
                    if len(target) > self._max_output_bytes:
                        raise ValueError("media probe output exceeded limit")
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("media probe timed out")
            return (
                bytes(streams[process.stdout.fileno()]),
                bytes(streams[process.stderr.fileno()]),
                process.wait(remaining),
            )
        except BaseException:
            self._terminate_process_group(process)
            raise
        finally:
            selector.close()

    @staticmethod
    def _terminate_process_group(process: subprocess.Popen[bytes]) -> None:
        try:
            if os.name == "posix":
                os.killpg(os.getpgid(process.pid), signal.SIGTERM)
            else:
                process.terminate()
            process.wait(timeout=0.2)
        except (OSError, subprocess.TimeoutExpired):
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(process.pid), signal.SIGKILL)
                else:
                    process.kill()
                process.wait(timeout=1)
            except (OSError, subprocess.TimeoutExpired):
                pass

    def probe(self, stream: BinaryIO) -> MediaInfo:
        with tempfile.NamedTemporaryFile(prefix="nano-aural-probe-", suffix=".media") as snapshot:
            while True:
                chunk = stream.read(1024 * 1024)
                if not chunk:
                    break
                snapshot.write(chunk)
            snapshot.flush()
            return self.probe_path(Path(snapshot.name))


class StagingBlobStore(Protocol):
    def write_stream(self, session_id: str, chunks: Iterable[bytes]) -> str: ...
    def open_reader(self, staging_key: str) -> BinaryIO: ...
    def delete(self, staging_key: str) -> None: ...
    def exists(self, staging_key: str) -> bool: ...


class S3CompatibleStagingContract(StagingBlobStore, Protocol):
    """Provider boundary; multipart ETag is intentionally absent from identity APIs."""


class InMemoryS3CompatibleStaging:
    """CPU contract double for S3-compatible staging transports.

    ``multipart_etag`` is intentionally a presentation-only diagnostic; callers
    must pass staged bytes through :class:`UploadVerifier` for full-file SHA.
    """

    def __init__(self) -> None:
        self._objects: Dict[str, bytes] = {}
        self._etags: Dict[str, str] = {}

    def write_stream(self, session_id: str, chunks: Iterable[bytes]) -> str:
        key = LocalStagingBlobStore.key_for(session_id)
        data = b"".join(chunks)
        if key in self._objects:
            raise StateTransitionError("staged object is already immutable")
        self._objects[key] = data
        self._etags[key] = hashlib.md5(data).hexdigest() + "-2"  # nosec B303: transport simulation only
        return key

    def multipart_etag(self, staging_key: str) -> str:
        return self._etags[staging_key]

    def open_reader(self, staging_key: str) -> BinaryIO:
        self._validate_key(staging_key)
        return io.BytesIO(self._objects[staging_key])

    def delete(self, staging_key: str) -> None:
        self._validate_key(staging_key)
        self._objects.pop(staging_key, None)
        self._etags.pop(staging_key, None)

    def exists(self, staging_key: str) -> bool:
        self._validate_key(staging_key)
        return staging_key in self._objects

    @staticmethod
    def _validate_key(staging_key: str) -> None:
        if not isinstance(staging_key, str):
            raise ValueError("invalid staging key")
        parts = staging_key.split("/")
        if (
            len(parts) != 2
            or parts[0] != "staging"
            or staging_key != LocalStagingBlobStore.key_for(parts[1])
        ):
            raise ValueError("invalid staging key")


class LocalStagingBlobStore:
    """Safe, session-keyed local staging store with no symlink following."""

    def __init__(self, root: Path) -> None:
        self._root = Path(root)
        self._root.mkdir(parents=True, exist_ok=True)
        self._fd = os.open(str(self._root), os.O_RDONLY | os.O_DIRECTORY)

    def close(self) -> None:
        if self._fd >= 0:
            os.close(self._fd)
            self._fd = -1

    def __del__(self) -> None:
        self.close()

    @staticmethod
    def key_for(session_id: str) -> str:
        return "staging/" + str(UUID(session_id))

    def _name(self, key: str) -> str:
        if not isinstance(key, str) or "\\" in key or "\x00" in key or key.startswith("/"):
            raise ValueError("invalid staging key")
        parts = key.split("/")
        if len(parts) != 2 or parts[0] != "staging" or parts[1] in ("", ".", ".."):
            raise ValueError("invalid staging key")
        if key != self.key_for(parts[1]):
            raise ValueError("invalid staging key")
        return parts[1]

    def _dir(self, create: bool) -> int:
        if create:
            try:
                os.mkdir("staging", mode=0o700, dir_fd=self._fd)
            except FileExistsError:
                pass
        return os.open("staging", os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=self._fd)

    def write_stream(self, session_id: str, chunks: Iterable[bytes]) -> str:
        key, name = self.key_for(session_id), str(UUID(session_id))
        directory = self._dir(True)
        temporary = ".upload-" + uuid4().hex
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=directory,
            )
            with os.fdopen(fd, "wb") as stream:
                for chunk in chunks:
                    if not isinstance(chunk, bytes):
                        raise TypeError("upload chunks must be bytes")
                    stream.write(chunk)
                stream.flush()
                os.fsync(stream.fileno())
            try:
                os.link(
                    temporary,
                    name,
                    src_dir_fd=directory,
                    dst_dir_fd=directory,
                    follow_symlinks=False,
                )
            except FileExistsError as error:
                raise StateTransitionError("staged object is already immutable") from error
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
        return key

    def open_reader(self, staging_key: str) -> BinaryIO:
        directory = self._dir(False)
        try:
            fd = os.open(self._name(staging_key), os.O_RDONLY | os.O_NOFOLLOW, dir_fd=directory)
        finally:
            os.close(directory)
        info = os.fstat(fd)
        if not stat_module.S_ISREG(info.st_mode):
            os.close(fd)
            raise RuntimeError("staged object is not a regular file")
        return os.fdopen(fd, "rb")

    def delete(self, staging_key: str) -> None:
        directory = self._dir(False)
        try:
            try:
                os.unlink(self._name(staging_key), dir_fd=directory)
            except FileNotFoundError:
                return
            os.fsync(directory)
        finally:
            os.close(directory)

    def exists(self, staging_key: str) -> bool:
        try:
            with self.open_reader(staging_key):
                return True
        except FileNotFoundError:
            return False


def _sha256(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ValueError("sha256 must be lowercase 64-character hexadecimal")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("sha256 must be lowercase 64-character hexadecimal") from error


class UploadRepository(Protocol):
    def create_session(self, session: UploadSession) -> UploadSession: ...
    def get_session(self, session_id: str) -> UploadSession: ...
    def mark_uploaded(self, session_id: str, expected_version: int) -> UploadSession: ...
    def claim_verification(self, session_id: str, expected_version: int) -> UploadSession: ...
    def reject(self, session_id: str, expected_version: int, reason: str) -> UploadSession: ...
    def finalize_verified(
        self, session_id: str, expected_version: int, blob: BlobRecord, asset: AssetRecord
    ) -> UploadSession: ...
    def expire_before(self, now: datetime) -> Tuple[UploadSession, ...]: ...
    def terminal_staging_candidates(self) -> Tuple[UploadSession, ...]: ...


class InMemoryUploadRepository:
    """CPU contract double; PostgreSQL is still the production authority."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._sessions: Dict[str, UploadSession] = {}
        self._blobs: Dict[str, BlobRecord] = {}

    def create_session(self, session: UploadSession) -> UploadSession:
        with self._lock:
            if session.state != UploadState.INITIATED or session.version != 0:
                raise StateTransitionError("upload session must begin initiated at version zero")
            if session.session_id in self._sessions:
                raise DurableInvariantError("upload session already exists")
            self._sessions[session.session_id] = session
            return session

    def get_session(self, session_id: str) -> UploadSession:
        with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError as error:
                raise NotFoundError("upload session not found: {0}".format(session_id)) from error

    def mark_uploaded(self, session_id: str, expected_version: int) -> UploadSession:
        return self._transition(
            session_id, expected_version, UploadState.INITIATED, UploadState.UPLOADED
        )

    def claim_verification(self, session_id: str, expected_version: int) -> UploadSession:
        return self._transition(
            session_id, expected_version, UploadState.UPLOADED, UploadState.VERIFYING
        )

    def reject(self, session_id: str, expected_version: int, reason: str) -> UploadSession:
        with self._lock:
            current = self.get_session(session_id)
            if (
                current.version != expected_version
                or current.state != UploadState.VERIFYING
                or datetime.now(timezone.utc) >= current.expires_at
            ):
                raise StateTransitionError("upload session CAS rejected")
            updated = replace(
                current,
                state=UploadState.REJECTED,
                version=current.version + 1,
                rejection_reason=reason,
            )
            self._sessions[session_id] = updated
            return updated

    def finalize_verified(
        self, session_id: str, expected_version: int, blob: BlobRecord, asset: AssetRecord
    ) -> UploadSession:
        with self._lock:
            current = self.get_session(session_id)
            if (
                current.version != expected_version
                or current.state != UploadState.VERIFYING
                or datetime.now(timezone.utc) >= current.expires_at
            ):
                raise StateTransitionError("upload session CAS rejected")
            if asset.namespace_id != current.namespace_id or asset.blob_id != blob.blob_id:
                raise DurableInvariantError("verified asset does not match upload session")
            canonical = self._blobs.get(blob.sha256)
            if canonical is None:
                self._blobs[blob.sha256] = blob
                canonical = blob
            elif (
                canonical.size_bytes,
                canonical.storage_key,
                canonical.state,
                canonical.content_type,
            ) != (
                blob.size_bytes,
                blob.storage_key,
                blob.state,
                blob.content_type,
            ):
                raise DurableInvariantError("canonical blob digest has different metadata")
            updated = replace(
                current,
                state=UploadState.VERIFIED,
                version=current.version + 1,
                verified_blob_id=canonical.blob_id,
                verified_asset_id=asset.asset_id,
            )
            self._sessions[session_id] = updated
            return updated

    def expire_before(self, now: datetime) -> Tuple[UploadSession, ...]:
        with self._lock:
            expired = []
            for key, session in tuple(self._sessions.items()):
                if (
                    session.state
                    in (UploadState.INITIATED, UploadState.UPLOADED, UploadState.VERIFYING)
                    and session.expires_at < now
                ):
                    updated = replace(
                        session, state=UploadState.EXPIRED, version=session.version + 1
                    )
                    self._sessions[key] = updated
                    expired.append(updated)
            return tuple(expired)

    def terminal_staging_candidates(self) -> Tuple[UploadSession, ...]:
        with self._lock:
            return tuple(
                session
                for session in self._sessions.values()
                if session.state
                in (UploadState.VERIFIED, UploadState.REJECTED, UploadState.EXPIRED)
            )

    def _transition(
        self, session_id: str, version: int, before: UploadState, after: UploadState
    ) -> UploadSession:
        with self._lock:
            current = self.get_session(session_id)
            if (
                current.version != version
                or current.state != before
                or datetime.now(timezone.utc) >= current.expires_at
            ):
                raise StateTransitionError("upload session CAS rejected")
            updated = replace(current, state=after, version=current.version + 1)
            self._sessions[session_id] = updated
            return updated


class UploadVerifier:
    """Streams staged bytes, validates media, then promotes immutable canonical content.

    Promotion and the PostgreSQL finalization transaction cannot be globally
    atomic. If DB finalization fails after promotion, the canonical object is a
    safe deduplicated orphan candidate; a later canonical-orphan policy owns it.
    This 3B janitor deletes staging only, never canonical objects.
    """

    def __init__(
        self,
        repository: UploadRepository,
        staging: StagingBlobStore,
        canonical: CanonicalBlobStore,
        probe: MediaProbe,
        max_upload_bytes: int = 8 * 1024 * 1024 * 1024,
    ) -> None:
        self._repository, self._staging, self._canonical, self._probe = (
            repository,
            staging,
            canonical,
            probe,
        )
        if (
            isinstance(max_upload_bytes, bool)
            or not isinstance(max_upload_bytes, int)
            or max_upload_bytes < 1
        ):
            raise ValueError("max_upload_bytes must be a positive integer")
        self._max_upload_bytes = max_upload_bytes

    def finalize(self, session_id: str, expected_version: int) -> UploadSession:
        session = self._repository.get_session(session_id)
        if session.version != expected_version:
            raise StateTransitionError("upload session is not the requested version")
        if session.state == UploadState.UPLOADED:
            session = self._repository.claim_verification(session_id, expected_version)
            expected_version = session.version
        elif session.state != UploadState.VERIFYING:
            raise StateTransitionError("upload session is not available for verification")
        if session.expected_size_bytes > self._max_upload_bytes:
            rejected = self._repository.reject(session_id, expected_version, "size_limit_exceeded")
            self._staging.delete(session.staging_key)
            return rejected
        digest = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            with self._staging.open_reader(session.staging_key) as source:
                while True:
                    chunk = source.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    size += len(chunk)
                    if size > session.expected_size_bytes or size > self._max_upload_bytes:
                        rejected = self._repository.reject(
                            session_id, expected_version, "size_limit_exceeded"
                        )
                        self._staging.delete(session.staging_key)
                        return rejected
                    snapshot.write(chunk)
            actual = digest.hexdigest()
            if size != session.expected_size_bytes or (
                session.expected_sha256 and actual != session.expected_sha256
            ):
                rejected = self._repository.reject(
                    session_id, expected_version, "size_or_sha256_mismatch"
                )
                self._staging.delete(session.staging_key)
                return rejected
            try:
                snapshot.seek(0)
                media_info = self._probe.probe(snapshot)
            except (OSError, ValueError, subprocess.TimeoutExpired, RuntimeError):
                rejected = self._repository.reject(
                    session_id, expected_version, "media_probe_failed"
                )
                self._staging.delete(session.staging_key)
                return rejected
            snapshot.seek(0)
            object_ = self._canonical.put_stream(iter(lambda: snapshot.read(1024 * 1024), b""))
        blob = BlobRecord(
            str(uuid4()),
            object_.sha256,
            object_.size_bytes,
            object_.storage_key,
            BlobState.VERIFIED,
            media_info.media_type,
        )
        asset = AssetRecord(
            str(uuid4()),
            blob.blob_id,
            session.namespace_id,
            AssetKind.INPUT,
            AssetState.VERIFIED,
            {"media_type": media_info.media_type, "duration_seconds": media_info.duration_seconds},
        )
        verified = self._repository.finalize_verified(session_id, expected_version, blob, asset)
        self._staging.delete(session.staging_key)
        return verified

    def janitor(self, now: Optional[datetime] = None) -> int:
        count = 0
        self._repository.expire_before(now or datetime.now(timezone.utc))
        for session in self._repository.terminal_staging_candidates():
            if self._staging.exists(session.staging_key):
                self._staging.delete(session.staging_key)
                count += 1
        return count


@dataclass(frozen=True)
class UploadCliState:
    session_id: str
    staging_key: str
    version: int

    def __post_init__(self) -> None:
        UUID(self.session_id)
        if self.staging_key != "staging/" + str(UUID(self.session_id)):
            raise ValueError("staging_key must bind session_id")
        if isinstance(self.version, bool) or not isinstance(self.version, int) or self.version < 0:
            raise ValueError("version must be non-negative")

    def to_json(self) -> str:
        return json.dumps(
            {
                "session_id": self.session_id,
                "staging_key": self.staging_key,
                "version": self.version,
            },
            sort_keys=True,
        )

    @classmethod
    def from_json(cls, payload: str) -> "UploadCliState":
        value = json.loads(payload)
        if not isinstance(value, dict) or set(value) != {"session_id", "staging_key", "version"}:
            raise ValueError("CLI state must have exact keys")
        return cls(value["session_id"], value["staging_key"], value["version"])

    def save(self, path: Path) -> None:
        """Atomically persist non-secret local resume state with owner-only mode."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".tmp-" + uuid4().hex)
        try:
            fd = os.open(str(temporary), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as sink:
                sink.write(self.to_json())
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temporary, path)
            directory_fd = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    @classmethod
    def load(cls, path: Path) -> "UploadCliState":
        info = os.lstat(path)
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISREG(info.st_mode):
            raise ValueError("CLI state must be a regular file")
        if info.st_mode & 0o077:
            raise ValueError("CLI state permissions must be owner-only")
        return cls.from_json(Path(path).read_text(encoding="utf-8"))
