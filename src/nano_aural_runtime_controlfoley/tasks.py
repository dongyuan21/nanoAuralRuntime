"""Adapter-owned, immutable ControlFoley local task schemas."""

from __future__ import annotations

import math
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from nano_aural_runtime import ModelInvocation

UPSTREAM_PARITY_OPERATION = "upstream_parity"
EXPERIMENTAL_STAGED_OPERATION = "experimental_staged_v1"
_OPERATIONS = frozenset((UPSTREAM_PARITY_OPERATION, EXPERIMENTAL_STAGED_OPERATION))


class ControlFoleyTaskKind(str, Enum):
    V2A = "V2A"
    TV2A = "TV2A"
    TC_V2A = "TC-V2A"
    AC_V2A = "AC-V2A"
    T2A = "T2A"


@dataclass(frozen=True)
class ControlFoleyLocalRequest:
    """Local-executor request; paths never belong to a remote service contract."""

    task: ControlFoleyTaskKind
    video_path: Optional[Path] = None
    reference_audio_path: Optional[Path] = None
    prompt: Optional[str] = None
    duration_seconds: float = 8.0
    num_steps: int = 25
    guidance_scale: float = 4.5
    seed: int = 42

    def __post_init__(self) -> None:
        if not isinstance(self.task, ControlFoleyTaskKind):
            raise TypeError("task must be a ControlFoleyTaskKind")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(self.duration_seconds)
            or float(self.duration_seconds) <= 0
            or isinstance(self.num_steps, bool)
            or not isinstance(self.num_steps, int)
            or self.num_steps != 25
            or isinstance(self.guidance_scale, bool)
            or not isinstance(self.guidance_scale, (int, float))
            or not math.isfinite(self.guidance_scale)
            or float(self.guidance_scale) != 4.5
        ):
            raise ValueError(
                "duration must be finite/positive; steps and guidance use locked defaults"
            )
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        expected = {
            ControlFoleyTaskKind.V2A: (True, False, False),
            ControlFoleyTaskKind.TV2A: (True, False, True),
            ControlFoleyTaskKind.TC_V2A: (True, False, True),
            ControlFoleyTaskKind.AC_V2A: (True, True, False),
            ControlFoleyTaskKind.T2A: (False, False, True),
        }[self.task]
        actual = (
            self.video_path is not None,
            self.reference_audio_path is not None,
            self.prompt is not None,
        )
        if actual != expected:
            raise ValueError("task inputs do not match the exact ControlFoley task contract")
        for path in (self.video_path, self.reference_audio_path):
            if path is not None and (not isinstance(path, Path) or not path.is_file()):
                raise ValueError("local task input must be an existing regular file")
        if self.prompt is not None and (
            not isinstance(self.prompt, str) or not self.prompt.strip()
        ):
            raise ValueError("prompt must be non-empty")

    def to_invocation(
        self, invocation_id: str, operation: str = UPSTREAM_PARITY_OPERATION
    ) -> ModelInvocation:
        if operation not in _OPERATIONS:
            raise ValueError("unsupported ControlFoley operation")
        return ModelInvocation(
            invocation_id=invocation_id,
            operation=operation,
            inputs={
                "task": self.task.value,
                "video_path": None if self.video_path is None else str(self.video_path.resolve()),
                "reference_audio_path": (
                    None
                    if self.reference_audio_path is None
                    else str(self.reference_audio_path.resolve())
                ),
                "prompt": self.prompt,
                "duration_seconds": self.duration_seconds,
                "num_steps": self.num_steps,
                "guidance_scale": self.guidance_scale,
                "seed": self.seed,
            },
        )

    @classmethod
    def from_invocation(cls, invocation: ModelInvocation) -> "ControlFoleyLocalRequest":
        if invocation.operation not in _OPERATIONS:
            raise ValueError("unsupported ControlFoley operation")
        expected = {
            "task",
            "video_path",
            "reference_audio_path",
            "prompt",
            "duration_seconds",
            "num_steps",
            "guidance_scale",
            "seed",
        }
        if set(invocation.inputs) != expected:
            raise ValueError("ControlFoley invocation has missing or unexpected fields")
        values: Mapping[str, Any] = invocation.inputs
        return cls(
            task=ControlFoleyTaskKind(values["task"]),
            video_path=None if values["video_path"] is None else Path(str(values["video_path"])),
            reference_audio_path=(
                None
                if values["reference_audio_path"] is None
                else Path(str(values["reference_audio_path"]))
            ),
            prompt=values["prompt"],
            duration_seconds=values["duration_seconds"],
            num_steps=values["num_steps"],
            guidance_scale=values["guidance_scale"],
            seed=values["seed"],
        )
