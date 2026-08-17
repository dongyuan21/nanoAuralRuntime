"""Adapter-owned Stable Audio 3 Small-SFX local task schema."""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Mapping

from nano_aural_runtime import ModelInvocation

from .baseline import OPERATION, SUPPORTED_DURATIONS

_INPUT_KEYS = ("prompt", "duration_seconds", "seed")


@dataclass(frozen=True)
class StableAudio3LocalRequest:
    prompt: str
    duration_seconds: float
    seed: int = 42

    def __post_init__(self) -> None:
        if not isinstance(self.prompt, str) or not self.prompt.strip():
            raise ValueError("prompt must be a non-empty string")
        if (
            isinstance(self.duration_seconds, bool)
            or not isinstance(self.duration_seconds, (int, float))
            or not math.isfinite(float(self.duration_seconds))
            or float(self.duration_seconds) not in SUPPORTED_DURATIONS
        ):
            raise ValueError("duration_seconds must be 5, 30, or 120")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        object.__setattr__(self, "duration_seconds", float(self.duration_seconds))

    def to_invocation(self, invocation_id: str) -> ModelInvocation:
        return ModelInvocation(
            invocation_id=invocation_id,
            operation=OPERATION,
            inputs={
                "prompt": self.prompt,
                "duration_seconds": self.duration_seconds,
                "seed": self.seed,
            },
        )

    @classmethod
    def from_invocation(cls, invocation: ModelInvocation) -> "StableAudio3LocalRequest":
        if invocation.operation != OPERATION:
            raise ValueError("unsupported Stable Audio 3 operation")
        if set(invocation.inputs) != set(_INPUT_KEYS):
            raise ValueError("Stable Audio 3 invocation has missing or unexpected fields")
        values: Mapping[str, Any] = invocation.inputs
        return cls(
            prompt=str(values["prompt"]),
            duration_seconds=float(values["duration_seconds"]),
            seed=int(values["seed"]),
        )
