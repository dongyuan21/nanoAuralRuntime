# 计划 0003 — 持久化服务（路线图阶段 3A–3E）

状态：计划在阶段 1 之后即可开始；包含独立的路线图阶段 3A–3E，每个恰好对应一个 PR。其 ControlFoley 支持的 Worker 切片额外需要阶段 2B。PostgreSQL 是任务、尝试、产物、lease 与获胜结果的权威；BlobStore 仅保存字节。

## 范围

构建已验证资产到持久化任务的路径：LocalFS/S3-compatible BlobStore、PostgreSQL schema/仓储、带应用层全文件 SHA-256 的上传校验、幂等任务、`FOR UPDATE SKIP LOCKED` 领取、围栏/心跳/回收、Worker 执行、产物校验/终结、HTTP API 以及远程 CLI。

以下每一项均为一个 PR。保持至少一次推理与恰好一个可见获胜产物语义。

## 非目标

- 无恰好一次 GPU 执行、任意服务器文件路径、将 ETag/复合 checksum 当作 SHA-256 信任，或使 ComfyUI 成为状态权威。
- 无跨请求批处理、多 GPU 张量并行、缓存/profiler 优化或模型采样修改。
- 不提交权重/媒体/密钥/预签名 URL。

## 交付物

### 路线图阶段 3A（一个 PR）— 契约、schema、Local BlobStore

部署、blob、资产、任务、输入、尝试、产物、Worker 与事件的迁移与仓储；受保护的状态转换、幂等哈希、LocalBlobStore、Fake Runtime Worker 以及不变量/性质测试。不断言真实 GPU 工作。

### 路线图阶段 3B（一个 PR）— 已验证上传与内容寻址资产

上传会话、直接单/multipart 上传契约、流式校验器 SHA-256、媒体探测、规范键（`blobs/sha256/...`）、去重、已验证资产守卫、暂存过期/清理器以及上传 CLI 状态。服务器生成的文件名永不成为 Worker 路径。

### 路线图阶段 3C（一个 PR）— 队列、lease 与 Worker 骨架

事务性 `SKIP LOCKED` 领取、单调 `lease_epoch`、心跳 CAS、回收器、重试/退避、Worker 注册/就绪、取消传播，以及带重复 SHA/媒体校验的物化。仅使用假适配器。

### 路线图阶段 3D（一个 PR）— GPU Worker 集成

将围栏后的持久化 Worker 连接到 `Runtime.invoke()` 与 ControlFoley 适配器，包含每次尝试工作区、进程故障策略，以及一个 GPU 进程/会话的 single-flight 行为。不导入 ComfyUI。

### 路线图阶段 3E（一个 PR）— 产物、API 与远程 CLI

校验 audio/video 与产物 SHA-256，发布尝试特定的不可变对象，终结 CAS/获胜尝试、孤儿清理、授权下载、任务/状态/取消/事件 API、远程 CLI、结构化日志/指标、Compose 参考环境以及恢复 runbook。

## 测试

- Schema 约束/性质测试断言：仅 VERIFIED 输入、一个当前尝试、epoch 围栏、一个获胜者、`SUCCEEDED ⇒ READY required artifacts`、规范不可变 blob 身份以及幂等冲突行为。
- Local/S3-compatible BlobStore 契约测试；错误 SHA 拒绝；multipart ETag 不被接受为内容身份；去重与 Worker 再校验。
- 使用 Fake Runtime Worker 进行并发领取、心跳、回收器、过期 Worker 终结、取消竞态、上传/终结前后崩溃以及孤儿测试。
- 端到端 CPU 集成：upload → verify → submit → fake worker → READY download，然后 API/Worker/数据库重启恢复。
- GPU/ControlFoley 测试为条件测试，在缺少所需 4090 主机配置时必须跳过。

## Gate

- A：迁移与假闭环证明数据库不变量，且匹配幂等输入时无重复任务。
- B：仅 VERIFIED 资产进入任务；错误 SHA 失败；规范去重有效。
- C：两个 Worker 不能同为合法执行者；过期 Worker 不能心跳/终结；已终止尝试可恢复。
- D：无 ComfyUI 的 Worker 直接调用 Runtime。所需 4090 Worker smoke/恢复 Gate 在明确硬件延期下为 **DEFERRED**，并非已通过。
- E：恰好一个已验证可见获胜者；`SUCCEEDED` 具有所需 READY 产物；CPU 恢复矩阵通过。实际完整 4090 远程闭环仍为 **DEFERRED**，不表示为成功。
