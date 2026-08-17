"""Experimental, adapter-owned ControlFoley staged execution contracts.

This module contains no upstream implementation and imports neither torch nor
ControlFoley.  A staged backend must be supplied explicitly by an operator.
The deterministic backend is a CPU test double, not a parity implementation.
"""

from __future__ import annotations

import hashlib
import io
import math
import re
import wave
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import Enum
from types import MappingProxyType
from typing import (
    Any,
    Callable,
    ContextManager,
    Dict,
    Mapping,
    Optional,
    Protocol,
    Tuple,
    runtime_checkable,
)

from nano_aural_runtime import (
    AdapterExecutionError,
    ExecutionContext,
    InvocationCancelledError,
    InvocationRejectedError,
)

from .baseline import (
    ControlFoleyDeploymentManifest,
    fingerprint_file,
    fingerprint_text,
    manifest_sha256,
)
from .safe_tensor import build_u8_safe_tensor_bundle, validate_safe_tensor_bundle
from .tasks import (
    EXPERIMENTAL_STAGED_OPERATION,
    UPSTREAM_PARITY_OPERATION,
    ControlFoleyLocalRequest,
)


class ControlFoleyStage(str, Enum):
    MEDIA_RESOLVE_PREPROCESS = "media_resolve_preprocess"
    CONDITION_ENCODE_PROJECTION = "condition_encode_projection"
    INTEGRATE = "integrate"
    DECODE_VOCODER_POSTPROCESS = "decode_vocoder_postprocess"


CONTROLFOLEY_STAGE_ORDER: Tuple[ControlFoleyStage, ...] = tuple(ControlFoleyStage)
_BACKEND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}", re.ASCII)


def controlfoley_condition_feature_roles(
    request: ControlFoleyLocalRequest,
) -> Tuple[str, ...]:
    """Return the exact adapter-owned encoder roles for a task."""

    if not isinstance(request, ControlFoleyLocalRequest):
        raise TypeError("request must be ControlFoleyLocalRequest")
    return {
        "V2A": ("video",),
        "TV2A": ("video", "text"),
        "TC-V2A": ("video", "text"),
        "AC-V2A": ("video", "reference_audio"),
        "T2A": ("text",),
    }[request.task.value]


def validate_staged_backend_id(value: object) -> str:
    """Return a short path-free operator identity or reject it."""

    if not isinstance(value, str) or _BACKEND_ID.fullmatch(value) is None:
        raise ValueError(
            "staged backend_id must be a 1-64 character ASCII identifier using "
            "letters, digits, dot, underscore, or hyphen"
        )
    return value


class ControlFoleyStageExecutionError(AdapterExecutionError):
    """A named experimental stage failed and the session is not reusable."""

    def __init__(self, stage: ControlFoleyStage, detail: str) -> None:
        self.stage = stage
        super().__init__("ControlFoley stage {0} failed: {1}".format(stage.value, detail))


class ControlFoleyStageContractError(AdapterExecutionError):
    """An operator backend violated the staged protocol."""


@dataclass(frozen=True)
class ControlFoleyStagedBackendValidation:
    backend_id: str
    deployment_manifest_sha256: str
    source_revision: str
    variant: str
    precision: str
    checkpoint_sha256: str
    implementation: str

    def __post_init__(self) -> None:
        validate_staged_backend_id(self.backend_id)
        _sha256(self.deployment_manifest_sha256, "deployment_manifest_sha256")
        _git_revision(self.source_revision, "source_revision")
        _text(self.variant, "variant")
        _text(self.precision, "precision")
        _sha256(self.checkpoint_sha256, "checkpoint_sha256")
        _text(self.implementation, "implementation")

    def to_dict(self) -> Mapping[str, str]:
        return MappingProxyType(
            {
                "backend_id": self.backend_id,
                "deployment_manifest_sha256": self.deployment_manifest_sha256,
                "source_revision": self.source_revision,
                "variant": self.variant,
                "precision": self.precision,
                "checkpoint_sha256": self.checkpoint_sha256,
                "implementation": self.implementation,
            }
        )


@dataclass(frozen=True)
class ControlFoleyStagedBackendSession:
    backend_id: str
    deployment_fingerprint: str
    payload: Any = None

    def __post_init__(self) -> None:
        validate_staged_backend_id(self.backend_id)
        _sha256(self.deployment_fingerprint, "deployment_fingerprint")


@dataclass(frozen=True)
class ControlFoleyOracleDeploymentBinding:
    """The initially captured P2A oracle material for one comparison run."""

    deployment_manifest_sha256: str
    source_revision: str
    checkpoint_sha256: str

    def __post_init__(self) -> None:
        _sha256(self.deployment_manifest_sha256, "deployment_manifest_sha256")
        _git_revision(self.source_revision, "source_revision")
        _sha256(self.checkpoint_sha256, "checkpoint_sha256")

    @classmethod
    def from_manifest(
        cls, manifest: ControlFoleyDeploymentManifest
    ) -> "ControlFoleyOracleDeploymentBinding":
        if not isinstance(manifest, ControlFoleyDeploymentManifest):
            raise TypeError("manifest must be ControlFoleyDeploymentManifest")
        if manifest.checkpoint.status != "verified" or manifest.checkpoint.sha256 is None:
            raise InvocationRejectedError(
                "comparison oracle checkpoint fingerprint must be verified"
            )
        return cls(
            deployment_manifest_sha256=manifest_sha256(manifest.to_dict()),
            source_revision=manifest.source_revision,
            checkpoint_sha256=manifest.checkpoint.sha256,
        )

    def assert_manifest(self, manifest: ControlFoleyDeploymentManifest) -> None:
        if ControlFoleyOracleDeploymentBinding.from_manifest(manifest) != self:
            raise InvocationRejectedError(
                "comparison oracle deployment manifest changed during the run"
            )

    def assert_candidate_configuration(self, configuration: Mapping[str, Any]) -> None:
        expected = {
            "deployment_manifest_sha256": self.deployment_manifest_sha256,
            "source_revision": self.source_revision,
            "checkpoint_sha256": self.checkpoint_sha256,
        }
        if any(configuration.get(name) != value for name, value in expected.items()):
            raise InvocationRejectedError(
                "staged candidate is not bound to the captured oracle deployment"
            )


@dataclass(frozen=True)
class ControlFoleyStageValue:
    stage: ControlFoleyStage
    payload: Any
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.stage, ControlFoleyStage):
            raise TypeError("stage must be ControlFoleyStage")
        object.__setattr__(self, "evidence", _freeze_evidence(self.evidence))


@dataclass(frozen=True)
class ControlFoleyConditionFeature:
    """Invocation-local encoded feature; only its safe codec bytes may persist."""

    role: str
    payload: Any
    evidence: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.role not in ("video", "reference_audio", "text"):
            raise ValueError("condition feature role is unsupported")
        object.__setattr__(self, "evidence", _freeze_evidence(self.evidence))


@dataclass(frozen=True)
class ControlFoleyStagedArtifact:
    content: bytes
    name: str = "controlfoley-staged.flac"
    media_type: str = "audio/flac"
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not isinstance(self.content, bytes) or not self.content:
            raise ValueError("staged artifact content must be non-empty bytes")
        declared_media = {
            "controlfoley-staged.flac": "audio/flac",
            "controlfoley-staged.wav": "audio/wav",
        }
        if declared_media.get(self.name) != self.media_type:
            raise ValueError(
                "staged artifact name and media type must use a declared audio contract"
            )
        object.__setattr__(self, "metadata", _freeze_evidence(self.metadata))


_METRIC_NAMES = (
    "peak",
    "rms",
    "mae",
    "max_absolute_error",
    "waveform_cosine_similarity",
    "mel_spectrogram_distance",
)
_WAVEFORM_NAMES = ("channels", "samples", "sample_rate")


@dataclass(frozen=True)
class ControlFoleyComparisonEvidence:
    """Raw oracle/candidate observations with no threshold or parity claim."""

    state: str
    deployment_manifest_sha256: str
    candidate_deployment_fingerprint: str
    candidate_backend_id: str
    source_revision: str
    checkpoint_sha256: str
    canonical_invocation_sha256: str
    candidate_sha256: str
    oracle_sha256: Optional[str]
    raw_metrics: Mapping[str, Optional[float]]
    waveform: Mapping[str, Optional[int]]
    schema_version: int = 1
    oracle_operation: str = UPSTREAM_PARITY_OPERATION
    candidate_operation: str = EXPERIMENTAL_STAGED_OPERATION

    def __post_init__(self) -> None:
        if self.schema_version != 1:
            raise ValueError("unsupported comparison schema_version")
        if self.state not in ("unmeasured", "measured"):
            raise ValueError("comparison state must be unmeasured or measured")
        if self.oracle_operation != UPSTREAM_PARITY_OPERATION:
            raise ValueError("upstream_parity must remain the comparison oracle")
        if self.candidate_operation != EXPERIMENTAL_STAGED_OPERATION:
            raise ValueError("comparison candidate must be the experimental staged path")
        _sha256(self.deployment_manifest_sha256, "deployment_manifest_sha256")
        _sha256(self.candidate_deployment_fingerprint, "candidate_deployment_fingerprint")
        validate_staged_backend_id(self.candidate_backend_id)
        _git_revision(self.source_revision, "source_revision")
        _sha256(self.checkpoint_sha256, "checkpoint_sha256")
        _sha256(self.canonical_invocation_sha256, "canonical_invocation_sha256")
        _sha256(self.candidate_sha256, "candidate_sha256")
        raw_metrics = dict(self.raw_metrics)
        waveform = dict(self.waveform)
        if set(raw_metrics) != set(_METRIC_NAMES):
            raise ValueError("comparison metrics have missing or unexpected fields")
        if set(waveform) != set(_WAVEFORM_NAMES):
            raise ValueError("comparison waveform has missing or unexpected fields")
        if self.state == "unmeasured":
            if self.oracle_sha256 is not None or any(
                value is not None for value in (*raw_metrics.values(), *waveform.values())
            ):
                raise ValueError("unmeasured comparison cannot contain invented evidence")
        else:
            if self.oracle_sha256 is None:
                raise ValueError("measured comparison requires oracle SHA-256")
            _sha256(self.oracle_sha256, "oracle_sha256")
            for value in raw_metrics.values():
                if (
                    isinstance(value, bool)
                    or not isinstance(value, (int, float))
                    or not math.isfinite(value)
                ):
                    raise ValueError("measured comparison metrics must be finite")
            for name in ("peak", "rms", "mae", "max_absolute_error", "mel_spectrogram_distance"):
                metric = raw_metrics[name]
                if not isinstance(metric, (int, float)) or isinstance(metric, bool) or metric < 0:
                    raise ValueError(
                        "measured comparison distance and magnitude metrics must be nonnegative"
                    )
            cosine = raw_metrics["waveform_cosine_similarity"]
            if (
                not isinstance(cosine, (int, float))
                or isinstance(cosine, bool)
                or not -1.0 <= cosine <= 1.0
            ):
                raise ValueError("measured waveform cosine similarity must be between -1 and 1")
            for value in waveform.values():
                if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                    raise ValueError("measured waveform values must be positive integers")
        object.__setattr__(self, "raw_metrics", MappingProxyType(raw_metrics))
        object.__setattr__(self, "waveform", MappingProxyType(waveform))

    @classmethod
    def unmeasured(
        cls,
        *,
        deployment_manifest_sha256: str,
        candidate_deployment_fingerprint: str,
        candidate_backend_id: str,
        source_revision: str,
        checkpoint_sha256: str,
        canonical_invocation_sha256: str,
        candidate_sha256: str,
    ) -> "ControlFoleyComparisonEvidence":
        return cls(
            state="unmeasured",
            deployment_manifest_sha256=deployment_manifest_sha256,
            candidate_deployment_fingerprint=candidate_deployment_fingerprint,
            candidate_backend_id=candidate_backend_id,
            source_revision=source_revision,
            checkpoint_sha256=checkpoint_sha256,
            canonical_invocation_sha256=canonical_invocation_sha256,
            candidate_sha256=candidate_sha256,
            oracle_sha256=None,
            raw_metrics={name: None for name in _METRIC_NAMES},
            waveform={name: None for name in _WAVEFORM_NAMES},
        )

    @classmethod
    def measured(
        cls,
        *,
        deployment_manifest_sha256: str,
        candidate_deployment_fingerprint: str,
        candidate_backend_id: str,
        source_revision: str,
        checkpoint_sha256: str,
        canonical_invocation_sha256: str,
        candidate_sha256: str,
        oracle_sha256: str,
        raw_metrics: Mapping[str, float],
        waveform: Mapping[str, int],
    ) -> "ControlFoleyComparisonEvidence":
        return cls(
            state="measured",
            deployment_manifest_sha256=deployment_manifest_sha256,
            candidate_deployment_fingerprint=candidate_deployment_fingerprint,
            candidate_backend_id=candidate_backend_id,
            source_revision=source_revision,
            checkpoint_sha256=checkpoint_sha256,
            canonical_invocation_sha256=canonical_invocation_sha256,
            candidate_sha256=candidate_sha256,
            oracle_sha256=oracle_sha256,
            raw_metrics=raw_metrics,
            waveform=waveform,
        )

    def to_dict(self) -> Mapping[str, object]:
        # Threshold and claim keys are intentionally absent.
        return MappingProxyType(
            {
                "schema_version": self.schema_version,
                "state": self.state,
                "oracle_operation": self.oracle_operation,
                "candidate_operation": self.candidate_operation,
                "deployment_manifest_sha256": self.deployment_manifest_sha256,
                "candidate_deployment_fingerprint": self.candidate_deployment_fingerprint,
                "candidate_backend_id": self.candidate_backend_id,
                "source_revision": self.source_revision,
                "checkpoint_sha256": self.checkpoint_sha256,
                "canonical_invocation_sha256": self.canonical_invocation_sha256,
                "oracle_sha256": self.oracle_sha256,
                "candidate_sha256": self.candidate_sha256,
                "raw_metrics": dict(self.raw_metrics),
                "waveform": dict(self.waveform),
            }
        )


def controlfoley_invocation_fingerprint(request: ControlFoleyLocalRequest) -> str:
    """Bind comparison evidence to exact path-free request content and parameters."""

    if not isinstance(request, ControlFoleyLocalRequest):
        raise TypeError("request must be ControlFoleyLocalRequest")
    return manifest_sha256(
        {
            "schema_version": 1,
            "task": request.task.value,
            "inputs": {
                "video": (
                    None
                    if request.video_path is None
                    else fingerprint_file(request.video_path).to_dict()
                ),
                "reference_audio": (
                    None
                    if request.reference_audio_path is None
                    else fingerprint_file(request.reference_audio_path).to_dict()
                ),
                "prompt": (
                    None if request.prompt is None else fingerprint_text(request.prompt).to_dict()
                ),
            },
            "parameters": {
                "duration_seconds": float(request.duration_seconds),
                "num_steps": request.num_steps,
                "guidance_scale": float(request.guidance_scale),
                "seed": request.seed,
            },
        }
    )


class ControlFoleyStagedBackend(Protocol):
    @property
    def backend_id(self) -> str: ...

    def load(
        self, configuration: Mapping[str, Any]
    ) -> Tuple[ControlFoleyStagedBackendSession, ControlFoleyStagedBackendValidation]: ...

    def run_stage(
        self,
        session: ControlFoleyStagedBackendSession,
        stage: ControlFoleyStage,
        previous: Optional[ControlFoleyStageValue],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue: ...

    def unload(self, session: ControlFoleyStagedBackendSession) -> None: ...


@runtime_checkable
class ControlFoleyPreprocessCacheCodec(Protocol):
    """Explicit safe-bytes codec for only the deterministic L1 stage value."""

    def export_preprocess_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        value: ControlFoleyStageValue,
        request: ControlFoleyLocalRequest,
    ) -> bytes: ...

    def import_preprocess_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        payload: bytes,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue: ...


@runtime_checkable
class ControlFoleyConditionFeatureCacheBackend(Protocol):
    """Optional L2 split: encoders may be cached, projection always executes."""

    @property
    def condition_cache_codec_version(self) -> str: ...

    @property
    def condition_cache_schema_version(self) -> str: ...

    def encode_condition_feature(
        self,
        session: ControlFoleyStagedBackendSession,
        role: str,
        preprocess: ControlFoleyStageValue,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyConditionFeature: ...

    def project_condition_features(
        self,
        session: ControlFoleyStagedBackendSession,
        preprocess: ControlFoleyStageValue,
        features: Tuple[ControlFoleyConditionFeature, ...],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue: ...

    def export_condition_feature_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        feature: ControlFoleyConditionFeature,
        request: ControlFoleyLocalRequest,
    ) -> bytes: ...

    def import_condition_feature_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        role: str,
        payload: bytes,
        preprocess: ControlFoleyStageValue,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyConditionFeature: ...


class ControlFoleyStageObserver(Protocol):
    def observe(self, stage: ControlFoleyStage) -> ContextManager[None]: ...


class ControlFoleyStagedPath:
    """Strict stage-order orchestrator for an explicitly supplied backend."""

    def __init__(self, backend: ControlFoleyStagedBackend) -> None:
        try:
            validate_staged_backend_id(getattr(backend, "backend_id", None))
        except ValueError as error:
            raise TypeError("staged backend must expose a safe backend_id") from error
        self._backend = backend
        self._sessions: Dict[str, ControlFoleyStagedBackendSession] = {}
        self._validation: Dict[str, ControlFoleyStagedBackendValidation] = {}

    @property
    def backend_id(self) -> str:
        return self._backend.backend_id

    @property
    def supports_preprocess_cache(self) -> bool:
        return isinstance(self._backend, ControlFoleyPreprocessCacheCodec)

    @property
    def supports_condition_feature_cache(self) -> bool:
        return isinstance(self._backend, ControlFoleyConditionFeatureCacheBackend)

    @property
    def condition_cache_identity(self) -> Optional[Tuple[str, str]]:
        if not isinstance(self._backend, ControlFoleyConditionFeatureCacheBackend):
            return None
        return (
            self._backend.condition_cache_codec_version,
            self._backend.condition_cache_schema_version,
        )

    def load(
        self, session_id: str, configuration: Mapping[str, Any], deployment_fingerprint: str
    ) -> Mapping[str, str]:
        if session_id in self._sessions:
            raise InvocationRejectedError("staged session is already loaded")
        if configuration.get("staged_backend_id") != self.backend_id:
            raise InvocationRejectedError("sealed staged backend id does not match implementation")
        try:
            backend_session, validation = self._backend.load(configuration)
        except InvocationRejectedError:
            raise
        except Exception as error:
            raise InvocationRejectedError("operator staged backend failed to load") from error
        if not isinstance(backend_session, ControlFoleyStagedBackendSession) or not isinstance(
            validation, ControlFoleyStagedBackendValidation
        ):
            self._reject_loaded(
                backend_session,
                "operator staged backend returned an invalid load contract",
            )
        if backend_session.backend_id != self.backend_id:
            self._reject_loaded(backend_session, "staged backend session identity mismatch")
        if backend_session.deployment_fingerprint != deployment_fingerprint:
            self._reject_loaded(backend_session, "staged backend session fingerprint mismatch")
        expected = {
            "backend_id": self.backend_id,
            "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
            "source_revision": configuration["source_revision"],
            "variant": configuration["variant"],
            "precision": configuration["precision"],
            "checkpoint_sha256": configuration["checkpoint_sha256"],
        }
        report = validation.to_dict()
        if any(report[name] != value for name, value in expected.items()):
            self._reject_loaded(
                backend_session,
                "operator staged backend validation does not bind the sealed deployment",
            )
        self._sessions[session_id] = backend_session
        self._validation[session_id] = validation
        # Operator implementation strings are diagnostic-only and may contain
        # paths or secrets.  Only the fixed, sealed deployment binding escapes.
        return MappingProxyType(dict(expected))

    def _reject_loaded(self, session: object, message: str) -> None:
        try:
            self._backend.unload(session)  # type: ignore[arg-type]
        except Exception:
            pass
        raise InvocationRejectedError(message)

    def invoke(
        self,
        session_id: str,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
        observer: Optional[ControlFoleyStageObserver] = None,
        cached_preprocess_payload: Optional[bytes] = None,
        decode_cached_preprocess: Optional[Callable[[bytes], ControlFoleyStageValue]] = None,
        reject_cached_preprocess: Optional[Callable[[], None]] = None,
        capture_preprocess: Optional[Callable[[ControlFoleyStageValue], None]] = None,
        condition_cache_payloads: Optional[Mapping[str, bytes]] = None,
        reject_condition_feature: Optional[Callable[[str], None]] = None,
        capture_condition_feature: Optional[Callable[[ControlFoleyConditionFeature], None]] = None,
        on_condition_encoder: Optional[Callable[[str], None]] = None,
        on_condition_projection: Optional[Callable[[], None]] = None,
    ) -> Tuple[ControlFoleyStagedArtifact, Tuple[ControlFoleyStageValue, ...]]:
        try:
            backend_session = self._sessions[session_id]
        except KeyError as error:
            raise InvocationRejectedError("staged session is not loaded") from error
        values = []
        previous: Optional[ControlFoleyStageValue] = None
        used_cached_preprocess = False
        for stage in CONTROLFOLEY_STAGE_ORDER:
            context.cancellation_token.raise_if_cancelled()
            try:
                observation = nullcontext() if observer is None else observer.observe(stage)
                with observation:
                    if stage is ControlFoleyStage.CONDITION_ENCODE_PROJECTION and (
                        condition_cache_payloads is not None
                        or isinstance(self._backend, ControlFoleyConditionFeatureCacheBackend)
                    ):
                        value = self._run_condition_feature_stage(
                            backend_session,
                            previous,
                            request,
                            context,
                            condition_cache_payloads or {},
                            reject_condition_feature,
                            capture_condition_feature,
                            on_condition_encoder,
                            on_condition_projection,
                        )
                    elif (
                        stage is ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS
                        and cached_preprocess_payload is not None
                        and decode_cached_preprocess is not None
                    ):
                        try:
                            value = decode_cached_preprocess(cached_preprocess_payload)
                            used_cached_preprocess = True
                        except InvocationCancelledError:
                            raise
                        except Exception:
                            if reject_cached_preprocess is not None:
                                try:
                                    reject_cached_preprocess()
                                except Exception:
                                    pass
                            value = self._backend.run_stage(
                                backend_session, stage, previous, request, context
                            )
                    else:
                        value = self._backend.run_stage(
                            backend_session, stage, previous, request, context
                        )
            except (InvocationCancelledError, InvocationRejectedError, AdapterExecutionError):
                raise
            except Exception as error:
                raise ControlFoleyStageExecutionError(stage, "operator backend error") from error
            if not isinstance(value, ControlFoleyStageValue) or value.stage is not stage:
                raise ControlFoleyStageContractError(
                    "operator backend returned the wrong value for stage {0}".format(stage.value)
                )
            context.cancellation_token.raise_if_cancelled()
            values.append(value)
            previous = value
            if (
                stage is ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS
                and not used_cached_preprocess
                and capture_preprocess is not None
            ):
                # Cache observation must never change staged execution.
                try:
                    capture_preprocess(value)
                except Exception:
                    pass
        if previous is None or not isinstance(previous.payload, ControlFoleyStagedArtifact):
            raise ControlFoleyStageContractError(
                "decode/vocoder/postprocess must return ControlFoleyStagedArtifact"
            )
        return previous.payload, tuple(values)

    def _run_condition_feature_stage(
        self,
        session: ControlFoleyStagedBackendSession,
        preprocess: Optional[ControlFoleyStageValue],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
        cached_payloads: Mapping[str, bytes],
        reject_cached: Optional[Callable[[str], None]],
        capture: Optional[Callable[[ControlFoleyConditionFeature], None]],
        on_encoder: Optional[Callable[[str], None]],
        on_projection: Optional[Callable[[], None]],
    ) -> ControlFoleyStageValue:
        if not isinstance(self._backend, ControlFoleyConditionFeatureCacheBackend):
            raise ControlFoleyStageContractError(
                "staged backend does not implement the L2 condition feature split"
            )
        if preprocess is None or preprocess.stage is not ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS:
            raise ControlFoleyStageContractError("L2 condition features require preprocess output")
        roles = controlfoley_condition_feature_roles(request)
        if set(cached_payloads) - set(roles):
            raise ControlFoleyStageContractError("L2 cache supplied an unexpected feature role")
        features = []
        for role in roles:
            context.cancellation_token.raise_if_cancelled()
            feature: Optional[ControlFoleyConditionFeature] = None
            payload = cached_payloads.get(role)
            if payload is not None:
                try:
                    feature = self._backend.import_condition_feature_cache(
                        session, role, payload, preprocess, request, context
                    )
                    self._validate_condition_feature(feature, role)
                except InvocationCancelledError:
                    raise
                except Exception:
                    if reject_cached is not None:
                        try:
                            reject_cached(role)
                        except Exception:
                            pass
                    feature = None
            if feature is None:
                if on_encoder is not None:
                    try:
                        on_encoder(role)
                    except Exception:
                        pass
                feature = self._backend.encode_condition_feature(
                    session, role, preprocess, request, context
                )
                self._validate_condition_feature(feature, role)
                if capture is not None:
                    try:
                        capture(feature)
                    except Exception:
                        pass
            features.append(feature)
        context.cancellation_token.raise_if_cancelled()
        if on_projection is not None:
            try:
                on_projection()
            except Exception:
                pass
        result = self._backend.project_condition_features(
            session, preprocess, tuple(features), request, context
        )
        if (
            not isinstance(result, ControlFoleyStageValue)
            or result.stage is not ControlFoleyStage.CONDITION_ENCODE_PROJECTION
        ):
            raise ControlFoleyStageContractError("L2 projection returned an invalid stage value")
        return result

    @staticmethod
    def _validate_condition_feature(value: object, role: str) -> None:
        if not isinstance(value, ControlFoleyConditionFeature) or value.role != role:
            raise ControlFoleyStageContractError(
                "condition encoder returned an invalid role-bound feature"
            )

    def decode_cached_preprocess(
        self,
        session_id: str,
        payload: bytes,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue:
        backend_session = self._sessions.get(session_id)
        if backend_session is None:
            raise InvocationRejectedError("staged session is not loaded")
        if not isinstance(self._backend, ControlFoleyPreprocessCacheCodec):
            raise ControlFoleyStageContractError(
                "staged backend does not implement the L1 safe-bytes codec"
            )
        context.cancellation_token.raise_if_cancelled()
        value = self._backend.import_preprocess_cache(backend_session, payload, request, context)
        if (
            not isinstance(value, ControlFoleyStageValue)
            or value.stage is not ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS
        ):
            raise ControlFoleyStageContractError(
                "L1 codec returned an invalid preprocess stage value"
            )
        return value

    def encode_preprocess(
        self,
        session_id: str,
        value: ControlFoleyStageValue,
        request: ControlFoleyLocalRequest,
    ) -> bytes:
        backend_session = self._sessions.get(session_id)
        if backend_session is None:
            raise InvocationRejectedError("staged session is not loaded")
        if not isinstance(self._backend, ControlFoleyPreprocessCacheCodec):
            raise ControlFoleyStageContractError(
                "staged backend does not implement the L1 safe-bytes codec"
            )
        if value.stage is not ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS:
            raise ControlFoleyStageContractError("only preprocess values may enter L1")
        payload = self._backend.export_preprocess_cache(backend_session, value, request)
        if not isinstance(payload, bytes) or not payload:
            raise ControlFoleyStageContractError("L1 codec must export non-empty immutable bytes")
        return payload

    def encode_condition_feature(
        self,
        session_id: str,
        feature: ControlFoleyConditionFeature,
        request: ControlFoleyLocalRequest,
    ) -> bytes:
        session = self._sessions.get(session_id)
        if session is None:
            raise InvocationRejectedError("staged session is not loaded")
        if not isinstance(self._backend, ControlFoleyConditionFeatureCacheBackend):
            raise ControlFoleyStageContractError(
                "staged backend does not implement the L2 safe feature codec"
            )
        self._validate_condition_feature(feature, feature.role)
        payload = self._backend.export_condition_feature_cache(session, feature, request)
        if not isinstance(payload, bytes) or not payload:
            raise ControlFoleyStageContractError(
                "L2 feature codec must export non-empty immutable bytes"
            )
        return payload

    def unload(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        self._validation.pop(session_id, None)
        if session is not None:
            self._backend.unload(session)


class DeterministicFakeStagedBackend:
    """CPU test double.  It is deliberately not an upstream or GPU backend."""

    backend_id = "deterministic-cpu-test-double-v1"
    condition_cache_codec_version = "fake-condition-codec-v1"
    condition_cache_schema_version = "fake-condition-schema-v1"

    def __init__(
        self,
        fail_stage: Optional[ControlFoleyStage] = None,
        cancel_stage: Optional[ControlFoleyStage] = None,
    ) -> None:
        self.fail_stage = fail_stage
        self.cancel_stage = cancel_stage
        self.calls: list[ControlFoleyStage] = []
        self.loaded = 0
        self.unloaded = 0
        self.requests: list[ControlFoleyLocalRequest] = []
        self.condition_encoder_calls: Dict[str, int] = {
            role: 0 for role in ("video", "reference_audio", "text")
        }
        self.condition_projection_calls = 0

    def load(
        self, configuration: Mapping[str, Any]
    ) -> Tuple[ControlFoleyStagedBackendSession, ControlFoleyStagedBackendValidation]:
        self.loaded += 1
        fingerprint = str(configuration["staged_deployment_fingerprint"])
        return (
            ControlFoleyStagedBackendSession(self.backend_id, fingerprint, "cpu-test-double"),
            ControlFoleyStagedBackendValidation(
                self.backend_id,
                str(configuration["deployment_manifest_sha256"]),
                str(configuration["source_revision"]),
                str(configuration["variant"]),
                str(configuration["precision"]),
                str(configuration["checkpoint_sha256"]),
                "deterministic CPU test double; no parity or performance claim",
            ),
        )

    def run_stage(
        self,
        session: ControlFoleyStagedBackendSession,
        stage: ControlFoleyStage,
        previous: Optional[ControlFoleyStageValue],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue:
        if stage is ControlFoleyStage.CONDITION_ENCODE_PROJECTION:
            if previous is None:
                raise ControlFoleyStageContractError("condition stage requires preprocess output")
            features = tuple(
                self.encode_condition_feature(session, role, previous, request, context)
                for role in controlfoley_condition_feature_roles(request)
            )
            return self.project_condition_features(session, previous, features, request, context)
        self.calls.append(stage)
        self.requests.append(request)
        if stage is self.cancel_stage:
            context.cancellation_token.cancel("deterministic staged cancellation")
            context.cancellation_token.raise_if_cancelled()
        if stage is self.fail_stage:
            raise RuntimeError("deterministic staged fault")
        del session
        prior = b"root" if previous is None else _payload_bytes(previous.payload)
        request_bytes = _canonical_request_bytes(request)
        value = hashlib.sha256(prior + b":" + stage.value.encode() + b":" + request_bytes).digest()
        payload: object = value
        if stage is ControlFoleyStage.DECODE_VOCODER_POSTPROCESS:
            payload = ControlFoleyStagedArtifact(
                _deterministic_wav(value),
                name="controlfoley-staged.wav",
                media_type="audio/wav",
                metadata={"test_double": True},
            )
        return ControlFoleyStageValue(stage, payload, {"test_double": True})

    def encode_condition_feature(
        self,
        session: ControlFoleyStagedBackendSession,
        role: str,
        preprocess: ControlFoleyStageValue,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyConditionFeature:
        del session
        context.cancellation_token.raise_if_cancelled()
        if role not in controlfoley_condition_feature_roles(request):
            raise ControlFoleyStageContractError("fake encoder role is not required by task")
        self.condition_encoder_calls[role] += 1
        payload = hashlib.sha256(
            role.encode("ascii")
            + b":"
            + _payload_bytes(preprocess.payload)
            + b":"
            + _canonical_request_bytes(request)
        ).digest()
        return ControlFoleyConditionFeature(role, payload, {"test_double": True})

    def project_condition_features(
        self,
        session: ControlFoleyStagedBackendSession,
        preprocess: ControlFoleyStageValue,
        features: Tuple[ControlFoleyConditionFeature, ...],
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue:
        del session
        self.calls.append(ControlFoleyStage.CONDITION_ENCODE_PROJECTION)
        self.requests.append(request)
        if self.cancel_stage is ControlFoleyStage.CONDITION_ENCODE_PROJECTION:
            context.cancellation_token.cancel("deterministic staged cancellation")
            context.cancellation_token.raise_if_cancelled()
        if self.fail_stage is ControlFoleyStage.CONDITION_ENCODE_PROJECTION:
            raise RuntimeError("deterministic staged fault")
        context.cancellation_token.raise_if_cancelled()
        expected = controlfoley_condition_feature_roles(request)
        if tuple(feature.role for feature in features) != expected:
            raise ControlFoleyStageContractError("fake projection feature roles are invalid")
        self.condition_projection_calls += 1
        payload = hashlib.sha256(
            _payload_bytes(preprocess.payload)
            + b":condition_encode_projection:"
            + b":".join(_payload_bytes(feature.payload) for feature in features)
            + b":"
            + _canonical_request_bytes(request)
        ).digest()
        return ControlFoleyStageValue(
            ControlFoleyStage.CONDITION_ENCODE_PROJECTION,
            payload,
            {"test_double": True},
        )

    def export_condition_feature_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        feature: ControlFoleyConditionFeature,
        request: ControlFoleyLocalRequest,
    ) -> bytes:
        del session, request
        if not isinstance(feature.payload, bytes) or not feature.payload:
            raise ControlFoleyStageContractError("fake feature payload is invalid")
        return build_u8_safe_tensor_bundle(
            feature.payload,
            codec_version=self.condition_cache_codec_version,
            schema_version=self.condition_cache_schema_version,
            tensor_name=feature.role,
        )

    def import_condition_feature_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        role: str,
        payload: bytes,
        preprocess: ControlFoleyStageValue,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyConditionFeature:
        del session, preprocess
        context.cancellation_token.raise_if_cancelled()
        if role not in controlfoley_condition_feature_roles(request):
            raise ControlFoleyStageContractError("fake cached feature role is invalid")
        bundle = validate_safe_tensor_bundle(
            payload,
            codec_version=self.condition_cache_codec_version,
            schema_version=self.condition_cache_schema_version,
        )
        if (
            len(bundle.tensors) != 1
            or bundle.tensors[0].name != role
            or bundle.tensors[0].dtype != "U8"
            or bundle.tensors[0].shape != (len(bundle.data),)
        ):
            raise ControlFoleyStageContractError("fake cached feature tensor schema is invalid")
        value = bundle.tensor_bytes(role)
        if not value:
            raise ControlFoleyStageContractError("fake cached feature bytes are empty")
        return ControlFoleyConditionFeature(
            role,
            value,
            {"test_double": True, "cache_level": "l2_condition"},
        )

    def unload(self, session: ControlFoleyStagedBackendSession) -> None:
        del session
        self.unloaded += 1

    def export_preprocess_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        value: ControlFoleyStageValue,
        request: ControlFoleyLocalRequest,
    ) -> bytes:
        del session, request
        if (
            value.stage is not ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS
            or not isinstance(value.payload, bytes)
            or not value.payload
        ):
            raise ControlFoleyStageContractError("fake preprocess value is invalid")
        return value.payload

    def import_preprocess_cache(
        self,
        session: ControlFoleyStagedBackendSession,
        payload: bytes,
        request: ControlFoleyLocalRequest,
        context: ExecutionContext,
    ) -> ControlFoleyStageValue:
        del session, request
        context.cancellation_token.raise_if_cancelled()
        if not isinstance(payload, bytes) or not payload:
            raise ControlFoleyStageContractError("fake cached preprocess bytes are invalid")
        return ControlFoleyStageValue(
            ControlFoleyStage.MEDIA_RESOLVE_PREPROCESS,
            payload,
            {"test_double": True, "cache_level": "l1_preprocess"},
        )


def _payload_bytes(value: object) -> bytes:
    if isinstance(value, bytes):
        return value
    if isinstance(value, ControlFoleyStagedArtifact):
        return value.content
    raise ControlFoleyStageContractError("fake staged payload is invalid")


def _canonical_request_bytes(request: ControlFoleyLocalRequest) -> bytes:
    values = (
        request.task.value,
        request.prompt or "",
        str(request.duration_seconds),
        str(request.num_steps),
        str(request.guidance_scale),
        str(request.seed),
    )
    return "\x1f".join(values).encode("utf-8")


def _deterministic_wav(digest: bytes) -> bytes:
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(8_000)
        samples = bytearray()
        for index in range(0, 16, 2):
            sample = int.from_bytes(digest[index : index + 2], "little", signed=False) - 32_768
            samples.extend(sample.to_bytes(2, "little", signed=True))
        output.writeframes(bytes(samples))
    return buffer.getvalue()


def _freeze_evidence(value: Mapping[str, object]) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise TypeError("stage evidence must be a mapping")
    frozen: Dict[str, object] = {}
    for key, item in value.items():
        _text(key, "evidence key")
        frozen[key] = _freeze_evidence_value(item)
    return MappingProxyType(frozen)


def _freeze_evidence_value(value: object) -> object:
    if isinstance(value, Mapping):
        return _freeze_evidence(value)
    if isinstance(value, (list, tuple)):
        return tuple(_freeze_evidence_value(item) for item in value)
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError("stage evidence contains an unsupported value")


def _text(value: str, name: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("{0} must be a non-empty string".format(name))


def _sha256(value: str, name: str) -> None:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or value != value.lower()
        or len(set(value)) == 1
    ):
        raise ValueError("{0} must be a lowercase full SHA-256".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be hexadecimal".format(name)) from error


def _git_revision(value: str, name: str) -> None:
    if not isinstance(value, str) or len(value) != 40 or value != value.lower():
        raise ValueError("{0} must be a lowercase full git revision".format(name))
    try:
        int(value, 16)
    except ValueError as error:
        raise ValueError("{0} must be hexadecimal".format(name)) from error
