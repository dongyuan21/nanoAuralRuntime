# 文档

本目录说明 nanoAuralRuntime **做什么、怎么用**。路线图、阶段状态与 Agent 规则在仓库根目录的 `ROADMAP.md`、`plans/`、`AGENTS.md`，不是产品说明。

## 能力与用法

- [统一 SFX 工作流](sfx-workflows.md) — 文本生成、视频生成、生成后封装。
- [第二适配器许可边界](second-adapter-license-boundary.md) — 运营方必须自行解决的上游源码与权重条款。
- [可选 ComfyUI 前端](comfyui-compatibility-removal.md) — Embedded / Remote 如何接到同一套 Runtime，以及如何拆除。
- [架构决策](decisions/0001-model-agnostic-core.md) — 模型无关 Core、首个适配器、隔离 Worker、插件路由。

## 持久化路径

- [持久化运维与恢复](durable-operations.md) — 已验证上传、任务/尝试、Worker、发布与恢复。

## 运营附录

下列文件面向指定宿主上的基线、发行打包或历史研究，**不是**功能说明：

- ControlFoley / Stable Audio 3 / Woosh V2A 的 4090 runbook
- [发行就绪](release-readiness.md)、[发行安全证据](release-security-evidence.md)、[迁移恢复](release-migration-recovery.md)
- [第二适配器研究笔记](stable-audio-3-and-woosh-v2a-research.md)
- [归档研究](source-plans/README.md)
