# Stable Audio 3 Small-SFX baseline runbook (Phase 8A)

This runbook is deferred validation, not a completed result. Run it only on the
designated RTX 4090 host after the operator has accepted the gated Hugging Face
terms and installed the pinned source and weights **outside** this repository.
Do not commit weights, Hugging Face caches, tokens, prompts, or generated media.

## Preconditions

- Isolated environment `stable-audio-3-pytorch-2.7.1-cu126`.
- Source revision `a0b57f5483c4588f827f3552b7d5c6ca2a9687be`.
- Model id `small-sfx` from `stabilityai/stable-audio-3-small-sfx`.
- Official post-trained policy: `steps=8`, `cfg_scale=1.0`, `sampler=pingpong`,
  `batch_size=1`.
- Output contract: 44.1 kHz stereo WAV.

```bash
export STABLE_AUDIO_3_SOURCE_DIR=/operator/controlled/stable-audio-3
export STABLE_AUDIO_3_WEIGHTS_DIR=/operator/controlled/stable-audio-3-weights
export HF_HOME=/operator/controlled/hf-cache
```

## Validate checked-in manifests without weights

```bash
pytest -m "gpu" tests/test_stable_audio_3_baseline.py -v

for fixture in t2sfx-5s t2sfx-30s t2sfx-120s; do
  python benchmarks/stable_audio_3_baseline.py \
    --deployment benchmarks/fixtures/stable-audio-3/deployment.lock.json \
    --fixture "benchmarks/fixtures/stable-audio-3/${fixture}.json" \
    --write-result-template "benchmarks/results/stable-audio-3/${fixture}.planned.json"
done
```

Checked-in fingerprints remain `pending`. A skip is not a passed Gate.

## Capture real evidence later

Seal private copies of the deployment and fixtures from the exact installed
weights and prompts, then record sanitized 5s / 30s / 120s cold/warm results.
Do not mark this Gate passed until those files exist on the designated host.
