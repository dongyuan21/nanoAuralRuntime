# Execution Status

Last updated: 2026-08-18. The files in `docs/source-plans/` are archived research inputs. `plans/0002`–`0006` are programme plans; their Roadmap Phase subitems are independently gated and each one equals one PR.

| Phase | Current status | Entry condition | Hardware evidence |
|---|---|---|---|
| P0 Architecture bootstrap | complete | Document Gate passed: authoritative terminology, dependency direction, and one-Phase/one-PR mapping are consistent | Not applicable |
| P1 Runtime Core | complete | 18 CPU tests, Ruff lint/format, Pyright, import-boundary scan, and lifecycle/single-flight review passed | Not applicable |
| P2A ControlFoley baseline | complete (non-hardware Gate passed) | Source-level provenance locks the upstream direct runner to `large_44k`/FP32; manifests, isolated execution, exact bindings, 9 CPU tests, Ruff, Pyright, and Core-boundary checks agree. | RTX 4090 baseline **DEFERRED — release-blocking** |
| P2B adapter + local CLI | complete (non-hardware Gate passed) | 41 targeted CPU tests, sealed fixture/deployment binding, execute-time provenance revalidation, child module-origin guards, process-group cancellation, waveform-comparison plumbing, Ruff, Pyright, Core-boundary, and independent P0/P1 review passed | RTX 4090 parity **DEFERRED — release-blocking** |
| P3A durable contracts/schema | complete | Real PostgreSQL 16 migration/repository Gate: 30 tests passed; invariant, append-only, LocalBlobStore, Ruff, Pyright, Core-boundary, and independent P0/P1 review passed | Not applicable |
| P3B verified upload | complete | Real PostgreSQL upload/reclaim/expiry/concurrency Gate, streaming SHA and full-frame media validation, bounded command probe, Local/S3-compatible contracts, multipart/ETag separation, canonical deduplication, verified-only jobs, Ruff, Pyright, and independent P0/P1 review passed | Not applicable |
| P3C queue/lease skeleton | complete | Real PostgreSQL 16 full-repository Gate: 115 passed with 2 expected GPU skips; overlap claim/reaper, DB-owned materialization, legacy-bypass quarantine, Ruff/format, Pyright, migration mirrors, and independent Core-boundary Gate all passed | Not applicable |
| P3D GPU worker integration | complete | Independent Gate PASS: real-PG and full-suite validation completed with 149 passed and 3 expected conditional GPU skips | RTX 4090 worker smoke **DEFERRED — release-blocking** |
| P3E artifacts/API/remote CLI | complete (non-hardware Gate passed) | Independent Gate PASS: real PostgreSQL 16 full-repository validation completed with 329 passed and 6 expected conditional GPU skips; publication, API/auth, remote CLI, bounded recovery, Ruff/format, Pyright, migration mirrors, and Core-boundary checks passed. Docker daemon validation is explicitly UNRUN because no container CLI is installed. | RTX 4090 remote E2E **DEFERRED — release-blocking** |
| P4A staged path | complete (non-hardware Gate passed) | Independent Gate PASS: 51 targeted tests passed with 3 expected conditional GPU skips; staged execution is adapter-contained, explicit, non-default, and auditable | RTX 4090 parity **DEFERRED — release-blocking** |
| P4B profiler | complete (non-hardware Gate passed) | Independent Gate PASS: sealed execution provenance, truthful CPU/CUDA capability reporting, failure isolation, 15 profiler tests with 1 expected GPU skip, combined regressions, Ruff/format, and Pyright passed | RTX 4090 profile validation **DEFERRED — release-blocking** |
| P4C L0/L1 cache | complete (non-hardware Gate passed) | Independent Gate PASS: explicit default-off seal, complete semantic keys, input TOCTOU fencing, delayed commit, bounded thread-safe LRU, corruption/fault cold fallback, fail-closed invalidation, truthful CacheReport, 19 tests with 1 expected GPU skip, Ruff/format, and Pyright passed | RTX 4090 cache equivalence **DEFERRED — release-blocking** |
| P4D L2 cache/bench tooling | complete (non-hardware Gate passed) | Independent Gate PASS: persistent fail-closed L2 feature storage, strict data-only safetensors validation, source/weights isolation, truthful encoder/projection counters, single-resident sealed benchmark tooling, 30 targeted tests with 1 expected GPU skip, combined regressions, Ruff/format, and Pyright passed | RTX 4090 benchmark **DEFERRED — release-blocking** |
| P5A ComfyUI embedded | complete (non-hardware Gate passed) | Independent Gate PASS: lifecycle/teardown, fail-closed cancellation, strict operator bootstrap, host-discovered producer/output workflow, removability, Ruff/format, Pyright, and 34 targeted tests with 2 expected GPU skips passed | RTX 4090 UI smoke **DEFERRED — release-blocking** |
| P5B ComfyUI remote | complete (non-hardware Gate passed) | Independent Gate PASS: public RemoteClient/API-only nodes, zero/single/multi-input flows, bounded wait/cancel, strict response schemas, redacted full exception chains, authorized checksum-verified downloads, linked OUTPUT workflow, Ruff/format, Pyright, and 17 tests with 1 expected GPU skip passed | RTX 4090 UI smoke **DEFERRED — release-blocking** |
| P5C ComfyUI removability | complete | Independent Gate PASS: current official/Embedded/Remote node mappings coexist without collisions, three linked v0.4 OUTPUT workflows validate, origin conflicts fail closed, three repository-external physical-omission matrices execute headless Core/CLI/API/Worker paths, and Ruff/format/Pyright pass | Not applicable |
| P6 Release hardening | blocked (non-hardware hardening passed; full Gate unrun) | Independent software slices passed for deterministic packaging, license/notices, checksum-sealed migration and real PostgreSQL 16 dump/restore, least-privilege runtime roles, release canaries, strict artifact/security evidence, digest-pinned reference images, and hash-locked API dependencies. Final frozen-tree validation: 519 passed with 9 explicit GPU skips; Ruff/format, Pyright, migration mirrors, fresh-wheel installs, external SPDX validation, and current API-lock vulnerability audit passed. This does not complete the Roadmap Phase. | Deferred RTX 4090 and real Embedded/Remote ComfyUI evidence remains release-blocking; Docker daemon build/start/restart evidence is **UNRUN** on this host |
| P7A multi-adapter freeze | complete | Document Gate passed: Stable Audio 3 = T2SFX only; Woosh = V2SFX with `dvflow-8s`/`vflow-8s` only; no Woosh T2A/Flow/DFlow; isolated Worker environments; plugin/routing ADRs; Core unchanged; no production Python | Not applicable |
| P7B plugin/worker routing | complete | Independent Gate PASS: torch-free plugin catalog, DurableInvocationBuilder registry, Worker capability matching, generic nano-aural dispatcher, ControlFoley compatibility, and Core-boundary checks | Not applicable |
| P8A Stable Audio baseline | complete (non-hardware Gate passed) | Independent Gate PASS: Small-SFX provenance lock, 5s/30s/120s fixtures, pending weight fingerprints, skip-clean GPU diagnostics, and no gated download in CI | RTX 4090 **DEFERRED** |
| P8B Stable Audio adapter | complete (non-hardware Gate passed) | Independent Gate PASS: audio.text_to_sfx local adapter, WAV 44.1k stereo contract, injected-runner CPU tests, official runner fail-closed without source, CLI dispatcher | RTX 4090 parity **DEFERRED** |
| P8C Stable Audio durable | complete (non-hardware Gate passed) | Independent Gate PASS: text-to-sfx DurableInvocationBuilder, no binary inputs, operator-field rejection, registry registration | RTX 4090 worker **DEFERRED** |
| P8D Stable Audio editing | optional, not started | Entry: P8B; does not gate Woosh | RTX 4090 **DEFERRED** |
| P8E Stable Audio profiler/cache | optional, not started | Entry: P8B; does not gate Woosh | RTX 4090 **DEFERRED** |
| P9A Woosh V2A baseline | not started | Entry: P7A | RTX 4090 **DEFERRED** |
| P9B Woosh V2A adapter | not started | Entry: P7B and P9A | RTX 4090 parity **DEFERRED** |
| P9C Woosh durable | not started | Entry: P9B and P3E | RTX 4090 worker **DEFERRED** |
| P9D Woosh profiler/cache | not started | Entry: P9B | RTX 4090 **DEFERRED** |
| P10A unified workflows | not started | Entry: P8B, P9B, and P5C | RTX 4090 UI **DEFERRED** |
| P10B 4090 multi-adapter | blocked | Hardware host unavailable | RTX 4090 **DEFERRED — release-blocking** |
| P11 second-adapter hardening | not started | Software slices 7A–9C; hardware may remain deferred | Deferred family evidence remains release-blocking for a combined release |

## Current instruction

The user explicitly permits continued development while RTX 4090 validation is unavailable. Therefore each hardware-only gate above is deferred, not failed and not passed. Agents may proceed only after the applicable non-hardware gates pass; they must not manufacture measurements, mark skipped tests as passing, claim parity, or claim performance improvement. This execution waiver does not waive the ControlFoley release Gate: P6 remains blocked until the deferred hardware evidence is actually recorded. Independent Phases 7A–9C may proceed under the same deferral.

Woosh scope is strictly `Woosh-VFlow-8s` and `Woosh-DVFlow-8s`. Do not implement Woosh-Flow, Woosh-DFlow, TextConditionerA, or any Woosh text-to-audio path.

## Required 4090 follow-up

On the designated host, set `CONTROLFOLEY_SOURCE_DIR`, `CONTROLFOLEY_WEIGHTS_DIR`, and `HF_HOME`, then run the documented `pytest -m "gpu and controlfoley" -v` suite plus the baseline/parity/remote commands supplied by the implemented phases. After Phases 8A/9A land, also run the Stable Audio and Woosh V2A commands from those runbooks on isolated environments. Record only sanitized manifests and result summaries—never weights, caches, user media, private paths, tokens, or host identity.

## Cross-phase invariants

- Frontend → Workflow → Durable Service/Local Executor → Runtime Core → Adapter → original backend.
- Core does not depend on ControlFoley, Stable Audio 3, Woosh, database/storage/API, or ComfyUI.
- ControlFoley, Stable Audio 3, and Woosh V2A use isolated Worker environments; the root package has no torch dependency.
- Only verified SHA-256 assets enter jobs; PostgreSQL is the durable state authority.
- Attempts are at-least-once, fenced by monotonic lease epoch; only one verified artifact result may be visible.
- ComfyUI may be removed without breaking Core, CLI, API, or Worker.
- No Woosh T2A, Woosh-Flow, or Woosh-DFlow production path exists in this repository.
