"""CPU-only validation for the Phase 8A Stable Audio 3 Small-SFX baseline."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

from nano_aural_runtime_stable_audio_3.baseline import (
    GpuPrerequisitesUnavailable,
    SchemaValidationError,
    StableAudio3DeploymentManifest,
    StableAudio3FixtureManifest,
    collect_sanitized_environment,
    detect_gpu_prerequisites,
    planned_result,
    require_gpu_prerequisites,
)

ROOT = Path(__file__).parents[1]
FIXTURES = ROOT / "benchmarks" / "fixtures" / "stable-audio-3"


def load(name: str):
    with (FIXTURES / name).open(encoding="utf-8") as handle:
        return json.load(handle)


class StableAudio3BaselineTests(unittest.TestCase):
    def test_locked_deployment_is_small_sfx_only_with_pending_weights(self) -> None:
        deployment = StableAudio3DeploymentManifest.from_dict(load("deployment.lock.json"))
        self.assertEqual("a0b57f5483c4588f827f3552b7d5c6ca2a9687be", deployment.source_revision)
        self.assertEqual("small-sfx", deployment.model_id)
        self.assertEqual("pytorch", deployment.backend)
        self.assertEqual(8, deployment.steps)
        self.assertEqual(1.0, deployment.cfg_scale)
        self.assertEqual("pending", deployment.weights.status)
        self.assertIsNone(deployment.weights.sha256)

    def test_duration_fixtures_are_exactly_5_30_and_120_seconds(self) -> None:
        durations = []
        for name in ("t2sfx-5s.json", "t2sfx-30s.json", "t2sfx-120s.json"):
            fixture = StableAudio3FixtureManifest.from_dict(load(name))
            self.assertEqual("audio.text_to_sfx", fixture.operation)
            self.assertEqual("pending", fixture.prompt.status)
            durations.append(fixture.duration_seconds)
        self.assertEqual([5.0, 30.0, 120.0], durations)

    def test_non_v1_models_and_durations_are_rejected(self) -> None:
        deployment = load("deployment.lock.json")
        deployment["model_id"] = "medium"
        with self.assertRaises(SchemaValidationError):
            StableAudio3DeploymentManifest.from_dict(deployment)
        fixture = load("t2sfx-5s.json")
        fixture["duration_seconds"] = 7.0
        with self.assertRaises(SchemaValidationError):
            StableAudio3FixtureManifest.from_dict(fixture)
        fixture = load("t2sfx-5s.json")
        fixture["operation"] = "audio_to_audio"
        with self.assertRaises(SchemaValidationError):
            StableAudio3FixtureManifest.from_dict(fixture)

    def test_placeholder_digest_is_rejected(self) -> None:
        deployment = load("deployment.lock.json")
        deployment["weights"] = {"status": "verified", "sha256": "a" * 64, "size_bytes": 1}
        with self.assertRaises(SchemaValidationError):
            StableAudio3DeploymentManifest.from_dict(deployment)

    def test_planned_result_contains_no_measurements_or_paths(self) -> None:
        deployment = StableAudio3DeploymentManifest.from_dict(load("deployment.lock.json"))
        fixture = StableAudio3FixtureManifest.from_dict(load("t2sfx-5s.json"))
        result = planned_result(deployment, fixture)
        self.assertEqual("planned", result["state"])
        self.assertIsNone(result["wall_time_seconds"])
        encoded = json.dumps(result)
        self.assertNotIn("/operator", encoded)
        self.assertNotIn("hostname", encoded)

    def test_environment_is_sanitized_and_gpu_diagnostics_are_actionable(self) -> None:
        environment = collect_sanitized_environment()
        self.assertEqual(
            {"schema_version", "captured_at", "python_version", "platform", "torch", "cuda"},
            set(environment),
        )
        prerequisites = detect_gpu_prerequisites(None, None)
        self.assertFalse(prerequisites.available)
        self.assertIn("STABLE_AUDIO_3_SOURCE_DIR is not set", prerequisites.reasons)

    def test_harness_writes_planned_template_without_executing(self) -> None:
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "stable_audio_3_baseline_harness",
            ROOT / "benchmarks" / "stable_audio_3_baseline.py",
        )
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "planned.json"
            code = module.main(
                [
                    "--deployment",
                    str(FIXTURES / "deployment.lock.json"),
                    "--fixture",
                    str(FIXTURES / "t2sfx-5s.json"),
                    "--write-result-template",
                    str(output),
                ]
            )
            self.assertEqual(0, code)
            payload = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual("planned", payload["state"])


@pytest.mark.gpu
def test_stable_audio_3_gpu_prerequisites_skip_without_a_configured_host() -> None:
    with pytest.raises(GpuPrerequisitesUnavailable) as error:
        require_gpu_prerequisites(None, None)
    pytest.skip(str(error.value))
