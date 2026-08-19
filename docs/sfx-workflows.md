# 统一 SFX 工作流（Phase 10A）

这些工作流位于适配器之上。它们不改变 Runtime Core 的
`load()` / `invoke()` / `unload()`，也不是 ComfyUI 的作业权威。

| 工作流 | 适配器 | 操作 | Mux |
| --- | --- | --- | --- |
| `sfx.text_generate` | Stable Audio 3 Small-SFX | `audio.text_to_sfx` | 否 |
| `sfx.video_generate` | ControlFoley、Woosh-DVFlow-8s（默认）、Woosh-VFlow-8s | ControlFoley 任务或 `audio.video_to_sfx` | 否 |
| `sfx.generate_and_mux` | 相同的视频后端，在 WAV 已存在之后 | 不是适配器操作 | 是 |

Mux 是显式的适配器后步骤。Woosh 与 Stable Audio 3 适配器仅输出 WAV。
`integrations/comfyui_compat/sfx_mapping.py` 中的 ComfyUI 节点名称是可选的显示映射；省略 `integrations/` 不得破坏无头路径。本映射不包含任何 Woosh T2A、Flow 或 DFlow 节点。
