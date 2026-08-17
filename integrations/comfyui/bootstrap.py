"""Operator-owned bootstrap configuration for the optional embedded frontend."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

OPERATOR_CONFIG_ENV = "NANO_AURAL_COMFYUI_OPERATOR_CONFIG"


class OperatorConfigError(ValueError):
    """An operator configuration is absent or unsafe to use."""


@dataclass(frozen=True)
class EmbeddedOperatorConfig:
    """Sealed local deployment locations, never exposed as node inputs."""

    manifest_path: Path
    source_dir: Path
    weights_dir: Path


def _absolute_path(value: Any, field: str) -> Path:
    if not isinstance(value, str) or not value:
        raise OperatorConfigError("operator config {0} must be a non-empty string".format(field))
    path = Path(value)
    if not path.is_absolute():
        raise OperatorConfigError("operator config paths must be absolute")
    return path.resolve()


def load_operator_config(path: Path) -> EmbeddedOperatorConfig:
    """Load a strict JSON document without retaining unknown fields or secrets."""

    config_path = Path(path)
    if not config_path.is_file():
        raise OperatorConfigError("operator config file is unavailable")
    try:
        with config_path.open(encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError) as error:
        raise OperatorConfigError("operator config file is not valid JSON") from error
    if not isinstance(value, Mapping) or set(value) != {
        "schema_version",
        "manifest_path",
        "source_dir",
        "weights_dir",
    }:
        raise OperatorConfigError("operator config has missing or unexpected fields")
    if (
        isinstance(value["schema_version"], bool)
        or not isinstance(value["schema_version"], int)
        or value["schema_version"] != 1
    ):
        raise OperatorConfigError("operator config schema_version must be 1")
    manifest_path = _absolute_path(value["manifest_path"], "manifest_path")
    source_dir = _absolute_path(value["source_dir"], "source_dir")
    weights_dir = _absolute_path(value["weights_dir"], "weights_dir")
    if not manifest_path.is_file() or not source_dir.is_dir() or not weights_dir.is_dir():
        raise OperatorConfigError("operator deployment inputs are unavailable")
    return EmbeddedOperatorConfig(manifest_path, source_dir, weights_dir)


def operator_config_from_environment(
    environ: Optional[Mapping[str, str]] = None,
) -> EmbeddedOperatorConfig:
    values = os.environ if environ is None else environ
    raw = values.get(OPERATOR_CONFIG_ENV)
    if not isinstance(raw, str) or not raw:
        raise OperatorConfigError(
            "set {0} to an absolute operator config JSON path".format(OPERATOR_CONFIG_ENV)
        )
    config_path = Path(raw)
    if not config_path.is_absolute():
        raise OperatorConfigError("{0} must contain an absolute path".format(OPERATOR_CONFIG_ENV))
    return load_operator_config(config_path)
