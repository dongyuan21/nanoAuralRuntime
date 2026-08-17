"""Thin ComfyUI-style nodes over the authoritative public remote client."""

from __future__ import annotations

import atexit
import json
import math
import re
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Protocol, Tuple, cast
from uuid import UUID

from nano_aural_runtime_remote import (
    RemoteClient,
    RemoteEventPage,
    UrllibTransport,
)

from .bootstrap import (
    REMOTE_CONFIG_ENV,
    RemoteOperatorConfig,
    remote_operator_config_from_environment,
)


class RemoteNodeError(RuntimeError):
    pass


class RemoteNodeValidationError(RemoteNodeError):
    pass


class RemoteNodeExecutionError(RemoteNodeError):
    pass


class RemoteNodeCancelled(RemoteNodeError):
    pass


class CancellationSource(Protocol):
    def is_cancelled(self) -> bool: ...


class RemoteClientProtocol(Protocol):
    def upload_asset(self, namespace_id: str, source: Path) -> Mapping[str, object]: ...

    def submit(self, command: Mapping[str, object]) -> Mapping[str, object]: ...

    def status(self, job_id: str) -> Mapping[str, object]: ...

    def wait(
        self,
        job_id: str,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: Optional[float] = None,
    ) -> Mapping[str, object]: ...

    def cancel(self, job_id: str) -> Mapping[str, object]: ...

    def events(
        self, job_id: str, *, cursor: Optional[str] = None, limit: int = 50
    ) -> RemoteEventPage: ...

    def artifacts(self, job_id: str) -> Tuple[Mapping[str, object], ...]: ...

    def download(self, job_id: str, artifact_id: str, destination: Path) -> Path: ...


_JOB_STATES = frozenset(("queued", "running", "succeeded", "failed", "cancelled"))
_ASSET_ROLES = ("video", "reference_audio", "audio")
_ARTIFACT_KINDS = ("output", "manifest")
_EVENT_TYPES = frozenset(
    (
        "job_created",
        "attempt_started",
        "cancel_requested",
        "attempt_cancelled",
        "attempt_failed",
        "attempt_requeued",
        "job_succeeded",
    )
)
_CONTENT_TYPE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,63}/[a-z0-9][a-z0-9.+-]{0,63}$")


def _text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise RemoteNodeExecutionError("remote response has an invalid {0}".format(field))
    return value


def _uuid_text(value: object, field: str) -> str:
    text = _text(value, field)
    try:
        parsed = UUID(text)
    except ValueError:
        raise RemoteNodeExecutionError(
            "remote response has an invalid canonical {0}".format(field)
        ) from None
    if str(parsed) != text:
        raise RemoteNodeExecutionError("remote response has an invalid canonical {0}".format(field))
    return text


def _event_cursor(value: object, field: str) -> str:
    text = _text(value, field)
    if len(text) > 20 or not text.isascii() or not text.isdecimal() or text != str(int(text)):
        raise RemoteNodeExecutionError("remote response has an invalid event cursor")
    return text


def _event_type(value: object) -> str:
    text = _text(value, "event type")
    if text not in _EVENT_TYPES:
        raise RemoteNodeExecutionError("remote response has an invalid event type")
    return text


def _monotonic_value(value: object, *, previous: Optional[float] = None) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value < 0
        or (previous is not None and value < previous)
    ):
        raise RemoteNodeExecutionError("monotonic clock returned invalid data")
    return float(value)


def _read_monotonic(clock: Callable[[], float], *, previous: Optional[float] = None) -> float:
    try:
        value = clock()
    except BaseException:
        raise RemoteNodeExecutionError("monotonic clock failed") from None
    return _monotonic_value(value, previous=previous)


@dataclass(frozen=True)
class RemoteAssetBinding:
    role: str
    asset_id: str

    def __post_init__(self) -> None:
        if self.role not in _ASSET_ROLES:
            raise RemoteNodeValidationError("asset role is not allowed")
        _uuid_text(self.asset_id, "asset_id")


@dataclass(frozen=True)
class RemoteAssetBundle:
    bindings: Tuple[RemoteAssetBinding, ...] = ()

    def __post_init__(self) -> None:
        if (
            not isinstance(self.bindings, tuple)
            or len(self.bindings) > 8
            or not all(isinstance(item, RemoteAssetBinding) for item in self.bindings)
        ):
            raise RemoteNodeValidationError("remote asset bundle is invalid")
        roles = tuple(item.role for item in self.bindings)
        if len(set(roles)) != len(roles):
            raise RemoteNodeValidationError("remote asset bundle roles must be unique")


@dataclass(frozen=True)
class RemoteJobRef:
    job_id: str
    state: str
    event_count: int = 0
    event_types: Tuple[str, ...] = ()

    def __post_init__(self) -> None:
        _uuid_text(self.job_id, "job_id")
        if self.state not in _JOB_STATES:
            raise RemoteNodeExecutionError("remote response has an invalid job state")
        if (
            isinstance(self.event_count, bool)
            or not isinstance(self.event_count, int)
            or self.event_count < 0
            or not isinstance(self.event_types, tuple)
            or self.event_count != len(self.event_types)
            or not all(item in _EVENT_TYPES for item in self.event_types)
        ):
            raise RemoteNodeExecutionError("remote event summary is invalid")


@dataclass(frozen=True)
class RemoteEventSummary:
    job_id: str
    count: int
    event_types: Tuple[str, ...]
    next_cursor: Optional[str]

    def __post_init__(self) -> None:
        _uuid_text(self.job_id, "job_id")
        if (
            isinstance(self.count, bool)
            or not isinstance(self.count, int)
            or not isinstance(self.event_types, tuple)
            or self.count != len(self.event_types)
            or not all(item in _EVENT_TYPES for item in self.event_types)
        ):
            raise RemoteNodeExecutionError("remote event summary is invalid")
        if self.next_cursor is not None:
            _event_cursor(self.next_cursor, "next_cursor")


@dataclass(frozen=True)
class RemoteArtifactRef:
    job_id: str
    artifact_id: str
    kind: str
    sha256: str
    size_bytes: int
    content_type: str

    def __post_init__(self) -> None:
        _uuid_text(self.job_id, "job_id")
        _uuid_text(self.artifact_id, "artifact_id")
        if self.kind not in _ARTIFACT_KINDS:
            raise RemoteNodeExecutionError("remote artifact kind is invalid")
        if not isinstance(self.content_type, str) or not _CONTENT_TYPE.fullmatch(self.content_type):
            raise RemoteNodeExecutionError("remote artifact content_type is invalid")
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise RemoteNodeExecutionError("remote artifact has an invalid size")
        if (
            not isinstance(self.sha256, str)
            or len(self.sha256) != 64
            or self.sha256.lower() != self.sha256
        ):
            raise RemoteNodeExecutionError("remote artifact has invalid SHA-256 evidence")
        try:
            int(self.sha256, 16)
        except ValueError:
            raise RemoteNodeExecutionError("remote artifact has invalid SHA-256 evidence") from None


@dataclass(frozen=True)
class RemoteArtifactCollection:
    job_id: str
    artifacts: Tuple[RemoteArtifactRef, ...]

    def __post_init__(self) -> None:
        _uuid_text(self.job_id, "job_id")
        if not isinstance(self.artifacts, tuple) or not all(
            isinstance(item, RemoteArtifactRef) and item.job_id == self.job_id
            for item in self.artifacts
        ):
            raise RemoteNodeExecutionError("remote artifact collection is invalid")


@dataclass(frozen=True)
class RemoteDownloadRef:
    job_id: str
    artifact_id: str
    filename: str
    size_bytes: int

    def __post_init__(self) -> None:
        _uuid_text(self.job_id, "job_id")
        _uuid_text(self.artifact_id, "artifact_id")
        _output_name(self.filename)
        if (
            isinstance(self.size_bytes, bool)
            or not isinstance(self.size_bytes, int)
            or self.size_bytes < 0
        ):
            raise RemoteNodeExecutionError("remote download size is invalid")


def _job_ref(payload: Mapping[str, object], *, events: Tuple[str, ...] = ()) -> RemoteJobRef:
    state = _text(payload.get("state"), "state")
    return RemoteJobRef(
        _uuid_text(payload.get("job_id"), "job_id"),
        state,
        len(events),
        events,
    )


def _artifact_ref(job_id: str, payload: Mapping[str, object]) -> RemoteArtifactRef:
    size = payload.get("size_bytes")
    sha256 = payload.get("sha256")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise RemoteNodeExecutionError("remote artifact has an invalid size")
    if not isinstance(sha256, str) or len(sha256) != 64 or sha256.lower() != sha256:
        raise RemoteNodeExecutionError("remote artifact has invalid SHA-256 evidence")
    try:
        int(sha256, 16)
    except ValueError:
        raise RemoteNodeExecutionError("remote artifact has invalid SHA-256 evidence") from None
    return RemoteArtifactRef(
        job_id=job_id,
        artifact_id=_uuid_text(payload.get("artifact_id"), "artifact_id"),
        kind=cast(str, payload.get("kind")),
        sha256=sha256,
        size_bytes=size,
        content_type=cast(str, payload.get("content_type")),
    )


def _request_json(raw: str) -> Mapping[str, object]:
    if not isinstance(raw, str):
        raise RemoteNodeValidationError("request_json must be a string")
    if len(raw.encode("utf-8")) > 1024 * 1024:
        raise RemoteNodeValidationError("request_json exceeds the node size limit")
    try:
        value = json.loads(raw)
    except ValueError:
        raise RemoteNodeValidationError("request_json must contain valid JSON") from None
    if not isinstance(value, dict):
        raise RemoteNodeValidationError("request_json must contain an object")
    return value


def _output_name(value: str) -> str:
    if not isinstance(value, str) or not value or value in {".", ".."}:
        raise RemoteNodeValidationError("output_name must be a non-empty filename")
    if Path(value).name != value or "/" in value or "\\" in value:
        raise RemoteNodeValidationError("output_name must not contain a path")
    if len(value.encode("utf-8")) > 240:
        raise RemoteNodeValidationError("output_name is too long")
    return value


def _check_cancelled(source: Optional[CancellationSource], timeout_seconds: float = 0.2) -> bool:
    if source is None:
        return False
    finished = threading.Event()
    result: list[bool] = []
    failure: list[BaseException] = []

    def check() -> None:
        try:
            result.append(source.is_cancelled())
        except BaseException as error:
            failure.append(error)
        finally:
            finished.set()

    threading.Thread(target=check, name="nano-aural-remote-cancel", daemon=True).start()
    if not finished.wait(timeout_seconds):
        raise RemoteNodeExecutionError("ComfyUI cancellation check blocked")
    if failure:
        raise RemoteNodeExecutionError("ComfyUI cancellation check failed") from None
    if len(result) != 1 or not isinstance(result[0], bool):
        raise RemoteNodeExecutionError("ComfyUI cancellation check returned an invalid value")
    return result[0]


def _acquire_cancellation_source(
    factory: Callable[[], CancellationSource], timeout_seconds: float = 0.2
) -> CancellationSource:
    finished = threading.Event()
    result: list[object] = []
    failure: list[BaseException] = []

    def acquire() -> None:
        try:
            result.append(factory())
        except BaseException as error:
            failure.append(error)
        finally:
            finished.set()

    threading.Thread(
        target=acquire,
        name="nano-aural-remote-cancel-factory",
        daemon=True,
    ).start()
    if not finished.wait(timeout_seconds):
        raise RemoteNodeExecutionError("ComfyUI cancellation source factory blocked")
    if failure:
        raise RemoteNodeExecutionError("ComfyUI cancellation source factory failed") from None
    if len(result) != 1 or not callable(getattr(result[0], "is_cancelled", None)):
        raise RemoteNodeExecutionError("ComfyUI cancellation source factory returned invalid data")
    return cast(CancellationSource, result[0])


class _ComfyHostCancellationSource:
    def __init__(self, callback: Callable[[], None]) -> None:
        self._callback = callback

    def is_cancelled(self) -> bool:
        try:
            self._callback()
        except BaseException as error:
            if type(error).__name__ == "InterruptProcessingException":
                return True
            raise
        return False


def _host_cancellation_source() -> CancellationSource:
    module = sys.modules.get("comfy.model_management")
    callback = getattr(module, "throw_exception_if_processing_interrupted", None)
    if not callable(callback):
        raise RemoteNodeExecutionError("ComfyUI cancellation API is unavailable")
    return _ComfyHostCancellationSource(cast(Callable[[], None], callback))


class _ClientOwner:
    def __init__(
        self,
        factory: Callable[[], RemoteClientProtocol],
        config: RemoteOperatorConfig,
    ) -> None:
        self._factory = factory
        self.config = config
        self._client: Optional[RemoteClientProtocol] = None
        self._lock = threading.Lock()

    def client(self) -> RemoteClientProtocol:
        with self._lock:
            if self._client is None:
                self._client = self._factory()
            return self._client


_owner_lock = threading.Lock()
_owner: Optional[_ClientOwner] = None


def _production_client(config: RemoteOperatorConfig, token: str) -> RemoteClientProtocol:
    transport = UrllibTransport(
        config.base_url,
        config.transport_timeout_seconds,
        allow_loopback_http=config.allow_loopback_http,
    )
    return RemoteClient(
        transport,
        token,
        max_download_bytes=config.max_download_bytes,
        max_upload_bytes=config.max_upload_bytes,
    )


def _configured_owner() -> _ClientOwner:
    global _owner
    with _owner_lock:
        if _owner is None:
            try:
                config, token = remote_operator_config_from_environment()
            except Exception:
                raise RemoteNodeValidationError(
                    "remote operator configuration is unavailable; set {0} to a valid "
                    "strict JSON config".format(REMOTE_CONFIG_ENV)
                ) from None
            _owner = _ClientOwner(lambda: _production_client(config, token), config)
        return _owner


def configure_remote_client_for_host(
    factory: Callable[[], RemoteClientProtocol], config: RemoteOperatorConfig
) -> None:
    global _owner
    if not callable(factory) or not isinstance(config, RemoteOperatorConfig):
        raise TypeError("factory and RemoteOperatorConfig are required")
    with _owner_lock:
        if _owner is not None:
            raise RemoteNodeExecutionError("remote client is already configured")
        _owner = _ClientOwner(factory, config)


def teardown_remote_client() -> None:
    global _owner
    with _owner_lock:
        _owner = None


class _NodeBase:
    CATEGORY = "audio/nanoAuralRuntime/remote"

    def __init__(self, owner: Optional[_ClientOwner] = None) -> None:
        self._owner = owner or _configured_owner()

    @property
    def _client(self) -> RemoteClientProtocol:
        return self._owner.client()

    def _convert(self, operation: Callable[[], Any]) -> Any:
        try:
            return operation()
        except RemoteNodeError:
            raise
        except Exception:
            raise RemoteNodeExecutionError("remote node operation failed") from None


class RemoteUploadNode(_NodeBase):
    FUNCTION = "upload"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_ASSET_BINDING",)
    RETURN_NAMES = ("asset_binding",)

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {
            "required": {
                "namespace_id": ("STRING",),
                "role": (_ASSET_ROLES,),
                "source": ("STRING",),
            }
        }

    def upload(self, namespace_id: str, role: str, source: str) -> Tuple[RemoteAssetBinding]:
        if role not in _ASSET_ROLES:
            raise RemoteNodeValidationError("asset role is not allowed")
        if not isinstance(source, str) or not source:
            raise RemoteNodeValidationError("upload source must be a local path string")
        payload = self._convert(lambda: self._client.upload_asset(namespace_id, Path(source)))
        if payload.get("state") != "verified":
            raise RemoteNodeExecutionError("remote upload did not produce a verified asset")
        return (RemoteAssetBinding(role, _uuid_text(payload.get("asset_id"), "asset_id")),)


class RemoteAssetBundleNode:
    CATEGORY = "audio/nanoAuralRuntime/remote"
    FUNCTION = "bundle"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_ASSET_BUNDLE",)
    RETURN_NAMES = ("inputs",)

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {
            "required": {"asset": ("NANO_AURAL_REMOTE_ASSET_BINDING",)},
            "optional": {"inputs": ("NANO_AURAL_REMOTE_ASSET_BUNDLE",)},
        }

    def bundle(
        self,
        asset: RemoteAssetBinding,
        inputs: Optional[RemoteAssetBundle] = None,
    ) -> Tuple[RemoteAssetBundle]:
        if not isinstance(asset, RemoteAssetBinding):
            raise RemoteNodeValidationError("asset must be a remote asset binding")
        if inputs is not None and not isinstance(inputs, RemoteAssetBundle):
            raise RemoteNodeValidationError("inputs must be a remote asset bundle")
        existing = () if inputs is None else inputs.bindings
        return (RemoteAssetBundle(existing + (asset,)),)


class RemoteSubmitNode(_NodeBase):
    FUNCTION = "submit"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_JOB",)
    RETURN_NAMES = ("job",)

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {
            "required": {
                "namespace_id": ("STRING",),
                "idempotency_key": ("STRING",),
                "deployment_id": ("STRING",),
                "request_json": ("STRING", {"multiline": True, "default": "{}"}),
                "required_artifact_kind": (_ARTIFACT_KINDS,),
            },
            "optional": {
                "inputs": ("NANO_AURAL_REMOTE_ASSET_BUNDLE",),
            },
        }

    def submit(
        self,
        namespace_id: str,
        idempotency_key: str,
        deployment_id: str,
        request_json: str,
        required_artifact_kind: str = "output",
        inputs: Optional[RemoteAssetBundle] = None,
    ) -> Tuple[RemoteJobRef]:
        request = _request_json(request_json)
        if required_artifact_kind not in _ARTIFACT_KINDS:
            raise RemoteNodeValidationError("required artifact kind is not allowed")
        if inputs is not None and not isinstance(inputs, RemoteAssetBundle):
            raise RemoteNodeValidationError("inputs must be a remote asset bundle")
        input_values = (
            []
            if inputs is None
            else [{"role": item.role, "asset_id": item.asset_id} for item in inputs.bindings]
        )
        command: Mapping[str, object] = {
            "namespace_id": namespace_id,
            "idempotency_key": idempotency_key,
            "deployment_id": deployment_id,
            "request": request,
            "inputs": input_values,
            "required_artifact_kinds": [required_artifact_kind],
        }
        payload = self._convert(lambda: self._client.submit(command))
        return (_job_ref(payload),)


class RemoteStatusNode(_NodeBase):
    FUNCTION = "status"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_JOB",)
    RETURN_NAMES = ("job",)

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {"required": {"job": ("NANO_AURAL_REMOTE_JOB",)}}

    def status(self, job: RemoteJobRef) -> Tuple[RemoteJobRef]:
        if not isinstance(job, RemoteJobRef):
            raise RemoteNodeValidationError("job must be a remote job reference")
        payload = self._convert(lambda: self._client.status(job.job_id))
        return (_job_ref(payload),)


class RemoteCancelNode(RemoteStatusNode):
    FUNCTION = "cancel"

    def cancel(self, job: RemoteJobRef) -> Tuple[RemoteJobRef]:
        if not isinstance(job, RemoteJobRef):
            raise RemoteNodeValidationError("job must be a remote job reference")
        payload = self._convert(lambda: self._client.cancel(job.job_id))
        return (_job_ref(payload),)


class RemoteEventsNode(_NodeBase):
    FUNCTION = "events"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_EVENTS",)
    RETURN_NAMES = ("events",)

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {
            "required": {"job": ("NANO_AURAL_REMOTE_JOB",)},
            "optional": {
                "cursor": ("STRING", {"default": ""}),
                "limit": ("INT", {"default": 50, "min": 1, "max": 100}),
            },
        }

    def events(
        self, job: RemoteJobRef, cursor: str = "", limit: int = 50
    ) -> Tuple[RemoteEventSummary]:
        if not isinstance(job, RemoteJobRef):
            raise RemoteNodeValidationError("job must be a remote job reference")
        if cursor:
            _event_cursor(cursor, "cursor")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
            raise RemoteNodeValidationError("event limit must be between 1 and 100")
        page = self._convert(
            lambda: self._client.events(job.job_id, cursor=cursor or None, limit=limit)
        )
        for event in page.events:
            _event_cursor(event.get("event_id"), "event_id")
        types = tuple(_event_type(event.get("type")) for event in page.events)
        return (RemoteEventSummary(job.job_id, len(types), types, page.next_cursor),)


class RemoteWaitNode(_NodeBase):
    FUNCTION = "wait"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_JOB",)
    RETURN_NAMES = ("job",)

    def __init__(
        self,
        owner: Optional[_ClientOwner] = None,
        cancellation_source_factory: Callable[[], CancellationSource] = _host_cancellation_source,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(owner)
        self._cancellation_source_factory = cancellation_source_factory
        self._monotonic = monotonic

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {
            "required": {
                "job": ("NANO_AURAL_REMOTE_JOB",),
                "interval_seconds": ("FLOAT", {"default": 1.0, "min": 0.05, "max": 10.0}),
                "timeout_seconds": ("FLOAT", {"default": 300.0, "min": 0.1, "max": 86400.0}),
            }
        }

    def wait(
        self,
        job: RemoteJobRef,
        interval_seconds: float = 1.0,
        timeout_seconds: float = 300.0,
        cancellation_source: Optional[CancellationSource] = None,
    ) -> Mapping[str, object]:
        if not isinstance(job, RemoteJobRef):
            raise RemoteNodeValidationError("job must be a remote job reference")
        if (
            isinstance(interval_seconds, bool)
            or not isinstance(interval_seconds, (int, float))
            or not math.isfinite(interval_seconds)
            or interval_seconds <= 0
            or interval_seconds > 10
            or isinstance(timeout_seconds, bool)
            or not isinstance(timeout_seconds, (int, float))
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise RemoteNodeValidationError("wait interval and timeout must be positive")
        if timeout_seconds > self._owner.config.max_wait_seconds:
            raise RemoteNodeValidationError("wait timeout exceeds the operator limit")
        source = (
            cancellation_source
            if cancellation_source is not None
            else _acquire_cancellation_source(self._cancellation_source_factory)
        )
        if not callable(getattr(source, "is_cancelled", None)):
            raise RemoteNodeExecutionError("ComfyUI cancellation source is invalid")
        previous_time = _read_monotonic(self._monotonic)
        deadline = previous_time + timeout_seconds
        if not math.isfinite(deadline):
            raise RemoteNodeExecutionError("monotonic clock returned invalid data")
        cursor: Optional[str] = None
        event_types: list[str] = []
        for _ in range(self._owner.config.max_poll_iterations):
            if _check_cancelled(source):
                self._convert(lambda: self._client.cancel(job.job_id))
                raise RemoteNodeCancelled("remote job cancellation was requested")
            now = _read_monotonic(self._monotonic, previous=previous_time)
            previous_time = now
            remaining = deadline - now
            if remaining <= 0:
                raise RemoteNodeExecutionError("remote job wait exceeded its bounded timeout")
            page = self._convert(
                lambda event_cursor=cursor: self._client.events(
                    job.job_id, cursor=event_cursor, limit=50
                )
            )
            for event in page.events:
                _event_cursor(event.get("event_id"), "event_id")
                event_types.append(_event_type(event.get("type")))
            if len(event_types) > 256:
                event_types = event_types[-256:]
            if page.next_cursor is not None:
                cursor = _event_cursor(page.next_cursor, "next_cursor")
            elif page.events:
                cursor = _event_cursor(page.events[-1].get("event_id"), "event_id")
            try:
                payload = self._client.wait(
                    job.job_id,
                    interval_seconds=min(interval_seconds, remaining),
                    timeout_seconds=min(interval_seconds, remaining),
                )
            except TimeoutError:
                continue
            except RemoteNodeError:
                raise
            except Exception:
                raise RemoteNodeExecutionError("remote wait failed") from None
            final = _job_ref(payload, events=tuple(event_types))
            return {
                "ui": {
                    "nano_aural_remote_job": [
                        {
                            "job_id": final.job_id,
                            "state": final.state,
                            "event_count": final.event_count,
                        }
                    ]
                },
                "result": (final,),
            }
        raise RemoteNodeExecutionError("remote job wait exceeded the operator poll limit")


class RemoteArtifactsNode(_NodeBase):
    FUNCTION = "artifacts"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_ARTIFACTS",)
    RETURN_NAMES = ("artifacts",)

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {"required": {"job": ("NANO_AURAL_REMOTE_JOB",)}}

    def artifacts(self, job: RemoteJobRef) -> Tuple[RemoteArtifactCollection]:
        if not isinstance(job, RemoteJobRef):
            raise RemoteNodeValidationError("job must be a remote job reference")
        payloads = self._convert(lambda: self._client.artifacts(job.job_id))
        return (
            RemoteArtifactCollection(
                job.job_id,
                tuple(_artifact_ref(job.job_id, item) for item in payloads),
            ),
        )


class RemoteDownloadNode(_NodeBase):
    FUNCTION = "download"
    RETURN_TYPES = ("NANO_AURAL_REMOTE_DOWNLOAD",)
    RETURN_NAMES = ("download",)

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {
            "required": {
                "artifacts": ("NANO_AURAL_REMOTE_ARTIFACTS",),
                "output_name": ("STRING", {"default": "nano-aural-output.flac"}),
            },
            "optional": {"artifact_id": ("STRING", {"default": ""})},
        }

    def download(
        self,
        artifacts: RemoteArtifactCollection,
        output_name: str,
        artifact_id: str = "",
    ) -> Tuple[RemoteDownloadRef]:
        if not isinstance(artifacts, RemoteArtifactCollection):
            raise RemoteNodeValidationError("artifacts must be a remote artifact collection")
        matches = tuple(
            item
            for item in artifacts.artifacts
            if not artifact_id or item.artifact_id == artifact_id
        )
        if len(matches) != 1:
            raise RemoteNodeValidationError("select exactly one listed artifact_id before download")
        artifact = matches[0]
        name = _output_name(output_name)
        destination = self._owner.config.download_dir / name
        path = self._convert(
            lambda: self._client.download(artifact.job_id, artifact.artifact_id, destination)
        )
        if not isinstance(path, Path) or path != destination:
            raise RemoteNodeExecutionError("verified download is unavailable")
        try:
            size = path.stat().st_size
        except OSError:
            raise RemoteNodeExecutionError("verified download is unavailable") from None
        if size != artifact.size_bytes:
            raise RemoteNodeExecutionError("downloaded file size differs from artifact listing")
        return (RemoteDownloadRef(artifact.job_id, artifact.artifact_id, name, size),)


class RemoteOutputNode:
    CATEGORY = "audio/nanoAuralRuntime/remote"
    FUNCTION = "present"
    OUTPUT_NODE = True
    RETURN_TYPES: Tuple[()] = ()

    @classmethod
    def INPUT_TYPES(cls) -> Mapping[str, Mapping[str, Tuple[Any, ...]]]:
        return {"required": {"download": ("NANO_AURAL_REMOTE_DOWNLOAD",)}}

    def present(self, download: RemoteDownloadRef) -> Mapping[str, object]:
        if not isinstance(download, RemoteDownloadRef):
            raise RemoteNodeValidationError("download must be a remote download reference")
        return {
            "ui": {
                "nano_aural_remote_download": [
                    {
                        "job_id": download.job_id,
                        "artifact_id": download.artifact_id,
                        "filename": download.filename,
                        "size_bytes": download.size_bytes,
                    }
                ]
            },
            "result": (),
        }


NODE_CLASS_MAPPINGS = {
    "NanoAuralRemoteUpload": RemoteUploadNode,
    "NanoAuralRemoteAssetBundle": RemoteAssetBundleNode,
    "NanoAuralRemoteSubmit": RemoteSubmitNode,
    "NanoAuralRemoteStatus": RemoteStatusNode,
    "NanoAuralRemoteWait": RemoteWaitNode,
    "NanoAuralRemoteCancel": RemoteCancelNode,
    "NanoAuralRemoteEvents": RemoteEventsNode,
    "NanoAuralRemoteArtifacts": RemoteArtifactsNode,
    "NanoAuralRemoteDownload": RemoteDownloadNode,
    "NanoAuralRemoteOutput": RemoteOutputNode,
}
NODE_DISPLAY_NAME_MAPPINGS = {
    name: name.replace("NanoAural", "NanoAural ").replace("Remote", "Remote ").strip()
    for name in NODE_CLASS_MAPPINGS
}


atexit.register(teardown_remote_client)
