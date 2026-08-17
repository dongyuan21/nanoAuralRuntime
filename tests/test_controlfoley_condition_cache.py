"""Roadmap Phase 4D CPU contracts for the experimental condition cache."""

from __future__ import annotations

import hashlib
import importlib
import json
import math
import os
import stat
import struct
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping, Optional, cast

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime import (
    AdapterRegistry,
    CacheReport,
    CancellationToken,
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
    ModelDeployment,
    ProfileReport,
    Runtime,
)
from nano_aural_runtime_controlfoley.adapter import (
    ControlFoleyAdapter,
    _staged_deployment_fingerprint,
    controlfoley_staged_l2_cached_local_deployment,
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
    ControlFoleyCachePolicy,
    ControlFoleyL0L1Cache,
)
from nano_aural_runtime_controlfoley.condition_benchmark import (
    ControlFoleyGpuL2BenchmarkConfiguration,
    ControlFoleyL2BenchmarkObservation,
    ControlFoleyL2BenchmarkPlan,
    ControlFoleyL2BenchmarkRunner,
    write_controlfoley_l2_benchmark_evidence,
)
from nano_aural_runtime_controlfoley.condition_cache import (
    EXPERIMENTAL_L2_CACHE_MODE,
    ControlFoleyConditionCache,
    ControlFoleyConditionCachePolicy,
    ControlFoleyConditionCacheStore,
    ControlFoleyLocalConditionCacheStore,
)
from nano_aural_runtime_controlfoley.profile import (
    ControlFoleyProfileLevel,
    ControlFoleyProfiler,
    ControlFoleyProfileSealError,
    TorchCudaProfileBackend,
    require_controlfoley_gpu_profile_preflight,
)
from nano_aural_runtime_controlfoley.safe_tensor import (
    build_u8_safe_tensor_bundle,
    validate_safe_tensor_bundle,
)
from nano_aural_runtime_controlfoley.staged import (
    CONTROLFOLEY_STAGE_ORDER,
    ControlFoleyOracleDeploymentBinding,
    ControlFoleyPreprocessCacheCodec,
    ControlFoleyStage,
    ControlFoleyStageContractError,
    ControlFoleyStagedBackend,
    ControlFoleyStagedBackendSession,
    ControlFoleyStagedBackendValidation,
    ControlFoleyStageExecutionError,
    ControlFoleyStageValue,
    DeterministicFakeStagedBackend,
    controlfoley_condition_feature_roles,
    controlfoley_invocation_fingerprint,
)
from nano_aural_runtime_controlfoley.tasks import (
    EXPERIMENTAL_STAGED_OPERATION,
    ControlFoleyLocalRequest,
    ControlFoleyTaskKind,
)

_SOURCE_REVISION = "6858cd12a48d141201e3266e7abe1f38357a133e"


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "condition-cache"
    root.mkdir(mode=0o700, parents=True)
    return root


def _base_policy() -> ControlFoleyCachePolicy:
    return ControlFoleyCachePolicy(
        preprocess_version="media-v1",
        code_version="cache-code-v1",
        max_entries=32,
        max_entry_bytes=4096,
        max_total_bytes=32_768,
        ttl_seconds=60.0,
    )


def _condition_policy(root: Path, **changes: object) -> ControlFoleyConditionCachePolicy:
    values: dict[str, object] = {
        "root": root,
        "codec_version": DeterministicFakeStagedBackend.condition_cache_codec_version,
        "schema_version": DeterministicFakeStagedBackend.condition_cache_schema_version,
        "max_entries": 16,
        "max_entry_bytes": 4096,
        "max_total_bytes": 32_768,
        "ttl_seconds": 60.0,
        "cleanup_grace_seconds": 0.01,
    }
    values.update(changes)
    return ControlFoleyConditionCachePolicy(**values)  # type: ignore[arg-type]


def _configuration(
    base_policy: ControlFoleyCachePolicy,
    condition_policy: ControlFoleyConditionCachePolicy,
) -> dict[str, str]:
    source_dir = condition_policy.root.parent / "locked-source"
    weights_dir = condition_policy.root.parent / "locked-weights"
    source_dir.mkdir(mode=0o700, exist_ok=True)
    weights_dir.mkdir(mode=0o700, exist_ok=True)
    values = {
        "manifest_path": "operator-only-unused-in-cpu-test",
        "deployment_manifest_sha256": _digest("deployment-manifest"),
        "source_dir": str(source_dir),
        "weights_dir": str(weights_dir),
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": _SOURCE_REVISION,
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": _digest("checkpoint"),
        "execution_path": EXPERIMENTAL_STAGED_OPERATION,
        "staged_backend_id": DeterministicFakeStagedBackend.backend_id,
    }
    values.update(base_policy.configuration())
    values.update(condition_policy.configuration())
    values["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(values)
    return values


def _deployment(
    adapter: ControlFoleyAdapter,
    base_policy: ControlFoleyCachePolicy,
    condition_policy: ControlFoleyConditionCachePolicy,
) -> ModelDeployment:
    configuration = _configuration(base_policy, condition_policy)
    return ModelDeployment(
        "controlfoley-experimental-l2-cpu-test",
        adapter.descriptor,
        configuration["staged_deployment_fingerprint"],
        configuration,
    )


def _uncached_deployment(adapter: ControlFoleyAdapter) -> ModelDeployment:
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
        "staged_backend_id": DeterministicFakeStagedBackend.backend_id,
    }
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    return ModelDeployment(
        "controlfoley-experimental-uncached-cpu-test",
        adapter.descriptor,
        configuration["staged_deployment_fingerprint"],
        configuration,
    )


def _runtime(adapter: ControlFoleyAdapter) -> Runtime:
    registry = AdapterRegistry()
    registry.register(adapter)
    return Runtime(registry)


def _request(prompt: str = "bird wing") -> ControlFoleyLocalRequest:
    return ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt=prompt)


def _invoke(runtime: Runtime, session, request: ControlFoleyLocalRequest, name: str):  # type: ignore[no-untyped-def]
    return runtime.invoke(
        session,
        request.to_invocation(name, operation=EXPERIMENTAL_STAGED_OPERATION),
    )


def _invoke_with_context(
    runtime: Runtime,
    session,
    request: ControlFoleyLocalRequest,
    name: str,
    context: ExecutionContext,
):  # type: ignore[no-untyped-def]
    return runtime.invoke(
        session,
        request.to_invocation(name, operation=EXPERIMENTAL_STAGED_OPERATION),
        context,
    )


def _raw_bundle(header: object, data: bytes) -> bytes:
    encoded = (
        header
        if isinstance(header, bytes)
        else json.dumps(header, separators=(",", ":")).encode("utf-8")
    )
    return struct.pack("<Q", len(encoded)) + encoded + data


def test_strict_safe_tensor_bundle_roundtrip_and_malformed_rejection() -> None:
    codec = "codec-v1"
    schema = "schema-v1"
    valid = build_u8_safe_tensor_bundle(b"feature", codec_version=codec, schema_version=schema)
    bundle = validate_safe_tensor_bundle(
        valid, codec_version=codec, schema_version=schema, max_size_bytes=len(valid)
    )
    assert bundle.tensor_bytes("condition") == b"feature"

    metadata = {
        "format": "nano-aural-controlfoley-condition",
        "codec_version": codec,
        "schema_version": schema,
    }
    malformed = (
        b"",
        struct.pack("<Q", 100) + b"{}",
        struct.pack("<Q", struct.unpack("<Q", valid[:8])[0] + 1) + b" " + valid[8:],
        _raw_bundle(
            b'{"__metadata__":{"format":"nano-aural-controlfoley-condition",'
            b'"codec_version":"codec-v1","schema_version":"schema-v1"},'
            b'"x":{"dtype":"U8","dtype":"U8","shape":[1],"data_offsets":[0,1]}}',
            b"x",
        ),
        _raw_bundle(
            {
                "__metadata__": metadata,
                "a": {"dtype": "U8", "shape": [True], "data_offsets": [0, 1]},
            },
            b"x",
        ),
        _raw_bundle(
            {
                "__metadata__": metadata,
                "a": {"dtype": "U8", "shape": [1], "data_offsets": [1, 2]},
            },
            b"xx",
        ),
        valid + b"trailing",
    )
    for payload in malformed:
        with pytest.raises(ValueError):
            validate_safe_tensor_bundle(
                payload, codec_version=codec, schema_version=schema, max_size_bytes=1024
            )
    with pytest.raises(ValueError, match="exceeds"):
        validate_safe_tensor_bundle(
            valid, codec_version=codec, schema_version=schema, max_size_bytes=len(valid) - 1
        )

    multi = _raw_bundle(
        {
            "__metadata__": metadata,
            "a": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]},
            "b": {"dtype": "U8", "shape": [2], "data_offsets": [1, 3]},
        },
        b"abc",
    )
    parsed = validate_safe_tensor_bundle(multi, codec_version=codec, schema_version=schema)
    assert parsed.tensor_bytes("a") == b"a"
    assert parsed.tensor_bytes("b") == b"bc"


def test_fake_condition_codec_requires_an_exact_single_role_tensor() -> None:
    backend = DeterministicFakeStagedBackend()
    session, _ = backend.load(
        {
            "staged_deployment_fingerprint": _digest("staged"),
            "deployment_manifest_sha256": _digest("manifest"),
            "source_revision": _SOURCE_REVISION,
            "variant": "large_44k",
            "precision": "fp32",
            "checkpoint_sha256": _digest("checkpoint"),
        }
    )
    metadata = {
        "format": "nano-aural-controlfoley-condition",
        "codec_version": backend.condition_cache_codec_version,
        "schema_version": backend.condition_cache_schema_version,
    }
    extra_tensor = _raw_bundle(
        {
            "__metadata__": metadata,
            "text": {"dtype": "U8", "shape": [1], "data_offsets": [0, 1]},
            "extra": {"dtype": "U8", "shape": [1], "data_offsets": [1, 2]},
        },
        b"ab",
    )
    with pytest.raises(ControlFoleyStageContractError, match="tensor schema"):
        backend.import_condition_feature_cache(
            session,
            "text",
            extra_tensor,
            ControlFoleyStageValue(ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS, b"preprocess"),
            _request(),
            ExecutionContext(),
        )


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def __call__(self) -> float:
        return self.value


def test_disk_store_restart_integrity_conflict_ttl_eviction_and_symlink(tmp_path: Path) -> None:
    root = _root(tmp_path)
    clock = _Clock()
    base = _base_policy()
    policy = _condition_policy(
        root,
        max_entries=1,
        max_total_bytes=4096,
        ttl_seconds=5.0,
    )
    cache = ControlFoleyConditionCache(base, policy)
    configuration = _configuration(base, policy)
    parent = cache.parent_key(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    key = cache.key_for_parent(parent, "text")
    store = ControlFoleyLocalConditionCacheStore(policy, wall_clock=clock)
    payload = build_u8_safe_tensor_bundle(
        b"first",
        codec_version=policy.codec_version,
        schema_version=policy.schema_version,
        tensor_name="text",
    )
    assert store.put_if_absent(key, payload) == "stored"
    assert store.put_if_absent(key, payload) == "present"
    different = build_u8_safe_tensor_bundle(
        b"other",
        codec_version=policy.codec_version,
        schema_version=policy.schema_version,
        tensor_name="text",
    )
    assert store.put_if_absent(key, different) == "conflict"
    store.close()

    restarted = ControlFoleyLocalConditionCacheStore(policy, wall_clock=clock)
    assert restarted.get_entry(key) is not None
    second_parent = cache.parent_key(
        _request("owl"), configuration, configuration["staged_deployment_fingerprint"]
    )
    second_key = cache.key_for_parent(second_parent, "text")
    assert restarted.put_if_absent(second_key, different) == "stored"
    assert restarted.get_entry(key) is None
    assert restarted.snapshot().entries == 1

    data_path = root / (second_key.digest + ".safetensors")
    data_path.unlink()
    target = tmp_path / "outside"
    target.write_bytes(different)
    os.symlink(target, data_path)
    assert restarted.get_entry(second_key) is None
    assert not data_path.exists() and target.exists()

    assert restarted.put_if_absent(second_key, different) == "stored"
    clock.value += 6.0
    assert restarted.get_entry(second_key) is None
    restarted.close()
    cache.close()


def test_disk_store_enforces_entry_and_total_byte_limits(tmp_path: Path) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    payload = build_u8_safe_tensor_bundle(
        b"bounded-feature",
        codec_version=DeterministicFakeStagedBackend.condition_cache_codec_version,
        schema_version=DeterministicFakeStagedBackend.condition_cache_schema_version,
        tensor_name="text",
    )
    rejecting_policy = _condition_policy(
        root,
        max_entry_bytes=len(payload) - 1,
        max_total_bytes=len(payload) - 1,
    )
    rejecting_cache = ControlFoleyConditionCache(base, rejecting_policy)
    rejecting_configuration = _configuration(base, rejecting_policy)
    rejecting_parent = rejecting_cache.parent_key(
        _request(),
        rejecting_configuration,
        rejecting_configuration["staged_deployment_fingerprint"],
    )
    rejecting_store = ControlFoleyLocalConditionCacheStore(rejecting_policy)
    assert (
        rejecting_store.put_if_absent(
            rejecting_cache.key_for_parent(rejecting_parent, "text"), payload
        )
        == "rejected"
    )
    rejecting_store.close()
    rejecting_cache.close()

    bounded_policy = _condition_policy(
        root,
        max_entries=8,
        max_entry_bytes=len(payload),
        max_total_bytes=len(payload) + 1,
    )
    bounded_cache = ControlFoleyConditionCache(base, bounded_policy)
    bounded_configuration = _configuration(base, bounded_policy)
    first_parent = bounded_cache.parent_key(
        _request("first"),
        bounded_configuration,
        bounded_configuration["staged_deployment_fingerprint"],
    )
    second_parent = bounded_cache.parent_key(
        _request("second"),
        bounded_configuration,
        bounded_configuration["staged_deployment_fingerprint"],
    )
    bounded_store = ControlFoleyLocalConditionCacheStore(bounded_policy)
    assert (
        bounded_store.put_if_absent(bounded_cache.key_for_parent(first_parent, "text"), payload)
        == "stored"
    )
    assert (
        bounded_store.put_if_absent(bounded_cache.key_for_parent(second_parent, "text"), payload)
        == "stored"
    )
    snapshot = bounded_store.snapshot()
    assert snapshot.entries == 1
    assert snapshot.bytes_used == len(payload)
    assert snapshot.evictions >= 1
    bounded_store.close()
    bounded_cache.close()
    smaller_restart = ControlFoleyLocalConditionCacheStore(
        _condition_policy(
            root,
            max_entries=8,
            max_entry_bytes=len(payload) - 1,
            max_total_bytes=len(payload) - 1,
        )
    )
    assert smaller_restart.snapshot().entries == 0
    smaller_restart.close()


def test_restart_applies_current_ttl_cap_without_extending_old_entries(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    clock = _Clock()
    base = _base_policy()
    long_policy = _condition_policy(root, ttl_seconds=3600.0)
    configuration = _configuration(base, long_policy)
    cache = ControlFoleyConditionCache(base, long_policy)
    parent = cache.parent_key(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    key = cache.key_for_parent(parent, "text")
    payload = build_u8_safe_tensor_bundle(
        b"ttl",
        codec_version=long_policy.codec_version,
        schema_version=long_policy.schema_version,
        tensor_name="text",
    )
    store = ControlFoleyLocalConditionCacheStore(long_policy, wall_clock=clock)
    assert store.put_if_absent(key, payload) == "stored"
    store.close()
    cache.close()

    clock.value += 2.0
    short_policy = _condition_policy(root, ttl_seconds=1.0)
    short_store = ControlFoleyLocalConditionCacheStore(short_policy, wall_clock=clock)
    assert short_store.snapshot().entries == 0
    assert short_store.get_entry(key) is None
    short_store.close()

    clock.value = 2_000.0
    short_first = _condition_policy(root, ttl_seconds=1.0)
    short_store = ControlFoleyLocalConditionCacheStore(short_first, wall_clock=clock)
    assert short_store.put_if_absent(key, payload) == "stored"
    short_store.close()
    clock.value += 2.0
    longer_restart = ControlFoleyLocalConditionCacheStore(
        _condition_policy(root, ttl_seconds=3600.0), wall_clock=clock
    )
    assert longer_restart.get_entry(key) is None
    longer_restart.close()


def test_disk_store_threaded_first_writer_is_immutable(tmp_path: Path) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    cache = ControlFoleyConditionCache(base, policy)
    configuration = _configuration(base, policy)
    parent = cache.parent_key(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    key = cache.key_for_parent(parent, "text")
    store = ControlFoleyLocalConditionCacheStore(policy)
    payload = build_u8_safe_tensor_bundle(
        b"feature",
        codec_version=policy.codec_version,
        schema_version=policy.schema_version,
        tensor_name="text",
    )
    with ThreadPoolExecutor(max_workers=8) as executor:
        outcomes = tuple(executor.map(lambda _: store.put_if_absent(key, payload), range(8)))
    assert outcomes.count("stored") == 1
    assert outcomes.count("present") == 7
    assert store.snapshot().entries == 1
    store.close()
    cache.close()


def test_two_store_instances_share_conditional_first_writer_and_conflict_fuses(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    cache = ControlFoleyConditionCache(base, policy)
    configuration = _configuration(base, policy)
    parent = cache.parent_key(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    key = cache.key_for_parent(parent, "text")
    first = ControlFoleyLocalConditionCacheStore(policy)
    second = ControlFoleyLocalConditionCacheStore(policy)
    payload = build_u8_safe_tensor_bundle(
        b"shared",
        codec_version=policy.codec_version,
        schema_version=policy.schema_version,
        tensor_name="text",
    )
    barrier = threading.Barrier(2)

    def put(store: ControlFoleyLocalConditionCacheStore, value: bytes) -> str:
        barrier.wait(timeout=5.0)
        return store.put_if_absent(key, value)

    with ThreadPoolExecutor(max_workers=2) as executor:
        same = tuple(
            future.result(timeout=5.0)
            for future in (
                executor.submit(put, first, payload),
                executor.submit(put, second, payload),
            )
        )
    assert sorted(same) == ["present", "stored"]
    first.invalidate_all()

    different = build_u8_safe_tensor_bundle(
        b"different",
        codec_version=policy.codec_version,
        schema_version=policy.schema_version,
        tensor_name="text",
    )
    barrier = threading.Barrier(2)
    with ThreadPoolExecutor(max_workers=2) as executor:
        conflicting = tuple(
            future.result(timeout=5.0)
            for future in (
                executor.submit(put, first, payload),
                executor.submit(put, second, different),
            )
        )
    assert sorted(conflicting) == ["conflict", "stored"]
    first.close()
    second.close()

    # The facade must fail closed after observing nondeterministic bytes for a
    # single semantic key, even if an immutable physical entry remains.
    store = ControlFoleyLocalConditionCacheStore(policy)
    facade = ControlFoleyConditionCache(base, policy, store)
    store.invalidate_all()
    transaction = facade.begin(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    transaction.stage("text", payload)
    facade.commit(
        transaction,
        _request(),
        configuration,
        configuration["staged_deployment_fingerprint"],
    )
    conflicting_transaction = facade.begin(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    conflicting_transaction.stage("text", different)
    report = facade.commit(
        conflicting_transaction,
        _request(),
        configuration,
        configuration["staged_deployment_fingerprint"],
    )
    assert report.metadata["status"] == "degraded"
    assert report.metadata["reason"] == "conflict"
    unavailable = facade.begin(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    assert unavailable.cached_payloads == {}
    assert unavailable.report().metadata["levels"]["l2_condition_text"] == "unavailable"
    facade.close()
    cache.close()


def test_disk_security_corruption_cleanup_and_root_replacement(tmp_path: Path) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root, cleanup_grace_seconds=0.01)
    cache = ControlFoleyConditionCache(base, policy)
    configuration = _configuration(base, policy)
    parent = cache.parent_key(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    key = cache.key_for_parent(parent, "text")
    store = ControlFoleyLocalConditionCacheStore(policy)
    payload = build_u8_safe_tensor_bundle(
        b"feature",
        codec_version=policy.codec_version,
        schema_version=policy.schema_version,
        tensor_name="text",
    )
    assert store.put_if_absent(key, payload) == "stored"
    metadata_path = root / (key.digest + ".json")
    metadata_path.write_bytes(b"not-json")
    assert store.get_entry(key) is None
    assert not metadata_path.exists()

    assert store.put_if_absent(key, payload) == "stored"
    data_path = root / (key.digest + ".safetensors")
    data_path.chmod(0o644)
    assert store.get_entry(key) is None
    assert not data_path.exists()

    old = time.time() - 1.0
    temp = root / ".tmp-abandoned"
    temp.write_bytes(b"partial")
    temp.chmod(0o600)
    os.utime(temp, (old, old))
    orphan = root / (_digest("orphan") + ".safetensors")
    orphan.write_bytes(payload)
    orphan.chmod(0o600)
    os.utime(orphan, (old, old))
    assert store.cleanup() >= 2
    assert not temp.exists() and not orphan.exists()
    store.close()
    cache.close()

    original = tmp_path / "replaceable-root"
    original.mkdir(mode=0o700)
    replaced_policy = _condition_policy(original)
    original.rename(tmp_path / "old-root")
    original.mkdir(mode=0o755)
    with pytest.raises(ValueError, match="private directory"):
        ControlFoleyLocalConditionCacheStore(replaced_policy)


def test_l2_cold_warm_and_restart_skip_encoders_but_never_projection(tmp_path: Path) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    backend = DeterministicFakeStagedBackend()
    condition_cache = ControlFoleyConditionCache(base, policy)
    adapter = ControlFoleyAdapter(
        staged_backend=backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=condition_cache,
    )
    runtime = _runtime(adapter)
    deployment = _deployment(adapter, base, policy)
    session = runtime.load(deployment)
    cold = _invoke(runtime, session, _request(), "cold")
    warm = _invoke(runtime, session, _request(), "warm")
    assert cold.artifacts[0].content == warm.artifacts[0].content
    assert cold.cache.metadata["mode"] == EXPERIMENTAL_L2_CACHE_MODE
    assert cold.cache.metadata["encoder_calls"] == 1
    assert cold.cache.metadata["projection_calls"] == 1
    assert warm.cache.metadata["encoder_calls"] == 0
    assert warm.cache.metadata["projection_calls"] == 1
    assert warm.cache.metadata["levels"]["l2_condition_text"] == "hit"
    assert backend.condition_encoder_calls["text"] == 1
    assert backend.condition_projection_calls == 2
    assert cold.artifacts[0].metadata["stage_order"] == tuple(
        stage.value for stage in CONTROLFOLEY_STAGE_ORDER
    )
    runtime.unload(session)
    condition_cache.close()

    restarted_backend = DeterministicFakeStagedBackend()
    restarted_cache = ControlFoleyConditionCache(base, policy)
    restarted_adapter = ControlFoleyAdapter(
        staged_backend=restarted_backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=restarted_cache,
    )
    restarted_runtime = _runtime(restarted_adapter)
    restarted_session = restarted_runtime.load(_deployment(restarted_adapter, base, policy))
    restarted = _invoke(restarted_runtime, restarted_session, _request(), "restart")
    assert restarted.artifacts[0].content == cold.artifacts[0].content
    assert restarted.cache.metadata["encoder_calls"] == 0
    assert restarted.cache.metadata["projection_calls"] == 1
    restarted_runtime.unload(restarted_session)
    restarted_cache.close()


class _DeceptiveFeatureBackend(DeterministicFakeStagedBackend):
    """Its opaque condition stage is deliberately wrong and must not be called."""

    backend_id = "deceptive-condition-test-double-v1"

    def __init__(self) -> None:
        super().__init__()
        self.opaque_condition_calls = 0

    def run_stage(
        self,
        session: ControlFoleyStagedBackendSession,
        stage: ControlFoleyStage,
        previous: Optional[ControlFoleyStageValue],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue:
        if stage is ControlFoleyStage.CONDITION_ENCODE_PROJECTION:
            self.opaque_condition_calls += 1
            return ControlFoleyStageValue(stage, b"opaque-shortcut", {"test_double": True})
        return super().run_stage(session, stage, previous, request, context)


def test_uncached_feature_backend_counts_actual_encoder_and_projection_calls() -> None:
    backend = _DeceptiveFeatureBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    deployment = _uncached_deployment(adapter)
    configuration = dict(deployment.configuration)
    configuration["staged_backend_id"] = backend.backend_id
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    deployment = ModelDeployment(
        deployment.deployment_id,
        adapter.descriptor,
        configuration["staged_deployment_fingerprint"],
        configuration,
    )
    session = runtime.load(deployment)
    result = _invoke(runtime, session, _request(), "uncached-feature-split")
    assert result.metadata["condition_execution"] == {
        "observable": True,
        "encoder_calls": 1,
        "projection_calls": 1,
    }
    assert backend.condition_encoder_calls["text"] == 1
    assert backend.condition_projection_calls == 1
    assert backend.opaque_condition_calls == 0
    runtime.unload(session)


def test_opaque_condition_backend_does_not_report_synthetic_zero_counters() -> None:
    backend = _PreprocessOnlyBackend()
    adapter = ControlFoleyAdapter(staged_backend=backend)
    runtime = _runtime(adapter)
    deployment = _uncached_deployment(adapter)
    configuration = dict(deployment.configuration)
    configuration["staged_backend_id"] = backend.backend_id
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    session = runtime.load(
        ModelDeployment(
            deployment.deployment_id,
            adapter.descriptor,
            configuration["staged_deployment_fingerprint"],
            configuration,
        )
    )
    result = _invoke(runtime, session, _request(), "opaque-condition")
    assert result.metadata["condition_execution"] == {
        "observable": False,
        "encoder_calls": None,
        "projection_calls": None,
    }
    assert backend.delegate.condition_projection_calls == 1
    runtime.unload(session)


def test_all_task_roles_and_partial_multi_role_hit(tmp_path: Path) -> None:
    video = tmp_path / "video.bin"
    audio = tmp_path / "audio.bin"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")
    requests = {
        ControlFoleyTaskKind.V2A: ControlFoleyLocalRequest(
            ControlFoleyTaskKind.V2A, video_path=video
        ),
        ControlFoleyTaskKind.TV2A: ControlFoleyLocalRequest(
            ControlFoleyTaskKind.TV2A, video_path=video, prompt="bird"
        ),
        ControlFoleyTaskKind.TC_V2A: ControlFoleyLocalRequest(
            ControlFoleyTaskKind.TC_V2A, video_path=video, prompt="bird"
        ),
        ControlFoleyTaskKind.AC_V2A: ControlFoleyLocalRequest(
            ControlFoleyTaskKind.AC_V2A,
            video_path=video,
            reference_audio_path=audio,
        ),
        ControlFoleyTaskKind.T2A: _request(),
    }
    expected = {
        ControlFoleyTaskKind.V2A: ("video",),
        ControlFoleyTaskKind.TV2A: ("video", "text"),
        ControlFoleyTaskKind.TC_V2A: ("video", "text"),
        ControlFoleyTaskKind.AC_V2A: ("video", "reference_audio"),
        ControlFoleyTaskKind.T2A: ("text",),
    }
    assert {
        task: controlfoley_condition_feature_roles(request) for task, request in requests.items()
    } == expected

    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    store = ControlFoleyLocalConditionCacheStore(policy)
    condition_cache = ControlFoleyConditionCache(base, policy, store)
    backend = DeterministicFakeStagedBackend()
    adapter = ControlFoleyAdapter(
        staged_backend=backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=condition_cache,
    )
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, base, policy))
    request = requests[ControlFoleyTaskKind.TV2A]
    cold = _invoke(runtime, session, request, "multi-cold")
    parent = condition_cache.parent_key(
        request, session.deployment.configuration, session.deployment.fingerprint
    )
    store.delete(condition_cache.key_for_parent(parent, "text"))
    partial = _invoke(runtime, session, request, "multi-partial")
    assert partial.artifacts[0].content == cold.artifacts[0].content
    assert partial.cache.metadata["levels"] == {
        "l0_metadata": "hit",
        "l1_preprocess": "hit",
        "l2_condition_video": "hit",
        "l2_condition_text": "miss",
    }
    assert partial.cache.metadata["encoder_calls"] == 1
    assert partial.cache.metadata["projection_calls"] == 1
    runtime.unload(session)
    condition_cache.close()


class _PreprocessOnlyBackend:
    backend_id = "preprocess-only-test-double-v1"

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
                "preprocess-only CPU test double",
            ),
        )

    def run_stage(self, session, stage, previous, request, context):  # type: ignore[no-untyped-def]
        return self.delegate.run_stage(session, stage, previous, request, context)

    def unload(self, session: ControlFoleyStagedBackendSession) -> None:
        self.delegate.unload(session)

    def export_preprocess_cache(self, session, value, request):  # type: ignore[no-untyped-def]
        return self.delegate.export_preprocess_cache(session, value, request)

    def import_preprocess_cache(self, session, payload, request, context):  # type: ignore[no-untyped-def]
        return self.delegate.import_preprocess_cache(session, payload, request, context)


def test_l2_is_default_off_sealed_and_requires_codec_before_backend_load(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    backend = _PreprocessOnlyBackend()
    assert isinstance(backend, ControlFoleyPreprocessCacheCodec)
    condition_cache = ControlFoleyConditionCache(base, policy)
    adapter = ControlFoleyAdapter(
        staged_backend=backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=condition_cache,
    )
    configuration = _configuration(base, policy)
    configuration["staged_backend_id"] = backend.backend_id
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    deployment = ModelDeployment(
        "l2-no-codec",
        adapter.descriptor,
        configuration["staged_deployment_fingerprint"],
        configuration,
    )
    with pytest.raises(InvocationRejectedError, match="feature codec/split"):
        _runtime(adapter).load(deployment)
    assert backend.loaded == 0

    real_backend = DeterministicFakeStagedBackend()
    real_adapter = ControlFoleyAdapter(
        staged_backend=real_backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=condition_cache,
    )
    drifted = _configuration(base, policy)
    drifted["l2_cache_schema_version"] = "drifted-schema"
    with pytest.raises((InvocationRejectedError, ValueError)):
        _runtime(real_adapter).load(
            ModelDeployment(
                "l2-drift",
                real_adapter.descriptor,
                drifted["staged_deployment_fingerprint"],
                drifted,
            )
        )
    assert real_backend.loaded == 0
    condition_cache.close()


def test_l2_runtime_load_rejects_cache_root_overlapping_locked_source(
    tmp_path: Path,
) -> None:
    source = tmp_path / "locked-source"
    weights = tmp_path / "locked-weights"
    source.mkdir(mode=0o700)
    weights.mkdir(mode=0o700)
    root = source / "l2-cache"
    root.mkdir(mode=0o700)
    base = _base_policy()
    policy = _condition_policy(root)
    backend = DeterministicFakeStagedBackend()
    condition_cache = ControlFoleyConditionCache(base, policy)
    adapter = ControlFoleyAdapter(
        staged_backend=backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=condition_cache,
    )
    configuration = _configuration(base, policy)
    configuration["source_dir"] = str(source)
    configuration["weights_dir"] = str(weights)
    configuration["staged_deployment_fingerprint"] = _staged_deployment_fingerprint(configuration)
    deployment = ModelDeployment(
        "overlapping-l2-root",
        adapter.descriptor,
        configuration["staged_deployment_fingerprint"],
        configuration,
    )
    with pytest.raises(InvocationRejectedError, match="condition cache policies"):
        _runtime(adapter).load(deployment)
    assert backend.loaded == 0
    store = ControlFoleyLocalConditionCacheStore(policy)
    assert store.snapshot().entries == 0
    store.close()
    condition_cache.close()

    ancestor_root = tmp_path / "ancestor-cache"
    ancestor_root.mkdir(mode=0o700)
    nested_source = ancestor_root / "nested-source"
    nested_source.mkdir(mode=0o700)
    sibling_weights = tmp_path / "sibling-weights"
    sibling_weights.mkdir(mode=0o700)
    ancestor_policy = _condition_policy(ancestor_root)
    with pytest.raises(ValueError, match="disjoint"):
        ancestor_policy.assert_operator_separation(nested_source, sibling_weights)

    source_alias = tmp_path / "source-alias"
    os.symlink(sibling_weights, source_alias)
    with pytest.raises(ValueError, match="canonical"):
        ancestor_policy.assert_operator_separation(source_alias, nested_source)


class _DelegatingStore:
    def __init__(self, delegate: ControlFoleyLocalConditionCacheStore) -> None:
        self.delegate = delegate
        self.reads = 0

    def get_entry(self, key):  # type: ignore[no-untyped-def]
        self.reads += 1
        return self.delegate.get_entry(key)

    def put_if_absent(self, key, payload):  # type: ignore[no-untyped-def]
        return self.delegate.put_if_absent(key, payload)

    def delete(self, key):  # type: ignore[no-untyped-def]
        self.delegate.delete(key)

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        return self.delegate.invalidate_deployment(deployment_fingerprint)

    def invalidate_all(self) -> int:
        return self.delegate.invalidate_all()

    def cleanup(self) -> int:
        return self.delegate.cleanup()

    def snapshot(self):  # type: ignore[no-untyped-def]
        return self.delegate.snapshot()

    def close(self) -> None:
        self.delegate.close()


class _DeleteFsyncStore(ControlFoleyLocalConditionCacheStore):
    def __init__(self, policy: ControlFoleyConditionCachePolicy) -> None:
        self.root_fsyncs = 0
        self.fail_next_root_fsync = False
        super().__init__(policy)

    def _fsync_root(self) -> None:
        self.root_fsyncs += 1
        if self.fail_next_root_fsync:
            self.fail_next_root_fsync = False
            raise OSError("injected delete directory fsync failure")
        super()._fsync_root()


class _BlockingStore(_DelegatingStore):
    def __init__(
        self,
        delegate: ControlFoleyLocalConditionCacheStore,
        *,
        fail_delete: bool = False,
    ) -> None:
        super().__init__(delegate)
        self.persisted = threading.Event()
        self.release = threading.Event()
        self.fail_delete = fail_delete

    def put_if_absent(self, key, payload):  # type: ignore[no-untyped-def]
        outcome = self.delegate.put_if_absent(key, payload)
        self.persisted.set()
        if not self.release.wait(timeout=5.0):
            raise RuntimeError("test did not release blocking L2 store")
        return outcome

    def delete(self, key):  # type: ignore[no-untyped-def]
        if self.fail_delete:
            raise OSError("injected rollback delete failure")
        self.delegate.delete(key)


class _PersistThenRaiseStore(_DelegatingStore):
    def put_if_absent(self, key, payload):  # type: ignore[no-untyped-def]
        self.delegate.put_if_absent(key, payload)
        raise OSError("injected uncertain post-publish failure")


@pytest.mark.parametrize("fail_fsync", (False, True))
def test_rollback_delete_is_directory_durable_or_persistently_fused(
    tmp_path: Path, fail_fsync: bool
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    delegate = _DeleteFsyncStore(policy)
    store = _BlockingStore(delegate)
    _, condition_cache, _, runtime, session = _system_with_store(base, policy, store)
    token = CancellationToken()
    context = ExecutionContext(cancellation_token=token)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _invoke_with_context,
            runtime,
            session,
            _request(),
            "delete-fsync",
            context,
        )
        assert store.persisted.wait(timeout=5.0)
        fsyncs_before_delete = delegate.root_fsyncs
        delegate.fail_next_root_fsync = fail_fsync
        token.cancel("cancel before rollback delete")
        store.release.set()
        with pytest.raises(InvocationCancelledError):
            future.result(timeout=5.0)
    assert delegate.root_fsyncs > fsyncs_before_delete

    restarted_store = ControlFoleyLocalConditionCacheStore(policy)
    restarted_cache = ControlFoleyConditionCache(base, policy, restarted_store)
    configuration = _configuration(base, policy)
    restarted = restarted_cache.begin(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    expected = "unavailable" if fail_fsync else "miss"
    assert restarted.report().metadata["levels"]["l2_condition_text"] == expected
    if fail_fsync:
        restarted_cache.invalidate_deployment(session.deployment.fingerprint)
        recovered = restarted_cache.begin(
            _request(), configuration, configuration["staged_deployment_fingerprint"]
        )
        assert recovered.report().metadata["levels"]["l2_condition_text"] == "miss"
    restarted_cache.close()
    runtime.unload(session)
    condition_cache.close()


class _MaintenanceFailStore(_DelegatingStore):
    def __init__(self, delegate: ControlFoleyLocalConditionCacheStore, failure: str) -> None:
        super().__init__(delegate)
        self.failure = failure

    def invalidate_deployment(self, deployment_fingerprint: str) -> int:
        if self.failure == "deployment":
            raise OSError("injected deployment invalidation failure")
        return super().invalidate_deployment(deployment_fingerprint)

    def invalidate_all(self) -> int:
        if self.failure == "all":
            raise OSError("injected global invalidation failure")
        return super().invalidate_all()

    def cleanup(self) -> int:
        if self.failure == "release":
            raise OSError("injected release cleanup failure")
        return super().cleanup()

    def close(self) -> None:
        if self.failure == "close":
            raise OSError("injected close failure")
        super().close()


def _system_with_store(
    base: ControlFoleyCachePolicy,
    policy: ControlFoleyConditionCachePolicy,
    store: ControlFoleyConditionCacheStore,
    *,
    backend: Optional[DeterministicFakeStagedBackend] = None,
):  # type: ignore[no-untyped-def]
    selected_backend = backend or DeterministicFakeStagedBackend()
    condition_cache = ControlFoleyConditionCache(base, policy, store)
    adapter = ControlFoleyAdapter(
        staged_backend=selected_backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=condition_cache,
    )
    runtime = _runtime(adapter)
    session = runtime.load(_deployment(adapter, base, policy))
    return selected_backend, condition_cache, adapter, runtime, session


@pytest.mark.parametrize("fail_delete", (False, True))
def test_cancel_during_persistence_rolls_back_or_fuses_stale_entry(
    tmp_path: Path, fail_delete: bool
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    delegate = ControlFoleyLocalConditionCacheStore(policy)
    store = _BlockingStore(delegate, fail_delete=fail_delete)
    backend, condition_cache, _, runtime, session = _system_with_store(base, policy, store)
    token = CancellationToken()
    context = ExecutionContext(cancellation_token=token)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(
            _invoke_with_context,
            runtime,
            session,
            _request(),
            "cancel-during-persist",
            context,
        )
        assert store.persisted.wait(timeout=5.0)
        token.cancel("test cancellation after disk publish")
        store.release.set()
        with pytest.raises(InvocationCancelledError):
            future.result(timeout=5.0)
    if fail_delete:
        assert tuple(root.glob("*.safetensors"))
        restarted_store = ControlFoleyLocalConditionCacheStore(policy)
        restarted_cache = ControlFoleyConditionCache(base, policy, restarted_store)
        configuration = _configuration(base, policy)
        restarted = restarted_cache.begin(
            _request(), configuration, configuration["staged_deployment_fingerprint"]
        )
        assert restarted.cached_payloads == {}
        assert restarted.report().metadata["levels"]["l2_condition_text"] == "unavailable"
        assert restarted_store.snapshot().entries == 0
        restarted_cache.close()
        reads_before = store.reads
        fallback = _invoke(runtime, session, _request(), "after-failed-rollback")
        assert store.reads == reads_before
        assert fallback.cache.hits == 0
        assert fallback.cache.metadata["levels"]["l2_condition_text"] == "unavailable"
        assert fallback.cache.metadata["status"] == "degraded"
        assert backend.condition_encoder_calls["text"] == 2
    else:
        assert delegate.snapshot().entries == 0
    runtime.unload(session)
    condition_cache.close()


@pytest.mark.parametrize("fail_delete", (False, True))
def test_input_drift_during_persistence_rejects_or_persistently_fuses(
    tmp_path: Path, fail_delete: bool
) -> None:
    root = _root(tmp_path)
    video = tmp_path / "video.bin"
    video.write_bytes(b"sealed-video")
    request = ControlFoleyLocalRequest(ControlFoleyTaskKind.V2A, video_path=video)
    base = _base_policy()
    policy = _condition_policy(root)
    delegate = ControlFoleyLocalConditionCacheStore(policy)
    store = _BlockingStore(delegate, fail_delete=fail_delete)
    _, condition_cache, _, runtime, session = _system_with_store(base, policy, store)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(_invoke, runtime, session, request, "drift-during-persist")
        assert store.persisted.wait(timeout=5.0)
        video.write_bytes(b"mutated-video")
        store.release.set()
        with pytest.raises(InvocationRejectedError, match="inputs changed"):
            future.result(timeout=5.0)
    if fail_delete:
        assert tuple(root.glob("*.safetensors"))
        restarted_store = ControlFoleyLocalConditionCacheStore(policy)
        restarted_cache = ControlFoleyConditionCache(base, policy, restarted_store)
        configuration = _configuration(base, policy)
        restarted = restarted_cache.begin(
            request, configuration, configuration["staged_deployment_fingerprint"]
        )
        assert restarted.cached_payloads == {}
        assert restarted.report().metadata["levels"]["l2_condition_video"] == "unavailable"
        restarted_cache.close()
    else:
        assert delegate.snapshot().entries == 0
    runtime.unload(session)
    condition_cache.close()


def test_uncertain_put_fuses_future_reads_without_changing_model_result(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    delegate = ControlFoleyLocalConditionCacheStore(policy)
    store = _PersistThenRaiseStore(delegate)
    backend, condition_cache, _, runtime, session = _system_with_store(base, policy, store)
    first = _invoke(runtime, session, _request(), "uncertain-put")
    assert first.artifacts[0].content
    assert first.cache.metadata["status"] == "degraded"
    restarted_store = ControlFoleyLocalConditionCacheStore(policy)
    restarted_cache = ControlFoleyConditionCache(base, policy, restarted_store)
    configuration = _configuration(base, policy)
    restarted = restarted_cache.begin(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    assert restarted.cached_payloads == {}
    assert restarted.report().metadata["levels"]["l2_condition_text"] == "unavailable"
    assert restarted_store.snapshot().entries == 0
    restarted_cache.close()
    reads_before = store.reads
    second = _invoke(runtime, session, _request(), "after-uncertain-put")
    assert second.artifacts[0].content == first.artifacts[0].content
    assert store.reads == reads_before
    assert second.cache.metadata["levels"]["l2_condition_text"] == "unavailable"
    assert backend.condition_encoder_calls["text"] == 2
    runtime.unload(session)
    condition_cache.close()


@pytest.mark.parametrize("failure", ("deployment", "all", "release", "close"))
def test_failed_lifecycle_operation_never_reuses_old_disk_entries(
    tmp_path: Path, failure: str
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    delegate = ControlFoleyLocalConditionCacheStore(policy)
    store = _MaintenanceFailStore(delegate, failure)
    backend, condition_cache, adapter, runtime, session = _system_with_store(base, policy, store)
    deployment = session.deployment
    first = _invoke(runtime, session, _request(), "lifecycle-fill")
    assert first.cache.metadata["levels"]["l2_condition_text"] == "miss"
    assert delegate.snapshot().entries == 1

    if failure == "deployment":
        adapter.invalidate_cache(deployment.fingerprint)
    elif failure == "all":
        adapter.invalidate_cache()
    elif failure == "release":
        runtime.unload(session)
        session = runtime.load(deployment)
    else:
        adapter.close_cache()

    restarted_store = ControlFoleyLocalConditionCacheStore(policy)
    restarted_cache = ControlFoleyConditionCache(base, policy, restarted_store)
    configuration = _configuration(base, policy)
    restarted = restarted_cache.begin(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    assert restarted.cached_payloads == {}
    assert restarted.report().metadata["levels"]["l2_condition_text"] == "unavailable"
    if failure in ("all", "close"):
        restarted_cache.invalidate_all()
    else:
        restarted_cache.invalidate_deployment(deployment.fingerprint)
    recovered = restarted_cache.begin(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    assert recovered.report().metadata["levels"]["l2_condition_text"] == "miss"
    restarted_cache.close()

    reads_before = store.reads
    fallback = _invoke(runtime, session, _request(), "lifecycle-fallback")
    assert fallback.artifacts[0].content == first.artifacts[0].content
    assert fallback.cache.metadata["levels"]["l2_condition_text"] == "unavailable"
    assert fallback.cache.metadata["status"] == "degraded"
    assert fallback.cache.metadata["encoder_calls"] == 1
    assert store.reads == reads_before
    assert backend.condition_encoder_calls["text"] == 2
    runtime.unload(session)
    if failure == "close":
        delegate.invalidate_all()
        delegate.close()
    else:
        condition_cache.close()


class _ExportFaultBackend(DeterministicFakeStagedBackend):
    def export_condition_feature_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        feature,  # type: ignore[no-untyped-def]
        request: ControlFoleyLocalRequest,
    ) -> bytes:
        del session, feature, request
        raise OSError("injected feature export failure")


def test_condition_codec_fault_is_a_cold_bypass_and_does_not_change_artifact(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    store = ControlFoleyLocalConditionCacheStore(policy)
    backend = _ExportFaultBackend()
    _, condition_cache, _, runtime, session = _system_with_store(
        base, policy, store, backend=backend
    )
    first = _invoke(runtime, session, _request(), "codec-fault-first")
    second = _invoke(runtime, session, _request(), "codec-fault-second")
    assert second.artifacts[0].content == first.artifacts[0].content
    assert first.cache.metadata["status"] == "degraded"
    assert second.cache.metadata["status"] == "degraded"
    assert first.cache.metadata["levels"]["l2_condition_text"] == "miss"
    assert second.cache.metadata["levels"]["l2_condition_text"] == "miss"
    assert backend.condition_encoder_calls["text"] == 2
    assert store.snapshot().entries == 0
    runtime.unload(session)
    condition_cache.close()


def test_fault_never_commits_l2_and_combined_invalidation_is_exact(tmp_path: Path) -> None:
    fault_root = _root(tmp_path / "fault")
    base = _base_policy()
    fault_policy = _condition_policy(fault_root)
    fault_store = ControlFoleyLocalConditionCacheStore(fault_policy)
    fault_backend = DeterministicFakeStagedBackend(fail_stage=ControlFoleyStage.INTEGRATE)
    _, fault_cache, _, fault_runtime, fault_session = _system_with_store(
        base, fault_policy, fault_store, backend=fault_backend
    )
    with pytest.raises(ControlFoleyStageExecutionError):
        _invoke(fault_runtime, fault_session, _request(), "model-fault")
    assert fault_store.snapshot().entries == 0
    fault_runtime.unload(fault_session)
    fault_cache.close()

    root = _root(tmp_path / "invalidate")
    policy = _condition_policy(root)
    store = ControlFoleyLocalConditionCacheStore(policy)
    backend, condition_cache, adapter, runtime, session = _system_with_store(base, policy, store)
    first = _invoke(runtime, session, _request(), "fill-before-invalidate")
    assert first.cache.metadata["writes"] == 3
    assert adapter.invalidate_cache(session.deployment.fingerprint) == 3
    assert store.snapshot().entries == 0
    second = _invoke(runtime, session, _request(), "cold-after-invalidate")
    assert second.artifacts[0].content == first.artifacts[0].content
    assert second.cache.metadata["encoder_calls"] == 1
    assert backend.condition_encoder_calls["text"] == 2
    runtime.unload(session)
    condition_cache.close()


class _BenchmarkClock:
    def __init__(self) -> None:
        self.value = 0.0

    def __call__(self) -> float:
        self.value += 0.25
        return self.value


class _ResidentTrackingBackend(DeterministicFakeStagedBackend):
    def __init__(self) -> None:
        super().__init__()
        self.resident = 0
        self.maximum_resident = 0

    def load(self, configuration):  # type: ignore[no-untyped-def]
        result = super().load(configuration)
        self.resident += 1
        self.maximum_resident = max(self.maximum_resident, self.resident)
        return result

    def unload(self, session: ControlFoleyStagedBackendSession) -> None:
        super().unload(session)
        self.resident -= 1


def test_cpu_matrix_unloads_before_l2_load_and_resets_transition_state(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    backend = _ResidentTrackingBackend()
    off_adapter = ControlFoleyAdapter(staged_backend=backend)
    condition_cache = ControlFoleyConditionCache(base, policy)
    l2_adapter = ControlFoleyAdapter(
        staged_backend=backend,
        cache=ControlFoleyL0L1Cache(base),
        condition_cache=condition_cache,
    )
    off_runtime = _runtime(off_adapter)
    l2_runtime = _runtime(l2_adapter)
    off_session = off_runtime.load(_uncached_deployment(off_adapter))
    l2_deployment = _deployment(l2_adapter, base, policy)
    l2_session = None
    transition_resets = 0
    plan = ControlFoleyL2BenchmarkPlan(
        deployment_manifest_sha256=_digest("manifest"),
        source_revision=_SOURCE_REVISION,
        checkpoint_sha256=_digest("checkpoint"),
        fixture_manifest_sha256=_digest("fixture"),
        canonical_invocation_sha256=_digest("invocation"),
        backend_id=backend.backend_id,
        uncached_deployment_fingerprint=off_session.deployment.fingerprint,
        l2_deployment_fingerprint=l2_deployment.fingerprint,
        condition_codec_version=policy.codec_version,
        condition_schema_version=policy.schema_version,
        base_cache_policy_fingerprint=base.fingerprint,
        condition_cache_policy_fingerprint=policy.fingerprint,
        repeats=1,
    )

    def prepare(cell: str, repeat: int) -> None:
        nonlocal off_session, l2_session, transition_resets
        del repeat
        if cell == "l2_cold" and l2_session is None:
            assert off_session is not None
            off_runtime.unload(off_session)
            off_session = None
            assert backend.resident == 0
            transition_resets += 1
            l2_session = l2_runtime.load(l2_deployment)
        if cell == "l2_cold":
            l2_adapter.invalidate_cache(l2_deployment.fingerprint)

    def execute(cell: str, repeat: int) -> ControlFoleyL2BenchmarkObservation:
        session = off_session if cell == "off" else l2_session
        runtime = off_runtime if cell == "off" else l2_runtime
        assert session is not None and backend.resident == 1
        result = _invoke(runtime, session, _request(), "cpu-matrix-{0}-{1}".format(cell, repeat))
        counters = result.metadata["condition_execution"]
        assert counters["observable"] is True
        return ControlFoleyL2BenchmarkObservation(
            output_sha256=hashlib.sha256(result.artifacts[0].content).hexdigest(),
            cache=result.cache,
            profile=result.profile,
            encoder_calls=cast(int, counters["encoder_calls"]),
            projection_calls=cast(int, counters["projection_calls"]),
        )

    evidence = ControlFoleyL2BenchmarkRunner(_BenchmarkClock()).run(plan, execute, prepare)
    assert backend.maximum_resident == 1
    assert transition_resets == 1
    assert len({sample.output_sha256 for sample in evidence.samples}) == 1
    assert l2_session is not None
    l2_runtime.unload(l2_session)
    condition_cache.close()


def test_benchmark_plan_contains_no_measurements_and_runner_records_raw_matrix(
    tmp_path: Path,
) -> None:
    plan = ControlFoleyL2BenchmarkPlan(
        deployment_manifest_sha256=_digest("manifest"),
        source_revision=_SOURCE_REVISION,
        checkpoint_sha256=_digest("checkpoint"),
        fixture_manifest_sha256=_digest("fixture"),
        canonical_invocation_sha256=_digest("invocation"),
        backend_id=DeterministicFakeStagedBackend.backend_id,
        uncached_deployment_fingerprint=_digest("off-deployment"),
        l2_deployment_fingerprint=_digest("l2-deployment"),
        condition_codec_version="fake-condition-codec-v1",
        condition_schema_version="fake-condition-schema-v1",
        base_cache_policy_fingerprint=_digest("base-cache-policy"),
        condition_cache_policy_fingerprint=_digest("condition-cache-policy"),
        repeats=2,
    )
    plan.assert_cache_bindings(
        condition_codec_version="fake-condition-codec-v1",
        condition_schema_version="fake-condition-schema-v1",
        base_cache_policy_fingerprint=_digest("base-cache-policy"),
        condition_cache_policy_fingerprint=_digest("condition-cache-policy"),
    )
    with pytest.raises(ValueError, match="binding drifted"):
        plan.assert_cache_bindings(
            condition_codec_version="different-codec-v1",
            condition_schema_version="fake-condition-schema-v1",
            base_cache_policy_fingerprint=_digest("base-cache-policy"),
            condition_cache_policy_fingerprint=_digest("condition-cache-policy"),
        )
    planned = plan.to_dict()
    assert planned["measurements"] is None
    serialized_plan = json.dumps(dict(planned), sort_keys=True).lower()
    for forbidden in ("speedup", "threshold", "parity", "claim"):
        assert forbidden not in serialized_plan

    prepared = []
    resident = {"off"}
    maximum_resident = 1
    transitions = 0

    def prepare(cell: str, repeat: int) -> None:
        nonlocal maximum_resident, transitions
        prepared.append((cell, repeat))
        if cell == "l2_cold" and "l2" not in resident:
            resident.remove("off")
            transitions += 1
            resident.add("l2")
        maximum_resident = max(maximum_resident, len(resident))

    def execute(cell: str, repeat: int) -> ControlFoleyL2BenchmarkObservation:
        del repeat
        assert resident == ({"off"} if cell == "off" else {"l2"})
        is_warm = cell == "l2_warm"
        return ControlFoleyL2BenchmarkObservation(
            output_sha256=_digest("same-output"),
            cache=CacheReport(
                hits=1 if is_warm else 0,
                misses=0 if cell == "off" or is_warm else 1,
                bytes_used=128 if cell != "off" else 0,
            ),
            profile=ProfileReport(
                metrics={
                    "controlfoley.cuda.condition.milliseconds": 1.5,
                    "controlfoley.cuda.condition.peak_allocated_bytes": 1024.0,
                    "controlfoley.cuda.condition.peak_reserved_bytes": 2048.0,
                }
            ),
            encoder_calls=0 if is_warm else 1,
            projection_calls=1,
        )

    evidence = ControlFoleyL2BenchmarkRunner(_BenchmarkClock()).run(plan, execute, prepare)
    assert prepared == [
        (cell, repeat) for cell in ("off", "l2_cold", "l2_warm") for repeat in range(2)
    ]
    assert len(evidence.samples) == 6
    assert maximum_resident == 1 and transitions == 1
    assert all(sample.wall_time_seconds == 0.25 for sample in evidence.samples)
    assert all(sample.output_sha256 == _digest("same-output") for sample in evidence.samples)
    assert all(sample.projection_calls == 1 for sample in evidence.samples)
    serialized = json.dumps(dict(evidence.to_dict()), sort_keys=True).lower()
    for forbidden in ("speedup", "threshold", "parity", "claim"):
        assert forbidden not in serialized

    strict_gpu_plan = replace(plan, expected_device_name="NVIDIA GeForce RTX 4090")

    def missing_cuda_execute(cell: str, repeat: int) -> ControlFoleyL2BenchmarkObservation:
        del cell, repeat
        return ControlFoleyL2BenchmarkObservation(
            output_sha256=_digest("same-output"),
            cache=CacheReport(),
            profile=ProfileReport(),
            encoder_calls=1,
            projection_calls=1,
        )

    with pytest.raises(ValueError, match="complete CUDA observations"):
        ControlFoleyL2BenchmarkRunner(_BenchmarkClock()).run(
            strict_gpu_plan, missing_cuda_execute, lambda cell, repeat: None
        )
    output = tmp_path / "evidence.json"
    write_controlfoley_l2_benchmark_evidence(output, evidence)
    assert stat.S_IMODE(output.stat().st_mode) == 0o600
    assert json.loads(output.read_text(encoding="utf-8"))["state"] == "measured_raw"
    with pytest.raises(ValueError, match="already exists"):
        write_controlfoley_l2_benchmark_evidence(output, evidence)
    assert not tuple(tmp_path.glob(".nano-aural-p4d-evidence-*"))
    blocked = tmp_path / "blocked.json"

    def reject_publish() -> None:
        raise RuntimeError("injected final seal drift")

    with pytest.raises(RuntimeError, match="seal drift"):
        write_controlfoley_l2_benchmark_evidence(blocked, evidence, reject_publish)
    assert not blocked.exists()
    assert not tuple(tmp_path.glob(".nano-aural-p4d-evidence-*"))


def _gpu_l2_configuration_values(tmp_path: Path) -> dict[str, object]:
    deployment = tmp_path / "deployment.json"
    fixture = tmp_path / "fixture.json"
    video = tmp_path / "video.bin"
    source = tmp_path / "source"
    weights = tmp_path / "weights"
    root = tmp_path / "l2-root"
    deployment.write_text("{}", encoding="utf-8")
    fixture.write_text("{}", encoding="utf-8")
    video.write_bytes(b"video")
    source.mkdir(mode=0o700)
    weights.mkdir(mode=0o700)
    root.mkdir(mode=0o700)
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
        "condition_cache_root": str(root),
        "condition_codec_version": "operator-condition-codec-v1",
        "condition_schema_version": "operator-condition-schema-v1",
        "expected_device_name": "NVIDIA GeForce RTX 4090",
        "repeats": 2,
    }


def test_gpu_l2_configuration_is_exact_bounded_and_outside_locked_material(
    tmp_path: Path,
) -> None:
    values = _gpu_l2_configuration_values(tmp_path)
    parsed = ControlFoleyGpuL2BenchmarkConfiguration.from_dict(values)
    assert parsed.repeats == 2
    assert parsed.condition_cache_root.is_absolute()
    with pytest.raises(ValueError, match="between 1 and 20"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(dict(values, repeats=21))
    with pytest.raises(ValueError, match="source and weights"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(
            dict(values, condition_cache_root=values["source_dir"])
        )
    with pytest.raises(ValueError, match="source and weights"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(
            dict(values, condition_cache_root=str(tmp_path))
        )
    with pytest.raises(ValueError, match="evidence_output"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(
            dict(
                values,
                evidence_output=str(Path(cast(str, values["condition_cache_root"])) / "x.json"),
            )
        )
    with pytest.raises(ValueError, match="evidence_output"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(
            dict(values, evidence_output=str(Path(cast(str, values["source_dir"])) / "x.json"))
        )
    with pytest.raises(ValueError, match="device_name"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(
            dict(values, expected_device_name="NVIDIA/RTX4090")
        )
    with pytest.raises(ValueError, match="RTX 4090"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(
            dict(values, expected_device_name="NVIDIA A100-SXM4-80GB")
        )
    with pytest.raises(ValueError, match="unexpected"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(dict(values, token="secret"))
    symlink = tmp_path / "linked-root"
    os.symlink(cast(str, values["condition_cache_root"]), symlink)
    with pytest.raises(ValueError, match="symlink"):
        ControlFoleyGpuL2BenchmarkConfiguration.from_dict(
            dict(values, condition_cache_root=str(symlink))
        )


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_controlfoley_l2_benchmark_matrix_when_operator_configured() -> None:
    raw = os.environ.get("CONTROLFOLEY_P4D_GPU_CONFIG")
    if raw is None:
        pytest.skip("CONTROLFOLEY_P4D_GPU_CONFIG is not set; RTX 4090 L2 evidence is deferred")
    assert raw is not None
    try:
        configuration = ControlFoleyGpuL2BenchmarkConfiguration.from_dict(json.loads(raw))
        deployment_manifest = ControlFoleyDeploymentManifest.from_dict(
            load_json(configuration.base.deployment)
        )
        fixture = FixtureManifest.from_dict(load_json(configuration.base.fixture))
    except (json.JSONDecodeError, OSError, TypeError, ValueError) as error:
        pytest.fail("CONTROLFOLEY_P4D_GPU_CONFIG is invalid: {0}".format(type(error).__name__))
    if fixture.task != configuration.base.task.value:
        pytest.fail("fixture task does not match the sealed GPU L2 configuration")
    request_values: dict[str, object] = {
        "task": configuration.base.task,
        "duration_seconds": fixture.duration_seconds,
        "num_steps": fixture.num_steps,
        "guidance_scale": fixture.guidance_scale,
        "seed": fixture.seed,
    }
    if configuration.base.video is not None:
        request_values["video_path"] = configuration.base.video
        assert next(
            item for item in fixture.inputs if item.role == "video"
        ).fingerprint == fingerprint_file(configuration.base.video)
    if configuration.base.audio is not None:
        request_values["reference_audio_path"] = configuration.base.audio
        assert next(
            item for item in fixture.inputs if item.role == "reference_audio"
        ).fingerprint == fingerprint_file(configuration.base.audio)
    if configuration.base.prompt is not None:
        request_values["prompt"] = configuration.base.prompt
        assert next(
            item for item in fixture.inputs if item.role == "prompt"
        ).fingerprint == fingerprint_text(configuration.base.prompt)
    request = ControlFoleyLocalRequest(**request_values)  # type: ignore[arg-type]
    invocation_sha = controlfoley_invocation_fingerprint(request)
    fixture_sha = manifest_sha256(fixture.to_dict())
    oracle = ControlFoleyOracleDeploymentBinding.from_manifest(deployment_manifest)
    try:
        require_controlfoley_gpu_profile_preflight(
            configuration.base.source_dir,
            configuration.base.weights_dir,
            deployment_manifest,
        )
    except ControlFoleyProfileSealError as error:
        pytest.fail(str(error))
    except GpuPrerequisitesUnavailable as error:
        pytest.skip(str(error))

    module = importlib.import_module(configuration.base.backend_module)
    factory = getattr(module, "create_controlfoley_staged_backend", None)
    if not callable(factory):
        pytest.fail("backend_module must expose create_controlfoley_staged_backend()")
    assert callable(factory)
    backend = cast(ControlFoleyStagedBackend, factory())
    try:
        cuda_backend = TorchCudaProfileBackend()
        actual_device_name = cuda_backend.device_name()
    except Exception as error:
        pytest.fail("configured CUDA profiler could not be created: {0}".format(error))
    if actual_device_name != configuration.expected_device_name:
        pytest.fail("configured CUDA device identity does not match expected_device_name")

    def release_cuda_between_deployments() -> None:
        torch_module = importlib.import_module("torch")
        torch_module.cuda.synchronize()
        torch_module.cuda.empty_cache()
        torch_module.cuda.reset_peak_memory_stats()

    off_adapter = ControlFoleyAdapter(
        staged_backend=backend,
        profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES, cuda_backend=cuda_backend),
    )
    off_deployment = controlfoley_staged_local_deployment(
        off_adapter,
        configuration.base.deployment,
        configuration.base.source_dir,
        configuration.base.weights_dir,
        backend.backend_id,
    )
    base_policy = ControlFoleyCachePolicy(
        preprocess_version=configuration.base.preprocess_version,
        code_version=configuration.base.code_version,
    )
    condition_policy = ControlFoleyConditionCachePolicy(
        root=configuration.condition_cache_root,
        codec_version=configuration.condition_codec_version,
        schema_version=configuration.condition_schema_version,
    )
    condition_cache = ControlFoleyConditionCache(base_policy, condition_policy)
    l2_adapter = ControlFoleyAdapter(
        staged_backend=backend,
        profiler=ControlFoleyProfiler(ControlFoleyProfileLevel.STAGES, cuda_backend=cuda_backend),
        cache=ControlFoleyL0L1Cache(base_policy),
        condition_cache=condition_cache,
    )
    l2_deployment = controlfoley_staged_l2_cached_local_deployment(
        l2_adapter,
        configuration.base.deployment,
        configuration.base.source_dir,
        configuration.base.weights_dir,
        backend.backend_id,
        base_policy,
        condition_policy,
    )
    oracle.assert_candidate_configuration(off_deployment.configuration)
    oracle.assert_candidate_configuration(l2_deployment.configuration)
    plan = ControlFoleyL2BenchmarkPlan(
        deployment_manifest_sha256=oracle.deployment_manifest_sha256,
        source_revision=oracle.source_revision,
        checkpoint_sha256=oracle.checkpoint_sha256,
        fixture_manifest_sha256=fixture_sha,
        canonical_invocation_sha256=invocation_sha,
        backend_id=backend.backend_id,
        uncached_deployment_fingerprint=off_deployment.fingerprint,
        l2_deployment_fingerprint=l2_deployment.fingerprint,
        condition_codec_version=condition_policy.codec_version,
        condition_schema_version=condition_policy.schema_version,
        base_cache_policy_fingerprint=base_policy.fingerprint,
        condition_cache_policy_fingerprint=condition_policy.fingerprint,
        repeats=configuration.repeats,
        expected_device_name=actual_device_name,
    )
    plan.assert_cache_bindings(
        condition_codec_version=l2_deployment.configuration["l2_cache_codec_version"],
        condition_schema_version=l2_deployment.configuration["l2_cache_schema_version"],
        base_cache_policy_fingerprint=l2_deployment.configuration["cache_policy_fingerprint"],
        condition_cache_policy_fingerprint=l2_deployment.configuration[
            "l2_cache_policy_fingerprint"
        ],
    )
    off_runtime = _runtime(off_adapter)
    l2_runtime = _runtime(l2_adapter)
    off_session = off_runtime.load(off_deployment)
    l2_session = None

    def prepare(cell: str, repeat: int) -> None:
        nonlocal off_session, l2_session
        del repeat
        if cell == "l2_cold" and l2_session is None:
            if off_session is None:
                pytest.fail("uncached session was not resident before the L2 transition")
            off_runtime.unload(off_session)
            off_session = None
            try:
                release_cuda_between_deployments()
            except Exception as error:
                pytest.fail("CUDA deployment transition cleanup failed: {0}".format(error))
            l2_session = l2_runtime.load(l2_deployment)
        if cell == "l2_cold":
            l2_adapter.invalidate_cache(l2_deployment.fingerprint)

    def execute(cell: str, repeat: int) -> ControlFoleyL2BenchmarkObservation:
        runtime, session = (
            (off_runtime, off_session)
            if cell == "off"
            else (
                l2_runtime,
                l2_session,
            )
        )
        if session is None:
            pytest.fail("benchmark cell has no resident model session")
        result = _invoke(runtime, session, request, "gpu-l2-{0}-{1}".format(cell, repeat))
        counters = result.metadata["condition_execution"]
        if counters.get("observable") is not True:
            pytest.fail("configured L2 benchmark backend lacks observable feature splitting")
        if not isinstance(counters["encoder_calls"], int) or not isinstance(
            counters["projection_calls"], int
        ):
            pytest.fail("configured L2 benchmark returned invalid condition counters")
        return ControlFoleyL2BenchmarkObservation(
            output_sha256=hashlib.sha256(result.artifacts[0].content).hexdigest(),
            cache=result.cache,
            profile=result.profile,
            encoder_calls=int(counters["encoder_calls"]),
            projection_calls=int(counters["projection_calls"]),
            device_name=actual_device_name,
        )

    try:
        evidence = ControlFoleyL2BenchmarkRunner(time.monotonic).run(plan, execute, prepare)
        post_manifest = ControlFoleyDeploymentManifest.from_dict(
            load_json(configuration.base.deployment)
        )
        oracle.assert_manifest(post_manifest)
        try:
            require_controlfoley_gpu_profile_preflight(
                configuration.base.source_dir,
                configuration.base.weights_dir,
                post_manifest,
            )
        except (ControlFoleyProfileSealError, GpuPrerequisitesUnavailable) as error:
            pytest.fail("L2 benchmark prerequisites changed: {0}".format(error))
        if (
            manifest_sha256(
                FixtureManifest.from_dict(load_json(configuration.base.fixture)).to_dict()
            )
            != fixture_sha
        ):
            pytest.fail("fixture manifest changed during the L2 benchmark")
        if controlfoley_invocation_fingerprint(request) != invocation_sha:
            pytest.fail("benchmark invocation inputs changed during execution")
    finally:
        if off_session is not None:
            off_runtime.unload(off_session)
        if l2_session is not None:
            l2_runtime.unload(l2_session)
        release_cuda_between_deployments()
        condition_cache.close()

    assert len({sample.output_sha256 for sample in evidence.samples}) == 1
    for sample in evidence.samples:
        assert sample.wall_time_seconds > 0 and math.isfinite(sample.wall_time_seconds)
        assert sample.projection_calls == 1
        if sample.cell == "l2_warm":
            assert sample.encoder_calls == 0 and sample.cache_hits >= 1
        elif sample.cell in ("off", "l2_cold"):
            assert sample.encoder_calls >= 1
    serialized = json.dumps(_jsonable(evidence.to_dict()), sort_keys=True)
    assert "/" not in serialized and "token" not in serialized.lower()
    for forbidden in ("speedup", "threshold", "parity", "claim"):
        assert forbidden not in serialized.lower()

    def assert_final_seal() -> None:
        # Backend unload/cache close are operator code and may not be trusted
        # to preserve the seal. The atomic writer calls this after serialization
        # and immediately before opening its private temporary file.
        try:
            final_manifest = ControlFoleyDeploymentManifest.from_dict(
                load_json(configuration.base.deployment)
            )
            oracle.assert_manifest(final_manifest)
            require_controlfoley_gpu_profile_preflight(
                configuration.base.source_dir,
                configuration.base.weights_dir,
                final_manifest,
            )
            final_fixture_sha = manifest_sha256(
                FixtureManifest.from_dict(load_json(configuration.base.fixture)).to_dict()
            )
            final_invocation_sha = controlfoley_invocation_fingerprint(request)
            plan.assert_cache_bindings(
                condition_codec_version=condition_policy.codec_version,
                condition_schema_version=condition_policy.schema_version,
                base_cache_policy_fingerprint=base_policy.fingerprint,
                condition_cache_policy_fingerprint=condition_policy.fingerprint,
            )
            if cuda_backend.device_name() != plan.expected_device_name:
                raise ValueError("CUDA device identity changed during benchmark cleanup")
        except (
            ControlFoleyProfileSealError,
            GpuPrerequisitesUnavailable,
            OSError,
            TypeError,
            ValueError,
        ) as error:
            pytest.fail("L2 benchmark prerequisites changed during cleanup: {0}".format(error))
        if final_fixture_sha != fixture_sha:
            pytest.fail("fixture manifest changed during benchmark cleanup")
        if final_invocation_sha != invocation_sha:
            pytest.fail("benchmark invocation inputs changed during cleanup")

    write_controlfoley_l2_benchmark_evidence(
        configuration.base.evidence_output, evidence, assert_final_seal
    )


def _jsonable(value: object) -> object:
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def test_l2_key_seal_mutation_and_no_forbidden_boundaries(tmp_path: Path) -> None:
    root = _root(tmp_path)
    base = _base_policy()
    policy = _condition_policy(root)
    cache = ControlFoleyConditionCache(base, policy)
    configuration = _configuration(base, policy)
    parent = cache.parent_key(
        _request(), configuration, configuration["staged_deployment_fingerprint"]
    )
    key = cache.key_for_parent(parent, "text")
    assert replace(key, codec_version="another-codec-v1").digest != key.digest
    for field, value in (
        ("source_revision", "1" * 40),
        ("deployment_manifest_sha256", _digest("new-manifest")),
        ("checkpoint_sha256", _digest("new-checkpoint")),
        ("variant", "another-variant"),
        ("precision", "fp16"),
    ):
        changed_parent = replace(parent, **{field: value})
        assert cache.key_for_parent(changed_parent, "text").digest != key.digest
    serialized = repr(key.canonical_bytes)
    assert str(root) not in serialized and "token" not in serialized.lower()
    sources = "\n".join(
        Path(path).read_text(encoding="utf-8")
        for path in (
            "src/nano_aural_runtime_controlfoley/condition_cache.py",
            "src/nano_aural_runtime_controlfoley/safe_tensor.py",
        )
    ).lower()
    for forbidden in (
        "import pickle",
        "torch.save",
        "torch.load",
        "flow_matching",
        "latent",
    ):
        assert forbidden not in sources
    core = "\n".join(
        path.read_text(encoding="utf-8") for path in Path("src/nano_aural_runtime").glob("*.py")
    )
    assert "ControlFoleyConditionCache" not in core
