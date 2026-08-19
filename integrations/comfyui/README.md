# 可选嵌入式 ComfyUI 前端

本目录是可移除的自定义节点前端。它不导入
ComfyUI，并有意位于 `src/` 无头包之外。
删除或省略它会使 Runtime Core、本地 CLI、适配器、API 与
worker 保持不变。

## 生命周期所有权

运营方编写严格的配置 JSON，并在 ComfyUI 启动前将
`NANO_AURAL_COMFYUI_OPERATOR_CONFIG` 设置为其绝对路径。
节点发现随后惰性创建单一 Runtime 所有者。部署路径保持
由运营方拥有，且永不作为 ComfyUI 节点输入。

```json
{
  "schema_version": 1,
  "manifest_path": "/operator/controlfoley/deployment.json",
  "source_dir": "/operator/controlfoley/source",
  "weights_dir": "/operator/controlfoley/weights"
}
```

```sh
export NANO_AURAL_COMFYUI_OPERATOR_CONFIG=/operator/nano-aural-comfyui.json
```

所有者复用一个 single-flight Runtime 会话。`teardown_embedded_runtime()`
在卸载前等待活动调用；卸载失败会将
句柄保留在不安全状态，以便可以重试拆除。宿主特定引导
也可以调用 `configure_embedded_runtime()`，传入 Runtime 工厂
以及可选的取消源工厂。

已发现节点通过仅协议 shim 适配 ComfyUI 已加载的中断回调；本包永不导入 ComfyUI。若回调
缺失、意外抛出或阻塞，执行 fail closed 且不返回音频
结果。进程退出也会注册显式拆除钩子。

桥接拒绝已从不同来源导入 `controlfoley` 的进程。
在恰好选择一个官方/本地 ControlFoley 源码后重启 ComfyUI；混合模块来源永不被静默接受。

`examples/embedded_controlfoley_t2a.json` 是带有生产者与终端 `OUTPUT_NODE` 的标准链接工作流。输出节点仅返回名称、媒体
类型与字节数；它不发布文件或适配器/部署元数据。

## 条件 GPU 冒烟

仅在指定硬件宿主上，提供密封的本地部署路径
并运行嵌入式冒烟：

```sh
export CONTROLFOLEY_COMFYUI_GPU_CONFIG='{"operator_config_path":"/operator/nano-aural-comfyui.json","task":"T2A","prompt":"gentle rain"}'
.venv/bin/pytest -m "gpu and controlfoley" -q tests/test_comfyui_embedded.py
```

没有该宿主配置时，测试有意跳过，且 UI
GPU 证据仍为 **DEFERRED**。
