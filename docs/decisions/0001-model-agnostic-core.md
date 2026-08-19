# ADR 0001：采用模型无关的 Runtime Core

**状态：** 已接受
**日期：** 2026-08-16

## 背景

归档研究计划包含具体的 ControlFoley 执行形态：视频与参考音频输入、prompt 与 guidance 参数、分阶段编码器、Flow Matching、VAE 与 vocoder。这对第一个适配器是有价值的证据，但若把这些细节当作 Runtime Core 契约，会把后续所有音频模型耦合到 ControlFoley。

## 决策

Core 围绕通用生命周期与执行概念定义：
`AudioModelAdapter`、`ModelDescriptor`、`ModelDeployment`、`ModelSession`、
`ModelInvocation`、`InvocationResult`、`ProducedArtifact`、
`ExecutionContext`、`CancellationToken`、`ProfileReport` 与 `CacheReport`。

`AudioModelAdapter` 仅有三个必需的生命周期操作：`load()`、
`invoke()` 与 `unload()`。能力声明与适配器自有的任务 schema 描述可选输入、输出与生成语义。Core 不会标准化通用 generate 请求，也不会点名特定的条件编码器、采样算法、解码器或输出格式。

依赖顺序保持为：

```text
Frontend -> Workflow -> Durable Service / Local Executor -> Runtime Core
         -> Model Adapter -> Original Model Backend
```

## 后果

- 未来适配器可以支持不同的音频任务，而无需扩大 Core 请求类型。
- 前端与持久化 Worker 共享 Core 契约，但永不拥有模型语义。
- 适配器作者必须提供校验、能力元数据，以及适配器特定的可观测性/缓存语义。
- 通用 Core API 有意不如模型特定请求对象便利；便利性属于适配器 SDK 与工作流。

## 被否决的替代方案

形如 ControlFoley 的公共 `GenerateRequest` 被否决，因为它会把视频、参考音频、prompt、步数、guidance 与模型内部实现硬编码进每一个模型集成。
