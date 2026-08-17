"""Fenced, server-side materialization of verified durable job inputs.

This module intentionally accepts durable records and a canonical BlobStore,
never request paths, filenames, staging keys, multipart metadata, or ETags.
It is the Phase 3C boundary between a claimed lease and an attempt-local
workspace; it neither changes queue state nor publishes an artifact.
"""

from __future__ import annotations

import hashlib
import math
import os
import stat as stat_module
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, Sequence, Tuple

from .domain import (
    AssetRecord,
    AssetState,
    AttemptRecord,
    AttemptState,
    BlobRecord,
    BlobState,
    JobRecord,
    JobState,
)
from .errors import DurableError
from .storage import LocalBlobStore
from .upload_transports import CanonicalBlobStore
from .uploads import MediaInfo, MediaProbe


class MaterializationError(DurableError):
    """A verified input could not safely be materialized for an attempt."""


class EvidenceDriftError(MaterializationError):
    """The supplied durable records do not prove a legal current input."""


class MaterializationIntegrityError(MaterializationError):
    """Canonical bytes no longer match their durable SHA-256/size evidence."""


class MaterializationMediaError(MaterializationError):
    """A media probe disagrees with the previously verified media evidence."""


class MaterializationLeaseLostError(MaterializationError):
    """The caller is no longer the current fenced executor for the attempt."""


class WorkspaceCleanupError(MaterializationError):
    """Attempt workspace cleanup could not be completed safely."""


class AttemptLease(Protocol):
    """Structural view of the Phase 3C queue lease; no queue import needed."""

    @property
    def job_id(self) -> str: ...

    @property
    def attempt_id(self) -> str: ...

    @property
    def worker_id(self) -> str: ...

    @property
    def lease_epoch(self) -> int: ...


class LeaseAuthority(Protocol):
    """Queue-facing fence check supplied by the Phase 3C lease repository."""

    def assert_current(self, lease: AttemptLease) -> None: ...


@dataclass(frozen=True)
class VerifiedInputEvidence:
    """One DB-read job input plus its verified asset and canonical blob facts."""

    role: str
    asset: AssetRecord
    blob: BlobRecord


@dataclass(frozen=True)
class MaterializedInput:
    """Immutable, attempt-local result with a server-generated local path."""

    role: str
    asset_id: str
    blob_id: str
    sha256: str
    size_bytes: int
    path: Path
    media: MediaInfo | None = None


class AttemptInputMaterializer:
    """Context manager that writes only verified canonical bytes into one workspace.

    The caller obtains all records from the durable database in its claim flow
    and supplies a queue authority that fences the lease immediately before and
    immediately after I/O.  This class never accepts a client path and does not
    mutate queue, job, attempt, asset, blob, or artifact state.
    """

    def __init__(
        self,
        job: JobRecord,
        attempt: AttemptRecord,
        lease: AttemptLease,
        evidence: Sequence[VerifiedInputEvidence],
        canonical: CanonicalBlobStore,
        lease_authority: LeaseAuthority,
        workspace_root: Path,
        probe: MediaProbe,
        chunk_size: int = 1024 * 1024,
    ) -> None:
        if not isinstance(chunk_size, int) or isinstance(chunk_size, bool) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        if probe is None:
            raise ValueError("a media probe is required for every durable input")
        self._job = job
        self._attempt = attempt
        self._lease = lease
        self._evidence = tuple(evidence)
        self._canonical = canonical
        self._lease_authority = lease_authority
        self._root = Path(workspace_root).resolve()
        self._probe = probe
        self._chunk_size = chunk_size
        self._workspace: Path | None = None
        self._materialized: Tuple[MaterializedInput, ...] = ()

    @property
    def workspace(self) -> Path:
        if self._workspace is None:
            raise MaterializationError("workspace has not been entered")
        return self._workspace

    @property
    def inputs(self) -> Tuple[MaterializedInput, ...]:
        return self._materialized

    def __enter__(self) -> "AttemptInputMaterializer":
        self._validate_evidence()
        self._assert_lease()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._workspace = Path(tempfile.mkdtemp(prefix="attempt-", dir=self._root))
        try:
            materialized = tuple(
                self._materialize_one(index, item)
                for index, item in enumerate(sorted(self._evidence, key=lambda value: value.role))
            )
            self._assert_lease()
            self._materialized = materialized
            return self
        except BaseException:
            self._cleanup_after_failure()
            raise

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> bool:
        try:
            self._cleanup()
        except WorkspaceCleanupError:
            if exc_type is None:
                raise
        return False

    def _validate_evidence(self) -> None:
        if (
            self._job.state != JobState.RUNNING
            or self._job.current_attempt_id != self._attempt.attempt_id
            or self._attempt.job_id != self._job.job_id
            or self._attempt.state != AttemptState.ACTIVE
            or self._job.lease_epoch != self._attempt.lease_epoch
        ):
            raise EvidenceDriftError("job/attempt evidence is not a current active execution")
        if (
            self._lease.job_id != self._job.job_id
            or self._lease.attempt_id != self._attempt.attempt_id
            or self._lease.worker_id != self._attempt.worker_id
            or self._lease.lease_epoch != self._attempt.lease_epoch
        ):
            raise EvidenceDriftError("lease evidence does not match the active attempt")
        expected = {item.role: item.asset_id for item in self._job.inputs}
        supplied = {item.role: item for item in self._evidence}
        if len(supplied) != len(self._evidence) or set(supplied) != set(expected):
            raise EvidenceDriftError("input evidence must cover each job role exactly once")
        for role, asset_id in expected.items():
            evidence = supplied[role]
            if evidence.asset.asset_id != asset_id:
                raise EvidenceDriftError(
                    "input evidence asset does not match the persisted job input"
                )
            if (
                evidence.asset.namespace_id != self._job.namespace_id
                or evidence.asset.blob_id != evidence.blob.blob_id
                or evidence.asset.state != AssetState.VERIFIED
                or evidence.blob.state != BlobState.VERIFIED
                or evidence.blob.storage_key != LocalBlobStore.canonical_key(evidence.blob.sha256)
            ):
                raise EvidenceDriftError(
                    "input evidence is not a verified canonical asset/blob pair"
                )
            expected_media = self._expected_media(evidence.asset)
            if evidence.blob.content_type.startswith(("audio/", "video/")) and (
                expected_media.media_type != evidence.blob.content_type
            ):
                raise EvidenceDriftError(
                    "asset media evidence conflicts with verified blob content type"
                )

    def _assert_lease(self) -> None:
        try:
            self._lease_authority.assert_current(self._lease)
        except DurableError as error:
            raise MaterializationLeaseLostError("attempt lease is no longer current") from error

    def _materialize_one(self, index: int, evidence: VerifiedInputEvidence) -> MaterializedInput:
        expected_media = self._expected_media(evidence.asset)
        filename = "input-{0:04d}-{1}.bin".format(index, evidence.blob.sha256[:16])
        workspace_fd = self._open_workspace_fd()
        try:
            target_fd = os.open(
                filename,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o600,
                dir_fd=workspace_fd,
            )
        finally:
            os.close(workspace_fd)
        digest = hashlib.sha256()
        size = 0
        try:
            with os.fdopen(target_fd, "wb") as target:
                with self._canonical.open_reader(evidence.blob.storage_key) as source:
                    while True:
                        chunk = source.read(self._chunk_size)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise MaterializationIntegrityError(
                                "canonical reader returned non-bytes"
                            )
                        size += len(chunk)
                        if size > evidence.blob.size_bytes:
                            raise MaterializationIntegrityError(
                                "canonical object exceeds persisted size"
                            )
                        digest.update(chunk)
                        target.write(chunk)
                    target.flush()
                    os.fsync(target.fileno())
        except MaterializationError:
            raise
        except (OSError, ValueError) as error:
            raise MaterializationIntegrityError("cannot safely read canonical input") from error
        if size != evidence.blob.size_bytes or digest.hexdigest() != evidence.blob.sha256:
            raise MaterializationIntegrityError(
                "canonical object differs from persisted SHA-256 evidence"
            )
        path = self.workspace / filename
        media = self._probe_media(path, expected_media)
        return MaterializedInput(
            evidence.role,
            evidence.asset.asset_id,
            evidence.blob.blob_id,
            evidence.blob.sha256,
            size,
            path,
            media,
        )

    def _probe_media(self, path: Path, expected: MediaInfo) -> MediaInfo:
        try:
            with path.open("rb") as stream:
                actual = self._probe.probe(stream)
        except (OSError, ValueError) as error:
            raise MaterializationMediaError("verified media could not be re-probed") from error
        if actual.media_type != expected.media_type or not math.isclose(
            actual.duration_seconds,
            expected.duration_seconds,
            rel_tol=1e-9,
            abs_tol=1e-9,
        ):
            raise MaterializationMediaError(
                "media probe differs from persisted verification metadata"
            )
        return actual

    @staticmethod
    def _expected_media(asset: AssetRecord) -> MediaInfo:
        metadata = asset.metadata
        has_media_type = "media_type" in metadata
        has_duration = "duration_seconds" in metadata
        if not has_media_type or not has_duration:
            raise EvidenceDriftError(
                "every durable input requires complete media verification metadata"
            )
        media_type = metadata["media_type"]
        duration = metadata["duration_seconds"]
        if (
            not isinstance(media_type, str)
            or not media_type
            or isinstance(duration, bool)
            or not isinstance(duration, (int, float))
        ):
            raise EvidenceDriftError("media verification metadata is invalid")
        try:
            return MediaInfo(media_type, float(duration))
        except ValueError as error:
            raise EvidenceDriftError("media verification metadata is invalid") from error

    def _open_workspace_fd(self) -> int:
        workspace = self.workspace
        if workspace.parent != self._root or not workspace.name.startswith("attempt-"):
            raise WorkspaceCleanupError("refusing an unexpected workspace path")
        return os.open(str(workspace), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)

    def _cleanup_after_failure(self) -> None:
        try:
            self._cleanup()
        except WorkspaceCleanupError:
            pass

    def _cleanup(self) -> None:
        if self._workspace is None:
            return
        workspace = self._workspace
        self._workspace = None
        if workspace.parent != self._root or not workspace.name.startswith("attempt-"):
            raise WorkspaceCleanupError("refusing an unexpected workspace cleanup target")
        try:
            root_fd = os.open(str(self._root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                self._unlink_tree(root_fd, workspace.name)
            finally:
                os.close(root_fd)
        except OSError as error:
            raise WorkspaceCleanupError("attempt workspace cleanup failed") from error
        self._materialized = ()

    @classmethod
    def _unlink_tree(cls, parent_fd: int, name: str) -> None:
        info = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        if stat_module.S_ISLNK(info.st_mode) or not stat_module.S_ISDIR(info.st_mode):
            raise WorkspaceCleanupError("workspace is not a real directory")
        directory_fd = os.open(
            name,
            os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
            dir_fd=parent_fd,
        )
        try:
            for child in os.listdir(directory_fd):
                child_info = os.stat(child, dir_fd=directory_fd, follow_symlinks=False)
                if stat_module.S_ISDIR(child_info.st_mode):
                    cls._unlink_tree(directory_fd, child)
                else:
                    os.unlink(child, dir_fd=directory_fd)
        finally:
            os.close(directory_fd)
        os.rmdir(name, dir_fd=parent_fd)
