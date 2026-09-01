# 执行状态

最后更新：2026-09-01。本文件是执行索引，不是 Gate 权威。阶段定义见 `ROADMAP.md`。`docs/source-plans/` 为归档研究。

| 阶段 | 状态 | 硬件证据 |
|---|---|---|
| P0 架构引导 | 已完成 | 不适用 |
| P1 Runtime Core | 已完成 | 不适用 |
| P2A ControlFoley 基线 | 已完成（非硬件） | RTX 4090 基线 **DEFERRED — 发行阻塞** |
| P2B 适配器 + 本地 CLI | 已完成（非硬件） | RTX 4090 对齐 **DEFERRED — 发行阻塞** |
| P3A 持久化契约/schema | 已完成 | 不适用 |
| P3B 已验证上传 | 已完成 | 不适用 |
| P3C 队列/lease 骨架 | 已完成 | 不适用 |
| P3D GPU Worker 集成 | 已完成（非硬件） | RTX 4090 Worker smoke **DEFERRED — 发行阻塞** |
| P3E 产物/API/远程 CLI | 已完成（非硬件） | RTX 4090 远程 E2E **DEFERRED — 发行阻塞**；本机 Docker daemon **UNRUN** |
| P4A 分阶段路径 | 已完成（非硬件，实验性/非默认） | RTX 4090 对齐 **DEFERRED — 发行阻塞** |
| P4B profiler | 已完成（非硬件，实验性/非默认） | RTX 4090 profile **DEFERRED — 发行阻塞** |
| P4C L0/L1 缓存 | 已完成（非硬件，实验性/非默认） | RTX 4090 缓存等价性 **DEFERRED — 发行阻塞** |
| P4D L2 缓存/基准工具 | 已完成（非硬件，实验性/非默认） | RTX 4090 基准 **DEFERRED — 发行阻塞** |
| P5A ComfyUI Embedded | 已完成（非硬件） | RTX 4090 UI smoke **DEFERRED — 发行阻塞** |
| P5B ComfyUI Remote | 已完成（非硬件） | RTX 4090 UI smoke **DEFERRED — 发行阻塞** |
| P5C ComfyUI 可拆除性 | 已完成 | 不适用 |
| P6 发行加固 | 已阻塞（软件切片已通过；完整 Gate 未跑） | RTX 4090 与真实 ComfyUI **DEFERRED**；Docker daemon **UNRUN** |
| P7A 多适配器冻结 | 已完成 | 不适用 |
| P7B 插件/Worker 路由 | 已完成 | 不适用 |
| P8A Stable Audio 基线 | 已完成（非硬件） | RTX 4090 **DEFERRED** |
| P8B Stable Audio 适配器 | 已完成（非硬件） | RTX 4090 对齐 **DEFERRED** |
| P8C Stable Audio 持久化 | 已完成（非硬件） | RTX 4090 Worker **DEFERRED** |
| P8D Stable Audio 编辑 | 可选，未开始 | RTX 4090 **DEFERRED** |
| P8E Stable Audio profiler/缓存 | 可选，未开始 | RTX 4090 **DEFERRED** |
| P9A Woosh V2A 基线 | 已完成（非硬件） | RTX 4090 **DEFERRED** |
| P9B Woosh V2A 适配器 | 已完成（非硬件） | RTX 4090 对齐 **DEFERRED** |
| P9C Woosh 持久化 | 已完成（非硬件） | RTX 4090 Worker **DEFERRED** |
| P9D Woosh profiler/缓存 | 已完成（非硬件） | RTX 4090 **DEFERRED** |
| P10A 统一工作流 | 已完成（非硬件） | RTX 4090 UI **DEFERRED** |
| P10B 4090 多适配器 | 已阻塞 | RTX 4090 **DEFERRED — 发行阻塞** |
| P11 第二适配器加固 | 已完成（非硬件） | 软件 Gate：Actions [32098048988](https://github.com/dongyuan21/nanoAuralRuntime/actions/runs/32098048988)，PR head `11b250f`。4090 仍为发行阻塞。 |

## 发行阻塞

完整发行仍等待指定宿主上的 RTX 4090 证据、真实 Docker daemon 校验，以及运营方持有的权重。GPU skip 不是通过。软件 Gate 以对应 PR 的 GitHub Actions 为准。

当前主线是 `main`（P11 已合并，PR #14）。Woosh 范围仅为 `Woosh-VFlow-8s` 与 `Woosh-DVFlow-8s`。

## 跨阶段不变量

- Frontend → Workflow → Durable Service/Local Executor → Runtime Core → Adapter → 原始后端。
- Core 不依赖 ControlFoley、Stable Audio 3、Woosh、database/storage/API 或 ComfyUI。
- 三条适配器线使用隔离 Worker 环境；根包无 torch。
- 仅已验证 SHA-256 资产进入任务；PostgreSQL 是持久化状态权威。
- 尝试至少一次，由 lease epoch 围栏；仅一个已验证获胜结果可见。
- 拆除 ComfyUI 不得破坏 Core、CLI、API 或 Worker。
