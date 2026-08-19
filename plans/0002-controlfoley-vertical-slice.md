# 计划 0002 — ControlFoley 垂直切片（路线图阶段 2A–2B）

状态：阶段 1 之后即可开始。本计划包含两个独立的路线图阶段 2A 与 2B；每个路线图阶段恰好对应一个 PR。ControlFoley 是适配器/工作流，绝非 Core 抽象。

支持的上游源基线为 `xiaomi-research/controlfoley` revision `6858cd12a48d141201e3266e7abe1f38357a133e`。上游直接路径是对齐权威。Runtime 执行不得经由 ComfyUI。

## 范围

实现可复现的官方基线 harness，然后实现 `upstream_parity` ControlFoley 适配器、仅 ControlFoley 的任务模式、源码/权重/依赖清单、兼容性检查以及本地 CLI。在上游能力与夹具可用时覆盖 V2A、TV2A、TC-V2A、AC-V2A 和 T2A 配置。模型会话保持一次一个 Worker 进程 / 一个部署 / 一次调用。

## 非目标

- 无分阶段管线、特征缓存、profiler、编译调优、减少 steps、solver 变更、CUDA/Triton/TensorRT/ONNX 或性能声称。
- 无 API、数据库、对象存储、持久化 Worker、远程 CLI 或 ComfyUI 节点。
- 无上游编辑，不将权重/HF 缓存/媒体夹具/私有路径提交到仓库。
- 在实际于该主机运行之前，不断言 GPU smoke/对齐或 4090 基准已通过。

## 交付物

### 路线图阶段 2A（一个 PR）— 上游基线 harness

- Direct runner 调用上游实现；它不是正式适配器。
- 配置/夹具清单捕获上游 revision、checkpoint 与输入指纹、任务、seed、precision、steps、guidance、波形 shape/rate、时序、峰值 allocated/reserved VRAM 以及脱敏环境清单。
- 自重复比较/报告：shape、有限样本、peak/RMS、MAE、最大误差、余弦相似度以及 mel 距离。阈值来自测量记录，绝不臆造。
- 清晰的 4090 runbook；GPU 测试带有 `gpu` 与 `controlfoley` 标记，在缺少 CUDA、源码、权重或夹具时干净跳过。

### 路线图阶段 2B（一个 PR）— 适配器与本地 CLI

- `ControlFoleyAdapter`、其任务模式/工作流、部署校验、源模块来源守卫、checkpoint 指纹校验、兼容 precision/device 检查，以及窄范围上游兼容 shim。
- `upstream_parity` 直接调用原始模型，在适配器边界支持取消，并产生 Runtime 结果与清单。
- 本地 CLI 在 local 模式下仅物化用户本地输入并调用通用 Runtime；不扩展 Core 类型。
- CPU 配置/校验测试加上条件 GPU smoke/对齐测试。

## 测试

- CPU：清单与任务校验、源来源冲突检测、缺失/无效源与 checkpoint 处理、CLI 序列化以及 Core 隔离回归测试。
- 条件 GPU：对支持的任务夹具进行直接上游基线与适配器输出比较；load/invoke/unload/reload；取消；无效参考媒体。
- 4090 命令与预期结果位置已文档化。在缺少所需 CUDA/源码/权重的主机上，pytest 报告 **skipped**，而非通过。

## Gate

- 路线图阶段 2A：基线元数据完整，且未提交禁止的产物。实际 4090 基线 Gate 按**明确项目指示延期**，直至指定主机可用；记录为 `DEFERRED`，绝不记为“已通过”。
- 路线图阶段 2B：Core 保持模型无关且无 ComfyUI；适配器仅使用 `upstream_parity`；CPU 校验通过。其 4090 对齐/smoke Gate 同样**仅因所需主机有意不可用而延期/跳过**，精确命令保留在 `STATUS.md`。
- 延期的硬件 Gate 不授权伪造测量或速度/对齐声称。它们是未完成的发行证据；后续非硬件开发可在用户授权的延期下继续。
