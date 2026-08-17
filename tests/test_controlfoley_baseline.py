"""CPU-only validation for the Phase 2A ControlFoley baseline harness."""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime_controlfoley.baseline import (
    BaselineResultManifest,
    ControlFoleyDeploymentManifest,
    FixtureManifest,
    GpuPrerequisitesUnavailable,
    SchemaValidationError,
    collect_sanitized_environment,
    detect_gpu_prerequisites,
    fingerprint_file,
    manifest_sha256,
    require_gpu_prerequisites,
    validate_materialized_weights,
    verify_result_bindings,
)
from nano_aural_runtime_controlfoley.upstream_worker import isolated_demo_workspace

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures" / "controlfoley"


def load(name: str):
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class ControlFoleyBaselineTests(unittest.TestCase):
    def test_locked_deployment_manifest_is_valid_and_checkpoint_is_pending(self) -> None:
        deployment = ControlFoleyDeploymentManifest.from_dict(load("deployment.lock.json"))
        self.assertEqual("6858cd12a48d141201e3266e7abe1f38357a133e", deployment.source_revision)
        self.assertEqual("pending", deployment.checkpoint.status)
        self.assertIsNone(deployment.checkpoint.sha256)
        self.assertEqual(4, len(deployment.external_weights))
        self.assertTrue(
            all(weight.fingerprint.status == "pending" for weight in deployment.external_weights)
        )

    def test_all_declared_task_fixtures_are_valid_without_media_or_paths(self) -> None:
        expected = {"V2A", "TV2A", "TC-V2A", "AC-V2A", "T2A"}
        fixtures = [
            FixtureManifest.from_dict(load(name))
            for name in ("v2a.json", "tv2a.json", "tc-v2a.json", "ac-v2a.json", "t2a.json")
        ]
        self.assertEqual(expected, {fixture.task for fixture in fixtures})
        for fixture in fixtures:
            for item in fixture.inputs:
                self.assertEqual("pending", item.fingerprint.status)
                self.assertIsNone(item.fingerprint.sha256)

    def test_unknown_source_lock_and_placeholder_digest_are_rejected(self) -> None:
        deployment = load("deployment.lock.json")
        deployment["source_revision"] = "a" * 40
        with self.assertRaises(SchemaValidationError):
            ControlFoleyDeploymentManifest.from_dict(deployment)

        deployment = load("deployment.lock.json")
        deployment["precision"] = "bf16"
        with self.assertRaises(SchemaValidationError):
            ControlFoleyDeploymentManifest.from_dict(deployment)

        deployment = load("deployment.lock.json")
        deployment["variant"] = "other"
        with self.assertRaises(SchemaValidationError):
            ControlFoleyDeploymentManifest.from_dict(deployment)

    def test_task_input_roles_are_exact_and_tc_v2a_requires_text(self) -> None:
        fixture = load("tc-v2a.json")
        self.assertEqual(["video", "prompt"], [item["role"] for item in fixture["inputs"]])
        fixture["inputs"][1]["role"] = "temporal_control"
        fixture["inputs"][1]["kind"] = "control"
        with self.assertRaises(SchemaValidationError):
            FixtureManifest.from_dict(fixture)

        deployment = load("deployment.lock.json")
        deployment["checkpoint"] = {"status": "verified", "sha256": "0" * 64, "size_bytes": 1}
        with self.assertRaises(SchemaValidationError):
            ControlFoleyDeploymentManifest.from_dict(deployment)

    def test_weight_preflight_checks_the_main_checkpoint_and_four_external_paths(self) -> None:
        manifest = load("deployment.lock.json")
        with tempfile.TemporaryDirectory() as directory:
            weights_dir = Path(directory)
            paths = [manifest["checkpoint_relative_path"]] + [
                item["relative_path"] for item in manifest["external_weights"]
            ]
            for index, relative_path in enumerate(paths):
                candidate = weights_dir / relative_path
                candidate.parent.mkdir(parents=True, exist_ok=True)
                candidate.write_bytes("weight-{0}".format(index).encode("utf-8"))
                fingerprint = fingerprint_file(candidate).to_dict()
                if index == 0:
                    manifest["checkpoint"] = fingerprint
                else:
                    manifest["external_weights"][index - 1]["fingerprint"] = fingerprint
            deployment = ControlFoleyDeploymentManifest.from_dict(manifest)
            self.assertEqual((), validate_materialized_weights(deployment, weights_dir))
            (weights_dir / paths[-1]).unlink()
            self.assertIn(
                "required weight is missing",
                validate_materialized_weights(deployment, weights_dir)[0],
            )

    def test_isolated_workspace_exposes_only_the_supplied_weights_to_a_fake_demo(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            weights = root / "verified-weights"
            weights.mkdir()
            (weights / "marker.txt").write_text("verified", encoding="utf-8")
            source = root / "source"
            source.mkdir()
            demo = source / "demo.py"
            demo.write_text(
                "from pathlib import Path\n"
                "Path('observed.txt').write_text(Path('model_weights/marker.txt').read_text())\n",
                encoding="utf-8",
            )
            with isolated_demo_workspace(weights) as workspace:
                subprocess.run(
                    [sys.executable, str(demo.resolve())], cwd=workspace, check=True, shell=False
                )
                self.assertEqual(
                    "verified", (workspace / "observed.txt").read_text(encoding="utf-8")
                )
                self.assertEqual(weights.resolve(), (workspace / "model_weights").resolve())

    def test_result_schema_never_allows_measurements_in_a_planned_result(self) -> None:
        empty_metrics = {
            "peak": None,
            "rms": None,
            "mae": None,
            "max_absolute_error": None,
            "waveform_cosine_similarity": None,
            "mel_spectrogram_distance": None,
        }
        result = {
            "schema_version": 1,
            "run_id": "pending-run",
            "fixture_id": "v2a-8s-pending-inputs",
            "deployment_id": "controlfoley-large44k-fp32-v1",
            "deployment_manifest_sha256": hashlib.sha256(b"deployment").hexdigest(),
            "fixture_manifest_sha256": hashlib.sha256(b"fixture").hexdigest(),
            "source_revision": "6858cd12a48d141201e3266e7abe1f38357a133e",
            "checkpoint": {"status": "pending", "sha256": None, "size_bytes": None},
            "input_fingerprints": {
                "video": {"status": "pending", "sha256": None, "size_bytes": None}
            },
            "parameters": {"task": "V2A"},
            "state": "planned",
            "environment": collect_sanitized_environment(),
            "waveform": {"channels": None, "samples": None, "sample_rate": None},
            "wall_time_seconds": None,
            "peak_allocated_bytes": None,
            "peak_reserved_bytes": None,
            "self_repeat_metrics": empty_metrics,
            "self_repeat_thresholds": empty_metrics,
            "repeat_evidence": [],
        }
        self.assertEqual("planned", BaselineResultManifest.from_dict(result).state)
        result["wall_time_seconds"] = 1.0
        with self.assertRaises(SchemaValidationError):
            BaselineResultManifest.from_dict(result)

    def test_offline_binding_verifier_rejects_parameter_drift(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            video = root / "video.mp4"
            video.write_bytes(b"video")
            fixture_data = load("v2a.json")
            fixture_data["inputs"][0]["fingerprint"] = fingerprint_file(video).to_dict()
            fixture = FixtureManifest.from_dict(fixture_data)
            deployment_data = load("deployment.lock.json")
            weights = root / "weights"
            paths = [deployment_data["checkpoint_relative_path"]] + [
                item["relative_path"] for item in deployment_data["external_weights"]
            ]
            for index, relative_path in enumerate(paths):
                path = weights / relative_path
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_bytes("weight-{0}".format(index).encode("utf-8"))
                fingerprint = fingerprint_file(path).to_dict()
                if index == 0:
                    deployment_data["checkpoint"] = fingerprint
                else:
                    deployment_data["external_weights"][index - 1]["fingerprint"] = fingerprint
            deployment = ControlFoleyDeploymentManifest.from_dict(deployment_data)
            output = root / "output.flac"
            output.write_bytes(b"output")
            output_fingerprint = fingerprint_file(output).to_dict()
            metrics = {
                "peak": 1.0,
                "rms": 0.5,
                "mae": 0.0,
                "max_absolute_error": 0.0,
                "waveform_cosine_similarity": 1.0,
                "mel_spectrogram_distance": 0.0,
            }
            evidence = {
                "output": output_fingerprint,
                "waveform": {"channels": 1, "samples": 1, "sample_rate": 44_100},
                "wall_time_seconds": 1.0,
                "peak_allocated_bytes": 1,
                "peak_reserved_bytes": 1,
            }
            result_data = {
                "schema_version": 1,
                "run_id": "verified-repeat",
                "fixture_id": fixture.fixture_id,
                "deployment_id": deployment.deployment_id,
                "deployment_manifest_sha256": manifest_sha256(deployment.to_dict()),
                "fixture_manifest_sha256": manifest_sha256(fixture.to_dict()),
                "source_revision": deployment.source_revision,
                "checkpoint": deployment.checkpoint.to_dict(),
                "input_fingerprints": {"video": fixture.inputs[0].fingerprint.to_dict()},
                "parameters": {
                    "task": fixture.task,
                    "variant": deployment.variant,
                    "precision": deployment.precision,
                    "video": fixture.inputs[0].fingerprint.to_dict(),
                    "audio": None,
                    "duration_seconds": fixture.duration_seconds,
                    "cfg_strength": fixture.guidance_scale,
                    "num_steps": fixture.num_steps,
                    "seed": fixture.seed,
                    "skip_video_composite": False,
                    "mask_away_clip": False,
                    "output": {"path_redacted": True, "repeat_specific": True},
                    "prompt": None,
                    "negative_prompt": None,
                },
                "state": "completed",
                "environment": collect_sanitized_environment(),
                "waveform": {"channels": 1, "samples": 1, "sample_rate": 44_100},
                "wall_time_seconds": 1.0,
                "peak_allocated_bytes": 1,
                "peak_reserved_bytes": 1,
                "self_repeat_metrics": metrics,
                "self_repeat_thresholds": {name: None for name in metrics},
                "repeat_evidence": [evidence, evidence],
            }
            result = BaselineResultManifest.from_dict(result_data)
            verify_result_bindings(deployment, fixture, result)
            result_data["parameters"]["seed"] = fixture.seed + 1
            with self.assertRaises(SchemaValidationError):
                verify_result_bindings(
                    deployment, fixture, BaselineResultManifest.from_dict(result_data)
                )
            result_data["parameters"]["seed"] = fixture.seed
            for name, value in (
                ("negative_prompt", {"status": "verified"}),
                ("skip_video_composite", True),
                ("mask_away_clip", True),
            ):
                result_data["parameters"][name] = value
                with self.assertRaises(SchemaValidationError):
                    verify_result_bindings(
                        deployment, fixture, BaselineResultManifest.from_dict(result_data)
                    )
                result_data["parameters"][name] = None if name == "negative_prompt" else False

    def test_environment_is_sanitized_and_gpu_diagnostics_are_actionable(self) -> None:
        environment = collect_sanitized_environment()
        self.assertEqual(
            {"schema_version", "captured_at", "python_version", "platform", "torch", "cuda"},
            set(environment),
        )
        self.assertNotIn("hostname", environment)
        prerequisites = detect_gpu_prerequisites(None, None)
        self.assertFalse(prerequisites.available)
        self.assertIn("CONTROLFOLEY_SOURCE_DIR is not set", prerequisites.reasons)


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_controlfoley_gpu_prerequisites_skip_without_a_configured_host() -> None:
    with pytest.raises(GpuPrerequisitesUnavailable) as error:
        require_gpu_prerequisites(None, None)
    pytest.skip(str(error.value))
