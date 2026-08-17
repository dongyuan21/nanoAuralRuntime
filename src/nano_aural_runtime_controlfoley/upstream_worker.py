"""Isolated child process for one locked ControlFoley ``demo.py`` invocation.

It is launched only by the explicit ``--execute-upstream`` baseline command.
Torch and the upstream repository are imported here, never by the CPU-only
parent harness or Runtime Core.
"""

from __future__ import annotations

import argparse
import json
import os
import runpy
import sys
import tempfile
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional, Sequence


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--weights-dir", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("demo_argv", nargs=argparse.REMAINDER)
    return parser


@contextmanager
def isolated_demo_workspace(weights_dir: Path):
    """Expose verified weights at ``./model_weights`` without changing source."""

    with tempfile.TemporaryDirectory(prefix="nano-aural-controlfoley-") as directory:
        workspace = Path(directory)
        (workspace / "model_weights").symlink_to(weights_dir.resolve(), target_is_directory=True)
        yield workspace


def _module_origins_are_locked(source_dir: Path) -> None:
    """Reject module shadowing after the original demo has executed."""

    source = source_dir.resolve()
    for name, module in tuple(sys.modules.items()):
        if (
            name != "controlfoley"
            and not name.startswith("controlfoley.")
            and name != "lib.flow_matching"
        ):
            continue
        module_file = getattr(module, "__file__", None)
        if not isinstance(module_file, str):
            raise RuntimeError("upstream module {0} has no source file".format(name))
        try:
            Path(module_file).resolve().relative_to(source)
        except ValueError as error:
            raise RuntimeError(
                "upstream module {0} did not load from locked source".format(name)
            ) from error


def run_locked_demo(source_dir: Path, weights_dir: Path, demo_arguments: Sequence[str]) -> None:
    """Run the original file in the isolated weights workspace, then audit imports."""

    demo_path = source_dir / "demo.py"
    if not demo_path.is_file():
        raise RuntimeError("locked source is missing demo.py")
    # Refuse an already-shadowed upstream module before demo.py can run any
    # import-time side effect or write an output marker.
    _module_origins_are_locked(source_dir)
    previous_argv = sys.argv
    with isolated_demo_workspace(weights_dir) as workspace:
        previous_cwd = Path.cwd()
        try:
            os.chdir(workspace)
            sys.path.insert(0, str(source_dir.resolve()))
            sys.argv = [str(demo_path), *demo_arguments]
            try:
                runpy.run_path(str(demo_path), run_name="__main__")
            except SystemExit as error:
                if error.code not in (None, 0):
                    raise
            _module_origins_are_locked(source_dir)
        finally:
            sys.argv = previous_argv
            os.chdir(previous_cwd)
            if sys.path and sys.path[0] == str(source_dir.resolve()):
                del sys.path[0]


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments = _parser().parse_args(argv)
    if not arguments.demo_argv or arguments.demo_argv[0] != "--":
        raise SystemExit("demo argv must follow --")
    demo_arguments = arguments.demo_argv[1:]
    # Deliberately local to the GPU child process.
    import torch  # type: ignore[import-not-found]

    torch.cuda.reset_peak_memory_stats()
    started = time.perf_counter()
    run_locked_demo(arguments.source_dir, arguments.weights_dir, demo_arguments)
    elapsed = time.perf_counter() - started
    report = {
        "wall_time_seconds": elapsed,
        "peak_allocated_bytes": int(torch.cuda.max_memory_allocated()),
        "peak_reserved_bytes": int(torch.cuda.max_memory_reserved()),
    }
    arguments.report.parent.mkdir(parents=True, exist_ok=True)
    with arguments.report.open("w", encoding="utf-8") as handle:
        json.dump(report, handle, sort_keys=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
