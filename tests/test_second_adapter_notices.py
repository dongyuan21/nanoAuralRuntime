# pyright: reportMissingImports=false
from __future__ import annotations

from pathlib import Path

from nano_aural_runtime_workers.secrets import huggingface_token, redact_environment

ROOT = Path(__file__).resolve().parents[1]


def test_notice_records_stable_audio_and_woosh_license_boundaries():
    notice = (ROOT / "NOTICE").read_text(encoding="utf-8")
    assert "https://github.com/Stability-AI/stable-audio-3" in notice
    assert "https://huggingface.co/stabilityai/stable-audio-3-small-sfx" in notice
    assert "https://ai.google.dev/gemma/terms" in notice
    assert "https://github.com/SonyResearch/Woosh" in notice
    assert "https://huggingface.co/hkchengrex/MMAudio" in notice
    assert "CC BY-NC 4.0 includes a NonCommercial restriction" in notice
    assert "HF_TOKEN" in notice
    assert "must not log, commit, or ship those tokens" in notice


def test_huggingface_tokens_are_redacted_and_never_echoed():
    token = "hf_this_is_not_a_real_token"
    environ = {
        "HF_TOKEN": token,
        "HUGGING_FACE_HUB_TOKEN": token,
        "PATH": "/usr/bin",
    }
    assert huggingface_token(environ) == token
    redacted = redact_environment(environ)
    encoded = str(redacted)
    assert token not in encoded
    assert redacted["HF_TOKEN"] == "<redacted>"
    assert redacted["PATH"] == "/usr/bin"
