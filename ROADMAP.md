# nanoAuralRuntime 路线图

## 交付规则与分类

一个交付阶段恰好对应一个 PR。阶段只有在其全部测试通过、且该 PR 中记录 Gate 已通过后才算完成。失败或未跑的 Gate 会阻塞所有依赖阶段；必须在同一阶段内修复、收窄，或明确重排。不要把多个阶段合并进同一个 PR。

`docs/source-plans/` 中的源计划仅为归档研究输入。`plans/0001-runtime-core.md` 到 `plans/0006-stable-audio-3-and-woosh-v2a.md` 是计划文件，不是单 PR 阶段定义。其带字母切片与下列交付阶段一一对应：

| 计划切片 | 交付阶段 / 一个 PR |
| --- | --- |
| `0001` | 阶段 1 |
| `0002-A`、`0002-B` | 阶段 2A、2B |
| `0003-A` 至 `0003-E` | 阶段 3A 至 3E |
| `0004-A` 至 `0004-D` | 阶段 4A 至 4D |
| `0005-A` 至 `0005-C` | 阶段 5A 至 5C |
| `0006-7A`、`0006-7B` | 阶段 7A、7B |
| `0006-8A` 至 `0006-8E` | 阶段 8A 至 8E |
| `0006-9A` 至 `0006-9D` | 阶段 9A 至 9D |
| `0006-10A`、`0006-10B` | 阶段 10A、10B |
| `0006-11` | 阶段 11 |

阶段 0 与阶段 6 同样各对应一个 PR。`plans/STATUS.md` 报告这些交付阶段，但不能替代其 Gate。下列阶段有意用 `ARCHITECTURE.md` 中的模型无关架构，替换研究计划里以 ControlFoley 为形态的 Core。阶段 7A–11 加入 Stable Audio 3 Small-SFX 与 Woosh V2A，且不扩大该 Core。阶段 6 的硬件延期不阻塞独立的阶段 7A–9C 软件切片。

## 阶段 0 — 架构引导

**范围：** 确立项目章程、架构、ADR、路线图与 agent 交付规则。

**非目标：** 生产 Python、上游检出、模型执行、API、数据库模式、CUDA 工作与基准。

**交付物：** 权威引导文档，以及归档研究的指定说明。

**测试：** 术语审查：Core 术语保持模型中立；ControlFoley 术语仅出现在适配器特定语境；所有阶段声明必填字段。

**Gate：** 文档就固定分层、前端独立性，以及一阶段/一 PR 规则达成一致。

## 阶段 1 — 模型无关 Runtime Core

**范围：** 实现契约、适配器生命周期、不可变部署、会话状态、执行上下文、取消、通用结果/产物报告、零细节 `ProfileReport`/`CacheReport` 值契约，以及本地执行器测试装置。

**非目标：** ControlFoley 实现、持久化服务、数据库、ComfyUI、性能优化，以及模型特定请求字段。

**交付物：** 经测试的 Core 包与使用假适配器的契约夹具；能力协商、生命周期文档，以及稳定的空或由适配器提供的 profile/cache 报告类型（不是 profiler 或 cache 引擎）。

**测试：** load/invoke/unload 生命周期；故障处理；取消；single-flight 强制；Core 导入边界测试；假适配器契约测试；稳定的 profile/cache 报告契约测试。

**Gate：** 非生成型假适配器与生成形态假适配器均通过同一 Core 运行，且不向 Core 添加模型特定字段。

## 阶段 2A — ControlFoley 上游基线夹具

**范围：** 锁定受支持的上游源码、依赖、权重、夹具与基线运行元数据；文档化可复现的上游对齐夹具。

**非目标：** 实现适配器或 CLI、更改上游算法、持久化服务、ComfyUI、缓存、优化，或声称测量结果。

**交付物：** 源码/权重/依赖清单、脱敏夹具与环境清单、自重复比较模式，以及 4090 runbook。

**测试：** 清单/模式校验、缺失源码/权重诊断，以及在前置条件缺失时将依赖 CUDA/源码/权重的测试标为 skip 的测试收集。

**Gate：** 溯源与 runbook 完整，未提交禁止产物，且非 GPU 检查通过。真实 4090 基线记为 `DEFERRED`，永不记为已通过；它不阻塞独立的阶段 3 工作。

## 阶段 2B — ControlFoley 适配器与本地 CLI

**范围：** 在阶段 1 Core 上实现 ControlFoley 自有的任务模式、`upstream_parity` 适配器边界、兼容性/源码守卫，以及本地 CLI。

**非目标：** 分阶段执行、cache、profiler、更改上游语义、持久化服务、ComfyUI，或性能声称。

**交付物：** 适配器包、任务模式/工作流、部署/源码校验、兼容性垫片、本地 CLI，以及条件 GPU 测试命令。

**测试：** CPU 任务/配置校验、源码来源冲突、取消契约、CLI 序列化，以及 Core 隔离测试；GPU 冒烟与对齐测试在其声明的前置条件缺失时 skip。

**Gate：** 阶段 2A 与全部可用 CPU 检查通过；Core 保持模型中立且不含 ComfyUI。GPU 冒烟/对齐为 `DEFERRED`，不是已通过。这仅启用下列明确列出的依赖，绝不构成对齐或性能声称。

## 阶段 3A — 持久化契约、模式与本地存储

**范围：** 定义并实现已验证资产、任务、尝试、产物与部署的持久化；本地 blob 存储；假运行时 Worker；幂等性；以及状态转换守卫。

**非目标：** 真实 GPU 推理、公开 API 暴露、S3 多段上传，或 ComfyUI。

**交付物：** 迁移、仓储、状态机、假 Worker 集成环境，以及不变量测试。

**测试：** 只有已验证资产可进入任务；重复幂等行为；并发认领互斥；状态机/属性测试；假端到端成功、终态失败、重试与取消。

**Gate：** 阶段 1 与全部本地数据库测试通过；约束证明任何任务不能有两个合法的当前尝试或两个可见获胜者。本阶段仅依赖阶段 1，不依赖任何 ControlFoley 或 GPU 校验。

## 阶段 3B — 已验证上传与内容寻址资产

**范围：** 实现上传会话、直接上传契约、流式全文件 SHA-256 校验、媒体探测、规范提升、去重，以及暂存清理。

**非目标：** GPU 执行、Worker 隔离、将 ETag 当作身份，或 ComfyUI。

**交付物：** 校验器、BlobStore 契约、上传 CLI 状态、规范键策略，以及 janitor 行为。

**测试：** 错误 SHA 拒绝、多段身份分离、仅已验证资产守卫、去重，以及 Local/S3 兼容契约测试。

**Gate：** 阶段 3A 通过，且只有经全文件 SHA-256 验证的资产可进入任务。

## 阶段 3C — 队列、租约与假 Worker 执行

**范围：** 加入事务性认领、单调租约世代、心跳 CAS、回收、重试/退避、取消、输入物化，以及假 Worker 执行。

**非目标：** 真实模型调用、ControlFoley 导入、GPU 要求，或产物发布最终化。

**交付物：** 队列/租约仓储、Worker 骨架、恢复策略，以及假运行时集成环境。

**测试：** 并发认领、心跳/回收器、过期 Worker 拒绝、取消竞态、输入再校验，以及使用假适配器的进程故障恢复。

**Gate：** 阶段 3B 通过；两个 Worker 不能同时成为合法执行者，且过期 Worker 不能心跳或最终化。

## 阶段 3D — 带围栏的 GPU Worker 集成

**范围：** 将持久化 Worker 连接到 `Runtime.invoke()` 与阶段 2B 适配器，具备单进程/会话 single-flight 行为与按尝试的工作区。

**非目标：** ComfyUI、恰好一次推理、批处理，或未经测量的 GPU 声称。

**交付物：** 直接无头 Worker 集成、进程故障策略，以及条件 GPU 冒烟/恢复命令。

**测试：** 无 ComfyUI 导入边界、假/CPU Worker 集成，以及在缺少声明的宿主、源码、权重与夹具时 skip 的条件 ControlFoley Worker 测试。

**Gate：** 阶段 3C 与 2B 通过。Worker 直接调用 Runtime，且其非 GPU 集成测试通过。4090 冒烟/恢复证据仍为 `DEFERRED`，不是已通过。

## 阶段 3E — 已验证产物、API 与远程 CLI

**范围：** 加入输出校验器、不可变尝试发布、最终化 CAS、孤儿处理、授权下载、任务 API/远程 CLI、指标，以及恢复 runbook。

**非目标：** 将一次成功的模型调用当作任务成功的证明、ComfyUI 状态权威，或不被支持的可靠性声称。

**交付物：** 产物/API/客户端路径、结构化可观测性、Compose 参考环境，以及恢复文档。

**测试：** 校验器失败、发布边界崩溃、取消/最终化竞态、获胜结果唯一性、下载 SHA 校验，以及 CPU 假端到端恢复。

**Gate：** 阶段 3D 通过；恰好一个已验证获胜者可见，且 `SUCCEEDED` 意味着所需 READY 产物。完整 4090 远程 E2E 在指定校验宿主可用前仍为 `DEFERRED`。

## 阶段 4A — 实验性分阶段对齐路径

**范围：** 在适配器之后拆分 ControlFoley 特定阶段，同时保留 `upstream_parity` 作为选定的默认/oracle。

**非目标：** 默认分阶段执行、对齐声称、求解器/默认值更改，或性能声称。

**交付物：** 显式实验性分阶段路径与比较管线。

**测试：** 阶段契约测试与比较管线；任何 GPU 比较在其声明的前置条件缺失时 skip。

**Gate：** 阶段 2B 通过，且分阶段路径封闭在适配器内、可选择、且非默认。已测量对齐为 `DEFERRED`；其缺失阻塞默认启用，但不阻塞实验性实现。

## 阶段 4B — 实验性 profiler

**范围：** 加入通用 profile 报告与 ControlFoley 适配器阶段，不含合成 CUDA 计时。

**非目标：** 性能声称、在 CPU 上推断 GPU 测量、cache，或默认路径更改。

**交付物：** profile 级别、阶段模式、CPU 计时、条件 CUDA 事件计时，以及如实报告不可用能力。

**测试：** CPU profile 有效性、不可用 CUDA 字段，以及阶段记账测试。

**Gate：** 阶段 4A 通过，且观测不改变语义。4090 计时证据为 `DEFERRED`；它仅阻塞性能声称。

## 阶段 4C — 实验性 L0/L1 cache

**范围：** 加入有界元数据与确定性预处理 cache，以及通用 cache 报告与失效。

**非目标：** L2 功能、采样/轨迹 cache、质量变化，或性能声称。

**交付物：** L0/L1 cache 策略、有界存储、失效/版本、指标，以及 cache 文档。

**测试：** cache 键变更、损坏/未命中、驱逐/限额、失效，以及 cache 开/关语义等价。

**Gate：** 阶段 4B 通过；cache 保持结果不变，并在延期对齐证据允许默认路径决策之前保持实验性。

## 阶段 4D — 实验性 L2 条件 cache 与基准工具

**范围：** 加入适配器自有的视频/参考/文本特征 cache 与可复现基准工具。

**非目标：** 默认使用 L2、加速声称、step/latent cache，或改变质量的复用。

**交付物：** 安全磁盘持久化、特征 cache 生命周期/指标、编码器计数器，以及基准矩阵工具。

**测试：** L2 键/失效/损坏测试、编码器计数器测试，以及条件热/冷 GPU 基准命令。

**Gate：** 阶段 4C 通过；L2 cache 在可用测试中保持结果不变，且保持非默认。真实对齐与基准证据仍为 `DEFERRED`。

## 阶段 5A — 可选嵌入式 ComfyUI 前端

**范围：** 加入将 ComfyUI 值翻译到既有本地 Runtime/ControlFoley 契约的薄嵌入节点。

**非目标：** 将 ComfyUI 置于集成边界之下、重复执行代码、UI 状态权威，或远程服务功能。

**交付物：** 嵌入节点、映射/取消转换、来源冲突守卫、生命周期规则，以及示例工作流。

**测试：** 节点映射/错误/取消、边界导入测试，以及条件本地 GPU 冒烟测试。

**Gate：** 阶段 2B 通过；该集成可拆除，且不扩大 Core 契约。硬件不可用时 UI GPU 冒烟为 `DEFERRED`。

## 阶段 5B — 可选远程 ComfyUI 前端

**范围：** 使用公开客户端与持久化 API 加入远程上传/提交/状态/获取节点。

**非目标：** 本地模型依赖、远程包中的 CUDA，或新的任务/产物状态机。

**交付物：** 远程节点、进度展示、下载授权，以及示例工作流。

**测试：** 假运行时远程集成、包/导入隔离，以及条件 GPU UI 冒烟测试。

**Gate：** 阶段 3E 通过；远程节点不需要模型依赖，并使用既有持久化权威。硬件 UI 校验仍为 `DEFERRED`。

## 阶段 5C — ComfyUI 共存与可拆除加固

**范围：** 校验官方插件共存/冲突处理，以及可选集成的拆除。

**非目标：** 使插件成为依赖，或放宽无头测试。

**交付物：** A/B 检查、诊断、兼容性文档，以及无 ComfyUI 的 CI 覆盖。

**测试：** 官方插件冲突行为，以及对 Core、CLI、API 与 Worker 的删除/省略回归检查。

**Gate：** 阶段 5A 与 5B 通过；删除该集成后全部无头检查仍通过。

## 阶段 6 — 发行加固

**范围：** 执行安全、运维、迁移、许可、基准与发行就绪工作。

**非目标：** 静默放宽先前 Gate，或引入新的执行模型。

**交付物：** runbook、SBOM/依赖证据、模型许可声明、脱敏可复现基准证据，以及发行说明。

**测试：** 备份/恢复、迁移兼容性、安全检查、完整恢复矩阵、发行冒烟测试，以及延期硬件套件。

**Gate：** 每个依赖 Gate 均为通过；延期的 ControlFoley GPU 对齐与适用硬件基准证据已完成；任何发行声称均不超过证据。

## 阶段 7A — 多适配器架构冻结

**范围：** 仅在文档中冻结第二适配器计划：Stable Audio 3 Small-SFX 作为 text-to-SFX，Woosh 仅作 V2A，使用 `dvflow-8s` / `vflow-8s` Deployment，隔离的 Worker 环境，以及插件/路由 ADR。

**非目标：** 生产 Python、新适配器、根包中的 torch、Woosh T2A/Flow/DFlow、Core 契约变更、权重，以及硬件声称。

**交付物：** `plans/0006-stable-audio-3-and-woosh-v2a.md`、ADR 0003、ADR 0004、路线图/状态更新，以及源码/依赖/许可研究说明。

**测试：** 术语审查。Core 术语保持模型中立。Woosh T2A 术语仅作为明确范围外出现。一阶段/一 PR 映射完整。

**Gate：** 文档就产品拆分、环境隔离、Woosh 仅 V2A 范围、DVFlow 默认、VFlow 可选、8 秒失败即关闭窗口，以及本 PR 不含生产 Python 达成一致。本阶段依赖阶段 1，不等待阶段 6 硬件证据。

## 阶段 7B — 插件、builder 注册表与 Worker 路由

**范围：** 实现无 torch 的适配器插件元数据、`DurableInvocationBuilder` 注册表、Worker 能力描述符、感知部署的认领过滤，以及带 ControlFoley 兼容性的通用 `nano-aural` 调度器。

**非目标：** Stable Audio 或 Woosh 模型代码、Core 字段新增、根包上的 torch，以及 ComfyUI 变更。

**交付物：** 插件元数据、builder 注册表、能力路由、CLI 调度器，以及 ControlFoley 回归覆盖。

**测试：** 无 torch 发现；ControlFoley `--help` 与本地 CLI 路径；builder 拒绝任务上的仅运营方字段；能力不匹配失败即关闭；Core 导入边界扫描。

**Gate：** 阶段 7A 通过。没有新模型后端落地。ControlFoley 仍是唯一可执行适配器。

## 阶段 8A — Stable Audio 3 基线与溯源

**范围：** 通过直接 runner 锁定官方 Small-SFX 源码、门控 HF 身份、依赖，以及 5s / 30s / 120s 基线配置。

**非目标：** 生产适配器、持久化 Worker、编辑模式、LoRA、优化，或性能声称。

**交付物：** 清单、准备 runbook、自重复模式，以及条件 GPU 收集。

**测试：** 模式校验、缺失源码/权重诊断、门控访问诊断，以及干净 skip 的 GPU 测试。

**Gate：** 阶段 7A 通过。溯源完整，未提交禁止产物。真实 4090 证据为 `DEFERRED`。

## 阶段 8B — Stable Audio 3 本地适配器与 CLI

**范围：** 在阶段 1 Core 上实现 `audio.text_to_sfx`，带隔离会话、本地 CLI，以及 44.1 kHz 立体声 WAV 校验。

**非目标：** A2A/inpaint/continuation、持久化 Worker、cache、ComfyUI，以及未经测量的速度声称。

**交付物：** `nano_aural_runtime_stable_audio_3`、密封部署、本地 CLI，以及条件 GPU 测试。

**测试：** CPU 请求/部署校验、源码/权重不匹配失败即关闭、CLI 序列化、Core 隔离；GPU 冒烟在缺失时 skip。

**Gate：** 阶段 7B 与 8A 通过。Core 保持模型中立。GPU 对齐为 `DEFERRED`。

## 阶段 8C — Stable Audio 3 持久化 Worker

**范围：** 通过阶段 7B 注册表将持久化 Worker 连接到 Small-SFX 适配器，带隔离环境与已验证发布。

**非目标：** 编辑模式、ComfyUI、批处理，以及将模型调用当作任务成功。

**交付物：** invocation builder、环境锁、能力路由、产物校验、远程 CLI，以及恢复测试。

**测试：** 假/CPU Worker 集成、密封字段拒绝、发布唯一性，以及在缺少前置条件时 skip 的条件 GPU Worker 测试。

**Gate：** 阶段 8B 与 3E 通过。非 GPU 集成测试通过。4090 Worker 证据仍为 `DEFERRED`。

## 阶段 8D — Stable Audio 3 编辑扩展（可选）

**范围：** 适配器自有的 `audio_to_audio`、inpaint 与 continuation。

**非目标：** 阻塞 Woosh V2A、Core 变更，以及默认路径编辑。

**Gate：** 阶段 8B 通过。本阶段可选，且不门控 9A–9C。

## 阶段 8E — Stable Audio 3 profiler 与 cache（可选）

**范围：** 实验性、默认关闭的 profile 阶段与文本条件 cache。

**非目标：** 阻塞 Woosh V2A、语义变更，以及性能声称。

**Gate：** 阶段 8B 通过。4090 cache 证据为 `DEFERRED`。本阶段可选，且不门控 9A–9C。

## 阶段 9A — Woosh VFlow/DVFlow 基线与溯源

**范围：** 锁定 SonyResearch/Woosh `v1.0.0`（或经审查的后续钉死版本）、Woosh-AE、TextConditionerV、Woosh-VFlow-8s、Woosh-DVFlow-8s，以及 MMAudio Synchformer checkpoint。面向 8 秒窗口的官方直接运行夹具。

**非目标：** 检查或适配 Woosh-Flow、Woosh-DFlow、TextConditionerA，或任何 T2A 路径；生产适配器；持久化 Worker；profiler/cache。

**交付物：** 源码/发行/归档清单、8 秒夹具契约、可选提示与同 seed 比较模式，以及 4090 runbook。

**测试：** 清单校验、缺失源码/权重/Synchformer 诊断，以及干净 skip 的 GPU 收集。不要提交权重或媒体。

**Gate：** 阶段 7A 通过。溯源完整。GPU 证据为 `DEFERRED`。

## 阶段 9B — Woosh V2A 本地适配器与 CLI

**范围：** 一个 `woosh-v2a` 适配器，操作 `audio.video_to_sfx`，密封后端 `dvflow-8s`（默认）与 `vflow-8s`，从 0 起的 8 秒窗口，拒绝更短视频，48 kHz 单声道 WAV，不 mux。

**非目标：** Woosh T2A、Flow/DFlow、在 `ModelInvocation` 上暴露求解器/CFG/renoise、Core 字段、持久化 Worker、cache，以及 ComfyUI。

**交付物：** `nano_aural_runtime_woosh`、本地 CLI、溯源再校验，以及条件 GPU 测试。

**测试：** CPU 模式与窗口策略、后端选择、源码/checkpoint/Synchformer 不匹配失败即关闭、Core 隔离；GPU 对齐在缺失时 skip。

**Gate：** 阶段 7B 与 9A 通过。Core 保持模型中立。GPU 对齐为 `DEFERRED`。

## 阶段 9C — Woosh 持久化 Worker

**范围：** Woosh V2A invocation builder、隔离环境、能力路由、已验证视频物化、Runtime invoke、发布与恢复。

**非目标：** T2A、任务上的求解器覆盖、ComfyUI，以及适配器内 mux。

**交付物：** Worker 绑定、环境锁、远程 CLI，以及恢复测试。

**测试：** 假/CPU 集成、视频角色要求、密封字段拒绝、获胜结果唯一性，以及在缺少前置条件时 skip 的条件 GPU 测试。

**Gate：** 阶段 9B 与 3E 通过。非 GPU 集成测试通过。4090 Worker 证据仍为 `DEFERRED`。

## 阶段 9D — Woosh profiler 与 cache

**范围：** 实验性、默认关闭的阶段与 cache，面向视频预处理、Synchformer 特征、文本 token，以及空/无条件条件。

**非目标：** ODE 轨迹 cache、latent 复用、跳步、跨 seed 复用、默认启用，以及性能声称。

**Gate：** 阶段 9B 通过。cache 保持结果不变且非默认。4090 证据为 `DEFERRED`。

## 阶段 10A — 统一 SFX 工作流与可选 ComfyUI 映射

**范围：** 工作流 `sfx.text_generate`（Stable Audio 3 Small-SFX）与 `sfx.video_generate`（ControlFoley、Woosh-DVFlow-8s、Woosh-VFlow-8s），外加明确的 `sfx.generate_and_mux`，后者不是模型适配器步骤。

**非目标：** 使 ComfyUI 成为任务权威、重复执行代码，以及加入 Woosh T2A 节点。

**Gate：** 阶段 8B、9B 与 5C 通过。集成保持可拆除。

## 阶段 10B — RTX 4090 多适配器证据

**范围：** 在指定宿主上记录脱敏的 Stable Audio 5s/30s/120s、Woosh DVFlow/VFlow 8s，以及既有 ControlFoley 工作负载。

**非目标：** 编造测量、把 skip 的测试标为已通过，或声称加速。

**Gate：** 阶段 8B 与 9B 通过。本 Gate 在宿主可用前保持 `DEFERRED`，且不阻塞独立软件切片。

## 阶段 11 — 第二适配器发行加固

**范围：** Stability Community License 与 Gemma 声明、门控 HF token 处理、Woosh MIT/Apache-2.0 与 CC BY-NC 权重声明、Synchformer/MMAudio 署名，以及新家族的发行证据。

**非目标：** 静默放宽先前 Gate，或在 ControlFoley 阶段 6 硬件证据仍延期时声称组合产品发行。

**Gate：** 7A–9C 的软件切片均为通过；新家族的硬件证据已记录或明确延期；任何发行声称均不超过证据。
