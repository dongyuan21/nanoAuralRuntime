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
   Phase 5C needs both. Phase 7A depends on Phase 1 and does not wait for
   Phase 6 hardware; Phase 7B needs 7A; Phase 8A needs 7A; Phase 8B needs 7B
   and 8A; Phase 8C needs 8B and 3E; Phases 8D and 8E are optional and do not
   gate Woosh; Phase 9A needs 7A; Phase 9B needs 7B and 9A; Phase 9C needs 9B
   and 3E; Phase 9D needs 9B; Phase 10A needs 8B, 9B, and 5C. Read
   `ROADMAP.md` for the complete dependency graph. Do not add Woosh T2A,
   Woosh-Flow, or Woosh-DFlow paths.
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

## Cursor Cloud specific instructions

- Development happens on `codex/multi-adapter-stable-woosh-v2a`, not `main`.
  `main` is docs-only planning bootstrap. The implemented Runtime Core, CLI,
  durable service, adapters, tests, and `compose.yaml` live on that Codex
  integration branch. Open PRs against it unless the user says otherwise.
- CPU install matches CI: `python -m pip install 'setuptools==82.0.1'` then
  `python -m pip install --no-build-isolation -e '.[dev]'`. Canonical lint,
  type-check, and CPU test commands are in `.github/workflows/ci.yml` and
  `README.md` (`ruff format --check`, `ruff check`, `pyright`,
  `pytest -m 'not gpu'`).
- This Cloud Agent VM has no NVIDIA GPU and no operator-supplied ControlFoley,
  Stable Audio 3, or Woosh V2A weights. GPU-marked tests skip; do not treat
  skips as passed Gates. Keep the root package torch-free.
- PostgreSQL 16 suites need `NANO_AURAL_POSTGRES_BIN` pointing at PG16 binaries
  (`initdb`/`postgres`/`pg_ctl`); without it those tests skip. The Compose
  reference stack in `docs/durable-operations.md` needs five owner-only secret
  files *outside* the repo — never write secrets into `.env` or `compose.yaml`.
- Optional ComfyUI trees under `integrations/` are not in the wheel; removing
  them must not break headless tests.
