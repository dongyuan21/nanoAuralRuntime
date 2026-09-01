# 持久化服务运维与恢复

持久化路径用来把已验证媒体变成可恢复的任务：上传与完整性校验、排队、Worker 认领、产物发布与下载。PostgreSQL 是资产、任务、尝试、租约、发布状态、READY 产物与获胜尝试的权威。规范 BlobStore 只在数据库校验之后才是字节的权威。尝试对象是临时、不可变的发布中间物。

本文的参考 Worker 使用 Core `FakeAudioAdapter` 产出确定性静音 WAV，用来演练队列、隔离、校验、发布与下载。它不评价模型质量。完整 ControlFoley 远程闭环与 4090 冒烟/恢复仍为 **DEFERRED**。

## Compose 参考启动

`compose.yaml` 是 JSON 形态的 YAML，以便 Compose 与标准 JSON 工具都能解析入库参考。它定义 PostgreSQL 16、一次性迁移与运行时权限服务、API、CPU 假发布 Worker、持久化的数据库/规范/暂存/尝试卷，以及惰性的 `gpu-deferred` profile。每个从 `ops/Dockerfile.api` 构建的服务固定为 `linux/amd64`。Python 3.12 基础使用 tag 加经审查的 Docker Hub index digest，API 依赖是精确、要求哈希、仅二进制的 Linux x86_64 锁。因此在其他宿主架构上运行此参考需要 amd64 支持或模拟；入库哈希不声称原生 arm64 或 ppc64le 支持。单独的 `postgres:16.3-bookworm` Compose 镜像与匹配的 CI 服务保留该 tag，并钉死经审查的 Docker Hub index digest `sha256:d0f363f8366fbc3f52d172c6e76bc27151c3d643b870e1062b4e8bfe65baf609`。

在仓库外准备五份互不相同、仅所有者可读（`0400` 或 `0600`）的密钥文件。迁移与运行时凭据绝不可使用同一密钥或密码：

| 密钥 | 内容 |
| --- | --- |
| `NANO_AURAL_POSTGRES_MIGRATION_PASSWORD_FILE` | 固定数据库所有者 `nano_aural_migrator` 的密码。 |
| `NANO_AURAL_POSTGRES_RUNTIME_PASSWORD_FILE` | 仅用于初始化非所有者 `nano_aural_runtime` LOGIN 的独立密码。 |
| `NANO_AURAL_MIGRATION_DATABASE_DSN_FILE` | `nano_aural_migrator` 的所有者 DSN；仅由迁移与权限服务挂载。 |
| `NANO_AURAL_RUNTIME_DATABASE_DSN_FILE` | `nano_aural_runtime` 的非所有者 DSN；仅由 API 与 Worker 挂载。 |
| `NANO_AURAL_TOKEN_GRANTS_FILE` | `token_sha256`、`subject`、`scopes` 与 `namespaces` 的 JSON 数组；绝不是明文 bearer token。 |

将这些五个环境变量设为密钥文件的绝对路径，然后运行：

```sh
docker compose config
docker compose up --build --wait \
  postgres migrate grant-runtime-privileges api cpu-reference-worker
```

迁移服务以迁移所有者身份运行 `python -m nano_aural_runtime.durable.service
--migrate-only`；重复执行是已入账的空操作。仅在其完成后，`grant-runtime-privileges` 使用所有者 DSN 运行
`migration_admin --grant-runtime-role nano_aural_runtime`。
它校验该固定角色是非所有者 LOGIN，且没有 superuser、角色、数据库、复制、继承、bypass-RLS 或成员提升。然后授予 schema 使用权、应用表 DML、序列访问，以及面向未来表的匹配所有者默认权限。迁移账本仅为 SELECT：表与列 DML 授权被撤销，并再次校验精确封印。该操作是幂等的。

对于文件支持的密钥，Compose 无法重映射宿主所有权。因此 root 包装器只接受常规、非符号链接、仅所有者可读的源，最多将 4 KiB 复制到 PostgreSQL 所有、模式为 `0400` 的私有 `tmpfs` 文件，并立即把控制权交给镜像的官方入口点。PostgreSQL 初始化只读取该暂存文件，不把密码放入进程参数。SQL 使用 psql 变量字面量与固定角色标识符。API 与 Worker 服务依赖权限授予成功完成，且只挂载运行时 DSN；它们从不接收迁移密码或 DSN。正常 API 启动不会隐式迁移。CPU 参考部署 id 是
`00000000-0000-4000-8000-000000000301`；向此假部署只提交需要单一
`output` 产物种类的任务。

API 镜像只复制 `src/nano_aural_runtime`。它排除 ControlFoley 包、torch/CUDA 依赖、权重、上游源码与 ComfyUI。启用 `--profile gpu-deferred` 只会启动一个有意失败的占位符；没有入库的 GPU 镜像或 GPU 就绪声称。

## 进程与重启规则

- 在 API 或 Worker 启动前，PostgreSQL 必须健康，且一次性迁移与运行时权限服务均须成功。
- API 与 Worker 连接对读取使用 autocommit；每一次持久化变更仍拥有显式仓储事务。
- 运行时与发布心跳监视器使用独立 PostgreSQL 连接，并在每次尝试后关闭它们。Worker 在优雅关闭时卸载其 Core 会话，并关闭存储与命令连接。
- PostgreSQL 恢复后数据库重启是安全的。重启迁移、API 与 Worker；迁移账本使此操作幂等。
- API 重启不丢失持久化状态。在提交前被中断的请求可用同一幂等键重试；下载客户端校验全文件 SHA-256 与大小。
- Worker 重启从不假定其旧尝试。等待租约过期，运行回收器，然后允许带更高世代的新认领。
- 对象存储/本地卷重启必须保留规范、暂存与尝试卷。若规范存储不可用，不要启动 Worker。

## 恢复命令

API、Worker 与日常恢复命令通过 `ops/load_secrets.py` 消费运行时 DSN；模式迁移与权限管理消费单独的迁移 DSN。原始 `NANO_AURAL_DATABASE_DSN_FILE` 加载器输入仅对非 Compose 部署仍然可用。命令只打印有界的结构化结果与计数，从不打印任务、尝试、产物、提示、路径、token 或 DSN 值。

```sh
# Inspect help without a database or secrets.
python -m nano_aural_runtime.durable.reference_worker --help
python -m nano_aural_runtime.durable.recovery --help

# Reap expired leases using PostgreSQL's clock.
docker compose run --rm cpu-reference-worker \
  python -m nano_aural_runtime.durable.recovery --reap-expired

# Expire DB-clock-overdue uploads and delete only terminal staging bytes.
docker compose run --rm api \
  python -m nano_aural_runtime.durable.recovery --expire-uploads

# Mandatory dry run before attempt-object deletion; the grace floor is 5 min.
docker compose run --rm cpu-reference-worker \
  python -m nano_aural_runtime.durable.recovery \
  --attempt-orphans-dry-run --grace-seconds 600 --limit 100

# After reviewing database state and backups, perform bounded deletion.
docker compose run --rm cpu-reference-worker \
  python -m nano_aural_runtime.durable.recovery \
  --sweep-attempt-orphans --grace-seconds 600 --limit 100
```

孤儿清理器只删除由发布账本授权的尝试特定对象，或没有账本行的旧清单对象。活动租约与首次过期宽限观察被保留。若第一次只将发布标记为过期，则在数据库强制的过期宽限之后再运行一次。有意没有规范 blob 删除命令：提升后崩溃所产生的规范孤儿是安全、去重的保留。回收/删除需要后续独立审查的可达性策略。

尝试存储在创建不可变尝试对象之前记录有界清单日志。dry-run 与 sweep 在尝试卷上有独立的持久游标。`--limit N` 限制每次调用检查的日志条目、哈希的对象、数据库键数组与恢复候选。已知/活动或年轻条目消耗当前页但推进其游标，因此重复运行会到达后续未知对象，而不是反复扫描同一前缀。

`--expire-uploads --limit N` 对 DB 时钟过期转换与终态暂存对象清理施加一个全局界限。每一行被检查的终态行都在 PostgreSQL 中记录幂等清理证据，即使其暂存对象已经不存在。这防止空的早期页饿死后续对象；重复有界运行直到报告计数为零。未过期的 `VERIFYING` 会话永远不是终态清理候选。

## 恢复矩阵

| 失败或观察 | 持久化证据 | 运营方动作 | 安全结果 |
| --- | --- | --- | --- |
| 任务 commit 前 API/DB 断开 | 无任务，或已提交的幂等任务 | 恢复 DB/API；用相同正文与幂等键重新提交 | 一个任务 id，或不同内容的冲突 |
| 租约活动时 Worker 进程退出 | RUNNING 任务、ACTIVE 尝试，Worker 在 DB 租约过期前为 BUSY | 不要手工重置行；过期后运行 `--reap-expired`，重启 Worker | 旧世代被隔离；重试获得更高世代 |
| 取消与执行/发布竞态 | `cancel_requested_at` 隔离心跳/发布/最终化 | 让监视器取消或回收器终态化；安全地重试取消/状态 | 被取消的尝试不能成为可见获胜者 |
| 回收器与心跳竞态 | PostgreSQL 时钟与 Worker→任务→尝试锁决定 | 如需要再次运行有界回收器 | 恰好一个当前执行者；过期心跳/最终化失败 |
| 过期前上传卡在 `VERIFYING` | 暂存字节加非终态会话 | 保留暂存；拥有精确版本的校验器可以回收。否则等待 DB 过期并运行 `--expire-uploads` | 只有完整重读 SHA/媒体验证的资产成为 VERIFIED |
| 上传在 `INITIATED`、`UPLOADED` 或 `VERIFYING` 过期 | DB 时钟逾期的会话 | 运行 `--expire-uploads` | 会话成为终态 EXPIRED，暂存字节被移除 |
| `after_attempt_write` 崩溃 | RESERVED 行；不可变对象可能存在但其键未被记录 | 若租约仍当前则重启 Worker；否则在宽限后 dry-run/sweep 清单 | 重放写入相同字节，或删除未入账的旧尝试对象 |
| `after_object_recorded` 崩溃 | 带不可变键/SHA/大小的 OBJECT_WRITTEN 行 | 重启当前尝试；否则回收，然后在过期宽限后 sweep | 校验从已记录证据恢复 |
| `after_validation` 崩溃 | OBJECT_WRITTEN 行；对象已在进程中校验，但未提交规范证据 | 重启当前尝试 | 校验器重读字节；不存在可见性 |
| `after_canonical_promotion` 崩溃 | OBJECT_WRITTEN 行；规范 blob 可能存在但没有 DB blob/发布链接 | 重启当前尝试；保守保留规范字节 | 去重提升恢复；未链接的规范永不暴露或自动删除 |
| `after_validated_recorded` 崩溃 | VALIDATED 行与 VERIFIED 规范 blob | 在租约过期前重启当前尝试，或回收并在宽限后 sweep 尝试对象 | 最终化前再次检查规范证据 |
| 最终化前立即取消/回收 | VALIDATED 发布但租约过期/已取消 | 不要强制成功；回收器/取消拥有终态，然后 sweep | 最终化 CAS 拒绝过期世代；没有可见产物 |
| `after_finalize` 崩溃 | SUCCEEDED 任务、已成功尝试、FINALIZED 发布、READY 产物、获胜尝试 | 将 DB 结果视为已提交；重试状态/下载；稍后运行尝试清理 | 恰好一个可见的已验证获胜者保留 |
| 获胜者提交后尝试清理崩溃 | 终态发布但缺少清理证据 | 先 dry-run 再有界 sweep | 只移除尝试中间物；可见规范产物保留 |
| 规范对象不可用或完整性不同 | DB 证据存在但存储读取/stat 失败 | 停止 Worker/API 下载流量，从备份恢复精确已验证字节，调查存储 | 永不把规范键替换为不同字节；不要手工标记成功 |
| 完整 API/Worker/对象重启 | PostgreSQL 与命名卷持久化 | 启动 Postgres、迁移、API、Worker；回收过期尝试并过期上传 | 排队任务恢复；已提交获胜者保持可见 |

永远不要手工编辑 `jobs`、`job_attempts`、`artifact_publications`、`artifacts` 或
`upload_sessions` 以跳过转换。约束与 CAS 路径就是恢复机制，而不是其障碍。

## 可观测性与密钥规则

`durable.observability` 是唯一的服务日志/指标策略。其指标名称与完整标签域在代码中固定：

- `nano_aural_api_requests_total`：路由类、方法、结果；
- `nano_aural_publications_total`：固定发布阶段与结果；
- `nano_aural_lease_events_total`：heartbeat/cancel/lost/reaped/retry 与结果；
- `nano_aural_orphan_actions_total`：retain/abandon/delete 与结果；
- `nano_aural_download_integrity_total`：固定完整性结果。

结构化事件字段限于允许列表中的组件、结果、原因码、时间戳，以及有界数值 `bytes`、`count` 或
`duration_ms`。永远不要把命名空间、主体、任务/尝试/产物/资产 id、幂等键、提示/请求内容、URL、本地路径、对象键、头、DSN、token、密钥文件路径或异常字符串作为标签或日志字段。只通过已认证 API 或受限数据库会话诊断特定任务。

Bearer 明文只属于客户端密钥管理器。服务器授权包含 SHA-256 摘要。DSN 与授权通过只读 Compose 密钥与允许列表加载器进入容器；它们不得出现在镜像、Compose 环境字面量、`.env`、日志、指标、崩溃报告或 shell 历史中。轮换 token 的方式是加入新摘要、重启 API 实例，然后在客户端迁移后移除旧摘要。

## 备份与校验

将 PostgreSQL 与规范对象作为同一运维恢复集备份。暂存与尝试对象是可恢复中间物，但保留它们有助于诊断被中断的操作。恢复演练必须校验迁移、任务/事件读取、获胜者/目录连接，以及全文件下载 SHA-256。它不得把延期的 4090 校验报告为已通过。
