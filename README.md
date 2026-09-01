# nanoAuralRuntime

nanoAuralRuntime 是一套音频原生、模型无关的 Runtime Core、适配器 SDK 与持久化服务栈。ControlFoley 是首个垂直工作流；当前生产适配器还包括 Stable Audio 3 Small-SFX（`audio.text_to_sfx`）与 Woosh V2A（`audio.video_to_sfx`）。单一模型或 UI 框架不是系统边界。

状态：活跃的 pre-alpha。软件/CPU 阶段已实现，但不是已完成的发行。所需的 RTX 4090 对齐、基准、Worker、远程与 UI 证据仍为 **延期（DEFERRED）**，并阻塞发行。

## 架构与权威

依赖方向固定向下：

```text
Frontend -> Workflow -> Durable Service / Local Executor
         -> Runtime Core -> Model Adapter -> Original Model Backend
```

Runtime Core 不依赖 ControlFoley、PostgreSQL、存储、API 或 ComfyUI。ComfyUI 是可选、可拆除的前端；持久化数据库仍是资产、任务、尝试与产物状态的权威。

权威文档：

- [项目章程](PROJECT_CHARTER.md)
- [架构](ARCHITECTURE.md)
- [路线图](ROADMAP.md)
- [阶段状态](plans/STATUS.md)
- [ADR 0003：隔离的 Worker 环境](docs/decisions/0003-model-specific-worker-environments.md)
- [ADR 0004：适配器插件与 Worker 路由](docs/decisions/0004-adapter-plugin-and-worker-routing.md)

## 安装

开发安装与 CI 一致：

```sh
python -m pip install 'setuptools==82.0.1'
python -m pip install --no-build-isolation -e '.[dev,postgres-test]'
```

PostgreSQL 运行时支持是显式的 `durable-postgres` extra。无头 wheel / sdist 的构建与审计见 [发行就绪与产物契约](docs/release-readiness.md)：

```sh
mkdir -p dist
python tools/release_artifacts.py --output-dir dist
python -m pip install --no-index --no-deps \
  dist/nano_aural_runtime-0.1.0.dev0-py3-none-any.whl
```

基础发行包没有强制第三方依赖。ControlFoley、Stable Audio 3 与 Woosh V2A 的源码与权重由运营方提供，不进入 extra、wheel 或参考容器。许可边界见 [NOTICE](NOTICE) 与 [第二适配器许可边界](docs/second-adapter-license-boundary.md)。

## 无头命令

下列帮助必须在没有模型权重、API 凭据、PostgreSQL 或网络的情况下可用：

```sh
nano-aural --help
nano-aural-controlfoley --help
nano-aural-remote --help
python -m nano_aural_runtime_cli --help
python -m nano_aural_runtime_remote --help
python -m nano_aural_runtime.durable.service --help
python -m nano_aural_runtime.durable.reference_worker --help
python -m nano_aural_runtime.durable.recovery --help
```

`nano-aural controlfoley local` 是本地、由运营方控制的路径。`nano-aural-remote` 与公开的持久化 API 交换已验证的资产、任务与产物标识。配置与恢复见 [持久化运维指南](docs/durable-operations.md)。

## 持久化参考环境

`compose.yaml` 与 `ops/Dockerfile.api` 是 CPU 假发布参考栈，不是生产 HTTP/TLS 边界。密钥、镜像钉死与恢复步骤见 [持久化运维指南](docs/durable-operations.md)。没有容器 daemon 时 Docker 校验保持未跑；`gpu-deferred` profile 不能满足硬件 Gate。

## 发行与安全

- [发行就绪与产物契约](docs/release-readiness.md)
- [ComfyUI 兼容与拆除](docs/comfyui-compatibility-removal.md)
- [变更日志](CHANGELOG.md)
- [安全策略](SECURITY.md)
- [许可证](LICENSE) 与 [声明](NOTICE)

发行产物不得包含权重、媒体、缓存、密钥、私有路径、生成证据或归档研究。安全问题请通过 `SECURITY.md` 中的私有流程报告。
