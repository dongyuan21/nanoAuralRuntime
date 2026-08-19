# Stable Audio 3 Small-SFX 与 Woosh V2A 研究说明

**状态：** 计划 0006 的阶段 7A 研究输入。本文件不是归档的 `docs/source-plans/` 材料，也不能替代 ADR 或路线图 Gate。

**记录：** 2026-08-18。观察到的上游修订是后续溯源阶段的研究钉死；阶段 8A 与 9A 必须在将其视为 Deployment 封印之前再次校验。

本说明记录用于冻结第二适配器计划的精确公开源码、依赖、许可与发行归档事实。它不下载权重、不执行模型，也不声称对齐。

## 产品拆分

| 家族 | 公开操作 | 范围内 | 范围外 |
| --- | --- | --- | --- |
| Stable Audio 3 Small-SFX | `audio.text_to_sfx` | 官方 PyTorch Small-SFX text-to-SFX | A2A、inpaint、continuation、LoRA、TensorRT、TFLite、MLX、`batch > 1`、音乐 checkpoint |
| Woosh V2A | `audio.video_to_sfx` | `Woosh-VFlow-8s`、`Woosh-DVFlow-8s`、Woosh-AE、TextConditionerV、Synchformer | Woosh-Flow、Woosh-DFlow、全部 Woosh T2A、TextConditionerA、Woosh-CLAP 独立推理 |

适配器 ID：`controlfoley`、`stable-audio-3-small-sfx`、`woosh-v2a`。

Woosh 后端是 Deployment，不是适配器 ID：`dvflow-8s`（默认）与 `vflow-8s`（可选参考/质量）。

## Stable Audio 3 Small-SFX

| 字段 | 观察值 | 后续冻结权威 |
| --- | --- | --- |
| 源仓库 | `https://github.com/Stability-AI/stable-audio-3` | 阶段 8A |
| 观察到的 `main` commit | `a0b57f5483c4588f827f3552b7d5c6ca2a9687be`（2026-08-02） | 阶段 8A 必须重新解析并钉死 |
| 模型 id | `small-sfx`，经由 `StableAudioModel.from_pretrained("small-sfx")` | 阶段 8A |
| Hugging Face 仓库 | `stabilityai/stable-audio-3-small-sfx` | 门控；阶段 8A runbook |
| Python | `>=3.10` | 上游 `pyproject.toml` |
| Torch / Torchaudio | `2.7.1` / `2.7.1` | 上游 `pyproject.toml` |
| 官方 CUDA wheel 行 | `cu126`（`https://download.pytorch.org/whl/cu126`） | 上游 `pyproject.toml` / README |
| 其他声明依赖 | `einops`、`einops-exts`、`numpy`、`packaging`、`safetensors`、`huggingface-hub`、`transformers`、`soundfile` | 上游 `pyproject.toml` |
| V1 采样器策略 | `steps=8`、`cfg_scale=1.0`、`batch_size=1`、官方后训练 PingPong 路径 | 推理文档；Deployment 自有 |
| 输出 | 44.1 kHz 立体声，可变长度，最长 120 秒 | 模型卡 / 推理文档 |
| 文本条件器 | 公开标识为 T5Gemma（`t5gemma-b-b-ul2`） | 模型卡；适用 Gemma Terms of Use |
| 论文 | `https://arxiv.org/abs/2605.17991` | 仅引用 |
| 代码/权重许可 | Stability AI Community License；商业使用有单独 Stability 许可路径 | 运营方必须在获取时重读 |
| 附加条款 | Gemma Terms of Use，含第 3.2 节使用限制 | 门控 HF 协议 |
| 环境 id | `stable-audio-3-pytorch-2.7.1-cu126` | ADR 0003 |

由适配器模式拥有的 V1 远程/本地请求字段：`prompt`、`duration_seconds`、`seed`。Deployment 自有且在任务上被拒绝：模型 id、后端、步数、CFG、采样器、精度、分块解码策略、源修订、HF 已解析修订，以及权重指纹。

## Woosh V2A

| 字段 | 观察值 | 后续冻结权威 |
| --- | --- | --- |
| 源仓库 | `https://github.com/SonyResearch/Woosh` | 阶段 9A |
| 发行 tag | `v1.0.0` | 阶段 9A |
| tag commit | `f6ff658efc6d63dee9959964cd75c63415910a19`（2026-03-16） | 阶段 9A 必须再次校验 |
| Python | `>=3.12` | 上游 `pyproject.toml` |
| Torch / Torchaudio | `2.8.0` / `2.8.0` | 上游 `pyproject.toml` |
| 官方 CUDA wheel 行 | `cu128`（`https://download.pytorch.org/whl/cu128`） | 上游 uv indexes |
| 其他声明依赖 | `einops`、`hydra-core`、`lightning`、`timm`、`torchdiffeq`、`transformers`、`hear21passt==0.0.26`、`pydantic`、`omegaconf`、`av`、`requests`、`gradio`；extra 拉取 `torchvision` | 上游 `pyproject.toml` |
| V2A 视频条件器 | `woosh.utils.video.SynchformerProcessor(frame_rate=24)` | `test_Woosh-VFlow.py`、`test_Woosh-DVFlow.py` |
| 外部 Synchformer | Hugging Face `hkchengrex/MMAudio`，文件 `ext_weights/synchformer_state_dict.pth` | 公开 processor 实现；SHA-256 在阶段 9A 前未解析 |
| 共享 AE | Woosh-AE，48 kHz，单声道 | 发行 + 测试 |
| 范围内文本条件器 | 仅 TextConditionerV | V2A 测试 / 本计划 |
| 噪声形状 | `[B, 128, 801]` | 两份官方 V2A 测试 |
| 窗口 | `start_time=0`、`end_time=8` | 两份官方 V2A 测试 |
| 输出 | 48 kHz，峰值归一化 WAV；remux 是测试辅助，不是适配器产物 | 测试 |
| 论文 | `v1.0.0` 源 README 引用 `arXiv:2412.15322`；后续公开文稿 `arXiv:2604.01929` | 阶段 9A 必须钉死锁定修订所使用的引用 |
| 代码许可 | README：多数 MIT；V2A 路径使用改编的 MM-AUDIO / MotionFormer 代码，许可为 Apache-2.0 | 运营方必须在钉死修订上重读 `LICENSE` |
| 权重许可 | `v1.0.0` 发行页开放权重为 CC BY-NC 4.0 | NonCommercial 限制；本项目不授予权利 |
| 环境 id | `woosh-v2a-pytorch-2.8.0-cu128` | ADR 0003 |

### 范围内的 `v1.0.0` 发行归档

2026-08-18 观察到的 GitHub 发行资产摘要：

| 资产 | SHA-256 | 角色 |
| --- | --- | --- |
| `Woosh-AE.zip` | `d6f77e3792ee43c21da580f39d6576e0da3e4b46b949223259adf36036c1f9af` | 共享解码器 |
| `TextConditionerV.zip` | `64d8ba0d647d3e685365b37526c2c95790110823623b58fa1642dd8f2139f6ac` | V2A 文本 token |
| `Woosh-VFlow-8s.zip` | `b1d9193d611d33471c39a878c205d4fb52ca380c28abd3557d610439d23b583a` | `vflow-8s` |
| `Woosh-DVFlow-8s.zip` | `c6f4b60d1cbc88a49ddd1ffa704a251570c0d7dafa8fdd1b4af7d8ba90d61d79` | `dvflow-8s` |
| `samples.zip` | `4fd43cc2c6625996c8d0fabd6ae34f89d0ac205228a383b91696780b098533b4` | 可选夹具源；归档内按文件许可 |

内部 `config.yaml` / `weights.safetensors` SHA-256 值在运营方于阶段 9A 在仓库外解压归档之前未解析。

### 明确范围外的 `v1.0.0` 发行归档

不要下载、为适配而检查，或为其加入任务模式：

| 资产 | SHA-256 |
| --- | --- |
| `TextConditionerA.zip` | `68a777b9ac28aa5daf6017b21af9a3659de75074ea14dac65f5231a42c375193` |
| `Woosh-Flow.zip` | `f748c70972798ca09f98fe49e505e700ccfe4d38b3b12b955a06cb89aa0e024c` |
| `Woosh-DFlow.zip` | `26cfe732500e3952c58aaaf433d29d75b46d42afe5e52f49430d6093eabfdb04` |
| `Woosh-CLAP.zip` | `fa2cd7cfedae45fde39b5dc81bc6c9a40d721d0c9d422a954b60d69584177f62` |

### 作为 Deployment 策略冻结的官方 V2A 采样器示例

来自 `v1.0.0` 的 `test_Woosh-DVFlow.py`：

```text
backend_id = dvflow-8s
class = FlowMapFromPretrained
sampler = sample_euler
num_steps = 4
renoise = [0, 0.5, 0.5, 0.3]
cfg = 3
```

来自 `v1.0.0` 的 `test_Woosh-VFlow.py`：

```text
backend_id = vflow-8s
class = VideoKontext
sampler = flowmatching_integrate
cfg = 4.5
atol = 1e-3
rtol = 1e-3
dtype = float32 on MPS, float64 otherwise
```

公开测试在调用点未命名 `dopri5`。阶段 9A 在修订 `f6ff658efc6d63dee9959964cd75c63415910a19` 读取了 `woosh.inference.flowmatching_sampler.flowmatching_integrate`，并确认默认 `method="dopri5"`（torchdiffeq `odeint`）。远程任务不能提供求解器、CFG、re-noise、dtype 或 checkpoint 路径。

V1 视频策略：要求时长 `>= 8` 秒并使用 `[0, 8)`。更短视频失败即关闭。没有静默填充、末帧重复或自动多分段生成。

## 硬环境冲突

三个家族不能共享根 virtualenv：

| 环境 id | Python | Torch | CUDA wheel 行 |
| --- | --- | --- | --- |
| `controlfoley-pytorch-<locked>` | 既有 ControlFoley 锁 | 既有 ControlFoley 锁 | 既有宿主 |
| `stable-audio-3-pytorch-2.7.1-cu126` | `>=3.10` | `2.7.1` | `cu126` |
| `woosh-v2a-pytorch-2.8.0-cu128` | `>=3.12` | `2.8.0` | `cu128` |

nanoAuralRuntime 自身要求 `>=3.9,<3.13`。Woosh 的 `>=3.12` 约束是隔离 Worker 的问题，不是在阶段 7A 提高 Core 包下限的理由。

## 阶段 9A 之后未解决问题

1. `stabilityai/stable-audio-3-small-sfx` 的 Hugging Face 已解析修订与文件 SHA-256（门控；需要运营方 token）。
2. 获取时 T5Gemma tokenizer/模型已解析修订与许可确认。
3. 解压后内部 Woosh 归档成员 SHA-256 值（Woosh-AE、TextConditionerV、Woosh-VFlow-8s 与 Woosh-DVFlow-8s 的 `config.yaml` 与 `weights.safetensors`）。
4. `hkchengrex/MMAudio` 已解析修订与 `ext_weights/synchformer_state_dict.pth` SHA-256。
5. 官方 VFlow `cfg=4.5` 与 DVFlow `cfg=3` 在真实 4090 基线之后是否仍为运营方钉死；在此之前它们是官方脚本默认值，不是质量声称。
6. ControlFoley 在指定宿主上的精确 torch 钉死，以便在不猜测的情况下完成 `controlfoley-pytorch-<locked>` 标识符。
7. `samples.zip` 内的按文件许可；若再分发不明确，不得提交夹具。

阶段 9A 已解决：`v1.0.0` tag commit 为 `f6ff658efc6d63dee9959964cd75c63415910a19`；VFlow 积分器默认值为 `dopri5`；范围内 GitHub 发行归档 SHA-256 与大小已封印。

这些未解决问题均不授权将权重下载进本仓库，或将硬件 Gate 标为已通过。
