# nanoAuralRuntime

nanoAuralRuntime 用来在本机或持久化服务上运行音频基础模型：加载已声明的部署、调用适配器、校验产物并交出去。它不把某一个模型或某一个 UI 当成系统本身。

当前可以做的事：

| 任务 | 适配器 | 工作流 |
| --- | --- | --- |
| 文本生成音效 | Stable Audio 3 Small-SFX（`audio.text_to_sfx`） | `sfx.text_generate` |
| 视频生成音效 | ControlFoley，或 Woosh V2A（默认 `dvflow-8s`） | `sfx.video_generate` |
| 生成后再与视频封装 | 适配器只产出 WAV；mux 在适配器之后 | `sfx.generate_and_mux` |

本地路径由 CLI 直接 `load` / `invoke` / `unload`，不需要 HTTP、数据库或 ComfyUI。

持久化路径先把媒体变成已验证、按全文件 SHA-256 寻址的资产，再进入任务。Worker 调用同一套 Runtime Core；对外只暴露一个已验证的获胜产物。模型调用成功不等于任务成功。

ComfyUI 是可选前端。上游源码与权重由运营方提供，不进入本仓库、wheel 或参考容器。许可见 [NOTICE](NOTICE) 与 [第二适配器许可边界](docs/second-adapter-license-boundary.md)。

当前是 pre-alpha：软件路径可用，指定宿主上的 RTX 4090 证据仍为延期，并阻塞发行声称。

## 分层

```text
Frontend -> Workflow -> Durable Service / Local Executor
         -> Runtime Core -> Model Adapter -> Original Model Backend
```

Runtime Core 模型无关，适配器生命周期只有 `load()`、`invoke()`、`unload()`。ControlFoley、Stable Audio 3 与 Woosh V2A 使用隔离的 Worker 环境；根包不含 torch。

## 使用

```sh
python -m pip install 'setuptools==82.0.1'
python -m pip install --no-build-isolation -e '.[dev,postgres-test]'
```

PostgreSQL 运行时支持是 `durable-postgres` extra。无头入口在没有权重、凭据或网络时也应能打印帮助：

```sh
nano-aural --help
nano-aural-controlfoley --help
nano-aural-remote --help
python -m nano_aural_runtime.durable.service --help
```

本地 ControlFoley：`nano-aural controlfoley local`。远程客户端与公开 API 只交换已验证的资产、任务与产物标识。

## 文档

做什么、怎么用见 [docs/README.md](docs/README.md)。系统怎么分层见 [ARCHITECTURE.md](ARCHITECTURE.md)。安全问题按 [SECURITY.md](SECURITY.md) 私下报告。
