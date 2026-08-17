# ADR 0001: Adopt a model-agnostic Runtime Core

**Status:** Accepted
**Date:** 2026-08-16

## Context

The archived research plan contains a concrete ControlFoley execution shape:
video and reference-audio inputs, prompt and guidance parameters, staged
encoders, Flow Matching, VAE, and vocoder. It is valuable evidence for the
first adapter, but using these details as Runtime Core contracts would couple
all later audio models to ControlFoley.

## Decision

The Core is defined around generic lifecycle and execution concepts:
`AudioModelAdapter`, `ModelDescriptor`, `ModelDeployment`, `ModelSession`,
`ModelInvocation`, `InvocationResult`, `ProducedArtifact`,
`ExecutionContext`, `CancellationToken`, `ProfileReport`, and `CacheReport`.

`AudioModelAdapter` has only three required lifecycle operations: `load()`,
`invoke()`, and `unload()`. Capability declarations and adapter-owned task
schemas describe optional inputs, outputs, and generation semantics. The Core
will not standardize a universal generate request and will not name a
particular conditioning encoder, sampling algorithm, decoder, or output
format.

The dependency order remains:

```text
Frontend -> Workflow -> Durable Service / Local Executor -> Runtime Core
         -> Model Adapter -> Original Model Backend
```

## Consequences

- A future adapter can support a different audio task without expanding Core
  request types.
- Frontends and durable workers share Core contracts but never own model
  semantics.
- Adapter authors must supply validation, capability metadata, and
  adapter-specific observability/cache semantics.
- Generic Core APIs are deliberately less convenient than a model-specific
  request object; convenience belongs in adapter SDKs and workflows.

## Rejected alternative

A common `GenerateRequest` shaped like ControlFoley was rejected because it
would hard-code video, reference audio, prompts, steps, guidance, and model
internals into every model integration.
