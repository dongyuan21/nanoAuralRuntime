# Programme Plan 0004 — Runtime Observability and Cache (Roadmap Phases 4A–4D)

Status: blocked on the Phase 2B `upstream_parity` adapter implementation. This programme plan contains independent Roadmap Phases 4A–4D, each exactly one PR. During the hardware deferral, it may implement only non-default staged/profile/cache plumbing; runtime profiling/caching must retain upstream parity as the oracle.

## Scope

Introduce non-default ControlFoley staged/profile/cache plumbing that remains behind explicit opt-in until it can be measured against upstream parity. Add stage-level timing/VRAM observability and bounded, content-addressed caches for metadata, preprocessing, and condition encoder features. Cache keys incorporate input digest, preprocessing version, source revision, deployment/checkpoint fingerprint, task parameters, precision, and relevant code version.

## Non-goals

- No cache for Flow Matching steps/latents, step skipping, altered solver/default steps, or changed model behavior.
- No CUDA/Triton/TensorRT/ONNX/FP8 kernel work, benchmark-based speed claims without measurements, or ComfyUI cache dependence.
- No cache of unsafe/untrusted pickle data; use safe formats such as `safetensors` for disk tensors.

## Deliverables

### Roadmap Phase 4A (one PR) — staged parity path

Split only ControlFoley-specific stages (media resolution/preprocess, condition encoders/projection, integration, decode/vocoder) behind the adapter. Make `upstream_parity` selectable and authoritative until measured parity permits staged use.

### Roadmap Phase 4B (one PR) — profiler

Add structured stage profile reports, CPU timings, and CUDA Event timings/allocated/reserved/peak memory when CUDA exists. Ensure timing synchronization and capability absence are explicit rather than synthetic.

### Roadmap Phase 4C (one PR) — L0/L1 cache

Implement metadata and deterministic preprocessing cache with byte-bounded LRU, metrics, invalidation/versioning, and cache reports.

### Roadmap Phase 4D (one PR) — L2 condition cache and benchmark tooling

Add video/reference/text encoder feature cache, safe disk persistence, lifecycle cleanup, hit/miss accounting, encoder counters, and reproducible benchmark-matrix tooling.

## Tests

- Stage contracts and staged-versus-upstream comparison plumbing without invented tolerance values.
- Profiler reports valid CPU data; CUDA fields are absent/marked unavailable on CPU.
- Cache key mutation tests, corruption/miss handling, eviction/byte-limit tests, invalidation across deployment/source/preprocess changes, and cache-on/off equivalence tests.
- With a suitable GPU fixture, record measured parity envelope, then run warm/cold cache and encoder-counter tests. Missing GPU prerequisites yield skips.

## Gate

- 4A: staged execution is not made default before measured upstream parity is accepted; it stays explicit opt-in while hardware evidence is deferred.
- 4B–4D: cache changes never alter validated output; same asset warm L2 invocation has encoder counter zero; cache metrics are truthful. Without real parity evidence, all plumbing remains non-default and must not be described as result-preserving in release documentation.
- All 4090 parity/performance benchmarks are **DEFERRED and release-blocking** under the current hardware deferral. No acceleration or parity claim may be published until those measurements exist; the Phase 6 release Gate remains blocked.
