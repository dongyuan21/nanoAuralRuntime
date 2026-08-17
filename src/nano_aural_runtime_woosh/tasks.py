"""Adapter-owned Woosh V2A local task schema."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from nano_aural_runtime import ModelInvocation

from .baseline import OPERATION

_REQUIRED_KEYS = frozenset(("video_path", "seed"))
_OPTIONAL_KEYS = frozenset(("prompt",))
_FORBIDDEN_KEYS = frozenset(
    (
        "duration_seconds",
        "cfg",
        "guidance_scale",
        "solver",
        "solver_method",
        "renoise",
        "num_steps",
        "backend",
        "backend_id",
        "source_dir",
        "weights_dir",
        "checkpoint_path",
        "synchformer_path",
    )
)


@dataclass(frozen=True)
class WooshV2ALocalRequest:
    video_path: Path
    seed: int = 42
    prompt: Optional[str] = None

    def __post_init__(self) -> None:
        if not isinstance(self.video_path, Path) or not self.video_path.is_file():
            raise ValueError("video_path must be an existing regular file")
        if not isinstance(self.seed, int) or isinstance(self.seed, bool):
            raise TypeError("seed must be an integer")
        if self.prompt is not None and (
            not isinstance(self.prompt, str) or not self.prompt.strip()
        ):
            raise ValueError("prompt must be a non-empty string when provided")

    def to_invocation(self, invocation_id: str) -> ModelInvocation:
        inputs = {
            "video_path": str(self.video_path.resolve()),
            "seed": self.seed,
        }
        if self.prompt is not None:
            inputs["prompt"] = self.prompt
        return ModelInvocation(
            invocation_id=invocation_id,
            operation=OPERATION,
            inputs=inputs,
        )

    @classmethod
    def from_invocation(cls, invocation: ModelInvocation) -> "WooshV2ALocalRequest":
        if invocation.operation != OPERATION:
            raise ValueError("unsupported Woosh V2A operation")
        keys = set(invocation.inputs)
        if keys & _FORBIDDEN_KEYS:
            raise ValueError("Woosh V2A invocation contains operator-owned fields")
        if not _REQUIRED_KEYS.issubset(keys) or not keys.issubset(_REQUIRED_KEYS | _OPTIONAL_KEYS):
            raise ValueError("Woosh V2A invocation has missing or unexpected fields")
        values: Mapping[str, Any] = invocation.inputs
        prompt = values.get("prompt")
        return cls(
            video_path=Path(str(values["video_path"])),
            seed=int(values["seed"]),
            prompt=None if prompt is None else str(prompt),
        )
