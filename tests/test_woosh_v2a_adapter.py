# pyright: reportMissingImports=false
from __future__ import annotations

import io
import wave
from pathlib import Path
from unittest.mock import patch

import pytest

from nano_aural_runtime import AdapterRegistry, InvocationRejectedError, Runtime
from nano_aural_runtime_cli.main import main as dispatcher_main
from nano_aural_runtime_woosh.adapter import (
    OfficialWooshV2ARunner,
    WooshV2AAdapter,
    woosh_v2a_local_deployment,
)
from nano_aural_runtime_woosh.baseline import SAMPLE_RATE, WINDOW_END_SECONDS
from nano_aural_runtime_woosh.cli import main as cli_main
from nano_aural_runtime_woosh.tasks import WooshV2ALocalRequest
from nano_aural_runtime_woosh.video import require_explicit_eight_second_window

ROOT = Path(__file__).resolve().parents[1]
DVFLOW = ROOT / "benchmarks" / "fixtures" / "woosh-v2a" / "dvflow-8s.lock.json"
VFLOW = ROOT / "benchmarks" / "fixtures" / "woosh-v2a" / "vflow-8s.lock.json"


def _silent_wav(duration_seconds: float = WINDOW_END_SECONDS) -> bytes:
    frames = int(duration_seconds * SAMPLE_RATE)
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00\x00" * frames)
    return buffer.getvalue()


class ProbeDouble:
    def __init__(self, duration: float = 8.0) -> None:
        self.duration = duration

    def duration_seconds(self, path: Path) -> float:
        assert path.is_file()
        return self.duration


class RunnerDouble:
    def validate(self, configuration):
        return {"backend_id": configuration["backend_id"]}

    def invoke(self, configuration, request, context):
        context.cancellation_token.raise_if_cancelled()
        return _silent_wav()


def _operator_dirs(tmp_path: Path):
    source = tmp_path / "source"
    weights = tmp_path / "weights"
    synchformer = tmp_path / "synchformer.pth"
    video = tmp_path / "clip.mp4"
    source.mkdir()
    weights.mkdir()
    synchformer.write_bytes(b"synch")
    video.write_bytes(b"fake-video")
    return source, weights, synchformer, video


def test_request_schema_requires_existing_video_and_rejects_empty_prompt(tmp_path: Path):
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    WooshV2ALocalRequest(video, 1)
    WooshV2ALocalRequest(video, 1, "metal blocks colliding")
    try:
        WooshV2ALocalRequest(tmp_path / "missing.mp4", 1)
    except ValueError:
        pass
    else:
        raise AssertionError("missing video must fail")
    try:
        WooshV2ALocalRequest(video, 1, "  ")
    except ValueError:
        return
    raise AssertionError("empty prompt must fail")


def test_invocation_rejects_duration_and_solver_overrides(tmp_path: Path):
    from nano_aural_runtime import ModelInvocation

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake")
    illegal = ModelInvocation(
        "w1",
        "audio.video_to_sfx",
        {"video_path": str(video), "seed": 42, "duration_seconds": 8.0},
    )
    try:
        WooshV2ALocalRequest.from_invocation(illegal)
    except ValueError:
        return
    raise AssertionError("duration override must fail")


def test_window_policy_rejects_short_video_and_allows_longer():
    require_explicit_eight_second_window(8.0)
    require_explicit_eight_second_window(12.0)
    try:
        require_explicit_eight_second_window(7.9)
    except ValueError:
        return
    raise AssertionError("short video must fail closed")


def test_adapter_load_invoke_unload_with_injected_runner(tmp_path: Path):
    source, weights, synchformer, video = _operator_dirs(tmp_path)
    adapter = WooshV2AAdapter(RunnerDouble(), ProbeDouble())
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    deployment = woosh_v2a_local_deployment(adapter, DVFLOW, source, weights, synchformer)
    assert deployment.configuration["backend_id"] == "dvflow-8s"
    session = runtime.load(deployment)
    try:
        result = runtime.invoke(
            session,
            WooshV2ALocalRequest(video, 42, "metal blocks").to_invocation("woosh-1"),
        )
    finally:
        runtime.unload(session)
    assert result.artifacts[0].media_type == "audio/wav"
    assert result.artifacts[0].metadata["channels"] == 1
    assert result.artifacts[0].metadata["sample_rate"] == SAMPLE_RATE
    assert result.artifacts[0].metadata["backend_id"] == "dvflow-8s"


def test_adapter_rejects_short_video(tmp_path: Path):
    source, weights, synchformer, video = _operator_dirs(tmp_path)
    adapter = WooshV2AAdapter(RunnerDouble(), ProbeDouble(3.0))
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    deployment = woosh_v2a_local_deployment(adapter, DVFLOW, source, weights, synchformer)
    session = runtime.load(deployment)
    try:
        runtime.invoke(session, WooshV2ALocalRequest(video, 1).to_invocation("short"))
    except InvocationRejectedError:
        return
    finally:
        runtime.unload(session)
    raise AssertionError("short video must fail closed")


def test_vflow_manifest_is_selectable(tmp_path: Path):
    source, weights, synchformer, video = _operator_dirs(tmp_path)
    adapter = WooshV2AAdapter(RunnerDouble(), ProbeDouble())
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    deployment = woosh_v2a_local_deployment(adapter, VFLOW, source, weights, synchformer)
    assert deployment.configuration["backend_id"] == "vflow-8s"
    session = runtime.load(deployment)
    try:
        result = runtime.invoke(session, WooshV2ALocalRequest(video, 7).to_invocation("vflow"))
    finally:
        runtime.unload(session)
    assert result.artifacts[0].metadata["backend_id"] == "vflow-8s"


def test_official_runner_fail_closed_without_source(tmp_path: Path):
    adapter = WooshV2AAdapter(OfficialWooshV2ARunner(), ProbeDouble())
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    deployment = woosh_v2a_local_deployment(
        adapter,
        DVFLOW,
        tmp_path / "missing-source",
        tmp_path / "missing-weights",
        tmp_path / "missing.pth",
    )
    try:
        runtime.load(deployment)
    except InvocationRejectedError:
        return
    raise AssertionError("missing source must fail closed")


def test_cli_writes_wav_with_injected_runner(tmp_path: Path):
    source, weights, synchformer, video = _operator_dirs(tmp_path)
    output = tmp_path / "out.wav"
    with patch(
        "nano_aural_runtime_woosh.cli.WooshV2AAdapter",
        lambda: WooshV2AAdapter(RunnerDouble(), ProbeDouble()),
    ):
        code = cli_main(
            [
                "woosh",
                "video-to-sfx",
                "--deployment",
                "dvflow-8s",
                "--manifest",
                str(DVFLOW),
                "--source-dir",
                str(source),
                "--weights-dir",
                str(weights),
                "--synchformer-path",
                str(synchformer),
                "--video",
                str(video),
                "--prompt",
                "metal blocks colliding and shattering",
                "--output",
                str(output),
            ]
        )
    assert code == 0
    assert output.is_file()
    assert output.read_bytes()[:4] == b"RIFF"


def test_dispatcher_exposes_woosh_help_and_rejects_text_to_sfx():
    assert dispatcher_main(["woosh", "video-to-sfx", "--help"]) == 0
    assert dispatcher_main(["woosh", "text-to-sfx"]) == 2


@pytest.mark.gpu
def test_woosh_adapter_gpu_parity_remains_deferred():
    pytest.skip("WOOSH GPU parity is deferred until RTX 4090 evidence is recorded")
