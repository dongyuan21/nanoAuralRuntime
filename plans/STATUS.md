# 执行状态

最后更新：2026-08-18。`docs/source-plans/` 中的文件为归档研究输入。`plans/0002`–`0006` 为计划文件；其中路线图阶段子项各自独立 Gate，每一项对应一个 PR。

| 阶段 | 当前状态 | 进入条件 | 硬件证据 |
|---|---|---|---|
| P0 架构引导 | 已完成 | 文档 Gate 已通过：权威术语、依赖方向以及一阶段/一 PR 映射保持一致 | 不适用 |
| P1 Runtime Core | 已完成 | 18 项 CPU 测试、Ruff lint/format、Pyright、import-boundary 扫描以及生命周期/single-flight 审查已通过 | 不适用 |
| P2A ControlFoley 基线 | 已完成（非硬件 Gate 已通过） | 源码级溯源将上游 direct runner 锁定为 `large_44k`/FP32；清单、隔离执行、精确绑定、9 项 CPU 测试、Ruff、Pyright 以及 Core 边界检查一致。 | RTX 4090 基线 **DEFERRED — 发行阻塞** |
| P2B 适配器 + 本地 CLI | 已完成（非硬件 Gate 已通过） | 41 项定向 CPU 测试、封印的夹具/部署绑定、执行时溯源再校验、子模块来源守卫、进程组取消、波形比较管线、Ruff、Pyright、Core 边界以及独立 P0/P1 审查已通过 | RTX 4090 对齐 **DEFERRED — 发行阻塞** |
| P3A 持久化契约/schema | 已完成 | 真实 PostgreSQL 16 迁移/仓储 Gate：30 项测试通过；不变量、仅追加、LocalBlobStore、Ruff、Pyright、Core 边界以及独立 P0/P1 审查已通过 | 不适用 |
| P3B 已验证上传 | 已完成 | 真实 PostgreSQL 上传/回收/过期/并发 Gate、流式 SHA 与全帧媒体校验、有界命令探测、Local/S3-compatible 契约、multipart/ETag 分离、规范去重、仅已验证任务、Ruff、Pyright 以及独立 P0/P1 审查已通过 | 不适用 |
| P3C 队列/lease 骨架 | 已完成 | 真实 PostgreSQL 16 全仓库 Gate：115 通过，2 项预期 GPU 跳过；重叠领取/回收器、数据库持有的物化、遗留旁路隔离、Ruff/format、Pyright、迁移镜像以及独立 Core 边界 Gate 全部通过 | 不适用 |
| P3D GPU Worker 集成 | 已完成 | 独立 Gate PASS：真实 PG 与全套验证完成，149 通过，3 项预期条件 GPU 跳过 | RTX 4090 Worker smoke **DEFERRED — 发行阻塞** |
| P3E 产物/API/远程 CLI | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：真实 PostgreSQL 16 全仓库验证完成，329 通过，6 项预期条件 GPU 跳过；发布、API/auth、远程 CLI、有界恢复、Ruff/format、Pyright、迁移镜像以及 Core 边界检查已通过。因未安装容器 CLI，Docker daemon 验证明确为 UNRUN。 | RTX 4090 远程 E2E **DEFERRED — 发行阻塞** |
| P4A 分阶段路径 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：51 项定向测试通过，3 项预期条件 GPU 跳过；分阶段执行由适配器封装、显式、非默认且可审计 | RTX 4090 对齐 **DEFERRED — 发行阻塞** |
| P4B profiler | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：封印的执行溯源、如实的 CPU/CUDA 能力报告、失败隔离、15 项 profiler 测试含 1 项预期 GPU 跳过、组合回归、Ruff/format 以及 Pyright 已通过 | RTX 4090 profile 验证 **DEFERRED — 发行阻塞** |
| P4C L0/L1 缓存 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：显式默认关闭封印、完整语义键、输入 TOCTOU 围栏、延迟提交、有界线程安全 LRU、损坏/故障冷回退、失败即关闭失效、如实 CacheReport、19 项测试含 1 项预期 GPU 跳过、Ruff/format 以及 Pyright 已通过 | RTX 4090 缓存等价性 **DEFERRED — 发行阻塞** |
| P4D L2 缓存/基准工具 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：持久失败即关闭 L2 特征存储、严格仅数据 safetensors 校验、源码/权重隔离、如实编码器/投影计数器、单驻留封印基准工具、30 项定向测试含 1 项预期 GPU 跳过、组合回归、Ruff/format 以及 Pyright 已通过 | RTX 4090 基准 **DEFERRED — 发行阻塞** |
| P5A ComfyUI Embedded | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：生命周期/拆除、失败即关闭取消、严格运营方引导、主机发现的 producer/output 工作流、可拆除性、Ruff/format、Pyright 以及 34 项定向测试含 2 项预期 GPU 跳过已通过 | RTX 4090 UI smoke **DEFERRED — 发行阻塞** |
| P5B ComfyUI Remote | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：公开 RemoteClient/仅 API 节点、零/单/多输入流、有界等待/取消、严格响应 schema、脱敏完整异常链、授权且校验 checksum 的下载、链接的 OUTPUT 工作流、Ruff/format、Pyright 以及 17 项测试含 1 项预期 GPU 跳过已通过 | RTX 4090 UI smoke **DEFERRED — 发行阻塞** |
| P5C ComfyUI 可拆除性 | 已完成 | 独立 Gate PASS：当前 official/Embedded/Remote 节点映射共存且无冲突，三个链接的 v0.4 OUTPUT 工作流校验通过，来源冲突失败即关闭，三个仓库外物理省略矩阵在无头 Core/CLI/API/Worker 路径上执行，且 Ruff/format/Pyright 通过 | 不适用 |
| P6 发行加固 | 已阻塞（非硬件加固已通过；完整 Gate 未运行） | 独立软件切片已通过：确定性打包、license/notices、checksum 封印的迁移与真实 PostgreSQL 16 dump/restore、最小权限运行时角色、发行 canary、严格产物/安全证据、digest 钉死的参考镜像以及哈希锁定的 API 依赖。最终冻结树验证：519 通过，9 项显式 GPU 跳过；Ruff/format、Pyright、迁移镜像、全新 wheel 安装、外部 SPDX 校验以及当前 API-lock 漏洞审计已通过。这并不完成该路线图阶段。 | 延期的 RTX 4090 与真实 Embedded/Remote ComfyUI 证据仍为发行阻塞；本机上 Docker daemon 构建/启动/重启证据为 **UNRUN** |
| P7A 多适配器冻结 | 已完成 | 文档 Gate 已通过：Stable Audio 3 = 仅 T2SFX；Woosh = 带 `dvflow-8s`/`vflow-8s` 的 V2SFX；无 Woosh T2A/Flow/DFlow；隔离的 Worker 环境；插件/路由 ADR；Core 未改动；无生产 Python | 不适用 |
| P7B 插件/Worker 路由 | 已完成 | 独立 Gate PASS：无 torch 的插件目录、DurableInvocationBuilder 注册表、Worker 能力匹配、通用 nano-aural 分发器、ControlFoley 兼容性以及 Core 边界检查 | 不适用 |
| P8A Stable Audio 基线 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：Small-SFX 溯源锁定、5s/30s/120s 夹具、待定权重指纹、干净跳过的 GPU 诊断，以及 CI 中无门控下载 | RTX 4090 **DEFERRED** |
| P8B Stable Audio 适配器 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：audio.text_to_sfx 本地适配器、WAV 44.1k stereo 契约、注入 runner 的 CPU 测试、无源码时官方 runner 失败即关闭、CLI 分发器 | RTX 4090 对齐 **DEFERRED** |
| P8C Stable Audio 持久化 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：text-to-sfx DurableInvocationBuilder、无二进制输入、运营方字段拒绝、注册表登记 | RTX 4090 Worker **DEFERRED** |
| P8D Stable Audio 编辑 | 可选，未开始 | 进入条件：P8B；不 Gate Woosh | RTX 4090 **DEFERRED** |
| P8E Stable Audio profiler/缓存 | 可选，未开始 | 进入条件：P8B；不 Gate Woosh | RTX 4090 **DEFERRED** |
| P9A Woosh V2A 基线 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：VFlow/DVFlow 溯源锁定于 `v1.0.0`/`f6ff658`、范围内归档 SHA-256、8s 窗口夹具、待定 inner/Synchformer 指纹、干净跳过的 GPU 诊断；无 Flow/DFlow/T2A | RTX 4090 **DEFERRED** |
| P9B Woosh V2A 适配器 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：woosh-v2a 本地适配器、audio.video_to_sfx、dvflow-8s 默认、vflow-8s 可选、8s 失败即关闭窗口、48k mono WAV、CLI 分发器 | RTX 4090 对齐 **DEFERRED** |
| P9C Woosh 持久化 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：WooshV2ADurableInvocationBuilder、必需 video 角色、可选 prompt、8s 媒体失败即关闭、solver/CFG 拒绝、注册表登记 | RTX 4090 Worker **DEFERRED** |
| P9D Woosh profiler/缓存 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：默认关闭的阶段 profiler、仅特征缓存类型、禁止 ODE/latent/step/cross-seed 键、后端特定采样器阶段 | RTX 4090 **DEFERRED** |
| P10A 统一工作流 | 已完成（非硬件 Gate 已通过） | 独立 Gate PASS：sfx.text_generate / sfx.video_generate / sfx.generate_and_mux 目录，mux 不是适配器步骤，可选 ComfyUI 映射且无 Woosh T2A 节点 | RTX 4090 UI **DEFERRED** |
| P10B 4090 多适配器 | 已阻塞 | 硬件主机不可用 | RTX 4090 **DEFERRED — 发行阻塞** |
| P11 第二适配器加固 | 已完成（非硬件 Gate 已通过） | 独立软件 Gate PASS，Actions 运行 [32098048988](https://github.com/dongyuan21/nanoAuralRuntime/actions/runs/32098048988)，PR head `11b250f`，merge-ref `8bfe3ff`：Ruff format/lint 清洁；Pyright 0 错误；CPU Python 3.12 `pytest -m 'not gpu'` 523 通过，60 跳过（CPU job 未安装 PG extras），13 项 GPU 取消选择；PostgreSQL 16 job 87 通过；静态发行/边界 78 通过，2 项 GPU 跳过。包含心跳后取消探测中未知异常的失败即关闭分类。合并的 P6/P10B 发行仍阻塞。 | 延期的 4090 系列证据仍为发行阻塞；合并的 P6/P10B 发行 Gate 在获得硬件 + Docker 证据前仍阻塞 |

## 当前指令

遵循 `main`。P11 软件 Gate 已独立 PASS 并合并（PR #14，`a974d8e`）。完整发行 Gate 仍因 RTX 4090、Docker daemon 以及运营方持有的权重/封印 GPU 证据而阻塞。仅硬件 Gate 为延期，既未失败也未通过。Agent 不得伪造测量、将跳过的测试标为通过、声称对齐或声称性能提升。此执行豁免并不豁免 ControlFoley 发行 Gate：在延期的硬件证据实际记录之前，P6 仍阻塞。

Woosh 范围严格为 `Woosh-VFlow-8s` 与 `Woosh-DVFlow-8s`。不要实现 Woosh-Flow、Woosh-DFlow、TextConditionerA 或任何 Woosh text-to-audio 路径。

## 必需的 4090 后续工作

在指定主机上设置 `CONTROLFOLEY_SOURCE_DIR`、`CONTROLFOLEY_WEIGHTS_DIR` 和 `HF_HOME`，然后运行已文档化的 `pytest -m "gpu and controlfoley" -v` 套件以及各已实现阶段提供的基线/对齐/远程命令。阶段 8A/9A 落地后，还须在隔离环境中运行这些 runbook 中的 Stable Audio 与 Woosh V2A 命令。仅记录已脱敏的清单与结果摘要——绝不记录权重、缓存、用户媒体、私有路径、token 或主机身份。

## 跨阶段不变量

- Frontend → Workflow → Durable Service/Local Executor → Runtime Core → Adapter → 原始后端。
- Core 不依赖 ControlFoley、Stable Audio 3、Woosh、database/storage/API 或 ComfyUI。
- ControlFoley、Stable Audio 3 与 Woosh V2A 使用隔离的 Worker 环境；根包无 torch 依赖。
- 仅已验证的 SHA-256 资产进入任务；PostgreSQL 是持久化状态权威。
- 尝试为至少一次，由单调 lease epoch 围栏；仅一个已验证产物结果可见。
- 拆除 ComfyUI 不得破坏 Core、CLI、API 或 Worker。
- 本仓库不存在 Woosh T2A、Woosh-Flow 或 Woosh-DFlow 生产路径。
