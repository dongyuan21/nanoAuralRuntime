"""Roadmap Phase 5C true-omission and reverse-dependency CI contracts."""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterator

import pytest  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).parents[1]
SMOKE = ROOT / "tests/_p5c_headless_smoke.py"


def _copy_snapshot(snapshot: Path, included_integrations: tuple[str, ...]) -> None:
    shutil.copytree(
        ROOT / "src",
        snapshot / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    tests = snapshot / "tests"
    tests.mkdir()
    shutil.copy2(SMOKE, tests / SMOKE.name)
    if included_integrations:
        destination = snapshot / "integrations"
        destination.mkdir()
        shutil.copy2(ROOT / "integrations/__init__.py", destination / "__init__.py")
        for package in included_integrations:
            shutil.copytree(
                ROOT / "integrations" / package,
                destination / package,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )


@pytest.mark.parametrize(
    ("label", "included", "omitted"),
    (
        ("embedded-omitted", ("comfyui_remote",), ("comfyui",)),
        ("remote-omitted", ("comfyui",), ("comfyui_remote",)),
        ("all-omitted", (), ("comfyui", "comfyui_remote")),
    ),
)
def test_physical_omission_keeps_headless_cpu_paths_running_in_isolation(
    tmp_path: Path,
    label: str,
    included: tuple[str, ...],
    omitted: tuple[str, ...],
) -> None:
    snapshot = (tmp_path / label).resolve()
    snapshot.mkdir()
    try:
        snapshot.relative_to(ROOT.resolve())
    except ValueError:
        pass
    else:
        raise AssertionError("omission snapshot must live outside the repository")
    _copy_snapshot(snapshot, included)
    for package in omitted:
        assert not (snapshot / "integrations" / package).exists()
    work = snapshot / "detached-cwd"
    work.mkdir()
    environment = dict(os.environ)
    for name in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP"):
        environment.pop(name, None)

    completed = subprocess.run(
        [
            sys.executable,
            "-E",
            "-S",
            str(snapshot / "tests" / SMOKE.name),
            str(snapshot),
            str(ROOT.resolve()),
            ",".join(included),
        ],
        cwd=work,
        env=environment,
        check=False,
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stdout + completed.stderr
    assert "P5C_HEADLESS_OK" in completed.stdout
    assert str(ROOT.resolve()) not in completed.stdout
    assert str(ROOT.resolve()) not in completed.stderr


def _absolute_imports(path: Path) -> Iterator[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            yield from (alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
            yield node.module


def test_headless_and_remote_packages_have_no_reverse_ui_or_model_imports() -> None:
    headless = (
        ROOT / "src/nano_aural_runtime",
        ROOT / "src/nano_aural_runtime_cli",
        ROOT / "src/nano_aural_runtime_controlfoley",
        ROOT / "src/nano_aural_runtime_remote",
        ROOT / "src/nano_aural_runtime_stable_audio_3",
        ROOT / "src/nano_aural_runtime_workers",
    )
    for package in headless:
        for path in package.rglob("*.py"):
            for imported in _absolute_imports(path):
                assert imported != "comfy" and not imported.startswith("comfy.")
                assert imported != "integrations" and not imported.startswith("integrations.")

    prohibited_remote = (
        "torch",
        "controlfoley",
        "nano_aural_runtime",
        "nano_aural_runtime_controlfoley",
        "nano_aural_runtime_workers",
    )
    remote_packages = (
        ROOT / "src/nano_aural_runtime_remote",
        ROOT / "integrations/comfyui_remote",
    )
    for package in remote_packages:
        for path in package.rglob("*.py"):
            for imported in _absolute_imports(path):
                assert not any(
                    imported == name or imported.startswith(name + ".")
                    for name in prohibited_remote
                )
