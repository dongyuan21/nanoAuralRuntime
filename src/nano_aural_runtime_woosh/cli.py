"""Local-only Woosh V2A CLI."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Optional, Sequence

from nano_aural_runtime import AdapterRegistry, Runtime

from .adapter import WooshV2AAdapter, woosh_v2a_local_deployment
from .baseline import BACKEND_DVFLOW, SUPPORTED_BACKENDS
from .tasks import WooshV2ALocalRequest


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="nano-aural")
    root = parser.add_subparsers(dest="frontend", required=True)
    woosh = root.add_parser("woosh").add_subparsers(dest="command", required=True)
    generate = woosh.add_parser("video-to-sfx")
    generate.add_argument("--deployment", default=BACKEND_DVFLOW, choices=SUPPORTED_BACKENDS)
    generate.add_argument("--manifest", required=True, type=Path)
    generate.add_argument("--source-dir", required=True, type=Path)
    generate.add_argument("--weights-dir", required=True, type=Path)
    generate.add_argument("--synchformer-path", required=True, type=Path)
    generate.add_argument("--video", required=True, type=Path)
    generate.add_argument("--prompt")
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
        request = WooshV2ALocalRequest(
            video_path=arguments.video,
            seed=arguments.seed,
            prompt=arguments.prompt,
        )
        adapter = WooshV2AAdapter()
        registry = AdapterRegistry()
        registry.register(adapter)
        deployment = woosh_v2a_local_deployment(
            adapter,
            arguments.manifest,
            arguments.source_dir,
            arguments.weights_dir,
            arguments.synchformer_path,
        )
        if deployment.configuration["backend_id"] != arguments.deployment:
            raise ValueError("manifest backend does not match --deployment")
        runtime = Runtime(registry)
        session = runtime.load(deployment)
        try:
            result = runtime.invoke(session, request.to_invocation("local-woosh-v2a"))
        finally:
            runtime.unload(session)
        _write_new_file_atomically(output, result.artifacts[0].content)
        print(
            json.dumps(
                {
                    "operation": "audio.video_to_sfx",
                    "backend_id": arguments.deployment,
                    "output": str(output),
                },
                sort_keys=True,
            )
        )
    except Exception:
        print(
            "nano-aural: request failed; check inputs and operator configuration",
            file=sys.stderr,
        )
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
