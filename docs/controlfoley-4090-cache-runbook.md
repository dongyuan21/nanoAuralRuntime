# ControlFoley Phase 4C L0/L1 缓存运行手册（RTX 4090）

状态：**DEFERRED**，直至指定 RTX 4090 宿主具备已验证的 Phase
2A 部署、密封 fixture 输入，以及实现 Phase 4C 安全字节预处理编解码器的真实分阶段后端。跳过不是已通过的硬件
Gate。

Phase 4C 为实验性，默认禁用。`upstream_parity` 仍为
默认/oracle；本命令比较同一显式选定分阶段后端的关缓存、冷缓存与热缓存运行。
它不对照上游 oracle 测量一致性、不定义阈值，也不支持性能
主张。

## 缓存值与安全边界

- L0 仅存储无路径的规范元数据 JSON：完整输入 SHA-256/大小、
  任务及全部调用参数、源码/部署/清单/检查点
  身份、精度、后端 id、预处理版本以及缓存代码
  版本。
- L1 仅存储分阶段后端为
  `media_resolve_preprocess` 导出的不可变字节。后端必须实现
  `ControlFoleyPreprocessCacheCodec`；没有它，已缓存部署将无法加载。
- 条件编码器、Flow Matching/积分、潜变量、求解器状态、解码、
  声码器以及最终产物在 Phase 4C 中永不缓存。
- 存储为进程本地、线程安全、按字节/条目有界的内存 LRU。Phase
  4C 不写入 pickle 或磁盘缓存。过期、损坏或无法解码的条目
  成为未命中并被移除；模型执行以冷阶段继续。

写入使用不可变的条件先写者胜出语义，并延迟
直至整个调用成功。输入文件在预处理前进行完整哈希，并在提交前立即再次哈希。取消、模型故障
或输入漂移不提交任何待写入条目。Runtime 同一会话执行保持
single-flight；不同会话中的并发冷请求可能重复
确定性预处理，之后第一次成功的不可变 put
胜出。

## 密封运营方配置

在仓库外创建精确的 JSON 对象。额外字段会被拒绝。
每条路径均为绝对路径；输入必须存在，证据输出不得存在，
且其父目录必须已经存在。

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
  "preprocess_version": "operator-media-v1",
  "code_version": "operator-cache-code-v1",
  "evidence_output": "<absolute-new-path-for-p4c-evidence.json>"
}
```

对于 `V2A`，省略 `prompt`。对于 `AC-V2A`，添加 `audio` 并省略 `prompt`。对于
`T2A`，省略 `video`。`TV2A` 与 `TC-V2A` 均需要 `video` 加上 `prompt`。

后端模块必须暴露：

```python
def create_controlfoley_staged_backend() -> ControlFoleyStagedBackend: ...
```

返回的后端还必须在结构上实现
`ControlFoleyPreprocessCacheCodec`。运营方拥有所声明的
`preprocess_version` 与 `code_version`；更改任一者会产生不同的
密封部署与缓存键。

## 精确命令

在仓库根目录，且运营方后端可导入时：

```bash
export CONTROLFOLEY_P4C_GPU_CONFIG="$($PWD/.venv/bin/python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))))' /absolute/path/to/p4c-config.json)"
.venv/bin/pytest -q -m 'gpu and controlfoley' tests/test_controlfoley_cache.py::test_controlfoley_l0_l1_cache_equivalence_when_operator_configured
```

已配置的源码来源/修订/脏状态、清单、fixture、输入、
检查点或外部权重失败即使 CUDA 也缺失，仍为测试失败。
仅当配置本身有效、但宿主没有实际
torch/CUDA 能力时才会跳过。

在写入证据之前，harness 重复完整的源码/权重密封、
重新读取部署与 fixture 清单，并重新计算规范
调用指纹。独占创建、无路径的 JSON 记录原始
输出 SHA-256 值以及如实的冷/热 `CacheReport` 值。它不包含
计时目标、基准结果、上游一致性裁决或加速
主张。RTX 4090 缓存等价性在该证据实际捕获并审查之前仍为延期。
