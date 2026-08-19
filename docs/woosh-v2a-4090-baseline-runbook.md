# Woosh V2A VFlow/DVFlow 基线运行手册（Phase 9A）

本运行手册是延期验证，不是已完成结果。仅在指定 RTX 4090 宿主上、运营方已将固定的 Woosh
源码与范围内的 `v1.0.0` 归档安装到本仓库**之外**后运行。不得提交
权重、Synchformer 检查点、视频、提示或生成的媒体。不得
下载或检查 `Woosh-Flow`、`Woosh-DFlow`、`TextConditionerA` 或
`Woosh-CLAP`。

## 前置条件

- 隔离环境 `woosh-v2a-pytorch-2.8.0-cu128`。
- 源码标签 `v1.0.0`，修订 `f6ff658efc6d63dee9959964cd75c63415910a19`。
- 仅范围内归档：Woosh-AE、TextConditionerV、Woosh-VFlow-8s、
  Woosh-DVFlow-8s。
- Synchformer 来自 Hugging Face `hkchengrex/MMAudio`，
  `ext_weights/synchformer_state_dict.pth`。
- 官方采样器固定：DVFlow `sample_euler` steps=4，renoise
  `[0, 0.5, 0.5, 0.3]`，cfg=3；VFlow `flowmatching_integrate` method=`dopri5`，
  atol/rtol=`1e-3`，cfg=4.5。
- 视频窗口从 t=0 起为 `[0, 8)`。短于 8 秒的视频 fail closed。
- 输出契约：48 kHz 单声道 WAV。Mux 不是适配器步骤。

```bash
export WOOSH_SOURCE_DIR=/operator/controlled/woosh
export WOOSH_WEIGHTS_DIR=/operator/controlled/woosh-checkpoints
export WOOSH_SYNCHFORMER_PATH=/operator/controlled/synchformer_state_dict.pth
export HF_HOME=/operator/controlled/hf-cache
```

## 在无权重情况下验证已检入清单

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

已检入的内部配置/权重与 Synchformer 指纹保持 `pending`。
跳过不是已通过的 Gate。

## 稍后捕获真实证据

从精确解压的归档、Synchformer 文件与 8 秒视频密封部署与 fixture 的私有副本，然后记录脱敏的 DVFlow
与 VFlow 在仅视频、视频+提示以及同一种子
自重复上的冷/热结果。在指定宿主上这些文件存在之前，不要将此 Gate 标记为通过。
