"""Experimental, default-off Woosh V2A stage profiling. No performance claims."""

from __future__ import annotations

from dataclasses import dataclass, field

from nano_aural_runtime import ProfileReport

from .baseline import BACKEND_DVFLOW, BACKEND_VFLOW, SUPPORTED_BACKENDS

WOOSH_PROFILE_MODE_OFF = "off"
WOOSH_PROFILE_MODE_STAGES = "stages"
SHARED_STAGES = (
    "video_probe",
    "video_decode",
    "frame_resample",
    "synchformer",
    "text_condition",
    "noise_init",
    "woosh_ae_decode",
    "normalize",
    "serialize",
)
SAMPLER_STAGES = {
    BACKEND_DVFLOW: "dvflow_euler",
    BACKEND_VFLOW: "vflow_integrate",
}


class WooshProfileError(ValueError):
    """A profile configuration or stage name is not sealed."""


def stage_order(backend_id: str) -> tuple[str, ...]:
    if backend_id not in SUPPORTED_BACKENDS:
        raise WooshProfileError("V1 supports only dvflow-8s and vflow-8s")
    sampler = SAMPLER_STAGES[backend_id]
    return SHARED_STAGES[:6] + (sampler,) + SHARED_STAGES[6:]


@dataclass
class WooshStageProfiler:
    backend_id: str
    mode: str = WOOSH_PROFILE_MODE_OFF
    _seconds: dict[str, float] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in (WOOSH_PROFILE_MODE_OFF, WOOSH_PROFILE_MODE_STAGES):
            raise WooshProfileError("profile mode must be off or stages")
        self._allowed = frozenset(stage_order(self.backend_id))

    def record(self, stage: str, seconds: float) -> None:
        if self.mode == WOOSH_PROFILE_MODE_OFF:
            return
        if stage not in self._allowed:
            raise WooshProfileError("unknown or forbidden Woosh profile stage")
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)) or seconds < 0:
            raise WooshProfileError("stage seconds must be a non-negative number")
        self._seconds[stage] = float(seconds)

    def report(self) -> ProfileReport:
        if self.mode == WOOSH_PROFILE_MODE_OFF:
            return ProfileReport()
        metrics = {stage: self._seconds.get(stage, 0.0) for stage in stage_order(self.backend_id)}
        return ProfileReport(
            metrics=metrics,
            metadata={"mode": self.mode, "backend_id": self.backend_id, "enabled": True},
        )


def empty_profile() -> ProfileReport:
    return ProfileReport(metadata={"mode": WOOSH_PROFILE_MODE_OFF, "enabled": False})
