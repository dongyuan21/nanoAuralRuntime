# Optional remote ComfyUI frontend

This directory is a removable, remote-only custom-node package. It imports the
public `nano_aural_runtime_remote` client and standard-library modules only. It
does not import ComfyUI, Runtime Core, ControlFoley, torch, CUDA, workers, or
durable internals.

## Operator configuration

Write a strict JSON file outside workflows and set
`NANO_AURAL_COMFYUI_REMOTE_CONFIG` to its absolute path. The bearer token is
read from the separately named environment variable; neither the token, base
URL, config path, nor download directory becomes a node value or UI summary.

```json
{
  "schema_version": 1,
  "base_url": "https://nano-aural.example",
  "token_env": "NANO_AURAL_API_TOKEN",
  "allow_loopback_http": false,
  "download_dir": "/operator/nano-aural-downloads",
  "transport_timeout_seconds": 30,
  "max_upload_bytes": 1073741824,
  "max_download_bytes": 1073741824,
  "max_wait_seconds": 3600,
  "max_poll_iterations": 3600
}
```

`UrllibTransport` remains the endpoint authority: HTTPS is required, except
for explicitly enabled loopback HTTP development. `RemoteClient` remains the
authority for verified upload, request validation, authorization, download
SHA-256/size verification, atomic publication, and no-overwrite behavior.

## Node flow

`RemoteUpload` uploads a bounded local file and returns only an immutable
`(role, asset_id)` binding after the service reports a verified asset.
`RemoteAssetBundle` combines up to eight bindings with unique allow-listed
roles, so zero-input, video-only, and video-plus-reference-audio requests use
the same submit node. Subsequent nodes exchange only asset, job, and artifact
IDs plus allow-listed status/integrity fields—never a client path, server path,
storage key, endpoint, or credential. Durable asset, job, and artifact IDs are
accepted from the service only in canonical UUID form; event cursors remain
their separately validated canonical decimal protocol values.

The wait node calls the public client's bounded `wait()` slices, presents
allow-listed event types/counts, and converts a host interruption into the
public durable `cancel()` call. It does not define another job state machine.
The final `OUTPUT_NODE` displays only job/artifact IDs, a local basename, and
verified byte count.

See `examples/remote_controlfoley_v2a.json` for the standard linked workflow.

## Conditional remote GPU smoke

On the designated remote-GPU validation setup, set
`CONTROLFOLEY_COMFYUI_REMOTE_GPU_CONFIG` to a JSON object containing
`operator_config_path`, `namespace_id`, `idempotency_key`, `deployment_id`,
`request_json`, `source`, `role`, and `output_name`, then run:

```sh
.venv/bin/pytest -m "gpu and controlfoley" -q tests/test_comfyui_remote.py
```

Without those prerequisites the UI smoke is skipped and remains **DEFERRED**.
