# Programme Plan 0006 — Stable Audio 3 Small-SFX and Woosh V2A

Status: ready after Phase 1 and the completed ControlFoley non-hardware Gates.
This programme plan contains independently gated Roadmap Phases 7A–11; each
Roadmap Phase is exactly one PR. ControlFoley remains the first adapter. Stable
Audio 3 Small-SFX and Sony Woosh V2A are additional adapters. They are not Core
abstractions and they do not reopen Phase 6.

`docs/source-plans/` remains archived research and must not be edited.

## Product split

```text
Text-to-SFX:
  Stable Audio 3 Small-SFX

Video-to-SFX:
  ControlFoley
  Woosh-DVFlow-8s   (default Woosh production backend)
  Woosh-VFlow-8s    (selectable reference/quality backend)
```

## Scope

Add two new model families behind the existing Runtime Core:

- Stable Audio 3 V1: operation `audio.text_to_sfx` only, official PyTorch
  Small-SFX path, 44.1 kHz stereo WAV, variable length up to 120 seconds.
- Woosh V1: one adapter `woosh-v2a`, one operation `audio.video_to_sfx`, two
  sealed Deployment backends `dvflow-8s` and `vflow-8s`. Fixed 8-second window,
  48 kHz mono WAV. Shared Woosh-AE, TextConditionerV, and Synchformer video
  conditioning.

Phase 7A is documentation only. Phase 7B adds plugin metadata, builder
registry, Worker capability routing, and a generic CLI dispatcher without any
new model backend. Later lettered slices add provenance, local adapters,
durable Workers, optional profiler/cache, optional workflows, and hardware
evidence.

## Non-goals

- No change to Runtime Core `load()` / `invoke()` / `unload()`.
- No Core fields named `prompt`, `video_path`, `duration_seconds`, `num_steps`,
  `cfg`, `solver`, `renoise`, Synchformer, SAME, T5Gemma, VFlow, or DVFlow.
- No torch, Hugging Face, or upstream model dependency on the root package.
- No single virtualenv or universal GPU Worker image for all three families.
- No Woosh-Flow, Woosh-DFlow, Woosh text-to-audio, TextConditionerA, 5-second
  Woosh T2A, or a unified Woosh T2A/V2A adapter.
- No Stable Audio audio-to-audio, inpainting, continuation, LoRA, TensorRT,
  TFLite, MLX, or `batch_size > 1` in V1.
- No implicit multi-segment Woosh video handling, silent padding, last-frame
  freeze, or in-adapter mux.
- No weights, caches, generated media, Hugging Face tokens, or private paths
  committed to the repository.
- No parity, quality, or performance claim from skipped or deferred GPU tests.
- No combining lettered slices into one PR, and no treating Phase 6 hardware
  deferral as a passed Gate.

## Deliverables

### Roadmap Phase 7A (one PR) — multi-adapter architecture freeze

Documentation only: this programme plan, ADR 0003, ADR 0004, Roadmap and Status
updates, and the source/dependency/license research note. No production Python.

### Roadmap Phase 7B (one PR) — plugin, builder registry, Worker routing

Torch-free adapter plugin metadata, `DurableInvocationBuilder` registry,
Worker capability descriptors, deployment-aware claim filtering, generic
`nano-aural` dispatcher, and ControlFoley compatibility. No new model adapter.

### Roadmap Phase 8A (one PR) — Stable Audio 3 baseline/provenance

Official direct runner lock, source/HF revision lock, gated-model prepare
runbook, weight manifest schema, and 5s / 30s / 120s baseline configs.
Conditional GPU tests skip when prerequisites are absent.

### Roadmap Phase 8B (one PR) — Stable Audio 3 local adapter

`nano_aural_runtime_stable_audio_3`, `audio.text_to_sfx` task schema,
in-process session, local CLI, WAV serialization, 44.1 kHz stereo validation,
and execute-time source/weight revalidation.

### Roadmap Phase 8C (one PR) — Stable Audio 3 durable Worker

Invocation builder, isolated Worker environment, capability routing, durable
execution, artifact publication, remote CLI, and recovery tests.

### Roadmap Phase 8D (one PR, optional) — Stable Audio 3 editing

`audio_to_audio`, inpaint, and continuation. Does not block Woosh V2A.

### Roadmap Phase 8E (one PR, optional) — Stable Audio 3 profiler/cache

Experimental, default-off observation and text-conditioning cache. Does not
block Woosh V2A.

### Roadmap Phase 9A (one PR) — Woosh VFlow/DVFlow baseline/provenance

Pin SonyResearch/Woosh, release/tag provenance, Woosh-AE, TextConditionerV,
Woosh-VFlow-8s, Woosh-DVFlow-8s, and the MMAudio Synchformer checkpoint.
Official direct-run harness, 8-second video fixture contract, optional prompt,
fixed-seed comparison. Do not inspect or adapt Woosh-Flow, Woosh-DFlow, or T2A.

### Roadmap Phase 9B (one PR) — Woosh V2A local adapter

One package, one task, two backends. Local CLI, 8-second window from 0,
reject videos shorter than 8 seconds, DVFlow default, VFlow selectable,
48 kHz mono WAV, no mux inside the adapter.

### Roadmap Phase 9C (one PR) — Woosh durable Worker

`WooshV2ADurableInvocationBuilder`, isolated environment, verified video
materialization, Runtime invoke, publication, fencing/recovery, remote CLI.

### Roadmap Phase 9D (one PR) — Woosh profiler/cache

Experimental, default-off. Cache only video preprocessing, Synchformer
features, text tokens, and empty/unconditional conditions. No ODE trajectory
cache, latent reuse, step skipping, or cross-seed reuse.

### Roadmap Phase 10A (one PR) — unified workflow and optional ComfyUI mapping

Workflows `sfx.text_generate`, `sfx.video_generate`, and explicit
`sfx.generate_and_mux`. Mux is not a Woosh adapter step. ComfyUI remains
optional and removable.

### Roadmap Phase 10B (one PR) — RTX 4090 evidence

Deferred until the designated host is available. Not a software-slice blocker.

### Roadmap Phase 11 (one PR) — adapter-family release hardening

License notices, gated HF token handling, Synchformer/MMAudio attribution, and
release evidence. The ControlFoley Phase 6 hardware Gate remains independent
and still release-blocking for a combined product release.

## Tests

- Phase 7A: terminology review. Core remains model-neutral. Woosh T2A terms
  appear only in explicit out-of-scope lists. One-Phase/one-PR mapping is
  complete.
- Phase 7B: plugin discovery without torch; ControlFoley CLI `--help` and
  local path remain; builder registry rejects sealed-field leaks; Worker
  capability mismatches fail closed; Core import-boundary scan stays green.
- Later phases: CPU schema/provenance tests always run; GPU/source/weight
  tests skip when declared prerequisites are absent; skipped tests are never
  recorded as passed.

## Gate

- Phase 7A: documents agree on the product split, isolated Worker
  environments, Woosh V2A-only scope, DVFlow default, VFlow selectable,
  8-second fail-closed window, and the one-Phase/one-PR rule. No production
  Python lands in this PR.
- Later non-hardware Gates follow the Roadmap. RTX 4090 evidence is
  `DEFERRED`, never passed, and does not block independent software slices
  the Roadmap marks as unblocked.
- Absence of Woosh T2A/Flow/DFlow code is an invariant of every Gate in this
  programme.
