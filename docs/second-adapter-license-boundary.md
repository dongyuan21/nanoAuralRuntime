# Second-adapter license and gated-access boundary (Phase 11)

This is a software license/notice slice. It does not complete a combined
product release. RTX 4090 evidence for ControlFoley, Stable Audio 3, and Woosh
V2A remains **DEFERRED**.

## Stable Audio 3 Small-SFX

- Source: `https://github.com/Stability-AI/stable-audio-3`
- Gated model: `https://huggingface.co/stabilityai/stable-audio-3-small-sfx`
- Operators must accept the Stability AI Community License and Gemma Terms of
  Use at acquisition time.
- Hugging Face tokens stay in the operator environment as `HF_TOKEN` or
  `HUGGING_FACE_HUB_TOKEN`. Do not commit tokens, write them into manifests, or
  print them in logs.

## Woosh V2A

- Source: `https://github.com/SonyResearch/Woosh` tag `v1.0.0`
- Code notices: MIT for the majority of Woosh; Apache-2.0 for adapted MM-AUDIO /
  MotionFormer paths.
- Weights: CC BY-NC 4.0 for in-scope `v1.0.0` archives.
- Synchformer: `hkchengrex/MMAudio` `ext_weights/synchformer_state_dict.pth`.
- Do not download or inspect Woosh-Flow, Woosh-DFlow, TextConditionerA, or
  Woosh T2A for this product path.

See [NOTICE](../NOTICE).
