# 发行安全与供应链证据

## 范围与非声称

本文描述阶段 6 非硬件发行安全切片。审计是确定性的，且只使用 Python 标准库。它不访问网络、不安装软件、不构建容器、不解析基础镜像 digest、不查询漏洞数据库，也不决定整体发行 Gate。

主 JSON 模式是名为 `nano-aural-internal-release-security-evidence` 的内部清单；不得将其呈现为标准 SBOM。在显式可复现创建 epoch 下，该工具还嵌入并可写出独立的 SPDX 2.3 JSON SBOM。两份文档都不是 CycloneDX、漏洞报告、许可意见或硬件结果。内部清单的 `release_gate` 字段始终为 `NOT_EVALUATED`。

正式 CycloneDX 生成、外部 SPDX 校验与外部漏洞扫描记录在 `capabilities` 下。审计在 `PATH` 上检测 `cyclonedx-py`、`cyclonedx-bom`、`pyspdxtools`、`spdx-tools`、`pip-audit`、`trivy` 与 `grype`，但从不调用它们。因此无论缺失还是仅可用，其状态都保持 `UNRUN`。空工具列表不得呈现为干净的漏洞结果或独立 SPDX 校验。

## 生成内部证据

在干净的仓库检出上运行：

```bash
.venv/bin/python tools/release_security_audit.py \
  --root . \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output /absolute/private/path/release-security.json \
  --spdx-output /absolute/private/path/release-security.spdx.json
```

两份输出均为仅所有者模式 `0600`，并作为同一失败即关闭的证据集发布，且不覆盖已有文件。在创建临时文件前预先检查每个目标；写入、链接或文件/目录 `fsync` 失败会删除该次调用创建的全部输出，并同步受影响目录。因此 `--spdx-output` 需要 `--output`；独立 stdout 仅对内部文档仍然可用。所提供的 epoch 成为 SPDX `creationInfo.created` 时间戳；环境时钟或绝对仓库路径不会进入任一文档。省略 epoch 将 SPDX 生成记为 `UNRUN`；它从不替代捏造的创建时间。

当打包产出候选归档时，校验精确字节并将其哈希加入同一证据：

```bash
.venv/bin/python tools/release_security_audit.py \
  --root . \
  --wheel /absolute/path/nano_aural_runtime-0.1.0.dev0-py3-none-any.whl \
  --sdist /absolute/path/nano_aural_runtime-0.1.0.dev0.tar.gz \
  --source-date-epoch "$SOURCE_DATE_EPOCH" \
  --output /absolute/private/path/release-security-with-artifacts.json \
  --spdx-output /absolute/private/path/release-security-with-artifacts.spdx.json
```

未提供 wheel 或 sdist 意味着那些产物校验缺失，而不是已通过。

## 证据内容

`release_inputs` 记录项目名称、声明版本、Apache-2.0 许可、`LICENSE`/`NOTICE` 哈希，以及面向预期 wheel 与源输入的已排序 SHA-256/大小记录。容器部分对 Dockerfile 与每个本地 `COPY`/`ADD` 输入求哈希，记录基础镜像名称、tag 与 digest，并枚举 Compose 与 CI 工作流中的静态 `image` 声明。参考镜像将 `python:3.12-slim-bookworm` 钉死到经审查的 Docker Hub 多平台 index digest
`sha256:d50fb7611f86d04a3b0471b46d7557818d88983fc3136726336b2a4c657aa30b`。
Compose 与 PostgreSQL CI 服务都将 `postgres:16.3-bookworm` 钉死到
`sha256:d0f363f8366fbc3f52d172c6e76bc27151c3d643b870e1062b4e8bfe65baf609`。
审计记录该声明，但不解析或独立校验 registry 对象。null digest 对 Dockerfile 基础产生
`container.base_image_not_digest_pinned`，对 Compose/CI 镜像产生
`container.image_not_digest_pinned`；畸形 digest 产生对应的 `*_digest_invalid` 发现。容器构建校验保持 `UNRUN`，因为对声明求哈希不是容器构建、registry 查找或镜像检查。

`dependencies` 合并构建、运行时、可选与 API 容器需求。每一行包含规范化名称、声明需求、已安装状态/版本/许可、已安装 `METADATA` 与 `RECORD` 哈希，以及发行归档哈希字段。API 容器行来自 pip 逻辑行，因此续行哈希附着到其包，而不会被误读为依赖。其解析后的 Python 3.12/Linux x86_64 子集精确钉死到 `psycopg` 3.2.13、`psycopg-binary` 3.2.13 与
`typing_extensions` 4.15.0。锁与 Docker 安装都要求哈希且仅二进制发行；Compose 将从此 Dockerfile 构建的每个服务固定为 `linux/amd64`。单一平台特定二进制哈希不是 arm64 或 ppc64le 支持的证据。

缺失的已安装包或未知许可保持显式。版本范围与已安装元数据哈希不是锁文件，也不能证明已下载 wheel 或 sdist 身份。null 的 `distribution_archive_hash` 对其声明来源产生 `dependency.distribution_archive_hash_unavailable`。`pyproject.toml` 中基于范围的库 extra 有意保持为库兼容性声明；容器锁是其经审查、已解析的部署子集。`supply_chain_findings` 还报告缺失 `--require-hashes`、缺失仅二进制策略、不支持的 pip 选项、非精确 API 容器需求、缺失或畸形的 SHA-256 归档哈希，或没有 digest 的基础镜像。每个结果只包含 `file` 与 `rule`。

`standard_sbom.document` 是确定性 SPDX 2.3 JSON 文档。它列出基础应用、独立的 API 容器组件，以及按规范化名称与有证据支持的版本分组的已声明 Python 依赖。对库声明，版本与许可在可用时来自观察到的本地发行。对 API 容器声明，版本改为来自其精确锁，其经审查的 wheel SHA-256 写入 SPDX 包 `checksums` 字段；无关的已安装版本不能覆盖任一值。同名且证据版本相同的声明共享一个节点，而不同版本获得确定性、带版本、抗碰撞的 SPDX 标识符与分开的关系边。仅当观察到的已安装版本匹配时，本地许可才附着到锁定节点。带版本节点携带匹配的 purl 引用。

该文档描述两个已交付组件，但不断言 Dockerfile 未证明的包含或变体关系。其图使用 SPDX
`BUILD_DEPENDENCY_OF`、`DEV_DEPENDENCY_OF`、`TEST_DEPENDENCY_OF`、
`OPTIONAL_DEPENDENCY_OF` 与 `DEPENDS_ON` 关系，保留每个 pyproject/容器组的声明用途。基础运行时依赖只附着到基础应用；仅容器依赖只附着到 API 容器组件。因此空的基础 `dependencies` 列表不产生基础应用 `DEPENDS_ON` 边。每个库包携带规范组与需求声明。命名空间种子覆盖全部包字段、那些声明、完整关系图，以及对发行输入清单的 SHA-256 绑定，外加显式 `SOURCE_DATE_EPOCH`。严格内置校验器重算该种子，并检查必填字段、标识符、唯一引用、许可允许列表、时间戳、声明到版本封印、SHA-256 校验和结构、精确 purl 版本，以及图闭包。这些是标准 SPDX 2.3 包与校验和结构，但 `external_validation` 在独立 SPDX 校验器实际执行前保持 `UNRUN`。

`external_materials` 显式记录 ControlFoley 源码检出、主 checkpoint、外部权重、Hugging Face 缓存、模型媒体与私有夹具由运营方提供，并从 nanoAuralRuntime Python 与容器发行中排除。此证据不授予获取、使用或再分发它们的权利。运营方必须单独解决全部上游源码、模型、数据集、权重与依赖许可。

## 产物校验

递归产物树扫描器使用调用方提供的显式顶层允许列表加上拒绝规则。它拒绝符号链接、密钥文件、虚拟环境、VCS/缓存目录、模型/checkpoint 格式、生成媒体、私钥，以及已编译 Python 文件。

wheel 校验将其规范文件名绑定到其唯一 dist-info 目录，以及完整 pyproject 发行契约：Name、Version、Summary、Author、Requires-Python、License-Expression/License-File、classifier、项目 URL、extra 与 Requires-Dist、README 正文，以及动态元数据。它恰好允许八个已声明包根与精确的七文件 dist-info 契约；即使是常见生成但未入契约的成员（如 `INSTALLER`）也会被拒绝。它要求全部八个 `__init__.py` 文件、源码相同的 `LICENSE`/`NOTICE`、三个已声明控制台入口点、顶层包元数据，以及精确的构建后端生成器/纯 Python tag。它还拒绝绝对路径、`..`、反斜杠、重复成员、符号链接/特殊文件、过大归档/成员、意外包或 dist-info 文件、缺失/重复的必填元数据或 `RECORD`、未列出成员、畸形行、大小不匹配、源成员/内容漂移，以及任何 SHA-256 不匹配。`RECORD` 自身行必须有空的哈希与大小字段。

源发行校验绑定其规范文件名、单一根与 `PKG-INFO` Name/Version；要求源码相同的 `README.md`、`LICENSE`、`NOTICE`、`pyproject.toml`、生成的 `setup.cfg`，以及全部八个包树。精确 egg-info 成员集是强制的：两份 `PKG-INFO` 副本必须等于 wheel 元数据契约；入口点、可选需求、顶层包、依赖链接与 `SOURCES.txt` 必须精确描述源契约。它拒绝额外或已更改的 egg-info 内容、`tests`、`integrations`、`setup.py`、其他未声明顶层、绝对/穿越或反斜杠路径、重复成员、链接、设备/特殊文件、被拒绝格式，以及过大归档/成员。归档校验就地读取成员，从不解压它们。每个产物的状态在未提供候选时为 `UNRUN`，仅在每个已提供归档都通过后为 `VALIDATED`。

## 密钥扫描与金丝雀

仓库扫描器递归检查有界 UTF-8 文本中的高信号私钥、提供商 token、AWS 密钥、JWT、凭据 URL 与硬编码密钥模式。输出恰好包含 `file` 与 `rule`；从不包含匹配值、行内容、环境数据或回溯。唯一内置例外是针对既有脱敏测试的精确 `(仓库相对文件, 规则, 匹配夹具值的 SHA-256)` 条目。没有子串、全路径或全规则占位符豁免。测试验证同一文件中匹配同一规则的不同凭据仍是发现。

发行金丝雀通过本地与远程 CLI、持久化服务配置/服务器/关闭路径、CPU 参考 Worker，以及恢复命令错误边界注入。检查它们的公开 stdout/stderr 与任何转义的完整回溯，以及持久化 HTTP 观察器日志。这些入口点上的配置、依赖、文件系统、传输、服务器生命周期与意外错误只发出稳定通用消息；原始异常字符串、目标路径、DSN、URL、响应体、提示与链式原因不会被打印。既有成功与错误退出码类别被保留。

## 所需外部后续

2026-08-17 非硬件预演用 `spdx-tools` 0.8.3 外部校验了生成的 SPDX 2.3 JSON，用精确哈希解析了 Linux/amd64 API 锁，用 `pip-audit` 2.10.1 查询了当前 PyPI 漏洞数据（三个锁定组件无已知漏洞），并为该锁发出了 CycloneDX JSON。这些是受时间与修订约束的观察，不是入库的发行证明或容器扫描。

在任何发行声称之前，运营方仍需要全部路线图要求的硬件证据，并必须对精确批准的候选与当前数据库重复独立 SPDX 校验、（如需要）CycloneDX 生成，以及漏洞扫描。他们必须审查每一个 `NOASSERTION`/未知依赖许可，校验精确构建的 wheel/sdist/容器字节，并解决或接受每一个剩余发现。入库容器声明关闭内部发现，并不校验 registry 拉取或 Docker 构建。延期的 RTX 4090 检查仍延期并阻塞发行；此非硬件证据不改变其状态。
