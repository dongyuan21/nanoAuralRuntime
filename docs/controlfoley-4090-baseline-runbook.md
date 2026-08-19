# ControlFoley 4090 基线与适配器运行手册（Phases 2A–2B）

本运行手册是延期验证，不是已完成结果。仅可在指定 RTX 4090 宿主上、于固定源码与运营方提供的
权重已安装到本仓库之外后运行。不得提交权重、HF
缓存、用户媒体、私有路径、环境变量或生成的媒体。

## 前置条件

- 检出包含 Phase 2A 的实现修订。
- 使用 ControlFoley 源码修订 `6858cd12a48d141201e3266e7abe1f38357a133e`。
- 锁定的上游 `demo.py` 基线对其变体 `large_44k` 使用默认 `fp32` 执行；Phase 2A 不传递或推断任何精度覆盖。
- 仅通过本 shell 会话提供源码与权重位置：

```bash
export CONTROLFOLEY_SOURCE_DIR=/operator/controlled/controlfoley
export CONTROLFOLEY_WEIGHTS_DIR=/operator/controlled/controlfoley-weights
export HF_HOME=/operator/controlled/hf-cache
python -m pip install -e ".[dev]"
```

## 验证每个已声明的基线 fixture

harness 在能够执行之前校验源码锁定、干净的 Git 工作树、来源诊断、
四个直接 demo 外部权重路径、主检查点以及每个输入
指纹。仅在给出 `--execute-upstream` 之后，于单独启动的 worker 内导入 torch/torchaudio 与上游
源码。在宿主配置完成之前，pytest GPU 测试会被跳过；跳过
与预检报告都不是成功的模型运行。

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

模板命令仅在
`benchmarks/results/controlfoley/<fixture>.planned.json` 写入 `state: planned` 清单。它不记录波形、
计时、VRAM、摘要、阈值或结果主张。

## 使用本仓库的 harness 捕获真实证据

首先从精确安装的权重密封部署清单的私有副本，
并从精确输入字节密封 fixture 的私有副本。
已检入的输入清单有意保持 `pending`；它们永远不能被
直接执行。

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

随后以下精确 harness 命令仅通过 argv（永不 `shell=True`）将固定源码的原始
`demo.py` 运行两次，使用文档中的
`variant`、`video`、适用时的 `audio`、`prompt`、`negative_prompt`、
`duration`、`cfg_strength`、`num_steps`、`output`、`seed`、
`skip_video_composite` 与 `mask_away_clip` 参数。它在所述结果位置写入一份
脱敏的已完成清单。

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

对于 V2A 省略 `--prompt`；对于 AC-V2A 添加 `--audio` 并密封该输入；对于
T2A 省略 `--video`。结果清单包含：

fixture 拥有时长、CFG 强度、步数与种子。harness
将这些值传递给原始 demo，并拒绝命令行覆盖，因此
已完成结果保持绑定到密封 fixture。

Phase 2A 还将 `negative_prompt` 锁定为上游默认空字符串，并将
`skip_video_composite` 与 `mask_away_clip` 锁定为 `false`；不要将这些
标志传递给执行命令。

- 波形通道数、样本数与采样率；
- 墙钟时间加上 allocated/reserved 峰值 VRAM；
- 两次相同上游运行的原始自重复指标；
- 两次重复的完整输出 SHA-256 以及已验证的源码/检查点/输入
  指纹；
- 脱敏的 argv 参数记录（值与文本由
  标量值或指纹表示，永不使用路径）；
- 仅在从已记录重复推导并论证之后的阈值；
- 脱敏的环境报告。

不要用占位摘要或阈值填充缺失值。缺失
指纹为 `status: pending`，且 `sha256` 与 `size_bytes` 均为 `null`。
项目必须继续将 4090 基线报告为 **DEFERRED**，直至
这些证据记录被审查。

## Phase 3D 围栏持久 worker 冒烟

这是条件性 worker 集成检查，不是一致性、性能或
产物发布证据。在指定宿主上，在 `CONTROLFOLEY_P3D_GPU_CONFIG` 中创建包含 PostgreSQL DSN、已注册
worker id、精确排队作业 id、租约时长、规范/工作区根以及
密封运营方配置的私有 JSON 对象。所选作业必须为 T2A，其提示
与锁定参数在规范请求中，且二进制输入为零。P3C
单独证明已验证媒体物化；本冒烟有意使用
T2A，因为当前默认 worker 探测仅为 WAV。永不提交
JSON、DSN、路径、权重、提示、凭据或生成输出。

```bash
export CONTROLFOLEY_P3D_GPU_CONFIG="$(cat /operator/controlled/p3d-worker-smoke.json)"
pytest -m "gpu and controlfoley" -q \
  tests/test_controlfoley_durable_binding.py::test_controlfoley_durable_runtime_worker_gpu_smoke_is_explicitly_conditional
```

测试通过 P3C 队列认领零输入 T2A 作业，使用 ControlFoley 适配器直接调用 Runtime，
并验证未发布产物、胜者
或 `SUCCEEDED` 状态。当此密封宿主
配置不可用时跳过；跳过不是已通过的 4090 结果。

## 在基线之后演练 Phase 2B 适配器

本地适配器接受同一已验证部署，并通过隔离 worker 将固定的
原始 `demo.py` 运行一次。它不接受
精度覆盖、分阶段路径或缓存选项。仅当目标尚不存在时命令才写入
所请求的 FLAC，并向标准输出打印
无路径结果清单。将该输出捕获到仓库之外、紧邻私有结果；不要将匹配文件名或
命令退出视为一致性结论。

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

然后在经审查的私有证据记录中记录基线与适配器输出的完整 SHA-256 值、波形
形状/采样率以及任何已测量比较。
本命令不定义任何比较阈值或一致性/性能主张。
