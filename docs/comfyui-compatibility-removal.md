# 可选 ComfyUI 前端

ComfyUI 只是可选前端。执行、任务与产物仍由本地 Runtime 或持久化服务做主。`src/` 下的包不依赖 ComfyUI。

可以同时存在三组节点，名称不得冲突：

| 节点族 | 做什么 |
| --- | --- |
| 官方 ControlFoley 插件 | 官方插件自己的执行路径 |
| NanoAural Embedded | 把 ComfyUI 控件翻译到本机 Runtime / 适配器，不复制模型代码 |
| NanoAural Remote | 走公开远程客户端与持久化 API，不加载模型或 CUDA |

官方插件的公开节点名记录在 `integrations/comfyui_compat`，示例工作流为 `integrations/comfyui_compat/examples/official_controlfoley_t2a_ab.json`。对照当前 `nodes.py`：<https://github.com/YJX-Research/comfyui-controlfoley-official>。

把每条示例当作独立 A/B 路径；不要在节点族之间交叉私有值类型。

## 与官方插件共存

官方路径与 Embedded 路径只有在已加载的 `controlfoley` 与 `lib.flow_matching` 都落在**同一份** ControlFoley 检出里时才能共存。官方插件包装器可以留在自己的 `custom_nodes` 目录。

启动 ComfyUI 之前：

1. 官方插件的 `CONTROLFOLEY_SOURCE_DIR` 指向该检出。
2. Embedded 密封运营方 JSON 的 `source_dir` 指向同一检出，并按 `integrations/comfyui/README.md` 导出 `NANO_AURAL_COMFYUI_OPERATOR_CONFIG`。
3. 用全新进程发现三组映射；在加载任一模型路径之前运行 `inspect_controlfoley_coexistence()`。

来源不一致会失败并要求重启。不要捕获后继续加载：已导入模块无法在活动进程里安全替换。Remote 前端不受影响，它不导入 ControlFoley、Runtime、Worker、torch 或 CUDA。

## 拆除

停止 ComfyUI，按前端文档拆除已加载的 Embedded 包，然后删除或省略：

- `integrations/comfyui/`
- `integrations/comfyui_remote/`
- 不需要共存诊断时的 `integrations/comfyui_compat/`

不需要迁移或清理持久化状态。远程任务与产物仍在持久化服务里；本机 Runtime 状态随进程结束。去掉这些目录后，无头 CLI、API 与 Worker 仍应可用。
