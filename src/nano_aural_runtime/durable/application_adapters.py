"""Production compositions behind the Phase 3E application Protocols."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, BinaryIO, Callable, Mapping, Optional, Protocol, Sequence
from uuid import uuid4

from .application import UploadView, VisibleArtifactEvidence
from .domain import ArtifactKind, EventType, JobEventRecord, JobInput, JobRecord
from .errors import NotFoundError
from .postgres_repository import PostgresDurableRepository
from .publication import PostgresPublicationRepository, VisibleArtifact
from .uploads import (
    StagingBlobStore,
    UploadMode,
    UploadRepository,
    UploadSession,
    UploadVerifier,
)


class PostgresApplicationRepository:
    """Job commands plus stable BIGSERIAL event-cursor reads."""

    def __init__(
        self,
        connection: Any,
        durable: Optional[PostgresDurableRepository] = None,
    ) -> None:
        self._connection = connection
        self._durable = durable or PostgresDurableRepository(connection)

    def create_job(
        self,
        namespace_id: str,
        idempotency_key: str,
        request: Mapping[str, object],
        deployment_id: str,
        inputs: Sequence[JobInput],
        required_artifact_kinds: Sequence[ArtifactKind],
    ) -> JobRecord:
        return self._durable.create_job(
            namespace_id,
            idempotency_key,
            request,
            deployment_id,
            inputs,
            required_artifact_kinds,
        )

    def get_job(self, job_id: str) -> JobRecord:
        return self._durable.get_job(job_id)

    def request_cancel(self, job_id: str) -> JobRecord:
        return self._durable.request_cancel(job_id)

    def list_events(
        self, job_id: str, after_event_id: Optional[str], limit: int
    ) -> Sequence[JobEventRecord]:
        if after_event_id is not None and (
            len(after_event_id) > 20
            or not after_event_id.isascii()
            or not after_event_id.isdecimal()
            or after_event_id != str(int(after_event_id))
        ):
            raise ValueError("event cursor must be a canonical decimal id")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 101:
            raise ValueError("event limit must be between 1 and 101")
        cursor = int(after_event_id) if after_event_id is not None else 0
        with self._connection.transaction():
            rows = self._connection.execute(
                """SELECT id,job_id,attempt_id,event_type,payload
                   FROM job_events WHERE job_id=%s AND id>%s
                     AND event_type=ANY(%s)
                   ORDER BY id LIMIT %s""",
                (job_id, cursor, [item.value for item in EventType], limit),
            ).fetchall()
        return tuple(
            JobEventRecord(
                event_id=str(row[0]),
                job_id=str(row[1]),
                attempt_id=str(row[2]) if row[2] is not None else None,
                event_type=EventType(str(row[3])),
                payload=dict(row[4]) if isinstance(row[4], Mapping) else {},
            )
            for row in rows
        )


class ArtifactReader(Protocol):
    def open_reader(self, storage_key: str) -> BinaryIO: ...


class WinnerCatalog(Protocol):
    def visible_winner(
        self, job_id: str, namespace_id: Optional[str] = None
    ) -> Sequence[VisibleArtifact]: ...


class PublishedArtifactCatalog:
    """Join PostgreSQL visible-winner evidence to immutable object reads."""

    def __init__(self, publications: WinnerCatalog, storage: ArtifactReader) -> None:
        self._publications = publications
        self._storage = storage

    @classmethod
    def from_postgres(cls, connection: Any, storage: ArtifactReader) -> "PublishedArtifactCatalog":
        return cls(PostgresPublicationRepository(connection), storage)

    def list_visible(self, job_id: str) -> Sequence[VisibleArtifactEvidence]:
        return self._publications.visible_winner(job_id)

    def open_reader(self, artifact: VisibleArtifactEvidence) -> BinaryIO:
        matches = tuple(
            item
            for item in self._publications.visible_winner(artifact.job_id)
            if item.artifact_id == artifact.artifact_id
            and item.attempt_id == artifact.attempt_id
            and item.sha256 == artifact.sha256
            and item.storage_key == artifact.storage_key
        )
        if len(matches) != 1:
            raise NotFoundError("visible artifact evidence is stale or invalid")
        return self._storage.open_reader(matches[0].storage_key)


class DurableAssetUploadWorkflow:
    """Small HTTP-facing composition of Phase 3B staging and verification."""

    def __init__(
        self,
        repository: UploadRepository,
        staging: StagingBlobStore,
        verifier: UploadVerifier,
        *,
        max_upload_bytes: int = 1024 * 1024,
        session_ttl: timedelta = timedelta(minutes=15),
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
    ) -> None:
        if (
            isinstance(max_upload_bytes, bool)
            or not isinstance(max_upload_bytes, int)
            or max_upload_bytes < 1
        ):
            raise ValueError("max_upload_bytes must be a positive integer")
        if session_ttl <= timedelta(0):
            raise ValueError("session_ttl must be positive")
        self._repository = repository
        self._staging = staging
        self._verifier = verifier
        self._max_upload_bytes = max_upload_bytes
        self._session_ttl = session_ttl
        self._clock = clock

    def initiate(
        self, namespace_id: str, expected_size_bytes: int, expected_sha256: Optional[str]
    ) -> UploadView:
        if expected_size_bytes > self._max_upload_bytes:
            raise ValueError("upload exceeds configured size limit")
        session_id = str(uuid4())
        session = UploadSession(
            session_id,
            namespace_id,
            UploadMode.SINGLE,
            expected_size_bytes,
            "staging/" + session_id,
            self._clock() + self._session_ttl,
            expected_sha256,
        )
        return self._view(self._repository.create_session(session))

    def get(self, session_id: str) -> UploadView:
        return self._view(self._repository.get_session(session_id))

    def upload(self, session_id: str, content: bytes) -> UploadView:
        session = self._repository.get_session(session_id)
        if len(content) > self._max_upload_bytes or len(content) != session.expected_size_bytes:
            raise ValueError("upload size does not match the initiated session")
        staging_key = self._staging.write_stream(session_id, (content,))
        if staging_key != session.staging_key:
            raise RuntimeError("staging store returned a key for another session")
        uploaded = self._repository.mark_uploaded(session_id, session.version)
        return self._view(self._verifier.finalize(session_id, uploaded.version))

    @staticmethod
    def _view(session: UploadSession) -> UploadView:
        return UploadView(
            session.session_id,
            session.namespace_id,
            session.expected_size_bytes,
            session.version,
            session.state.value,
            session.verified_asset_id,
        )
