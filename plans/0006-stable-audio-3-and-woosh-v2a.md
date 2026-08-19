# 计划 0006 — Stable Audio 3 Small-SFX 与 Woosh V2A

状态：阶段 1 以及已完成的 ControlFoley 非硬件 Gate 之后即可开始。
本计划包含独立 Gate 的路线图阶段 7A–11；每个
路线图阶段恰好对应一个 PR。ControlFoley 仍为第一个适配器。Stable
Audio 3 Small-SFX 与 Sony Woosh V2A 为额外适配器。它们不是 Core
抽象，也不重新打开阶段 6。

`docs/source-plans/` 仍为归档研究，不得编辑。

## 产品划分

```text
Text-to-SFX:
  Stable Audio 3 Small-SFX

Video-to-SFX:
  ControlFoley
  Woosh-DVFlow-8s   (默认 Woosh 生产后端)
  Woosh-VFlow-8s    (可选参考/质量后端)
```

## 范围

在既有 Runtime Core 之后增加两个新模型族：

- Stable Audio 3 V1：仅操作 `audio.text_to_sfx`，官方 PyTorch
  Small-SFX 路径，44.1 kHz stereo WAV，可变长度最长 120 秒。
- Woosh V1：一个适配器 `woosh-v2a`，一个操作 `audio.video_to_sfx`，两个
  封印的 Deployment 后端 `dvflow-8s` 与 `vflow-8s`。固定 8 秒窗口，
  48 kHz mono WAV。共享 Woosh-AE、TextConditionerV 与 Synchformer 视频
  条件。

阶段 7A 仅为文档。阶段 7B 增加插件元数据、builder
注册表、Worker 能力路由以及通用 CLI 分发器，不含任何
新模型后端。后续字母切片增加溯源、本地适配器、
持久化 Worker、可选 profiler/缓存、可选工作流以及硬件
证据。

## 非目标

- 不更改 Runtime Core `load()` / `invoke()` / `unload()`。
- Core 中无名为 `prompt`、`video_path`、`duration_seconds`、`num_steps`、
  `cfg`、`solver`、`renoise`、Synchformer、SAME、T5Gemma、VFlow 或 DVFlow 的字段。
- 根包无 torch、Hugging Face 或上游模型依赖。
- 不为全部三个族使用单一 virtualenv 或通用 GPU Worker 镜像。
- 无 Woosh-Flow、Woosh-DFlow、Woosh text-to-audio、TextConditionerA、5 秒
  Woosh T2A，或统一的 Woosh T2A/V2A 适配器。
- V1 中无 Stable Audio audio-to-audio、inpainting、continuation、LoRA、TensorRT、
  TFLite、MLX 或 `batch_size > 1`。
- 无隐式多段 Woosh 视频处理、静音填充、末帧
  冻结，或适配器内 mux。
- 不将权重、缓存、生成媒体、Hugging Face token 或私有路径
  提交到仓库。
- 不由跳过或延期的 GPU 测试得出对齐、质量或性能声称。
- 不将字母切片合并为一个 PR，也不将阶段 6 硬件
  延期视为已通过的 Gate。

## 交付物

### 路线图阶段 7A（一个 PR）— 多适配器架构冻结

仅为文档：本计划、ADR 0003、ADR 0004、Roadmap 与 Status
更新，以及源/依赖/许可证研究说明。无生产 Python。

### 路线图阶段 7B（一个 PR）— 插件、builder 注册表、Worker 路由

无 torch 的适配器插件元数据、`DurableInvocationBuilder` 注册表、
Worker 能力描述符、感知部署的领取过滤、通用
`nano-aural` 分发器以及 ControlFoley 兼容性。无新模型适配器。

### 路线图阶段 8A（一个 PR）— Stable Audio 3 基线/溯源

官方 direct runner 锁定、源/HF revision 锁定、门控模型准备
runbook、权重清单 schema 以及 5s / 30s / 120s 基线配置。
条件 GPU 测试在前置条件缺失时跳过。

### 路线图阶段 8B（一个 PR）— Stable Audio 3 本地适配器

`nano_aural_runtime_stable_audio_3`、`audio.text_to_sfx` 任务模式、
进程内会话、本地 CLI、WAV 序列化、44.1 kHz stereo 校验，
以及执行时源码/权重再校验。

### 路线图阶段 8C（一个 PR）— Stable Audio 3 持久化 Worker

调用构建器、隔离 Worker 环境、能力路由、持久化
执行、产物发布、远程 CLI 以及恢复测试。

### 路线图阶段 8D（一个 PR，可选）— Stable Audio 3 编辑

`audio_to_audio`、inpaint 与 continuation。不阻塞 Woosh V2A。

### 路线图阶段 8E（一个 PR，可选）— Stable Audio 3 profiler/缓存

实验性、默认关闭的观测与文本条件缓存。不
阻塞 Woosh V2A。

### 路线图阶段 9A（一个 PR）— Woosh VFlow/DVFlow 基线/溯源

钉扎 SonyResearch/Woosh、release/tag 溯源、Woosh-AE、TextConditionerV、
Woosh-VFlow-8s、Woosh-DVFlow-8s 以及 MMAudio Synchformer checkpoint。
官方直接运行 harness、8 秒视频夹具契约、可选 prompt、
固定 seed 比较。不检查或适配 Woosh-Flow、Woosh-DFlow 或 T2A。

### 路线图阶段 9B（一个 PR）— Woosh V2A 本地适配器

一个包、一个任务、两个后端。本地 CLI、自 0 起的 8 秒窗口，
拒绝短于 8 秒的视频，DVFlow 默认，VFlow 可选，
48 kHz mono WAV，适配器内无 mux。

### 路线图阶段 9C（一个 PR）— Woosh 持久化 Worker

`WooshV2ADurableInvocationBuilder`、隔离环境、已验证视频
物化、Runtime invoke、发布、围栏/恢复、远程 CLI。

### 路线图阶段 9D（一个 PR）— Woosh profiler/缓存

实验性、默认关闭。仅缓存视频预处理、Synchformer
特征、文本 token 以及 empty/unconditional 条件。无 ODE 轨迹
缓存、latent 复用、step 跳过或跨 seed 复用。

### 路线图阶段 10A（一个 PR）— 统一工作流与可选 ComfyUI 映射

工作流 `sfx.text_generate`、`sfx.video_generate` 以及显式
`sfx.generate_and_mux`。Mux 不是 Woosh 适配器步骤。ComfyUI 仍为
可选且可拆除。

### 路线图阶段 10B（一个 PR）— RTX 4090 证据

延期至指定主机可用。不是软件切片阻塞项。

### 路线图阶段 11（一个 PR）— 适配器族发行加固

许可证声明、门控 HF token 处理、Synchformer/MMAudio 归属以及
发行证据。ControlFoley 阶段 6 硬件 Gate 保持独立，
对合并产品发行仍为发行阻塞。

## 测试

- 阶段 7A：术语审查。Core 保持模型中立。Woosh T2A 术语
  仅出现在显式范围外列表中。一阶段/一 PR 映射
  完整。
- 阶段 7B：无 torch 的插件发现；ControlFoley CLI `--help` 与
  本地路径保持；builder 注册表拒绝封印字段泄漏；Worker
  能力不匹配失败即关闭；Core import-boundary 扫描保持通过。
- 后续阶段：CPU schema/溯源测试始终运行；GPU/源码/权重
  测试在声明的前置条件缺失时跳过；跳过的测试永不
  记录为通过。

## Gate

- 阶段 7A：文档就产品划分、隔离 Worker
  环境、仅 Woosh V2A 范围、DVFlow 默认、VFlow 可选、
  8 秒失败即关闭窗口以及一阶段/一 PR 规则达成一致。本 PR 无生产
  Python 落地。
- 后续非硬件 Gate 遵循路线图。RTX 4090 证据为
  `DEFERRED`，从未通过，且不阻塞路线图标为未阻塞的
  独立软件切片。
- 不存在 Woosh T2A/Flow/DFlow 代码是本计划中每个 Gate 的
  不变量。
