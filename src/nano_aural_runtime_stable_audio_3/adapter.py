"""Headless Stable Audio 3 Small-SFX adapter. No ComfyUI or durable imports."""

from __future__ import annotations

import hashlib
import io
import wave
from pathlib import Path
from typing import Any, Mapping, Optional, Protocol

from nano_aural_runtime import (
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
    InvocationResult,
    ModelDeployment,
    ModelDescriptor,
    ModelInvocation,
    ModelSession,
    ProducedArtifact,
)

from .baseline import (
    OPERATION,
    RUNTIME_ENVIRONMENT_ID,
    SUPPORTED_CHANNELS,
    SUPPORTED_HF_REPOSITORY,
    SUPPORTED_MODEL_ID,
    SUPPORTED_SAMPLE_RATE,
    SUPPORTED_SOURCE_REVISION,
    StableAudio3DeploymentManifest,
    load_json,
)
from .tasks import StableAudio3LocalRequest

_CONFIGURATION_KEYS = frozenset(
    (
        "manifest_path",
        "deployment_manifest_sha256",
        "source_dir",
        "weights_dir",
        "source_revision",
        "model_id",
        "hf_repository",
        "runtime_environment_id",
    )
)


class StableAudio3Runner(Protocol):
    def validate(self, configuration: Mapping[str, str]) -> Mapping[str, Any]: ...

    def invoke(
        self,
        configuration: Mapping[str, str],
        request: StableAudio3LocalRequest,
        context: ExecutionContext,
    ) -> bytes: ...


class OfficialStableAudio3Runner:
    """Fail-closed default runner; real generation is deferred without operator source."""

    def validate(self, configuration: Mapping[str, str]) -> Mapping[str, Any]:
        source = Path(configuration["source_dir"])
        weights = Path(configuration["weights_dir"])
        if not source.is_dir() or not weights.is_dir():
            raise InvocationRejectedError("Stable Audio 3 source or weights directory is missing")
        return {
            "source_revision": configuration["source_revision"],
            "model_id": configuration["model_id"],
        }

    def invoke(
        self,
        configuration: Mapping[str, str],
        request: StableAudio3LocalRequest,
        context: ExecutionContext,
    ) -> bytes:
        del configuration, request
        context.cancellation_token.raise_if_cancelled()
        raise InvocationRejectedError(
            "official Stable Audio 3 execution is unavailable without operator source"
        )


def _wav_duration_seconds(content: bytes) -> float:
    with wave.open(io.BytesIO(content), "rb") as handle:
        if handle.getnchannels() != SUPPORTED_CHANNELS:
            raise InvocationRejectedError("Stable Audio 3 artifact must be stereo")
        if handle.getframerate() != SUPPORTED_SAMPLE_RATE:
            raise InvocationRejectedError("Stable Audio 3 artifact must be 44100 Hz")
        if handle.getnframes() < 1:
            raise InvocationRejectedError("Stable Audio 3 artifact has no samples")
        return handle.getnframes() / float(handle.getframerate())


class StableAudio3Adapter:
    _descriptor = ModelDescriptor(
        adapter_id="stable-audio-3-small-sfx",
        model_id=SUPPORTED_MODEL_ID,
        version="text-to-sfx-1",
        capabilities={
            "operation": OPERATION,
            "comfyui": False,
            "max_duration_seconds": 120,
            "sample_rate": SUPPORTED_SAMPLE_RATE,
            "channels": SUPPORTED_CHANNELS,
        },
    )

    def __init__(self, runner: Optional[StableAudio3Runner] = None) -> None:
        self._runner = runner or OfficialStableAudio3Runner()
        self._sessions: dict[str, Mapping[str, str]] = {}

    @property
    def descriptor(self) -> ModelDescriptor:
        return self._descriptor

    def load(self, session: ModelSession) -> None:
        if session.deployment.descriptor.adapter_id != self.descriptor.adapter_id:
            raise InvocationRejectedError("deployment selects another adapter")
        configuration = {
            str(key): str(value) for key, value in session.deployment.configuration.items()
        }
        if set(configuration) != _CONFIGURATION_KEYS:
            raise InvocationRejectedError("Stable Audio 3 deployment configuration is not sealed")
        if configuration["source_revision"] != SUPPORTED_SOURCE_REVISION:
            raise InvocationRejectedError("source revision does not match the Phase 8A lock")
        if configuration["model_id"] != SUPPORTED_MODEL_ID:
            raise InvocationRejectedError("V1 supports only small-sfx")
        if configuration["hf_repository"] != SUPPORTED_HF_REPOSITORY:
            raise InvocationRejectedError("Hugging Face repository does not match the lock")
        if configuration["runtime_environment_id"] != RUNTIME_ENVIRONMENT_ID:
            raise InvocationRejectedError("runtime environment does not match the isolated lock")
        if session.deployment.fingerprint != configuration["deployment_manifest_sha256"]:
            raise InvocationRejectedError(
                "deployment fingerprint does not match the sealed manifest"
            )
        self._runner.validate(configuration)
        self._sessions[session.deployment.deployment_id] = configuration

    def invoke(
        self,
        session: ModelSession,
        invocation: ModelInvocation,
        context: ExecutionContext,
    ) -> InvocationResult:
        configuration = self._sessions.get(session.deployment.deployment_id)
        if configuration is None:
            raise InvocationRejectedError("session is not loaded")
        context.cancellation_token.raise_if_cancelled()
        try:
            request = StableAudio3LocalRequest.from_invocation(invocation)
        except (TypeError, ValueError) as error:
            raise InvocationRejectedError("Stable Audio 3 invocation is invalid") from error
        try:
            content = self._runner.invoke(configuration, request, context)
        except InvocationCancelledError:
            raise
        except InvocationRejectedError:
            raise
        except Exception as error:
            raise InvocationRejectedError("Stable Audio 3 runner failed") from error
        context.cancellation_token.raise_if_cancelled()
        duration = _wav_duration_seconds(content)
        if abs(duration - request.duration_seconds) > 0.05:
            raise InvocationRejectedError("artifact duration does not match the request")
        digest = hashlib.sha256(content).hexdigest()
        return InvocationResult(
            invocation.invocation_id,
            artifacts=(
                ProducedArtifact(
                    name="audio",
                    media_type="audio/wav",
                    content=content,
                    metadata={
                        "sample_rate": SUPPORTED_SAMPLE_RATE,
                        "channels": SUPPORTED_CHANNELS,
                        "duration_seconds": request.duration_seconds,
                        "sha256": digest,
                    },
                ),
            ),
            metadata={"operation": OPERATION, "sha256": digest},
        )

    def unload(self, session: ModelSession) -> None:
        self._sessions.pop(session.deployment.deployment_id, None)


def stable_audio_3_local_deployment(
    adapter: StableAudio3Adapter, manifest_path: Path, source_dir: Path, weights_dir: Path
) -> ModelDeployment:
    manifest = StableAudio3DeploymentManifest.from_dict(load_json(manifest_path))
    configuration = {
        "manifest_path": str(manifest_path),
        "deployment_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "source_dir": str(source_dir),
        "weights_dir": str(weights_dir),
        "source_revision": manifest.source_revision,
        "model_id": manifest.model_id,
        "hf_repository": manifest.hf_repository,
        "runtime_environment_id": manifest.runtime_environment_id,
    }
    return ModelDeployment(
        deployment_id=manifest.deployment_id,
        descriptor=adapter.descriptor,
        fingerprint=configuration["deployment_manifest_sha256"],
        configuration=configuration,
    )
