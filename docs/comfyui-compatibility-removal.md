# ComfyUI 兼容性与移除

NanoAural 的 ComfyUI 前端是可选、可移除的适配器。它们不
替代本地 Runtime 或持久服务作为执行、作业或产物
权威。ComfyUI 本身不是 `src/` 下任何包的依赖。

## 受支持的共存

Phase 5C 在同一宿主进程中检查三组独立拥有的节点族：

| 节点族 | 执行路径 | 前端中的模型代码 |
| --- | --- | --- |
| 官方 ControlFoley 插件 | 官方插件 | 由官方插件拥有 |
| NanoAural Embedded | 既有本地 Runtime 与适配器 | 无复制的执行代码 |
| NanoAural Remote | 公开远程客户端与持久服务 | 无 |

`integrations/comfyui_compat` 记录官方插件的公开
`NODE_CLASS_MAPPINGS` 名称，并校验标准 A/B 工作流
`integrations/comfyui_compat/examples/official_controlfoley_t2a_ab.json`。
契约快照基于当前公开的 `nodes.py`：
<https://github.com/YJX-Research/comfyui-controlfoley-official>。在接受官方插件更新前须重新运行
共存测试；缺失或重命名的公开节点是明确的兼容性失败，而不是
静默放宽发现条件的理由。

所有官方、Embedded 与 Remote 节点名称必须互不相交。兼容性
检查在映射可能静默互相替换之前拒绝重复名称。
三个示例工作流使用标准 ComfyUI 的 `nodes`、`widgets_values`、
六字段 `links` 以及终端 `OUTPUT_NODE` 形态，并可一并发现。
私有自定义值类型无意在节点族之间交叉；将
每个示例作为独立的 A/B 执行路径使用。

## ControlFoley 源码来源规则

当每个已加载的上游 `controlfoley` 模块与 `lib.flow_matching`
都解析到完全相同的 ControlFoley 检出目录之下时，官方路径与 NanoAural Embedded 路径可以共存。官方插件包装器可以保留在其自己的
ComfyUI `custom_nodes` 目录中。

启动 ComfyUI 之前：

1. 将官方插件的 `CONTROLFOLEY_SOURCE_DIR` 设置为所选检出目录。
2. 将 NanoAural Embedded 的密封运营方 JSON `source_dir` 设置为同一
   检出目录，并按 `integrations/comfyui/README.md` 的文档导出
   `NANO_AURAL_COMFYUI_OPERATOR_CONFIG`。
3. 启动全新进程，发现三组映射，并在加载任一模型路径之前，使用官方插件模块与当前
   `sys.modules` 映射运行
   `inspect_controlfoley_coexistence()`。

未知来源或密封检出目录之外的路径会被拒绝，并给出
实际与期望来源以及重启说明。不得捕获该失败
并继续加载：Python 无法在活动的 ComfyUI 进程中安全替换已导入模块。停止宿主、纠正两侧源码设置后重启。
Remote 前端不受影响，因为它不导入 ControlFoley、Runtime、
worker、torch、CUDA 或模型包。

## 移除与无头验证

停止 ComfyUI，并在移除已加载的 Embedded 包之前调用前端文档中的拆除流程。然后删除或省略任一可选目录：

- `integrations/comfyui/`
- `integrations/comfyui_remote/`
- 未部署共存诊断时的 `integrations/comfyui_compat/`

无需迁移或持久状态清理。远程作业与产物仍由
持久服务拥有；本地 Runtime 状态是进程本地的，并由
Embedded 拆除关闭。

Phase 5C 省略回归不仅隐藏导入。它将无头 `src/` 树复制到仓库之外，在物理上排除 Embedded、
Remote 或两者，切换到分离的工作目录，移除 Python 路径
环境变量，并用 `-E -S` 运行全新解释器。该进程
执行 Core、本地 ControlFoley CLI、ApplicationService、
ApplicationApi、伪持久 worker 以及 ControlFoley 持久调用
绑定的 CPU 冒烟路径。它还证明无头包没有反向集成或
ComfyUI 导入，且 Remote 没有模型侧导入。

使用以下命令运行范围内的证据：

```sh
.venv/bin/pytest -q tests/test_comfyui_coexistence.py tests/test_comfyui_removal.py
```

指定的 4090 ComfyUI 验证在其硬件与密封运营方配置不可用时仍为 **DEFERRED**。跳过不是通过的 GPU
结果，也不改变这些仅 CPU 的兼容性或移除主张。
