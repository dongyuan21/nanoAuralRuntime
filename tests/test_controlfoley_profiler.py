"""Roadmap Phase 4B ControlFoley profiler contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
from pathlib import Path
from typing import Any, Mapping, cast

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime import (
    AdapterRegistry,
    InvocationCancelledError,
    ModelDeployment,
    Runtime,
    SessionState,
)
from nano_aural_runtime_controlfoley.adapter import (
    ControlFoleyAdapter,
    _staged_deployment_fingerprint,
    controlfoley_staged_local_deployment,
)
from nano_aural_runtime_controlfoley.baseline import (
    ControlFoleyDeploymentManifest,
    FixtureManifest,
    GpuPrerequisites,
    GpuPrerequisitesUnavailable,
    fingerprint_file,
    fingerprint_text,
    load_json,
    manifest_sha256,
)
from nano_aural_runtime_controlfoley.profile import (
    ControlFoleyCudaObservation,
    ControlFoleyGpuProfileConfiguration,
    ControlFoleyGpuProfileEvidence,
    ControlFoleyGpuProfilePreflight,
    ControlFoleyProfileBinding,
    ControlFoleyProfileLevel,
    ControlFoleyProfiler,
    ControlFoleyProfileSealError,
    TorchCudaProfileBackend,
    require_controlfoley_gpu_profile_preflight,
)
from nano_aural_runtime_controlfoley.staged import (
    CONTROLFOLEY_STAGE_ORDER,
    ControlFoleyOracleDeploymentBinding,
    ControlFoleyStage,
    ControlFoleyStagedBackend,
    ControlFoleyStageExecutionError,
    DeterministicFakeStagedBackend,
    controlfoley_invocation_fingerprint,
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


class StepClock:
    def __init__(self, step_ns: int = 1_000_000_000) -> None:
        self.value = -step_ns
        self.step_ns = step_ns
        self.calls = 0

    def __call__(self) -> int:
        self.calls += 1
        self.value += self.step_ns
        return self.value


class BrokenClock:
    def __call__(self) -> int:
        raise RuntimeError("/private/clock token=must-not-leak")


class UnavailableCudaBackend:
    backend_id = "cpu-unavailable-test-capability"

    def __init__(self) -> None:
        self.availability_checks = 0
        self.begins = 0

    def is_available(self) -> bool:
        self.availability_checks += 1
        return False

    def begin(self, observation_name: str) -> object:
        del observation_name
        self.begins += 1
        raise AssertionError("unavailable CUDA backend must not begin an event")

    def end(self, token: object) -> ControlFoleyCudaObservation:
        del token
        raise AssertionError("unavailable CUDA backend must not end an event")


class BrokenCudaBackend(UnavailableCudaBackend):
    backend_id = "broken-cuda-capability"

    def is_available(self) -> bool:
        raise RuntimeError("/private/path token=must-not-leak")


class RunnerDouble:
    def validate(self, configuration: Mapping[str, Any]) -> Mapping[str, Any]:
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
        context.cancellation_token.raise_if_cancelled()
        content = b"profile-semantic-stability"
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


def _runtime(adapter: ControlFoleyAdapter) -> Runtime:
    registry = AdapterRegistry()
    registry.register(adapter)
    return Runtime(registry)


def _upstream_deployment(adapter: ControlFoleyAdapter) -> ModelDeployment:
    configuration = {
        "manifest_path": "unused",
        "deployment_manifest_sha256": _digest("manifest"),
        "source_dir": "unused",
        "weights_dir": "unused",
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": _SOURCE_REVISION,
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": _digest("checkpoint"),
    }
    return ModelDeployment(
        "controlfoley-profile-upstream",
        adapter.descriptor,
        configuration["deployment_manifest_sha256"],
        configuration,
    )


def _staged_deployment(adapter: ControlFoleyAdapter, backend_id: str) -> ModelDeployment:
    configuration = {
        "manifest_path": "unused",
        "deployment_manifest_sha256": _digest("manifest"),
        "source_dir": "unused",
        "weights_dir": "unused",
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": _SOURCE_REVISION,
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": _digest("checkpoint"),
        "execution_path": EXPERIMENTAL_STAGED_OPERATION,
        "staged_backend_id": backend_id,
    }
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    return ModelDeployment(
        "controlfoley-profile-staged",
        adapter.descriptor,
        configuration["staged_deployment_fingerprint"],
        configuration,
    )


def _invoke_upstream(adapter: ControlFoleyAdapter):  # type: ignore[no-untyped-def]
    runtime = _runtime(adapter)
    session = runtime.load(_upstream_deployment(adapter))
    try:
        return runtime.invoke(
            session,
            ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation(
                "profile-upstream"
            ),
        )
    finally:
        runtime.unload(session)


def _invoke_staged(adapter: ControlFoleyAdapter, backend_id: str):  # type: ignore[no-untyped-def]
    runtime = _runtime(adapter)
    session = runtime.load(_staged_deployment(adapter, backend_id))
    try:
        return runtime.invoke(
            session,
            ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation(
                "profile-staged", operation=EXPERIMENTAL_STAGED_OPERATION
            ),
        )
    finally:
        runtime.unload(session)


def _stage_records(result) -> tuple[Mapping[str, Any], ...]:  # type: ignore[no-untyped-def]
    return cast(tuple[Mapping[str, Any], ...], result.profile.metadata["stages"])


def _assert_cpu_only_profile(result, expected_names: tuple[str, ...]) -> None:  # type: ignore[no-untyped-def]
    metadata = result.profile.metadata
    assert set(metadata) == {
        "schema_version",
        "namespace",
        "status",
        "level",
        "operation",
        "cpu_clock",
        "total_cpu_seconds",
        "cuda",
        "binding",
        "stages",
    }
    assert metadata["status"] == "ok"
    assert metadata["cpu_clock"] == "monotonic_ns"
    assert metadata["cuda"] == {"status": "unavailable", "backend_id": None}
    binding = cast(Mapping[str, object], metadata["binding"])
    assert set(binding) == {
        "operation",
        "deployment_fingerprint",
        "deployment_manifest_sha256",
        "source_revision",
        "checkpoint_sha256",
        "canonical_invocation_sha256",
        "staged_backend_id",
    }
    assert binding["operation"] == metadata["operation"]
    assert "/" not in repr(binding)
    assert "token" not in repr(binding).lower()
    stages = _stage_records(result)
    assert tuple(item["name"] for item in stages) == expected_names
    assert all(set(item) == {"name", "cpu_seconds", "cuda"} for item in stages)
    assert all(item["cuda"] is None for item in stages)
    assert all(math.isfinite(value) and value >= 0 for value in result.profile.metrics.values())
    assert not any(name.startswith("controlfoley.cuda.") for name in result.profile.metrics)


def test_default_profile_is_empty_and_does_not_read_the_clock() -> None:
    clock = StepClock()
    adapter = ControlFoleyAdapter(
        runner=RunnerDouble(),
        profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.OFF, monotonic_ns=clock),
    )
    result = _invoke_upstream(adapter)
    assert result.profile.metrics == {}
    assert result.profile.metadata == {}
    assert clock.calls == 0
    assert adapter.descriptor.capabilities["profile_default"] == "off"
    assert adapter.descriptor.capabilities["profile_levels"] == (
        "off",
        "invocation",
        "stages",
    )


@pytest.mark.parametrize(
    ("level", "expected_names"),
    (
        (ControlFoleyProfileLevel.INVOCATION, ("invocation",)),
        (ControlFoleyProfileLevel.STAGES, (UPSTREAM_PARITY_OPERATION,)),
    ),
)
def test_upstream_cpu_profile_levels_are_truthful_and_cuda_unavailable(
    level: ControlFoleyProfileLevel, expected_names: tuple[str, ...]
) -> None:
    capability = UnavailableCudaBackend()
    result = _invoke_upstream(
        ControlFoleyAdapter(
            runner=RunnerDouble(),
            profiler=ControlFoleyProfiler(level, capability, StepClock()),
        )
    )
    _assert_cpu_only_profile(result, expected_names)
    assert result.profile.metadata["operation"] == UPSTREAM_PARITY_OPERATION
    # Parent CUDA events cannot observe the isolated upstream child.
    assert capability.availability_checks == 0
    assert capability.begins == 0


def test_staged_cpu_profile_has_exact_stage_accounting() -> None:
    backend = DeterministicFakeStagedBackend()
    capability = UnavailableCudaBackend()
    result = _invoke_staged(
        ControlFoleyAdapter(
            staged_backend=backend,
            profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES, capability, StepClock()),
        ),
        backend.backend_id,
    )
    expected = tuple(stage.value for stage in CONTROLFOLEY_STAGE_ORDER)
    _assert_cpu_only_profile(result, expected)
    stages = _stage_records(result)
    assert all(item["cpu_seconds"] == 1.0 for item in stages)
    assert result.profile.metrics["controlfoley.cpu.total_seconds"] == 9.0
    assert sum(cast(float, item["cpu_seconds"]) for item in stages) == 4.0
    assert tuple(backend.calls) == CONTROLFOLEY_STAGE_ORDER
    assert capability.availability_checks == 1
    assert capability.begins == 0


def test_default_monotonic_clock_records_real_cpu_observations() -> None:
    result = _invoke_upstream(
        ControlFoleyAdapter(
            runner=RunnerDouble(),
            profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES),
        )
    )
    total = result.profile.metrics["controlfoley.cpu.total_seconds"]
    stage = result.profile.metrics["controlfoley.cpu.upstream_parity.seconds"]
    assert math.isfinite(total) and total >= 0
    assert math.isfinite(stage) and stage >= 0
    assert total >= stage


def test_profiling_does_not_change_upstream_or_staged_artifacts() -> None:
    upstream_plain = _invoke_upstream(ControlFoleyAdapter(runner=RunnerDouble()))
    upstream_profiled = _invoke_upstream(
        ControlFoleyAdapter(
            runner=RunnerDouble(),
            profiler=ControlFoleyProfiler(
                ControlFoleyProfileLevel.STAGES, monotonic_ns=StepClock()
            ),
        )
    )
    assert upstream_profiled.artifacts == upstream_plain.artifacts
    assert upstream_profiled.metadata == upstream_plain.metadata

    plain_backend = DeterministicFakeStagedBackend()
    staged_plain = _invoke_staged(
        ControlFoleyAdapter(staged_backend=plain_backend), plain_backend.backend_id
    )
    profiled_backend = DeterministicFakeStagedBackend()
    staged_profiled = _invoke_staged(
        ControlFoleyAdapter(
            staged_backend=profiled_backend,
            profiler=ControlFoleyProfiler(
                ControlFoleyProfileLevel.STAGES, monotonic_ns=StepClock()
            ),
        ),
        profiled_backend.backend_id,
    )
    assert staged_profiled.artifacts == staged_plain.artifacts
    assert staged_profiled.metadata == staged_plain.metadata


def test_cuda_capability_fault_is_sanitized_and_does_not_change_result() -> None:
    plain_backend = DeterministicFakeStagedBackend()
    plain = _invoke_staged(
        ControlFoleyAdapter(staged_backend=plain_backend), plain_backend.backend_id
    )
    backend = DeterministicFakeStagedBackend()
    result = _invoke_staged(
        ControlFoleyAdapter(
            staged_backend=backend,
            profiler=ControlFoleyProfiler(
                ControlFoleyProfileLevel.STAGES, BrokenCudaBackend(), StepClock()
            ),
        ),
        backend.backend_id,
    )
    assert result.artifacts == plain.artifacts
    assert result.metadata == plain.metadata
    assert result.profile.metadata["status"] == "ok"
    assert result.profile.metadata["cuda"] == {"status": "error", "backend_id": None}
    assert all(item["cuda"] is None for item in _stage_records(result))
    serialized = repr((result.profile.metrics, result.profile.metadata))
    assert "/private/path" not in serialized
    assert "must-not-leak" not in serialized


def test_cpu_clock_fault_is_sanitized_and_does_not_change_result() -> None:
    result = _invoke_upstream(
        ControlFoleyAdapter(
            runner=RunnerDouble(),
            profiler=ControlFoleyProfiler(
                ControlFoleyProfileLevel.STAGES, monotonic_ns=BrokenClock()
            ),
        )
    )
    assert result.artifacts[0].content == b"profile-semantic-stability"
    assert result.profile.metrics == {}
    assert result.profile.metadata["status"] == "error"
    assert result.profile.metadata["reason"] == "cpu_clock_error"
    serialized = repr(result.profile.metadata)
    assert "/private/clock" not in serialized
    assert "must-not-leak" not in serialized


def test_cancel_and_fault_keep_existing_runtime_taxonomy() -> None:
    cancel_backend = DeterministicFakeStagedBackend(
        cancel_stage=ControlFoleyStage.CONDITION_ENCODE_PROJECTION
    )
    cancel_adapter = ControlFoleyAdapter(
        staged_backend=cancel_backend,
        profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES, monotonic_ns=StepClock()),
    )
    runtime = _runtime(cancel_adapter)
    session = runtime.load(_staged_deployment(cancel_adapter, cancel_backend.backend_id))
    with pytest.raises(InvocationCancelledError):
        runtime.invoke(
            session,
            ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation(
                "profile-cancel", operation=EXPERIMENTAL_STAGED_OPERATION
            ),
        )
    assert session.state is SessionState.READY
    runtime.unload(session)

    fault_backend = DeterministicFakeStagedBackend(fail_stage=ControlFoleyStage.INTEGRATE)
    fault_adapter = ControlFoleyAdapter(
        staged_backend=fault_backend,
        profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES, monotonic_ns=StepClock()),
    )
    runtime = _runtime(fault_adapter)
    session = runtime.load(_staged_deployment(fault_adapter, fault_backend.backend_id))
    with pytest.raises(ControlFoleyStageExecutionError) as error:
        runtime.invoke(
            session,
            ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation(
                "profile-fault", operation=EXPERIMENTAL_STAGED_OPERATION
            ),
        )
    assert error.value.stage is ControlFoleyStage.INTEGRATE
    assert session.state is SessionState.FAILED
    runtime.unload(session)


def test_cuda_observation_schema_rejects_nonfinite_negative_or_impossible_values() -> None:
    with pytest.raises(ValueError, match="finite and nonnegative"):
        ControlFoleyCudaObservation(float("nan"), 0, 0, 0, 0)
    with pytest.raises(ValueError, match="nonnegative integer"):
        ControlFoleyCudaObservation(0.0, -1, 0, 0, 0)
    with pytest.raises(ValueError, match="cannot be below"):
        ControlFoleyCudaObservation(0.0, 2, 2, 1, 2)
    with pytest.raises(ValueError, match="reserved_bytes cannot be below"):
        ControlFoleyCudaObservation(0.0, 2, 1, 2, 2)


def test_profile_report_is_bound_to_deployment_backend_and_canonical_invocation() -> None:
    backend = DeterministicFakeStagedBackend()
    result = _invoke_staged(
        ControlFoleyAdapter(
            staged_backend=backend,
            profiler=ControlFoleyProfiler(
                ControlFoleyProfileLevel.STAGES, monotonic_ns=StepClock()
            ),
        ),
        backend.backend_id,
    )
    binding = cast(Mapping[str, object], result.profile.metadata["binding"])
    request = ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird")
    assert binding == {
        "operation": EXPERIMENTAL_STAGED_OPERATION,
        "deployment_fingerprint": _staged_deployment_fingerprint(
            _staged_deployment(
                ControlFoleyAdapter(staged_backend=backend), backend.backend_id
            ).configuration
        ),
        "deployment_manifest_sha256": _digest("manifest"),
        "source_revision": _SOURCE_REVISION,
        "checkpoint_sha256": _digest("checkpoint"),
        "canonical_invocation_sha256": controlfoley_invocation_fingerprint(request),
        "staged_backend_id": backend.backend_id,
    }
    evidence = ControlFoleyGpuProfileEvidence(
        fixture_manifest_sha256=_digest("fixture"),
        artifact_sha256=hashlib.sha256(result.artifacts[0].content).hexdigest(),
        binding=ControlFoleyProfileBinding(**binding),  # type: ignore[arg-type]
        profile=result.profile,
    )
    serialized = json.dumps(_jsonable(evidence.to_dict()), sort_keys=True)
    assert "controlfoley-experimental-profile-evidence" in serialized
    assert "/" not in serialized
    assert "token" not in serialized.lower()

    drifted = ControlFoleyProfileBinding(
        operation=EXPERIMENTAL_STAGED_OPERATION,
        deployment_fingerprint=cast(str, binding["deployment_fingerprint"]),
        deployment_manifest_sha256=cast(str, binding["deployment_manifest_sha256"]),
        source_revision=cast(str, binding["source_revision"]),
        checkpoint_sha256=cast(str, binding["checkpoint_sha256"]),
        canonical_invocation_sha256=_digest("changed invocation"),
        staged_backend_id=backend.backend_id,
    )
    with pytest.raises(ValueError, match="not bound"):
        ControlFoleyGpuProfileEvidence(
            _digest("fixture"),
            hashlib.sha256(result.artifacts[0].content).hexdigest(),
            drifted,
            result.profile,
        )


def test_enabled_profiler_requires_safe_exact_execution_binding() -> None:
    with pytest.raises(ValueError, match="exact execution binding"):
        ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES).begin(EXPERIMENTAL_STAGED_OPERATION)
    with pytest.raises(ValueError, match="backend_id"):
        ControlFoleyProfileBinding(
            EXPERIMENTAL_STAGED_OPERATION,
            _digest("deployment"),
            _digest("manifest"),
            _SOURCE_REVISION,
            _digest("checkpoint"),
            _digest("invocation"),
            "../../private/token",
        )
    with pytest.raises(ValueError, match="cannot name"):
        ControlFoleyProfileBinding(
            UPSTREAM_PARITY_OPERATION,
            _digest("deployment"),
            _digest("manifest"),
            _SOURCE_REVISION,
            _digest("checkpoint"),
            _digest("invocation"),
            "unexpected-backend",
        )


def _gpu_configuration_values(tmp_path: Path) -> dict[str, object]:
    deployment = tmp_path / "deployment.json"
    fixture = tmp_path / "fixture.json"
    video = tmp_path / "video.bin"
    source_dir = tmp_path / "source"
    weights_dir = tmp_path / "weights"
    deployment.write_text("{}", encoding="utf-8")
    fixture.write_text("{}", encoding="utf-8")
    video.write_bytes(b"video")
    source_dir.mkdir()
    weights_dir.mkdir()
    return {
        "deployment": str(deployment),
        "fixture": str(fixture),
        "source_dir": str(source_dir),
        "weights_dir": str(weights_dir),
        "backend_module": "operator.controlfoley_backend",
        "task": "V2A",
        "profile_output": str(tmp_path / "profile.json"),
        "video": str(video),
    }


def test_gpu_profile_configuration_is_exact_task_aware_and_absolute(tmp_path: Path) -> None:
    values = _gpu_configuration_values(tmp_path)
    parsed = ControlFoleyGpuProfileConfiguration.from_dict(values)
    assert parsed.task is ControlFoleyTaskKind.V2A
    assert parsed.video is not None and parsed.video.is_absolute()
    assert parsed.profile_output.is_absolute()

    extra = dict(values, token="must-not-be-accepted")
    with pytest.raises(ValueError, match="missing or unexpected"):
        ControlFoleyGpuProfileConfiguration.from_dict(extra)
    relative = dict(values, deployment="deployment.json")
    with pytest.raises(ValueError, match="absolute"):
        ControlFoleyGpuProfileConfiguration.from_dict(relative)
    unsafe_module = dict(values, backend_module="../operator/backend")
    with pytest.raises(ValueError, match="safe dotted"):
        ControlFoleyGpuProfileConfiguration.from_dict(unsafe_module)
    wrong_task_shape = dict(values, task="T2A")
    with pytest.raises(ValueError, match="missing or unexpected"):
        ControlFoleyGpuProfileConfiguration.from_dict(wrong_task_shape)
    Path(cast(str, values["profile_output"])).write_text("exists", encoding="utf-8")
    with pytest.raises(ValueError, match="absolute new path"):
        ControlFoleyGpuProfileConfiguration.from_dict(values)


def test_gpu_preflight_never_classifies_invalid_seals_as_hardware_skip() -> None:
    unavailable = ControlFoleyGpuProfilePreflight.from_prerequisites(
        GpuPrerequisites(False, ("CUDA is unavailable",))
    )
    assert unavailable.seal_reasons == ()
    with pytest.raises(GpuPrerequisitesUnavailable, match="capability is unavailable"):
        unavailable.require()

    dirty_and_unavailable = ControlFoleyGpuProfilePreflight.from_prerequisites(
        GpuPrerequisites(
            False,
            (
                "ControlFoley source tree is dirty",
                "weight model_weights/controlfoley.ckpt fingerprint does not match the manifest",
                "CUDA is unavailable",
            ),
        )
    )
    assert dirty_and_unavailable.capability_reasons == ("CUDA is unavailable",)
    with pytest.raises(ControlFoleyProfileSealError, match="seals are invalid"):
        dirty_and_unavailable.require()

    valid = ControlFoleyGpuProfilePreflight.from_prerequisites(GpuPrerequisites(True, ()))
    assert valid.seal_reasons == valid.capability_reasons == ()
    valid.require()


def test_profile_module_keeps_core_and_optional_frontend_boundaries() -> None:
    profile_source = Path("src/nano_aural_runtime_controlfoley/profile.py").read_text(
        encoding="utf-8"
    )
    assert "\nimport torch" not in profile_source
    assert "\nfrom torch" not in profile_source
    assert profile_source.count("import torch") == 1
    assert "comfy" not in profile_source.lower()
    assert "cache" not in profile_source.lower()
    core_sources = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src/nano_aural_runtime").glob("*.py")
    )
    assert "ControlFoleyProfile" not in core_sources
    assert "controlfoley.cpu." not in core_sources


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_controlfoley_staged_cuda_profile_when_operator_configured() -> None:
    raw = os.environ.get("CONTROLFOLEY_P4B_GPU_CONFIG")
    if raw is None:
        pytest.skip("CONTROLFOLEY_P4B_GPU_CONFIG is not set; RTX 4090 timing is deferred")
    assert raw is not None
    try:
        config = ControlFoleyGpuProfileConfiguration.from_dict(json.loads(raw))
        deployment_manifest = ControlFoleyDeploymentManifest.from_dict(load_json(config.deployment))
        fixture = FixtureManifest.from_dict(load_json(config.fixture))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        pytest.fail("CONTROLFOLEY_P4B_GPU_CONFIG is invalid: {0}".format(type(error).__name__))
    if fixture.task != config.task.value:
        pytest.fail("fixture task does not match the sealed GPU profile configuration")
    oracle_binding = ControlFoleyOracleDeploymentBinding.from_manifest(deployment_manifest)
    fixture_manifest_sha256 = manifest_sha256(fixture.to_dict())

    request_values: dict[str, object] = {
        "task": config.task,
        "duration_seconds": fixture.duration_seconds,
        "num_steps": fixture.num_steps,
        "guidance_scale": fixture.guidance_scale,
        "seed": fixture.seed,
    }
    if config.video is not None:
        request_values["video_path"] = config.video
        assert next(item for item in fixture.inputs if item.role == "video").fingerprint == (
            fingerprint_file(config.video)
        )
    if config.audio is not None:
        request_values["reference_audio_path"] = config.audio
        assert next(
            item for item in fixture.inputs if item.role == "reference_audio"
        ).fingerprint == fingerprint_file(config.audio)
    if config.prompt is not None:
        request_values["prompt"] = config.prompt
        assert next(item for item in fixture.inputs if item.role == "prompt").fingerprint == (
            fingerprint_text(config.prompt)
        )
    request = ControlFoleyLocalRequest(**request_values)  # type: ignore[arg-type]
    invocation_fingerprint = controlfoley_invocation_fingerprint(request)

    try:
        require_controlfoley_gpu_profile_preflight(
            config.source_dir, config.weights_dir, deployment_manifest
        )
    except ControlFoleyProfileSealError as error:
        pytest.fail(str(error))
    except GpuPrerequisitesUnavailable as error:
        pytest.skip(str(error))
    oracle_binding.assert_manifest(
        ControlFoleyDeploymentManifest.from_dict(load_json(config.deployment))
    )

    module = importlib.import_module(config.backend_module)
    factory = getattr(module, "create_controlfoley_staged_backend", None)
    if not callable(factory):
        pytest.fail("backend_module must expose create_controlfoley_staged_backend()")
    assert callable(factory)
    backend = cast(ControlFoleyStagedBackend, factory())
    try:
        cuda = TorchCudaProfileBackend()
    except RuntimeError as error:
        pytest.skip(str(error))
    if not cuda.is_available():
        pytest.skip("actual CUDA capability is unavailable")
    adapter = ControlFoleyAdapter(
        staged_backend=backend,
        profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES, cuda),
    )
    runtime = _runtime(adapter)
    candidate_deployment = controlfoley_staged_local_deployment(
        adapter,
        config.deployment,
        config.source_dir,
        config.weights_dir,
        backend.backend_id,
    )
    oracle_binding.assert_candidate_configuration(candidate_deployment.configuration)
    oracle_binding.assert_manifest(
        ControlFoleyDeploymentManifest.from_dict(load_json(config.deployment))
    )
    session = runtime.load(candidate_deployment)
    try:
        result = runtime.invoke(
            session,
            request.to_invocation("gpu-profile", operation=EXPERIMENTAL_STAGED_OPERATION),
        )
        # Re-read every mutable external seal before evidence becomes visible.
        post_manifest = ControlFoleyDeploymentManifest.from_dict(load_json(config.deployment))
        oracle_binding.assert_manifest(post_manifest)
        oracle_binding.assert_candidate_configuration(session.deployment.configuration)
        try:
            require_controlfoley_gpu_profile_preflight(
                config.source_dir, config.weights_dir, post_manifest
            )
        except (ControlFoleyProfileSealError, GpuPrerequisitesUnavailable) as error:
            pytest.fail("profile prerequisites changed during execution: {0}".format(error))
        if manifest_sha256(FixtureManifest.from_dict(load_json(config.fixture)).to_dict()) != (
            fixture_manifest_sha256
        ):
            pytest.fail("fixture manifest changed during the profiled invocation")
        if controlfoley_invocation_fingerprint(request) != invocation_fingerprint:
            pytest.fail("profile invocation inputs changed during execution")
    finally:
        runtime.unload(session)
    assert result.profile.metadata["status"] == "ok"
    assert result.profile.metadata["cuda"] == {
        "status": "available",
        "backend_id": cuda.backend_id,
    }
    assert tuple(item["name"] for item in _stage_records(result)) == tuple(
        stage.value for stage in CONTROLFOLEY_STAGE_ORDER
    )
    assert all(item["cuda"] is not None for item in _stage_records(result))
    assert all(math.isfinite(value) and value >= 0 for value in result.profile.metrics.values())
    expected_binding = ControlFoleyProfileBinding(
        operation=EXPERIMENTAL_STAGED_OPERATION,
        deployment_fingerprint=candidate_deployment.fingerprint,
        deployment_manifest_sha256=oracle_binding.deployment_manifest_sha256,
        source_revision=oracle_binding.source_revision,
        checkpoint_sha256=oracle_binding.checkpoint_sha256,
        canonical_invocation_sha256=invocation_fingerprint,
        staged_backend_id=backend.backend_id,
    )
    assert dict(cast(Mapping[str, object], result.profile.metadata["binding"])) == dict(
        expected_binding.to_dict()
    )
    evidence = ControlFoleyGpuProfileEvidence(
        fixture_manifest_sha256=fixture_manifest_sha256,
        artifact_sha256=hashlib.sha256(result.artifacts[0].content).hexdigest(),
        binding=expected_binding,
        profile=result.profile,
    )
    with config.profile_output.open("x", encoding="utf-8") as handle:
        json.dump(_jsonable(evidence.to_dict()), handle, sort_keys=True)
        handle.write("\n")
