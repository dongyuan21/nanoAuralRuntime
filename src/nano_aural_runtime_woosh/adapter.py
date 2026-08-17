"""Headless Woosh V2A adapter. One operation, two sealed backends, no mux."""

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
    ADAPTER_ID,
    BACKEND_DVFLOW,
    OPERATION,
    RUNTIME_ENVIRONMENT_ID,
    SAMPLE_RATE,
    SUPPORTED_BACKENDS,
    SUPPORTED_SOURCE_REVISION,
    WINDOW_END_SECONDS,
    WooshV2ADeploymentManifest,
    load_json,
)
from .tasks import WooshV2ALocalRequest
from .video import (
    OfficialPyAVVideoProbe,
    VideoDurationProbe,
    VideoWindowError,
    require_explicit_eight_second_window,
    window_contract,
)

_CONFIGURATION_KEYS = frozenset(
    (
        "manifest_path",
        "deployment_manifest_sha256",
        "source_dir",
        "weights_dir",
        "synchformer_path",
        "source_revision",
        "adapter_id",
        "backend_id",
        "runtime_environment_id",
    )
)
_DURATION_TOLERANCE_SECONDS = 0.05
CHANNELS = 1


class WooshV2ARunner(Protocol):
    def validate(self, configuration: Mapping[str, str]) -> Mapping[str, Any]: ...

    def invoke(
        self,
        configuration: Mapping[str, str],
        request: WooshV2ALocalRequest,
        context: ExecutionContext,
    ) -> bytes: ...


class OfficialWooshV2ARunner:
    """Fail-closed default runner; real generation is deferred without operator source."""

    def validate(self, configuration: Mapping[str, str]) -> Mapping[str, Any]:
        source = Path(configuration["source_dir"])
        weights = Path(configuration["weights_dir"])
        synchformer = Path(configuration["synchformer_path"])
        if not source.is_dir() or not weights.is_dir() or not synchformer.is_file():
            raise InvocationRejectedError(
                "Woosh V2A source, weights, or Synchformer path is missing"
            )
        return {
            "source_revision": configuration["source_revision"],
            "backend_id": configuration["backend_id"],
        }

    def invoke(
        self,
        configuration: Mapping[str, str],
        request: WooshV2ALocalRequest,
        context: ExecutionContext,
    ) -> bytes:
        del configuration, request
        context.cancellation_token.raise_if_cancelled()
        raise InvocationRejectedError(
            "official Woosh V2A execution is unavailable without operator source"
        )


def _wav_duration_seconds(content: bytes) -> float:
    with wave.open(io.BytesIO(content), "rb") as handle:
        if handle.getnchannels() != CHANNELS:
            raise InvocationRejectedError("Woosh V2A artifact must be mono")
        if handle.getframerate() != SAMPLE_RATE:
            raise InvocationRejectedError("Woosh V2A artifact must be 48000 Hz")
        if handle.getnframes() < 1:
            raise InvocationRejectedError("Woosh V2A artifact has no samples")
        return handle.getnframes() / float(handle.getframerate())


class WooshV2AAdapter:
    _descriptor = ModelDescriptor(
        adapter_id=ADAPTER_ID,
        model_id=ADAPTER_ID,
        version="video-to-sfx-8s-1",
        capabilities={
            "operation": OPERATION,
            "comfyui": False,
            "default_backend": BACKEND_DVFLOW,
            "backends": list(SUPPORTED_BACKENDS),
            "window_seconds": WINDOW_END_SECONDS,
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
        },
    )

    def __init__(
        self,
        runner: Optional[WooshV2ARunner] = None,
        video_probe: Optional[VideoDurationProbe] = None,
    ) -> None:
        self._runner = runner or OfficialWooshV2ARunner()
        self._video_probe = video_probe or OfficialPyAVVideoProbe()
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
            raise InvocationRejectedError("Woosh V2A deployment configuration is not sealed")
        if configuration["source_revision"] != SUPPORTED_SOURCE_REVISION:
            raise InvocationRejectedError("source revision does not match the Phase 9A lock")
        if configuration["adapter_id"] != ADAPTER_ID:
            raise InvocationRejectedError("adapter_id must be woosh-v2a")
        if configuration["backend_id"] not in SUPPORTED_BACKENDS:
            raise InvocationRejectedError("V1 supports only dvflow-8s and vflow-8s")
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
            request = WooshV2ALocalRequest.from_invocation(invocation)
            duration = self._video_probe.duration_seconds(request.video_path)
            require_explicit_eight_second_window(duration)
        except (TypeError, ValueError, VideoWindowError) as error:
            raise InvocationRejectedError("Woosh V2A invocation is invalid") from error
        try:
            content = self._runner.invoke(configuration, request, context)
        except InvocationCancelledError:
            raise
        except InvocationRejectedError:
            raise
        except Exception as error:
            raise InvocationRejectedError("Woosh V2A runner failed") from error
        context.cancellation_token.raise_if_cancelled()
        artifact_duration = _wav_duration_seconds(content)
        if abs(artifact_duration - WINDOW_END_SECONDS) > _DURATION_TOLERANCE_SECONDS:
            raise InvocationRejectedError("artifact duration must be 8 seconds")
        digest = hashlib.sha256(content).hexdigest()
        metadata = {
            "sample_rate": SAMPLE_RATE,
            "channels": CHANNELS,
            "duration_seconds": WINDOW_END_SECONDS,
            "backend_id": configuration["backend_id"],
            "sha256": digest,
        }
        metadata.update(window_contract())
        return InvocationResult(
            invocation.invocation_id,
            artifacts=(
                ProducedArtifact(
                    name="audio",
                    media_type="audio/wav",
                    content=content,
                    metadata=metadata,
                ),
            ),
            metadata={"operation": OPERATION, "sha256": digest},
        )

    def unload(self, session: ModelSession) -> None:
        self._sessions.pop(session.deployment.deployment_id, None)


def woosh_v2a_local_deployment(
    adapter: WooshV2AAdapter,
    manifest_path: Path,
    source_dir: Path,
    weights_dir: Path,
    synchformer_path: Path,
) -> ModelDeployment:
    manifest = WooshV2ADeploymentManifest.from_dict(load_json(manifest_path))
    configuration = {
        "manifest_path": str(manifest_path),
        "deployment_manifest_sha256": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        "source_dir": str(source_dir),
        "weights_dir": str(weights_dir),
        "synchformer_path": str(synchformer_path),
        "source_revision": manifest.source_revision,
        "adapter_id": manifest.adapter_id,
        "backend_id": manifest.backend_id,
        "runtime_environment_id": manifest.runtime_environment_id,
    }
    return ModelDeployment(
        deployment_id=manifest.deployment_id,
        descriptor=adapter.descriptor,
        fingerprint=configuration["deployment_manifest_sha256"],
        configuration=configuration,
    )
