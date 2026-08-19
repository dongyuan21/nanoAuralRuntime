# 阶段 1 — 模型无关的 Runtime Core

状态：可开始实现。此为单个 PR，必须在任何模型特定或服务工作之前落地。

## 范围

围绕以下概念构建仅 CPU、模型无关的运行时包：`AudioModelAdapter`、`ModelDescriptor`、`ModelDeployment`、`ModelSession`、`ModelInvocation`、`InvocationResult`、`ProducedArtifact`、`ExecutionContext`、`CancellationToken`、`ProfileReport`、`CacheReport`、类型化错误、`AdapterRegistry` 以及 `Runtime.invoke()`。

会话生命周期显式（`UNLOADED → LOADING → READY → RUNNING → READY|FAILED → UNLOADING → UNLOADED`）。部署/会话为 single-flight：并发调用被确定性串行化或拒绝，运行中禁止 unload。包含假/回声适配器，以便在无 PyTorch 或 GPU 时完整测试生命周期。

## 非目标

- 无 ControlFoley 导入、字段、任务模式、模型加载或媒体生成语义。
- Core 中无 `GenerateRequest`/`GenerateResult`、video/reference-audio/prompt/steps/guidance 字段。
- 无 torch、CUDA、FastAPI、PostgreSQL、对象存储、Worker、HTTP API、CLI 或 ComfyUI 依赖。
- 无持久化队列、profiler/缓存实现、缓存存储或策略、基准或部署自动化。本阶段中 `ProfileReport` 与 `CacheReport` 仅为最小通用值契约。

## 交付物

- 可安装的 runtime-core 包与公开的纯类型契约。
- 不可变描述符/部署/调用/结果、最小通用 `ProfileReport`/`CacheReport` 值契约，以及多个 MIME 类型的 `ProducedArtifact`。
- 适配器协议限于 `load()`、`invoke()` 和 `unload()`；若后续需要生成专用协议，置于基协议之外。
- 生命周期管理器、注册表、取消令牌、错误分类以及 single-flight 策略。
- 假适配器与 CPU 单元测试夹具。
- 足以在干净 CPU 主机上运行检查的最小开发者配置与文档。

## 测试

- 在未安装 PyTorch 的环境中导入 Core。
- 假适配器：`load → invoke → unload`、unload 后 reload，以及失败转换行为。
- 验证调用前/调用中取消、非法生命周期调用、适配器查找失败，以及适配器出错后的清理。
- 验证对同一 single-flight 会话的同时请求被串行化或显式拒绝；永不并发运行。
- 验证一次调用可返回若干不同 MIME 类型的产物。
- 运行项目 formatter/linter、类型检查器以及 CPU 测试套件。

## Gate

- 全部 CPU 检查通过，且 Core 在无可选模型/服务/UI 依赖时即可导入。
- 依赖扫描与源码审查确认 Runtime Core 中无 ControlFoley、ComfyUI、FastAPI、SQLAlchemy、boto3、torch、`guidance`、`steps`、`video` 或 `reference_audio`。
- 生命周期与 single-flight 行为有直接测试证据。
- 此 Gate 无 RTX 4090 要求。在其通过之前不要开始路线图阶段 2A 或 3A。
