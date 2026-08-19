# ADR 0004：适配器插件发现与 Worker 路由

**状态：** 已接受
**日期：** 2026-08-18

## 背景

第一条垂直切片直接接线 ControlFoley：`nano-aural` 指向
`nano_aural_runtime_controlfoley.cli:main`，且
`nano_aural_runtime_workers.controlfoley.ControlFoleyDurableInvocationBuilder`
是仅 ControlFoley 的绑定。第二个适配器不得用通用 generate 请求替换该接线，也不得仅为声明自身存在而导入 torch 或加载 checkpoint。

Stable Audio 3 Small-SFX 与 Woosh V2A 也有不同的公开操作。
Woosh VFlow 与 DVFlow 共享一个操作，不得变成两个 Adapter ID。

## 决策

阶段 7B 引入三种通用、模型中立的机制。它们都不位于 Runtime Core，也都不导入 torch 或上游模型包。

1. **适配器插件元数据。** 每个适配器包发布无 torch 的描述符，含 `adapter_id`、受支持操作与包位置。发现必须在无头 CPU 环境中成功。最初的 ID 为：

   ```text
   controlfoley
   stable-audio-3-small-sfx
   woosh-v2a
   ```

2. **DurableInvocationBuilder 注册表。** 应用层 builder 仍由适配器拥有。注册表按 `adapter_id` 与操作选择 builder。Builder 只接受已验证资产与适配器自有请求字段；它们拒绝源码路径、权重路径、求解器、CFG、re-noise 与后端覆盖。

3. **Worker 能力路由。** Worker 进程声明一个环境、一个适配器、它能运行的操作与后端、密封指纹、设备以及 `max_concurrency`。认领过滤要求与 Deployment 密封精确匹配。ControlFoley 保持单飞。

Woosh 使用一个 Adapter ID 与两个密封的 Deployment 后端：

```text
adapter_id = woosh-v2a
backend_id = dvflow-8s   # default production backend
backend_id = vflow-8s    # selectable reference/quality backend
```

公开 CLI 成为分发器：

```text
nano-aural controlfoley ...
nano-aural stable-audio-3 ...
nano-aural woosh ...
```

既有的 `nano-aural` ControlFoley 路径仍作为兼容入口。
`nano-aural-controlfoley` 可保留为别名。不添加 Woosh T2A、Flow 或
DFlow 子命令。

## 后果

- ControlFoley 保留其任务 schema、builder 与本地 CLI 行为。
- 新适配器增加包、元数据、builder 与 Worker 绑定；它们不编辑 Core 值对象。
- 插件发现可在没有权重或 CUDA 的情况下做单元测试。
- ComfyUI 在后续映射时绑定到相同的适配器 ID 与操作。
  它保持可选且可拆除。

## 被否决的替代方案

Core 层的 `GenerateRequest` 已由 ADR 0001 否决。为 `vflow-8s` 与 `dvflow-8s` 使用分开的 Adapter ID 被否决，因为它们会重复 V2A 任务 schema、视频物化与输出契约。在 CLI `--help` 或 Worker 元数据列举期间导入适配器实现模块被否决，因为这些模块日后可能增长可选原生依赖。
