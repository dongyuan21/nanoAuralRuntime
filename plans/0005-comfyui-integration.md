# 计划 0005 — 可选 ComfyUI 集成（路线图阶段 5A–5C）

状态：阻塞于相关本地 Runtime 与持久化服务阶段。本计划包含独立的路线图阶段 5A–5C，每个恰好对应一个 PR。ComfyUI 是可拆除的前端，绝非模型/任务/产物权威。

## 范围

为两条路径提供薄层可选集成：Embedded 节点调用本地 Runtime/ControlFoley 适配器；Remote 节点使用上传、提交、status/wait 以及已验证产物下载 API。将其与 Core、API、Worker 和 CLI 分开打包。

## 非目标

- Runtime Core、ControlFoley 适配器、API、Worker、持久化 schema 或 Remote 节点模型安装中无 ComfyUI 依赖。
- 不将官方 `nodes.py` 作为 Runtime API 导入，不以 ComfyUI 执行缓存为事实来源，且无独立的任务/产物状态机。
- 无新的模型优化、模型权重分发，或不替代直接无头路径。

## 交付物

### 路线图阶段 5A（一个 PR）— Embedded 节点

薄层节点包装、本地输入/输出映射、取消/错误转换、源模块来源冲突守卫、模型生命周期所有权规则以及示例工作流。它们调用既有本地路径，不吸收 Core 关注点。

### 路线图阶段 5B（一个 PR）— Remote 节点

使用公开客户端/API 的 upload/submit/wait/fetch 节点、进度/状态呈现、短时下载授权以及工作流。Remote 包不含模型依赖。

### 路线图阶段 5C（一个 PR）— 共存与可拆除性加固

官方插件 A/B 工作流检查、模块来源诊断、兼容性文档，以及证明移除 `integrations/comfyui/` 后 Core、CLI、API 与 Worker 检查仍通过的 CI 测试。

## 测试

- 节点参数映射、无效媒体/错误行为、取消以及集成边界以下无 UI 类型的单元测试。
- Embedded 本地调用（条件 GPU）以及使用 Fake Runtime 路径的远程 upload/job/fetch 集成测试。
- 打包/导入测试证明 Remote 不导入 torch/模型代码，且 Core/Service 不导入 ComfyUI。
- 条件 4090 smoke 测试在缺失时跳过；不从未受支持的环境报告通过。

## Gate

- Embedded 与 Remote 路径使用既有 Runtime 与持久化契约，自身不成状态权威。
- 官方插件可共存，或其冲突被检测并以可操作方式拒绝；无静默模块混用。
- 删除/省略集成包后成功运行 Core/CLI/API/Worker 回归检查。
- 4090 UI smoke 证据在指定主机可用前为 **DEFERRED**；不能以成功声称替代。
