# Agent Instructions for nanoAuralRuntime

## Authority and reading order

Before changing code or plans, read `PROJECT_CHARTER.md`, `ARCHITECTURE.md`,
`ROADMAP.md`, and the relevant ADRs. Treat `docs/source-plans/` as archived
research, not as a specification to copy. If research conflicts with an
authoritative document, follow the authoritative document and record a focused
ADR or roadmap update when necessary.

## Phase discipline

1. Work on exactly one current Roadmap delivery Phase per PR. Lettered labels
   such as `2A`, `3D`, and `5C` are full Phases and each requires its own PR.
   The numbered files in `plans/` are programme plans; their lettered slices
   map one-to-one to the delivery Phases in `ROADMAP.md`.
2. Start by stating the Phase, its scope, non-goals, tests, and Gate.
3. Do not start a dependent later Phase while any required test or Gate is
   failing, unrun, unavailable, or ambiguous. Independent phases may proceed
   only when their own explicit Roadmap entry conditions are met. The only
   exception is validation explicitly scheduled as deferred in `ROADMAP.md`;
   it is not a passed Gate and never permits a related claim.
4. A hardware-unavailable check (including the 4090 benchmark) is recorded as
   deferred validation. Continue only with independent work the Roadmap marks
   as unblocked; do not bypass a prerequisite or report deferred validation as
   passed.
5. Respect entry conditions: Phase 3A depends only on Phase 1; Phase 3D also
   depends on Phase 2B; Phase 4A–4D are experimental/non-default while GPU
   parity is deferred; Phase 5A needs Phase 2B, Phase 5B needs Phase 3E, and
   Phase 5C needs both. Read `ROADMAP.md` for the complete dependency graph.
6. Finish each PR with changed files, commands run, test results, Gate-by-Gate
   status, known risks, and the next permitted Phase.

## Architecture boundaries

- Preserve the fixed dependency direction: Frontend -> Workflow -> Durable
  Service / Local Executor -> Runtime Core -> Model Adapter -> Original Model
  Backend.
- Keep the Runtime Core model-agnostic. Its stable lifecycle is `load()`,
  `invoke()`, and `unload()` through `AudioModelAdapter`.
- Do not put ControlFoley request fields or internals in Core contracts.
  `video_path`, `reference_audio_path`, `prompt`, `negative_prompt`,
  `num_steps`, `guidance_scale`, `mask_away_clip`, CLIP, CAV-MAE,
  Synchformer, CLAP, FlowMatching, VAE, and vocoder are adapter/task-schema
  concerns only.
- Runtime Core must not import FastAPI, database code, BlobStore clients,
  ComfyUI, or upstream model packages. Model adapters must not import frontend
  or durable-service code.
- ComfyUI remains optional. Do not make it a model execution, job-state, or
  artifact-state authority.

## Durable execution rules

- Remote requests must not contain server-local paths, source directories,
  weights paths, Python modules, or operator-only deployment settings.
- Accept only verified assets as durable job inputs. Full-file SHA-256 is the
  content identity; never substitute S3 ETag or multipart checksums.
- Preserve at-least-once attempt execution and exactly-one visible winning
  result. All heartbeat, publication, and finalization writes must use the
  attempt identity and fencing/lease epoch.
- Do not mark a job successful until its required artifacts are validated,
  durable, and atomically associated with the winning attempt.

## Change hygiene

- Read the existing code and tests before edits; make the smallest scoped
  change. Do not edit archived source plans.
- Do not commit model weights, caches, generated media, credentials, or
  private paths. Do not alter upstream algorithms or defaults unless a later
  approved Phase explicitly requires it.
- No CUDA, Triton, TensorRT, ONNX, solver changes, or speed claims in the
  initial vertical slice.
- Add or update tests for behavior changes and run the narrowest relevant suite
  before broader validation. Do not push or create a PR unless explicitly
  instructed.
