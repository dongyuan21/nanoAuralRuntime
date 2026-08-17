# pyright: reportMissingImports=false
from __future__ import annotations

import io
import wave
from pathlib import Path
from unittest.mock import patch

from nano_aural_runtime import AdapterRegistry, InvocationRejectedError, Runtime
from nano_aural_runtime_cli.main import main as dispatcher_main
from nano_aural_runtime_stable_audio_3.adapter import (
    OfficialStableAudio3Runner,
    StableAudio3Adapter,
    stable_audio_3_local_deployment,
)
from nano_aural_runtime_stable_audio_3.baseline import SUPPORTED_SAMPLE_RATE
from nano_aural_runtime_stable_audio_3.cli import main as cli_main
from nano_aural_runtime_stable_audio_3.tasks import StableAudio3LocalRequest

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "benchmarks" / "fixtures" / "stable-audio-3" / "deployment.lock.json"


def _silent_wav(duration_seconds: float) -> bytes:
    frames = int(duration_seconds * SUPPORTED_SAMPLE_RATE)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SUPPORTED_SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * frames * 2)
    return buffer.getvalue()


class RunnerDouble:
    def validate(self, configuration):
        return {"source_revision": configuration["source_revision"]}

    def invoke(self, configuration, request, context):
        context.cancellation_token.raise_if_cancelled()
        return _silent_wav(request.duration_seconds)


def test_request_schema_accepts_only_v1_durations():
    StableAudio3LocalRequest("metal impact", 5.0, 1)
    try:
        StableAudio3LocalRequest("metal impact", 7.0, 1)
    except ValueError:
        return
    raise AssertionError("non-V1 duration must fail")


def test_adapter_load_invoke_unload_with_injected_runner(tmp_path: Path):
    source = tmp_path / "source"
    weights = tmp_path / "weights"
    source.mkdir()
    weights.mkdir()
    adapter = StableAudio3Adapter(RunnerDouble())
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    deployment = stable_audio_3_local_deployment(adapter, MANIFEST, source, weights)
    session = runtime.load(deployment)
    try:
        result = runtime.invoke(
            session,
            StableAudio3LocalRequest("tile impact", 5.0, 42).to_invocation("sa3-1"),
        )
    finally:
        runtime.unload(session)
    assert result.artifacts[0].media_type == "audio/wav"
    assert result.artifacts[0].metadata["channels"] == 2


def test_official_runner_fail_closed_without_source(tmp_path: Path):
    adapter = StableAudio3Adapter(OfficialStableAudio3Runner())
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    deployment = stable_audio_3_local_deployment(
        adapter, MANIFEST, tmp_path / "missing-source", tmp_path / "missing-weights"
    )
    try:
        runtime.load(deployment)
    except InvocationRejectedError:
        return
    raise AssertionError("missing source must fail closed")


def test_cli_writes_wav_with_injected_runner(tmp_path: Path):
    source = tmp_path / "source"
    weights = tmp_path / "weights"
    output = tmp_path / "out.wav"
    source.mkdir()
    weights.mkdir()
    with patch(
        "nano_aural_runtime_stable_audio_3.cli.StableAudio3Adapter",
        lambda: StableAudio3Adapter(RunnerDouble()),
    ):
        code = cli_main(
            [
                "stable-audio-3",
                "text-to-sfx",
                "--manifest",
                str(MANIFEST),
                "--source-dir",
                str(source),
                "--weights-dir",
                str(weights),
                "--prompt",
                "metal tile impact",
                "--duration",
                "5",
                "--output",
                str(output),
            ]
        )
    assert code == 0
    assert output.is_file()
    assert output.read_bytes()[:4] == b"RIFF"


def test_dispatcher_exposes_stable_audio_help():
    assert dispatcher_main(["stable-audio-3", "--help"]) == 0
