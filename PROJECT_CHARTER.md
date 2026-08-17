# nanoAuralRuntime Project Charter

## Mission

nanoAuralRuntime is an audio-native, model-agnostic, adapter-first runtime,
workflow SDK, and durable serving stack for audio foundation models. It makes
model execution reproducible, observable, cancellable, and safely publishable
without making any one model or UI framework the system boundary.

ControlFoley is the first model adapter and the first complete vertical
workflow. It is evidence that the architecture works; it is not the definition
of the product.

## Outcomes

- A local execution path can load a declared model deployment, invoke it, and
  produce verified artifacts without a web service or ComfyUI.
- A durable execution path can accept verified assets, manage jobs and attempts,
  fence stale workers, and expose only a single verified winning result.
- Model-specific semantics live behind adapters and task schemas, allowing a
  second audio model to be added without widening the Runtime Core contract.
- CLI, HTTP API, and ComfyUI are replaceable frontends. Removing ComfyUI must
  not affect the Runtime Core, CLI, API, worker, or their tests.

## Architectural commitments

1. The Runtime Core is model-agnostic. Its stable concepts are
   `AudioModelAdapter`, `ModelDescriptor`, `ModelDeployment`, `ModelSession`,
   `ModelInvocation`, `InvocationResult`, `ProducedArtifact`,
   `ExecutionContext`, `CancellationToken`, `ProfileReport`, and `CacheReport`.
2. The only adapter lifecycle operations required by the Core are `load()`,
   `invoke()`, and `unload()`. Model capabilities and model-owned task schemas
   describe optional semantics.
3. Runtime execution is separated from durable control-plane state. The Core
   never depends on HTTP, PostgreSQL, object storage, authentication, or
   ComfyUI.
4. Durable execution is at-least-once at the attempt level and exactly-one at
   the visible-result level. A successful model call alone never means that a
   job succeeded.
5. All user-supplied media becomes a verified, content-addressed asset before
   it can become a durable job input. Application full-file SHA-256 is the
   content identity; object-store ETags are not.
6. Each Roadmap delivery Phase is delivered in exactly one PR and may proceed
   only after its stated Gate passes. Lettered phases (for example `2A` and
   `3D`) are full delivery Phases, not sub-tasks within one PR.

## Non-goals for the initial programme

- Training, fine-tuning, redistribution of model weights, or claiming model
  license rights.
- CUDA/Triton/TensorRT/ONNX optimization, changed sampling equations, or
  unmeasured performance claims.
- Multi-GPU tensor parallelism and cross-request continuous batching.
- Making ComfyUI, an upstream model repository, or a storage provider the
  authoritative source of job state.

## Authority of planning material

`docs/source-plans/controlfoley-runtime-research.md` and
`docs/source-plans/headless-durable-serving-research.md` are archived research
inputs. They are intentionally preserved, are not implementation authority,
and must not be edited to track the project. This charter, `ARCHITECTURE.md`,
the ADRs, and the gated `ROADMAP.md` are the authoritative bootstrap set.

`plans/0001-runtime-core.md` through `plans/0005-comfyui-integration.md` are
programme-plan files: they provide detailed work breakdowns. Their lettered
PR slices map one-to-one to the delivery Phases in `ROADMAP.md`; their numbered
file headings do not authorize combining the lettered slices into a single PR.
`plans/STATUS.md` is an execution index, not a plan or a Gate authority.

## Success criteria

The programme succeeds when a declared adapter can be invoked locally and via
the durable path; every exposed result is verified and belongs to one winning
attempt; model-specific options remain outside the Core; and deleting the
optional ComfyUI integration leaves the headless path intact.
