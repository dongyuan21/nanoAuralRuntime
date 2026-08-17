"""Operator secret handling. Never log Hugging Face tokens or private paths."""

from __future__ import annotations

from typing import Mapping, Optional

_TOKEN_NAMES = ("HF_TOKEN", "HUGGING_FACE_HUB_TOKEN", "HUGGINGFACE_HUB_TOKEN")


class SecretRedactionError(ValueError):
    """A secret-bearing environment mapping is unsafe to record."""


def huggingface_token(environ: Mapping[str, str]) -> Optional[str]:
    for name in _TOKEN_NAMES:
        value = environ.get(name)
        if isinstance(value, str) and value.strip():
            return value
    return None


def redact_environment(environ: Mapping[str, str]) -> dict[str, str]:
    redacted = {}
    for key, value in environ.items():
        if key in _TOKEN_NAMES or "TOKEN" in key or "SECRET" in key or "PASSWORD" in key:
            redacted[key] = "<redacted>"
            continue
        if not isinstance(value, str):
            raise SecretRedactionError("environment values must be strings")
        redacted[key] = value
    if any(name in redacted and redacted[name] != "<redacted>" for name in _TOKEN_NAMES):
        raise SecretRedactionError("Hugging Face token was not redacted")
    return redacted
