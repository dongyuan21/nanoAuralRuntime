# ControlFoley Phase 4B 性能分析运行手册（RTX 4090）

状态：**DEFERRED**，直至指定 RTX 4090 宿主具备已验证的 Phase
2A 部署、密封 fixture 输入，以及真实的进程内分阶段后端。
跳过不是已通过的硬件 Gate。

Phase 4B 仅记录观察结果。它不定义目标、回归阈值、
一致性裁决、加速主张或性能主张。性能分析默认禁用，
`upstream_parity` 仍为默认/oracle，且
`experimental_staged_v1` 仍为显式选择加入。

## 为何需要分阶段后端

原始上游 oracle 在隔离子进程中运行，因此父
进程 CUDA 事件无法如实测量其内核。条件 GPU
测试因此仅分析运营方提供的、进程内分阶段后端，
该后端直接实现 Phase 4A `ControlFoleyStagedBackend` 协议。
不得包装或调用原始 `demo.py`。

分析器仅在 `torch.cuda` 报告实际 CUDA 能力之后注入 `TorchCudaProfileBackend`。
它记录 CUDA Events 以及 allocated、reserved、
peak-allocated 与 peak-reserved 字节。若 torch 或 CUDA 缺失，测试
跳过；CPU 报告永不合成这些字段。

## 密封运营方配置

在仓库外创建 JSON 文件。对象必须恰好具有下方所示的
任务特定键：额外键会被拒绝。所有路径均为
运营方本地绝对路径，所有输入文件/目录必须已经存在，
输出不得存在，且其父目录必须已经存在。
fixture 任务、媒体哈希、提示哈希、时长、步数、引导与种子在
调用前被检查。

```json
{
  "deployment": "<absolute-path-to-verified-deployment.lock.json>",
  "fixture": "<absolute-path-to-verified-fixture.json>",
  "source_dir": "<absolute-path-to-source-at-6858cd12a48d141201e3266e7abe1f38357a133e>",
  "weights_dir": "<absolute-path-to-verified-model-weights>",
  "backend_module": "operator_controlfoley_staged_backend",
  "task": "TV2A",
  "video": "<absolute-path-to-verified-video>",
  "prompt": "the exact fixture prompt",
  "profile_output": "<absolute-new-path-for-profile.json>"
}
```

对于 `V2A`，省略 `prompt`。对于 `AC-V2A`，添加 `audio` 并省略 `prompt`。对于
`T2A`，省略 `video`。`TV2A` 与 `TC-V2A` 均使用 `video` 加上 `prompt`。

后端模块必须暴露：

```python
def create_controlfoley_staged_backend() -> ControlFoleyStagedBackend: ...
```

## 精确命令

在仓库根目录，且运营方后端可导入时：

```bash
export CONTROLFOLEY_P4B_GPU_CONFIG="$($PWD/.venv/bin/python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))))' /absolute/path/to/p4b-config.json)"
.venv/bin/pytest -q -m 'gpu and controlfoley' tests/test_controlfoley_profiler.py::test_controlfoley_staged_cuda_profile_when_operator_configured
```

在候选加载之前以及调用之后，harness 重新读取
部署与 fixture 清单，验证锁定的源码修订/来源/
干净状态，重新计算每个检查点与外部权重的完整 SHA-256，
并重新计算规范任务/输入/参数指纹。任何缺失或
无效的已配置密封（包括源码来源/修订/脏状态或任何
检查点/外部权重 SHA 不匹配）会使运行失败且不写入证据，
即使 CUDA 也不可用。仅在所有已配置密封通过后，实际 torch/CUDA 能力
缺失才可产生跳过。候选
会话必须匹配最初捕获的清单 SHA、源码修订、
检查点 SHA 以及安全后端身份。

输出文件以独占创建语义创建，并包含
无路径的证据信封。它将 fixture 清单 SHA、输出 SHA、
候选部署指纹、部署清单 SHA、源码修订、
检查点 SHA、安全分阶段后端 id 以及规范调用 SHA 绑定到
命名空间化的原始 `ProfileReport`。它不包含运营方路径或令牌。阶段
顺序恰好为：

1. `media_resolve_preprocess`
2. `condition_encode_projection`
3. `integrate`
4. `decode_vocoder_postprocess`

每个阶段具有真实的单调 CPU 时长，并且仅在实际 CUDA 上具有 CUDA
Event 时长与内存观察。缺失能力表示为
每阶段的 `status: unavailable`、`backend_id: null`、缺失的 CUDA 指标以及 `cuda: null`。
不要将这些观察与目标比较，也不要发布速度
主张。RTX 4090 性能分析验证与 Phase 6 发布 Gate 在审查过的证据被记录之前仍保持阻塞。
