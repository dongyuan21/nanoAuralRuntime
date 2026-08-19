# ADR 0002：将 ControlFoley 作为第一个适配器，而非系统边界

**状态：** 已接受
**日期：** 2026-08-16

## 背景

ControlFoley 提供了坚实的第一条垂直切片，归档研究文档也指出了有用的运维约束：直接无头调用、可变的上游模型状态、分阶段优化前先做对齐，以及可选的 ComfyUI 集成。但其 API 与内部实现并非所有音频模型普遍共享。

## 决策

ControlFoley 作为第一个 `AudioModelAdapter` 实现，拥有自己的任务 schema、部署清单、兼容层、profiler 阶段与缓存键。其上游执行由适配器直接调用；ComfyUI 不在运行时或 Worker 调用路径上。

第一条实现路径是 `upstream_parity`：在不可变部署下复现受支持的上游行为，并测量自重复包络。分阶段 ControlFoley 路径、适配器特定缓存以及任何优化，仅在对齐得到证明之后进行。因为上游状态可变，初始 ControlFoley `ModelSession` 为单飞。

ComfyUI 仅允许作为可拆除前端：

- 嵌入式节点将 ComfyUI 值翻译为 ControlFoley Runtime 契约；
- 远程节点调用持久化 API，且不导入模型代码、权重或 CUDA 依赖。

## 后果

- 无头本地路径与持久化路径在 ComfyUI 缺失时仍可工作。
- ControlFoley 特定的请求字段永不进入 Runtime Core 契约。
- 项目获得具体的对齐 oracle，同时不声称其 schema 适用于每一个未来模型。
- 4090 基线与性能声称仍延期，直至可复现的硬件验证可用。

## 被否决的替代方案

把官方 ComfyUI 节点当作执行引擎被否决，因为它会让可选 UI 基础设施成为依赖，且不能提供本项目的持久化资产/任务/尝试/产物权威。把第一个 ControlFoley 请求对象当作公开 Core 请求，已由 ADR 0001 否决。
