# pyright: reportMissingImports=false
from __future__ import annotations

import pytest

from nano_aural_runtime_woosh.cache import (
    FORBIDDEN_CACHE_KINDS,
    WooshCacheError,
    WooshFeatureCache,
    cache_key,
)
from nano_aural_runtime_woosh.profile import (
    WooshProfileError,
    WooshStageProfiler,
    empty_profile,
    stage_order,
)


def test_profile_is_empty_when_default_off():
    profiler = WooshStageProfiler("dvflow-8s")
    profiler.record("synchformer", 1.5)
    report = profiler.report()
    assert report.metrics == {}
    assert empty_profile().metadata["enabled"] is False


def test_stage_order_uses_backend_specific_sampler():
    assert "dvflow_euler" in stage_order("dvflow-8s")
    assert "vflow_integrate" in stage_order("vflow-8s")
    assert "ode_trajectory" not in stage_order("dvflow-8s")
    profiler = WooshStageProfiler("vflow-8s", mode="stages")
    profiler.record("vflow_integrate", 0.2)
    with pytest.raises(WooshProfileError):
        profiler.record("dvflow_euler", 0.1)
    report = profiler.report()
    assert report.metrics["vflow_integrate"] == 0.2
    assert report.metadata["enabled"] is True


def test_cache_allows_feature_kinds_and_rejects_forbidden_kinds():
    cache = WooshFeatureCache(mode="experimental_features")
    key = cache_key("synchformer_features", {"video_sha256": "a" * 64})
    assert len(key) == 64
    cache.put("text_tokens", {"prompt": "metal"}, b"tokens")
    assert cache.get("text_tokens", {"prompt": "metal"}) == b"tokens"
    assert cache.get("text_tokens", {"prompt": "other"}) is None
    report = cache.report()
    assert report.hits == 1
    assert report.misses == 1
    for kind in FORBIDDEN_CACHE_KINDS:
        with pytest.raises(WooshCacheError):
            cache_key(kind, {"x": 1})
    with pytest.raises(WooshCacheError):
        cache_key("synchformer_features", {"seed": 42})


def test_cache_default_off_never_stores():
    cache = WooshFeatureCache()
    cache.put("video_preprocess", {"video_sha256": "b" * 64}, b"frames")
    assert cache.get("video_preprocess", {"video_sha256": "b" * 64}) is None
    assert cache.report().metadata["enabled"] is False


@pytest.mark.gpu
def test_woosh_profile_cache_gpu_evidence_remains_deferred():
    pytest.skip("Woosh profiler/cache 4090 evidence is deferred")
