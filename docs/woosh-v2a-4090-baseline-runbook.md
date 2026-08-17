# Woosh V2A VFlow/DVFlow baseline runbook (Phase 9A)

This runbook is deferred validation, not a completed result. Run it only on the
designated RTX 4090 host after the operator has installed the pinned Woosh
source and in-scope `v1.0.0` archives **outside** this repository. Do not commit
weights, Synchformer checkpoints, videos, prompts, or generated media. Do not
download or inspect `Woosh-Flow`, `Woosh-DFlow`, `TextConditionerA`, or
`Woosh-CLAP`.

## Preconditions

- Isolated environment `woosh-v2a-pytorch-2.8.0-cu128`.
- Source tag `v1.0.0`, revision `f6ff658efc6d63dee9959964cd75c63415910a19`.
- In-scope archives only: Woosh-AE, TextConditionerV, Woosh-VFlow-8s,
  Woosh-DVFlow-8s.
- Synchformer from Hugging Face `hkchengrex/MMAudio`,
  `ext_weights/synchformer_state_dict.pth`.
- Official sampler pins: DVFlow `sample_euler` steps=4, renoise
  `[0, 0.5, 0.5, 0.3]`, cfg=3; VFlow `flowmatching_integrate` method=`dopri5`,
  atol/rtol=`1e-3`, cfg=4.5.
- Video window `[0, 8)` from t=0. Videos shorter than 8 seconds fail closed.
- Output contract: 48 kHz mono WAV. Mux is not an adapter step.

```bash
export WOOSH_SOURCE_DIR=/operator/controlled/woosh
export WOOSH_WEIGHTS_DIR=/operator/controlled/woosh-checkpoints
export WOOSH_SYNCHFORMER_PATH=/operator/controlled/synchformer_state_dict.pth
export HF_HOME=/operator/controlled/hf-cache
```

## Validate checked-in manifests without weights

```bash
pytest -m "gpu" tests/test_woosh_v2a_baseline.py -v

for pair in \
  "dvflow-8s.lock.json v2sfx-8s-video-only.json" \
  "dvflow-8s.lock.json v2sfx-8s-video-prompt.json" \
  "vflow-8s.lock.json v2sfx-8s-video-only.json" \
  "vflow-8s.lock.json v2sfx-8s-video-prompt.json"
do
  set -- $pair
  python benchmarks/woosh_v2a_baseline.py \
    --deployment "benchmarks/fixtures/woosh-v2a/$1" \
    --fixture "benchmarks/fixtures/woosh-v2a/$2" \
    --write-result-template "benchmarks/results/woosh-v2a/${1%.lock.json}-${2%.json}.planned.json"
done

python benchmarks/woosh_v2a_baseline.py \
  --deployment benchmarks/fixtures/woosh-v2a/dvflow-8s.lock.json \
  --fixture benchmarks/fixtures/woosh-v2a/v2sfx-8s-video-only.json \
  --self-repeat \
  --write-result-template benchmarks/results/woosh-v2a/dvflow-8s-self-repeat.planned.json

python benchmarks/woosh_v2a_baseline.py \
  --deployment benchmarks/fixtures/woosh-v2a/vflow-8s.lock.json \
  --fixture benchmarks/fixtures/woosh-v2a/v2sfx-8s-video-only.json \
  --self-repeat \
  --write-result-template benchmarks/results/woosh-v2a/vflow-8s-self-repeat.planned.json
```

Checked-in inner config/weight and Synchformer fingerprints remain `pending`.
A skip is not a passed Gate.

## Capture real evidence later

Seal private copies of the deployments and fixtures from the exact extracted
archives, Synchformer file, and 8-second video, then record sanitized DVFlow
and VFlow cold/warm results for video-only, video+prompt, and same-seed
self-repeat. Do not mark this Gate passed until those files exist on the
designated host.
