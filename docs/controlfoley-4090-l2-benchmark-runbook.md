# ControlFoley Phase 4D RTX 4090 L2 benchmark runbook

Status: **DEFERRED** until a declared RTX 4090 host, the sealed Phase 2A
deployment/fixture, and an operator staged backend implementing the Phase 4D
condition-feature split are available. This runbook does not contain or imply
a parity, quality, acceleration, or release claim.

## Experimental boundary

`upstream_parity` remains the default/oracle. Phase 4D is a distinct,
explicitly selected staged deployment. It may persist only per-role encoder
features for `condition_encode_projection`. Projection itself runs on every
invocation. Flow Matching/integration, latents, solver steps, decode, vocoder,
postprocess, and final artifacts are never cached.

The disk store accepts only strictly validated, data-only safetensors bundles.
It does not import pickle, `torch.load`, or a decoder selected by cache bytes.
The operator cache root must be an existing canonical absolute directory with
mode `0700`, outside the locked source and weights trees. Cache reports and
evidence contain no local path.

## Operator configuration

Create an empty private root before the run:

```bash
mkdir -m 700 /absolute/operator/l2-cache
```

Set `CONTROLFOLEY_P4D_GPU_CONFIG` to one JSON object. The task-specific keys
are the same as the Phase 4C fixture contract: `video`; `video,prompt`;
`video,audio`; or `prompt`.

```json
{
  "deployment": "/absolute/operator/deployment.lock.json",
  "fixture": "/absolute/operator/fixture.json",
  "source_dir": "/absolute/operator/ControlFoley",
  "weights_dir": "/absolute/operator/controlfoley-weights",
  "backend_module": "operator.controlfoley_backend",
  "task": "V2A",
  "preprocess_version": "operator-media-v1",
  "code_version": "nano-aural-p4d-v1",
  "evidence_output": "/absolute/operator/evidence/p4d-l2-matrix.json",
  "video": "/absolute/operator/fixtures/input.mp4",
  "condition_cache_root": "/absolute/operator/l2-cache",
  "condition_codec_version": "operator-condition-codec-v1",
  "condition_schema_version": "operator-condition-schema-v1",
  "expected_device_name": "NVIDIA GeForce RTX 4090",
  "repeats": 3
}
```

The backend module must expose `create_controlfoley_staged_backend()` and the
returned backend must implement the explicit condition-feature encode,
safe-export/import, and projection protocol. Its codec/schema identities must
exactly equal the deployment configuration.

## Exact command

From the repository root:

```bash
.venv/bin/pytest -q \
  tests/test_controlfoley_condition_cache.py::test_controlfoley_l2_benchmark_matrix_when_operator_configured
```

An absent configuration or a verified, pure CUDA-capability absence is an
expected skip. Once configuration is supplied, a source revision/origin/dirty
tree problem, weight/checkpoint/fixture/input fingerprint drift, unsafe cache
root, backend/codec/schema mismatch, or evidence-path conflict is a failure,
not a skip.

## Matrix and evidence

The runner executes the sealed cells in this order for every repeat, with at
most one model session resident. It unloads the uncached deployment and runs
CUDA synchronize/cache cleanup/peak reset before loading the L2 deployment:

1. `off`: experimental staged path with no cache.
2. `l2_cold`: explicit L0/L1/L2 deployment after exact deployment cache
   invalidation.
3. `l2_warm`: the same sealed L2 deployment without invalidation.

Evidence contains raw per-run wall timing, available CUDA event timing and
VRAM observations, cache hit/miss/bytes, encoder/projection call counts,
output SHA-256, and the path-free deployment/fixture/invocation provenance.
The plan explicitly seals the condition codec/schema versions plus the
semantic L0/L1 and L2 policy fingerprints; operational roots and capacity
limits remain operator-local and are not emitted.
Every configured sample must contain an available CUDA profile with timing and
allocated/reserved/peak memory fields, and the device must exactly identify
the declared `NVIDIA GeForce RTX 4090`. Evidence is published using a private
fsynced temporary file and exclusive atomic link; a partial final file is never
accepted.
It defines no tolerance, speedup, threshold, verdict, or performance claim.
The test requires exact output hashes across the available matrix as a safety
check; this CPU/GPU plumbing result is not an accepted upstream parity result.
