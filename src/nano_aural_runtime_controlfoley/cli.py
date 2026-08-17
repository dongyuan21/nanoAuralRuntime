"""Local-only ControlFoley CLI; it has no remote or durable-service semantics."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence

from nano_aural_runtime import AdapterRegistry, Runtime

from .adapter import ControlFoleyAdapter, controlfoley_local_deployment
from .tasks import ControlFoleyLocalRequest, ControlFoleyTaskKind


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nano-aural")
    root = parser.add_subparsers(dest="frontend", required=True)
    controlfoley = root.add_parser("controlfoley").add_subparsers(dest="mode", required=True)
    local = controlfoley.add_parser("local")
    local.add_argument("--manifest", required=True, type=Path)
    local.add_argument("--source-dir", required=True, type=Path)
    local.add_argument("--weights-dir", required=True, type=Path)
    local.add_argument(
        "--task", required=True, choices=[task.value for task in ControlFoleyTaskKind]
    )
    local.add_argument("--video", type=Path)
    local.add_argument("--audio", type=Path)
    local.add_argument("--prompt")
    local.add_argument("--seed", default=42, type=int)
    local.add_argument("--output", required=True, type=Path)
    return parser


def _safe_output(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("output path traversal is not allowed")
    resolved = path.resolve()
    if resolved.exists():
        raise ValueError("refusing to overwrite existing output")
    if resolved.suffix.lower() != ".flac":
        raise ValueError("output must use the .flac extension")
    return resolved


def _write_new_file_atomically(path: Path, content: bytes) -> None:
    """Write a result without ever replacing a pre-existing user output."""

    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".nano-aural-", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError as error:
            raise ValueError("refusing to overwrite existing output") from error
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _json_default(value: Any) -> Any:
    if isinstance(value, Mapping):
        return dict(value)
    raise TypeError("result manifest contains a non-JSON value")


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        output = _safe_output(arguments.output)
        request = ControlFoleyLocalRequest(
            task=ControlFoleyTaskKind(arguments.task),
            video_path=arguments.video,
            reference_audio_path=arguments.audio,
            prompt=arguments.prompt,
            seed=arguments.seed,
        )
        adapter = ControlFoleyAdapter()
        registry = AdapterRegistry()
        registry.register(adapter)
        deployment = controlfoley_local_deployment(
            adapter, arguments.manifest, arguments.source_dir, arguments.weights_dir
        )
        runtime = Runtime(registry)
        session = runtime.load(deployment)
        try:
            result = runtime.invoke(session, request.to_invocation("local-controlfoley"))
        finally:
            runtime.unload(session)
        _write_new_file_atomically(output, result.artifacts[0].content)
        print(json.dumps(result.metadata["manifest"], sort_keys=True, default=_json_default))
    except Exception:
        # Adapter/backend errors may carry operator paths, prompts, URLs, or
        # credentials in their message or chained causes.  The CLI boundary
        # therefore emits only a stable public diagnostic.
        print(
            "nano-aural: request failed; check inputs and operator configuration",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
