# Phase 1 — Model-agnostic Runtime Core

Status: ready for implementation. This is one PR and must land before any model-specific or service work.

## Scope

Build a CPU-only, model-agnostic runtime package around these concepts: `AudioModelAdapter`, `ModelDescriptor`, `ModelDeployment`, `ModelSession`, `ModelInvocation`, `InvocationResult`, `ProducedArtifact`, `ExecutionContext`, `CancellationToken`, `ProfileReport`, `CacheReport`, typed errors, `AdapterRegistry`, and `Runtime.invoke()`.

The session lifecycle is explicit (`UNLOADED → LOADING → READY → RUNNING → READY|FAILED → UNLOADING → UNLOADED`). A deployment/session is single-flight: concurrent invocation is deterministically serialized or rejected, and unload is prohibited while running. Include a fake/echo adapter so the complete lifecycle is testable without PyTorch or a GPU.

## Non-goals

- No ControlFoley imports, fields, task schemas, model loading, or media generation semantics.
- No `GenerateRequest`/`GenerateResult`, video/reference-audio/prompt/steps/guidance fields in Core.
- No torch, CUDA, FastAPI, PostgreSQL, object storage, worker, HTTP API, CLI, or ComfyUI dependency.
- No durable queue, profiler/cache implementation, cache storage or policy, benchmark, or deployment automation. `ProfileReport` and `CacheReport` are only minimal generic value contracts in this Phase.

## Deliverables

- Installable runtime-core package and public type-only contracts.
- Immutable descriptors/deployments/invocations/results, minimal generic `ProfileReport`/`CacheReport` value contracts, and multiple MIME-typed `ProducedArtifact`s.
- Adapter protocol limited to `load()`, `invoke()`, and `unload()`; generation-specific protocols, if later needed, live outside the base protocol.
- Lifecycle manager, registry, cancellation token, error taxonomy, and single-flight policy.
- Fake adapter and CPU unit-test fixtures.
- Minimal developer configuration and documentation sufficient to run checks on a clean CPU host.

## Tests

- Import the Core in an environment with no PyTorch installed.
- Fake adapter: `load → invoke → unload`, reload after unload, and failure transition behavior.
- Verify cancellation before/during invocation, invalid lifecycle calls, adapter lookup failure, and cleanup after adapter errors.
- Verify simultaneous requests to one single-flight session are serialized or explicitly rejected; never run concurrently.
- Verify one invocation may return several artifacts with distinct MIME types.
- Run the project formatter/linter, type checker, and CPU test suite.

## Gate

- All CPU checks pass and Core imports without optional model/service/UI dependencies.
- Dependency scan and source review find no ControlFoley, ComfyUI, FastAPI, SQLAlchemy, boto3, torch, `guidance`, `steps`, `video`, or `reference_audio` in Runtime Core.
- The lifecycle and single-flight behavior have direct test evidence.
- This gate has no RTX 4090 requirement. Do not start Roadmap Phase 2A or 3A until it passes.
