# nanoAuralRuntime Architecture

## Status and scope

This is the authoritative architecture for the bootstrap programme. The two
files in `docs/source-plans/` are archived research inputs: they supply useful
constraints and experiments, but do not define public contracts.

## Fixed system layers

```text
Frontend
  -> Workflow
  -> Durable Service / Local Executor
  -> Runtime Core
  -> Model Adapter
  -> Original Model Backend
```

Frontend means CLI, HTTP API, ComfyUI, or a future client. Workflow composes
declared invocations; it owns no model implementation. The Local Executor and
Durable Service are alternative execution environments. A durable worker calls
the same Runtime Core used by a local executor.

Dependencies point only downward. In particular, Runtime Core must not import
or require FastAPI, database drivers, object-storage clients, ComfyUI, or an
upstream model package. A model adapter must not depend on a frontend, durable
state store, or ComfyUI.

```text
CLI / HTTP API / ComfyUI
          |
       Workflow
       /       \
Local Executor  Durable Service -> GPU Worker
       \       /                    |
        Runtime Core <---------------+
             |
        Model Adapter
             |
   Original Model Backend
```

## Runtime Core contract

The Core owns generic lifecycle, capability negotiation, execution context,
cancellation, profiling, cache accounting, and artifact reporting. It exposes
the following vocabulary:

| Concept | Responsibility |
| --- | --- |
| `ModelDescriptor` | Stable adapter identity, version, and declared capabilities. |
| `ModelDeployment` | Immutable, operator-owned selection of adapter, source, weights, device policy, and fingerprints. |
| `AudioModelAdapter` | Model boundary that implements `load()`, `invoke()`, and `unload()`. |
| `ModelSession` | A loaded deployment with explicit lifecycle and concurrency limits. |
| `ModelInvocation` | An opaque, schema-validated invocation envelope selected by a task type. |
| `InvocationResult` | Adapter result metadata, warnings, profile, cache report, and produced artifacts. |
| `ProducedArtifact` | A named output with media/content metadata and integrity information. |
| `ExecutionContext` | Request identity, time/deadline policy, tracing, cancellation, and operator-approved resources. |
| `CancellationToken` | Thread-safe cooperative cancellation signal. |
| `ProfileReport` / `CacheReport` | Observation data; neither changes invocation semantics. |

At minimum, an adapter behaves conceptually as:

```text
session = adapter.load(deployment)
result  = adapter.invoke(session, invocation, context)
adapter.unload(session)
```

The Core does not define a universal generation request. It does not contain
`video_path`, `reference_audio_path`, `prompt`, `negative_prompt`, `num_steps`,
`guidance_scale`, `mask_away_clip`, or any ControlFoley implementation term
such as CLIP, CAV-MAE, Synchformer, CLAP, FlowMatching, VAE, or vocoder. Those
belong to the ControlFoley adapter's task schema, execution plan, profiler, and
cache keys. Generative-model extensions may be defined as optional protocols,
never as requirements of `AudioModelAdapter`.

`ModelSession` has an explicit lifecycle: `UNLOADED -> LOADING -> READY ->
RUNNING`, returning to `READY` after a successful or recoverable invocation;
faulted sessions must reject new work and unload. Each deployment declares its
concurrency model. The ControlFoley deployment starts single-flight because its
upstream model has mutable instance state.

## Execution environments

### Local executor

The local executor accepts local operator-controlled inputs, materializes an
adapter-specific invocation, calls the Core, validates output, and writes
artifacts. It does not create durable service state.

### Durable service

The durable service owns authenticated upload, verified assets, job state,
attempts, leases, cancellation requests, artifact authorization, and recovery.
It does not own model semantics. A GPU worker owns one declared deployment,
claims an attempt, materializes verified inputs, calls the Core, validates
outputs, and finalizes through compare-and-set fencing.

The authoritative boundaries are:

| Fact | Authority |
| --- | --- |
| Asset usability and job/attempt/artifact state | Durable database |
| Asset and artifact content identity | Full-file SHA-256 |
| Object bytes | Blob store key, after database verification |
| Legal attempt owner | Current attempt plus lease/fencing epoch |
| Model semantics | Adapter and its declared deployment |
| UI state | Never business authority |

The required invariant is at-least-once execution with exactly one visible
winning artifact set. A stale worker that loses its lease must be unable to
heartbeat, finalize, or expose artifacts. Only verified assets may be submitted
as inputs, and `SUCCEEDED` implies every required artifact is verified, ready,
and attached to the winning attempt.

Model families do not share a Worker environment. ControlFoley, Stable Audio 3
Small-SFX, and Woosh V2A each declare an isolated runtime environment, adapter
id, and sealed Deployment fingerprint. Worker capability routing is an
application concern; see ADR 0003 and ADR 0004. The Core still does not import
torch, Hugging Face clients, or any upstream model package.

## Observability and caching

Profiling is generic: it reports wall/GPU timing, resource usage, and
adapter-supplied stages without naming a model's internals. Caching is likewise
generic and reports keys, byte use, hit/miss status, and invalidation reasons.
An adapter defines any semantic cache keys and must include every factor that
can change its result. Cache contents are an optimization, never job authority.

## Frontend independence

ComfyUI is an optional integration in two forms: embedded nodes that translate
ComfyUI values to Runtime contracts, and remote nodes that call the API. It
must not duplicate model execution code. Remote nodes require neither model
weights nor CUDA. The no-ComfyUI regression suite is mandatory: removal of
`integrations/comfyui` must leave Core, CLI, API, and worker tests passing.

## Security and reproducibility boundaries

Remote clients never submit server-local paths, Python modules, source
directories, model weights, precision flags, or arbitrary execution settings.
Operators select immutable deployments; workers verify source and weight
fingerprints before declaring readiness. Upload filenames are metadata only and
never form server filesystem paths. Artifact publication uses immutable
attempt-specific keys followed by validation and fenced finalization.
