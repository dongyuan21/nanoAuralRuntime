# nanoAuralRuntime 的 Agent 说明

## 权威与阅读顺序

改代码或计划之前，先读 `PROJECT_CHARTER.md`、`ARCHITECTURE.md`、`ROADMAP.md` 以及相关 ADR。`docs/source-plans/` 是归档研究，不是实现规格。若研究与权威文档冲突，以权威文档为准。

仓库文档使用简体中文。`LICENSE` 与 `NOTICE` 保持英文原文。归档研究报告保持历史快照，不要按当前进度改写成活规格。

## 阶段纪律

1. 每个 PR 只做当前路线图中的一个交付阶段。带字母的标签如 `2A`、`3D`、`5C` 各自需要独立 PR。`plans/` 中的编号文件是计划；其带字母切片与 `ROADMAP.md` 中的交付阶段一一对应。
2. 开始时写明阶段、范围、非目标、测试与 Gate。
3. 必需 Gate 失败、未跑或含糊时，不要开始依赖它的后续阶段。`ROADMAP.md` 标明的延期验证不是已通过的 Gate。
4. 硬件不可用记为延期。只继续路线图标明未阻塞的独立工作；不要绕过前置条件，也不要把延期报成已通过。
5. 进入条件与依赖图见 `ROADMAP.md`。不要加入 Woosh T2A、Woosh-Flow 或 Woosh-DFlow 路径。
6. PR 说明应包含改动、验证命令与结果、Gate 状态、已知风险，以及下一个允许的阶段。

## 架构边界

- 依赖方向固定：Frontend -> Workflow -> Durable Service / Local Executor -> Runtime Core -> Model Adapter -> Original Model Backend。
- Runtime Core 保持模型无关。稳定生命周期是 `AudioModelAdapter` 的 `load()`、`invoke()`、`unload()`。
- ControlFoley 请求字段与内部实现不进入 Core 契约。`video_path`、`reference_audio_path`、`prompt`、`negative_prompt`、`num_steps`、`guidance_scale`、`mask_away_clip`、CLIP、CAV-MAE、Synchformer、CLAP、FlowMatching、VAE 与 vocoder 只属于适配器/任务模式。
- Runtime Core 不得导入 FastAPI、数据库代码、BlobStore 客户端、ComfyUI 或上游模型包。模型适配器不得导入前端或持久化服务代码。
- ComfyUI 保持可选，不是模型执行、任务状态或产物状态的权威。

## 持久化执行规则

- 远程请求不得包含服务器本地路径、源码目录、权重路径、Python 模块或仅运营方可设的部署设置。
- 只接受已验证资产作为持久化任务输入。全文件 SHA-256 是内容身份；不要用 S3 ETag 或多段校验和替代。
- 尝试级至少一次执行，可见结果恰好一个。心跳、发布与最终化必须使用尝试身份与 lease epoch。
- 在所需产物通过校验、已持久化并原子关联到获胜尝试之前，不要把任务标为成功。

## 变更卫生

- 编辑前先读现有代码与测试；做范围最小的改动。不要把归档研究当活规格改写。
- 不要提交模型权重、缓存、生成媒体、凭据或私有路径。除非后续已批准的阶段明确要求，不要改上游算法或默认值。
- 不要做未经测量的 CUDA/Triton/TensorRT/ONNX、求解器改动或速度声称。
- 行为变更要补充或更新测试。规范验证命令见 `.github/workflows/ci.yml`。除非被明确要求，不要 push 或创建 PR。

## 验证

以 CI 为准：`ruff format --check`、`ruff check`、`pyright`、`pytest -m 'not gpu'`，以及工作流中的 PostgreSQL 16 job。开发 extra 与 CI 一致：`setuptools==82.0.1`，然后 `pip install --no-build-isolation -e '.[dev,postgres-test]'`。

PostgreSQL 套件需要 `NANO_AURAL_POSTGRES_BIN`（Linux CI 为 `/usr/lib/postgresql/16/bin`）。GPU 测试在无硬件或无运营方权重时 skip；skip 不是通过。根包保持无 torch。Compose 参考栈的密钥放在仓库外部。

不要把本地计数或 skip 写成发行 Gate。只有对应 PR 的 GitHub Actions 绿灯才能更新 `plans/STATUS.md`。
