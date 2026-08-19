# 可选远程 ComfyUI 前端

本目录是可移除的、仅远程的自定义节点包。它仅导入
公开的 `nano_aural_runtime_remote` 客户端与标准库模块。
它不导入 ComfyUI、Runtime Core、ControlFoley、torch、CUDA、worker 或
持久内部实现。

## 运营方配置

在工作流之外编写严格的 JSON 文件，并将
`NANO_AURAL_COMFYUI_REMOTE_CONFIG` 设置为其绝对路径。Bearer 令牌从
单独命名的环境变量读取；令牌、基础
URL、配置路径与下载目录都不会成为节点值或 UI 摘要。

```json
{
  "schema_version": 1,
  "base_url": "https://nano-aural.example",
  "token_env": "NANO_AURAL_API_TOKEN",
  "allow_loopback_http": false,
  "download_dir": "/operator/nano-aural-downloads",
  "transport_timeout_seconds": 30,
  "max_upload_bytes": 1073741824,
  "max_download_bytes": 1073741824,
  "max_wait_seconds": 3600,
  "max_poll_iterations": 3600
}
```

`UrllibTransport` 仍为端点权威：要求 HTTPS，除非
显式启用回环 HTTP 开发。`RemoteClient` 仍为
已验证上传、请求校验、授权、下载
SHA-256/大小校验、原子发布以及禁止覆盖行为的权威。

## 节点流程

`RemoteUpload` 上传有界本地文件，并在服务报告已验证资产之后仅返回不可变
`(role, asset_id)` 绑定。
`RemoteAssetBundle` 将最多八个具有唯一白名单
角色的绑定组合在一起，因此零输入、仅视频以及视频加参考音频请求使用
同一提交节点。后续节点仅交换资产、作业与产物
ID 以及白名单状态/完整性字段——永不交换客户端路径、服务器路径、
存储键、端点或凭据。持久资产、作业与产物 ID 仅以规范 UUID 形式被服务接受；事件游标保持
其经单独校验的规范十进制协议值。

等待节点调用公开客户端的有界 `wait()` 切片，展示
白名单事件类型/计数，并将宿主中断转换为
公开持久 `cancel()` 调用。它不定义另一套作业状态机。
最终 `OUTPUT_NODE` 仅显示作业/产物 ID、本地基名以及
已验证字节数。

标准链接工作流见 `examples/remote_controlfoley_v2a.json`。

## 条件远程 GPU 冒烟

在指定的远程 GPU 验证设置上，将
`CONTROLFOLEY_COMFYUI_REMOTE_GPU_CONFIG` 设为包含
`operator_config_path`、`namespace_id`、`idempotency_key`、`deployment_id`、
`request_json`、`source`、`role` 与 `output_name` 的 JSON 对象，然后运行：

```sh
.venv/bin/pytest -m "gpu and controlfoley" -q tests/test_comfyui_remote.py
```

没有这些前置条件时，UI 冒烟被跳过并保持 **DEFERRED**。
