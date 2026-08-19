# ControlFoley Phase 4A 分阶段比较运行手册（RTX 4090）

状态：**DEFERRED**，直至合适的 RTX 4090 宿主、密封的 Phase 2A
部署、已验证输入以及独立实现的分阶段后端
可用。跳过的测试不是已通过的 Gate。

本运行手册仅记录原始证据。它不定义容差、一致性裁决、
速度目标或性能主张。固定的原始 `demo.py` 仍为
`upstream_parity` oracle，实验性分阶段路径无论此处产生的测量结果如何，仍为显式
选择加入且非默认。

## 运营方后端契约

运营方提供可导入的 Python 模块，暴露：

```python
def create_controlfoley_staged_backend() -> ControlFoleyStagedBackend: ...
```

返回的实现必须按此精确顺序直接执行适配器拥有的阶段：

1. `media_resolve_preprocess`
2. `condition_encode_projection`
3. `integrate`
4. `decode_vocoder_postprocess`

不得将原始 `demo.py` 作为分阶段实现来调用。这样做
只会包装 oracle，不是分阶段证据。仓库在 Phase 4A 中不
捆绑或假装提供 GPU 实现。

其 `backend_id` 必须是 1–64 个字符的 ASCII 标识符，以
字母或数字开头，且仅包含字母、数字、点、下划线或
连字符。路径、空白、控制字符以及类似令牌的 `key=value`
字符串会在到达部署或结果元数据之前被拒绝。

## 密封配置

在仓库外创建运营方本地 JSON 文件。所有媒体与输出
路径必须为绝对路径。`task` 及其输入必须与所选 Phase 2A
fixture 完全匹配；测试在执行前检查其完整 SHA-256 值。

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
  "comparison_output": "<absolute-new-path-for-comparison.json>"
}
```

对于 `V2A`，省略 `prompt`。对于 `AC-V2A`，添加 `audio` 并省略 `prompt`。对于
`T2A`，省略 `video`。`TV2A` 与 `TC-V2A` 均使用 `video` 加上 `prompt`。
测试使用 fixture 中的时长、步数、引导与种子值；它不
改变上游默认值或求解器。

## 精确命令

在仓库根目录，且运营方后端在活动环境中已可导入时：

```bash
export CONTROLFOLEY_P4A_GPU_CONFIG="$($PWD/.venv/bin/python -c 'import json,sys; print(json.dumps(json.load(open(sys.argv[1], encoding="utf-8"))))' /absolute/path/to/p4a-config.json)"
.venv/bin/pytest -q -m 'gpu and controlfoley' tests/test_controlfoley_staged.py::test_controlfoley_staged_gpu_comparison_when_operator_configured
```

测试将锁定的 Phase 2A 直接 oracle 运行两次，验证其已完成
结果绑定，将显式选定的分阶段部署运行一次，通过既有波形比较读取器校验
两个音频文件，并以独占创建语义创建
`comparison_output`。它拒绝覆盖已存在的证据文件。

输出将所有原始观察绑定到：

- 规范部署清单 SHA-256；
- 候选分阶段部署指纹与后端身份；
- 锁定的源码修订与检查点 SHA-256；
- 覆盖任务、完整输入哈希、
  提示哈希、时长、步数、引导与种子的无路径规范调用 SHA-256；
- 完整的 oracle 与候选输出 SHA-256 值。

运行器在执行前捕获 oracle 部署清单，然后
在直接运行之后、候选加载之后以及写入证据之前立即
重新读取并比较其规范清单 SHA-256、源码修订与
检查点 SHA-256。清单漂移会中止比较，而不是
在一条记录中混合两次部署。

指标为原始峰值、RMS、MAE、最大绝对误差、波形余弦
相似度与 mel 频谱距离，以及波形形状与采样率。
有意不包含 `threshold`、`claim`、`passed`、`parity` 或
性能结果字段。接受标准以及任何默认更改需要
后续以真实硬件证据支持的审查；在此之前 Phase 6 发布
Gate 仍保持阻塞。
