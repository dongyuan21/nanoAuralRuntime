# ControlFoley Phase 4C L0/L1 cache runbook (RTX 4090)

Status: **DEFERRED** until the designated RTX 4090 host has the verified Phase
2A deployment, sealed fixture inputs, and a real staged backend that implements
the Phase 4C safe-bytes preprocessing codec. A skip is not a passed hardware
Gate.

Phase 4C is experimental and disabled by default. `upstream_parity` remains the
default/oracle; this command compares cache-off, cold-cache, and warm-cache runs
of the same explicitly selected staged backend. It does not measure parity
against the upstream oracle, define a threshold, or support a performance
claim.

## Cached values and safety boundary

- L0 stores only path-free canonical metadata JSON: full input SHA-256/size,
  task and all invocation parameters, source/deployment/manifest/checkpoint
  identity, precision, backend id, preprocessing version, and cache code
  version.
- L1 stores only immutable bytes exported by the staged backend for
  `media_resolve_preprocess`. The backend must implement
  `ControlFoleyPreprocessCacheCodec`; a cached deployment fails to load without
  it.
- Condition encoders, Flow Matching/integration, latents, solver state, decode,
  vocoder, and final artifacts are never cached in Phase 4C.
- Storage is a process-local, thread-safe byte/item-bounded memory LRU. Phase
  4C writes no pickle or disk cache. Expired, corrupt, or undecodable entries
  become misses and are removed; model execution continues with a cold stage.

Writes use immutable conditional first-writer-wins semantics and are delayed
until the entire invocation succeeds. Input files are full-hashed before
preprocessing and again immediately before commit. Cancellation, model fault,
or input drift commits no pending entry. Runtime same-session execution remains
single-flight; concurrent cold requests in distinct sessions may duplicate
deterministic preprocessing, after which the first successful immutable put
wins.

## Sealed operator configuration

Create an exact JSON object outside the repository. Extra fields are rejected.
Every path is absolute; inputs must exist, the evidence output must not exist,
and its parent must already exist.

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
  "preprocess_version": "operator-media-v1",
  "code_version": "operator-cache-code-v1",
  "evidence_output": "<absolute-new-path-for-p4c-evidence.json>"
}
```

For `V2A`, omit `prompt`. For `AC-V2A`, add `audio` and omit `prompt`. For
`T2A`, omit `video`. `TV2A` and `TC-V2A` both require `video` plus `prompt`.

The backend module must expose:

```python
def create_controlfoley_staged_backend() -> ControlFoleyStagedBackend: ...
```

The returned backend must also structurally implement
`ControlFoleyPreprocessCacheCodec`. The operator owns the declared
`preprocess_version` and `code_version`; changing either produces a distinct
sealed deployment and cache key.

## Exact command

From the repository root, with the operator backend importable:

```bash
export CONTROLFOLEY_P4C_GPU_CONFIG="$($PWD/.venv/bin/python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))))' /absolute/path/to/p4c-config.json)"
.venv/bin/pytest -q -m 'gpu and controlfoley' tests/test_controlfoley_cache.py::test_controlfoley_l0_l1_cache_equivalence_when_operator_configured
```

Configured source origin/revision/dirty state, manifest, fixture, input,
checkpoint, or external-weight failure is a test failure even when CUDA is
also absent. Only an otherwise valid configuration on a host without actual
torch/CUDA capability is skipped.

Before evidence is written, the harness repeats the full source/weight seals,
re-reads the deployment and fixture manifests, and recalculates the canonical
invocation fingerprint. The exclusive-created, path-free JSON records raw
output SHA-256 values and truthful cold/warm `CacheReport` values. It contains
no timing target, benchmark result, upstream parity verdict, or acceleration
claim. RTX 4090 cache equivalence remains deferred until that evidence is
actually captured and reviewed.
