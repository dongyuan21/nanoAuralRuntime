"""Strict operator configuration for the removable remote ComfyUI frontend."""

from __future__ import annotations

import json
import math
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

REMOTE_CONFIG_ENV = "NANO_AURAL_COMFYUI_REMOTE_CONFIG"
_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{0,127}$")


class RemoteOperatorConfigError(ValueError):
    """Remote operator configuration is absent, unsafe, or malformed."""


@dataclass(frozen=True)
class RemoteOperatorConfig:
    base_url: str
    token_env: str
    allow_loopback_http: bool
    download_dir: Path
    transport_timeout_seconds: float
    max_upload_bytes: int
    max_download_bytes: int
    max_wait_seconds: float
    max_poll_iterations: int


def _positive_number(value: Any, field: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise RemoteOperatorConfigError("{0} must be positive".format(field))
    return float(value)


def _positive_integer(value: Any, field: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise RemoteOperatorConfigError("{0} must be a positive integer".format(field))
    return value


def load_remote_operator_config(path: Path) -> RemoteOperatorConfig:
    config_path = Path(path)
    if not config_path.is_absolute() or not config_path.is_file():
        raise RemoteOperatorConfigError("remote operator config must be an available absolute path")
    try:
        with config_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        raise RemoteOperatorConfigError("remote operator config is not valid JSON") from None
    expected = {
        "schema_version",
        "base_url",
        "token_env",
        "allow_loopback_http",
        "download_dir",
        "transport_timeout_seconds",
        "max_upload_bytes",
        "max_download_bytes",
        "max_wait_seconds",
        "max_poll_iterations",
    }
    if not isinstance(value, Mapping) or set(value) != expected:
        raise RemoteOperatorConfigError("remote operator config has missing or unexpected fields")
    if (
        isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != 1
    ):
        raise RemoteOperatorConfigError("remote operator config schema_version must be 1")
    base_url = value["base_url"]
    token_env = value["token_env"]
    allow_loopback_http = value["allow_loopback_http"]
    download_dir_value = value["download_dir"]
    if not isinstance(base_url, str) or not base_url:
        raise RemoteOperatorConfigError("base_url must be non-empty")
    if not isinstance(token_env, str) or not _ENV_NAME.fullmatch(token_env):
        raise RemoteOperatorConfigError("token_env must be a safe environment variable name")
    if token_env == REMOTE_CONFIG_ENV:
        raise RemoteOperatorConfigError("token_env must be separate from operator configuration")
    if not isinstance(allow_loopback_http, bool):
        raise RemoteOperatorConfigError("allow_loopback_http must be boolean")
    if not isinstance(download_dir_value, str) or not download_dir_value:
        raise RemoteOperatorConfigError("download_dir must be non-empty")
    download_dir = Path(download_dir_value)
    if not download_dir.is_absolute() or not download_dir.is_dir():
        raise RemoteOperatorConfigError("download_dir must be an available absolute directory")
    transport_timeout = _positive_number(
        value["transport_timeout_seconds"], "transport_timeout_seconds"
    )
    max_upload = _positive_integer(value["max_upload_bytes"], "max_upload_bytes")
    max_download = _positive_integer(value["max_download_bytes"], "max_download_bytes")
    max_wait = _positive_number(value["max_wait_seconds"], "max_wait_seconds")
    max_polls = _positive_integer(value["max_poll_iterations"], "max_poll_iterations")
    if transport_timeout > 120 or max_wait > 86400 or max_polls > 10000:
        raise RemoteOperatorConfigError("remote polling or transport limit is too large")
    if max_upload > 2**40 or max_download > 2**40:
        raise RemoteOperatorConfigError("remote transfer limit is too large")
    return RemoteOperatorConfig(
        base_url=base_url,
        token_env=token_env,
        allow_loopback_http=allow_loopback_http,
        download_dir=download_dir.resolve(),
        transport_timeout_seconds=transport_timeout,
        max_upload_bytes=max_upload,
        max_download_bytes=max_download,
        max_wait_seconds=max_wait,
        max_poll_iterations=max_polls,
    )


def remote_operator_config_from_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> tuple[RemoteOperatorConfig, str]:
    values = os.environ if environ is None else environ
    raw_path = values.get(REMOTE_CONFIG_ENV)
    if not isinstance(raw_path, str) or not raw_path:
        raise RemoteOperatorConfigError(
            "set {0} to an absolute strict JSON config path".format(REMOTE_CONFIG_ENV)
        )
    config = load_remote_operator_config(Path(raw_path))
    token = values.get(config.token_env)
    if not isinstance(token, str) or not token.strip():
        raise RemoteOperatorConfigError("the configured bearer-token environment value is absent")
    return config, token
