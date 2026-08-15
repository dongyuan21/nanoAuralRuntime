# ControlFoley-first Headless Audio Runtime 与 Durable Inference Service 实施计划

> 文件名：`PLAN.md` 参考版  
> 计划版本：2.0  
> 状态：Implementation-ready / 未实施  
> 研究冻结日期：2026-08-15  
> 首个硬件基线：单卡 NVIDIA RTX 4090 24 GB  
> 首个模型适配：Xiaomi Research ControlFoley  
> 主要执行入口：CLI / HTTP API / GPU Worker  
> 可选入口：ComfyUI Embedded / ComfyUI Remote  

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

# 4. 仓库与依赖结构

## 4.1 推荐 monorepo

```text
audio-runtime/
├── pyproject.toml
├── uv.lock                         # 或等价 lockfile
├── README.md
├── PLAN.md
├── LICENSE
├── THIRD_PARTY_NOTICES.md
├── MODEL_LICENSES.md
├── SECURITY.md
├── CHANGELOG.md
│
├── packages/
│   ├── contracts/
│   │   └── src/audio_runtime_contracts/
│   │       ├── enums.py
│   │       ├── assets.py
│   │       ├── jobs.py
│   │       ├── artifacts.py
│   │       ├── runtime.py
│   │       └── errors.py
│   │
│   ├── runtime-core/
│   │   └── src/audio_runtime/
│   │       ├── runtime.py
│   │       ├── session.py
│   │       ├── lifecycle.py
│   │       ├── cancellation.py
│   │       ├── profiling/
│   │       ├── cache/
│   │       ├── media/
│   │       └── adapters/base.py
│   │
│   ├── adapter-controlfoley/
│   │   └── src/audio_runtime_controlfoley/
│   │       ├── adapter.py
│   │       ├── loader.py
│   │       ├── upstream.py
│   │       ├── staged.py
│   │       ├── inputs.py
│   │       ├── capabilities.py
│   │       ├── compat.py
│   │       └── manifest.py
│   │
│   └── client/
│       └── src/audio_runtime_client/
│           ├── client.py
│           ├── uploads.py
│           ├── jobs.py
│           └── cli.py
│
├── services/
│   ├── api/
│   │   └── src/audio_runtime_api/
│   │       ├── main.py
│   │       ├── routes/
│   │       ├── auth/
│   │       ├── db/
│   │       ├── storage/
│   │       └── settings.py
│   │
│   ├── verifier/
│   │   └── src/audio_runtime_verifier/
│   │       ├── worker.py
│   │       ├── checksum.py
│   │       └── media_probe.py
│   │
│   ├── gpu-worker/
│   │   └── src/audio_runtime_worker/
│   │       ├── main.py
│   │       ├── claim.py
│   │       ├── heartbeat.py
│   │       ├── executor.py
│   │       ├── validation.py
│   │       ├── publication.py
│   │       └── recovery.py
│   │
│   └── janitor/
│       └── src/audio_runtime_janitor/
│           ├── leases.py
│           ├── uploads.py
│           └── orphans.py
│
├── integrations/
│   └── comfyui/
│       ├── embedded_nodes.py
│       ├── remote_nodes.py
│       ├── __init__.py
│       └── example_workflows/
│
├── migrations/
│   └── versions/
│
├── deploy/
│   ├── compose/
│   │   └── docker-compose.yml
│   ├── docker/
│   │   ├── api.Dockerfile
│   │   ├── verifier.Dockerfile
│   │   └── worker.Dockerfile
│   └── examples/
│       ├── .env.example
│       └── model-deployment.example.yaml
│
├── tests/
│   ├── unit/
│   ├── contract/
│   ├── integration/
│   ├── storage/
│   ├── parity/
│   ├── gpu/
│   ├── recovery/
│   └── fixtures/
│
├── benchmarks/
│   ├── controlfoley_baseline.py
│   ├── runtime_matrix.py
│   └── compare.py
│
└── docs/
    ├── architecture.md
    ├── operations.md
    ├── api.md
    ├── controlfoley-adapter.md
    ├── cache.md
    ├── profiling.md
    └── adr/
```

## 4.2 依赖方向

```text
contracts
   ↑
   ├── API
   ├── client / CLI
   ├── worker
   └── ComfyUI Remote

runtime-core
   ↑
adapter-controlfoley
   ↑
   ├── GPU Worker
   ├── local CLI
   └── ComfyUI Embedded
```

硬规则：

```text
runtime-core 不 import FastAPI、SQLAlchemy、boto3、ComfyUI。
adapter-controlfoley 不 import API、数据库、对象存储、ComfyUI。
API 镜像不安装 torch、CUDA 或 ControlFoley。
Verifier 镜像不安装 CUDA。
只有 GPU Worker / Embedded ComfyUI 安装模型依赖。
```

---

# 5. Model Runtime Core

## 5.1 核心契约

```python
class AudioGenerationAdapter(Protocol):
    def capabilities(self) -> ModelCapabilities: ...
    def load(self, config: ModelLoadConfig) -> ModelSession: ...
    def prepare_inputs(
        self,
        session: ModelSession,
        request: GenerateRequest,
        context: ExecutionContext,
    ) -> PreparedInputs: ...
    def encode_conditions(
        self,
        session: ModelSession,
        prepared: PreparedInputs,
        context: ExecutionContext,
    ) -> ConditionBundle: ...
    def sample(
        self,
        session: ModelSession,
        conditions: ConditionBundle,
        request: GenerateRequest,
        context: ExecutionContext,
    ) -> LatentResult: ...
    def decode(
        self,
        session: ModelSession,
        latent: LatentResult,
        context: ExecutionContext,
    ) -> AudioTensor: ...
    def unload(self, session: ModelSession) -> None: ...
```

## 5.2 ModelLoadConfig

```python
@dataclass(frozen=True)
class ModelLoadConfig:
    adapter: str
    variant: str
    source_dir: Path
    source_revision: str
    weights_dir: Path
    checkpoint_sha256: str
    device: str = "cuda"
    precision: str = "bf16"
    allow_tf32: bool = True
    compile_encoders: bool = False
    compile_generator: bool = False
    low_vram: bool = False
```

部署配置是服务端固定配置。远程用户不能修改：

```text
source_dir
weights_dir
source_revision
checkpoint
precision profile
compile flags
```

## 5.3 GenerateRequest

```python
@dataclass(frozen=True)
class GenerateRequest:
    request_id: str
    task: GenerationTask
    video_path: Path | None
    reference_audio_path: Path | None
    prompt: str
    negative_prompt: str
    duration_seconds: float
    seed: int
    num_steps: int
    guidance_scale: float
    mask_away_clip: bool = False
    clip_batch_size_multiplier: int = 8
    sync_batch_size_multiplier: int = 8
```

其中 `video_path` 和 `reference_audio_path` 只能由 Worker 将 **已验证 Asset** materialize 到 attempt workspace 后构造；API 不能直接赋值。

## 5.4 GenerateResult

```python
@dataclass
class GenerateResult:
    waveform: torch.Tensor            # CPU, float32, [channels, samples]
    sample_rate: int
    actual_duration_seconds: float
    runtime_manifest: RuntimeManifest
    profile: ProfileReport
    cache_report: CacheReport
    warnings: list[str]
```

## 5.5 ModelSession 状态机

```text
UNLOADED
   │
   ▼
LOADING
   │
   ▼
READY
   │
   ▼
RUNNING
   │
   ├── success ──────▶ READY
   ├── request error ▶ READY
   └── runtime fault ▶ FAILED
                         │
                         ▼
                     UNLOADING
                         │
                         ▼
                     UNLOADED
```

规则：

```text
同一 Session max_concurrency=1。
RUNNING 时禁止 unload。
FAILED Session 禁止继续接新 Job。
CUDA context fault 后 Worker 应退出，由进程监督器重启。
每个 Session deep-copy temporal config。
不得修改上游全局 ModelConfig。
```

## 5.6 CancellationToken

Runtime 接收一个同步、线程安全的 CancellationToken：

```python
class CancellationToken:
    def is_cancelled(self) -> bool: ...
    def raise_if_cancelled(self) -> None: ...
```

Worker heartbeat 线程发现以下任一条件时触发 token：

```text
job.cancel_requested_at 非空
heartbeat CAS 返回 0 行
lease 已失效
Worker shutdown
```

ControlFoley Euler loop 每 N steps 检查一次。由于 CUDA 异步提交，首版参考官方 ComfyUI 的做法，每 4 个 steps 做一次 GPU 同步后再检查取消；该频率必须进入 benchmark，不可写成未经测量的永久常数。

---

# 6. ControlFoley Adapter

## 6.1 两种执行模式

### `upstream_parity`

```text
Runtime
→ Adapter
→ 官方 inference_utils.generate()
```

用途：

```text
官方行为 oracle
V1 首先跑通
建立 parity envelope
验证模型加载、seed、dtype、生命周期
```

### `staged`

```text
resolve media
→ video preprocessing
→ reference preprocessing
→ CLIP
→ CAV-MAE
→ Synchformer
→ CLAP
→ timbre encoder
→ text encoders
→ condition projection
→ latent initialization
→ Flow Matching integration
→ unnormalize
→ VAE decode
→ vocoder
→ CPU postprocess
```

用途：

```text
阶段 Profiler
L1/L2 Cache
分阶段 offload
后续 compile region
后端替换
```

只有 staged 与 upstream_parity 通过输出 parity 后，staged 才能成为默认路径。

## 6.2 源码加载约束

一个 Worker 进程只加载一个 ControlFoley source revision。

加载时必须：

```text
校验必需文件存在
校验 git revision
校验 checkpoint SHA-256
校验第三方模型 snapshot/revision manifest
校验 controlfoley.__file__ 来源
校验 CUDA 可用
校验 precision capability
```

若 `sys.modules` 已加载另一个路径的 `controlfoley`，立即失败，不尝试热切换模块。

## 6.3 从官方 ComfyUI 迁移的兼容逻辑

迁入：

```text
BF16/FP16 timbre tensor 与 projection dtype/device 对齐
BigVGAN from_pretrained 参数兼容
CUDA-only capability guard
torch.compile / Triton capability probe
Flow Matching cancel hook
参考音频 16 kHz / 32 kHz 预处理
2–4 秒 timbre pad/truncate 规则
```

不迁入：

```text
folder_paths
ComfyUI VIDEO/IMAGE 类型
ComfyUI output 目录
ComfyUI UI preview
ComfyUI execution cache
ComfyUI IS_CHANGED
ComfyUI-specific unload epoch
```

## 6.4 模型部署 Manifest

```json
{
  "deployment_id": "cf-large44k-bf16-v1",
  "adapter_version": "0.1.0",
  "runtime_version": "0.1.0",
  "source": {
    "repository": "xiaomi-research/controlfoley",
    "revision": "6858cd12a48d141201e3266e7abe1f38357a133e"
  },
  "variant": "large_44k",
  "precision": "bf16",
  "checkpoint_sha256": "...",
  "external_weights": [
    {"path": "ext_weights/v1-44.pth", "sha256": "..."}
  ],
  "hf_snapshots": [
    {"repo_id": "...", "revision": "..."}
  ],
  "capabilities": {
    "cuda": true,
    "ac_v2a": true,
    "staged_offload": false
  }
}
```

Worker readiness 只有在 Manifest 所有对象校验完成后才返回 ready。

---

# 7. 可靠上传与内容寻址 Asset

## 7.1 上传主链

```text
CLI 计算 size + 全文件 SHA-256
       ↓
POST /v1/uploads
       ↓
API 创建 upload_session 和 staging key
       ↓
CLI 使用 presigned URL 直传 BlobStore
       ↓
POST /v1/uploads/{id}/complete
       ↓
Verifier 流式读取对象并重新计算全文件 SHA-256
       ↓
媒体探测 / 类型 / 时长 / 解码验证
       ↓
提升为 canonical content-addressed blob
       ↓
创建 VERIFIED asset
```

大文件不应先经过 API 进程。

## 7.2 为什么应用 SHA-256 必须独立保存

### Single PUT

可以通过 SigV4 presigned URL 携带 SHA-256 checksum，并由 S3 验证传输完整性。

### Multipart

S3 的 multipart SHA-256 是 composite checksum，不是可直接等同于原始全文件 SHA-256；multipart ETag 也不是全文件 MD5。因此：

```text
S3 checksum 负责对象存储层传输校验；
应用全文件 SHA-256 负责内容身份、去重、Worker 再验证和下载验证。
```

Verifier 必须流式读完整对象并计算：

```python
hashlib.sha256()
```

不把 ETag 或 composite checksum 写进 `blobs.sha256`。

## 7.3 BlobStore 接口

```python
class BlobStore(Protocol):
    def create_upload(...): ...
    def sign_put(...): ...
    def create_multipart_upload(...): ...
    def sign_upload_part(...): ...
    def complete_multipart_upload(...): ...
    def stat(...): ...
    def open_reader(...): ...
    def put_file_verified(...): ...
    def copy_if_absent(...): ...
    def delete(...): ...
    def sign_download(...): ...
```

实现：

```text
LocalBlobStore：开发、单机测试
S3BlobStore：AWS S3
S3CompatibleBlobStore：MinIO 等；应用 verifier 保持权威
```

## 7.4 Canonical key

```text
staging/uploads/{upload_session_id}/payload

blobs/sha256/{digest[0:2]}/{digest[2:4]}/{digest}
```

提升规则：

1. staging 对象验证通过；
2. 尝试 `copy_if_absent` 到 canonical key；
3. S3 使用 `If-None-Match: *` 防止并发覆盖；
4. 如果 key 已存在，校验 DB 中 size 和 digest 一致后复用；
5. 创建或复用 `blobs` 记录；
6. 删除 staging 对象。

## 7.5 Worker 二次验证

Worker 下载 Asset 后，仍须验证：

```text
size_bytes
full SHA-256
媒体可解码性
媒体 metadata 与提交记录一致性
```

这验证的是完整路径：

```text
客户端文件
→ 上传
→ 对象存储
→ 下载
→ Worker 本地文件
```

---

# 8. PostgreSQL 数据模型

以下是逻辑 schema；实现时用 migration 固化 enum/check/unique/index。

## 8.1 `model_deployments`

```text
id UUID PK
name TEXT UNIQUE
adapter TEXT
variant TEXT
runtime_version TEXT
adapter_version TEXT
source_repository TEXT
source_revision TEXT
checkpoint_sha256 CHAR(64)
manifest JSONB
state ENUM(REGISTERED, READY, UNHEALTHY, RETIRED)
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
```

## 8.2 `upload_sessions`

```text
id UUID PK
namespace_id TEXT
asset_kind ENUM(VIDEO, REFERENCE_AUDIO, OTHER_AUDIO)
original_filename TEXT
expected_size_bytes BIGINT
expected_sha256 CHAR(64)
staging_key TEXT UNIQUE
backend_upload_id TEXT NULL
state ENUM(CREATED, UPLOADING, UPLOADED, VERIFYING, COMPLETED, REJECTED, EXPIRED)
error_code TEXT NULL
error_detail JSONB NULL
expires_at TIMESTAMPTZ
created_at TIMESTAMPTZ
completed_at TIMESTAMPTZ NULL
version BIGINT
```

## 8.3 `blobs`

```text
id UUID PK
sha256 CHAR(64) UNIQUE
size_bytes BIGINT
storage_backend TEXT
storage_key TEXT UNIQUE
content_type TEXT
state ENUM(STAGING, VERIFIED, QUARANTINED, DELETING, DELETED)
created_at TIMESTAMPTZ
verified_at TIMESTAMPTZ NULL
```

## 8.4 `assets`

```text
id UUID PK
namespace_id TEXT
blob_id UUID FK blobs
kind ENUM(VIDEO, REFERENCE_AUDIO, GENERATED_AUDIO, GENERATED_VIDEO)
original_filename TEXT
media_metadata JSONB
state ENUM(PENDING, VERIFIED, REJECTED, DELETED)
created_at TIMESTAMPTZ
verified_at TIMESTAMPTZ NULL
```

## 8.5 `jobs`

```text
id UUID PK
namespace_id TEXT
idempotency_key TEXT
request_sha256 CHAR(64)
request_json JSONB
model_deployment_id UUID FK
state ENUM(QUEUED, RUNNING, FINALIZING, SUCCEEDED, FAILED, CANCELLED)
priority INTEGER
attempt_count INTEGER
max_attempts INTEGER
lease_epoch BIGINT
current_attempt_id UUID NULL
winning_attempt_id UUID NULL
next_eligible_at TIMESTAMPTZ
cancel_requested_at TIMESTAMPTZ NULL
failure_code TEXT NULL
failure_detail JSONB NULL
version BIGINT
created_at TIMESTAMPTZ
updated_at TIMESTAMPTZ
started_at TIMESTAMPTZ NULL
completed_at TIMESTAMPTZ NULL
UNIQUE(namespace_id, idempotency_key)
```

## 8.6 `job_inputs`

```text
job_id UUID FK
role ENUM(VIDEO, REFERENCE_AUDIO)
asset_id UUID FK
PRIMARY KEY(job_id, role)
```

## 8.7 `job_attempts`

```text
id UUID PK
job_id UUID FK
attempt_no INTEGER
worker_id UUID
lease_epoch BIGINT
state ENUM(ACTIVE, SUCCEEDED, FAILED_RETRYABLE, FAILED_TERMINAL,
           CANCELLED, LEASE_EXPIRED, SUPERSEDED)
phase ENUM(CLAIMED, MATERIALIZING_INPUTS, VERIFYING_INPUTS,
           LOADING_MODEL, INFERENCING, VALIDATING_OUTPUTS,
           UPLOADING_ARTIFACTS, FINALIZING)
lease_expires_at TIMESTAMPTZ
heartbeat_at TIMESTAMPTZ
started_at TIMESTAMPTZ
finished_at TIMESTAMPTZ NULL
error_code TEXT NULL
error_detail JSONB NULL
runtime_manifest JSONB NULL
profile JSONB NULL
UNIQUE(job_id, attempt_no)
```

## 8.8 `artifacts`

```text
id UUID PK
job_id UUID FK
attempt_id UUID FK
kind ENUM(AUDIO_WAV, AUDIO_FLAC, VIDEO_MP4, MANIFEST_JSON, PROFILE_JSON)
blob_id UUID FK NULL
state ENUM(STAGING, VERIFYING, READY, REJECTED, ORPHANED)
media_metadata JSONB
created_at TIMESTAMPTZ
verified_at TIMESTAMPTZ NULL
UNIQUE(attempt_id, kind)
```

## 8.9 `workers`

```text
id UUID PK
worker_group TEXT
gpu_uuid TEXT
model_deployment_id UUID FK
state ENUM(STARTING, READY, BUSY, DRAINING, UNHEALTHY, STOPPED)
current_attempt_id UUID NULL
software_manifest JSONB
last_heartbeat_at TIMESTAMPTZ
started_at TIMESTAMPTZ
```

## 8.10 `job_events`

```text
id BIGSERIAL PK
job_id UUID FK
attempt_id UUID NULL
event_type TEXT
payload JSONB
created_at TIMESTAMPTZ
```

`job_events` 是追加式审计，不是主状态权威。

## 8.11 必需索引

```sql
CREATE INDEX jobs_claim_idx
ON jobs (priority DESC, next_eligible_at, created_at, id)
WHERE state = 'QUEUED';

CREATE INDEX attempts_expired_idx
ON job_attempts (lease_expires_at)
WHERE state = 'ACTIVE';

CREATE INDEX upload_verify_idx
ON upload_sessions (created_at, id)
WHERE state = 'UPLOADED';

CREATE INDEX artifacts_job_idx
ON artifacts (job_id, state);
```

---

# 9. Job / Attempt 状态机

## 9.1 Job

```text
                    ┌───────────────┐
                    │    QUEUED     │
                    └───────┬───────┘
                            │ claim
                            ▼
                    ┌───────────────┐
              ┌────▶│    RUNNING    │────┐
              │     └───────┬───────┘    │
              │             │ output     │ cancel / terminal
              │             ▼            │
 retry/backoff│     ┌───────────────┐    │
              │     │  FINALIZING   │────┤
              │     └───────┬───────┘    │
              │             │ CAS win    │
              │             ▼            ▼
              │     ┌───────────────┐  ┌───────────────┐
              └─────│   SUCCEEDED   │  │FAILED/CANCELLED│
                    └───────────────┘  └───────────────┘
```

## 9.2 Attempt

```text
ACTIVE
  ├── SUCCEEDED
  ├── FAILED_RETRYABLE
  ├── FAILED_TERMINAL
  ├── CANCELLED
  ├── LEASE_EXPIRED
  └── SUPERSEDED
```

Attempt phase 单调前进，不允许倒退。

## 9.3 推理不是 exactly-once

故障场景：

```text
模型已经生成
→ Artifact 已上传
→ Worker 在 DB finalization 前崩溃
```

重试可能再次执行模型。因此 V1 明确追求：

```text
at-least-once attempt execution
+
exactly-one visible winning result
```

不使用“exactly-once inference”作为承诺。

---

# 10. Claim、Lease、Heartbeat 与 Fencing

## 10.1 Claim 事务

伪 SQL：

```sql
BEGIN;

SELECT id, attempt_count, lease_epoch
FROM jobs
WHERE state = 'QUEUED'
  AND next_eligible_at <= now()
  AND cancel_requested_at IS NULL
  AND model_deployment_id = :deployment_id
ORDER BY priority DESC, created_at ASC, id ASC
FOR UPDATE SKIP LOCKED
LIMIT 1;

-- Application generates :attempt_id.

INSERT INTO job_attempts (
  id, job_id, attempt_no, worker_id, lease_epoch,
  state, phase, lease_expires_at, heartbeat_at, started_at
) VALUES (
  :attempt_id, :job_id, :attempt_no, :worker_id, :next_epoch,
  'ACTIVE', 'CLAIMED', now() + :lease_interval, now(), now()
);

UPDATE jobs
SET state = 'RUNNING',
    attempt_count = :attempt_no,
    lease_epoch = :next_epoch,
    current_attempt_id = :attempt_id,
    started_at = COALESCE(started_at, now()),
    updated_at = now(),
    version = version + 1
WHERE id = :job_id;

COMMIT;
```

`SKIP LOCKED` 适合 queue-like table 的多个消费者，但返回的是不一致视图，所以只能用于领取候选任务，不能用于业务报表或状态查询。

## 10.2 Heartbeat CAS

```sql
UPDATE job_attempts AS a
SET heartbeat_at = now(),
    lease_expires_at = now() + :lease_interval,
    phase = :phase
FROM jobs AS j
WHERE a.id = :attempt_id
  AND a.worker_id = :worker_id
  AND a.lease_epoch = :lease_epoch
  AND a.state = 'ACTIVE'
  AND j.id = a.job_id
  AND j.current_attempt_id = a.id
  AND j.lease_epoch = a.lease_epoch
  AND j.state IN ('RUNNING', 'FINALIZING')
RETURNING j.cancel_requested_at;
```

语义：

```text
返回 1 行：仍持有 lease；读取 cancel_requested_at。
返回 0 行：已失去执行权，立即触发本地 cancellation token。
```

## 10.3 Lease Reaper

Janitor 定期：

1. `FOR UPDATE SKIP LOCKED` 领取过期 ACTIVE attempts；
2. 标记 `LEASE_EXPIRED`；
3. 仅当 `jobs.current_attempt_id` 仍匹配时处理 Job；
4. 有取消请求则 Job → CANCELLED；
5. 未超过 `max_attempts` 则 Job → QUEUED，设置 backoff；
6. 否则 Job → FAILED；
7. 清空 `current_attempt_id`；
8. 记录 `job_events`。

## 10.4 stale worker

旧 Worker 即便 GPU 计算最终结束，也无法：

```text
续 lease
把 artifact 设为 READY
把 Job 设为 SUCCEEDED
```

因为所有写入都要求：

```text
attempt_id
worker_id
lease_epoch
current_attempt_id
```

同时匹配。

---

# 11. GPU Worker 执行主链

## 11.1 启动

```text
读取固定 ModelDeployment
校验 source / weights / snapshots
注册 worker
加载 ModelSession
可选 warmup
上报 READY
启动 claim loop
```

## 11.2 单个 Attempt

```text
1. claim job + fencing epoch
2. 创建独立 workspace
3. 启动 heartbeat thread
4. 下载所有 verified input blobs
5. 本地重算 SHA-256
6. 媒体 probe / decode 验证
7. 构造 GenerateRequest
8. 调 runtime.generate()
9. 写本地临时 Artifact
10. 校验音频/视频内容
11. 计算 Artifact SHA-256
12. 上传 attempt-specific immutable key
13. 验证远端对象
14. DB 进入 FINALIZING
15. CAS 发布 artifact + winning attempt + SUCCEEDED
16. 清理 workspace
```

Attempt key：

```text
jobs/{job_id}/attempts/{attempt_id}/audio.wav
jobs/{job_id}/attempts/{attempt_id}/output.mp4
jobs/{job_id}/attempts/{attempt_id}/manifest.json
```

禁止所有 attempt 覆盖：

```text
jobs/{job_id}/output.wav
```

## 11.3 Worker 伪代码

```python
def execute_attempt(ctx: AttemptContext) -> None:
    with heartbeat(ctx) as cancellation:
        workspace = create_workspace(ctx.attempt_id)

        inputs = materialize_assets(
            ctx.job_inputs,
            workspace=workspace,
            cancellation=cancellation,
        )
        verify_input_hashes(inputs)
        probe_input_media(inputs)

        request = build_generate_request(ctx.job, inputs)
        result = runtime.generate(
            request,
            cancellation=cancellation,
        )

        local_artifacts = write_outputs(result, workspace)
        validated = validate_outputs(local_artifacts, result)
        uploaded = publish_attempt_objects(ctx, validated)

        finalize_with_cas(ctx, result, uploaded)
```

## 11.4 进程故障策略

| 故障 | Worker 行为 | Job 行为 |
|---|---|---|
| 输入无效 | 保持进程 | terminal failure |
| 对象存储短暂超时 | 保持进程 | retryable |
| CUDA OOM | 尽力记录后主动退出 | 同部署默认 terminal；有备用 profile 才 retry |
| CUDA illegal access/context fault | 主动退出 | fresh worker 上最多受控重试一次 |
| 模型加载失败 | worker UNHEALTHY | deployment UNHEALTHY；停止 claim |
| DB 连接短暂失败 | 暂停 claim/heartbeat 重连 | lease 可能过期并由 reaper 恢复 |

---

# 12. Artifact 验证与原子发布

## 12.1 音频硬校验

```text
文件存在且 size > 0
可重新打开并完整读取
sample rate 与 Runtime Result 一致
channel count 在允许范围
sample count > 0
全部 sample finite
无 NaN / Inf
实际 duration 在容差内
成功计算 full SHA-256
```

以下默认作为 warning，不作为硬失败：

```text
RMS 极低
全零或接近全零
峰值过高
clipping 比例过高
DC offset 异常
```

原因：某些合法输入可能自然产生近静音输出，质量规则不能与字节/结构完整性混为一谈。

## 12.2 视频硬校验

```text
容器可打开
至少一个视频流
至少一个音频流
所有 packet 可 demux
音视频 duration 合理
音频 sample rate / channels 合法
full SHA-256 成功
```

## 12.3 发布事务

前置：Artifact 字节已在 attempt-specific key，且远端验证通过。

```sql
BEGIN;

UPDATE jobs
SET state = 'FINALIZING',
    updated_at = now(),
    version = version + 1
WHERE id = :job_id
  AND state = 'RUNNING'
  AND current_attempt_id = :attempt_id
  AND lease_epoch = :lease_epoch
  AND cancel_requested_at IS NULL
RETURNING id;

-- Must return exactly one row.

INSERT INTO blobs (... verified artifact blob ...)
ON CONFLICT (sha256) DO ...;

INSERT INTO artifacts (..., state='READY', ...);

UPDATE job_attempts
SET state = 'SUCCEEDED',
    phase = 'FINALIZING',
    finished_at = now(),
    runtime_manifest = :manifest,
    profile = :profile
WHERE id = :attempt_id
  AND state = 'ACTIVE'
  AND lease_epoch = :lease_epoch;

UPDATE jobs
SET state = 'SUCCEEDED',
    winning_attempt_id = :attempt_id,
    current_attempt_id = NULL,
    completed_at = now(),
    updated_at = now(),
    version = version + 1
WHERE id = :job_id
  AND state = 'FINALIZING'
  AND current_attempt_id = :attempt_id
  AND lease_epoch = :lease_epoch
  AND cancel_requested_at IS NULL
RETURNING id;

COMMIT;
```

任一 CAS 返回 0 行：

```text
attempt → SUPERSEDED
artifact → ORPHANED
禁止对外可见
Janitor 后续删除对象
```

## 12.4 成功的定义

```text
模型返回 Tensor ≠ Job 成功
文件写出 ≠ Job 成功
对象上传完成 ≠ Job 成功

只有：
Artifact READY + winning attempt CAS 成功
才是 Job SUCCEEDED。
```

---

# 13. Idempotency、取消与重试

## 13.1 Job idempotency

客户端发送：

```http
Idempotency-Key: <client-generated-key>
```

服务端：

1. 校验 key 格式和长度；
2. 将已解析、补默认值后的请求生成 canonical representation；
3. 计算 `request_sha256`；
4. 事务内插入 `(namespace_id, idempotency_key)`；
5. 冲突时读取现有 Job。

结果：

```text
相同 key + 相同 request hash → 返回已有 Job。
相同 key + 不同 request hash → HTTP 409。
```

Idempotency-Key 的过期范围和保留期由本项目 API 契约定义，不依赖尚未成为正式 RFC 的草案语义。

## 13.2 Cancellation race

规则：

```text
QUEUED：API 可直接 CAS 到 CANCELLED。
RUNNING/FINALIZING：只写 cancel_requested_at，由 Worker 协作取消。
SUCCEEDED/FAILED/CANCELLED：终态后 cancel 返回 already_terminal。
```

若取消与 finalization 竞争：

- finalization 事务先提交：Job 成功，后续 cancel 无效；
- cancel 先写入：finalization 的 `cancel_requested_at IS NULL` CAS 失败，产物 orphaned，Job 取消。

数据库提交顺序决定唯一结果。

## 13.3 Retry taxonomy

### Terminal

```text
输入 SHA-256 不匹配
媒体不可解码
任务输入组合非法
不支持的模型能力
部署 source/checkpoint 不匹配
确定性的 output structural validation failure
权限或配额错误
```

### Retryable

```text
PostgreSQL 短暂不可用
BlobStore 短暂网络错误
Worker 进程退出
lease expiry
节点重启
受控的一次 fresh-process CUDA context retry
```

### 特殊处理

```text
CUDA OOM：同一部署配置重复通常不会改善；默认 terminal resource failure。
磁盘满：worker unhealthy，Job retryable 到其他健康节点；单节点则人工处理。
模型加载失败：deployment unhealthy，禁止任务无限重试。
```

## 13.4 Backoff

```text
next_eligible_at = now() + bounded_exponential_backoff(attempt_no)
```

加入 jitter。最大 attempt 数由 ModelDeployment 或 Job policy 限制，客户端不能任意提高。

---

# 14. API 契约

## 14.1 Uploads

```text
POST /v1/uploads
GET  /v1/uploads/{upload_id}
POST /v1/uploads/{upload_id}/parts
POST /v1/uploads/{upload_id}/complete
POST /v1/uploads/{upload_id}/abort
GET  /v1/assets/{asset_id}
```

创建请求：

```json
{
  "kind": "video",
  "filename": "input.mp4",
  "size_bytes": 19382731,
  "sha256": "<64 hex>"
}
```

创建响应：

```json
{
  "upload_id": "...",
  "mode": "single_put",
  "upload_url": "<presigned>",
  "required_headers": {
    "x-amz-checksum-sha256": "..."
  },
  "expires_at": "..."
}
```

## 14.2 Jobs

```text
POST /v1/jobs
GET  /v1/jobs/{job_id}
GET  /v1/jobs/{job_id}/events
POST /v1/jobs/{job_id}/cancel
GET  /v1/jobs/{job_id}/artifacts
```

提交：

```json
{
  "model_deployment": "cf-large44k-bf16-v1",
  "task": "ac_v2a",
  "inputs": {
    "video_asset_id": "asset_video_01",
    "reference_audio_asset_id": "asset_audio_01"
  },
  "generation": {
    "prompt": "",
    "negative_prompt": "",
    "duration_seconds": 8.0,
    "seed": 42,
    "num_steps": 25,
    "guidance_scale": 4.5,
    "mask_away_clip": false
  },
  "outputs": {
    "audio_format": "wav",
    "mux_video": true
  }
}
```

Job 创建事务必须锁定并确认所有输入 asset 为 VERIFIED。

## 14.3 Artifacts

```text
GET  /v1/artifacts/{artifact_id}
POST /v1/artifacts/{artifact_id}/download-url
```

只允许为：

```text
artifact.state=READY
AND artifact.attempt_id=job.winning_attempt_id
```

签发下载 URL。

## 14.4 状态观看

V1：

```text
CLI 指数退避轮询 GET /jobs/{id}
```

后续可增加：

```text
SSE / WebSocket
```

但不把长连接本身作为状态权威。

---

# 15. CLI

## 15.1 Local 模式

```bash
audio-runtime local generate \
  --adapter controlfoley \
  --task ac-v2a \
  --video input.mp4 \
  --reference-audio metal.wav \
  --duration 8 \
  --steps 25 \
  --guidance 4.5 \
  --seed 42 \
  --output result.wav
```

链路：

```text
CLI → Runtime Core → Adapter → 原模型
```

## 15.2 Remote 模式

```bash
audio-runtime upload input.mp4
audio-runtime upload metal.wav

audio-runtime jobs submit \
  --task ac-v2a \
  --video-asset asset_x \
  --reference-audio-asset asset_y \
  --idempotency-key run-20260815-001

audio-runtime jobs watch job_x
audio-runtime jobs cancel job_x

audio-runtime artifacts download \
  --job job_x \
  --kind audio_wav \
  --output result.wav \
  --verify-sha256
```

## 15.3 运维命令

```bash
audio-runtime model verify --manifest deployment.json
audio-runtime cache inspect
audio-runtime cache clear --level l1
audio-runtime admin jobs requeue <job-id>
audio-runtime admin artifacts sweep-orphans --dry-run
```

危险 admin 命令必须与普通用户凭证分离。

---

# 16. Profiler 与 Cache 路线

## 16.1 Profiler

Profile levels：

```text
off
summary
stages
trace
```

Stage 计时：

```text
CPU：time.perf_counter()
GPU：torch.cuda.Event
请求末统一 synchronize 后计算 CUDA elapsed time
```

禁止对每个普通 stage 都同步 GPU，否则测量本身会显著改变执行。

记录：

```text
model load / warmup
asset materialization
video decode / preprocess
CLIP / CAV-MAE / Synchformer
CLAP / timbre / text
condition projection
Flow integration
VAE / vocoder
CPU copy
artifact write / upload
peak allocated / peak reserved
```

## 16.2 Cache 层次

### L0 Metadata

```text
媒体时长
采样率
FPS
分辨率
资产 SHA-256
```

### L1 Preprocessing

```text
视频抽帧和 resize/normalize Tensor
16 kHz / 32 kHz reference waveform
```

### L2 Encoder Features

```text
CLIP
CAV-MAE
Synchformer
CLAP
MusicGen timbre
positive/negative text
```

### L3 Projected Conditions

绑定 checkpoint、duration、shape、prompt、reference、dtype，后置实现。

### L4 Step/Sampling Cache

```text
residual / attention / trajectory reuse
step skip
adaptive solver
```

属于 V2 research，可能改变质量，不与普通 Asset Cache 混名。

## 16.3 Cache key

必须包含：

```text
input asset full SHA-256
requested + effective duration
preprocess schema version
source revision
encoder checkpoint fingerprint
adapter version
dtype policy
mask flags
relevant model shape parameters
```

## 16.4 Cache 存储

V1：

```text
CPU Tensor byte-bounded LRU
可选 safetensors + JSON disk cache
GPU cache 默认关闭
```

4090 的 24 GB 显存优先留给模型和 sampling。

---

# 17. ComfyUI 可选集成

## 17.1 Embedded Nodes

```text
Audio Runtime ControlFoley Loader
Audio Runtime Generate
Audio Runtime Profile
Audio Runtime Cache Stats
Audio Runtime Save
Audio Runtime Mux
Audio Runtime Unload
```

节点只做：

```text
ComfyUI 类型
↔ Runtime contract
```

不复制模型执行代码。

## 17.2 Remote Nodes

```text
Audio Runtime Upload Asset
Audio Runtime Submit Job
Audio Runtime Job Status
Audio Runtime Fetch Artifact
```

Remote 节点不需要安装 ControlFoley 权重或 CUDA，只依赖 client package。

## 17.3 共存要求

```text
官方 ControlFoley ComfyUI 插件可同时安装。
Embedded 模式检测 controlfoley module origin 冲突。
Remote 模式不会 import controlfoley。
官方节点可作为 A/B baseline。
```

---

# 18. 替代框架审查

## 18.1 ComfyUI 作为主执行引擎

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

## 18.2 PGMQ

PGMQ 提供 Postgres 上的 visibility timeout 队列，语义接近 SQS，适合作为可选 QueueBackend。

V1 不采用扩展，原因：

```text
Job/Attempt/Artifact 本就需要自有领域表和事务
普通 PostgreSQL 已可用 SKIP LOCKED 实现 claim
避免要求用户安装数据库扩展
```

即便未来采用 PGMQ，其 message delivery 也不能把外部 GPU 推理变成 exactly-once；winning artifact 仍须依赖领域数据库 CAS。

## 18.3 Temporal

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

## 18.4 BentoML Async Tasks

BentoML 已提供 submit/status/get/cancel/retry 风格的异步任务接口，适合长时间媒体生成。但其文档同时说明，本地部署时的请求队列和临时存储是非持久内存、主要面向开发；业务级 Asset SHA、Attempt fencing、Artifact publication 仍需补齐。

可在后续提供 BentoML deployment adapter，但不作为 V1 状态权威。

## 18.5 LitServe

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

## 18.6 tus / tusd

`tusd` 是可恢复上传协议的参考服务器，支持本地、S3 和 S3-compatible 存储。

适合网络不稳定、超大文件和跨浏览器续传。V1 默认先实现 presigned S3 single/multipart，以减少组件；后续可增加 `TusUploadBackend`。

注意：tus 的 chunk/extension checksum 不能替代本项目所要求的最终全文件 SHA-256。

## 18.7 Celery / Redis / RabbitMQ

V1 不选，主要是避免：

```text
Broker delivery state
+
PostgreSQL Job state
```

之间的双写与恢复歧义。未来如果规模需要，可以实现 QueueBackend，但 Job/Attempt/Artifact 权威仍保持在 PostgreSQL。

---

# 19. 安全边界

```text
API 禁止提交本地路径、Python module、source_dir、weights_dir。
文件名仅作为 metadata，不参与服务器路径拼接。
上传限制 size、content type、duration、分辨率。
服务端执行媒体 sniff/probe，不信任扩展名。
每个 attempt 使用独立 workspace，拒绝路径穿越。
外部程序调用使用 argv，禁止 shell=True。
ffprobe/ffmpeg 设置超时和资源上限。
下载 URL 短期有效，最小权限。
对象存储 bucket 默认私有。
缓存 Tensor 使用 safetensors，不读取不可信 pickle。
模型权重使用 torch.load(weights_only=True)。
Worker 可在模型同步后关闭非必要网络出口。
API、Verifier、Worker 使用不同数据库角色和凭证。
日志不记录 presigned URL、token、完整本地路径或用户媒体内容。
```

首版认证至少提供：

```text
API Key / Bearer token
namespace isolation
per-key quota
```

公开互联网部署前补充正式身份系统和审计策略。

---

# 20. 可观测性与运维

## 20.1 结构化日志

所有日志字段：

```text
service
request_id
namespace_id
job_id
attempt_id
worker_id
lease_epoch
model_deployment_id
phase
event
error_code
duration_ms
```

## 20.2 Metrics

```text
queue_depth
oldest_queued_age_seconds
active_attempts
claim_latency_seconds
lease_expiry_total
retry_total
cancel_total
job_duration_seconds
inference_duration_seconds
model_load_seconds
artifact_validation_failure_total
upload_checksum_mismatch_total
cache_hit_ratio
cache_bytes
GPU allocated/reserved/peak
worker_restart_total
```

## 20.3 Health

API：

```text
/live：进程存活
/ready：DB + storage 可用
```

Worker：

```text
/live：进程存活
/ready：GPU + model manifest + ModelSession READY
```

## 20.4 LISTEN/NOTIFY

Worker 启动顺序：

1. `LISTEN jobs_ready` 并提交；
2. 立即扫描数据库；
3. 后续 NOTIFY 只触发提前扫描；
4. 始终保留周期性 poll。

这是为了避免 LISTEN 初始注册的竞态，也避免把通知队列当作持久任务队列。

---

# 21. 测试体系

## 21.1 Unit

```text
config validation
request validation
canonical request hashing
cache keys
file fingerprint
state transitions
retry classification
artifact validation
model manifest validation
source module origin guard
```

## 21.2 Contract

```text
BlobStore Local/S3-compatible 行为一致
Queue claim 接口
Heartbeat/finalize CAS
API schema backward compatibility
CLI request serialization
```

## 21.3 Integration（无 GPU）

Docker Compose：

```text
PostgreSQL
MinIO
API
Verifier
Janitor
Fake Runtime Worker
```

Fake Runtime 可控制：

```text
sleep
success
retryable error
terminal error
cancel delay
artifact corruption
```

## 21.4 GPU

标记：

```python
@pytest.mark.gpu
@pytest.mark.controlfoley
```

覆盖：

```text
V2A
TV2A / TC-V2A
AC-V2A
T2A
BF16
FP16
FP32 baseline
load / unload / reload
different durations
cancel
invalid reference
worker process restart
```

## 21.5 Parity

不能只比较文件 hash。

先建立官方 upstream self-repeat envelope，再比较：

```text
shape
sample rate
finite
peak
RMS
MAE
max absolute error
waveform cosine similarity
mel spectrogram distance
```

阈值必须由基线实测得出，而不是凭空设定。

## 21.6 Recovery / Fault Injection Matrix

| 注入点 | 预期结果 |
|---|---|
| 客户端上传中断 | upload session 保留，可重试/续传；未验证 Asset 不可提交 Job |
| 对象已上传但未 complete | session 最终过期；staging 被 Janitor 清理 |
| expected SHA 错误 | verifier REJECTED；不产生 VERIFIED asset |
| Job 写入后 API 崩溃 | Job 仍在 PostgreSQL，Worker 可 claim |
| Worker claim 后立即退出 | lease expiry；attempt 记账；Job 受控 requeue |
| 下载到一半磁盘满 | attempt retryable；worker unhealthy 或退避 |
| 本地 input SHA 不一致 | terminal integrity failure；不调用模型 |
| 推理中 `kill -9` | lease expiry；新 attempt 可重跑 |
| 推理完成、Artifact 上传前退出 | 新 attempt 重跑；无 READY artifact |
| Artifact 已上传、DB finalization 前退出 | 对象 orphan；新 attempt 可获胜；Janitor 清理旧对象 |
| cancel 与完成并发 | 数据库提交顺序决定 CANCELLED 或 SUCCEEDED，不能双终态 |
| lease 过期后旧 Worker 恢复 | heartbeat/finalize CAS 失败；旧产物 orphaned |
| 两个 Worker 同时 finalize | 只有 current attempt + epoch 可赢 |
| 下载 URL 过期 | 重新授权；Artifact 状态不变 |
| DB 暂时不可用 | Worker 停止提交；可能失 lease；恢复后由 reaper 收敛 |
| S3/MinIO 暂时不可用 | retryable；不得提前标 SUCCEEDED |
| Artifact 字节被破坏 | validation 失败；不发布 READY |

## 21.7 Property / Invariant Tests

对状态机做随机操作序列，持续断言 INV-01 至 INV-12。重点测试：

```text
claim
heartbeat
expire
cancel
retry
finalize
janitor sweep
```

任意并发顺序下均不能产生两个 winning attempts。

---

# 22. Benchmark

## 22.1 固定环境

```text
GPU：RTX 4090 24 GB
batch：1
precision：BF16
steps：25
guidance：4.5
worker concurrency：1
```

## 22.2 Workloads

```text
8s V2A
8s TV2A
8s AC-V2A
10s T2A
```

## 22.3 比较组

```text
A. 官方 demo.py / FP32 baseline
B. 官方 ComfyUI BF16
C. Runtime upstream_parity BF16
D. Runtime staged no-cache
E. Runtime staged L1 warm
F. Runtime staged L2 warm
```

## 22.4 指标

```text
cold model load
warm total latency
input materialization
video preprocess
condition encoders
Flow integration
decode/vocoder
artifact validation/upload
peak allocated
peak reserved
CPU cache bytes
cache hit ratio
end-to-end Job latency
```

README 在正式、可复现 benchmark 完成前不得写“快 X 倍”。

---

# 23. 分阶段实施计划

每个 Phase 单独 PR；前一阶段验收失败时，不进入后一阶段。

## Phase 0 — Source Lock 与官方基线

### 目标

建立不可争议的模型行为、源码、权重和环境基线。

### 工作项

```text
P0-01  clone 两个官方仓库并记录 commit
P0-02  锁定 ControlFoley 6858cd1
P0-03  生成 model deployment manifest
P0-04  记录所有 weight SHA-256 和 HF snapshot revision
P0-05  编写官方 demo baseline runner
P0-06  准备 V2A / TV2A / AC-V2A / T2A fixture manifest
P0-07  记录 upstream self-repeat envelope
P0-08  记录 4090 软件环境和显存基线
```

### 产物

```text
benchmarks/controlfoley_baseline.py
docs/source-lock.md
deploy/examples/controlfoley-deployment.lock.json
tests/fixtures/controlfoley/*.json
```

### Gate

```text
官方直接推理可运行；
输入、参数、输出和环境全部可追溯；
未修改上游仓库；
未提交权重或大媒体。
```

## Phase 1 — Headless Runtime Core + ControlFoley Parity

### 目标

完全不依赖 ComfyUI，直接调用原模型。

### 工作项

```text
P1-01  contracts 和 typed errors
P1-02  Adapter Protocol
P1-03  ModelSession state machine
P1-04  single-flight lock
P1-05  ControlFoley source/weight loader
P1-06  compatibility shims
P1-07  upstream_parity execution
P1-08  local CLI
P1-09  cancellation token
P1-10  parity tests
```

### Gate

```text
未安装 ComfyUI 时五类任务 smoke 通过；
load → generate → unload → reload 通过；
不同 duration 请求不会并发修改状态；
结果落入 upstream self-repeat envelope。
```

## Phase 2 — Durable Contracts、Schema 与 Local BlobStore

### 目标

建立服务状态机和可测试的持久契约，不接真实 GPU。

### 工作项

```text
P2-01  migrations
P2-02  Job/Attempt/Artifact repositories
P2-03  state transition guards
P2-04  idempotency contract
P2-05  LocalBlobStore
P2-06  fake runtime worker
P2-07  job_events
P2-08  invariant tests
```

### Gate

```text
数据库约束覆盖核心不变量；
相同 idempotency key 不重复创建 Job；
并发 claim 不能领取同一 Job；
Fake Runtime 完整闭环通过。
```

## Phase 3 — Verified Upload 与 Content-addressed Asset

### 目标

任何 Job 输入在进入队列前都完成全文件完整性验证。

### 工作项

```text
P3-01  Upload API
P3-02  S3/MinIO BlobStore
P3-03  presigned single PUT
P3-04  multipart session
P3-05  verifier claim loop
P3-06  streaming SHA-256
P3-07  media probe
P3-08  canonical blob promotion
P3-09  staging expiry / cleanup
P3-10  CLI upload + resume state
```

### Gate

```text
错误 SHA 被拒绝；
multipart ETag 不作为 SHA；
相同字节成功去重；
只有 VERIFIED Asset 可提交 Job；
Worker 侧可再次验证同一 digest。
```

## Phase 4 — PostgreSQL Queue、Lease 与 GPU Worker

### 目标

单卡 Worker 能可靠 claim、heartbeat、失效和恢复。

### 工作项

```text
P4-01  SKIP LOCKED claim transaction
P4-02  fencing epoch
P4-03  heartbeat thread
P4-04  lease reaper
P4-05  retry/backoff
P4-06  worker registration/readiness
P4-07  input materialization
P4-08  Runtime call
P4-09  cancel propagation
P4-10  process fault policy
```

### Gate

```text
两个 Worker 不会同时成为合法 current attempt；
kill -9 后 Job 可恢复；
stale Worker 无法 finalize；
取消能在受控延迟内进入 Runtime。
```

## Phase 5 — Artifact Validation 与 Atomic Finalization

### 目标

模型完成不等于 Job 成功；只有验证并赢得 CAS 的产物可见。

### 工作项

```text
P5-01  audio validator
P5-02  video validator
P5-03  attempt-specific object keys
P5-04  artifact SHA-256
P5-05  finalization CAS
P5-06  winning attempt
P5-07  orphan mark/sweep
P5-08  download authorization
P5-09  manifest/profile artifacts
```

### Gate

```text
SUCCEEDED 一定存在 READY artifact；
两个 attempts 最多一个获胜；
上传后崩溃不产生错误可见产物；
下载后 CLI 校验 SHA 成功。
```

## Phase 6 — Remote API/CLI 垂直闭环与恢复红队

### 目标

完成用户真实主链并系统性注入故障。

### 工作项

```text
P6-01  Job API
P6-02  status/cancel/events
P6-03  remote CLI
P6-04  API auth / namespace / quotas
P6-05  structured logging / metrics
P6-06  recovery matrix automation
P6-07  Docker Compose reference deployment
P6-08  operations runbook
```

### Gate

```text
CLI 上传 → 提交 → Worker → 下载完整闭环；
API/Worker/DB 任一重启后任务状态可收敛；
故障矩阵全部有自动化测试和明确结论。
```

## Phase 7 — Staged Pipeline、Profiler 与 Cache

### 目标

在不改变模型结果的前提下提升可观测性和重复资产效率。

### 工作项

```text
P7-01  staged adapter
P7-02  CUDA Event profiler
P7-03  L0/L1 cache
P7-04  L2 video encoder cache
P7-05  L2 reference/text cache
P7-06  byte-bounded LRU
P7-07  safetensors disk cache
P7-08  cache invalidation tests
P7-09  benchmark matrix
```

### Gate

```text
staged 与 parity 输出一致；
cache on/off 输出一致；
相同资产第二次调用 encoder counter=0；
无正式数据前不发布加速宣称。
```

## Phase 8 — ComfyUI Optional Integration

### 目标

保留优秀交互体验，但不影响 Headless 主链。

### 工作项

```text
P8-01  Embedded thin nodes
P8-02  Remote upload/job/fetch nodes
P8-03  module origin conflict guard
P8-04  official plugin A/B workflows
P8-05  no-Comfy regression suite
```

### Gate

```text
官方插件与本项目可共存；
Remote 节点不安装模型依赖；
删除 integrations/comfyui 后 Core/Service 测试全部通过。
```

## Phase 9 — Release Hardening

### 目标

形成可公开维护的 reference implementation。

### 工作项

```text
P9-01  security review
P9-02  migration compatibility tests
P9-03  backup/restore runbook
P9-04  model/license notices
P9-05  public benchmark
P9-06  sample deployment manifests
P9-07  changelog and release notes
P9-08  dependency/SBOM scan
```

### Gate

```text
文档、迁移、恢复、许可、安全和 benchmark 齐全；
不会分发 ControlFoley 权重；
不会暗示权重可商用。
```

---

# 24. 推荐 Issue 列表

```text
AR-001  Freeze official ControlFoley source and dependency manifest
AR-002  Capture official direct-inference baseline
AR-003  Scaffold contracts and runtime-core packages
AR-004  Implement ModelSession lifecycle and single-flight
AR-005  Implement ControlFoley upstream-parity adapter
AR-006  Add local CLI and parity suite

AR-010  Add durable PostgreSQL schema and migrations
AR-011  Implement Job repository and idempotency
AR-012  Implement LocalBlobStore and FakeRuntimeWorker
AR-013  Add state-machine invariant tests

AR-020  Implement upload sessions and presigned PUT
AR-021  Implement multipart upload
AR-022  Implement full-file SHA-256 verifier
AR-023  Implement media probing and asset promotion
AR-024  Implement content-addressed blob deduplication

AR-030  Implement SKIP LOCKED claim transaction
AR-031  Implement lease epoch and heartbeat CAS
AR-032  Implement lease reaper and retry policy
AR-033  Implement GPU Worker lifecycle
AR-034  Implement Runtime cancellation propagation

AR-040  Implement audio/video Artifact validators
AR-041  Implement immutable attempt Artifact upload
AR-042  Implement finalization CAS and winning attempt
AR-043  Implement orphan Artifact janitor
AR-044  Implement verified download flow

AR-050  Complete HTTP Job API
AR-051  Complete remote CLI
AR-052  Add auth, quotas, metrics, and structured logs
AR-053  Automate failure-injection matrix
AR-054  Publish Docker Compose reference deployment

AR-060  Split ControlFoley staged pipeline
AR-061  Implement stage/CUDA profiler
AR-062  Implement L0/L1 cache
AR-063  Implement L2 condition feature cache
AR-064  Publish reproducible 4090 benchmark

AR-070  Build ComfyUI Embedded nodes
AR-071  Build ComfyUI Remote nodes
AR-072  Add official-plugin A/B workflows

AR-080  Complete security and license review
AR-081  Complete release documentation and SBOM
```

---

# 25. Definition of Done

项目首个公开稳定版本必须同时满足：

```text
[ ] CLI local 模式直接调用 ControlFoley 原模型，不依赖 ComfyUI。
[ ] CLI remote 模式完成上传、Job、Worker、下载闭环。
[ ] 所有输入具有服务端确认的全文件 SHA-256。
[ ] 只有 VERIFIED Asset 可进入 Job。
[ ] PostgreSQL 是 Job/Attempt/Artifact 唯一状态权威。
[ ] Worker 使用 lease_epoch fencing。
[ ] stale Worker 不能发布成功。
[ ] 允许重复计算，但最多一个 visible winning result。
[ ] SUCCEEDED 必须蕴含 READY Artifact。
[ ] 下载工具默认验证 Artifact SHA-256。
[ ] Worker 直接调用原模型，没有 ComfyUI 中转。
[ ] ModelSession single-flight。
[ ] source、weights、HF snapshots 和运行环境可追溯。
[ ] 官方 parity、故障恢复和 4090 benchmark 可复现。
[ ] ComfyUI 是可删除的 optional integration。
[ ] 不提交或重新分发模型权重。
[ ] README 明确 ControlFoley 权重的非商业许可证约束。
```

---

# 26. 可直接交给 Codex / GPT Work 的执行指令

```text
你是该仓库的实现工程师。请严格按照 PLAN.md 分阶段实现 ControlFoley-first Headless Audio Runtime 与 Durable Inference Service。

## 总目标

主链必须是：

CLI/API
→ 可靠上传与全文件 SHA-256
→ PostgreSQL Job/Attempt/Artifact
→ GPU Worker
→ Audio Runtime Core
→ ControlFoley Adapter
→ ControlFoley 原始模型
→ Artifact 验证和下载

ComfyUI 只能是 optional integration，不能成为 Runtime Core、API 或 GPU Worker 的依赖。

## 必读上游

- xiaomi-research/controlfoley
- YJX-Research/comfyui-controlfoley-official

首个支持的 ControlFoley revision：
6858cd12a48d141201e3266e7abe1f38357a133e

先阅读：

ControlFoley：
- demo.py
- controlfoley/inference_utils.py
- controlfoley/audio_model.py
- controlfoley/feature_extractor.py
- controlfoley/temporal_config.py
- lib/flow_matching.py

ComfyUI integration：
- nodes.py
- model_urls.py
- docs/known_issues.md
- docs/vram_speed_log.md
- README.md

## 硬边界

- 不修改上游仓库；
- 不 import 官方 ComfyUI nodes.py 作为 Runtime API；
- runtime-core 不得 import ComfyUI、FastAPI、SQLAlchemy、boto3；
- API 不得安装 torch/CUDA/ControlFoley；
- Worker 直接调用原模型；
- 不写 CUDA/Triton/TensorRT/ONNX；
- 不改变采样公式和默认 steps；
- 一个 ModelSession 同时只执行一个请求；
- 不宣称 exactly-once inference；
- 不把 ETag 或 multipart composite checksum 当全文件 SHA-256；
- 不让 API 用户提供任意本地路径；
- 不提交权重、HF cache、生成媒体或私有路径；
- 未经明确指令，不 push，不创建 PR。

## 执行纪律

1. 只实施当前 Phase。
2. 先读取现有代码和测试，再修改。
3. 每个 Phase 单独 commit。
4. 前一 Phase Gate 未通过，不进入后一 Phase。
5. 任何状态变化都必须说明事务边界和失败恢复行为。
6. 任何外部副作用都必须说明幂等、重试和 orphan 处理。
7. 每完成一个 Phase，输出：
   - 改动文件；
   - 关键设计；
   - 运行命令；
   - 测试与结果；
   - Gate 逐项结论；
   - 未解决风险；
   - 下一 Phase 的前置条件。

## 第一项工作

只执行 Phase 0：

- 锁定上游 commit；
- 生成模型和依赖 manifest；
- 跑官方 direct inference baseline；
- 建立 fixture 与 self-repeat envelope；
- 不实现 API、队列、缓存或 ComfyUI 节点。
```

---

# 27. Source Register

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

# 28. 最终架构判定

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
