"""Unified SFX workflows. These are not Runtime Core operations and not mux-inside-adapter."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, Tuple

TEXT_GENERATE = "sfx.text_generate"
VIDEO_GENERATE = "sfx.video_generate"
GENERATE_AND_MUX = "sfx.generate_and_mux"


class WorkflowError(ValueError):
    """A workflow identity or backend selection is not sealed."""


@dataclass(frozen=True)
class SfxWorkflow:
    workflow_id: str
    adapters: Tuple[str, ...]
    backends: Tuple[str, ...]
    operations: Tuple[str, ...]
    mux: bool
    default_backend: str | None = None

    def __post_init__(self) -> None:
        if not self.workflow_id or not self.adapters or not self.operations:
            raise WorkflowError("workflow identity is incomplete")
        if self.mux and self.workflow_id != GENERATE_AND_MUX:
            raise WorkflowError("only sfx.generate_and_mux may mux")
        if not self.mux and self.workflow_id == GENERATE_AND_MUX:
            raise WorkflowError("sfx.generate_and_mux must declare mux")


TEXT_GENERATE_WORKFLOW = SfxWorkflow(
    workflow_id=TEXT_GENERATE,
    adapters=("stable-audio-3-small-sfx",),
    backends=("pytorch",),
    operations=("audio.text_to_sfx",),
    mux=False,
    default_backend="pytorch",
)
VIDEO_GENERATE_WORKFLOW = SfxWorkflow(
    workflow_id=VIDEO_GENERATE,
    adapters=("controlfoley", "woosh-v2a"),
    backends=("upstream_parity", "dvflow-8s", "vflow-8s"),
    operations=("audio.video_to_sfx", "V2A", "TV2A", "TC-V2A", "AC-V2A"),
    mux=False,
    default_backend="dvflow-8s",
)
GENERATE_AND_MUX_WORKFLOW = SfxWorkflow(
    workflow_id=GENERATE_AND_MUX,
    adapters=("controlfoley", "woosh-v2a"),
    backends=("upstream_parity", "dvflow-8s", "vflow-8s"),
    operations=(GENERATE_AND_MUX,),
    mux=True,
    default_backend=None,
)

_WORKFLOWS = (
    TEXT_GENERATE_WORKFLOW,
    VIDEO_GENERATE_WORKFLOW,
    GENERATE_AND_MUX_WORKFLOW,
)


class SfxWorkflowCatalog:
    def __init__(self, workflows: Tuple[SfxWorkflow, ...] = _WORKFLOWS) -> None:
        by_id = {item.workflow_id: item for item in workflows}
        if len(by_id) != len(workflows):
            raise WorkflowError("duplicate workflow id")
        self._by_id: Mapping[str, SfxWorkflow] = MappingProxyType(by_id)

    def get(self, workflow_id: str) -> SfxWorkflow:
        try:
            return self._by_id[workflow_id]
        except KeyError as error:
            raise WorkflowError("unknown workflow") from error

    def all_workflows(self) -> Tuple[SfxWorkflow, ...]:
        return tuple(self._by_id[key] for key in sorted(self._by_id))


DEFAULT_WORKFLOW_CATALOG = SfxWorkflowCatalog()


def assert_adapter_does_not_mux(adapter_id: str, operation: str) -> None:
    if operation == GENERATE_AND_MUX:
        raise WorkflowError("mux is a workflow step, not an adapter operation")
    if adapter_id == "woosh-v2a" and operation != "audio.video_to_sfx":
        raise WorkflowError("Woosh V2A exposes only audio.video_to_sfx")
    if adapter_id == "stable-audio-3-small-sfx" and operation != "audio.text_to_sfx":
        raise WorkflowError("Stable Audio 3 V1 exposes only audio.text_to_sfx")
