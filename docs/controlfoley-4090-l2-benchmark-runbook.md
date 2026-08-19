# ControlFoley Phase 4D RTX 4090 L2 基准运行手册

状态：**DEFERRED**，直至已声明的 RTX 4090 宿主、密封的 Phase 2A
部署/fixture，以及实现 Phase 4D 条件特征拆分的运营方分阶段后端可用。本运行手册不包含或暗示
一致性、质量、加速或发布主张。

## 实验边界

`upstream_parity` 仍为默认/oracle。Phase 4D 是独立的、
显式选定的分阶段部署。它仅可为 `condition_encode_projection` 持久化按角色的编码器
特征。投影本身在每次调用时运行。Flow Matching/积分、潜变量、求解器步骤、解码、声码器、
后处理以及最终产物永不缓存。

磁盘存储仅接受严格校验、纯数据的 safetensors 包。
它不导入 pickle、`torch.load`，或由缓存字节选择的解码器。
运营方缓存根必须是已存在的规范绝对目录，权限
模式为 `0700`，位于锁定的源码与权重树之外。缓存报告与
证据不包含本地路径。

## 运营方配置

在运行前创建空的私有根目录：

```bash
mkdir -m 700 /absolute/operator/l2-cache
```

将 `CONTROLFOLEY_P4D_GPU_CONFIG` 设为一个 JSON 对象。任务特定键
与 Phase 4C fixture 契约相同：`video`；`video,prompt`；
`video,audio`；或 `prompt`。

```json
{
  "deployment": "/absolute/operator/deployment.lock.json",
  "fixture": "/absolute/operator/fixture.json",
  "source_dir": "/absolute/operator/ControlFoley",
  "weights_dir": "/absolute/operator/controlfoley-weights",
  "backend_module": "operator.controlfoley_backend",
  "task": "V2A",
  "preprocess_version": "operator-media-v1",
  "code_version": "nano-aural-p4d-v1",
  "evidence_output": "/absolute/operator/evidence/p4d-l2-matrix.json",
  "video": "/absolute/operator/fixtures/input.mp4",
  "condition_cache_root": "/absolute/operator/l2-cache",
  "condition_codec_version": "operator-condition-codec-v1",
  "condition_schema_version": "operator-condition-schema-v1",
  "expected_device_name": "NVIDIA GeForce RTX 4090",
  "repeats": 3
}
```

后端模块必须暴露 `create_controlfoley_staged_backend()`，且
返回的后端必须实现显式的条件特征编码、
安全导出/导入以及投影协议。其编解码器/模式身份必须
与部署配置完全相等。

## 精确命令

在仓库根目录：

```bash
.venv/bin/pytest -q \
  tests/test_controlfoley_condition_cache.py::test_controlfoley_l2_benchmark_matrix_when_operator_configured
```

缺失配置，或已验证的纯 CUDA 能力缺失，是
预期的跳过。一旦提供配置，源码修订/来源/脏
树问题、权重/检查点/fixture/输入指纹漂移、不安全缓存
根、后端/编解码器/模式不匹配，或证据路径冲突均为失败，
而非跳过。

## 矩阵与证据

运行器对每次重复按此顺序执行密封单元，且
最多驻留一个模型会话。它卸载未缓存部署，并在加载 L2 部署之前运行
CUDA 同步/缓存清理/峰值重置：

1. `off`：无缓存的实验性分阶段路径。
2. `l2_cold`：在精确部署缓存失效之后的显式 L0/L1/L2 部署。
3. `l2_warm`：同一密封 L2 部署，不失效。

证据包含每次运行的原始墙钟计时、可用的 CUDA 事件计时与
显存观察、缓存命中/未命中/字节、编码器/投影调用次数、
输出 SHA-256，以及无路径的部署/fixture/调用溯源。
计划显式密封条件编解码器/模式版本以及
语义 L0/L1 与 L2 策略指纹；运营根与容量
限制保持为运营方本地，且不会发出。
每个已配置样本必须包含带有计时以及
allocated/reserved/peak 内存字段的可用 CUDA 分析，且设备必须精确标识
所声明的 `NVIDIA GeForce RTX 4090`。证据使用私有
已 fsync 的临时文件与独占原子链接发布；永不接受
部分最终文件。
它不定义容差、加速比、阈值、裁决或性能主张。
测试要求可用矩阵上的输出哈希完全一致作为安全
检查；该 CPU/GPU 管线结果不是被接受的上游一致性结果。
