# ADR 0003：隔离模型特定的 Worker 环境

**状态：** 已接受
**日期：** 2026-08-18

## 背景

ControlFoley 是第一个适配器。后续适配器是 Stable Audio 3
Small-SFX 与 Sony Woosh V2A（仅 `Woosh-VFlow-8s` 与 `Woosh-DVFlow-8s`）。
它们的上游栈钉死了不兼容的 Python 与 PyTorch 线路：

| 环境 | Python | Torch / Torchaudio | 官方 CUDA wheel 线路 |
| --- | --- | --- | --- |
| ControlFoley | 运营方锁定的既有 checkout | 运营方锁定的既有 checkout | 既有 ControlFoley 主机 |
| Stable Audio 3 | `>=3.10` | `2.7.1` / `2.7.1` | `cu126` |
| Woosh V2A | `>=3.12` | `2.8.0` / `2.8.0` | `cu128` |

Runtime Core 与持久化控制面必须保持不含 `torch` 以及任何上游模型包。单一根 virtualenv 或单一「通用 GPU Worker 镜像」无法在不混用模型后端的情况下满足这些钉死版本。

## 决策

每个已声明的模型族在其自身的 Worker 环境中运行。环境身份由运营方拥有，属于 Worker 就绪的一部分，而不是远程 Job 字段。

前三个环境标识符为：

```text
controlfoley-pytorch-<locked>
stable-audio-3-pytorch-2.7.1-cu126
woosh-v2a-pytorch-2.8.0-cu128
```

根包 `nano_aural_runtime` 保持无依赖。适配器包仅可在隔离子进程中，或在已经包含该后端的环境中导入上游后端。它们不得安装或导入另一族的栈。

Job 认领仅在以下全部与 Worker 能力描述符匹配时合法：`deployment.adapter_id`、密封的后端身份、`runtime_environment_id` 与部署指纹。远程请求仍不能携带源码路径、权重路径、Python 模块、求解器设置或环境标识符。

## 后果

- 运营方供给三套 Worker 镜像或 virtualenv，而不是一套。
- 加入第四个适配器需要新的环境标识符与新的密封 Deployment；它不扩大 Core 契约。
- CI 继续在不安装任何模型栈的情况下演练 Core、持久化与适配器 CPU 契约。
- 硬件证据仍按环境独立，并可各自为 `DEFERRED`。

## 被否决的替代方案

共享的带 torch 根 extra 被否决，因为它会让 Core 与每一个 Worker 镜像依赖单一模型的 pin。在同一镜像中安装全部三套栈被否决，因为 Python 与 CUDA wheel 线路冲突。在公开 Job 请求上暴露环境或求解器选择被否决，因为这些值是运营方拥有的 Deployment 字段。
