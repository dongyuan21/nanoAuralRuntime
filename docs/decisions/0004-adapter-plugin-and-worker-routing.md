# ADR 0004: Adapter plugin discovery and Worker routing

**Status:** Accepted
**Date:** 2026-08-18

## Context

The first vertical slice wired ControlFoley directly: `nano-aural` points at
`nano_aural_runtime_controlfoley.cli:main`, and
`nano_aural_runtime_workers.controlfoley.ControlFoleyDurableInvocationBuilder`
is a ControlFoley-only binding. A second adapter must not replace that wiring
with a universal generate request, and it must not import torch or load
checkpoints merely to advertise that it exists.

Stable Audio 3 Small-SFX and Woosh V2A also have different public operations.
Woosh VFlow and DVFlow share one operation and must not become two Adapter IDs.

## Decision

Phase 7B introduces three generic, model-neutral mechanisms. None of them live
in Runtime Core, and none of them import torch or an upstream model package.

1. **Adapter plugin metadata.** Each adapter package publishes a torch-free
   descriptor with `adapter_id`, supported operations, and package location.
   Discovery must succeed in the headless CPU environment. The first IDs are:

   ```text
   controlfoley
   stable-audio-3-small-sfx
   woosh-v2a
   ```

2. **DurableInvocationBuilder registry.** Application-layer builders remain
   adapter-owned. The registry selects a builder by `adapter_id` and
   operation. Builders accept only verified assets and adapter-owned request
   fields; they reject source paths, weight paths, solver, CFG, re-noise, and
   backend overrides.

3. **Worker capability routing.** A Worker process declares one environment,
   one adapter, the operations and backends it can run, sealed fingerprints,
   device, and `max_concurrency`. Claim filtering requires an exact match
   against the Deployment seal. ControlFoley remains single-flight.

Woosh uses one Adapter ID and two sealed Deployment backends:

```text
adapter_id = woosh-v2a
backend_id = dvflow-8s   # default production backend
backend_id = vflow-8s    # selectable reference/quality backend
```

The public CLI becomes a dispatcher:

```text
nano-aural controlfoley ...
nano-aural stable-audio-3 ...
nano-aural woosh ...
```

The existing `nano-aural` ControlFoley path remains a compatibility entry.
`nano-aural-controlfoley` may be retained as an alias. No Woosh T2A, Flow, or
DFlow subcommand is added.

## Consequences

- ControlFoley keeps its task schema, builder, and local CLI behaviour.
- New adapters add a package, metadata, builder, and Worker binding; they do
  not edit Core value objects.
- Plugin discovery can be unit-tested without weights or CUDA.
- ComfyUI, when later mapped, binds to the same adapter IDs and operations.
  It remains optional and removable.

## Rejected alternatives

A Core-level `GenerateRequest` was already rejected by ADR 0001. Separate
Adapter IDs for `vflow-8s` and `dvflow-8s` were rejected because they duplicate
the V2A task schema, video materialization, and output contract. Importing
adapter implementation modules during CLI `--help` or Worker metadata listing
was rejected because those modules may later grow optional native dependencies.
