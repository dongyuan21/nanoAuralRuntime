# ADR 0002: Make ControlFoley the first adapter, not the system boundary

**Status:** Accepted
**Date:** 2026-08-16

## Context

ControlFoley provides a strong first vertical slice and the archived research
documents identify useful operational constraints: direct headless invocation,
mutable upstream model state, parity before staged optimization, and optional
ComfyUI integration. Its APIs and internals, however, are not generally shared
by all audio models.

## Decision

ControlFoley is implemented as the first `AudioModelAdapter` with its own task
schema, deployment manifest, compatibility layer, profiler stages, and cache
keys. Its upstream execution is invoked directly by the adapter; ComfyUI is
not in the runtime or worker call path.

The first implementation path is `upstream_parity`: reproduce the supported
upstream behavior under an immutable deployment and measure a self-repeat
envelope. A staged ControlFoley path, adapter-specific caches, and any
optimization follow only after parity is proven. Because upstream state is
mutable, the initial ControlFoley `ModelSession` is single-flight.

ComfyUI is permitted only as a removable frontend:

- embedded nodes translate ComfyUI values to ControlFoley Runtime contracts;
- remote nodes call the durable API and do not import model code, weights, or
  CUDA dependencies.

## Consequences

- The headless local and durable paths work when ComfyUI is absent.
- ControlFoley-specific request fields never enter Runtime Core contracts.
- The project gains a concrete parity oracle without claiming that its schema
  fits every future model.
- The 4090 baseline and performance claims remain deferred until reproducible
  hardware validation is available.

## Rejected alternatives

Using the official ComfyUI nodes as the execution engine was rejected because
it would make optional UI infrastructure a dependency and does not provide the
project's durable asset/job/attempt/artifact authority. Making the first
ControlFoley request object the public Core request was rejected by ADR 0001.
