# ControlFoley Phase 4B profiler runbook (RTX 4090)

Status: **DEFERRED** until the designated RTX 4090 host has a verified Phase
2A deployment, sealed fixture inputs, and a real in-process staged backend. A
skip is not a passed hardware Gate.

Phase 4B records observations only. It defines no target, regression threshold,
parity verdict, acceleration claim, or performance claim. Profiling is disabled
by default, `upstream_parity` remains the default/oracle, and
`experimental_staged_v1` remains explicit opt-in.

## Why the staged backend is required

The original upstream oracle runs in an isolated child process, so parent
process CUDA events cannot truthfully measure its kernels. The conditional GPU
test therefore profiles only an operator-supplied, in-process staged backend
that implements the Phase 4A `ControlFoleyStagedBackend` protocol directly. It
must not wrap or invoke the original `demo.py`.

The profiler injects `TorchCudaProfileBackend` only after `torch.cuda` reports
an actual CUDA capability. It records CUDA Events plus allocated, reserved,
peak-allocated, and peak-reserved bytes. If torch or CUDA is absent, the test
skips; CPU reports never synthesize these fields.

## Sealed operator configuration

Create a JSON file outside the repository. The object must have exactly the
task-specific keys shown below: extra keys are rejected. All paths are
operator-local absolute paths, all input files/directories must already exist,
the output must not exist, and its parent directory must already exist. The
fixture task, media hashes, prompt hash, duration, steps, guidance, and seed are
checked before invocation.

```json
{
  "deployment": "<absolute-path-to-verified-deployment.lock.json>",
  "fixture": "<absolute-path-to-verified-fixture.json>",
  "source_dir": "<absolute-path-to-source-at-6858cd12a48d141201e3266e7abe1f38357a133e>",
  "weights_dir": "<absolute-path-to-verified-model-weights>",
  "backend_module": "operator_controlfoley_staged_backend",
  "task": "TV2A",
  "video": "<absolute-path-to-verified-video>",
  "prompt": "the exact fixture prompt",
  "profile_output": "<absolute-new-path-for-profile.json>"
}
```

For `V2A`, omit `prompt`. For `AC-V2A`, add `audio` and omit `prompt`. For
`T2A`, omit `video`. `TV2A` and `TC-V2A` both use `video` plus `prompt`.

The backend module must expose:

```python
def create_controlfoley_staged_backend() -> ControlFoleyStagedBackend: ...
```

## Exact command

From the repository root, with the operator backend importable:

```bash
export CONTROLFOLEY_P4B_GPU_CONFIG="$($PWD/.venv/bin/python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))))' /absolute/path/to/p4b-config.json)"
.venv/bin/pytest -q -m 'gpu and controlfoley' tests/test_controlfoley_profiler.py::test_controlfoley_staged_cuda_profile_when_operator_configured
```

Before candidate load and again after invocation, the harness re-reads the
deployment and fixture manifests, verifies the locked source revision/origin/
clean state, recalculates every checkpoint and external-weight full SHA-256,
and recalculates the canonical task/input/parameter fingerprint. Any missing or
invalid configured seal (including source origin/revision/dirty state or any
checkpoint/external-weight SHA mismatch) fails the run and no evidence is
written, even if CUDA is also unavailable. Only actual torch/CUDA capability
absence may produce a skip after all configured seals pass. The candidate
session must match the initially captured manifest SHA, source revision,
checkpoint SHA, and safe backend identity.

The output file is created with exclusive-create semantics and contains a
path-free evidence envelope. It binds the fixture manifest SHA, output SHA,
candidate deployment fingerprint, deployment manifest SHA, source revision,
checkpoint SHA, safe staged backend id, and canonical invocation SHA to the
namespaced raw `ProfileReport`. It contains no operator paths or tokens. Stage
order is exactly:

1. `media_resolve_preprocess`
2. `condition_encode_projection`
3. `integrate`
4. `decode_vocoder_postprocess`

Each stage has a real monotonic CPU duration and, only on actual CUDA, a CUDA
Event duration and memory observations. Missing capability is represented by
`status: unavailable`, `backend_id: null`, absent CUDA metrics, and `cuda: null`
per stage. Do not compare these observations to a target or publish a speed
claim. The RTX 4090 profile validation and Phase 6 release Gate remain blocked
until reviewed evidence is recorded.
