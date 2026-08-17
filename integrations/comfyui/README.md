# Optional embedded ComfyUI frontend

This directory is a removable custom-node frontend. It does not import
ComfyUI, and it is intentionally outside the `src/` headless packages.
Deleting or omitting it leaves the Runtime Core, local CLI, adapter, API, and
workers unchanged.

## Lifecycle ownership

The operator writes a strict configuration JSON and sets
`NANO_AURAL_COMFYUI_OPERATOR_CONFIG` to its absolute path before ComfyUI starts.
Node discovery then creates one Runtime owner lazily. Deployment paths remain
operator-owned and are never ComfyUI node inputs.

```json
{
  "schema_version": 1,
  "manifest_path": "/operator/controlfoley/deployment.json",
  "source_dir": "/operator/controlfoley/source",
  "weights_dir": "/operator/controlfoley/weights"
}
```

```sh
export NANO_AURAL_COMFYUI_OPERATOR_CONFIG=/operator/nano-aural-comfyui.json
```

The owner reuses one single-flight Runtime session. `teardown_embedded_runtime()`
waits for active invocations before unload; an unload failure retains the
handle in an unsafe state so teardown can be retried. A host-specific bootstrap
may alternatively call `configure_embedded_runtime()` with a Runtime factory
and an optional cancellation-source factory.

Discovered nodes adapt ComfyUI's already-loaded interruption callback through
a protocol-only shim; this package never imports ComfyUI. If the callback is
missing, throws unexpectedly, or blocks, execution fails closed and no audio
result is returned. Process exit also registers the explicit teardown hook.

The bridge refuses a process that has already imported `controlfoley` from a
different origin. Restart ComfyUI after choosing exactly one official/local
ControlFoley source; mixing module origins is never silently accepted.

`examples/embedded_controlfoley_t2a.json` is a standard linked workflow with a
producer and terminal `OUTPUT_NODE`. The output node returns only name, media
type, and byte count; it does not publish files or adapter/deployment metadata.

## Conditional GPU smoke

On the designated hardware host only, provide sealed local deployment paths
and run the embedded smoke:

```sh
export CONTROLFOLEY_COMFYUI_GPU_CONFIG='{"operator_config_path":"/operator/nano-aural-comfyui.json","task":"T2A","prompt":"gentle rain"}'
.venv/bin/pytest -m "gpu and controlfoley" -q tests/test_comfyui_embedded.py
```

Without that host configuration the test is intentionally skipped and the UI
GPU evidence remains **DEFERRED**.
