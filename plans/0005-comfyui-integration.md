# Programme Plan 0005 — Optional ComfyUI Integration (Roadmap Phases 5A–5C)

Status: blocked on the relevant local Runtime and durable-service phases. This programme plan contains independent Roadmap Phases 5A–5C, each exactly one PR. ComfyUI is a removable frontend, never a model/job/artifact authority.

## Scope

Provide thin optional integrations for two routes: Embedded nodes call the local Runtime/ControlFoley adapter; Remote nodes use upload, submit, status/wait, and verified-artifact download APIs. Package them separately from Core, API, Worker, and CLI.

## Non-goals

- No ComfyUI dependency in Runtime Core, ControlFoley adapter, API, Worker, durable schema, or remote-node model installation.
- No import of official `nodes.py` as a Runtime API, no ComfyUI execution cache as a source of truth, and no separate job/artifact state machine.
- No new model optimization, model-weight distribution, or replacement of direct headless paths.

## Deliverables

### Roadmap Phase 5A (one PR) — embedded nodes

Thin node wrappers, local input/output mapping, cancellation/error conversion, source-module origin conflict guard, model lifecycle ownership rules, and example workflows. They call the established local path without absorbing Core concerns.

### Roadmap Phase 5B (one PR) — remote nodes

Upload/submit/wait/fetch nodes using the public client/API, progress/status presentation, short-lived download authorization, and workflows. Remote package contains no model dependency.

### Roadmap Phase 5C (one PR) — coexistence and removability hardening

Official-plugin A/B workflow checks, module-origin diagnostics, compatibility documentation, and a CI test proving that removing `integrations/comfyui/` leaves Core, CLI, API, and Worker checks passing.

## Tests

- Unit tests for node parameter mapping, invalid media/error behavior, cancellation, and absence of UI types below the integration boundary.
- Integration tests for embedded local invocation (conditional GPU) and remote upload/job/fetch using the Fake Runtime path.
- Packaging/import tests prove Remote does not import torch/model code and Core/Service do not import ComfyUI.
- Conditional 4090 smoke tests skip when absent; do not report pass from an unsupported environment.

## Gate

- Embedded and Remote paths use existing Runtime and durable contracts without becoming state authorities.
- The official plugin can coexist or its conflict is detected with actionable refusal; no silent module mixing.
- Delete/omit the integration package and run Core/CLI/API/Worker regression checks successfully.
- 4090 UI smoke evidence is **DEFERRED** until the designated host is available; it cannot be substituted with a claim of success.
