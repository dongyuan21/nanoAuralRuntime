# Unified SFX workflows (Phase 10A)

These workflows sit above adapters. They do not change Runtime Core
`load()` / `invoke()` / `unload()`, and they are not a ComfyUI job authority.

| Workflow | Adapter | Operation | Mux |
| --- | --- | --- | --- |
| `sfx.text_generate` | Stable Audio 3 Small-SFX | `audio.text_to_sfx` | no |
| `sfx.video_generate` | ControlFoley, Woosh-DVFlow-8s (default), Woosh-VFlow-8s | ControlFoley tasks or `audio.video_to_sfx` | no |
| `sfx.generate_and_mux` | same video backends, after WAV exists | not an adapter operation | yes |

Mux is an explicit post-adapter step. Woosh and Stable Audio 3 adapters emit WAV
only. ComfyUI node names in `integrations/comfyui_compat/sfx_mapping.py` are
optional display mappings; omitting `integrations/` must not break headless
paths. No Woosh T2A, Flow, or DFlow nodes are part of this mapping.
