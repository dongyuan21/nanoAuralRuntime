"""CPU contracts and conditional GPU evidence for Roadmap Phase 4A."""

from __future__ import annotations

import hashlib
import io
import json
import math
import os
import sys
import tempfile
import wave
from dataclasses import replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping, Optional, cast

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime import (
    AdapterRegistry,
    CancellationToken,
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
    ModelDeployment,
    Runtime,
    SessionState,
)
from nano_aural_runtime_controlfoley.adapter import (
    ControlFoleyAdapter,
    _staged_deployment_fingerprint,
    controlfoley_staged_deployment_configuration,
    controlfoley_staged_local_deployment,
)
from nano_aural_runtime_controlfoley.baseline import ControlFoleyDeploymentManifest
from nano_aural_runtime_controlfoley.staged import (
    CONTROLFOLEY_STAGE_ORDER,
    ControlFoleyComparisonEvidence,
    ControlFoleyOracleDeploymentBinding,
    ControlFoleyStage,
    ControlFoleyStageContractError,
    ControlFoleyStagedArtifact,
    ControlFoleyStagedBackend,
    ControlFoleyStagedBackendSession,
    ControlFoleyStagedBackendValidation,
    ControlFoleyStageExecutionError,
    ControlFoleyStageValue,
    DeterministicFakeStagedBackend,
    controlfoley_invocation_fingerprint,
    validate_staged_backend_id,
)
from nano_aural_runtime_controlfoley.tasks import (
    EXPERIMENTAL_STAGED_OPERATION,
    UPSTREAM_PARITY_OPERATION,
    ControlFoleyLocalRequest,
    ControlFoleyTaskKind,
)

_SOURCE_REVISION = "6858cd12a48d141201e3266e7abe1f38357a133e"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _staged_configuration(backend_id: str) -> dict[str, str]:
    configuration = {
        "manifest_path": "operator-only-unused-in-cpu-test",
        "deployment_manifest_sha256": _digest("deployment-manifest"),
        "source_dir": "operator-only-unused-in-cpu-test",
        "weights_dir": "operator-only-unused-in-cpu-test",
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": _SOURCE_REVISION,
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": _digest("checkpoint"),
        "execution_path": EXPERIMENTAL_STAGED_OPERATION,
        "staged_backend_id": backend_id,
    }
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    return configuration


def _staged_deployment(
    adapter: ControlFoleyAdapter, configuration: Optional[Mapping[str, str]] = None
) -> ModelDeployment:
    values = dict(configuration or _staged_configuration("deterministic-cpu-test-double-v1"))
    return ModelDeployment(
        deployment_id="controlfoley-experimental-staged-cpu-test",
        descriptor=adapter.descriptor,
        fingerprint=values["staged_deployment_fingerprint"],
        configuration=values,
    )


def _runtime(adapter: ControlFoleyAdapter) -> Runtime:
    registry = AdapterRegistry()
    registry.register(adapter)
    return Runtime(registry)


def _t2a(prompt: str = "bird wing") -> ControlFoleyLocalRequest:
    return ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt=prompt)


class _UpstreamRunnerDouble:
    def __init__(self) -> None:
        self.validated = 0
        self.invoked = 0

    def validate(self, configuration: Mapping[str, Any]) -> Mapping[str, Any]:
        self.validated += 1
        return {
            "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
            "source_revision": configuration["source_revision"],
            "variant": configuration["variant"],
            "precision": configuration["precision"],
            "checkpoint_sha256": configuration["checkpoint_sha256"],
            "checkpoint_size_bytes": 1,
            "external_weight_sha256": {"ext_weights/v1-44.pth": _digest("external")},
        }

    def invoke(self, configuration, request, context):  # type: ignore[no-untyped-def]
        del request
        self.invoked += 1
        context.cancellation_token.raise_if_cancelled()
        content = b"real-runner-double"
        return content, {
            "sha256": hashlib.sha256(content).hexdigest(),
            "size_bytes": len(content),
            "source_revision": configuration["source_revision"],
            "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
            "variant": configuration["variant"],
            "precision": configuration["precision"],
            "checkpoint_sha256": configuration["checkpoint_sha256"],
            "format": "flac",
            "worker": {
                "wall_time_seconds": 1.0,
                "peak_allocated_bytes": 0,
                "peak_reserved_bytes": 0,
            },
        }


def _upstream_deployment(adapter: ControlFoleyAdapter) -> ModelDeployment:
    configuration = {
        name: value
        for name, value in _staged_configuration("unused").items()
        if name not in {"execution_path", "staged_backend_id", "staged_deployment_fingerprint"}
    }
    return ModelDeployment(
        deployment_id="controlfoley-upstream-test",
        descriptor=adapter.descriptor,
        fingerprint=configuration["deployment_manifest_sha256"],
        configuration=configuration,
    )


def _comparison_bindings(request: Optional[ControlFoleyLocalRequest] = None) -> dict[str, str]:
    return {
        "deployment_manifest_sha256": _digest("deployment-manifest"),
        "candidate_deployment_fingerprint": _staged_configuration(
            "deterministic-cpu-test-double-v1"
        )["staged_deployment_fingerprint"],
        "candidate_backend_id": "deterministic-cpu-test-double-v1",
        "source_revision": _SOURCE_REVISION,
        "checkpoint_sha256": _digest("checkpoint"),
        "canonical_invocation_sha256": controlfoley_invocation_fingerprint(request or _t2a()),
    }


def _contains_key(value: object, target: str) -> bool:
    if isinstance(value, Mapping):
        return target in value or any(_contains_key(item, target) for item in value.values())
    if isinstance(value, (tuple, list)):
        return any(_contains_key(item, target) for item in value)
    return False


def test_staged_path_is_explicit_non_default_and_keeps_upstream_oracle() -> None:
    backend = DeterministicFakeStagedBackend()
    runner = _UpstreamRunnerDouble()
    adapter = ControlFoleyAdapter(runner=runner, staged_backend=backend)
    request = _t2a()

    assert request.to_invocation("default").operation == UPSTREAM_PARITY_OPERATION
    assert adapter.descriptor.capabilities["default_operation"] == UPSTREAM_PARITY_OPERATION
    assert adapter.descriptor.capabilities["experimental_operations"] == (
        EXPERIMENTAL_STAGED_OPERATION,
    )

    runtime = _runtime(adapter)
    upstream = runtime.load(_upstream_deployment(adapter))
    runtime.invoke(upstream, request.to_invocation("oracle"))
    runtime.unload(upstream)
    assert runner.validated == 1
    assert runner.invoked == 1
    assert backend.loaded == 0


def test_staged_runtime_uses_exact_order_defaults_and_a_real_wav_test_artifact() -> None:
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter))
    request = _t2a()
    result = runtime.invoke(
        session,
        request.to_invocation("staged", operation=EXPERIMENTAL_STAGED_OPERATION),
    )

    assert tuple(backend.calls) == CONTROLFOLEY_STAGE_ORDER
    assert all(item.num_steps == 25 for item in backend.requests)
    assert all(item.guidance_scale == 4.5 for item in backend.requests)
    assert all(item.seed == 42 for item in backend.requests)
    artifact = result.artifacts[0]
    assert artifact.name == "controlfoley-staged.wav"
    assert artifact.media_type == "audio/wav"
    with wave.open(io.BytesIO(artifact.content), "rb") as audio:
        assert (audio.getnchannels(), audio.getsampwidth(), audio.getframerate()) == (1, 2, 8_000)
        assert audio.getnframes() == 8
    assert artifact.metadata["format"] == "wav"
    assert artifact.metadata["stage_order"] == tuple(
        stage.value for stage in CONTROLFOLEY_STAGE_ORDER
    )
    assert "backend_metadata" not in artifact.metadata
    assert "implementation" not in result.metadata["deployment"]
    assert result.warnings == (
        "experimental staged path; upstream parity and performance are unmeasured",
    )
    comparison = result.metadata["comparison"]
    assert comparison["state"] == "unmeasured"
    assert comparison["oracle_operation"] == UPSTREAM_PARITY_OPERATION
    assert comparison["candidate_operation"] == EXPERIMENTAL_STAGED_OPERATION
    assert comparison["candidate_deployment_fingerprint"] == session.deployment.fingerprint
    assert comparison["candidate_backend_id"] == backend.backend_id
    assert comparison["canonical_invocation_sha256"] == controlfoley_invocation_fingerprint(request)
    assert not _contains_key(comparison, "threshold")
    assert not _contains_key(comparison, "claim")
    runtime.unload(session)
    assert backend.unloaded == 1


@pytest.mark.parametrize(
    "backend_id",
    (
        "",
        ".hidden",
        "bad/id",
        "bad\\id",
        "bad id",
        "token=secret",
        "line\nbreak",
        "后端",
        "a" * 65,
    ),
)
def test_staged_backend_id_rejects_path_token_and_control_text(backend_id: str) -> None:
    with pytest.raises(ValueError, match="ASCII identifier"):
        validate_staged_backend_id(backend_id)
    with pytest.raises(ValueError, match="ASCII identifier"):
        controlfoley_staged_deployment_configuration(
            Path("unused-manifest"), Path("unused-source"), Path("unused-weights"), backend_id
        )


def test_every_backend_identity_entrypoint_uses_the_same_safe_validator() -> None:
    fingerprint = _digest("deployment")
    with pytest.raises(ValueError, match="ASCII identifier"):
        ControlFoleyStagedBackendSession("bad/path", fingerprint)
    with pytest.raises(ValueError, match="ASCII identifier"):
        ControlFoleyStagedBackendValidation(
            "token=secret",
            _digest("manifest"),
            _SOURCE_REVISION,
            "large_44k",
            "fp32",
            _digest("checkpoint"),
            "implementation",
        )
    backend = DeterministicFakeStagedBackend()
    backend.backend_id = "bad path"  # type: ignore[misc]
    with pytest.raises(TypeError, match="safe backend_id"):
        ControlFoleyAdapter(staged_backend=backend)


def _verified_manifest(checkpoint_label: str = "checkpoint") -> ControlFoleyDeploymentManifest:
    return ControlFoleyDeploymentManifest.from_dict(
        {
            "schema_version": 1,
            "deployment_id": "controlfoley-large44k-fp32-v1",
            "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
            "source_revision": _SOURCE_REVISION,
            "variant": "large_44k",
            "precision": "fp32",
            "checkpoint_relative_path": "weights/controlfoley.pth",
            "checkpoint": {
                "status": "verified",
                "sha256": _digest(checkpoint_label),
                "size_bytes": 1,
            },
            "external_weights": [
                {
                    "relative_path": relative_path,
                    "fingerprint": {
                        "status": "verified",
                        "sha256": _digest(relative_path),
                        "size_bytes": 1,
                    },
                }
                for relative_path in (
                    "ext_weights/cav_mae_st.pth",
                    "ext_weights/music_speech_audioset_epoch_15_esc_89.98.pt",
                    "ext_weights/synchformer_state_dict.pth",
                    "ext_weights/v1-44.pth",
                )
            ],
        }
    )


def test_oracle_binding_rejects_candidate_or_manifest_drift() -> None:
    manifest = _verified_manifest()
    binding = ControlFoleyOracleDeploymentBinding.from_manifest(manifest)
    candidate = _staged_configuration("deterministic-cpu-test-double-v1")
    candidate["deployment_manifest_sha256"] = binding.deployment_manifest_sha256
    candidate["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(candidate)
    binding.assert_manifest(manifest)
    binding.assert_candidate_configuration(candidate)

    for name, value in (
        ("deployment_manifest_sha256", _digest("changed-manifest")),
        ("source_revision", "1234567890abcdef1234567890abcdef12345678"),
        ("checkpoint_sha256", _digest("changed-checkpoint")),
    ):
        drifted = dict(candidate)
        drifted[name] = value
        with pytest.raises(InvocationRejectedError, match="captured oracle"):
            binding.assert_candidate_configuration(drifted)
    with pytest.raises(InvocationRejectedError, match="changed during the run"):
        binding.assert_manifest(_verified_manifest("changed-checkpoint"))


def test_public_staged_deployment_builder_seals_backend_and_manifest(tmp_path: Path) -> None:
    manifest_path = tmp_path / "deployment.json"
    manifest_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "deployment_id": "controlfoley-large44k-fp32-v1",
                "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
                "source_revision": _SOURCE_REVISION,
                "variant": "large_44k",
                "precision": "fp32",
                "checkpoint_relative_path": "weights/controlfoley.pth",
                "checkpoint": {
                    "status": "verified",
                    "sha256": _digest("checkpoint"),
                    "size_bytes": 1,
                },
                "external_weights": [
                    {
                        "relative_path": relative_path,
                        "fingerprint": {
                            "status": "verified",
                            "sha256": _digest(relative_path),
                            "size_bytes": 1,
                        },
                    }
                    for relative_path in (
                        "ext_weights/cav_mae_st.pth",
                        "ext_weights/music_speech_audioset_epoch_15_esc_89.98.pt",
                        "ext_weights/synchformer_state_dict.pth",
                        "ext_weights/v1-44.pth",
                    )
                ],
            }
        ),
        encoding="utf-8",
    )
    source_dir = tmp_path / "source"
    weights_dir = tmp_path / "weights"
    source_dir.mkdir()
    weights_dir.mkdir()
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    configuration = controlfoley_staged_deployment_configuration(
        manifest_path, source_dir, weights_dir, backend.backend_id
    )
    assert configuration["execution_path"] == EXPERIMENTAL_STAGED_OPERATION
    assert configuration["staged_backend_id"] == backend.backend_id
    assert (
        configuration["staged_deployment_fingerprint"]
        != (configuration["deployment_manifest_sha256"])
    )
    deployment = controlfoley_staged_local_deployment(
        adapter, manifest_path, source_dir, weights_dir, backend.backend_id
    )
    assert deployment.fingerprint == configuration["staged_deployment_fingerprint"]


def test_staged_operation_and_deployment_must_select_each_other_exactly() -> None:
    request = _t2a()
    adapter_without_backend = ControlFoleyAdapter()
    with pytest.raises(InvocationRejectedError, match="operator-supplied backend"):
        _runtime(adapter_without_backend).load(_staged_deployment(adapter_without_backend))

    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter))
    with pytest.raises(InvocationRejectedError, match="does not match"):
        runtime.invoke(session, request.to_invocation("wrong-default"))
    assert session.state is SessionState.READY
    runtime.unload(session)

    runner = _UpstreamRunnerDouble()
    adapter = ControlFoleyAdapter(runner=runner, staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_upstream_deployment(adapter))
    with pytest.raises(InvocationRejectedError, match="does not match"):
        runtime.invoke(
            session,
            request.to_invocation("wrong-staged", operation=EXPERIMENTAL_STAGED_OPERATION),
        )
    assert session.state is SessionState.READY
    runtime.unload(session)


def test_staged_deployment_fingerprint_drift_is_rejected_before_backend_load() -> None:
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    configuration = _staged_configuration(backend.backend_id)
    configuration["checkpoint_sha256"] = _digest("mutated-checkpoint")
    with pytest.raises(InvocationRejectedError, match="not sealed"):
        _runtime(adapter).load(_staged_deployment(adapter, configuration))
    assert backend.loaded == 0


class _WrongValidationBackend(DeterministicFakeStagedBackend):
    def load(self, configuration):  # type: ignore[no-untyped-def]
        session, validation = super().load(configuration)
        return session, replace(validation, checkpoint_sha256=_digest("wrong-checkpoint"))


class _WrongSessionBackend(DeterministicFakeStagedBackend):
    def load(self, configuration):  # type: ignore[no-untyped-def]
        session, validation = super().load(configuration)
        return replace(session, deployment_fingerprint=_digest("wrong-deployment")), validation


class _WrongValidationUnloadRaises(_WrongValidationBackend):
    def unload(self, session):  # type: ignore[no-untyped-def]
        super().unload(session)
        raise RuntimeError("operator unload fault")


@pytest.mark.parametrize(
    "backend",
    [_WrongValidationBackend(), _WrongSessionBackend(), _WrongValidationUnloadRaises()],
)
def test_invalid_successful_backend_load_is_unloaded_before_rejection(
    backend: DeterministicFakeStagedBackend,
) -> None:
    adapter = ControlFoleyAdapter(staged_backend=backend)
    with pytest.raises(InvocationRejectedError):
        _runtime(adapter).load(
            _staged_deployment(adapter, _staged_configuration(backend.backend_id))
        )
    assert backend.loaded == 1
    assert backend.unloaded == 1


def test_stage_cancellation_is_cooperative_and_keeps_session_ready() -> None:
    backend = DeterministicFakeStagedBackend(
        cancel_stage=ControlFoleyStage.CONDITION_ENCODE_PROJECTION
    )
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter))
    with pytest.raises(InvocationCancelledError):
        runtime.invoke(
            session,
            _t2a().to_invocation("cancel", operation=EXPERIMENTAL_STAGED_OPERATION),
        )
    assert session.state is SessionState.READY
    assert backend.calls == list(CONTROLFOLEY_STAGE_ORDER[:2])
    runtime.unload(session)


def test_pre_cancelled_staged_invocation_runs_no_stage() -> None:
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter))
    token = CancellationToken()
    token.cancel("test")
    with pytest.raises(InvocationCancelledError):
        runtime.invoke(
            session,
            _t2a().to_invocation("pre-cancel", operation=EXPERIMENTAL_STAGED_OPERATION),
            ExecutionContext(cancellation_token=token),
        )
    assert backend.calls == []
    assert session.state is SessionState.READY
    runtime.unload(session)


def test_named_stage_failure_is_typed_and_marks_session_failed() -> None:
    stage = ControlFoleyStage.INTEGRATE
    backend = DeterministicFakeStagedBackend(fail_stage=stage)
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter))
    with pytest.raises(ControlFoleyStageExecutionError) as error:
        runtime.invoke(
            session,
            _t2a().to_invocation("fault", operation=EXPERIMENTAL_STAGED_OPERATION),
        )
    assert error.value.stage is stage
    assert session.state is SessionState.FAILED
    runtime.unload(session)


class _WrongStageBackend(DeterministicFakeStagedBackend):
    def run_stage(self, session, stage, previous, request, context):  # type: ignore[no-untyped-def]
        value = super().run_stage(session, stage, previous, request, context)
        if stage is ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS:
            return replace(value, stage=ControlFoleyStage.INTEGRATE)
        return value


def test_backend_cannot_reorder_or_skip_stage_contract() -> None:
    backend = _WrongStageBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter))
    with pytest.raises(ControlFoleyStageContractError, match="wrong value"):
        runtime.invoke(
            session,
            _t2a().to_invocation("wrong-stage", operation=EXPERIMENTAL_STAGED_OPERATION),
        )
    assert session.state is SessionState.FAILED
    runtime.unload(session)


def test_nested_stage_evidence_is_recursively_immutable() -> None:
    original = {"outer": [{"secret": "redacted", "values": [1, 2]}]}
    value = ControlFoleyStageValue(ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS, b"payload", original)
    original["outer"][0]["secret"] = "mutated"  # type: ignore[index]
    outer = value.evidence["outer"]
    assert isinstance(outer, tuple)
    nested = outer[0]
    assert isinstance(nested, MappingProxyType)
    assert nested["secret"] == "redacted"
    assert nested["values"] == (1, 2)
    with pytest.raises(TypeError):
        nested["secret"] = "cannot-mutate"  # type: ignore[index]


class _SensitiveMetadataBackend(DeterministicFakeStagedBackend):
    def load(self, configuration):  # type: ignore[no-untyped-def]
        session, validation = super().load(configuration)
        return session, replace(
            validation, implementation="/operator/private/backend.py token=do-not-leak"
        )

    def run_stage(self, session, stage, previous, request, context):  # type: ignore[no-untyped-def]
        value = super().run_stage(session, stage, previous, request, context)
        if stage is ControlFoleyStage.DECODE_VOCODER_POSTPROCESS:
            assert isinstance(value.payload, ControlFoleyStagedArtifact)
            artifact = replace(
                value.payload,
                metadata={"private_path": "/operator/private", "token": "do-not-leak"},
            )
            return replace(value, payload=artifact)
        return value


def test_backend_metadata_is_not_exposed_in_core_result() -> None:
    backend = _SensitiveMetadataBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter))
    result = runtime.invoke(
        session,
        _t2a().to_invocation("sanitized", operation=EXPERIMENTAL_STAGED_OPERATION),
    )
    serialized = repr((result.artifacts[0].metadata, result.metadata))
    assert "/operator/private" not in serialized
    assert "do-not-leak" not in serialized
    runtime.unload(session)


def test_invocation_fingerprint_binds_input_content_and_exact_parameters(tmp_path: Path) -> None:
    video = tmp_path / "video.bin"
    video.write_bytes(b"one")
    first = ControlFoleyLocalRequest(
        ControlFoleyTaskKind.TV2A,
        video_path=video,
        prompt="footsteps",
        duration_seconds=8.0,
        seed=42,
    )
    first_digest = controlfoley_invocation_fingerprint(first)
    video.write_bytes(b"two")
    changed_input = replace(first, video_path=video)
    assert controlfoley_invocation_fingerprint(changed_input) != first_digest
    assert controlfoley_invocation_fingerprint(replace(changed_input, seed=0)) != (
        controlfoley_invocation_fingerprint(changed_input)
    )
    assert controlfoley_invocation_fingerprint(replace(changed_input, duration_seconds=10.0)) != (
        controlfoley_invocation_fingerprint(changed_input)
    )


def test_comparison_evidence_is_sealed_raw_and_has_no_claim_or_threshold() -> None:
    bindings = _comparison_bindings()
    unmeasured = ControlFoleyComparisonEvidence.unmeasured(
        **bindings, candidate_sha256=_digest("candidate")
    )
    unmeasured_data = unmeasured.to_dict()
    assert unmeasured_data["oracle_sha256"] is None
    assert all(value is None for value in unmeasured_data["raw_metrics"].values())  # type: ignore[union-attr]
    assert not _contains_key(unmeasured_data, "threshold")
    assert not _contains_key(unmeasured_data, "claim")

    measured = ControlFoleyComparisonEvidence.measured(
        **bindings,
        candidate_sha256=_digest("candidate"),
        oracle_sha256=_digest("oracle"),
        raw_metrics={
            "peak": 0.9,
            "rms": 0.1,
            "mae": 0.01,
            "max_absolute_error": 0.2,
            "waveform_cosine_similarity": 0.75,
            "mel_spectrogram_distance": 0.03,
        },
        waveform={"channels": 1, "samples": 100, "sample_rate": 44_100},
    )
    assert measured.state == "measured"
    assert measured.canonical_invocation_sha256 == bindings["canonical_invocation_sha256"]

    with pytest.raises(ValueError, match="nonnegative"):
        replace(measured, raw_metrics={**measured.raw_metrics, "mae": -0.1})
    with pytest.raises(ValueError, match="between -1 and 1"):
        replace(
            measured,
            raw_metrics={**measured.raw_metrics, "waveform_cosine_similarity": 1.1},
        )
    with pytest.raises(ValueError, match="full SHA-256"):
        replace(measured, canonical_invocation_sha256="0" * 64)
    with pytest.raises(ValueError, match="ASCII identifier"):
        replace(measured, candidate_backend_id="/operator/private/token=secret")
    with pytest.raises(ValueError, match="invented evidence"):
        replace(unmeasured, oracle_sha256=_digest("invented"))


def test_staged_module_and_core_boundary_have_no_backend_or_controlfoley_imports() -> None:
    staged_source = Path("src/nano_aural_runtime_controlfoley/staged.py").read_text(
        encoding="utf-8"
    )
    assert "import torch" not in staged_source
    assert "import controlfoley" not in staged_source.lower()
    core_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src/nano_aural_runtime").glob("*.py")
    )
    assert "ControlFoley" not in core_sources
    assert EXPERIMENTAL_STAGED_OPERATION not in core_sources


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_controlfoley_staged_gpu_comparison_when_operator_configured() -> None:
    """Execute only with the sealed config documented in the Phase 4A runbook."""

    raw = os.environ.get("CONTROLFOLEY_P4A_GPU_CONFIG")
    if raw is None:
        pytest.skip("CONTROLFOLEY_P4A_GPU_CONFIG is not set; RTX 4090 evidence is deferred")
    assert raw is not None
    # The imports and operator module are delayed so CPU collection stays free
    # of torch/upstream dependencies.
    import importlib

    from nano_aural_runtime_controlfoley.baseline import (
        BaselineResultManifest,
        ControlFoleyDeploymentManifest,
        FixtureManifest,
        compare_waveforms,
        fingerprint_file,
        fingerprint_text,
        load_json,
        verify_result_bindings,
    )

    config = json.loads(raw)
    required = {
        "deployment",
        "fixture",
        "source_dir",
        "weights_dir",
        "backend_module",
        "task",
        "comparison_output",
    }
    if not isinstance(config, dict) or required - set(config):
        pytest.fail("CONTROLFOLEY_P4A_GPU_CONFIG is incomplete")
    deployment_path = Path(config["deployment"])
    fixture_path = Path(config["fixture"])
    deployment_manifest = ControlFoleyDeploymentManifest.from_dict(load_json(deployment_path))
    oracle_binding = ControlFoleyOracleDeploymentBinding.from_manifest(deployment_manifest)
    fixture = FixtureManifest.from_dict(load_json(fixture_path))
    task = ControlFoleyTaskKind(config["task"])
    assert fixture.task == task.value
    module = importlib.import_module(config["backend_module"])
    factory = getattr(module, "create_controlfoley_staged_backend", None)
    if not callable(factory):
        pytest.fail("backend_module must expose create_controlfoley_staged_backend()")
    assert callable(factory)
    backend = cast(ControlFoleyStagedBackend, factory())

    request_values: dict[str, object] = {
        "task": task,
        "duration_seconds": fixture.duration_seconds,
        "num_steps": fixture.num_steps,
        "guidance_scale": fixture.guidance_scale,
        "seed": fixture.seed,
    }
    direct_command = [
        sys.executable,
        "benchmarks/controlfoley_baseline.py",
        "--deployment",
        str(deployment_path),
        "--fixture",
        str(fixture_path),
        "--source-dir",
        config["source_dir"],
        "--weights-dir",
        config["weights_dir"],
        "--execute-upstream",
    ]
    if task is not ControlFoleyTaskKind.T2A:
        video = Path(config["video"])
        request_values["video_path"] = video
        direct_command.extend(("--video", str(video)))
        assert next(item for item in fixture.inputs if item.role == "video").fingerprint == (
            fingerprint_file(video)
        )
    if task is ControlFoleyTaskKind.AC_V2A:
        audio = Path(config["audio"])
        request_values["reference_audio_path"] = audio
        direct_command.extend(("--audio", str(audio)))
        assert next(
            item for item in fixture.inputs if item.role == "reference_audio"
        ).fingerprint == fingerprint_file(audio)
    if task in (ControlFoleyTaskKind.TV2A, ControlFoleyTaskKind.TC_V2A, ControlFoleyTaskKind.T2A):
        prompt = config["prompt"]
        request_values["prompt"] = prompt
        direct_command.extend(("--prompt", prompt))
        assert next(item for item in fixture.inputs if item.role == "prompt").fingerprint == (
            fingerprint_text(prompt)
        )
    request = ControlFoleyLocalRequest(**request_values)  # type: ignore[arg-type]
    sealed_invocation_fingerprint = controlfoley_invocation_fingerprint(request)

    with tempfile.TemporaryDirectory(prefix="controlfoley-p4a-") as directory:
        root = Path(directory)
        baseline_result_path = root / "baseline.json"
        direct_command.extend(
            (
                "--output-dir",
                str(root / "oracle-one"),
                "--repeat-output-dir",
                str(root / "oracle-two"),
                "--result",
                str(baseline_result_path),
            )
        )
        import subprocess

        subprocess.run(direct_command, check=True, shell=False)
        baseline_result = BaselineResultManifest.from_dict(load_json(baseline_result_path))
        verify_result_bindings(deployment_manifest, fixture, baseline_result)
        oracle_binding.assert_manifest(
            ControlFoleyDeploymentManifest.from_dict(load_json(deployment_path))
        )
        assert controlfoley_invocation_fingerprint(request) == sealed_invocation_fingerprint

        adapter = ControlFoleyAdapter(staged_backend=backend)
        runtime = _runtime(adapter)
        candidate_deployment = controlfoley_staged_local_deployment(
            adapter,
            deployment_path,
            Path(config["source_dir"]),
            Path(config["weights_dir"]),
            backend.backend_id,
        )
        oracle_binding.assert_candidate_configuration(candidate_deployment.configuration)
        session = runtime.load(candidate_deployment)
        oracle_binding.assert_candidate_configuration(session.deployment.configuration)
        try:
            result = runtime.invoke(
                session,
                request.to_invocation("gpu-staged", operation=EXPERIMENTAL_STAGED_OPERATION),
            )
        finally:
            runtime.unload(session)
        assert controlfoley_invocation_fingerprint(request) == sealed_invocation_fingerprint
        assert (
            result.metadata["comparison"]["canonical_invocation_sha256"]
            == sealed_invocation_fingerprint
        )
        assert result.metadata["comparison"]["candidate_backend_id"] == backend.backend_id
        candidate = root / result.artifacts[0].name
        candidate.write_bytes(result.artifacts[0].content)
        oracle = next((root / "oracle-one").rglob("*.flac"))
        metrics, waveform_shape = compare_waveforms(oracle, candidate)
        oracle_binding.assert_manifest(
            ControlFoleyDeploymentManifest.from_dict(load_json(deployment_path))
        )
        oracle_binding.assert_candidate_configuration(session.deployment.configuration)
        assert controlfoley_invocation_fingerprint(request) == sealed_invocation_fingerprint
        evidence = ControlFoleyComparisonEvidence.measured(
            deployment_manifest_sha256=oracle_binding.deployment_manifest_sha256,
            candidate_deployment_fingerprint=session.deployment.fingerprint,
            candidate_backend_id=backend.backend_id,
            source_revision=oracle_binding.source_revision,
            checkpoint_sha256=oracle_binding.checkpoint_sha256,
            canonical_invocation_sha256=sealed_invocation_fingerprint,
            candidate_sha256=fingerprint_file(candidate).sha256 or "",
            oracle_sha256=fingerprint_file(oracle).sha256 or "",
            raw_metrics=metrics,
            waveform=waveform_shape,
        )
        assert all(math.isfinite(value) for value in metrics.values())
        oracle_binding.assert_manifest(
            ControlFoleyDeploymentManifest.from_dict(load_json(deployment_path))
        )
        oracle_binding.assert_candidate_configuration(session.deployment.configuration)
        output = Path(config["comparison_output"])
        output.parent.mkdir(parents=True, exist_ok=True)
        with output.open("x", encoding="utf-8") as handle:
            json.dump(dict(evidence.to_dict()), handle, sort_keys=True)
            handle.write("\n")
