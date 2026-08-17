# nanoAuralRuntime Roadmap

## Delivery rule and taxonomy

One delivery Phase equals exactly one PR. A Phase is not complete until all of
its tests pass and its Gate is recorded as passed in that PR. A failed or unrun
Gate blocks every dependent Phase; it must be fixed, narrowed, or explicitly
replanned in the same Phase. Do not combine Phases in one PR.

The source plans in `docs/source-plans/` are archived research input only. The
files `plans/0001-runtime-core.md` through `plans/0005-comfyui-integration.md`
are programme plans, not one-PR phase definitions. Their lettered slices map
to the following delivery Phases exactly:

| Programme plan slice | Delivery Phase / one PR |
| --- | --- |
| `0001` | Phase 1 |
| `0002-A`, `0002-B` | Phases 2A, 2B |
| `0003-A` through `0003-E` | Phases 3A through 3E |
| `0004-A` through `0004-D` | Phases 4A through 4D |
| `0005-A` through `0005-C` | Phases 5A through 5C |

Phase 0 and Phase 6 are likewise one PR each. `plans/STATUS.md` reports these
delivery phases and is not a substitute for their Gates. The phases below
deliberately replace the research plan's ControlFoley-shaped Core with the
model-agnostic architecture in `ARCHITECTURE.md`.

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
