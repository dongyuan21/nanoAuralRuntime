# nanoAuralRuntime Roadmap

## Delivery rule and taxonomy

One delivery Phase equals exactly one PR. A Phase is not complete until all of
its tests pass and its Gate is recorded as passed in that PR. A failed or unrun
Gate blocks every dependent Phase; it must be fixed, narrowed, or explicitly
replanned in the same Phase. Do not combine Phases in one PR.

The source plans in `docs/source-plans/` are archived research input only. The
files `plans/0001-runtime-core.md` through `plans/0006-stable-audio-3-and-woosh-v2a.md`
are programme plans, not one-PR phase definitions. Their lettered slices map
to the following delivery Phases exactly:

| Programme plan slice | Delivery Phase / one PR |
| --- | --- |
| `0001` | Phase 1 |
| `0002-A`, `0002-B` | Phases 2A, 2B |
| `0003-A` through `0003-E` | Phases 3A through 3E |
| `0004-A` through `0004-D` | Phases 4A through 4D |
| `0005-A` through `0005-C` | Phases 5A through 5C |
| `0006-7A`, `0006-7B` | Phases 7A, 7B |
| `0006-8A` through `0006-8E` | Phases 8A through 8E |
| `0006-9A` through `0006-9D` | Phases 9A through 9D |
| `0006-10A`, `0006-10B` | Phases 10A, 10B |
| `0006-11` | Phase 11 |

Phase 0 and Phase 6 are likewise one PR each. `plans/STATUS.md` reports these
delivery phases and is not a substitute for their Gates. The phases below
deliberately replace the research plan's ControlFoley-shaped Core with the
model-agnostic architecture in `ARCHITECTURE.md`. Phases 7A–11 add Stable
Audio 3 Small-SFX and Woosh V2A without widening that Core. Phase 6 hardware
deferral does not block independent Phases 7A–9C software slices.

## Phase 0 — Architecture bootstrap

**Scope:** establish the project charter, architecture, ADRs, roadmap, and
agent delivery rules.

**Non-goals:** production Python, upstream checkout, model execution, API,
database schema, CUDA work, and benchmarks.

**Deliverables:** the authoritative bootstrap documents and archived-research
designation.

**Tests:** terminology review: Core terms are model-neutral; ControlFoley terms
appear only in adapter-specific context; all phases declare the required fields.

**Gate:** documents agree on the fixed layers, frontend independence, and the
one-Phase/one-PR rule.

## Phase 1 — Model-agnostic Runtime Core

**Scope:** implement contracts, adapter lifecycle, immutable deployments,
session state, execution context, cancellation, generic result/artifact
reporting, zero-detail `ProfileReport`/`CacheReport` value contracts, and a
local executor test harness.

**Non-goals:** ControlFoley implementation, durable service, database,
ComfyUI, performance optimization, and model-specific request fields.

**Deliverables:** tested Core packages and contract fixtures using a fake
adapter; capability negotiation, lifecycle documentation, and stable empty or
adapter-supplied profile/cache report types (not profiler or cache engines).

**Tests:** load/invoke/unload lifecycle; fault handling; cancellation;
single-flight enforcement; Core import-boundary tests; fake-adapter contract
tests; stable profile/cache report contract tests.

**Gate:** a non-generative fake adapter and a generative-shaped fake adapter
both run through the same Core without adding model-specific fields to it.

## Phase 2A — ControlFoley upstream baseline harness

**Scope:** lock the supported upstream source, dependencies, weights, fixtures,
and baseline-run metadata; document the reproducible upstream parity harness.

**Non-goals:** implementing the adapter or CLI, changing upstream algorithms,
durable serving, ComfyUI, caching, optimization, or claiming measurements.

**Deliverables:** source/weight/dependency manifests, sanitized fixture and
environment manifests, self-repeat comparison schema, and a 4090 runbook.

**Tests:** manifest/schema validation, absent-source/weight diagnostics, and
test collection that marks CUDA/source/weight-dependent tests as skipped when
their prerequisites are absent.

**Gate:** provenance and runbook are complete, no prohibited artifact is
committed, and non-GPU checks pass. The real 4090 baseline is recorded as
`DEFERRED`, never passed; it does not block independent Phase 3 work.

## Phase 2B — ControlFoley adapter and local CLI

**Scope:** implement the ControlFoley-owned task schema, `upstream_parity`
adapter boundary, compatibility/source guards, and local CLI on the Phase 1
Core.

**Non-goals:** staged execution, cache, profiler, changed upstream semantics,
durable service, ComfyUI, or performance claims.

**Deliverables:** adapter package, task schemas/workflows, deployment/source
validation, compatibility shims, local CLI, and conditional GPU test commands.

**Tests:** CPU task/configuration validation, source-origin conflicts,
cancellation contract, CLI serialization, and Core-isolation tests; GPU smoke
and parity tests skip when their declared prerequisites are absent.

**Gate:** Phase 2A and all available CPU checks pass; the Core remains
model-neutral and ComfyUI-free. GPU smoke/parity is `DEFERRED`, not passed.
This enables only the dependencies explicitly listed below, never a parity or
performance claim.

## Phase 3A — Durable contracts, schema, and local storage

**Scope:** define and implement verified asset, job, attempt, artifact, and
deployment persistence; local blob storage; a fake runtime worker; idempotency;
and state-transition guards.

**Non-goals:** real GPU inference, public API exposure, S3 multipart upload,
or ComfyUI.

**Deliverables:** migrations, repositories, state machine, fake-worker
integration environment, and invariant tests.

**Tests:** only verified assets enter jobs; duplicate idempotency behavior;
concurrent claim exclusivity; state-machine/property tests; fake end-to-end
success, terminal failure, retry, and cancellation.

**Gate:** Phase 1 and all local database tests pass; constraints prove that no
job can have two legal current attempts or two visible winners. This Phase
depends only on Phase 1, not on any ControlFoley or GPU validation.

## Phase 3B — Verified upload and content-addressed assets

**Scope:** implement upload sessions, direct upload contracts, streaming
full-file SHA-256 verification, media probe, canonical promotion, deduplication,
and staging cleanup.

**Non-goals:** GPU execution, worker fencing, ETag-as-identity, or ComfyUI.

**Deliverables:** verifier, BlobStore contracts, upload CLI state, canonical
key policy, and janitor behavior.

**Tests:** bad SHA rejection, multipart identity separation, verified-only
asset guards, deduplication, and Local/S3-compatible contract tests.

**Gate:** Phase 3A passes and only verified full-file-SHA-256 assets can enter
a job.

## Phase 3C — Queue, lease, and fake-worker execution

**Scope:** add transactional claim, monotonic lease epochs, heartbeat CAS,
reaping, retry/backoff, cancellation, input materialization, and fake-worker
execution.

**Non-goals:** real model invocation, ControlFoley import, GPU requirement, or
artifact publication finalization.

**Deliverables:** queue/lease repositories, worker skeleton, recovery policy,
and fake-runtime integration environment.

**Tests:** concurrent claims, heartbeat/reaper, stale-worker rejection,
cancellation race, input revalidation, and process-fault recovery with a fake
adapter.

**Gate:** Phase 3B passes; two workers cannot be legal executors and a stale
worker cannot heartbeat or finalize.

## Phase 3D — Fenced GPU-worker integration

**Scope:** connect the durable worker to `Runtime.invoke()` and the Phase 2B
adapter, with one process/session single-flight behavior and per-attempt
workspaces.

**Non-goals:** ComfyUI, exactly-once inference, batching, or unmeasured GPU
claims.

**Deliverables:** direct headless worker integration, process-fault policy, and
conditional GPU smoke/recovery commands.

**Tests:** no-ComfyUI import boundary, fake/CPU worker integration, and
conditional ControlFoley worker tests that skip without the declared host,
source, weights, and fixtures.

**Gate:** Phases 3C and 2B pass. The worker calls Runtime directly and its
non-GPU integration tests pass. The 4090 smoke/recovery evidence remains
`DEFERRED`, not passed.

## Phase 3E — Verified artifacts, API, and remote CLI

**Scope:** add output validators, immutable attempt publication, finalization
CAS, orphan handling, authorized download, job API/remote CLI, metrics, and
the recovery runbook.

**Non-goals:** a successful model call as proof of job success, ComfyUI state
authority, or unsupported reliability claims.

**Deliverables:** artifact/API/client path, structured observability, Compose
reference environment, and recovery documentation.

**Tests:** validator failures, publication-boundary crashes, cancel/finalize
races, winning-result uniqueness, download SHA verification, and CPU fake
end-to-end recovery.

**Gate:** Phase 3D passes; exactly one verified winner is visible and
`SUCCEEDED` implies required READY artifacts. Full 4090 remote E2E remains
`DEFERRED` until the designated validation host is available.

## Phase 4A — Experimental staged parity path

**Scope:** split ControlFoley-specific stages behind the adapter while retaining
`upstream_parity` as the selected default/oracle.

**Non-goals:** default staged execution, parity claims, solver/default changes,
or performance claims.

**Deliverables:** an explicitly experimental staged path and comparison
plumbing.

**Tests:** stage contract tests and comparison plumbing; any GPU comparison is
skipped when its declared prerequisites are absent.

**Gate:** Phase 2B passes and the staged path is adapter-contained, selectable,
and non-default. Measured parity is `DEFERRED`; its absence blocks default
enablement but not experimental implementation.

## Phase 4B — Experimental profiler

**Scope:** add generic profile reports and ControlFoley adapter stages without
synthetic CUDA timing.

**Non-goals:** a performance claim, inferred GPU measurements on CPU, cache,
or a default-path change.

**Deliverables:** profile levels, stage schemas, CPU timing, conditional CUDA
event timing, and truthful unavailable-capability reporting.

**Tests:** CPU profile validity, unavailable CUDA fields, and stage-accounting
tests.

**Gate:** Phase 4A passes and observations do not alter semantics. 4090 timing
evidence is `DEFERRED`; it blocks performance claims only.

## Phase 4C — Experimental L0/L1 cache

**Scope:** add bounded metadata and deterministic preprocessing caches with
generic cache reports and invalidation.

**Non-goals:** L2 features, sampling/trajectory cache, quality changes, or a
performance claim.

**Deliverables:** L0/L1 cache policy, bounded storage, invalidation/versioning,
metrics, and cache documentation.

**Tests:** cache-key mutation, corruption/miss, eviction/limits, invalidation,
and cache-on/off semantic equivalence.

**Gate:** Phase 4B passes; cache is result-preserving and remains experimental
until the deferred parity evidence permits a default-path decision.

## Phase 4D — Experimental L2 condition cache and benchmark tooling

**Scope:** add adapter-owned video/reference/text feature cache and reproducible
benchmark tooling.

**Non-goals:** default L2 use, acceleration claims, step/latent cache, or
quality-altering reuse.

**Deliverables:** safe disk persistence, feature-cache lifecycle/metrics,
encoder counters, and benchmark matrix tooling.

**Tests:** L2 key/invalidation/corruption tests, encoder-counter tests, and
conditional warm/cold GPU benchmark commands.

**Gate:** Phase 4C passes; L2 cache is result-preserving in available tests and
remains non-default. Real parity and benchmark evidence remain `DEFERRED`.

## Phase 5A — Optional embedded ComfyUI frontend

**Scope:** add thin embedded nodes that translate ComfyUI values to the
established local Runtime/ControlFoley contracts.

**Non-goals:** ComfyUI below the integration boundary, duplicate execution
code, UI state authority, or remote-service features.

**Deliverables:** embedded nodes, mapping/cancellation conversion, origin
conflict guard, lifecycle rules, and example workflows.

**Tests:** node mapping/errors/cancellation, boundary-import tests, and
conditional local GPU smoke tests.

**Gate:** Phase 2B passes; the integration is removable and does not broaden
Core contracts. UI GPU smoke is `DEFERRED` when hardware is unavailable.

## Phase 5B — Optional remote ComfyUI frontend

**Scope:** add remote upload/submit/status/fetch nodes using the public client
and durable API.

**Non-goals:** local model dependencies, CUDA in the remote package, or a new
job/artifact state machine.

**Deliverables:** remote nodes, progress presentation, download authorization,
and example workflows.

**Tests:** fake-runtime remote integration, package/import isolation, and
conditional GPU UI smoke tests.

**Gate:** Phase 3E passes; remote nodes require no model dependencies and use
existing durable authority. Hardware UI validation remains `DEFERRED`.

## Phase 5C — ComfyUI coexistence and removability hardening

**Scope:** validate official-plugin coexistence/conflict handling and removal
of the optional integration.

**Non-goals:** making a plugin a dependency or relaxing headless tests.

**Deliverables:** A/B checks, diagnostics, compatibility documentation, and
no-ComfyUI CI coverage.

**Tests:** official-plugin conflict behavior and deletion/omission regression
checks for Core, CLI, API, and worker.

**Gate:** Phases 5A and 5B pass; deleting the integration leaves all headless
checks passing.

## Phase 6 — Release hardening

**Scope:** perform security, operations, migration, license, benchmark, and
release-readiness work.

**Non-goals:** silently relaxing earlier gates or introducing a new execution
model.

**Deliverables:** runbooks, SBOM/dependency evidence, model license notices,
sanitized reproducible benchmark evidence, and release notes.

**Tests:** backup/restore, migration compatibility, security checks, full
recovery matrix, release smoke tests, and the deferred hardware suite.

**Gate:** every dependent Gate is green; deferred ControlFoley GPU parity and
suitable-hardware benchmark evidence are completed; no release claim exceeds
the evidence.

## Phase 7A — Multi-adapter architecture freeze

**Scope:** freeze the second-adapter programme in documents only: Stable Audio 3
Small-SFX as text-to-SFX, Woosh as V2A-only with `dvflow-8s` / `vflow-8s`
Deployments, isolated Worker environments, and plugin/routing ADRs.

**Non-goals:** production Python, new adapters, torch in the root package,
Woosh T2A/Flow/DFlow, Core contract changes, weights, and hardware claims.

**Deliverables:** `plans/0006-stable-audio-3-and-woosh-v2a.md`, ADR 0003,
ADR 0004, Roadmap/Status updates, and the source/dependency/license research
note.

**Tests:** terminology review. Core terms stay model-neutral. Woosh T2A terms
appear only as explicit out-of-scope. One-Phase/one-PR mapping is complete.

**Gate:** documents agree on the product split, environment isolation, Woosh
V2A-only scope, DVFlow default, VFlow selectable, 8-second fail-closed window,
and no production Python in this PR. This Phase depends on Phase 1 and does
not wait for Phase 6 hardware evidence.

## Phase 7B — Plugin, builder registry, and Worker routing

**Scope:** implement torch-free adapter plugin metadata, a
`DurableInvocationBuilder` registry, Worker capability descriptors,
deployment-aware claim filtering, and a generic `nano-aural` dispatcher with
ControlFoley compatibility.

**Non-goals:** Stable Audio or Woosh model code, Core field additions, torch on
the root package, and ComfyUI changes.

**Deliverables:** plugin metadata, builder registry, capability routing, CLI
dispatcher, and ControlFoley regression coverage.

**Tests:** discovery without torch; ControlFoley `--help` and local CLI path;
builder rejection of operator-only fields on Jobs; capability mismatch
fail-closed; Core import-boundary scan.

**Gate:** Phase 7A passes. No new model backend lands. ControlFoley remains the
only executable adapter.

## Phase 8A — Stable Audio 3 baseline and provenance

**Scope:** lock the official Small-SFX source, gated HF identity, dependencies,
and 5s / 30s / 120s baseline configs behind a direct runner.

**Non-goals:** the production adapter, durable Worker, editing modes, LoRA,
optimization, or a performance claim.

**Deliverables:** manifests, prepare runbook, self-repeat schema, and
conditional GPU collection.

**Tests:** schema validation, absent-source/weight diagnostics, gated-access
diagnostics, and skip-clean GPU tests.

**Gate:** Phase 7A passes. Provenance is complete and no prohibited artifact is
committed. Real 4090 evidence is `DEFERRED`.

## Phase 8B — Stable Audio 3 local adapter and CLI

**Scope:** implement `audio.text_to_sfx` on the Phase 1 Core with an isolated
session, local CLI, and 44.1 kHz stereo WAV validation.

**Non-goals:** A2A/inpaint/continuation, durable Worker, cache, ComfyUI, and
unmeasured speed claims.

**Deliverables:** `nano_aural_runtime_stable_audio_3`, sealed deployment, local
CLI, and conditional GPU tests.

**Tests:** CPU request/deployment validation, source/weight mismatch
fail-closed, CLI serialization, Core isolation; GPU smoke skipped when absent.

**Gate:** Phases 7B and 8A pass. Core remains model-neutral. GPU parity is
`DEFERRED`.

## Phase 8C — Stable Audio 3 durable Worker

**Scope:** connect the durable Worker to the Small-SFX adapter through the
Phase 7B registry, with an isolated environment and verified publication.

**Non-goals:** editing modes, ComfyUI, batching, and treating a model call as
job success.

**Deliverables:** invocation builder, environment lock, capability routing,
artifact validation, remote CLI, and recovery tests.

**Tests:** fake/CPU worker integration, sealed-field rejection, publication
uniqueness, and conditional GPU worker tests that skip without prerequisites.

**Gate:** Phases 8B and 3E pass. Non-GPU integration tests pass. 4090 worker
evidence remains `DEFERRED`.

## Phase 8D — Stable Audio 3 editing extensions (optional)

**Scope:** adapter-owned `audio_to_audio`, inpaint, and continuation.

**Non-goals:** blocking Woosh V2A, Core changes, and default-path edits.

**Gate:** Phase 8B passes. This Phase is optional and does not gate 9A–9C.

## Phase 8E — Stable Audio 3 profiler and cache (optional)

**Scope:** experimental, default-off profile stages and text-conditioning cache.

**Non-goals:** blocking Woosh V2A, semantic change, and performance claims.

**Gate:** Phase 8B passes. 4090 cache evidence is `DEFERRED`. This Phase is
optional and does not gate 9A–9C.

## Phase 9A — Woosh VFlow/DVFlow baseline and provenance

**Scope:** lock SonyResearch/Woosh `v1.0.0` (or a later reviewed pin),
Woosh-AE, TextConditionerV, Woosh-VFlow-8s, Woosh-DVFlow-8s, and the MMAudio
Synchformer checkpoint. Official direct-run harness for the 8-second window.

**Non-goals:** inspecting or adapting Woosh-Flow, Woosh-DFlow, TextConditionerA,
or any T2A path; production adapter; durable Worker; profiler/cache.

**Deliverables:** source/release/archive manifests, 8-second fixture contract,
optional-prompt and same-seed comparison schema, and a 4090 runbook.

**Tests:** manifest validation, absent-source/weight/Synchformer diagnostics,
and skip-clean GPU collection. Do not commit weights or media.

**Gate:** Phase 7A passes. Provenance is complete. GPU evidence is `DEFERRED`.

## Phase 9B — Woosh V2A local adapter and CLI

**Scope:** one `woosh-v2a` adapter, operation `audio.video_to_sfx`, sealed
backends `dvflow-8s` (default) and `vflow-8s`, 8-second window from 0, reject
shorter video, 48 kHz mono WAV, no mux.

**Non-goals:** Woosh T2A, Flow/DFlow, exposing solver/CFG/renoise on
`ModelInvocation`, Core fields, durable Worker, cache, and ComfyUI.

**Deliverables:** `nano_aural_runtime_woosh`, local CLI, provenance
revalidation, and conditional GPU tests.

**Tests:** CPU schema and window policy, backend selection, source/checkpoint/
Synchformer mismatch fail-closed, Core isolation; GPU parity skipped when
absent.

**Gate:** Phases 7B and 9A pass. Core remains model-neutral. GPU parity is
`DEFERRED`.

## Phase 9C — Woosh durable Worker

**Scope:** Woosh V2A invocation builder, isolated environment, capability
routing, verified video materialization, Runtime invoke, publication, and
recovery.

**Non-goals:** T2A, solver overrides on Jobs, ComfyUI, and in-adapter mux.

**Deliverables:** Worker binding, environment lock, remote CLI, and recovery
tests.

**Tests:** fake/CPU integration, video-role requirements, sealed-field
rejection, winning-result uniqueness, and conditional GPU tests that skip
without prerequisites.

**Gate:** Phases 9B and 3E pass. Non-GPU integration tests pass. 4090 worker
evidence remains `DEFERRED`.

## Phase 9D — Woosh profiler and cache

**Scope:** experimental, default-off stages and caches for video preprocessing,
Synchformer features, text tokens, and empty/unconditional conditions.

**Non-goals:** ODE trajectory cache, latent reuse, step skipping, cross-seed
reuse, default enablement, and performance claims.

**Gate:** Phase 9B passes. Cache remains result-preserving and non-default.
4090 evidence is `DEFERRED`.

## Phase 10A — Unified SFX workflows and optional ComfyUI mapping

**Scope:** workflows `sfx.text_generate` (Stable Audio 3 Small-SFX) and
`sfx.video_generate` (ControlFoley, Woosh-DVFlow-8s, Woosh-VFlow-8s), plus an
explicit `sfx.generate_and_mux` that is not a model adapter step.

**Non-goals:** making ComfyUI a job authority, duplicating execution code, and
adding Woosh T2A nodes.

**Gate:** Phases 8B, 9B, and 5C pass. Integrations remain removable.

## Phase 10B — RTX 4090 multi-adapter evidence

**Scope:** record sanitized Stable Audio 5s/30s/120s, Woosh DVFlow/VFlow 8s,
and existing ControlFoley workloads on the designated host.

**Non-goals:** fabricating measurements, marking skipped tests passed, or
claiming speedups.

**Gate:** Phases 8B and 9B pass. This Gate remains `DEFERRED` until the host
is available and does not block independent software slices.

## Phase 11 — Second-adapter release hardening

**Scope:** Stability Community License and Gemma notices, gated HF token
handling, Woosh MIT/Apache-2.0 and CC BY-NC weight notices, Synchformer/
MMAudio attribution, and release evidence for the new families.

**Non-goals:** silently relaxing earlier Gates or claiming a combined product
release while ControlFoley Phase 6 hardware evidence remains deferred.

**Gate:** software slices for 7A–9C are green; hardware evidence for the new
families is recorded or explicitly deferred; no release claim exceeds evidence.
