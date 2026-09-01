# 统一 SFX 工作流

工作流在适配器之上组合一次完整任务。它们不实现模型，也不把 ComfyUI 当成作业权威。

| 工作流 | 做什么 | 适配器 | Mux |
| --- | --- | --- | --- |
| `sfx.text_generate` | 由文本生成音效 WAV | Stable Audio 3 Small-SFX（`audio.text_to_sfx`） | 否 |
| `sfx.video_generate` | 由视频生成音效 WAV | ControlFoley，或 Woosh V2A（默认 `dvflow-8s`，可选 `vflow-8s`） | 否 |
| `sfx.generate_and_mux` | 在已有 WAV 之后与视频封装 | 与视频生成相同的后端；mux 不是适配器步骤 | 是 |

Woosh 与 Stable Audio 3 适配器只输出 WAV。需要画面+声音一体文件时，走 `sfx.generate_and_mux`，不要把封装塞进模型调用。

可选 ComfyUI 节点名称是显示映射，见 `integrations/comfyui_compat/sfx_mapping.py`。去掉 `integrations/` 不得破坏无头 CLI/API。本目录不提供 Woosh T2A、Flow 或 DFlow 工作流。
