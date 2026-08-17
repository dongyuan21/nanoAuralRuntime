"""Adapter-owned 8-second video window policy. No torch and no mux."""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from .baseline import WINDOW_END_SECONDS, WINDOW_START_SECONDS


class VideoWindowError(ValueError):
    """The local video cannot legally occupy the V1 8-second window."""


class VideoDurationProbe(Protocol):
    def duration_seconds(self, path: Path) -> float: ...


class OfficialPyAVVideoProbe:
    """Probe duration with PyAV when the operator environment provides it."""

    def duration_seconds(self, path: Path) -> float:
        if not path.is_file():
            raise VideoWindowError("video file is missing")
        try:
            import av  # pyright: ignore[reportMissingImports]
        except ImportError as error:
            raise VideoWindowError("PyAV is unavailable for video probing") from error
        container = av.open(str(path))
        try:
            duration = container.duration
            if duration is None:
                raise VideoWindowError("video duration is unknown")
            return float(duration) / 1_000_000.0
        finally:
            container.close()


def require_explicit_eight_second_window(duration_seconds: float) -> None:
    if not isinstance(duration_seconds, (int, float)) or isinstance(duration_seconds, bool):
        raise VideoWindowError("video duration must be a number")
    duration = float(duration_seconds)
    if duration < WINDOW_END_SECONDS:
        raise VideoWindowError("video shorter than 8 seconds is not supported")
    # Longer videos are legal; V1 uses [0, 8) and does not segment or pad.


def window_contract() -> dict[str, float]:
    return {
        "window_start_seconds": WINDOW_START_SECONDS,
        "window_end_seconds": WINDOW_END_SECONDS,
    }
