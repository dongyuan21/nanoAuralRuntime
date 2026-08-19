# 发行就绪与产物契约

## 当前决策

阶段 6 非硬件加固已通过独立审查的软件 Gate。本文定义构建与校验机制；它不宣布整体发行 Gate 已通过。所需的 RTX 4090 与真实 ComfyUI 证据仍为 **DEFERRED**，并阻塞发行。真实 PostgreSQL 16 备份/恢复已执行；本宿主上的 Docker daemon 校验仍为 **UNRUN**，不得由静态测试推断。

## 无头发行允许列表

`tools/release_artifacts.py` 要求活动解释器恰好提供 setuptools 82.0.1，以匹配构建系统钉死版本，然后在无构建隔离、无网络的情况下运行该后端。可复现性证据适用于此钉死后端，而不是某一范围的 setuptools 实现。该工具复制一份临时源码快照，仅包含：

- `pyproject.toml`、`README.md`、`LICENSE` 与 `NOTICE`；
- `nano_aural_runtime`、
  `nano_aural_runtime_cli`、`nano_aural_runtime_controlfoley`、
  `nano_aural_runtime_remote`、`nano_aural_runtime_stable_audio_3`、
  `nano_aural_runtime_woosh`、`nano_aural_runtime_workflows` 与
  `nano_aural_runtime_workers` 下的 Python 文件；
- 五份已声明的 `nano_aural_runtime/durable/sql/*.sql` 迁移资源。

意外的包文件、未知迁移资源、符号链接、特殊文件、不安全归档路径、重复的 wheel 成员、意外的 setuptools 元数据，或已存在的输出，都会导致失败即关闭的构建。wheel 审计校验每一个 `RECORD` 行、成员哈希与大小、其空的自身条目、精确的控制台入口点，以及相对 `pyproject.toml` 的元数据/依赖语义。sdist 审计拒绝重复名称，将全部四份根元数据文件绑定到仓库字节，并要求两份 `PKG-INFO` 副本与全部 egg-info 语义匹配经审计的 wheel 与项目契约。

完成后的 wheel 与 sdist 在发布前接受审计。两个目标名称在任一产物创建前预先检查；暂存文件与最终链接作为同一集合同步。链接或目录同步失败时，仅删除该次调用创建的目标，保留无关或既有文件，并同步回滚。setuptools 的 sdist 内容按排序顺序重写，并在固定时间 gzip 包络内使用固定时间戳、所有者、组与模式；wheel 使用同一固定源 epoch。由相同字节进行的两次构建必须产出相同的 wheel 与 sdist 字节。该命令只发出产物基名、SHA-256、大小，以及显式的 blocked/deferred 状态——从不发出发行或硬件声称。

两份产物均不含 `integrations`、测试、基准、归档研究、权重、checkpoint、模型/Hugging Face 缓存、媒体、密钥、生成证据，或仓库本地构建/缓存状态。

## 全新安装验收

对每个对外声明的 Python 版本，创建新的虚拟环境，并用 `--no-index --no-deps` 安装经审计的 wheel。验收要求：

1. `nano-aural --help` 与 `nano-aural-remote --help` 在没有运营方配置或第三方模型依赖的情况下执行；
2. 本地适配器、公开远程客户端、持久化服务/Worker/恢复帮助，以及全部四个无头包家族在没有 ComfyUI 或 torch 的情况下可导入；
3. `importlib.resources` 恰好暴露五份 SQL 迁移；
4. `integrations.comfyui*` 不存在；
5. 已安装发行包含 Apache `LICENSE`、`NOTICE`，以及两个已声明的控制台入口点。

基础 wheel 没有强制第三方依赖。PostgreSQL 支持使用 `.[durable-postgres]` 或 `.[postgres-test]` 单独测试。ControlFoley 后端与模型材料是运营方管理的外部依赖，而不是 Python extra。

需要 ComfyUI 前端时，用 `tools/release_comfyui_archives.py` 单独打 zip（Embedded / Remote / Compat），细节见 [ComfyUI 兼容与拆除](comfyui-compatibility-removal.md)。这些 zip 不是 wheel 的一部分，也不参与无头路径。

## 容器与 HTTP 边界

参考 Dockerfile 有意只复制 `src/nano_aural_runtime`，而经审计的 wheel 包含该精确树，外加分层放置的本地适配器/Worker/客户端包。打包测试证明 wheel 的 Core/持久化字节等于 Docker 构建上下文源码。`durable-postgres` 库 extra 仍是兼容范围 `psycopg[binary]>=3.1,<4`；单独的容器需求文件是其 Python 3.12/Linux x86_64 解析子集，带精确版本、经审查的 wheel SHA-256 值、`--require-hashes`，以及仅二进制解析。Compose 将从该 Dockerfile 构建的每个服务固定为 `linux/amd64`，且 Python 基础 tag 钉死到经审查的 Docker Hub index digest。这不声称其他架构。Docker 镜像不是通用 wheel 的替代，源码复制镜像必须从与其 wheel 产物相同的经审查修订重建。

标准库 WSGI 服务器与 Compose 拓扑是 CPU 恢复/参考工具。它们不提供生产 TLS、代理、多进程、慢客户端或 Internet 边缘安全边界。生产声称需要另行审查的服务器/代理部署、超时、并发/资源限制、优雅关闭证据、备份/恢复演练，以及真实 daemon 校验。

## CI 证据与条件环境

干净检出工作流将第三方 action 钉死到 commit SHA，并配置为运行：

- Python 3.9–3.12 CPU 测试，以及按版本的 wheel/sdist/全新 venv 打包；
- 对源码、可选集成、工具与测试的 Ruff format/check 与 Pyright；
- 当 runner 暴露已声明 PG16 二进制时的 PostgreSQL 16 迁移、服务回环与发布套件；
- 单独、显式选择加入的 Docker 参考作业；
- 单独、显式选择加入的自托管 RTX 4090 证据收集作业。

Compose 与 CI 使用的 PostgreSQL 16 服务镜像保留其可读的 `postgres:16.3-bookworm` tag，并钉死经审查的 Docker Hub index digest。静态审计覆盖拒绝这些正式 `image` 字段中缺失或畸形的 digest。这是声明证据，不是 registry 或 daemon 检查。

Docker 与 GPU 作业在不可用时不会被静默转为成功。GPU 作业仅用于证据收集：输出仍需对照每一项路线图硬件 Gate 独立审查。skip 的测试永远不是通过的硬件结果。

## 未决发行阻塞项

- 完成并独立审查每一套延期的 RTX 4090 runbook/证据，包括真实 Embedded 与 Remote ComfyUI 宿主执行。
- 在真实 daemon 上执行 Docker 作业，并记录启动、迁移、假闭环、重启、密钥/日志、卷与关闭证据。
- 从精确批准的发行修订重复成功的 wheel/sdist、SPDX 校验、API-lock 漏洞审计、CycloneDX 生成、密钥扫描、依赖/许可审查，以及产物校验和捕获。当前证据属于 `0.1.0.dev0` pre-alpha 候选。
- 在作出发行声称前，解决或显式接受每一个剩余最终候选发现，包括基于范围的库/构建声明的归档身份，以及任何 `NOASSERTION` 许可字段。
- 仅在候选版本与发行说明获批准后替换 `0.1.0.dev0`。在此之前不要移除 pre-alpha 或实验性措辞。
