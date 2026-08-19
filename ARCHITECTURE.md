# nanoAuralRuntime 架构

## 状态与范围

本文是引导计划的权威架构。`docs/source-plans/` 中的两份文件是归档研究输入：它们提供有用的约束与实验，但不定义公开契约。

## 固定系统层

```text
Frontend
  -> Workflow
  -> Durable Service / Local Executor
  -> Runtime Core
  -> Model Adapter
  -> Original Model Backend
```

Frontend 指 CLI、HTTP API、ComfyUI，或未来客户端。Workflow 组合已声明的调用；它不拥有任何模型实现。Local Executor 与 Durable Service 是互替的执行环境。持久化 Worker 调用与本地执行器相同的 Runtime Core。

依赖仅指向下层。尤其是，Runtime Core 不得导入或依赖 FastAPI、数据库驱动、对象存储客户端、ComfyUI 或上游模型包。模型适配器不得依赖前端、持久化状态存储或 ComfyUI。

```text
CLI / HTTP API / ComfyUI
          |
       Workflow
       /       \
Local Executor  Durable Service -> GPU Worker
       \       /                    |
        Runtime Core <---------------+
             |
        Model Adapter
             |
   Original Model Backend
```

## Runtime Core 契约

Core 拥有通用生命周期、能力协商、执行上下文、取消、profiling、缓存计量与产物报告。它暴露以下词汇：

| 概念 | 职责 |
| --- | --- |
| `ModelDescriptor` | 稳定的适配器身份、版本与已声明能力。 |
| `ModelDeployment` | 不可变、由运营方拥有的适配器、源码、权重、设备策略与指纹选择。 |
| `AudioModelAdapter` | 实现 `load()`、`invoke()` 与 `unload()` 的模型边界。 |
| `ModelSession` | 带显式生命周期与并发限制的已加载部署。 |
| `ModelInvocation` | 按任务类型选择的、不透明且经 schema 校验的调用信封。 |
| `InvocationResult` | 适配器结果元数据、警告、profile、缓存报告与已产出产物。 |
| `ProducedArtifact` | 带媒体/内容元数据与完整性信息的命名输出。 |
| `ExecutionContext` | 请求身份、时间/截止策略、追踪、取消，以及运营方批准的资源。 |
| `CancellationToken` | 线程安全的协作式取消信号。 |
| `ProfileReport` / `CacheReport` | 观测数据；二者均不改变调用语义。 |

适配器在概念上至少表现为：

```text
session = adapter.load(deployment)
result  = adapter.invoke(session, invocation, context)
adapter.unload(session)
```

Core 不定义通用生成请求。它不含 `video_path`、`reference_audio_path`、`prompt`、`negative_prompt`、`num_steps`、`guidance_scale`、`mask_away_clip`，也不含任何 ControlFoley 实现术语，例如 CLIP、CAV-MAE、Synchformer、CLAP、FlowMatching、VAE 或 vocoder。这些属于 ControlFoley 适配器的任务 schema、执行计划、profiler 与缓存键。生成模型扩展可定义为可选协议，永不作为 `AudioModelAdapter` 的要求。

`ModelSession` 具有显式生命周期：`UNLOADED -> LOADING -> READY ->
RUNNING`，在成功或可恢复调用后回到 `READY`；故障会话必须拒绝新工作并卸载。每个部署声明其并发模型。ControlFoley 部署以单飞起步，因其上游模型具有可变实例状态。

## 执行环境

### 本地执行器

本地执行器接受本地、由运营方控制的输入，物化适配器特定的调用，调用 Core，校验输出并写入产物。它不创建持久化服务状态。

### 持久化服务

持久化服务拥有已认证上传、已验证资产、任务状态、尝试、租约、取消请求、产物授权与恢复。它不拥有模型语义。GPU Worker 拥有一个已声明部署，认领一次尝试，物化已验证输入，调用 Core，校验输出，并通过 compare-and-set 隔离完成最终化。

权威边界为：

| 事实 | 权威 |
| --- | --- |
| 资产可用性与任务/尝试/产物状态 | 持久化数据库 |
| 资产与产物内容身份 | 全文件 SHA-256 |
| 对象字节 | Blob 存储键，在数据库核验之后 |
| 合法尝试所有者 | 当前尝试加上租约/隔离世代 |
| 模型语义 | 适配器及其已声明部署 |
| UI 状态 | 绝非业务权威 |

所需不变量是至少一次执行，以及恰好一个可见获胜产物集。失去租约的过期 Worker 必须无法心跳、最终化或暴露产物。只有已验证资产可作为输入提交，且 `SUCCEEDED` 意味着每一个所需产物均已核验、就绪，并附着到获胜尝试。

模型族不共享 Worker 环境。ControlFoley、Stable Audio 3
Small-SFX 与 Woosh V2A 各自声明隔离的运行时环境、适配器 id 与密封的 Deployment 指纹。Worker 能力路由是应用层关注点；见 ADR 0003 与 ADR 0004。Core 仍不导入 torch、Hugging Face 客户端或任何上游模型包。

## 可观测性与缓存

Profiling 是通用的：它报告 wall/GPU 耗时、资源用量，以及适配器提供的阶段，而不点名模型内部实现。缓存同样通用，报告键、字节用量、命中/未命中状态与失效原因。适配器定义任何语义缓存键，且必须纳入每一个可能改变其结果的因素。缓存内容是优化，绝非任务权威。

## 前端独立性

ComfyUI 是可选集成，有两种形式：将 ComfyUI 值翻译为 Runtime 契约的嵌入式节点，以及调用 API 的远程节点。它不得复制模型执行代码。远程节点既不需要模型权重，也不需要 CUDA。无 ComfyUI 回归套件是强制的：拆除 `integrations/comfyui` 后，Core、CLI、API 与 Worker 测试必须仍能通过。

## 安全与可复现边界

远程客户端永不提交服务器本地路径、Python 模块、源码目录、模型权重、精度标志或任意执行设置。运营方选择不可变部署；Worker 在声明就绪前核验源码与权重指纹。上传文件名仅为元数据，永不构成服务器文件系统路径。产物发布使用不可变的尝试专用键，随后进行校验与隔离最终化。
