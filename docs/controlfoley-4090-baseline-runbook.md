# ControlFoley 4090 baseline and adapter runbook (Phases 2A–2B)

This runbook is deferred validation, not a completed result. It may be run only
on the designated RTX 4090 host after the pinned source and operator-provided
weights have been installed outside this repository. Do not commit weights, HF
caches, user media, private paths, environment variables, or generated media.

## Preconditions

- Check out the implementation revision that contains Phase 2A.
- Use ControlFoley source revision `6858cd12a48d141201e3266e7abe1f38357a133e`.
- The locked upstream `demo.py` baseline uses its default `fp32` execution for
  variant `large_44k`; Phase 2A does not pass or infer any precision override.
- Supply the source and weight locations only through this shell session:

```bash
export CONTROLFOLEY_SOURCE_DIR=/operator/controlled/controlfoley
export CONTROLFOLEY_WEIGHTS_DIR=/operator/controlled/controlfoley-weights
export HF_HOME=/operator/controlled/hf-cache
python -m pip install -e ".[dev]"
```

## Validate each declared baseline fixture

The harness validates the source lock, clean Git working tree, origin diagnostic,
the four direct-demo external weight paths, the main checkpoint, and each input
fingerprint before it can execute. It imports torch/torchaudio and the upstream
source only inside a separately launched worker after `--execute-upstream` is
given. Before the host is configured, the pytest GPU test is skipped; neither a
skip nor a preflight report is a successful model run.

```bash
pytest -m "gpu and controlfoley" -v

for fixture in v2a tv2a tc-v2a ac-v2a t2a; do
  python benchmarks/controlfoley_baseline.py \
    --deployment benchmarks/fixtures/controlfoley/deployment.lock.json \
    --fixture "benchmarks/fixtures/controlfoley/${fixture}.json" \
    --source-dir "$CONTROLFOLEY_SOURCE_DIR" \
    --weights-dir "$CONTROLFOLEY_WEIGHTS_DIR" \
    --write-result-template "benchmarks/results/controlfoley/${fixture}.planned.json"
done
```

The template command writes only a `state: planned` manifest at
`benchmarks/results/controlfoley/<fixture>.planned.json`. It records no waveform,
timing, VRAM, digest, threshold, or result claim.

## Capture real evidence with this repository's harness

First seal a private copy of the deployment manifest from the exact installed
weights, and seal a private copy of the fixture from the exact input bytes. The
checked-in input manifests intentionally remain `pending`; they can never be
executed directly.

```bash
python benchmarks/controlfoley_baseline.py \
  --deployment benchmarks/fixtures/controlfoley/deployment.lock.json \
  --fixture benchmarks/fixtures/controlfoley/tv2a.json \
  --weights-dir "$CONTROLFOLEY_WEIGHTS_DIR" \
  --write-verified-deployment /operator/controlled/controlfoley-deployment.verified.json

python benchmarks/controlfoley_baseline.py \
  --deployment benchmarks/fixtures/controlfoley/deployment.lock.json \
  --fixture benchmarks/fixtures/controlfoley/tv2a.json \
  --video /operator/controlled/media/tv2a.mp4 \
  --prompt "operator supplied prompt" \
  --write-verified-fixture /operator/controlled/tv2a.verified.json
```

Then the following exact harness command runs the pinned source's original
`demo.py` twice, by argv only (never `shell=True`), with the documented
`variant`, `video`, `audio` when applicable, `prompt`, `negative_prompt`,
`duration`, `cfg_strength`, `num_steps`, `output`, `seed`,
`skip_video_composite`, and `mask_away_clip` parameters. It writes one
sanitized completed manifest at the stated result location.

```bash
python benchmarks/controlfoley_baseline.py \
  --deployment /operator/controlled/controlfoley-deployment.verified.json \
  --fixture /operator/controlled/tv2a.verified.json \
  --source-dir "$CONTROLFOLEY_SOURCE_DIR" \
  --weights-dir "$CONTROLFOLEY_WEIGHTS_DIR" \
  --execute-upstream \
  --video /operator/controlled/media/tv2a.mp4 \
  --prompt "operator supplied prompt" \
  --output-dir /operator/controlled/results/tv2a-repeat-1 \
  --repeat-output-dir /operator/controlled/results/tv2a-repeat-2 \
  --result benchmarks/results/controlfoley/tv2a.result.json
```

For V2A omit `--prompt`; for AC-V2A add `--audio` and seal that input; for
T2A omit `--video`. The result manifest contains:

The fixture owns duration, CFG strength, number of steps, and seed. The harness
passes those values to the original demo and rejects command-line overrides, so
the completed result stays bound to the sealed fixture.

Phase 2A also locks `negative_prompt` to the upstream default empty string and
locks `skip_video_composite` and `mask_away_clip` to `false`; do not pass those
flags to the execute command.

- waveform channels, samples, and sample rate;
- wall time plus allocated/reserved peak VRAM;
- raw self-repeat metrics for two identical upstream runs;
- output full SHA-256 for both repeats and verified source/checkpoint/input
  fingerprints;
- the sanitized argv parameter record (values and text are represented by
  scalar values or fingerprints, never paths);
- thresholds only after they are derived and justified from recorded repeats;
- the sanitized environment report.

Do not fill a missing value with a placeholder digest or threshold. A missing
fingerprint is `status: pending` with `sha256` and `size_bytes` both `null`.
The project must continue to report the 4090 baseline as **DEFERRED** until
those evidence records are reviewed.

## Phase 3D fenced durable-worker smoke

This is a conditional worker-integration check, not parity, performance, or
artifact-publication evidence. On the designated host, create a private JSON
object in `CONTROLFOLEY_P3D_GPU_CONFIG` containing a PostgreSQL DSN, registered
worker id, exact queued job id, lease duration, canonical/workspace roots, and
the sealed operator configuration. The selected job must be T2A with its prompt
and locked parameters in the canonical request and zero binary inputs. P3C
separately proves verified-media materialization; this smoke deliberately uses
T2A because the current default worker probe is WAV-only. Never commit the
JSON, DSN, paths, weights, prompt, credentials, or generated output.

```bash
export CONTROLFOLEY_P3D_GPU_CONFIG="$(cat /operator/controlled/p3d-worker-smoke.json)"
pytest -m "gpu and controlfoley" -q \
  tests/test_controlfoley_durable_binding.py::test_controlfoley_durable_runtime_worker_gpu_smoke_is_explicitly_conditional
```

The test claims the zero-input T2A job through the P3C queue, invokes the Runtime
directly with the ControlFoley adapter, and verifies that no artifact, winner,
or `SUCCEEDED` state was published. It is skipped when this sealed host
configuration is unavailable; a skip is not a passed 4090 result.

## Exercise the Phase 2B adapter after the baseline

The local adapter accepts the same verified deployment and runs the pinned
original `demo.py` once through an isolated worker. It does not accept a
precision override, staged path, or cache option. The command writes the
requested FLAC only if the destination does not already exist, and prints a
path-free result manifest to standard output. Capture that output outside the
repository next to the private result; do not treat matching filenames or a
command exit as a parity conclusion.

```bash
nano-aural controlfoley local \
  --manifest /operator/controlled/controlfoley-deployment.verified.json \
  --source-dir "$CONTROLFOLEY_SOURCE_DIR" \
  --weights-dir "$CONTROLFOLEY_WEIGHTS_DIR" \
  --task TV2A \
  --video /operator/controlled/media/tv2a.mp4 \
  --prompt "operator supplied prompt" \
  --seed 0 \
  --output /operator/controlled/results/tv2a.adapter.flac \
  > /operator/controlled/results/tv2a.adapter.result.json
```

Then record the baseline and adapter output full SHA-256 values, waveform
shape/rate, and any measured comparison in a reviewed private evidence record.
No comparison threshold or parity/performance claim is defined by this command.
