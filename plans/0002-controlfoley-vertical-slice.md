# Programme Plan 0002 — ControlFoley Vertical Slice (Roadmap Phases 2A–2B)

Status: ready after Phase 1. This programme plan contains the two independent Roadmap Phases 2A and 2B; each Roadmap Phase is exactly one PR. ControlFoley is an adapter/workflow, never a Core abstraction.

The supported upstream source baseline is `xiaomi-research/controlfoley` revision `6858cd12a48d141201e3266e7abe1f38357a133e`. The direct upstream path is the parity oracle. Runtime execution must not transit through ComfyUI.

## Scope

Implement a reproducible official baseline harness, then an `upstream_parity` ControlFoley adapter, ControlFoley-only task schema, source/weight/dependency manifests, compatibility checks, and a local CLI. Cover V2A, TV2A, TC-V2A, AC-V2A, and T2A configurations where upstream capabilities and fixtures are available. Model session remains one worker process / one deployment / one invocation at a time.

## Non-goals

- No staged pipeline, feature cache, profiler, compile tuning, reduced steps, solver changes, CUDA/Triton/TensorRT/ONNX, or performance claim.
- No API, database, object store, durable worker, remote CLI, or ComfyUI node.
- No upstream edits, weights/HF cache/media fixtures/private paths committed to the repository.
- No assertion that GPU smoke/parity or a 4090 benchmark passed until actually run on that host.

## Deliverables

### Roadmap Phase 2A (one PR) — upstream baseline harness

- Direct runner calling the upstream implementation; it is not the formal adapter.
- Configuration/fixture manifests capturing upstream revision, checkpoint and input fingerprints, task, seed, precision, steps, guidance, waveform shape/rate, timing, peak allocated/reserved VRAM, and sanitized environment manifest.
- Self-repeat comparison/reporting for shape, finite samples, peak/RMS, MAE, maximum error, cosine similarity, and mel distance. Thresholds are recorded from measurements, never invented.
- Clear 4090 runbook; GPU tests have `gpu` and `controlfoley` markers and skip cleanly when CUDA, source, weights, or fixtures are absent.

### Roadmap Phase 2B (one PR) — adapter and local CLI

- `ControlFoleyAdapter`, its task schemas/workflows, deployment validation, source module-origin guard, checkpoint fingerprint validation, compatible precision/device checks, and narrowly scoped upstream compatibility shims.
- `upstream_parity` invokes the original model directly, supports cancellation at the adapter boundary, and produces a Runtime result plus manifest.
- Local CLI materializes only user-local inputs in local mode and calls the generic Runtime; it does not broaden Core types.
- CPU configuration/validation tests plus conditional GPU smoke/parity tests.

## Tests

- CPU: manifest and task validation, source-origin conflict detection, missing/invalid source and checkpoint handling, CLI serialization, and Core isolation regression tests.
- Conditional GPU: direct upstream baseline and adapter output comparisons for supported task fixtures; load/invoke/unload/reload; cancellation; invalid reference media.
- 4090 commands and expected result locations are documented. On hosts without the required CUDA/source/weights, pytest reports **skipped**, not passed.

## Gate

- Roadmap Phase 2A: baseline metadata is complete and no prohibited artifact is committed. The actual 4090 baseline gate is **deferred by explicit project direction** until the designated host is available; it is recorded as `DEFERRED`, never “passed”.
- Roadmap Phase 2B: Core remains model-agnostic and ComfyUI-free; adapter uses only `upstream_parity`; CPU validation passes. Its 4090 parity/smoke gate is likewise **deferred/skipped only because the required host is intentionally unavailable**, with exact commands retained in `STATUS.md`.
- The deferred hardware gates do not authorize fabricated measurements or a speed/parity claim. They are outstanding release evidence; subsequent non-hardware development may continue under the user-authorized deferral.
