"""CPU-only validation for the Phase 9A Woosh V2A VFlow/DVFlow baseline."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime_woosh.baseline import (
    GpuPrerequisitesUnavailable,
    SchemaValidationError,
    WooshV2ADeploymentManifest,
    WooshV2AFixtureManifest,
    baseline_matrix,
    collect_sanitized_environment,
    detect_gpu_prerequisites,
    planned_result,
    require_gpu_prerequisites,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures" / "woosh-v2a"


def load(name: str):
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class WooshV2ABaselineTests(unittest.TestCase):
    def test_locked_deployments_are_vflow_and_dvflow_only(self) -> None:
        dvflow = WooshV2ADeploymentManifest.from_dict(load("dvflow-8s.lock.json"))
        vflow = WooshV2ADeploymentManifest.from_dict(load("vflow-8s.lock.json"))
        self.assertEqual("f6ff658efc6d63dee9959964cd75c63415910a19", dvflow.source_revision)
        self.assertEqual("v1.0.0", dvflow.source_tag)
        self.assertEqual("woosh-v2a", dvflow.adapter_id)
        self.assertEqual("dvflow-8s", dvflow.backend_id)
        self.assertEqual("sample_euler", dvflow.sampler_policy.sampler)
        self.assertEqual(4, dvflow.sampler_policy.num_steps)
        self.assertEqual((0.0, 0.5, 0.5, 0.3), dvflow.sampler_policy.renoise)
        self.assertEqual(3.0, dvflow.sampler_policy.cfg)
        self.assertIsNone(dvflow.sampler_policy.solver_method)
        self.assertEqual("vflow-8s", vflow.backend_id)
        self.assertEqual("flowmatching_integrate", vflow.sampler_policy.sampler)
        self.assertEqual("dopri5", vflow.sampler_policy.solver_method)
        self.assertEqual(4.5, vflow.sampler_policy.cfg)
        self.assertIsNone(vflow.sampler_policy.num_steps)
        self.assertEqual("pending", dvflow.backend_weights.status)
        self.assertEqual("pending", dvflow.synchformer_weights.status)
        self.assertEqual("Woosh-DVFlow-8s.zip", dvflow.backend_archive.filename)
        self.assertEqual("Woosh-VFlow-8s.zip", vflow.backend_archive.filename)

    def test_fixtures_are_an_explicit_eight_second_window(self) -> None:
        video_only = WooshV2AFixtureManifest.from_dict(load("v2sfx-8s-video-only.json"))
        prompted = WooshV2AFixtureManifest.from_dict(load("v2sfx-8s-video-prompt.json"))
        self.assertEqual("audio.video_to_sfx", video_only.operation)
        self.assertEqual(0.0, video_only.window_start_seconds)
        self.assertEqual(8.0, video_only.window_end_seconds)
        self.assertEqual("omitted", video_only.prompt.status)
        self.assertEqual("pending", prompted.prompt.status)
        self.assertEqual("pending", video_only.video.status)

    def test_baseline_matrix_covers_backends_prompt_and_self_repeat(self) -> None:
        pairs = baseline_matrix()
        self.assertEqual(6, len(pairs))
        backends = {name for name, _, _ in pairs}
        self.assertEqual({"dvflow-8s.lock.json", "vflow-8s.lock.json"}, backends)
        self.assertTrue(any(self_repeat for _, _, self_repeat in pairs))
        for deployment_name, fixture_name, self_repeat in pairs:
            deployment = WooshV2ADeploymentManifest.from_dict(load(deployment_name))
            fixture = WooshV2AFixtureManifest.from_dict(load(fixture_name))
            result = planned_result(deployment, fixture, self_repeat=self_repeat)
            self.assertEqual("planned", result["state"])
            self.assertEqual("self_repeat" if self_repeat else "single", result["comparison"])

    def test_out_of_scope_backends_and_archives_are_rejected(self) -> None:
        deployment = load("dvflow-8s.lock.json")
        deployment["backend_id"] = "dflow"
        with self.assertRaises(SchemaValidationError):
            WooshV2ADeploymentManifest.from_dict(deployment)
        deployment = load("dvflow-8s.lock.json")
        deployment["backend_archive"] = dict(deployment["backend_archive"])
        deployment["backend_archive"]["filename"] = "Woosh-Flow.zip"
        with self.assertRaises(SchemaValidationError):
            WooshV2ADeploymentManifest.from_dict(deployment)
        fixture = load("v2sfx-8s-video-only.json")
        fixture["operation"] = "audio.text_to_sfx"
        with self.assertRaises(SchemaValidationError):
            WooshV2AFixtureManifest.from_dict(fixture)
        fixture = load("v2sfx-8s-video-only.json")
        fixture["window_end_seconds"] = 5.0
        with self.assertRaises(SchemaValidationError):
            WooshV2AFixtureManifest.from_dict(fixture)

    def test_placeholder_digest_is_rejected(self) -> None:
        deployment = load("dvflow-8s.lock.json")
        deployment["backend_weights"] = {"status": "verified", "sha256": "a" * 64, "size_bytes": 1}
        with self.assertRaises(SchemaValidationError):
            WooshV2ADeploymentManifest.from_dict(deployment)

    def test_planned_result_contains_no_measurements_or_paths(self) -> None:
        deployment = WooshV2ADeploymentManifest.from_dict(load("dvflow-8s.lock.json"))
        fixture = WooshV2AFixtureManifest.from_dict(load("v2sfx-8s-video-only.json"))
        result = planned_result(deployment, fixture)
        self.assertIsNone(result["wall_time_seconds"])
        self.assertFalse(result["prompt_present"])
        encoded = json.dumps(result)
        self.assertNotIn("/operator", encoded)
        self.assertNotIn("hostname", encoded)

    def test_environment_is_sanitized_and_gpu_diagnostics_are_actionable(self) -> None:
        environment = collect_sanitized_environment()
        self.assertEqual(
            {"schema_version", "captured_at", "python_version", "platform", "torch", "cuda"},
            set(environment),
        )
        prerequisites = detect_gpu_prerequisites(None, None, None)
        self.assertFalse(prerequisites.available)
        self.assertIn("WOOSH_SOURCE_DIR is not set", prerequisites.reasons)
        self.assertIn("WOOSH_SYNCHFORMER_PATH is not set", prerequisites.reasons)

    def test_package_does_not_import_torch_or_out_of_scope_models(self) -> None:
        import ast

        package = ROOT / "src" / "nano_aural_runtime_woosh"
        names = []
        for path in package.rglob("*.py"):
            tree = ast.parse(path.read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    names.extend(alias.name.split(".", 1)[0] for alias in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    names.append(node.module.split(".", 1)[0])
        self.assertNotIn("torch", names)
        self.assertNotIn("woosh", names)

    def test_harness_writes_planned_template_without_executing(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "woosh_v2a_baseline_harness",
            ROOT / "benchmarks" / "woosh_v2a_baseline.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "planned.json"
            code = module.main(
                [
                    "--deployment",
                    str(FIXTURES / "dvflow-8s.lock.json"),
                    "--fixture",
                    str(FIXTURES / "v2sfx-8s-video-only.json"),
                    "--write-result-template",
                    str(output),
                ]
            )
            self.assertEqual(0, code)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("planned", payload["state"])
            self.assertEqual("dvflow-8s", payload["backend_id"])


@pytest.mark.gpu
def test_woosh_v2a_gpu_prerequisites_skip_without_a_configured_host() -> None:
    with pytest.raises(GpuPrerequisitesUnavailable) as error:
        require_gpu_prerequisites(None, None, None)
    pytest.skip(str(error.value))
