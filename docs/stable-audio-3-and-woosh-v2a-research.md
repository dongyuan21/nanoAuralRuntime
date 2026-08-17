# Stable Audio 3 Small-SFX and Woosh V2A research note

**Status:** Phase 7A research input for programme plan 0006. This file is not
archived `docs/source-plans/` material and is not a substitute for the ADRs or
Roadmap Gates.

**Recorded:** 2026-08-18. Observed upstream revisions are research pins for
later provenance Phases; Phases 8A and 9A must re-verify them before treating
them as Deployment seals.

This note records exact public source, dependency, license, and release-archive
facts used to freeze the second-adapter programme. It does not download weights,
does not execute models, and does not claim parity.

## Product split

| Family | Public operation | In scope | Out of scope |
| --- | --- | --- | --- |
| Stable Audio 3 Small-SFX | `audio.text_to_sfx` | Official PyTorch Small-SFX text-to-SFX | A2A, inpaint, continuation, LoRA, TensorRT, TFLite, MLX, `batch > 1`, music checkpoints |
| Woosh V2A | `audio.video_to_sfx` | `Woosh-VFlow-8s`, `Woosh-DVFlow-8s`, Woosh-AE, TextConditionerV, Synchformer | Woosh-Flow, Woosh-DFlow, all Woosh T2A, TextConditionerA, Woosh-CLAP standalone inference |

Adapter IDs: `controlfoley`, `stable-audio-3-small-sfx`, `woosh-v2a`.

Woosh backends are Deployments, not Adapter IDs: `dvflow-8s` (default) and
`vflow-8s` (selectable reference/quality).

## Stable Audio 3 Small-SFX

| Field | Observed value | Authority for later freeze |
| --- | --- | --- |
| Source repository | `https://github.com/Stability-AI/stable-audio-3` | Phase 8A |
| Observed `main` commit | `a0b57f5483c4588f827f3552b7d5c6ca2a9687be` (2026-08-02) | Phase 8A must re-resolve and pin |
| Model id | `small-sfx` via `StableAudioModel.from_pretrained("small-sfx")` | Phase 8A |
| Hugging Face repo | `stabilityai/stable-audio-3-small-sfx` | gated; Phase 8A runbook |
| Python | `>=3.10` | upstream `pyproject.toml` |
| Torch / Torchaudio | `2.7.1` / `2.7.1` | upstream `pyproject.toml` |
| Official CUDA wheel line | `cu126` (`https://download.pytorch.org/whl/cu126`) | upstream `pyproject.toml` / README |
| Other declared deps | `einops`, `einops-exts`, `numpy`, `packaging`, `safetensors`, `huggingface-hub`, `transformers`, `soundfile` | upstream `pyproject.toml` |
| V1 sampler policy | `steps=8`, `cfg_scale=1.0`, `batch_size=1`, official post-trained PingPong path | inference docs; Deployment-owned |
| Output | 44.1 kHz stereo, variable length, maximum 120 seconds | model card / inference docs |
| Text conditioner | publicly identified as T5Gemma (`t5gemma-b-b-ul2`) | model card; Gemma Terms of Use apply |
| Paper | `https://arxiv.org/abs/2605.17991` | citation only |
| Code/weights license | Stability AI Community License; commercial use has a separate Stability license path | operators must re-read at acquisition |
| Additional terms | Gemma Terms of Use, including use restrictions in Section 3.2 | gated HF agreement |
| Environment id | `stable-audio-3-pytorch-2.7.1-cu126` | ADR 0003 |

V1 remote/local request fields owned by the adapter schema: `prompt`,
`duration_seconds`, `seed`. Deployment-owned and rejected on Jobs: model id,
backend, steps, CFG, sampler, precision, chunked-decode policy, source
revision, HF resolved revision, and weight fingerprints.

## Woosh V2A

| Field | Observed value | Authority for later freeze |
| --- | --- | --- |
| Source repository | `https://github.com/SonyResearch/Woosh` | Phase 9A |
| Release tag | `v1.0.0` | Phase 9A |
| Tag commit | `f6ff658efc6d63dee9959964cd75c63415910a19` (2026-03-16) | Phase 9A must re-verify |
| Python | `>=3.12` | upstream `pyproject.toml` |
| Torch / Torchaudio | `2.8.0` / `2.8.0` | upstream `pyproject.toml` |
| Official CUDA wheel line | `cu128` (`https://download.pytorch.org/whl/cu128`) | upstream uv indexes |
| Other declared deps | `einops`, `hydra-core`, `lightning`, `timm`, `torchdiffeq`, `transformers`, `hear21passt==0.0.26`, `pydantic`, `omegaconf`, `av`, `requests`, `gradio`; extras pull `torchvision` | upstream `pyproject.toml` |
| V2A video conditioner | `woosh.utils.video.SynchformerProcessor(frame_rate=24)` | `test_Woosh-VFlow.py`, `test_Woosh-DVFlow.py` |
| External Synchformer | Hugging Face `hkchengrex/MMAudio`, file `ext_weights/synchformer_state_dict.pth` | public processor implementation; SHA-256 unresolved until Phase 9A |
| Shared AE | Woosh-AE, 48 kHz, mono | release + tests |
| Text conditioner in scope | TextConditionerV only | V2A tests / this programme |
| Noise shape | `[B, 128, 801]` | both official V2A tests |
| Window | `start_time=0`, `end_time=8` | both official V2A tests |
| Output | 48 kHz, peak-normalized WAV; remux is a test helper, not an adapter artifact | tests |
| Paper | source README at `v1.0.0` cites `arXiv:2412.15322`; later public write-up `arXiv:2604.01929` | Phase 9A must pin the citation used by the locked revision |
| Code license | README: majority MIT; V2A path uses adapted MM-AUDIO / MotionFormer code under Apache-2.0 | operators must re-read `LICENSE` at the pinned revision |
| Weight license | CC BY-NC 4.0 for open weights on the `v1.0.0` release page | NonCommercial restriction; this project grants no rights |
| Environment id | `woosh-v2a-pytorch-2.8.0-cu128` | ADR 0003 |

### In-scope `v1.0.0` release archives

GitHub release asset digests observed on 2026-08-18:

| Asset | SHA-256 | Role |
| --- | --- | --- |
| `Woosh-AE.zip` | `d6f77e3792ee43c21da580f39d6576e0da3e4b46b949223259adf36036c1f9af` | shared decoder |
| `TextConditionerV.zip` | `64d8ba0d647d3e685365b37526c2c95790110823623b58fa1642dd8f2139f6ac` | V2A text tokens |
| `Woosh-VFlow-8s.zip` | `b1d9193d611d33471c39a878c205d4fb52ca380c28abd3557d610439d23b583a` | `vflow-8s` |
| `Woosh-DVFlow-8s.zip` | `c6f4b60d1cbc88a49ddd1ffa704a251570c0d7dafa8fdd1b4af7d8ba90d61d79` | `dvflow-8s` |
| `samples.zip` | `4fd43cc2c6625996c8d0fabd6ae34f89d0ac205228a383b91696780b098533b4` | optional fixture source; per-file licenses inside the archive |

Inner `config.yaml` / `weights.safetensors` SHA-256 values are unresolved until
an operator extracts the archives outside the repository in Phase 9A.

### Explicitly out-of-scope `v1.0.0` release archives

Do not download, inspect for adaptation, or add task schemas for:

| Asset | SHA-256 |
| --- | --- |
| `TextConditionerA.zip` | `68a777b9ac28aa5daf6017b21af9a3659de75074ea14dac65f5231a42c375193` |
| `Woosh-Flow.zip` | `f748c70972798ca09f98fe49e505e700ccfe4d38b3b12b955a06cb89aa0e024c` |
| `Woosh-DFlow.zip` | `26cfe732500e3952c58aaaf433d29d75b46d42afe5e52f49430d6093eabfdb04` |
| `Woosh-CLAP.zip` | `fa2cd7cfedae45fde39b5dc81bc6c9a40d721d0c9d422a954b60d69584177f62` |

### Official V2A sampler examples to freeze as Deployment policy

From `test_Woosh-DVFlow.py` at `v1.0.0`:

```text
backend_id = dvflow-8s
class = FlowMapFromPretrained
sampler = sample_euler
num_steps = 4
renoise = [0, 0.5, 0.5, 0.3]
cfg = 3
```

From `test_Woosh-VFlow.py` at `v1.0.0`:

```text
backend_id = vflow-8s
class = VideoKontext
sampler = flowmatching_integrate
cfg = 4.5
atol = 1e-3
rtol = 1e-3
dtype = float32 on MPS, float64 otherwise
```

The public test does not name `dopri5` at the call site. Phase 9A read
`woosh.inference.flowmatching_sampler.flowmatching_integrate` at revision
`f6ff658efc6d63dee9959964cd75c63415910a19` and confirmed the default
`method="dopri5"` (torchdiffeq `odeint`). Remote Jobs cannot supply solver,
CFG, re-noise, dtype, or checkpoint paths.

V1 video policy: require duration `>= 8` seconds and use `[0, 8)`. Shorter
videos fail closed. No silent pad, last-frame repeat, or automatic multi-segment
generation.

## Hard environment conflict

The three families cannot share a root virtualenv:

| Environment id | Python | Torch | CUDA wheel line |
| --- | --- | --- | --- |
| `controlfoley-pytorch-<locked>` | existing ControlFoley lock | existing ControlFoley lock | existing host |
| `stable-audio-3-pytorch-2.7.1-cu126` | `>=3.10` | `2.7.1` | `cu126` |
| `woosh-v2a-pytorch-2.8.0-cu128` | `>=3.12` | `2.8.0` | `cu128` |

nanoAuralRuntime itself requires `>=3.9,<3.13`. Woosh's `>=3.12` constraint is
an isolated Worker concern, not a reason to raise the Core package floor in
Phase 7A.

## Unresolved questions after Phase 9A

1. Hugging Face resolved revision and file SHA-256 for
   `stabilityai/stable-audio-3-small-sfx` (gated; requires operator token).
2. T5Gemma tokenizer/model resolved revision and license confirmation at
   acquisition time.
3. Inner Woosh archive member SHA-256 values after unzip (`config.yaml` and
   `weights.safetensors` for Woosh-AE, TextConditionerV, Woosh-VFlow-8s, and
   Woosh-DVFlow-8s).
4. `hkchengrex/MMAudio` resolved revision and
   `ext_weights/synchformer_state_dict.pth` SHA-256.
5. Whether official VFlow `cfg=4.5` and DVFlow `cfg=3` remain the operator pins
   after a real 4090 baseline; until then they are the official-script defaults,
   not a quality claim.
6. ControlFoley's exact torch pin on the designated host, so the
   `controlfoley-pytorch-<locked>` identifier can be completed without guessing.
7. Per-file licenses inside `samples.zip`; fixtures must not be committed if
   redistribution is unclear.

Resolved in Phase 9A: `v1.0.0` tag commit is
`f6ff658efc6d63dee9959964cd75c63415910a19`; VFlow integrator default is
`dopri5`; in-scope GitHub release archive SHA-256 and sizes are sealed.

None of these unresolved items authorizes downloading weights into this
repository or marking a hardware Gate passed.
