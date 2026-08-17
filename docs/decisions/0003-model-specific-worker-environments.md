# ADR 0003: Isolate model-specific Worker environments

**Status:** Accepted
**Date:** 2026-08-18

## Context

ControlFoley is the first adapter. The next adapters are Stable Audio 3
Small-SFX and Sony Woosh V2A (`Woosh-VFlow-8s` and `Woosh-DVFlow-8s` only).
Their upstream stacks pin incompatible Python and PyTorch lines:

| Environment | Python | Torch / Torchaudio | Official CUDA wheel line |
| --- | --- | --- | --- |
| ControlFoley | operator-locked existing checkout | operator-locked existing checkout | existing ControlFoley host |
| Stable Audio 3 | `>=3.10` | `2.7.1` / `2.7.1` | `cu126` |
| Woosh V2A | `>=3.12` | `2.8.0` / `2.8.0` | `cu128` |

The Runtime Core and durable control plane must remain free of `torch` and of
every upstream model package. A single root virtualenv or a single “universal
GPU Worker image” cannot satisfy those pins without mixing model backends.

## Decision

Each declared model family runs in its own Worker environment. Environment
identity is operator-owned and is part of Worker readiness, not a remote Job
field.

The first three environment identifiers are:

```text
controlfoley-pytorch-<locked>
stable-audio-3-pytorch-2.7.1-cu126
woosh-v2a-pytorch-2.8.0-cu128
```

The root package `nano_aural_runtime` remains dependency-free. Adapter packages
may import an upstream backend only inside an isolated child process or an
environment that already contains that backend. They must not install or import
another family's stack.

A Job claim is legal only when all of the following match the Worker
capability descriptor: `deployment.adapter_id`, sealed backend identity,
`runtime_environment_id`, and deployment fingerprint. Remote requests still
cannot carry source paths, weight paths, Python modules, solver settings, or
environment identifiers.

## Consequences

- Operators provision three Worker images or virtualenvs, not one.
- Adding a fourth adapter requires a new environment identifier and a new
  sealed Deployment; it does not widen Core contracts.
- CI continues to exercise Core, durable, and adapter CPU contracts without
  installing any model stack.
- Hardware evidence remains per-environment and may be `DEFERRED` independently.

## Rejected alternatives

A shared torch-bearing root extra was rejected because it would make Core and
every Worker image depend on one model's pin. Installing all three stacks in
one image was rejected because the Python and CUDA wheel lines conflict.
Exposing environment or solver selection on the public Job request was
rejected because those values are operator-owned Deployment fields.
