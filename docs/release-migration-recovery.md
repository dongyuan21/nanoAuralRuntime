# 阶段 6 迁移与恢复加固

## 范围、非目标、测试与 Gate

此阶段 6 非硬件切片封印迁移历史，并提供有界的 PostgreSQL 加规范 blob 恢复集工具。它覆盖全新迁移、重复迁移、从每一个历史 `0001` 到 `0005` 前缀的升级、对仅文件名遗留账本的显式采纳、不可变账本强制、恢复集完整性，以及可重启的备份/恢复阶段。

它不运行 ControlFoley、CUDA、ComfyUI、Docker 或延期的 RTX 4090 套件。它不使完整阶段 6 Gate 通过。skip 的硬件或缺失工具检查永远不是成功证据。

此切片的软件 Gate 要求：

- 根与打包迁移字节相同；
- 真实 PostgreSQL 16 迁移/采纳/篡改测试；
- 备份/恢复契约与全部故障注入测试；
- 当这些客户端二进制存在时的真实 `pg_dump`/`pg_restore` 演练；
- 恢复后的迁移账本、事件、获胜者目录、规范输入，以及下载输出 SHA-256/大小校验。

非硬件 Gate 使用匹配的 PostgreSQL 16.3 服务器与客户端工具执行。聚焦套件完成 42 项测试且无 skip，包括真实 dump/restore 获胜者、目录、输入、输出与迁移账本检查。未来缺少 `pg_dump`、`pg_restore` 或 `psql` 的宿主仍须将演练报告为 **UNRUN_ENV**，永不作为通过。

## 校验和封印的迁移账本

每一个新应用的 SQL 文件按严格 UTF-8 解码，并对其精确字节求哈希。运行器执行 SQL，并在既有咨询事务锁下于同一事务中插入其文件名与小写 SHA-256。账本行必须是可用迁移集的精确有序前缀。缺失、未知、乱序、畸形或校验和不匹配的历史失败即关闭。

封印后，摘要为 `NOT NULL`，约束为小写 SHA-256，账本行由精确校验的数据库触发器/函数对保护。它拒绝每一次 `UPDATE` 或 `DELETE`，并拒绝 `INSERT`，除非迁移运行器已安装其事务本地守卫。每一次调用都重新检查精确已校验 CHECK 表达式、触发器事件掩码、启用模式、函数体/标志/所有者，以及对象基数；同名空操作、禁用触发器或仅副本触发器失败即关闭。

事务本地守卫防止迁移所有者意外直接插入；它不是密钥，也不是对该所有者的防御。部署边界是 PostgreSQL ACL：只有专用迁移所有者/migrator 角色可以拥有或插入账本，而应用与 Worker 运行时角色只获得 `SELECT`，没有账本 `INSERT`、`UPDATE` 或 `DELETE`。安装器从 `PUBLIC` 撤销这些操作。数据库所有者/superuser 仍是受信任根，可以故意更改 ACL、禁用触发器或伪造守卫状态；没有任何库内机制可以声称防护恶意所有者。将迁移凭据保持在运行时服务之外。校验还拒绝向非所有者角色授予任何表级或列级账本 DML；迁移命令必须以账本所有者连接（或显式对其 `SET ROLE`）。即使运行时角色精确复现文档中的事务本地守卫，仍无法追加。

包含文件名但没有摘要的历史账本，不是哪些 SQL 字节被执行的证据。因此正常迁移以如下结果退出：

```text
MIGRATION_LEGACY_CHECKSUMS_REQUIRED
```

它从不自动把当前源摘要复制进那些行。

### 显式遗留采纳

从单独认证的发行源获取校验和清单，而不是从正在检查的可变部署检出。该文件必须是绝对路径、所有者拥有的常规文件，模式 `0600`（或更严），无符号链接，且至多 64 KiB。其精确模式为：

```json
{
  "schema": "nano-aural-migration-checksums/v1",
  "migrations": [
    {
      "filename": "0001_durable_foundation.sql",
      "sha256": "<release-trusted-lowercase-sha256>"
    }
  ]
}
```

清单必须恰好包含每一个既有账本文件名。采纳检查账本是有序前缀、每一个受信任摘要匹配当前迁移字节，且任何已存在摘要一致。仅在此之后，一个事务填充遗留摘要并安装不可变约束。它不应用待处理迁移。

```sh
export NANO_AURAL_DATABASE_DSN='<operator secret supplied outside source control>'
python -m nano_aural_runtime.durable.migration_admin \
  --adopt-checksums /absolute/private/trusted-migration-checksums.json
python -m nano_aural_runtime.durable.migration_admin --verify
python -m nano_aural_runtime.durable.service --migrate-only
python -m nano_aural_runtime.durable.migration_admin \
  --grant-runtime-role nano_aural_runtime
python -m nano_aural_runtime.durable.migration_admin --verify
```

管理命令发出固定结果或错误码。它从不打印 DSN、清单路径、SQL 字节、异常文本或密钥值。

运行时授权有意放在迁移之后。PostgreSQL 初始化必须已经创建固定的 `nano_aural_runtime` LOGIN，带独立密码且无提升属性。仅所有者命令授予应用 DML 与序列访问，为未来应用表建立所有者默认权限，撤销每一个表/列账本 DML 权限，仅授予账本 SELECT，并重新运行精确封印校验。它既不创建该角色，也不接受任意标识符。

## 恢复集契约

在整个演练期间关闭 API、Worker、回收器、上传校验，以及任何其他数据库或规范存储写入者。从最初空目标预先检查直到完成，目标 PostgreSQL 数据库与目标规范根必须离线，并由此操作独占。这是硬安全前置条件，不是建议。

`pg_dump`、`pg_restore` 与 `psql` 只从一个绝对、规范二进制目录选择。每个二进制必须是当前用户或 root 拥有的常规可执行文件，且不可被组/其他人写。PostgreSQL 凭据来自绝对、所有者拥有、模式 `0600` 的 `pg_service.conf`；命令只使用其已校验服务名。服务文件与客户端二进制在打开时不跟随符号链接；其描述符保持为身份锚点。每一次调用在子进程前后精确重新检查路径与描述符元数据，并再次对服务文件求哈希。子进程通过 `/dev/fd` 接收该持续绑定的服务文件，使用最小环境、`shell=False`、有界 stdout、固定超时，并丢弃 stderr。被替换或就地修改的服务/工具失败即关闭。命令参数、stderr、DSN 与私有路径从不在工具输出中转发。

完整恢复集目录恰好包含：

```text
database.dump
canonical/blobs/sha256/<aa>/<bb>/<full-sha256>
manifest.json
```

`database.dump` 是 PostgreSQL custom 格式。`manifest.json` 最后写入，只记录其 SHA-256/大小、SHA-256 化名源数据库身份，以及已排序的规范对象键、SHA-256、大小清单与总字节。该身份对服务器报告的集群系统标识符与数据库 OID/名称求哈希；它让重启拒绝被切换的服务或数据库，而不暴露那些值。清单不含 DSN、服务路径、命名空间、任务/尝试/产物 id、请求、token、宿主或源路径。每个文件恰好为 `0600`；目录恰好为 `0700`。文件与全部嵌套目录在暂存发布前 fsync。限额独立限制 dump 字节、对象数量、单个规范对象、规范总字节、临时暂存字节、清单字节、子进程输出与子进程时间。对备份，临时字节指 dump 加规范暂存字节；对恢复，它指新创建的规范暂存（已存在、只读的恢复集 dump 不第二次计入）。全部哈希与复制按块进行；文件增长超过声明或配置界限则中止。

源根与目标都必须是绝对、规范、私有、非符号链接目录，且源/目标树必须不相交。规范键必须精确匹配 `blobs/sha256/aa/bb/<digest>`。未知文件、未列出或空前缀目录、符号链接、替代拼写、摘要漂移、清单漂移，以及已存在目标，都失败即关闭。

### 创建恢复集

```sh
python -m nano_aural_runtime.durable.release_recovery backup \
  --postgres-bin-root /absolute/postgresql/bin \
  --pg-service-file /absolute/private/pg_service.conf \
  --service source \
  --canonical-root /absolute/private/canonical-source \
  --recovery-set /absolute/private/recovery-set-001 \
  --max-single-blob-bytes 68719476736 \
  --max-blob-bytes 1099511627776 \
  --max-dump-bytes 1099511627776 \
  --max-temporary-bytes 2199023255552
```

在开始 dump 之前，备份要求完整发行权威：精确有序的文件名/SHA-256 账本必须等于与运行工具一起打包的迁移，外加通过每一项封印与 ACL 检查。错误摘要、历史前缀或未知行在 `pg_dump` 之前失败。

备份使用确定性私有兄弟暂存与固定有界状态文件。dump、规范清单、哈希、清单与目录 fsync 在暂存以内核不可替换操作原子重命名到最终路径之前完成。持久状态绑定服务器报告的源数据库身份。不完整构建的崩溃使下一次相同命令只丢弃其标记的私有暂存并重建；若完整、已校验清单已经持久，则在不第二次 dump 的情况下恢复发布。恢复路径在推进持久状态之前再次递归 fsync 完整暂存树；被中断的 fsync 将 `building` 留在原地，下一次重启重复它。最终重命名后的崩溃也收敛且不覆盖。
被切换的源服务/数据库在 dump 或发布之前被拒绝。

### 恢复到空目标

在同一仅所有者文件中为新创建、空、离线的目标数据库配置另一个服务，然后运行：

```sh
python -m nano_aural_runtime.durable.release_recovery restore \
  --postgres-bin-root /absolute/postgresql/bin \
  --pg-service-file /absolute/private/pg_service.conf \
  --service empty-target \
  --recovery-set /absolute/private/recovery-set-001 \
  --canonical-root /absolute/private/canonical-restored
```

恢复首先校验整个 dump 与规范清单，将 blob 复制到带每对象、合计与临时字节界限的私有兄弟暂存，对其再哈希，并 fsync 暂存树。然后在调用带
`--single-transaction --exit-on-error --no-owner --no-privileges` 的 `pg_restore` 之前写入 `database_restoring`。

持久恢复状态存储目标的服务器报告集群/数据库身份，并在数据库恢复与规范发布之前重新检查它；被切换的目标被拒绝。在强制独占/离线目标规则下，重启解释是明确的：空数据库且处于 `database_restoring` 意味着单事务未提交，可以重试；非空数据库意味着单事务在本地标记推进前已提交。在规范发布之前，权威探测固定为 `pg_catalog` 与 `public`，并要求精确有序的已打包文件名/SHA-256 迁移集、精确表与列 ACL 边界、精确已校验 CHECK、精确触发器/函数定义，以及启用的 `ORIGIN` 或 `ALWAYS` 模式。影子对象、旧前缀、错误/未知行、空操作函数、意外 DML 授权、禁用触发器与仅副本触发器失败。真实演练仍将完整迁移校验器作为独立恢复后检查调用。

数据库权威校验之后，规范暂存重命名到先前不存在的目标。数据库恢复或规范重命名后的崩溃从状态文件恢复，且从不运行第二次已提交恢复或覆盖目标。若任何外部行为者可能已写入目标 DB，非空恢复推断不安全：隔离该目标，创建新的空离线数据库与不存在的规范根，并重复演练。

成功 CLI 输出只包含有界计数。它不标识对象、行、路径、凭据或运营方。

## 真实演练校验

使用与服务器同一发行的 PostgreSQL 16 客户端工具。自动化真实演练创建 VERIFIED 输入、任务、事件、隔离尝试、FINALIZED 发布、SUCCEEDED 任务、READY 可见产物，以及规范输入/输出字节。dump 与 restore 之后它校验：

1. 对照打包 SQL 字节的完整迁移账本；
2. 任务事件可读；
3. 可见获胜者/目录连接恰好返回一个 READY 获胜者；
4. 获胜者 SHA-256 与大小等于发布证据；
5. 下载的规范输出字节匹配这两个值；
6. 已验证输入规范字节也匹配。

用以下命令运行聚焦套件：

```sh
NANO_AURAL_POSTGRES_BIN=/absolute/postgresql/bin \
  pytest -q tests/test_release_migration_recovery.py
```

若缺少 `pg_dump`、`pg_restore` 或 `psql`，真实演练报告 `UNRUN_ENV`。不要把该 skip 翻译成通过。在 CI 中，将此整个测试文件包含进 PostgreSQL 作业，并使用包含全部六个所需服务器/客户端可执行文件的二进制目录。
