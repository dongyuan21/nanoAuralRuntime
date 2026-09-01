# ControlFoley-first Headless Audio Runtime 与 Durable Inference Service 研究快照

> 归档研究输入，不是实现规格。  
> 研究冻结日期：2026-08-15  
> 首个硬件基线：单卡 NVIDIA RTX 4090 24 GB  
> 首个模型适配：Xiaomi Research ControlFoley  

实施阶段与 Gate 以仓库根目录的 `ROADMAP.md` 与 `plans/` 为准。本文只保留研究结论与源码登记。

---

## 0. 本计划取代什么

本计划取代此前以“Runtime Core + ComfyUI 插件”为主要交付顺序的方案，但保留其中已经成立的四项核心资产：

1. Model Adapter；
2. Runtime Config；
3. 分阶段 Profiler；
4. Asset / Condition Cache。

主要修订是：

```text
旧的重心：
Python SDK / CLI / ComfyUI
        ↓
Audio Runtime Core
        ↓
ControlFoley

新的主链：
CLI / API
   ↓
可靠上传与完整性验证
   ↓
持久 Job / Attempt / Artifact
   ↓
GPU Worker
   ↓
Audio Runtime Core
   ↓
ControlFoley Adapter
   ↓
ControlFoley 原始模型
   ↓
产物验证与下载
```

ComfyUI 不再是主链必经组件，而是两个可选 Frontend：

```text
Embedded：ComfyUI Node → Runtime Core → 本机 ControlFoley
Remote：  ComfyUI Node → API → Durable Queue → GPU Worker → Runtime Core
```

**硬验收条件：完全删除或从未安装 ComfyUI 时，CLI/API 主链仍须完整工作。**

---

# 1. 执行结论

## 1.1 项目定义

项目建议定义为：

> 一个面向迭代式音频生成模型的 Headless Inference Runtime 与 Durable Serving Reference Implementation。ControlFoley 是首个 Model Adapter；CLI、HTTP API 和 ComfyUI 都只是可替换 Frontend。

对外名称可以继续使用 **Audio Diffusion Runtime**，但内部抽象必须保持中性，因为 ControlFoley 的公开推理实现使用的是：

```text
Flow Matching
+ Euler ODE integration
+ classifier-free guidance
```

因此核心接口采用：

```text
prepare_inputs
→ encode_conditions
→ sample / integrate
→ decode
→ validate
```

不要把通用接口写死成：

```text
denoise()
```

## 1.2 两层 Runtime 必须分开

### A. Model Inference Runtime

负责：

```text
模型加载和卸载
ModelSession 生命周期
设备与精度策略
输入规范化
条件编码
Flow Matching 采样
VAE / Vocoder 解码
取消检查
Profiler
特征 Cache
结果 Manifest
```

其核心调用应能简化为：

```python
result = runtime.generate(request)
```

它不知道 HTTP、用户、数据库、对象存储和下载权限。

### B. Durable Inference Service

负责：

```text
可靠上传
全文件 SHA-256
内容寻址资产
Job 持久化
Attempt / lease / heartbeat
Worker 调度
取消与重试
Artifact 发布
下载授权
故障恢复
审计与可观测性
```

两者的依赖方向只能是：

```text
Durable Service / GPU Worker
             ↓
Model Inference Runtime
             ↓
Model Adapter
             ↓
ControlFoley 原始模型
```

禁止 Runtime Core 反向依赖 API、PostgreSQL、S3 或 ComfyUI。

## 1.3 V1 的核心技术选择

| 领域 | V1 选择 | 说明 |
|---|---|---|
| 开发语言 | Python 3.10 系列 | 与 ControlFoley 依赖基线对齐 |
| API | FastAPI + Pydantic 2 | API 与 Worker 镜像分离 |
| 数据库 | PostgreSQL | Job 状态权威，同时承担 V1 队列 |
| 队列领取 | `FOR UPDATE SKIP LOCKED` | 单事务 claim，避免独立 broker 双写 |
| 唤醒 | `LISTEN/NOTIFY` + 定时轮询 | NOTIFY 只是提示，数据库扫描才是权威 |
| 数据访问 | SQLAlchemy Core / psycopg 3 + 显式 SQL | claim、heartbeat、finalize 使用显式事务 |
| 对象存储 | BlobStore 抽象；LocalFS + S3/MinIO | 生产采用 S3-compatible，开发采用 LocalFS |
| 上传 | Presigned PUT；大文件支持 multipart | 应用级全文件 SHA-256 为权威 |
| GPU Worker | 每 GPU 一个进程 | 每个 ModelSession 首版 single-flight |
| 模型执行 | Worker 直接 import ControlFoley 原始源码 | 不经过 ComfyUI |
| 推理语义 | at-least-once attempt execution | 不追求 exactly-once GPU 计算 |
| 发布语义 | exactly-one visible winning artifact | 使用 fencing token + DB CAS |
| 开源布局 | Python monorepo | Core、Service、CLI、ComfyUI 共享 contracts |

## 1.4 V1 不做

```text
不改 ControlFoley 权重
不训练或微调模型
不改变采样方程
不减少默认 steps
不实现 step skipping
不写 CUDA / Triton
不做 TensorRT / ONNX
不做多 GPU 张量并行
不做跨请求 continuous batching
不允许 API 提交任意服务器本地路径
不把 ComfyUI 当任务或状态权威
不把 S3 ETag 当作全文件 SHA-256
不宣称 exactly-once inference
不在正式 benchmark 前宣称加速倍数
```

---

# 2. 研究与源码冻结

## 2.1 ControlFoley 源码状态

研究时锁定：

```text
仓库：xiaomi-research/controlfoley
当前 main：68cefb5dde12c5492a4a271a857b2acd17bc473a
首个支持 revision：6858cd12a48d141201e3266e7abe1f38357a133e
```

官方 ComfyUI 集成固定使用 `6858cd1...`。对比该 revision 与 2026-08-13 的当前 main，后续仅变化了 README 和展示图片，没有模型执行代码变化。因此 `6858cd1...` 可以继续作为 V1 的受支持源码基线，但发布前仍需再次运行 commit diff 检查。

## 2.2 官方原模型可直接运行

官方 `demo.py` 已经是脱离 ComfyUI 的 Python/CLI 推理入口，直接完成：

```text
解析 video/audio/prompt 参数
加载 AudioGenerationNetwork
加载 FeaturesUtils
构建 FlowMatching
执行 generate()
保存 FLAC
可选封装回视频
```

因此：

> Headless Runtime 不需要把请求提交给 ComfyUI；它应直接适配官方 Python/PyTorch 推理实现。

## 2.3 官方 ComfyUI 已沉淀的工程能力

官方 ComfyUI 插件不是模型本体，但已包含一套 CF-specific embedded runtime：

```text
ControlFoleyRuntime 数据结构
模型和视频预处理缓存
BF16 / FP16 / FP32
torch.compile 探测
CUDA-only 保护
参考音频 dtype 对齐补丁
BigVGAN 兼容补丁
取消检查
模型卸载
权重和源码发现
WAV / FLAC 保存
视频封装
```

这些经验应迁入独立 Adapter / Runtime，而不是继续依赖其 `nodes.py`。

## 2.4 ControlFoley 的关键计算事实

官方 `generate()` 先进行：

```text
CLIP 视频编码
CAV-MAE 视频编码
Synchformer 同步编码
CLAP 参考音频编码
MusicGen-style timbre 编码
文本编码
condition projection
```

之后才进入 Flow Matching 循环。模型源码还明确将 `preprocess_conditions()` 描述为“不依赖 latent/time step、在推理 steps 间复用的特征”。

默认 `num_steps=25` 时，Euler 求解器执行 25 次 step；默认 CFG 大于等于 1 时，每 step 分别执行 conditional 与 empty/unconditional `predict_flow()`，因此大约是：

```text
25 × 2 = 50 次主干 predict_flow
```

这决定了性能路线：

- 重复视频/参考音频：L2 encoder feature cache 有价值；
- 单个全新请求：主要瓶颈仍可能是 50 次主干计算；
- 真正的单请求大幅加速属于后续 solver/compile/kernel 研究，而不是 V1 Asset Cache。

## 2.5 必须治理的可变状态

当前上游存在模型实例级可变状态：

```text
seq_cfg.total_time_seconds
net.update_seq_lengths(...)
net 内部 sequence lengths
RoPE rotations
MusicGen style conditioner parameters
model_cfg.model_path
```

所以 V1 必须采用：

```text
一个 GPU Worker 进程
一个固定 ModelDeployment
一个常驻 ModelSession
同一时刻一个 generate
```

不能把单个模型实例当成线程安全对象。

---

# 3. 系统上下文与权威边界

## 3.1 总体架构

```text
                                  Control Plane

┌───────────────┐       ┌─────────────────────────────┐
│ CLI / Web App │──────▶│ API                         │
└───────────────┘       │ - Auth / limits             │
                        │ - Upload sessions            │
┌───────────────┐       │ - Job submission            │
│ ComfyUI Remote│──────▶│ - Status / cancellation     │
└───────────────┘       │ - Download authorization    │
                        └──────────────┬──────────────┘
                                       │
                                       ▼
                        ┌─────────────────────────────┐
                        │ PostgreSQL                  │
                        │ - assets / blobs            │
                        │ - jobs / attempts           │
                        │ - workers / artifacts       │
                        │ - events / leases           │
                        └──────────────┬──────────────┘
                                       │
                        ┌──────────────┴──────────────┐
                        ▼                             ▼
             ┌─────────────────────┐      ┌─────────────────────┐
             │ Blob Store          │      │ Asset Verifier      │
             │ Local / S3 / MinIO  │      │ Full SHA-256        │
             └─────────────────────┘      │ Media probe         │
                                          └─────────────────────┘

                                    Data Plane

                        ┌─────────────────────────────┐
                        │ GPU Worker                  │
                        │ - claim + lease             │
                        │ - local materialization     │
                        │ - SHA-256 recheck           │
                        │ - Runtime call              │
                        │ - output validation         │
                        │ - artifact publication      │
                        └──────────────┬──────────────┘
                                       │
                                       ▼
                        ┌─────────────────────────────┐
                        │ Audio Runtime Core          │
                        │ ModelSession / Config       │
                        │ Profiler / Cache / Cancel   │
                        └──────────────┬──────────────┘
                                       │
                                       ▼
                        ┌─────────────────────────────┐
                        │ ControlFoley Adapter        │
                        └──────────────┬──────────────┘
                                       │
                                       ▼
                        ┌─────────────────────────────┐
                        │ Original ControlFoley       │
                        │ PyTorch / CUDA              │
                        └─────────────────────────────┘
```

## 3.2 权威边界

| 事实 | 唯一权威 |
|---|---|
| 资产是否可用于任务 | PostgreSQL `assets.state=VERIFIED` |
| 资产内容身份 | 应用计算的全文件 SHA-256 |
| 对象字节位置 | BlobStore `storage_key` |
| Job 当前状态 | PostgreSQL `jobs` |
| 当前合法执行者 | `current_attempt_id + lease_epoch` |
| 哪次执行获胜 | `winning_attempt_id` |
| 哪个产物可下载 | `artifacts.state=READY` 且属于 winning attempt |
| 模型执行语义 | Runtime / Adapter |
| UI 工作流状态 | 不是业务权威 |
| S3 ETag | 仅对象存储元数据，不是内容身份权威 |

## 3.3 关键不变量

```text
INV-01  只有 VERIFIED asset 可以成为 job input。
INV-02  每个 job 同时最多有一个 current attempt。
INV-03  每个 attempt 必须携带单调递增的 lease_epoch。
INV-04  stale worker 不能 heartbeat、finalize 或发布 READY artifact。
INV-05  每个 job 最多一个 winning_attempt_id。
INV-06  SUCCEEDED 必须蕴含所有 required artifacts 均 READY。
INV-07  READY artifact 必须已经通过字节和媒体校验。
INV-08  canonical blob key 对应不可变 SHA-256 内容。
INV-09  相同 idempotency key + 不同 request hash 必须冲突。
INV-10  API 请求中的文件名绝不能变成 Worker 任意本地路径。
INV-11  ModelDeployment fingerprint 创建后不可变。
INV-12  移除 ComfyUI 后，Core、API、Worker 和 CLI 测试仍通过。
```

---

# 4. 替代框架审查

## 4.1 ComfyUI 作为主执行引擎

**结论：不选为 Durable Service 主链。**

优点：

```text
工作流编辑体验优秀
本地调试方便
已有 ControlFoley 官方集成
```

不足：

```text
核心抽象是 workflow execution，不是 Asset/Job/Attempt/Artifact 业务状态机
可靠上传、全文件 SHA、lease fencing、winning attempt 仍需外置实现
把它放在 Worker 与原模型之间会增加不必要依赖
```

保留为 optional frontend。

## 4.2 PGMQ

PGMQ 提供 Postgres 上的 visibility timeout 队列，语义接近 SQS，适合作为可选 QueueBackend。

V1 不采用扩展，原因：

```text
Job/Attempt/Artifact 本就需要自有领域表和事务
普通 PostgreSQL 已可用 SKIP LOCKED 实现 claim
避免要求用户安装数据库扩展
```

即便未来采用 PGMQ，其 message delivery 也不能把外部 GPU 推理变成 exactly-once；winning artifact 仍须依赖领域数据库 CAS。

## 4.3 Temporal

适合：

```text
长活动
heartbeat
retry
cancellation
复杂 durable workflow
```

V1 暂不采用，原因：

```text
单 GPU reference implementation 的状态机可由 PostgreSQL 清晰证明
Temporal 会引入新的独立控制面和历史存储
若同时保留业务 DB，需要重新定义双重权威边界
```

当项目出现多阶段跨服务工作流、人工审核、长链补偿时再评估。

## 4.4 BentoML Async Tasks

BentoML 已提供 submit/status/get/cancel/retry 风格的异步任务接口，适合长时间媒体生成。但其文档同时说明，本地部署时的请求队列和临时存储是非持久内存、主要面向开发；业务级 Asset SHA、Attempt fencing、Artifact publication 仍需补齐。

可在后续提供 BentoML deployment adapter，但不作为 V1 状态权威。

## 4.5 LitServe

LitServe 适合快速构建自定义 Python inference server，并提供 batching、streaming、routing 和 GPU serving 能力。

它可以替代 API 到模型之间的在线 serving 外壳，但不自动解决：

```text
可靠大文件上传
内容寻址资产
持久 Job/Attempt
lease fencing
产物原子发布
```

因此列为未来 ServingBackend，而不是 Durable Control Plane。

## 4.6 tus / tusd

`tusd` 是可恢复上传协议的参考服务器，支持本地、S3 和 S3-compatible 存储。

适合网络不稳定、超大文件和跨浏览器续传。V1 默认先实现 presigned S3 single/multipart，以减少组件；后续可增加 `TusUploadBackend`。

注意：tus 的 chunk/extension checksum 不能替代本项目所要求的最终全文件 SHA-256。

## 4.7 Celery / Redis / RabbitMQ

V1 不选，主要是避免：

```text
Broker delivery state
+
PostgreSQL Job state
```

之间的双写与恢复歧义。未来如果规模需要，可以实现 QueueBackend，但 Job/Attempt/Artifact 权威仍保持在 PostgreSQL。

---

# 5. Source Register


研究日期：2026-08-15。实施前应重新检查上游 HEAD 与文档变更。

## ControlFoley / ComfyUI

1. Xiaomi Research ControlFoley  
   https://github.com/xiaomi-research/controlfoley  
   支持基线：`6858cd12a48d141201e3266e7abe1f38357a133e`  
   研究时 main：`68cefb5dde12c5492a4a271a857b2acd17bc473a`

2. Official ComfyUI integration  
   https://github.com/YJX-Research/comfyui-controlfoley-official  
   研究时 main：`5de7a7e784f50f39c8404d72b3b20ebd99a660d6`

## PostgreSQL

3. PostgreSQL 18 `SELECT` / `FOR UPDATE SKIP LOCKED`  
   https://www.postgresql.org/docs/18/sql-select.html

4. PostgreSQL 18 `LISTEN` / `NOTIFY`  
   https://www.postgresql.org/docs/18/sql-listen.html  
   https://www.postgresql.org/docs/18/sql-notify.html

5. PostgreSQL 18 `UPDATE ... RETURNING`  
   https://www.postgresql.org/docs/18/sql-update.html

## Object integrity

6. S3 presigned URLs and upload checksums  
   https://docs.aws.amazon.com/AmazonS3/latest/userguide/using-presigned-url.html

7. S3 full-object vs composite checksums / multipart ETag  
   https://docs.aws.amazon.com/AmazonS3/latest/userguide/checking-object-integrity-upload.html

8. S3 conditional writes  
   https://docs.aws.amazon.com/AmazonS3/latest/userguide/conditional-writes.html

## Alternative backends

9. PGMQ  
   https://github.com/pgmq/pgmq

10. tusd  
    https://github.com/tus/tusd

11. Temporal Python SDK  
    https://github.com/temporalio/sdk-python

12. BentoML async task queues  
    https://github.com/bentoml/BentoML

13. LitServe  
    https://github.com/Lightning-AI/LitServe

## PyTorch

14. `torch.compile` recompilation and dynamic shapes  
    https://docs.pytorch.org/docs/stable/user_guide/torch_compiler/compile/programming_model.recompilation.html

15. CUDA Event / asynchronous execution documentation  
    https://docs.pytorch.org/docs/stable/generated/torch.cuda.Event.html

---

# 6. 最终架构判定

这不是“给 ControlFoley 再包一层 ComfyUI”。

它是：

```text
一个可独立运行的 Model Runtime
+
一个可恢复的 Durable Inference Service
+
一个 ControlFoley Model Adapter
+
若干可替换 Frontend
```

项目的主链和开源价值应固定为：

```text
CLI/API
→ Verified Assets
→ Durable Jobs
→ Fenced GPU Worker
→ Original Model Runtime
→ Verified Artifacts
```

ComfyUI 很好用，但它是可选体验层；**删除 ComfyUI 之后，系统的可靠性、推理能力和产物语义必须完全不变。**
