# pyright: reportMissingImports=false
from __future__ import annotations

from uuid import uuid4

import pytest

from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime_stable_audio_3.adapter import StableAudio3Adapter
from nano_aural_runtime_workers.registry import DurableInvocationBuilderRegistry
from nano_aural_runtime_workers.stable_audio_3 import (
    StableAudio3DurableBindingError,
    StableAudio3DurableInvocationBuilder,
)

_DIGEST = "a" * 64


def _configuration() -> dict[str, str]:
    return {
        "manifest_path": "/operator/manifest.json",
        "deployment_manifest_sha256": _DIGEST,
        "source_dir": "/operator/source",
        "weights_dir": "/operator/weights",
        "source_revision": "a0b57f5483c4588f827f3552b7d5c6ca2a9687be",
        "model_id": "small-sfx",
        "hf_repository": "stabilityai/stable-audio-3-small-sfx",
        "runtime_environment_id": "stable-audio-3-pytorch-2.7.1-cu126",
    }


def _identity(configuration: dict[str, str]) -> dict[str, str]:
    return {
        key: configuration[key]
        for key in (
            "deployment_manifest_sha256",
            "source_revision",
            "model_id",
            "hf_repository",
            "runtime_environment_id",
        )
    }


def test_builder_materializes_text_to_sfx_without_binary_inputs():
    configuration = _configuration()
    builder = StableAudio3DurableInvocationBuilder(StableAudio3Adapter(), configuration)
    deployment = DeploymentRecord(
        str(uuid4()), "sa3", "stable-audio-3-small-sfx", _DIGEST, manifest=_identity(configuration)
    )
    job = JobRecord(
        str(uuid4()),
        "tenant",
        "key",
        "c" * 64,
        {"prompt": "metal impact", "duration_seconds": 5.0, "seed": 42},
        deployment.deployment_id,
        (),
    )
    invocation = builder.build(deployment, job, ())
    assert invocation.operation == "audio.text_to_sfx"
    assert invocation.inputs["prompt"] == "metal impact"
    core = builder.core_deployment(deployment)
    assert core.descriptor.adapter_id == "stable-audio-3-small-sfx"


def test_builder_rejects_binary_inputs_and_operator_fields():
    configuration = _configuration()
    builder = StableAudio3DurableInvocationBuilder(StableAudio3Adapter(), configuration)
    deployment = DeploymentRecord(
        str(uuid4()), "sa3", "stable-audio-3-small-sfx", _DIGEST, manifest=_identity(configuration)
    )
    job = JobRecord(
        str(uuid4()),
        "tenant",
        "key",
        "c" * 64,
        {"prompt": "x", "duration_seconds": 5.0, "seed": 1, "source_dir": "/secret"},
        deployment.deployment_id,
        (),
    )
    with pytest.raises(StableAudio3DurableBindingError):
        builder.build(deployment, job, ())


def test_registry_accepts_stable_audio_builder():
    registry = DurableInvocationBuilderRegistry()
    builder = StableAudio3DurableInvocationBuilder(StableAudio3Adapter(), _configuration())
    registry.register(builder)
    assert "stable-audio-3-small-sfx" in registry.registered_adapter_ids()
