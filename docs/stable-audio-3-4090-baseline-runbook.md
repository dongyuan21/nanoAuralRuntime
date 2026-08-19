# Stable Audio 3 Small-SFX 基线运行手册（Phase 8A）

本运行手册是延期验证，不是已完成结果。仅在指定 RTX 4090 宿主上、运营方已接受门控 Hugging Face
条款并将固定源码与权重安装到本仓库**之外**后运行。
不得提交权重、Hugging Face 缓存、令牌、提示或生成的媒体。

## 前置条件

- 隔离环境 `stable-audio-3-pytorch-2.7.1-cu126`。
- 源码修订 `a0b57f5483c4588f827f3552b7d5c6ca2a9687be`。
- 模型 id `small-sfx`，来自 `stabilityai/stable-audio-3-small-sfx`。
- 官方训后策略：`steps=8`、`cfg_scale=1.0`、`sampler=pingpong`、
  `batch_size=1`。
- 输出契约：44.1 kHz 立体声 WAV。

```bash
export STABLE_AUDIO_3_SOURCE_DIR=/operator/controlled/stable-audio-3
export STABLE_AUDIO_3_WEIGHTS_DIR=/operator/controlled/stable-audio-3-weights
export HF_HOME=/operator/controlled/hf-cache
```

## 在无权重情况下验证已检入清单

```bash
pytest -m "gpu" tests/test_stable_audio_3_baseline.py -v

for fixture in t2sfx-5s t2sfx-30s t2sfx-120s; do
  python benchmarks/stable_audio_3_baseline.py \
    --deployment benchmarks/fixtures/stable-audio-3/deployment.lock.json \
    --fixture "benchmarks/fixtures/stable-audio-3/${fixture}.json" \
    --write-result-template "benchmarks/results/stable-audio-3/${fixture}.planned.json"
done
```

已检入指纹保持 `pending`。跳过不是已通过的 Gate。

## 稍后捕获真实证据

从精确安装的权重与提示密封部署与 fixture 的私有副本，然后记录脱敏的 5s / 30s / 120s 冷/热结果。
在指定宿主上这些文件存在之前，不要将此 Gate 标记为通过。
