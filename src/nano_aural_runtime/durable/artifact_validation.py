"""Bounded streaming validators for immutable Phase 3E attempt objects."""

from __future__ import annotations

import hashlib
import math
import tempfile
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import BinaryIO, Mapping, Optional, Protocol

from .uploads import MediaProbe

_BUFFER_SIZE = 1024 * 1024
_MEDIA_TYPES = frozenset(("audio/flac", "audio/mpeg", "audio/wav", "video/mp4"))


class ArtifactValidationError(ValueError):
    """Attempt bytes cannot become a durable visible artifact."""


class ArtifactValidationPhase(str, Enum):
    STREAM = "stream"
    BEFORE_PROBE = "before_probe"
    AFTER_PROBE = "after_probe"


class ArtifactValidationProgress(Protocol):
    def __call__(self, phase: ArtifactValidationPhase, bytes_processed: int) -> None: ...


@dataclass(frozen=True)
class ArtifactValidationSpec:
    content_type: str
    max_size_bytes: int
    expected_sha256: Optional[str] = None
    expected_size_bytes: Optional[int] = None

    def __post_init__(self) -> None:
        if self.content_type not in _MEDIA_TYPES:
            raise ValueError("content_type is not an allowed audio/video type")
        if (
            isinstance(self.max_size_bytes, bool)
            or not isinstance(self.max_size_bytes, int)
            or self.max_size_bytes < 1
        ):
            raise ValueError("max_size_bytes must be a positive integer")
        _optional_size(self.expected_size_bytes, "expected_size_bytes")
        if self.expected_size_bytes is not None and self.expected_size_bytes > self.max_size_bytes:
            raise ValueError("expected_size_bytes exceeds the validator limit")
        if self.expected_sha256 is not None:
            _sha256(self.expected_sha256)


@dataclass(frozen=True)
class ValidatedArtifact:
    sha256: str
    size_bytes: int
    content_type: str
    duration_seconds: float
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _sha256(self.sha256)
        if isinstance(self.size_bytes, bool) or not isinstance(self.size_bytes, int):
            raise TypeError("size_bytes must be an integer")
        if self.size_bytes < 1:
            raise ValueError("validated artifact must not be empty")
        if self.content_type not in _MEDIA_TYPES:
            raise ValueError("content_type is not an allowed audio/video type")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or self.duration_seconds <= 0
        ):
            raise ValueError("duration_seconds must be positive")
        object.__setattr__(self, "metadata", MappingProxyType(dict(self.metadata)))


class ArtifactValidator(Protocol):
    def validate(
        self,
        stream: BinaryIO,
        spec: ArtifactValidationSpec,
        progress: Optional[ArtifactValidationProgress] = None,
    ) -> ValidatedArtifact: ...


class StreamingMediaArtifactValidator:
    """Hash, bound, snapshot and media-probe an audio/video object.

    The source is consumed once in bounded chunks.  The temporary snapshot is
    server-controlled and is rewound only for the injected safe media probe.
    """

    def __init__(self, probe: MediaProbe) -> None:
        self._probe = probe

    def validate(
        self,
        stream: BinaryIO,
        spec: ArtifactValidationSpec,
        progress: Optional[ArtifactValidationProgress] = None,
    ) -> ValidatedArtifact:
        if not isinstance(spec, ArtifactValidationSpec):
            raise TypeError("spec must be ArtifactValidationSpec")
        digest = hashlib.sha256()
        size = 0
        with tempfile.TemporaryFile(mode="w+b") as snapshot:
            while True:
                chunk = stream.read(_BUFFER_SIZE)
                if not chunk:
                    break
                if not isinstance(chunk, bytes):
                    raise ArtifactValidationError("artifact stream returned non-bytes")
                size += len(chunk)
                if size > spec.max_size_bytes:
                    raise ArtifactValidationError("artifact exceeds configured size limit")
                if spec.expected_size_bytes is not None and size > spec.expected_size_bytes:
                    raise ArtifactValidationError("artifact size differs from expected evidence")
                digest.update(chunk)
                snapshot.write(chunk)
                if progress is not None:
                    progress(ArtifactValidationPhase.STREAM, size)
            actual_sha256 = digest.hexdigest()
            if size < 1:
                raise ArtifactValidationError("artifact is empty")
            if spec.expected_size_bytes is not None and size != spec.expected_size_bytes:
                raise ArtifactValidationError("artifact size differs from expected evidence")
            if spec.expected_sha256 is not None and actual_sha256 != spec.expected_sha256:
                raise ArtifactValidationError("artifact SHA-256 differs from expected evidence")
            snapshot.seek(0)
            if progress is not None:
                progress(ArtifactValidationPhase.BEFORE_PROBE, size)
            try:
                media = self._probe.probe(snapshot)
            except (OSError, RuntimeError, ValueError) as error:
                raise ArtifactValidationError("artifact media validation failed") from error
            if progress is not None:
                progress(ArtifactValidationPhase.AFTER_PROBE, size)
            if media.media_type != spec.content_type:
                raise ArtifactValidationError("artifact content type differs from probed media")
        return ValidatedArtifact(
            actual_sha256,
            size,
            media.media_type,
            media.duration_seconds,
            {"media_type": media.media_type, "duration_seconds": media.duration_seconds},
        )


def _sha256(value: str) -> None:
    if not isinstance(value, str) or len(value) != 64 or value != value.lower():
        raise ValueError("sha256 must be lowercase 64-character hexadecimal")
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("sha256 must be lowercase 64-character hexadecimal") from error


def _optional_size(value: Optional[int], name: str) -> None:
    if value is not None and (isinstance(value, bool) or not isinstance(value, int) or value < 0):
        raise ValueError("{0} must be a non-negative integer".format(name))
