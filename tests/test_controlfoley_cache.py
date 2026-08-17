"""Roadmap Phase 4C experimental ControlFoley L0/L1 cache contracts."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional, cast

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime import (
    AdapterRegistry,
    InvocationCancelledError,
    InvocationRejectedError,
    ModelDeployment,
    Runtime,
    SessionState,
)
from nano_aural_runtime_controlfoley.adapter import (
    ControlFoleyAdapter,
    _staged_deployment_fingerprint,
    controlfoley_staged_cached_local_deployment,
    controlfoley_staged_local_deployment,
)
from nano_aural_runtime_controlfoley.baseline import (
    ControlFoleyDeploymentManifest,
    FixtureManifest,
    GpuPrerequisitesUnavailable,
    fingerprint_file,
    fingerprint_text,
    load_json,
    manifest_sha256,
)
from nano_aural_runtime_controlfoley.cache import (
    EXPERIMENTAL_L0_L1_CACHE_MODE,
    ControlFoleyCacheEntry,
    ControlFoleyCachePolicy,
    ControlFoleyCacheSnapshot,
    ControlFoleyGpuCacheConfiguration,
    ControlFoleyL0L1Cache,
    ControlFoleyMemoryCacheStore,
)
from nano_aural_runtime_controlfoley.profile import (
    ControlFoleyProfileLevel,
    ControlFoleyProfiler,
    ControlFoleyProfileSealError,
    require_controlfoley_gpu_profile_preflight,
)
from nano_aural_runtime_controlfoley.staged import (
    CONTROLFOLEY_STAGE_ORDER,
    ControlFoleyOracleDeploymentBinding,
    ControlFoleyStage,
    ControlFoleyStagedBackend,
    ControlFoleyStagedBackendSession,
    ControlFoleyStagedBackendValidation,
    ControlFoleyStageExecutionError,
    DeterministicFakeStagedBackend,
    controlfoley_invocation_fingerprint,
)
from nano_aural_runtime_controlfoley.tasks import (
    EXPERIMENTAL_STAGED_OPERATION,
    ControlFoleyLocalRequest,
    ControlFoleyTaskKind,
)

_SOURCE_REVISION = "6858cd12a48d141201e3266e7abe1f38357a133e"


def _digest(label: str) -> str:
    return hashlib.sha256(label.encode("utf-8")).hexdigest()


def _policy(**changes: object) -> ControlFoleyCachePolicy:
    values: dict[str, object] = {
        "preprocess_version": "media-v1",
        "code_version": "cache-code-v1",
        "max_entries": 16,
        "max_entry_bytes": 4096,
        "max_total_bytes": 16_384,
        "ttl_seconds": 60.0,
    }
    values.update(changes)
    return ControlFoleyCachePolicy(**values)  # type: ignore[arg-type]


def _configuration(
    backend_id: str, policy: Optional[ControlFoleyCachePolicy] = None
) -> dict[str, str]:
    values = {
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
    if policy is not None:
        values.update(policy.configuration())
    values["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(values)
    return values


def _deployment(
    adapter: ControlFoleyAdapter,
    backend_id: str,
    policy: Optional[ControlFoleyCachePolicy] = None,
) -> ModelDeployment:
    configuration = _configuration(backend_id, policy)
    return ModelDeployment(
        "controlfoley-cache-cpu-test",
        adapter.descriptor,
        configuration["staged_deployment_fingerprint"],
        configuration,
    )


def _runtime(adapter: ControlFoleyAdapter) -> Runtime:
    registry = AdapterRegistry()
    registry.register(adapter)
    return Runtime(registry)


def _t2a(prompt: str = "bird wing", duration: float = 8.0, seed: int = 42):  # type: ignore[no-untyped-def]
    return ControlFoleyLocalRequest(
        ControlFoleyTaskKind.T2A,
        prompt=prompt,
        duration_seconds=duration,
        seed=seed,
    )


def _invoke(runtime: Runtime, session, request, invocation_id: str):  # type: ignore[no-untyped-def]
    return runtime.invoke(
        session,
        request.to_invocation(invocation_id, operation=EXPERIMENTAL_STAGED_OPERATION),
    )


def test_default_is_off_and_cached_deployment_is_explicit() -> None:
    policy = _policy()
    cache = ControlFoleyL0L1Cache(policy)
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend, cache=cache)
    assert adapter.descriptor.capabilities["cache_default"] == "off"
    assert adapter.descriptor.capabilities["cache_modes"] == (
        "off",
        EXPERIMENTAL_L0_L1_CACHE_MODE,
        "experimental_l2_condition_features",
    )
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id))
    first = _invoke(runtime, session, _t2a(), "off-1")
    second = _invoke(runtime, session, _t2a(), "off-2")
    assert first.cache.metadata == second.cache.metadata == {}
    assert backend.calls.count(ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS) == 2
    runtime.unload(session)

    missing = ControlFoleyAdapter(staged_backend=DeterministicFakeStagedBackend())
    with pytest.raises(InvocationRejectedError, match="explicit adapter cache"):
        _runtime(missing).load(_deployment(missing, "deterministic-cpu-test-double-v1", policy))


class _NoCodecBackend:
    backend_id = "no-l1-codec-v1"

    def __init__(self) -> None:
        self.delegate = DeterministicFakeStagedBackend()
        self.loaded = 0

    def load(
        self, configuration: Mapping[str, Any]
    ) -> tuple[ControlFoleyStagedBackendSession, ControlFoleyStagedBackendValidation]:
        self.loaded += 1
        session, validation = self.delegate.load(configuration)
        return (
            ControlFoleyStagedBackendSession(
                self.backend_id, session.deployment_fingerprint, session.payload
            ),
            ControlFoleyStagedBackendValidation(
                self.backend_id,
                validation.deployment_manifest_sha256,
                validation.source_revision,
                validation.variant,
                validation.precision,
                validation.checkpoint_sha256,
                validation.implementation,
            ),
        )

    def run_stage(self, session, stage, previous, request, context):  # type: ignore[no-untyped-def]
        return self.delegate.run_stage(session, stage, previous, request, context)

    def unload(self, session: ControlFoleyStagedBackendSession) -> None:
        self.delegate.unload(session)


def test_cached_load_rejects_policy_drift_and_backend_without_codec_before_load() -> None:
    policy = _policy()
    backend = _NoCodecBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend, cache=ControlFoleyL0L1Cache(policy))
    with pytest.raises(InvocationRejectedError, match="safe-bytes codec"):
        _runtime(adapter).load(_deployment(adapter, backend.backend_id, policy))
    assert backend.loaded == 0

    real_backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(
        staged_backend=real_backend, cache=ControlFoleyL0L1Cache(_policy(code_version="v2"))
    )
    with pytest.raises(InvocationRejectedError, match="does not match"):
        _runtime(adapter).load(_deployment(adapter, real_backend.backend_id, policy))
    assert real_backend.loaded == 0


def test_cold_warm_cache_is_truthful_and_artifact_equivalent() -> None:
    policy = _policy()
    backend = DeterministicFakeStagedBackend()
    cache = ControlFoleyL0L1Cache(policy)
    adapter = ControlFoleyAdapter(staged_backend=backend, cache=cache)
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    cold = _invoke(runtime, session, _t2a(), "cold")
    warm = _invoke(runtime, session, _t2a(), "warm")

    assert (cold.artifacts[0].name, cold.artifacts[0].media_type, cold.artifacts[0].content) == (
        warm.artifacts[0].name,
        warm.artifacts[0].media_type,
        warm.artifacts[0].content,
    )
    assert cold.cache.hits == 0 and cold.cache.misses == 2
    assert cold.cache.metadata["writes"] == 2
    assert warm.cache.hits == 2 and warm.cache.misses == 0
    assert warm.cache.metadata["levels"] == {
        "l0_metadata": "hit",
        "l1_preprocess": "hit",
    }
    assert warm.cache.bytes_used > 0
    assert backend.calls.count(ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS) == 1
    for stage in CONTROLFOLEY_STAGE_ORDER[1:]:
        assert backend.calls.count(stage) == 2
    assert cold.artifacts[0].metadata["stage_order"] == warm.artifacts[0].metadata["stage_order"]
    serialized = repr(warm.cache.metadata)
    assert "/" not in serialized and "token" not in serialized.lower()

    off_backend = DeterministicFakeStagedBackend()
    off_adapter = ControlFoleyAdapter(staged_backend=off_backend)
    off_runtime = _runtime(off_adapter)
    off_session = off_runtime.load(_deployment(off_adapter, off_backend.backend_id))
    off = _invoke(off_runtime, off_session, _t2a(), "off-equivalence")
    assert off.artifacts[0].content == warm.artifacts[0].content
    off_runtime.unload(off_session)
    runtime.unload(session)
    assert cache.invalidate_all() == 0


def test_warm_cache_keeps_exact_profile_stage_accounting() -> None:
    policy = _policy()
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(
        staged_backend=backend,
        cache=ControlFoleyL0L1Cache(policy),
        profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES),
    )
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    _invoke(runtime, session, _t2a(), "profile-cold")
    warm = _invoke(runtime, session, _t2a(), "profile-warm")
    assert tuple(item["name"] for item in warm.profile.metadata["stages"]) == tuple(
        stage.value for stage in CONTROLFOLEY_STAGE_ORDER
    )
    runtime.unload(session)


def test_cache_key_mutates_for_every_semantic_binding(tmp_path: Path) -> None:
    policy = _policy()
    cache = ControlFoleyL0L1Cache(policy)
    configuration = _configuration("deterministic-cpu-test-double-v1", policy)
    deployment_fingerprint = configuration["staged_deployment_fingerprint"]
    base = cache.key_for(_t2a(), configuration, deployment_fingerprint)
    mutations = (
        cache.key_for(_t2a(prompt="owl"), configuration, deployment_fingerprint),
        cache.key_for(_t2a(duration=10.0), configuration, deployment_fingerprint),
        cache.key_for(_t2a(seed=0), configuration, deployment_fingerprint),
        replace(base, num_steps=26),
        replace(base, guidance_scale=4.6),
        replace(base, preprocess_version="media-v2"),
        replace(base, code_version="cache-code-v2"),
        replace(base, source_revision="1" * 40),
        replace(base, deployment_fingerprint=_digest("another deployment")),
        replace(base, deployment_manifest_sha256=_digest("another manifest")),
        replace(base, checkpoint_sha256=_digest("another checkpoint")),
        replace(base, variant="another-variant"),
        replace(base, precision="fp16"),
        replace(base, backend_id="another-safe-backend"),
    )
    assert all(value.digest != base.digest for value in mutations)

    video = tmp_path / "video.bin"
    video.write_bytes(b"first")
    first_request = ControlFoleyLocalRequest(ControlFoleyTaskKind.V2A, video_path=video)
    first = cache.key_for(first_request, configuration, deployment_fingerprint)
    video.write_bytes(b"second")
    second = cache.key_for(first_request, configuration, deployment_fingerprint)
    assert first.digest != second.digest
    task_mutation = cache.key_for(
        ControlFoleyLocalRequest(
            ControlFoleyTaskKind.TV2A,
            video_path=video,
            prompt="bird wing",
        ),
        configuration,
        deployment_fingerprint,
    )
    assert task_mutation.task != base.task
    assert task_mutation.digest != base.digest
    assert "/" not in repr(first.to_dict())


def _gpu_cache_configuration_values(tmp_path: Path) -> dict[str, object]:
    deployment = tmp_path / "deployment.json"
    fixture = tmp_path / "fixture.json"
    video = tmp_path / "video.bin"
    source = tmp_path / "source"
    weights = tmp_path / "weights"
    deployment.write_text("{}", encoding="utf-8")
    fixture.write_text("{}", encoding="utf-8")
    video.write_bytes(b"video")
    source.mkdir()
    weights.mkdir()
    return {
        "deployment": str(deployment),
        "fixture": str(fixture),
        "source_dir": str(source),
        "weights_dir": str(weights),
        "backend_module": "operator.controlfoley_backend",
        "task": "V2A",
        "preprocess_version": "operator-media-v1",
        "code_version": "cache-code-v1",
        "evidence_output": str(tmp_path / "evidence.json"),
        "video": str(video),
    }


def test_gpu_cache_configuration_is_exact_task_aware_and_absolute(tmp_path: Path) -> None:
    values = _gpu_cache_configuration_values(tmp_path)
    parsed = ControlFoleyGpuCacheConfiguration.from_dict(values)
    assert parsed.task is ControlFoleyTaskKind.V2A
    assert parsed.video is not None and parsed.video.is_absolute()
    with pytest.raises(ValueError, match="missing or unexpected"):
        ControlFoleyGpuCacheConfiguration.from_dict(dict(values, token="secret"))
    with pytest.raises(ValueError, match="absolute"):
        ControlFoleyGpuCacheConfiguration.from_dict(dict(values, fixture="fixture.json"))
    with pytest.raises(ValueError, match="safe dotted"):
        ControlFoleyGpuCacheConfiguration.from_dict(dict(values, backend_module="../operator"))
    with pytest.raises(ValueError, match="missing or unexpected"):
        ControlFoleyGpuCacheConfiguration.from_dict(dict(values, task="T2A"))


class _Clock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        return self.value


def test_memory_lru_enforces_entry_item_total_ttl_and_immutable_races() -> None:
    clock = _Clock()
    policy = _policy(
        max_entries=2,
        max_entry_bytes=4,
        max_total_bytes=6,
        ttl_seconds=5.0,
    )
    store = ControlFoleyMemoryCacheStore(policy, monotonic=clock)
    deployment = _digest("deployment")
    keys = tuple(_digest("key-{0}".format(index)) for index in range(3))
    assert store.put_if_absent("l1_preprocess", keys[0], deployment, b"aaa") == "stored"
    assert store.put_if_absent("l1_preprocess", keys[1], deployment, b"bbb") == "stored"
    assert store.get_entry("l1_preprocess", keys[0]) is not None
    assert store.put_if_absent("l1_preprocess", keys[2], deployment, b"cc") == "stored"
    assert store.get_entry("l1_preprocess", keys[1]) is None
    assert store.snapshot() == ControlFoleyCacheSnapshot(2, 5, 1)
    assert store.put_if_absent("l1_preprocess", _digest("large"), deployment, b"12345") == (
        "rejected"
    )

    clock.value = 6.0
    assert store.get_entry("l1_preprocess", keys[0]) is None

    race_key = _digest("race")
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = tuple(
            executor.map(
                lambda index: store.put_if_absent(
                    "l0_metadata", race_key, deployment, bytes([index + 1])
                ),
                range(8),
            )
        )
    assert outcomes.count("stored") == 1
    assert outcomes.count("present") == 7
    assert store.get_entry("l0_metadata", race_key) is not None


class _CorruptingStore(ControlFoleyMemoryCacheStore):
    corrupt_l1 = False
    deleted_l1 = False

    def get_entry(self, tier: str, key_digest: str) -> Optional[ControlFoleyCacheEntry]:
        entry = super().get_entry(tier, key_digest)
        if entry is not None and tier == "l1_preprocess" and self.corrupt_l1:
            return replace(entry, payload=b"corrupted")
        return entry

    def delete(self, tier: str, key_digest: str) -> None:
        if tier == "l1_preprocess":
            self.deleted_l1 = True
        super().delete(tier, key_digest)


def test_corruption_is_quarantined_as_miss_then_cold_computed() -> None:
    policy = _policy()
    store = _CorruptingStore(policy)
    cache = ControlFoleyL0L1Cache(policy, store)
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend, cache=cache)
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    first = _invoke(runtime, session, _t2a(), "first")
    store.corrupt_l1 = True
    second = _invoke(runtime, session, _t2a(), "second")
    assert second.artifacts[0].content == first.artifacts[0].content
    assert second.cache.metadata["levels"] == {
        "l0_metadata": "hit",
        "l1_preprocess": "corrupt",
    }
    assert second.cache.metadata["status"] == "degraded"
    assert store.deleted_l1
    assert backend.calls.count(ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS) == 2
    runtime.unload(session)


class _FailingStore:
    def get_entry(self, tier: str, key_digest: str) -> None:
        del tier, key_digest
        raise OSError("/private/cache token=must-not-leak")

    def put_if_absent(self, tier, key_digest, deployment_fingerprint, payload):  # type: ignore[no-untyped-def]
        del tier, key_digest, deployment_fingerprint, payload
        raise OSError("put failed")

    def delete(self, tier: str, key_digest: str) -> None:
        del tier, key_digest
        raise OSError("delete failed")

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        del deployment_fingerprint
        raise OSError("invalidate failed")

    def invalidate_all(self) -> int:
        raise OSError("invalidate failed")

    def snapshot(self) -> ControlFoleyCacheSnapshot:
        raise OSError("snapshot failed")

    def close(self) -> None:
        raise OSError("close failed")


def test_cache_store_faults_are_path_free_and_do_not_change_model_result() -> None:
    policy = _policy()
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(
        staged_backend=backend,
        cache=ControlFoleyL0L1Cache(policy, _FailingStore()),
    )
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    result = _invoke(runtime, session, _t2a(), "store-fault")
    assert result.artifacts[0].content
    assert result.cache.metadata["status"] == "degraded"
    assert result.cache.metadata["reason"] == "store_fault"
    serialized = repr(result.cache.metadata)
    assert "/private" not in serialized and "must-not-leak" not in serialized
    runtime.unload(session)


class _CodecFaultBackend(DeterministicFakeStagedBackend):
    fail_import = False
    fail_export = False

    def import_preprocess_cache(self, session, payload, request, context):  # type: ignore[no-untyped-def]
        if self.fail_import:
            raise ValueError("unsafe cached bytes")
        return super().import_preprocess_cache(session, payload, request, context)

    def export_preprocess_cache(self, session, value, request):  # type: ignore[no-untyped-def]
        if self.fail_export:
            raise ValueError("codec failed")
        return super().export_preprocess_cache(session, value, request)


def test_codec_import_and_export_faults_bypass_cache_without_artifact_drift() -> None:
    policy = _policy()
    backend = _CodecFaultBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend, cache=ControlFoleyL0L1Cache(policy))
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    good = _invoke(runtime, session, _t2a(), "good")
    backend.fail_import = True
    fallback = _invoke(runtime, session, _t2a(), "import-fault")
    assert fallback.artifacts[0].content == good.artifacts[0].content
    assert fallback.cache.metadata["status"] == "degraded"
    assert backend.calls.count(ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS) == 2
    runtime.unload(session)

    export_backend = _CodecFaultBackend()
    export_backend.fail_export = True
    export_adapter = ControlFoleyAdapter(
        staged_backend=export_backend, cache=ControlFoleyL0L1Cache(policy)
    )
    export_runtime = _runtime(export_adapter)
    export_session = export_runtime.load(
        _deployment(export_adapter, export_backend.backend_id, policy)
    )
    exported = _invoke(export_runtime, export_session, _t2a(), "export-fault")
    assert exported.artifacts[0].content == good.artifacts[0].content
    assert exported.cache.metadata["reason"] == "codec_fault"
    export_runtime.unload(export_session)


@pytest.mark.parametrize("mode", ("cancel", "fault"))
def test_cancel_or_model_fault_never_commits_partial_cache(mode: str) -> None:
    policy = _policy()
    backend = DeterministicFakeStagedBackend(
        cancel_stage=(ControlFoleyStage.INTEGRATE if mode == "cancel" else None),
        fail_stage=(ControlFoleyStage.INTEGRATE if mode == "fault" else None),
    )
    store = ControlFoleyMemoryCacheStore(policy)
    adapter = ControlFoleyAdapter(
        staged_backend=backend, cache=ControlFoleyL0L1Cache(policy, store)
    )
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    error = InvocationCancelledError if mode == "cancel" else ControlFoleyStageExecutionError
    with pytest.raises(error):
        _invoke(runtime, session, _t2a(), mode)
    assert store.snapshot().entries == 0
    assert session.state is (SessionState.READY if mode == "cancel" else SessionState.FAILED)
    runtime.unload(session)


class _MutatingInputBackend(DeterministicFakeStagedBackend):
    def run_stage(self, session, stage, previous, request, context):  # type: ignore[no-untyped-def]
        value = super().run_stage(session, stage, previous, request, context)
        if stage is ControlFoleyStage.INTEGRATE and request.video_path is not None:
            request.video_path.write_bytes(b"mutated-during-model-execution")
        return value


def test_input_toctou_rejects_result_and_never_commits(tmp_path: Path) -> None:
    video = tmp_path / "video.bin"
    video.write_bytes(b"sealed-before-execution")
    request = ControlFoleyLocalRequest(ControlFoleyTaskKind.V2A, video_path=video)
    policy = _policy()
    store = ControlFoleyMemoryCacheStore(policy)
    backend = _MutatingInputBackend()
    adapter = ControlFoleyAdapter(
        staged_backend=backend, cache=ControlFoleyL0L1Cache(policy, store)
    )
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    with pytest.raises(InvocationRejectedError, match="inputs changed"):
        _invoke(runtime, session, request, "toctou")
    assert store.snapshot().entries == 0
    assert session.state is SessionState.READY
    runtime.unload(session)


def test_explicit_invalidation_and_unload_cleanup() -> None:
    policy = _policy()
    store = ControlFoleyMemoryCacheStore(policy)
    cache = ControlFoleyL0L1Cache(policy, store)
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend, cache=cache)
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    _invoke(runtime, session, _t2a(), "fill")
    assert store.snapshot().entries == 2
    assert adapter.invalidate_cache(session.deployment.fingerprint) == 2
    assert store.snapshot().entries == 0
    _invoke(runtime, session, _t2a(), "refill")
    assert store.snapshot().entries == 2
    runtime.unload(session)
    assert store.snapshot().entries == 0
    adapter.close_cache()
    assert adapter.invalidate_cache() == 0


class _InvalidationFailingStore(ControlFoleyMemoryCacheStore):
    def __init__(self, policy: ControlFoleyCachePolicy, failure: str) -> None:
        super().__init__(policy)
        self.failure = failure
        self.reads = 0

    def get_entry(self, tier: str, key_digest: str) -> Optional[ControlFoleyCacheEntry]:
        self.reads += 1
        return super().get_entry(tier, key_digest)

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        if self.failure == "deployment":
            raise OSError("deployment invalidation failed")
        return super().invalidate_deployment(deployment_fingerprint)

    def invalidate_all(self) -> int:
        if self.failure == "all":
            raise OSError("global invalidation failed")
        return super().invalidate_all()

    def close(self) -> None:
        if self.failure == "close":
            raise OSError("cache close failed")
        super().close()


@pytest.mark.parametrize("failure", ("deployment", "all", "close"))
def test_failed_invalidation_or_close_fail_closed_without_stale_hits(failure: str) -> None:
    policy = _policy()
    store = _InvalidationFailingStore(policy, failure)
    cache = ControlFoleyL0L1Cache(policy, store)
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend, cache=cache)
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, backend.backend_id, policy))
    request = _t2a()
    filled = _invoke(runtime, session, request, "fill-before-failed-invalidation")
    key = cache.key_for(request, session.deployment.configuration, session.deployment.fingerprint)
    assert filled.cache.metadata["writes"] == 2
    assert store.get_entry("l1_preprocess", key.digest) is not None

    if failure == "deployment":
        assert adapter.invalidate_cache(session.deployment.fingerprint) == 0
    elif failure == "all":
        assert adapter.invalidate_cache() == 0
    else:
        adapter.close_cache()

    # The deliberately broken store retained the old entry. The facade must
    # nevertheless stop touching it and run the model path cold/unavailable.
    assert store.get_entry("l1_preprocess", key.digest) is not None
    reads_before = store.reads
    fallback = _invoke(runtime, session, request, "after-failed-invalidation")
    assert store.reads == reads_before
    assert fallback.artifacts[0].content == filled.artifacts[0].content
    assert fallback.cache.hits == 0
    assert fallback.cache.metadata["levels"] == {
        "l0_metadata": "unavailable",
        "l1_preprocess": "unavailable",
    }
    assert fallback.cache.metadata["status"] == "degraded"
    assert fallback.cache.metadata["reason"] == "store_fault"
    assert backend.calls.count(ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS) == 2
    runtime.unload(session)


def test_explicit_invalidation_fences_an_inflight_delayed_commit() -> None:
    policy = _policy()
    store = ControlFoleyMemoryCacheStore(policy)
    cache = ControlFoleyL0L1Cache(policy, store)
    configuration = _configuration("deterministic-cpu-test-double-v1", policy)
    request = _t2a()
    fingerprint = configuration["staged_deployment_fingerprint"]
    transaction = cache.begin(request, configuration, fingerprint)
    transaction.stage_preprocess(b"preprocessed")
    assert cache.invalidate_all() == 0
    report = cache.commit(transaction, request, configuration, fingerprint)
    assert report.metadata["reason"] == "invalidated"
    assert report.metadata["writes"] == 0
    assert store.snapshot().entries == 0


def test_cache_module_has_no_l2_solver_disk_or_architecture_boundary_leaks() -> None:
    source = Path("src/nano_aural_runtime_controlfoley/cache.py").read_text(encoding="utf-8")
    lowered = source.lower()
    for forbidden in (
        "torch",
        "pickle",
        "safetensors",
        "flow_matching",
        "latent",
        "comfy",
        "fastapi",
        "postgres",
    ):
        assert forbidden not in lowered
    core = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src/nano_aural_runtime").glob("*.py")
    )
    assert "ControlFoleyCache" not in core


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_controlfoley_l0_l1_cache_equivalence_when_operator_configured() -> None:
    raw = os.environ.get("CONTROLFOLEY_P4C_GPU_CONFIG")
    if raw is None:
        pytest.skip("CONTROLFOLEY_P4C_GPU_CONFIG is not set; RTX 4090 evidence is deferred")
    assert raw is not None
    try:
        configuration = ControlFoleyGpuCacheConfiguration.from_dict(json.loads(raw))
        deployment_manifest = ControlFoleyDeploymentManifest.from_dict(
            load_json(configuration.deployment)
        )
        fixture = FixtureManifest.from_dict(load_json(configuration.fixture))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        pytest.fail("CONTROLFOLEY_P4C_GPU_CONFIG is invalid: {0}".format(type(error).__name__))
    if fixture.task != configuration.task.value:
        pytest.fail("fixture task does not match the sealed GPU cache configuration")

    request_values: dict[str, object] = {
        "task": configuration.task,
        "duration_seconds": fixture.duration_seconds,
        "num_steps": fixture.num_steps,
        "guidance_scale": fixture.guidance_scale,
        "seed": fixture.seed,
    }
    if configuration.video is not None:
        request_values["video_path"] = configuration.video
        assert next(item for item in fixture.inputs if item.role == "video").fingerprint == (
            fingerprint_file(configuration.video)
        )
    if configuration.audio is not None:
        request_values["reference_audio_path"] = configuration.audio
        assert next(
            item for item in fixture.inputs if item.role == "reference_audio"
        ).fingerprint == fingerprint_file(configuration.audio)
    if configuration.prompt is not None:
        request_values["prompt"] = configuration.prompt
        assert next(item for item in fixture.inputs if item.role == "prompt").fingerprint == (
            fingerprint_text(configuration.prompt)
        )
    request = ControlFoleyLocalRequest(**request_values)  # type: ignore[arg-type]
    invocation_sha256 = controlfoley_invocation_fingerprint(request)
    fixture_sha256 = manifest_sha256(fixture.to_dict())
    oracle_binding = ControlFoleyOracleDeploymentBinding.from_manifest(deployment_manifest)
    try:
        require_controlfoley_gpu_profile_preflight(
            configuration.source_dir, configuration.weights_dir, deployment_manifest
        )
    except ControlFoleyProfileSealError as error:
        pytest.fail(str(error))
    except GpuPrerequisitesUnavailable as error:
        pytest.skip(str(error))

    module = importlib.import_module(configuration.backend_module)
    factory = getattr(module, "create_controlfoley_staged_backend", None)
    if not callable(factory):
        pytest.fail("backend_module must expose create_controlfoley_staged_backend()")
    assert callable(factory)
    uncached_backend = cast(ControlFoleyStagedBackend, factory())
    cached_backend = cast(ControlFoleyStagedBackend, factory())
    if uncached_backend.backend_id != cached_backend.backend_id:
        pytest.fail("operator backend factory returned inconsistent identities")

    uncached_adapter = ControlFoleyAdapter(staged_backend=uncached_backend)
    uncached_deployment = controlfoley_staged_local_deployment(
        uncached_adapter,
        configuration.deployment,
        configuration.source_dir,
        configuration.weights_dir,
        uncached_backend.backend_id,
    )
    oracle_binding.assert_candidate_configuration(uncached_deployment.configuration)
    uncached_runtime = _runtime(uncached_adapter)
    uncached_session = uncached_runtime.load(uncached_deployment)
    try:
        uncached = _invoke(uncached_runtime, uncached_session, request, "gpu-cache-off")
    finally:
        uncached_runtime.unload(uncached_session)

    policy = ControlFoleyCachePolicy(
        preprocess_version=configuration.preprocess_version,
        code_version=configuration.code_version,
    )
    cache = ControlFoleyL0L1Cache(policy)
    cached_adapter = ControlFoleyAdapter(staged_backend=cached_backend, cache=cache)
    cached_deployment = controlfoley_staged_cached_local_deployment(
        cached_adapter,
        configuration.deployment,
        configuration.source_dir,
        configuration.weights_dir,
        cached_backend.backend_id,
        policy,
    )
    oracle_binding.assert_candidate_configuration(cached_deployment.configuration)
    cached_runtime = _runtime(cached_adapter)
    cached_session = cached_runtime.load(cached_deployment)
    try:
        cold = _invoke(cached_runtime, cached_session, request, "gpu-cache-cold")
        warm = _invoke(cached_runtime, cached_session, request, "gpu-cache-warm")
        post_manifest = ControlFoleyDeploymentManifest.from_dict(
            load_json(configuration.deployment)
        )
        oracle_binding.assert_manifest(post_manifest)
        oracle_binding.assert_candidate_configuration(cached_session.deployment.configuration)
        try:
            require_controlfoley_gpu_profile_preflight(
                configuration.source_dir, configuration.weights_dir, post_manifest
            )
        except (ControlFoleyProfileSealError, GpuPrerequisitesUnavailable) as error:
            pytest.fail("cache prerequisites changed during execution: {0}".format(error))
        if manifest_sha256(
            FixtureManifest.from_dict(load_json(configuration.fixture)).to_dict()
        ) != (fixture_sha256):
            pytest.fail("fixture manifest changed during cache equivalence execution")
        if controlfoley_invocation_fingerprint(request) != invocation_sha256:
            pytest.fail("cache equivalence invocation inputs changed during execution")
    finally:
        cached_runtime.unload(cached_session)

    artifacts = (uncached.artifacts[0], cold.artifacts[0], warm.artifacts[0])
    assert len({artifact.content for artifact in artifacts}) == 1
    assert len({(artifact.name, artifact.media_type) for artifact in artifacts}) == 1
    assert cold.cache.hits == 0 and cold.cache.misses == 2
    assert warm.cache.hits == 2 and warm.cache.misses == 0
    assert all(
        math.isfinite(value) and value >= 0
        for value in (float(cold.cache.bytes_used), float(warm.cache.bytes_used))
    )
    uncached_sha256 = hashlib.sha256(uncached.artifacts[0].content).hexdigest()
    cold_sha256 = hashlib.sha256(cold.artifacts[0].content).hexdigest()
    warm_sha256 = hashlib.sha256(warm.artifacts[0].content).hexdigest()
    evidence = {
        "schema_version": 1,
        "kind": "controlfoley-experimental-l0-l1-cache-equivalence",
        "state": "measured_exact_output_match",
        "deployment_manifest_sha256": oracle_binding.deployment_manifest_sha256,
        "source_revision": oracle_binding.source_revision,
        "checkpoint_sha256": oracle_binding.checkpoint_sha256,
        "fixture_manifest_sha256": fixture_sha256,
        "canonical_invocation_sha256": invocation_sha256,
        "backend_id": cached_backend.backend_id,
        "uncached_deployment_fingerprint": uncached_deployment.fingerprint,
        "cached_deployment_fingerprint": cached_deployment.fingerprint,
        "uncached_output_sha256": uncached_sha256,
        "cold_output_sha256": cold_sha256,
        "warm_output_sha256": warm_sha256,
        "cold_cache": _jsonable_cache_report(cold.cache),
        "warm_cache": _jsonable_cache_report(warm.cache),
    }
    serialized = json.dumps(evidence, sort_keys=True)
    assert "/" not in serialized and "token" not in serialized.lower()
    with configuration.evidence_output.open("x", encoding="utf-8") as handle:
        handle.write(serialized + "\n")


def _jsonable_cache_report(report) -> Mapping[str, object]:  # type: ignore[no-untyped-def]
    return {
        "hits": report.hits,
        "misses": report.misses,
        "bytes_used": report.bytes_used,
        "metadata": _jsonable(report.metadata),
    }


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value
