# pyright: reportMissingImports=false
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest

from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime.durable.materialization import MaterializedInput
from nano_aural_runtime.durable.uploads import MediaInfo
from nano_aural_runtime_woosh.adapter import WooshV2AAdapter
from nano_aural_runtime_workers.registry import DurableInvocationBuilderRegistry
from nano_aural_runtime_workers.woosh import (
    WooshV2ADurableBindingError,
    WooshV2ADurableInvocationBuilder,
)

_DIGEST = "a" * 64


def _configuration(backend_id: str = "dvflow-8s") -> dict[str, str]:
    return {
        "manifest_path": "/operator/manifest.json",
        "deployment_manifest_sha256": _DIGEST,
        "source_dir": "/operator/source",
        "weights_dir": "/operator/weights",
        "synchformer_path": "/operator/synchformer.pth",
        "source_revision": "f6ff658efc6d63dee9959964cd75c63415910a19",
        "adapter_id": "woosh-v2a",
        "backend_id": backend_id,
        "runtime_environment_id": "woosh-v2a-pytorch-2.8.0-cu128",
    }


def _identity(configuration: dict[str, str]) -> dict[str, str]:
    return {
        key: configuration[key]
        for key in (
            "deployment_manifest_sha256",
            "source_revision",
            "adapter_id",
            "backend_id",
            "runtime_environment_id",
        )
    }


def _video(tmp_path: Path, duration: float = 8.0) -> MaterializedInput:
    path = tmp_path / "clip.mp4"
    path.write_bytes(b"server-owned")
    return MaterializedInput(
        "video",
        str(uuid4()),
        str(uuid4()),
        "d" * 64,
        path.stat().st_size,
        path,
        MediaInfo("video/mp4", duration),
    )


def test_builder_materializes_video_to_sfx_with_optional_prompt(tmp_path: Path):
    configuration = _configuration()
    builder = WooshV2ADurableInvocationBuilder(WooshV2AAdapter(), configuration)
    deployment = DeploymentRecord(
        str(uuid4()), "woosh", "woosh-v2a", _DIGEST, manifest=_identity(configuration)
    )
    job = JobRecord(
        str(uuid4()),
        "tenant",
        "key",
        "c" * 64,
        {"prompt": "metal blocks colliding", "seed": 42},
        deployment.deployment_id,
        (),
    )
    invocation = builder.build(deployment, job, (_video(tmp_path),))
    assert invocation.operation == "audio.video_to_sfx"
    assert invocation.inputs["prompt"] == "metal blocks colliding"
    core = builder.core_deployment(deployment)
    assert core.configuration["backend_id"] == "dvflow-8s"


def test_builder_rejects_short_video_audio_input_and_solver_fields(tmp_path: Path):
    configuration = _configuration("vflow-8s")
    builder = WooshV2ADurableInvocationBuilder(WooshV2AAdapter(), configuration)
    deployment = DeploymentRecord(
        str(uuid4()), "woosh", "woosh-v2a", _DIGEST, manifest=_identity(configuration)
    )
    job = JobRecord(
        str(uuid4()),
        "tenant",
        "key",
        "c" * 64,
        {"seed": 1, "cfg": 3},
        deployment.deployment_id,
        (),
    )
    with pytest.raises(WooshV2ADurableBindingError):
        builder.build(deployment, job, (_video(tmp_path),))
    job = JobRecord(
        str(uuid4()),
        "tenant",
        "key",
        "c" * 64,
        {"seed": 1},
        deployment.deployment_id,
        (),
    )
    with pytest.raises(WooshV2ADurableBindingError):
        builder.build(deployment, job, (_video(tmp_path, 4.0),))
    audio = MaterializedInput(
        "reference_audio",
        str(uuid4()),
        str(uuid4()),
        "e" * 64,
        1,
        tmp_path / "clip.mp4",
        MediaInfo("audio/wav", 8.0),
    )
    (tmp_path / "clip.mp4").write_bytes(b"x")
    with pytest.raises(WooshV2ADurableBindingError):
        builder.build(deployment, job, (audio,))


def test_registry_accepts_woosh_builder():
    registry = DurableInvocationBuilderRegistry()
    builder = WooshV2ADurableInvocationBuilder(WooshV2AAdapter(), _configuration())
    registry.register(builder)
    assert "woosh-v2a" in registry.registered_adapter_ids()
