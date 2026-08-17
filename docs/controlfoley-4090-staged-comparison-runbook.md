# ControlFoley Phase 4A staged comparison runbook (RTX 4090)

Status: **DEFERRED** until a suitable RTX 4090 host, the sealed Phase 2A
deployment, verified inputs, and an independently implemented staged backend
are available. A skipped test is not a passed Gate.

This runbook records raw evidence only. It defines no tolerance, parity verdict,
speed target, or performance claim. The pinned original `demo.py` remains the
`upstream_parity` oracle, and the experimental staged path remains explicit
opt-in and non-default regardless of the measurements produced here.

## Operator backend contract

The operator supplies an importable Python module that exposes:

```python
def create_controlfoley_staged_backend() -> ControlFoleyStagedBackend: ...
```

The returned implementation must execute the adapter-owned stages directly in
this exact order:

1. `media_resolve_preprocess`
2. `condition_encode_projection`
3. `integrate`
4. `decode_vocoder_postprocess`

It must not invoke the original `demo.py` as a staged implementation. Doing so
would only wrap the oracle and is not staged evidence. The repository does not
bundle or pretend to provide a GPU implementation in Phase 4A.

Its `backend_id` must be a 1–64 character ASCII identifier beginning with a
letter or digit and containing only letters, digits, dots, underscores, or
hyphens. Paths, whitespace, control characters, and token-like `key=value`
strings are rejected before they can reach deployment or result metadata.

## Sealed configuration

Create an operator-local JSON file outside the repository. All media and output
paths must be absolute. `task` and its inputs must match the selected Phase 2A
fixture exactly; the test checks their full SHA-256 values before execution.

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
  "comparison_output": "<absolute-new-path-for-comparison.json>"
}
```

For `V2A`, omit `prompt`. For `AC-V2A`, add `audio` and omit `prompt`. For
`T2A`, omit `video`. `TV2A` and `TC-V2A` both use `video` plus `prompt`. The
test uses fixture values for duration, steps, guidance, and seed; it does not
alter the upstream defaults or solver.

## Exact command

From the repository root, with the operator backend already importable in the
active environment:

```bash
export CONTROLFOLEY_P4A_GPU_CONFIG="$($PWD/.venv/bin/python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))))' /absolute/path/to/p4a-config.json)"
.venv/bin/pytest -q -m 'gpu and controlfoley' tests/test_controlfoley_staged.py::test_controlfoley_staged_gpu_comparison_when_operator_configured
```

The test runs the locked Phase 2A direct oracle twice, verifies its completed
result bindings, runs the explicitly selected staged deployment once, validates
both audio files through the existing waveform comparison reader, and creates
`comparison_output` with exclusive-create semantics. It refuses to overwrite an
existing evidence file.

The output binds all raw observations to:

- the canonical deployment manifest SHA-256;
- the candidate staged deployment fingerprint and backend identity;
- the locked source revision and checkpoint SHA-256;
- a path-free canonical invocation SHA-256 covering task, full input hashes,
  prompt hash, duration, steps, guidance, and seed;
- the full oracle and candidate output SHA-256 values.

The runner captures the oracle deployment manifest before execution, then
re-reads and compares its canonical manifest SHA-256, source revision, and
checkpoint SHA-256 after the direct run, after candidate load, and immediately
before writing evidence. Manifest drift aborts the comparison rather than
mixing two deployments in one record.

The metrics are raw peak, RMS, MAE, maximum absolute error, waveform cosine
similarity, and mel-spectrogram distance, plus waveform shape and sample rate.
There are intentionally no `threshold`, `claim`, `passed`, `parity`, or
performance-result fields. Acceptance criteria and any default change require a
later review backed by real hardware evidence; until then the Phase 6 release
Gate remains blocked.
