# nanoAuralRuntime 的 Agent 说明

## 权威与阅读顺序

改代码或计划之前，先读 `PROJECT_CHARTER.md`、`ARCHITECTURE.md`、`ROADMAP.md` 以及相关 ADR。把 `docs/source-plans/` 当作归档研究，而不是可复制的规格。若研究与权威文档冲突，以权威文档为准，必要时记录聚焦的 ADR 或路线图更新。

仓库文档使用简体中文。许可证原文（`LICENSE`、`NOTICE`）与两份超长归档研究报告保持英文原文，因其分别为法律文本与历史快照。

## 阶段纪律

1. 每个 PR 只做当前路线图中的一个交付阶段。带字母的标签如 `2A`、`3D`、`5C` 是完整阶段，各自需要独立 PR。`plans/` 中的编号文件是计划；其带字母切片与 `ROADMAP.md` 中的交付阶段一一对应。
2. 开始时先写明阶段、范围、非目标、测试与 Gate。
3. 任何必需测试或 Gate 失败、未跑、不可用或含糊时，不要开始依赖它的后续阶段。独立阶段仅在其自身明确的路线图进入条件满足后才能推进。唯一例外是 `ROADMAP.md` 中明确排期为延期的验证；它不是已通过的 Gate，也绝不允许据此声称。
4. 硬件不可用的检查（含 4090 基准）记为延期验证。只继续路线图标明未阻塞的独立工作；不要绕过前置条件，也不要把延期验证报成已通过。
5. 遵守进入条件：阶段 3A 只依赖阶段 1；阶段 3D 还依赖 2B；在 GPU 对齐延期期间，4A–4D 为实验性/非默认；5A 需要 2B，5B 需要 3E，5C 需要两者。阶段 7A 依赖阶段 1，不等阶段 6 硬件；7B 需要 7A；8A 需要 7A；8B 需要 7B 与 8A；8C 需要 8B 与 3E；8D 与 8E 可选且不门控 Woosh；9A 需要 7A；9B 需要 7B 与 9A；9C 需要 9B 与 3E；9D 需要 9B；10A 需要 8B、9B 与 5C。完整依赖图见 `ROADMAP.md`。不要加入 Woosh T2A、Woosh-Flow 或 Woosh-DFlow 路径。
6. 每个 PR 结束时写明改动文件、已跑命令、测试结果、逐项 Gate 状态、已知风险，以及下一个允许的阶段。

## 架构边界

- 保持固定依赖方向：Frontend -> Workflow -> Durable Service / Local Executor -> Runtime Core -> Model Adapter -> Original Model Backend。
- Runtime Core 保持模型无关。稳定生命周期是通过 `AudioModelAdapter` 的 `load()`、`invoke()`、`unload()`。
- 不要把 ControlFoley 请求字段或内部实现放进 Core 契约。`video_path`、`reference_audio_path`、`prompt`、`negative_prompt`、`num_steps`、`guidance_scale`、`mask_away_clip`、CLIP、CAV-MAE、Synchformer、CLAP、FlowMatching、VAE 与 vocoder 只属于适配器/任务模式。
- Runtime Core 不得导入 FastAPI、数据库代码、BlobStore 客户端、ComfyUI 或上游模型包。模型适配器不得导入前端或持久化服务代码。
- ComfyUI 保持可选。不要让它成为模型执行、任务状态或产物状态的权威。

## 持久化执行规则

- 远程请求不得包含服务器本地路径、源码目录、权重路径、Python 模块或仅运营方可设的部署设置。
- 只接受已验证资产作为持久化任务输入。全文件 SHA-256 是内容身份；切勿用 S3 ETag 或多段校验和替代。
- 保持尝试级至少一次执行，以及恰好一个可见获胜结果。所有心跳、发布与最终化写入必须使用尝试身份与隔离/租约世代（lease epoch）。
- 在所需产物通过校验、已持久化，并原子地关联到获胜尝试之前，不要把任务标为成功。

## 变更卫生

- 编辑前先读现有代码与测试；做范围最小的改动。不要改归档研究计划。
- 不要提交模型权重、缓存、生成媒体、凭据或私有路径。除非后续已批准的阶段明确要求，不要改上游算法或默认值。
- 初始垂直切片中不做 CUDA、Triton、TensorRT、ONNX、求解器改动或速度声称。
- 行为变更要补充或更新测试，先跑最窄的相关套件再做更广验证。除非被明确要求，不要 push 或创建 PR。用户明确要求直接推远端时除外。

## Cursor Cloud 特定说明

- 开发在 `main` 上进行。历史上的 `codex/*` 与 `cursor/*` 阶段分支已合并并删除；不要把它们重建为默认工作分支。除非用户另有说明，PR 针对 `main`。
- Cloud Agent 会话安装到仓库本地 `.venv`（Ubuntu 镜像没有 `ensurepip`，需先装 `python3.12-venv`）。用 `.venv/bin/python -m …` 调用工具。CPU extra 与 CI 一致：先 `setuptools==82.0.1`，再 `pip install --no-build-isolation -e '.[dev,postgres-test]'`。规范的 lint、类型检查与 CPU 测试命令见 `.github/workflows/ci.yml` 与 `README.md`（`ruff format --check`、`ruff check`、`pyright`、`pytest -m 'not gpu'`）。
- 本 Cloud Agent VM 没有 NVIDIA GPU，也没有运营方提供的 ControlFoley、Stable Audio 3 或 Woosh V2A 权重。带 GPU 标记的测试会 skip；不要把 skip 当成已通过的 Gate。保持根包无 torch。此处没有 Docker；用进程内持久化测试代替 `compose.yaml`。
- PostgreSQL 16 二进制在已安装时位于 `/usr/lib/postgresql/16/bin`。跑 postgres 套件前导出 `NANO_AURAL_POSTGRES_BIN` 到该路径；否则这些测试会 skip。恢复测试在 `/tmp`（或可写的短根 `/private/tmp`）下创建短 Unix socket 集群。它们不需要 Linux 上的 `/private/tmp` 符号链接；该说明仅作为历史 macOS 路径。`docs/durable-operations.md` 中的 Compose 参考栈需要仓库**外部**的五份仅所有者可读密钥文件——切勿把密钥写进 `.env` 或 `compose.yaml`。
- `integrations/` 下的可选 ComfyUI 树不在 wheel 中；拆除它们不得破坏无头测试。

## 常驻自验（Cloud Agent 默认）

修复 review 发现或准备 Gate 交接且无需用户逐步确认时，跑下面完整的非 GPU 软件门槛。不要逐步问用户确认；只在缺少密钥、运营权重或 RTX 4090 硬件时停止。

1. `export NANO_AURAL_POSTGRES_BIN=/usr/lib/postgresql/16/bin`（Linux PG16）。
2. `.venv/bin/python -m ruff format --check .`
3. `.venv/bin/python -m ruff check .`
4. `.venv/bin/python -m pyright`
5. `.venv/bin/python -m pytest -q -m 'not gpu'`
6. `.venv/bin/python -m pytest tests/test_release_packaging.py tests/test_release_security.py -q`
7. 触及这些区域时跑 P11 窄套件：

```bash
.venv/bin/python -m pytest \
  tests/test_second_adapter_notices.py \
  tests/test_release_packaging.py \
  tests/test_release_security.py \
  tests/test_sfx_workflows.py \
  tests/test_stable_audio_3_adapter.py \
  tests/test_woosh_v2a_adapter.py \
  tests/test_controlfoley_adapter.py \
  tests/test_adapter_plugins.py \
  -q
```

8. 与 `.github/workflows/ci.yml` 对应的 PostgreSQL 16 job：

```bash
.venv/bin/python -m pytest -q \
  tests/test_postgres_migration.py \
  tests/test_release_migration_recovery.py \
  tests/test_durable_service.py \
  tests/test_publishing_worker_postgres.py
```

把 GPU skip 当作 **延期**，绝不当成通过。不要从 Cloud 会话声称 4090 对齐、Docker daemon 证据，或合并的 P6/P10B 发行完成。本地 `pytest -m 'not gpu'` 计数不能替代 exact-PR GitHub Actions 绿灯。只有命令计数、commit SHA 与 Actions run ID 能对应那次绿灯时，才更新 `plans/STATUS.md`。
