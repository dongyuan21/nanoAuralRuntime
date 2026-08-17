#!/usr/bin/env python3
"""Stable Audio 3 Small-SFX Phase 8A provenance harness.

Without operator-supplied source/weights this command only validates manifests.
It never downloads gated Hugging Face files and never claims parity.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from nano_aural_runtime_stable_audio_3.baseline import (
    SchemaValidationError,
    StableAudio3DeploymentManifest,
    StableAudio3FixtureManifest,
    detect_gpu_prerequisites,
    load_json,
    planned_result,
    write_json,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--deployment", type=Path, required=True)
    parser.add_argument("--fixture", type=Path, required=True)
    parser.add_argument(
        "--source-dir", type=Path, default=os.environ.get("STABLE_AUDIO_3_SOURCE_DIR")
    )
    parser.add_argument(
        "--weights-dir", type=Path, default=os.environ.get("STABLE_AUDIO_3_WEIGHTS_DIR")
    )
    parser.add_argument("--write-result-template", type=Path)
    parser.add_argument("--execute-upstream", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        deployment = StableAudio3DeploymentManifest.from_dict(load_json(arguments.deployment))
        fixture = StableAudio3FixtureManifest.from_dict(load_json(arguments.fixture))
        if arguments.write_result_template is not None:
            write_json(arguments.write_result_template, planned_result(deployment, fixture))
        if arguments.execute_upstream:
            source = None if arguments.source_dir is None else str(arguments.source_dir)
            weights = None if arguments.weights_dir is None else str(arguments.weights_dir)
            detected = detect_gpu_prerequisites(source, weights)
            if not detected.available:
                raise SchemaValidationError("; ".join(detected.reasons))
            raise SchemaValidationError(
                "upstream execution is deferred until RTX 4090 evidence is recorded"
            )
    except (OSError, json.JSONDecodeError, SchemaValidationError, TypeError, ValueError):
        sys.stderr.write("stable-audio-3 baseline: manifest validation failed\n")
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
