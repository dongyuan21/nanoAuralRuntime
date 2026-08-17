"""Local-only Stable Audio 3 Small-SFX CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from nano_aural_runtime import AdapterRegistry, Runtime

from .adapter import StableAudio3Adapter, stable_audio_3_local_deployment
from .tasks import StableAudio3LocalRequest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nano-aural")
    root = parser.add_subparsers(dest="frontend", required=True)
    stable = root.add_parser("stable-audio-3").add_subparsers(dest="command", required=True)
    generate = stable.add_parser("text-to-sfx")
    generate.add_argument("--manifest", required=True, type=Path)
    generate.add_argument("--source-dir", required=True, type=Path)
    generate.add_argument("--weights-dir", required=True, type=Path)
    generate.add_argument("--prompt", required=True)
    generate.add_argument("--duration", required=True, type=float)
    generate.add_argument("--seed", default=42, type=int)
    generate.add_argument("--output", required=True, type=Path)
    return parser


def _safe_output(path: Path) -> Path:
    if ".." in path.parts:
        raise ValueError("output path traversal is not allowed")
    resolved = path.resolve()
    if resolved.exists():
        raise ValueError("refusing to overwrite existing output")
    if resolved.suffix.lower() != ".wav":
        raise ValueError("output must use the .wav extension")
    return resolved


def _write_new_file_atomically(path: Path, content: bytes) -> None:
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


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        output = _safe_output(arguments.output)
        request = StableAudio3LocalRequest(
            prompt=arguments.prompt,
            duration_seconds=arguments.duration,
            seed=arguments.seed,
        )
        adapter = StableAudio3Adapter()
        registry = AdapterRegistry()
        registry.register(adapter)
        deployment = stable_audio_3_local_deployment(
            adapter, arguments.manifest, arguments.source_dir, arguments.weights_dir
        )
        runtime = Runtime(registry)
        session = runtime.load(deployment)
        try:
            result = runtime.invoke(session, request.to_invocation("local-stable-audio-3"))
        finally:
            runtime.unload(session)
        _write_new_file_atomically(output, result.artifacts[0].content)
        print(json.dumps({"operation": "audio.text_to_sfx", "output": str(output)}, sort_keys=True))
    except Exception:
        print(
            "nano-aural: request failed; check inputs and operator configuration",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
