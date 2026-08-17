# pyright: reportMissingImports=false
"""CPU-only media validation tests for Phase 3E publication."""

from __future__ import annotations

import hashlib
import io
import wave
from typing import BinaryIO

import pytest

from nano_aural_runtime.durable.artifact_validation import (
    ArtifactValidationError,
    ArtifactValidationPhase,
    ArtifactValidationSpec,
    StreamingMediaArtifactValidator,
)
from nano_aural_runtime.durable.uploads import MediaInfo, WaveMediaProbe


def _wav(frames: int = 400) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


def test_streams_full_sha_size_and_media_metadata() -> None:
    content = _wav()
    result = StreamingMediaArtifactValidator(WaveMediaProbe()).validate(
        io.BytesIO(content),
        ArtifactValidationSpec(
            "audio/wav",
            len(content) + 1,
            hashlib.sha256(content).hexdigest(),
            len(content),
        ),
    )
    assert result.sha256 == hashlib.sha256(content).hexdigest()
    assert result.size_bytes == len(content)
    assert result.content_type == "audio/wav"
    assert result.duration_seconds == pytest.approx(0.05)


@pytest.mark.parametrize(
    "spec",
    (
        ArtifactValidationSpec("audio/wav", 32),
        ArtifactValidationSpec("audio/wav", 4096, expected_size_bytes=1),
        ArtifactValidationSpec("audio/wav", 4096, expected_sha256="0" * 64),
    ),
)
def test_rejects_limit_size_and_sha_mismatch_immediately(
    spec: ArtifactValidationSpec,
) -> None:
    with pytest.raises(ArtifactValidationError):
        StreamingMediaArtifactValidator(WaveMediaProbe()).validate(io.BytesIO(_wav()), spec)


def test_rejects_truncated_media_and_content_type_mismatch() -> None:
    content = _wav()
    validator = StreamingMediaArtifactValidator(WaveMediaProbe())
    with pytest.raises(ArtifactValidationError):
        validator.validate(
            io.BytesIO(content[:44]), ArtifactValidationSpec("audio/wav", len(content))
        )

    class VideoProbe:
        def probe(self, stream: BinaryIO) -> MediaInfo:
            return MediaInfo("video/mp4", 1.0)

    with pytest.raises(ArtifactValidationError):
        StreamingMediaArtifactValidator(VideoProbe()).validate(
            io.BytesIO(content), ArtifactValidationSpec("audio/wav", len(content) + 1)
        )


def test_video_probe_is_supported_without_loading_object_into_validator_memory() -> None:
    class ConsumingVideoProbe:
        def probe(self, stream: BinaryIO) -> MediaInfo:
            assert stream.read(16) == b"server-video-byt"
            return MediaInfo("video/mp4", 2.5)

    content = b"server-video-bytes" * 4096
    result = StreamingMediaArtifactValidator(ConsumingVideoProbe()).validate(
        io.BytesIO(content), ArtifactValidationSpec("video/mp4", len(content))
    )
    assert result.content_type == "video/mp4"
    assert result.sha256 == hashlib.sha256(content).hexdigest()


def test_empty_or_nonbytes_stream_is_rejected() -> None:
    validator = StreamingMediaArtifactValidator(WaveMediaProbe())
    with pytest.raises(ArtifactValidationError):
        validator.validate(io.BytesIO(), ArtifactValidationSpec("audio/wav", 10))

    class InvalidStream:
        def read(self, _size: int) -> str:
            return "not-bytes"

    with pytest.raises(ArtifactValidationError):
        validator.validate(InvalidStream(), ArtifactValidationSpec("audio/wav", 10))  # type: ignore[arg-type]


def test_progress_checkpoints_stream_and_probe_boundaries() -> None:
    phases: list[ArtifactValidationPhase] = []

    def progress(phase: ArtifactValidationPhase, bytes_processed: int) -> None:
        assert bytes_processed > 0
        phases.append(phase)

    content = _wav()
    StreamingMediaArtifactValidator(WaveMediaProbe()).validate(
        io.BytesIO(content),
        ArtifactValidationSpec("audio/wav", len(content)),
        progress,
    )
    assert phases[0] is ArtifactValidationPhase.STREAM
    assert phases[-2:] == [
        ArtifactValidationPhase.BEFORE_PROBE,
        ArtifactValidationPhase.AFTER_PROBE,
    ]
