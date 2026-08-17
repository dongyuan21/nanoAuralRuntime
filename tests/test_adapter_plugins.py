# pyright: reportMissingImports=false
from __future__ import annotations

import ast
import sys
from pathlib import Path
from uuid import uuid4

import pytest

from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime_cli.main import controlfoley_alias, main
from nano_aural_runtime_controlfoley.adapter import ControlFoleyAdapter
from nano_aural_runtime_workers.capabilities import (
    WorkerCapability,
    WorkerCapabilityError,
    reject_operator_owned_job_fields,
)
from nano_aural_runtime_workers.controlfoley import ControlFoleyDurableInvocationBuilder
from nano_aural_runtime_workers.plugins import DEFAULT_PLUGIN_CATALOG
from nano_aural_runtime_workers.registry import (
    BuilderRegistryError,
    DurableInvocationBuilderRegistry,
)

ROOT = Path(__file__).resolve().parents[1]


def _capability(**overrides):
    values = {
        "runtime_environment_id": "controlfoley-pytorch-locked",
        "adapter_id": "controlfoley",
        "supported_operations": frozenset({"V2A", "TV2A", "TC-V2A", "AC-V2A", "T2A"}),
        "supported_backends": ("upstream_parity",),
        "source_revision": "6858cd12a48d141201e3266e7abe1f38357a133e",
        "checkpoint_manifest_sha256": "a" * 64,
        "device": "cuda",
    }
    values.update(overrides)
    return WorkerCapability(**values)


def _operator_configuration():
    return {
        "manifest_path": "/operator/manifest.json",
        "deployment_manifest_sha256": "a" * 64,
        "source_dir": "/operator/source",
        "weights_dir": "/operator/weights",
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": "6858cd12a48d141201e3266e7abe1f38357a133e",
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": "b" * 64,
    }


def test_plugin_catalog_declares_three_adapters_without_implementing_new_models():
    plugins = {plugin.adapter_id: plugin for plugin in DEFAULT_PLUGIN_CATALOG.all_plugins()}
    assert set(plugins) == {"controlfoley", "stable-audio-3-small-sfx", "woosh-v2a"}
    assert plugins["controlfoley"].implemented is True
    assert plugins["stable-audio-3-small-sfx"].implemented is True
    assert plugins["woosh-v2a"].implemented is False
    assert plugins["woosh-v2a"].default_backend == "dvflow-8s"
    assert plugins["woosh-v2a"].backends == ("dvflow-8s", "vflow-8s")
    assert plugins["woosh-v2a"].operations == frozenset({"audio.video_to_sfx"})
    assert "audio.text_to_sfx" not in plugins["woosh-v2a"].operations


def test_routing_modules_do_not_import_torch_or_new_model_packages():
    for relative in (
        "src/nano_aural_runtime_workers/plugins.py",
        "src/nano_aural_runtime_workers/capabilities.py",
        "src/nano_aural_runtime_workers/registry.py",
    ):
        tree = ast.parse((ROOT / relative).read_text(encoding="utf-8"))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name.split(".", 1)[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.append(node.module.split(".", 1)[0])
        assert "torch" not in names
        assert "nano_aural_runtime_stable_audio_3" not in names
        assert "nano_aural_runtime_woosh" not in names
    tree = ast.parse((ROOT / "src/nano_aural_runtime_cli/main.py").read_text(encoding="utf-8"))
    names = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.extend(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module.split(".", 1)[0])
    assert "torch" not in names
    assert "nano_aural_runtime_woosh" not in names


def test_cli_help_lists_declared_frontends(capsys):
    assert main(["--help"]) == 0
    output = capsys.readouterr().out
    assert "controlfoley" in output
    assert "stable-audio-3" in output
    assert "woosh" in output
    assert "dflow" not in output.lower()


def test_cli_unimplemented_frontends_fail_closed(capsys):
    assert main(["woosh", "video-to-sfx"]) == 2
    assert main(["woosh", "text-to-sfx"]) == 2
    err = capsys.readouterr().err
    assert "not installed" in err


def test_cli_unknown_frontend_fail_closed(capsys):
    assert main(["unknown"]) == 2
    assert "unknown adapter frontend" in capsys.readouterr().err


def test_controlfoley_alias_forwards_help(capsys):
    assert controlfoley_alias(["--help"]) == 0
    assert "usage:" in capsys.readouterr().out


def test_builder_registry_registers_controlfoley_only():
    registry = DurableInvocationBuilderRegistry()
    builder = ControlFoleyDurableInvocationBuilder(ControlFoleyAdapter(), _operator_configuration())
    registry.register(builder)
    assert registry.registered_adapter_ids() == ("controlfoley",)
    assert registry.get("controlfoley") is builder
    with pytest.raises(BuilderRegistryError):
        registry.get("woosh-v2a")


def test_builder_registry_rejects_unimplemented_adapter():
    class _Fake:
        @property
        def adapter_id(self) -> str:
            return "woosh-v2a"

        @property
        def operations(self) -> frozenset:
            return frozenset({"audio.video_to_sfx"})

        def core_deployment(self, deployment):
            raise AssertionError("unreachable")

        def build(self, deployment, job, inputs):
            raise AssertionError("unreachable")

    with pytest.raises(BuilderRegistryError, match="not installed"):
        DurableInvocationBuilderRegistry().register(_Fake())


def test_capability_matches_controlfoley_deployment_without_backend_id():
    capability = _capability()
    deployment = DeploymentRecord(
        str(uuid4()),
        "controlfoley",
        "controlfoley",
        "a" * 64,
        manifest={"deployment_manifest_sha256": "a" * 64},
    )
    job = JobRecord(
        str(uuid4()),
        "tenant",
        "key",
        "c" * 64,
        {
            "task": "T2A",
            "prompt": "rain",
            "duration_seconds": 8.0,
            "num_steps": 25,
            "guidance_scale": 4.5,
            "seed": 1,
        },
        deployment.deployment_id,
        (),
    )
    capability.validate(deployment, job)


def test_capability_rejects_backend_and_environment_mismatch():
    capability = _capability(
        adapter_id="woosh-v2a",
        runtime_environment_id="woosh-v2a-pytorch-2.8.0-cu128",
        supported_operations=frozenset({"audio.video_to_sfx"}),
        supported_backends=("dvflow-8s",),
    )
    deployment = DeploymentRecord(
        str(uuid4()),
        "woosh",
        "woosh-v2a",
        "a" * 64,
        manifest={
            "backend_id": "vflow-8s",
            "runtime_environment_id": "woosh-v2a-pytorch-2.8.0-cu128",
        },
    )
    with pytest.raises(WorkerCapabilityError, match="backend"):
        capability.validate(deployment)


def test_operator_owned_fields_are_rejected_on_jobs():
    with pytest.raises(WorkerCapabilityError):
        reject_operator_owned_job_fields({"prompt": "x", "source_dir": "/secret"})
    with pytest.raises(WorkerCapabilityError):
        reject_operator_owned_job_fields({"solver": "dopri5"})
    reject_operator_owned_job_fields(
        {
            "task": "T2A",
            "prompt": "rain",
            "duration_seconds": 8.0,
            "num_steps": 25,
            "guidance_scale": 4.5,
            "seed": 1,
        }
    )


def test_importing_plugin_catalog_does_not_load_torch():
    before = {name for name in sys.modules if name == "torch" or name.startswith("torch.")}
    __import__("nano_aural_runtime_workers.plugins")
    __import__("nano_aural_runtime_cli")
    after = {name for name in sys.modules if name == "torch" or name.startswith("torch.")}
    assert after == before
