"""CPU-only Phase 2B adapter and local-CLI contracts."""

from __future__ import annotations

import json
import math
import os
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from hashlib import sha256
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime import (
    AdapterRegistry,
    CancellationToken,
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
    ModelDeployment,
    Runtime,
)
from nano_aural_runtime_controlfoley.adapter import (
    ControlFoleyAdapter,
    _cancel_process,
    _deployment_material,
    _guard_upstream_module_origins,
    _popen_isolation_kwargs,
    controlfoley_deployment_configuration,
    controlfoley_local_deployment,
)
from nano_aural_runtime_controlfoley.baseline import (
    FixtureManifest,
    compare_waveforms,
    fingerprint_file,
    fingerprint_text,
    load_json,
)
from nano_aural_runtime_controlfoley.cli import main as cli_main
from nano_aural_runtime_controlfoley.tasks import ControlFoleyLocalRequest, ControlFoleyTaskKind
from nano_aural_runtime_controlfoley.upstream_worker import run_locked_demo


class RunnerDouble:
    def __init__(self) -> None:
        self.loads = 0
        self.unloads = 0
        self.invocations = 0

    def validate(self, configuration):
        self.loads += 1
        return {
            "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
            "source_revision": configuration["source_revision"],
            "variant": configuration["variant"],
            "precision": configuration["precision"],
            "checkpoint_sha256": configuration["checkpoint_sha256"],
            "checkpoint_size_bytes": 1,
            "external_weight_sha256": {"ext_weights/v1-44.pth": "c" * 64},
        }

    def invoke(self, configuration, request, context):
        self.invocations += 1
        context.cancellation_token.raise_if_cancelled()
        content = b"FLAC-test-bytes"
        return content, {
            "sha256": sha256(content).hexdigest(),
            "size_bytes": len(content),
            "source_revision": configuration["source_revision"],
            "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
            "variant": configuration["variant"],
            "precision": configuration["precision"],
            "checkpoint_sha256": configuration["checkpoint_sha256"],
            "format": "flac",
            "worker": {
                "wall_time_seconds": 1.0,
                "peak_allocated_bytes": 0,
                "peak_reserved_bytes": 0,
            },
        }


class BadProvenanceRunner(RunnerDouble):
    def invoke(self, configuration, request, context):
        content, provenance = super().invoke(configuration, request, context)
        provenance["sha256"] = "0" * 64
        return content, provenance


class ExtraProvenanceRunner(RunnerDouble):
    def invoke(self, configuration, request, context):
        content, provenance = super().invoke(configuration, request, context)
        provenance["private_path"] = "/must/not/escape"
        return content, provenance


class BlockingRunner(RunnerDouble):
    def __init__(self) -> None:
        super().__init__()
        self.started = threading.Event()
        self.release = threading.Event()
        self.entries = 0
        self._entries_lock = threading.Lock()

    def invoke(self, configuration, request, context):
        with self._entries_lock:
            self.entries += 1
        self.started.set()
        if not self.release.wait(timeout=1):
            raise RuntimeError("test runner was not released")
        return super().invoke(configuration, request, context)


def deployment(adapter: ControlFoleyAdapter) -> ModelDeployment:
    configuration = {
        "manifest_path": "unused",
        "deployment_manifest_sha256": "a" * 64,
        "source_dir": "unused",
        "weights_dir": "unused",
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": "6858cd12a48d141201e3266e7abe1f38357a133e",
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": "b" * 64,
    }
    return ModelDeployment(
        deployment_id="controlfoley-test",
        descriptor=adapter.descriptor,
        fingerprint=configuration["deployment_manifest_sha256"],
        configuration=configuration,
    )


class ChildThatNeedsKill:
    def __init__(self) -> None:
        self.terminated = False
        self.killed = False
        self.returncode = None

    def poll(self):
        return self.returncode

    def terminate(self) -> None:
        self.terminated = True

    def kill(self) -> None:
        self.killed = True
        self.returncode = -9

    def wait(self, timeout):
        if not self.killed:
            raise subprocess.TimeoutExpired("fake-child", timeout)
        return self.returncode


class ControlFoleyAdapterTests(unittest.TestCase):
    def test_all_task_shapes_are_exact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            audio = root / "audio.wav"
            video.write_bytes(b"video")
            audio.write_bytes(b"audio")
            ControlFoleyLocalRequest(ControlFoleyTaskKind.V2A, video_path=video)
            ControlFoleyLocalRequest(ControlFoleyTaskKind.TV2A, video_path=video, prompt="sound")
            ControlFoleyLocalRequest(ControlFoleyTaskKind.TC_V2A, video_path=video, prompt="sound")
            ControlFoleyLocalRequest(
                ControlFoleyTaskKind.AC_V2A, video_path=video, reference_audio_path=audio
            )
            ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="sound")
            with self.assertRaises(ValueError):
                ControlFoleyLocalRequest(ControlFoleyTaskKind.TC_V2A, video_path=video)
            with self.assertRaises(ValueError):
                ControlFoleyLocalRequest(
                    ControlFoleyTaskKind.T2A, prompt="sound", duration_seconds=float("nan")
                )
            with self.assertRaises(TypeError):
                ControlFoleyLocalRequest("T2A", prompt="sound")  # type: ignore[arg-type]

    def test_load_invoke_unload_uses_generic_runtime_and_flac_artifact(self) -> None:
        runner = RunnerDouble()
        adapter = ControlFoleyAdapter(runner)
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(deployment(adapter))
        result = runtime.invoke(
            session,
            ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation("one"),
        )
        self.assertEqual("audio/flac", result.artifacts[0].media_type)
        self.assertEqual(b"FLAC-test-bytes", result.artifacts[0].content)
        self.assertEqual(
            "unmeasured; RTX 4090 parity evidence remains deferred",
            result.metadata["manifest"]["comparison"],
        )
        runtime.unload(session)
        self.assertEqual(1, runner.loads)
        self.assertEqual(1, runner.invocations)
        reloaded = runtime.load(deployment(adapter))
        runtime.unload(reloaded)
        self.assertEqual(2, runner.loads)

    def test_runtime_serializes_adapter_invocations_for_one_session(self) -> None:
        runner = BlockingRunner()
        adapter = ControlFoleyAdapter(runner)
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(deployment(adapter))
        invocation = ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird")
        errors = []

        def invoke(identifier):
            try:
                runtime.invoke(session, invocation.to_invocation(identifier))
            except Exception as error:
                errors.append(error)

        first = threading.Thread(target=invoke, args=("first",))
        second = threading.Thread(target=invoke, args=("second",))
        first.start()
        self.assertTrue(runner.started.wait(timeout=1))
        second.start()
        time.sleep(0.02)
        self.assertEqual(1, runner.entries)
        runner.release.set()
        first.join(timeout=1)
        second.join(timeout=1)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual([], errors)
        self.assertEqual(2, runner.invocations)
        runtime.unload(session)

    def test_precancel_rejects_without_runner_invocation(self) -> None:
        runner = RunnerDouble()
        adapter = ControlFoleyAdapter(runner)
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(deployment(adapter))
        token = CancellationToken()
        token.cancel("test")
        with self.assertRaises(InvocationCancelledError):
            runtime.invoke(
                session,
                ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation(
                    "two"
                ),
                ExecutionContext(cancellation_token=token),
            )
        self.assertEqual(0, runner.invocations)

    def test_runtime_rejects_bad_runner_provenance_and_deployment_fingerprint(self) -> None:
        adapter = ControlFoleyAdapter(BadProvenanceRunner())
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(deployment(adapter))
        with self.assertRaises(InvocationRejectedError):
            runtime.invoke(
                session,
                ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation(
                    "bad-provenance"
                ),
            )
        runtime.unload(session)

        wrong_fingerprint = ModelDeployment(
            deployment_id="controlfoley-test",
            descriptor=adapter.descriptor,
            fingerprint="not-the-manifest-sha",
            configuration=deployment(adapter).configuration,
        )
        with self.assertRaisesRegex(InvocationRejectedError, "fingerprint"):
            runtime.load(wrong_fingerprint)

    def test_extra_or_pathful_runner_provenance_is_rejected_without_stranding_session(self) -> None:
        adapter = ControlFoleyAdapter(ExtraProvenanceRunner())
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(deployment(adapter))
        with self.assertRaises(InvocationRejectedError):
            runtime.invoke(
                session,
                ControlFoleyLocalRequest(ControlFoleyTaskKind.T2A, prompt="bird").to_invocation(
                    "extra"
                ),
            )
        # The rejection is recoverable; the Core session remains READY until unload.
        self.assertEqual("ready", session.state.value)
        runtime.unload(session)

    def test_sealed_configuration_rejects_manifest_mutation(self) -> None:
        fixture = (
            Path(__file__).parents[1]
            / "benchmarks"
            / "fixtures"
            / "controlfoley"
            / "deployment.lock.json"
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            manifest_path = root / "deployment.json"
            manifest = json.loads(fixture.read_text(encoding="utf-8"))
            weights = root / "weights"
            all_weights = [manifest["checkpoint_relative_path"]] + [
                item["relative_path"] for item in manifest["external_weights"]
            ]
            for index, relative_path in enumerate(all_weights):
                path = weights / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes("weight-{0}".format(index).encode("utf-8"))
                if index == 0:
                    manifest["checkpoint"] = fingerprint_file(path).to_dict()
                else:
                    manifest["external_weights"][index - 1]["fingerprint"] = fingerprint_file(
                        path
                    ).to_dict()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            configuration = controlfoley_deployment_configuration(manifest_path, root, weights)
            _deployment_material(configuration)
            manifest["external_weights"][0]["fingerprint"] = fingerprint_file(
                weights / all_weights[1]
            ).to_dict()
            manifest["external_weights"][0]["fingerprint"]["sha256"] = sha256(
                b"changed"
            ).hexdigest()
            manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
            with self.assertRaisesRegex(InvocationRejectedError, "not bound"):
                _deployment_material(configuration)

    def test_core_source_has_no_controlfoley_or_comfyui_import(self) -> None:
        core = Path(__file__).parents[1] / "src" / "nano_aural_runtime"
        source = "\n".join(path.read_text(encoding="utf-8") for path in core.rglob("*.py"))
        self.assertNotIn("controlfoley", source.lower())
        self.assertNotIn("comfyui", source.lower())

    def test_cancelled_child_is_terminated_then_killed_if_needed(self) -> None:
        child = ChildThatNeedsKill()
        _cancel_process(child, 0.01)
        self.assertTrue(child.terminated)
        self.assertTrue(child.killed)

    def test_upstream_child_uses_new_process_session_on_posix(self) -> None:
        if os.name == "posix":
            self.assertEqual({"start_new_session": True}, _popen_isolation_kwargs())
        else:
            self.assertEqual({}, _popen_isolation_kwargs())

    def test_loaded_conflicting_upstream_module_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "source"
            source.mkdir()
            conflict = ModuleType("lib.flow_matching")
            conflict.__file__ = str(Path(directory) / "other" / "flow_matching.py")
            with patch.dict(sys.modules, {"lib.flow_matching": conflict}):
                with self.assertRaisesRegex(InvocationRejectedError, "does not originate"):
                    _guard_upstream_module_origins(source)

    def test_isolated_demo_post_execution_module_origin_guard(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, weights = root / "source", root / "weights"
            source.mkdir()
            weights.mkdir()
            (source / "controlfoley.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / "lib").mkdir()
            (source / "lib" / "__init__.py").write_text("", encoding="utf-8")
            (source / "lib" / "flow_matching.py").write_text("VALUE = 1\n", encoding="utf-8")
            (source / "demo.py").write_text(
                "import controlfoley\nfrom lib import flow_matching\n", encoding="utf-8"
            )
            run_locked_demo(source, weights, ())
            (source / "demo.py").write_text(
                "import sys\nfrom types import ModuleType\nsys.modules['lib.flow_matching'] = ModuleType('lib.flow_matching')\n",
                encoding="utf-8",
            )
            with self.assertRaisesRegex(RuntimeError, "has no source file"):
                run_locked_demo(source, weights, ())

    def test_isolated_demo_rejects_preloaded_conflict_before_demo_side_effect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source, weights, marker = root / "source", root / "weights", root / "marker"
            source.mkdir()
            weights.mkdir()
            (source / "demo.py").write_text(
                "from pathlib import Path\nPath(r'{}').write_text('ran')\n".format(marker),
                encoding="utf-8",
            )
            conflict = ModuleType("controlfoley")
            conflict.__file__ = str(root / "external" / "controlfoley.py")
            with patch.dict(sys.modules, {"controlfoley": conflict}):
                with self.assertRaisesRegex(RuntimeError, "did not load from locked source"):
                    run_locked_demo(source, weights, ())
            self.assertFalse(marker.exists())

    def test_local_cli_round_trip_with_injected_runner_and_refuses_overwrite(self) -> None:
        runner = RunnerDouble()
        adapter = ControlFoleyAdapter(runner)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            output = root / "result.flac"
            arguments = [
                "controlfoley",
                "local",
                "--manifest",
                str(root / "manifest.json"),
                "--source-dir",
                str(root),
                "--weights-dir",
                str(root),
                "--task",
                "T2A",
                "--prompt",
                "bird",
                "--output",
                str(output),
            ]
            with (
                patch(
                    "nano_aural_runtime_controlfoley.cli.ControlFoleyAdapter", return_value=adapter
                ),
                patch(
                    "nano_aural_runtime_controlfoley.cli.controlfoley_local_deployment",
                    return_value=deployment(adapter),
                ),
            ):
                self.assertEqual(0, cli_main(arguments))
            self.assertEqual(b"FLAC-test-bytes", output.read_bytes())
            with (
                patch(
                    "nano_aural_runtime_controlfoley.cli.ControlFoleyAdapter", return_value=adapter
                ),
                patch(
                    "nano_aural_runtime_controlfoley.cli.controlfoley_local_deployment",
                    return_value=deployment(adapter),
                ),
            ):
                self.assertEqual(2, cli_main(arguments))


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_controlfoley_adapter_gpu_smoke_and_parity_skip_without_sealed_local_host() -> None:
    """Run only when the operator explicitly supplies a sealed private JSON config.

    ``CONTROLFOLEY_P2B_GPU_CONFIG`` is a JSON object with deployment, fixture,
    source_dir, weights_dir, task, output_video (when needed), output_audio
    (when needed), and prompt (when needed).  The test invokes both the P2A
    direct oracle and P2B adapter and writes comparison evidence only to a temp
    directory; it makes no equality/threshold claim.
    """

    raw = os.environ.get("CONTROLFOLEY_P2B_GPU_CONFIG")
    if raw is None:
        pytest.skip("CONTROLFOLEY_P2B_GPU_CONFIG with sealed private inputs is not set")
    assert raw is not None
    config = json.loads(raw)
    required = {"deployment", "fixture", "source_dir", "weights_dir", "task"}
    if not isinstance(config, dict) or required - set(config):
        pytest.fail("CONTROLFOLEY_P2B_GPU_CONFIG is incomplete")
    task = ControlFoleyTaskKind(config["task"])
    fixture = FixtureManifest.from_dict(load_json(Path(config["fixture"])))
    assert fixture.task == task.value
    command = [
        sys.executable,
        "benchmarks/controlfoley_baseline.py",
        "--deployment",
        config["deployment"],
        "--fixture",
        config["fixture"],
        "--source-dir",
        config["source_dir"],
        "--weights-dir",
        config["weights_dir"],
        "--execute-upstream",
    ]
    request_kwargs: dict[str, object] = {
        "task": task,
        "duration_seconds": fixture.duration_seconds,
        "num_steps": fixture.num_steps,
        "guidance_scale": fixture.guidance_scale,
        "seed": fixture.seed,
    }
    if task in (
        ControlFoleyTaskKind.V2A,
        ControlFoleyTaskKind.TV2A,
        ControlFoleyTaskKind.TC_V2A,
        ControlFoleyTaskKind.AC_V2A,
    ):
        request_kwargs["video_path"] = Path(config["video"])
        command.extend(("--video", config["video"]))
        assert fixture.inputs[
            [item.role for item in fixture.inputs].index("video")
        ].fingerprint == fingerprint_file(Path(config["video"]))
    if task == ControlFoleyTaskKind.AC_V2A:
        request_kwargs["reference_audio_path"] = Path(config["audio"])
        command.extend(("--audio", config["audio"]))
        assert fixture.inputs[
            [item.role for item in fixture.inputs].index("reference_audio")
        ].fingerprint == fingerprint_file(Path(config["audio"]))
    if task in (ControlFoleyTaskKind.TV2A, ControlFoleyTaskKind.TC_V2A, ControlFoleyTaskKind.T2A):
        request_kwargs["prompt"] = config["prompt"]
        command.extend(("--prompt", config["prompt"]))
        assert fixture.inputs[
            [item.role for item in fixture.inputs].index("prompt")
        ].fingerprint == fingerprint_text(config["prompt"])
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        baseline_result = root / "baseline.json"
        command.extend(
            (
                "--output-dir",
                str(root / "one"),
                "--repeat-output-dir",
                str(root / "two"),
                "--result",
                str(baseline_result),
            )
        )
        subprocess.run(command, check=True, shell=False)
        adapter = ControlFoleyAdapter()
        registry = AdapterRegistry()
        registry.register(adapter)
        runtime = Runtime(registry)
        session = runtime.load(
            controlfoley_local_deployment(
                adapter,
                Path(config["deployment"]),
                Path(config["source_dir"]),
                Path(config["weights_dir"]),
            )
        )
        try:
            result = runtime.invoke(
                session,
                ControlFoleyLocalRequest(**request_kwargs).to_invocation("gpu"),  # type: ignore[arg-type]
            )
        finally:
            runtime.unload(session)
        baseline = json.loads(baseline_result.read_text(encoding="utf-8"))
        manifest = result.metadata["manifest"]
        assert manifest["source_revision"] == baseline["source_revision"]
        assert manifest["deployment_manifest_sha256"] == baseline["deployment_manifest_sha256"]
        assert manifest["artifact"]["sha256"] == result.artifacts[0].metadata["sha256"]
        assert manifest["parameters"] == {
            "task": fixture.task,
            "duration_seconds": fixture.duration_seconds,
            "cfg_strength": fixture.guidance_scale,
            "num_steps": fixture.num_steps,
            "seed": fixture.seed,
            "negative_prompt": None,
            "skip_video_composite": False,
            "mask_away_clip": False,
        }
        adapter_output = root / "adapter.flac"
        adapter_output.write_bytes(result.artifacts[0].content)
        direct_output = next((root / "one").rglob("*.flac"))
        metrics, waveform = compare_waveforms(direct_output, adapter_output)
        assert all(math.isfinite(value) for value in metrics.values())
        assert all(value > 0 for value in waveform.values())
        (root / "comparison.json").write_text(
            json.dumps({"metrics": metrics, "waveform": waveform}, sort_keys=True), encoding="utf-8"
        )
