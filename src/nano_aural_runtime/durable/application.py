"""Framework-free Phase 3E application and authorization boundary.

HTTP adapters authenticate a request, construct a :class:`Principal`, and
delegate here.  Namespace authorization deliberately turns cross-tenant object
access into ``ResourceNotFound`` so API adapters can return the same 404 for a
missing resource and an IDOR attempt.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import BinaryIO, Mapping, Optional, Protocol, Sequence, Tuple

from .domain import ArtifactKind, EventType, JobEventRecord, JobInput, JobRecord, JobState
from .errors import NotFoundError


class ApplicationError(Exception):
    """Base error which an HTTP adapter may translate without leaking internals."""


class AccessDenied(ApplicationError):
    """The authenticated principal lacks the required operation scope."""


class AuthenticationFailed(ApplicationError):
    """No valid authenticated principal could be established."""


class ResourceNotFound(ApplicationError):
    """A resource is missing or deliberately hidden across namespace boundaries."""


class InvalidRequest(ApplicationError):
    """The remote request contains an invalid or prohibited field."""


@dataclass(frozen=True)
class Principal:
    subject: str
    _authorization_context: object = field(default=None, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not isinstance(self.subject, str) or not self.subject.strip():
            raise ValueError("principal subject must be non-empty")


class Authenticator(Protocol):
    """Transport-owned bearer/session authentication boundary."""

    def authenticate(self, authorization: str) -> Principal: ...


class AuthorizationPolicy(Protocol):
    """Application authorization independent of an identity provider SDK."""

    def has_scope(self, principal: Principal, scope: str) -> bool: ...

    def allows_namespace(self, principal: Principal, namespace_id: str) -> bool: ...


class JobApplicationRepository(Protocol):
    def create_job(
        self,
        namespace_id: str,
        idempotency_key: str,
        request: Mapping[str, object],
        deployment_id: str,
        inputs: Sequence[JobInput],
        required_artifact_kinds: Sequence[ArtifactKind],
    ) -> JobRecord: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def request_cancel(self, job_id: str) -> JobRecord: ...

    def list_events(
        self, job_id: str, after_event_id: Optional[str], limit: int
    ) -> Sequence[JobEventRecord]: ...


class VisibleArtifactEvidence(Protocol):
    """Structural view implemented by the publication repository's winner record."""

    @property
    def artifact_id(self) -> str: ...

    @property
    def job_id(self) -> str: ...

    @property
    def attempt_id(self) -> str: ...

    @property
    def kind(self) -> ArtifactKind: ...

    @property
    def sha256(self) -> str: ...

    @property
    def size_bytes(self) -> int: ...

    @property
    def content_type(self) -> str: ...

    @property
    def storage_key(self) -> str: ...


class VisibleArtifactCatalog(Protocol):
    """Read-only view backed by the Phase 3E publication authority."""

    def list_visible(self, job_id: str) -> Sequence[VisibleArtifactEvidence]: ...

    def open_reader(self, artifact: VisibleArtifactEvidence) -> BinaryIO: ...


@dataclass(frozen=True)
class UploadView:
    session_id: str
    namespace_id: str
    expected_size_bytes: int
    version: int
    state: str
    asset_id: Optional[str] = None


class AssetUploadWorkflow(Protocol):
    """Application-facing composition of the Phase 3B upload primitives."""

    def initiate(
        self, namespace_id: str, expected_size_bytes: int, expected_sha256: Optional[str]
    ) -> UploadView: ...

    def get(self, session_id: str) -> UploadView: ...

    def upload(self, session_id: str, content: bytes) -> UploadView: ...


@dataclass(frozen=True)
class SubmitJob:
    namespace_id: str
    idempotency_key: str
    deployment_id: str
    request: Mapping[str, object]
    inputs: Tuple[JobInput, ...] = ()
    required_artifact_kinds: Tuple[ArtifactKind, ...] = (ArtifactKind.OUTPUT,)


@dataclass(frozen=True)
class JobView:
    job_id: str
    namespace_id: str
    state: JobState
    cancel_requested: bool
    winning_attempt_id: Optional[str]

    @classmethod
    def from_record(cls, job: JobRecord) -> "JobView":
        return cls(
            job.job_id,
            job.namespace_id,
            job.state,
            job.cancel_requested,
            job.winning_attempt_id,
        )


@dataclass(frozen=True)
class EventView:
    event_id: str
    event_type: EventType
    attempt_id: Optional[str]
    payload: Mapping[str, object]

    @classmethod
    def from_record(cls, event: JobEventRecord) -> "EventView":
        payload: dict[str, object] = {}
        reason = event.payload.get("reason")
        if cls._safe_text(reason, 256):
            payload["reason"] = reason
        for name in ("attempt_no", "lease_epoch"):
            value = event.payload.get(name)
            if not isinstance(value, bool) and isinstance(value, int) and 0 <= value <= 2**63 - 1:
                payload[name] = value
        retry = event.payload.get("retry_not_before")
        if cls._safe_text(retry, 64):
            payload["retry_not_before"] = retry
        kinds = event.payload.get("artifact_kinds")
        if isinstance(kinds, (tuple, list)) and all(
            isinstance(item, str) and item in {kind.value for kind in ArtifactKind}
            for item in kinds
        ):
            payload["artifact_kinds"] = tuple(kinds)
        return cls(event.event_id, event.event_type, event.attempt_id, payload)

    @staticmethod
    def _safe_text(value: object, limit: int) -> bool:
        if not isinstance(value, str) or not value or len(value) > limit:
            return False
        lowered = value.casefold()
        sensitive = ("/", "\\", "bearer", "token", "secret", "password", "authorization")
        return not any(marker in lowered for marker in sensitive)


@dataclass(frozen=True)
class EventPage:
    events: Tuple[EventView, ...]
    next_cursor: Optional[str]


@dataclass(frozen=True)
class ArtifactView:
    artifact_id: str
    kind: ArtifactKind
    sha256: str
    size_bytes: int
    content_type: str

    @classmethod
    def from_visible(cls, artifact: VisibleArtifactEvidence) -> "ArtifactView":
        return cls(
            artifact.artifact_id,
            artifact.kind,
            artifact.sha256,
            artifact.size_bytes,
            artifact.content_type,
        )


@dataclass
class ArtifactDownload:
    artifact: ArtifactView
    reader: BinaryIO


class ApplicationService:
    """Authenticated job commands and queries with namespace-safe reads."""

    _PROHIBITED_REQUEST_KEYS = frozenset(
        {
            "device",
            "module",
            "python_module",
            "source_dir",
            "weights_dir",
            "weights_path",
        }
    )

    def __init__(
        self,
        repository: JobApplicationRepository,
        artifacts: VisibleArtifactCatalog,
        authorization: AuthorizationPolicy,
        uploads: Optional[AssetUploadWorkflow] = None,
    ) -> None:
        self._repository = repository
        self._artifacts = artifacts
        self._authorization = authorization
        self._uploads = uploads

    def initiate_upload(
        self,
        principal: Principal,
        namespace_id: str,
        expected_size_bytes: int,
        expected_sha256: Optional[str],
    ) -> UploadView:
        self._scope(principal, "assets:write")
        if not self._authorization.allows_namespace(principal, namespace_id):
            raise ResourceNotFound("namespace not found")
        if self._uploads is None:
            raise ResourceNotFound("asset upload route not configured")
        return self._uploads.initiate(namespace_id, expected_size_bytes, expected_sha256)

    def upload_asset(self, principal: Principal, session_id: str, content: bytes) -> UploadView:
        self._scope(principal, "assets:write")
        if self._uploads is None:
            raise ResourceNotFound("asset upload route not configured")
        try:
            session = self._uploads.get(session_id)
        except NotFoundError as error:
            raise ResourceNotFound("upload not found") from error
        if not self._authorization.allows_namespace(principal, session.namespace_id):
            raise ResourceNotFound("upload not found")
        if len(content) != session.expected_size_bytes:
            raise InvalidRequest("upload size does not match the initiated session")
        return self._uploads.upload(session_id, content)

    def submit(self, principal: Principal, command: SubmitJob) -> JobView:
        self._scope(principal, "jobs:submit")
        if not self._authorization.allows_namespace(principal, command.namespace_id):
            raise ResourceNotFound("namespace not found")
        self._validate_remote_value(command.request)
        job = self._repository.create_job(
            command.namespace_id,
            command.idempotency_key,
            command.request,
            command.deployment_id,
            command.inputs,
            command.required_artifact_kinds,
        )
        if job.namespace_id != command.namespace_id:
            raise RuntimeError("repository returned a job from another namespace")
        return JobView.from_record(job)

    def get_job(self, principal: Principal, job_id: str) -> JobView:
        return JobView.from_record(self._owned_job(principal, job_id, "jobs:read"))

    def cancel(self, principal: Principal, job_id: str) -> JobView:
        job = self._owned_job(principal, job_id, "jobs:cancel")
        updated = self._repository.request_cancel(job.job_id)
        if updated.namespace_id != job.namespace_id:
            raise RuntimeError("repository cancellation crossed namespace boundary")
        return JobView.from_record(updated)

    def events(
        self,
        principal: Principal,
        job_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> EventPage:
        if cursor is not None and (
            not isinstance(cursor, str)
            or len(cursor) > 20
            or not cursor.isascii()
            or not cursor.isdecimal()
            or cursor != str(int(cursor))
        ):
            raise InvalidRequest("event cursor is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
            raise InvalidRequest("event limit must be between 1 and 100")
        job = self._owned_job(principal, job_id, "jobs:read")
        events = tuple(self._repository.list_events(job.job_id, cursor, limit + 1))
        if any(event.job_id != job.job_id for event in events):
            raise RuntimeError("repository returned an event for another job")
        visible = events[:limit]
        next_cursor = visible[-1].event_id if len(events) > limit else None
        return EventPage(tuple(EventView.from_record(event) for event in visible), next_cursor)

    def artifacts(self, principal: Principal, job_id: str) -> Tuple[ArtifactView, ...]:
        job = self._owned_job(principal, job_id, "artifacts:read")
        return tuple(ArtifactView.from_visible(item) for item in self._visible(job))

    def download(self, principal: Principal, job_id: str, artifact_id: str) -> ArtifactDownload:
        job = self._owned_job(principal, job_id, "artifacts:read")
        matches = tuple(item for item in self._visible(job) if item.artifact_id == artifact_id)
        if len(matches) != 1:
            raise ResourceNotFound("artifact not found")
        artifact = matches[0]
        return ArtifactDownload(
            ArtifactView.from_visible(artifact), self._artifacts.open_reader(artifact)
        )

    def _owned_job(self, principal: Principal, job_id: str, scope: str) -> JobRecord:
        self._scope(principal, scope)
        try:
            job = self._repository.get_job(job_id)
        except NotFoundError as error:
            raise ResourceNotFound("job not found") from error
        if not self._authorization.allows_namespace(principal, job.namespace_id):
            raise ResourceNotFound("job not found")
        return job

    def _scope(self, principal: Principal, scope: str) -> None:
        if not self._authorization.has_scope(principal, scope):
            raise AccessDenied("required scope is missing")

    def _visible(self, job: JobRecord) -> Tuple[VisibleArtifactEvidence, ...]:
        if job.state != JobState.SUCCEEDED or job.winning_attempt_id is None:
            return ()
        visible = tuple(self._artifacts.list_visible(job.job_id))
        legal = tuple(
            item
            for item in visible
            if item.job_id == job.job_id and item.attempt_id == job.winning_attempt_id
        )
        if len({item.artifact_id for item in legal}) != len(legal):
            raise RuntimeError("artifact catalog returned duplicate visible ids")
        return legal

    @classmethod
    def _validate_remote_value(cls, value: object) -> None:
        if value is None or isinstance(value, (bool, int, str)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise InvalidRequest("request cannot contain non-finite numbers")
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise InvalidRequest("request keys must be strings")
                normalized = key.lower()
                if normalized in cls._PROHIBITED_REQUEST_KEYS or normalized.endswith("_path"):
                    raise InvalidRequest("request contains a prohibited server-local field")
                cls._validate_remote_value(item)
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                cls._validate_remote_value(item)
            return
        raise InvalidRequest("request must contain only JSON values")
