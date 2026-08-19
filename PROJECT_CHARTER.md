# nanoAuralRuntime 项目章程

## 使命

nanoAuralRuntime 是面向音频基础模型的音频原生、模型无关、适配器优先的运行时、工作流 SDK 与持久化服务栈。它让模型执行可复现、可观测、可取消、可安全发布，同时不把任何单一模型或 UI 框架当作系统边界。

ControlFoley 是第一个模型适配器，也是第一条完整垂直工作流。它证明架构可行，但不是产品定义。

## 结果

- 本地执行路径可以加载已声明的模型部署、调用它，并在没有 Web 服务或 ComfyUI 的情况下产出已验证产物。
- 持久化执行路径可以接受已验证资产、管理任务与尝试、隔离过期 Worker，并且只暴露一个已验证的获胜结果。
- 模型特定语义放在适配器与任务模式之后，从而可以加入第二个音频模型而不扩大 Runtime Core 契约。
- CLI、HTTP API 与 ComfyUI 都是可替换前端。拆除 ComfyUI 不得影响 Runtime Core、CLI、API、Worker 或其测试。

## 架构承诺

1. Runtime Core 模型无关。其稳定概念是 `AudioModelAdapter`、`ModelDescriptor`、`ModelDeployment`、`ModelSession`、`ModelInvocation`、`InvocationResult`、`ProducedArtifact`、`ExecutionContext`、`CancellationToken`、`ProfileReport` 与 `CacheReport`。
2. Core 要求的适配器生命周期操作只有 `load()`、`invoke()` 与 `unload()`。模型能力与模型自有的任务模式描述可选语义。
3. 运行时执行与持久化控制面状态分离。Core 永不依赖 HTTP、PostgreSQL、对象存储、认证或 ComfyUI。
4. 持久化执行在尝试层至少一次（at-least-once），在可见结果层恰好一个（exactly-one）。模型调用成功本身绝不等于任务成功。
5. 用户提供的媒体必须先成为已验证、内容寻址的资产，才能作为持久化任务输入。应用层全文件 SHA-256 是内容身份；对象存储 ETag 不是。
6. 每个路线图交付阶段恰好对应一个 PR，且只有在其声明的 Gate 通过后才能继续。带字母的阶段（例如 `2A`、`3D`）是完整交付阶段，不是同一个 PR 里的子任务。

## 初始计划的非目标

- 训练、微调、再分发模型权重，或声称拥有模型许可权利。
- CUDA/Triton/TensorRT/ONNX 优化、改采样公式，或未经测量的性能声称。
- 多 GPU 张量并行与跨请求连续批处理。
- 让 ComfyUI、上游模型仓库或存储提供商成为任务状态的权威来源。

## 规划材料的权威性

`docs/source-plans/controlfoley-runtime-research.md` 与 `docs/source-plans/headless-durable-serving-research.md` 是归档研究输入。它们被有意保留，不是实现权威，也不得为跟踪项目而改写。本章程、`ARCHITECTURE.md`、ADR 以及带 Gate 的 `ROADMAP.md` 才是权威引导集合。

`plans/0001-runtime-core.md` 到 `plans/0006-stable-audio-3-and-woosh-v2a.md` 是计划文件：提供详细工作拆解。其带字母的 PR 切片与 `ROADMAP.md` 中的交付阶段一一对应；编号文件标题并不授权把带字母切片合并进同一个 PR。`plans/STATUS.md` 是执行索引，不是计划，也不是 Gate 权威。

## 成功标准

当已声明的适配器可以在本地与持久化路径上被调用；每一个对外结果都已验证且属于一个获胜尝试；模型特定选项仍留在 Core 之外；删除可选 ComfyUI 集成后无头路径仍然完整——本计划即告成功。
