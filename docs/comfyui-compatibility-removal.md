# ComfyUI compatibility and removal

The NanoAural ComfyUI frontends are optional, removable adapters. They do not
replace the local Runtime or durable service as execution, job, or artifact
authorities. ComfyUI itself is not a dependency of any package under `src/`.

## Supported coexistence

Phase 5C checks three independently owned node families in one host process:

| Node family | Execution route | Model code in frontend |
| --- | --- | --- |
| Official ControlFoley plugin | Official plugin | Official-plugin owned |
| NanoAural Embedded | Existing local Runtime and adapter | No copied execution code |
| NanoAural Remote | Public remote client and durable service | None |

`integrations/comfyui_compat` records the official plugin's public
`NODE_CLASS_MAPPINGS` names and validates the standard A/B workflow in
`integrations/comfyui_compat/examples/official_controlfoley_t2a_ab.json`.
The contract snapshot is based on the current public `nodes.py` in
<https://github.com/YJX-Research/comfyui-controlfoley-official>. Re-run the
coexistence tests before accepting an official-plugin update; a missing or
renamed public node is an explicit compatibility failure, not a reason to
silently relax discovery.

All official, embedded, and remote node names must be disjoint. The compatibility
check refuses duplicate names before mappings can silently replace one another.
The three example workflows use the standard ComfyUI `nodes`, `widgets_values`,
six-field `links`, and terminal `OUTPUT_NODE` shape and are discoverable together.
Private custom value types are not intended to cross between node families; use
each example as a separate A/B execution route.

## ControlFoley source-origin rule

The official and NanoAural Embedded routes may coexist when every already-loaded
upstream `controlfoley` module and `lib.flow_matching` resolves beneath the exact
same ControlFoley checkout. The official plugin wrapper may remain in its own
ComfyUI `custom_nodes` directory.

Before starting ComfyUI:

1. Set the official plugin's `CONTROLFOLEY_SOURCE_DIR` to the selected checkout.
2. Set NanoAural Embedded's sealed operator JSON `source_dir` to that same
   checkout and export `NANO_AURAL_COMFYUI_OPERATOR_CONFIG` as documented in
   `integrations/comfyui/README.md`.
3. Start a fresh process, discover the three mappings, and run
   `inspect_controlfoley_coexistence()` with the official plugin modules and the
   current `sys.modules` mapping before loading either model route.

An unknown origin or a path outside the sealed checkout is rejected with the
actual and expected origins and restart instructions. Do not catch that failure
and continue loading: Python cannot safely replace already-imported modules in a
live ComfyUI process. Stop the host, correct both source settings, and restart.
The Remote frontend is unaffected because it imports no ControlFoley, Runtime,
worker, torch, CUDA, or model package.

## Removal and headless verification

Stop ComfyUI and call the frontend's documented teardown before removing a
loaded Embedded package. Then delete or omit either optional directory:

- `integrations/comfyui/`
- `integrations/comfyui_remote/`
- `integrations/comfyui_compat/` when coexistence diagnostics are not deployed

No migration or durable-state cleanup is needed. Remote jobs and artifacts stay
owned by the durable service; local Runtime state is process-local and is closed
by Embedded teardown.

The Phase 5C omission regression does not merely hide imports. It copies the
headless `src/` tree outside the repository, physically leaves out Embedded,
Remote, or both, changes to a detached working directory, removes Python path
environment variables, and runs a fresh interpreter with `-E -S`. That process
executes CPU smoke paths for Core, the local ControlFoley CLI, ApplicationService,
ApplicationApi, the fake durable worker, and the ControlFoley durable invocation
binding. It also proves that headless packages have no reverse integration or
ComfyUI imports and that Remote has no model-side imports.

Run the scoped evidence with:

```sh
.venv/bin/pytest -q tests/test_comfyui_coexistence.py tests/test_comfyui_removal.py
```

The designated 4090 ComfyUI validation remains **DEFERRED** when its hardware
and sealed operator configuration are unavailable. A skip is not a passing GPU
result and does not alter these CPU-only compatibility or removal claims.
