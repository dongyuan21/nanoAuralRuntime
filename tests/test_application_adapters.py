# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import io
import json
import wave
from contextlib import AbstractContextManager
from dataclasses import replace
from pathlib import Path
from typing import BinaryIO, Mapping, Optional, Sequence, cast

import pytest

from nano_aural_runtime.durable.api import ApiRequest, ApplicationApi
from nano_aural_runtime.durable.application import (
    ApplicationService,
    AuthenticationFailed,
    JobApplicationRepository,
    Principal,
    VisibleArtifactEvidence,
)
from nano_aural_runtime.durable.application_adapters import (
    DurableAssetUploadWorkflow,
    PostgresApplicationRepository,
    PublishedArtifactCatalog,
)
from nano_aural_runtime.durable.domain import ArtifactKind, EventType, JobRecord
from nano_aural_runtime.durable.errors import NotFoundError
from nano_aural_runtime.durable.postgres_repository import PostgresDurableRepository
from nano_aural_runtime.durable.publication import VisibleArtifact
from nano_aural_runtime.durable.upload_transports import InMemoryS3CompatibleCanonical
from nano_aural_runtime.durable.uploads import (
    InMemoryS3CompatibleStaging,
    InMemoryUploadRepository,
    UploadVerifier,
    WaveMediaProbe,
)
from nano_aural_runtime_remote.client import (
    HttpResponse,
    RemoteClient,
    RemoteNotFound,
)


class Transaction(AbstractContextManager[None]):
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


class Result:
    def __init__(self, rows: Sequence[Sequence[object]]) -> None:
        self._rows = rows

    def fetchall(self) -> Sequence[Sequence[object]]:
        return self._rows


class Connection:
    def __init__(self, rows: Sequence[Sequence[object]]) -> None:
        self.rows = rows
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def transaction(self) -> Transaction:
        return Transaction()

    def execute(self, sql: str, params: tuple[object, ...]) -> Result:
        self.calls.append((sql, params))
        return Result(self.rows)


class Durable:
    def create_job(self, *args: object) -> JobRecord:
        raise AssertionError("not used")

    def get_job(self, job_id: str) -> JobRecord:
        raise AssertionError(job_id)

    def request_cancel(self, job_id: str) -> JobRecord:
        raise AssertionError(job_id)


def test_postgres_event_adapter_uses_bigserial_cursor_and_bounded_ordered_query() -> None:
    connection = Connection(
        (
            (8, "job-1", None, "job_created", {"reason": "accepted"}),
            (9, "job-1", "attempt-1", "cancel_requested", {}),
        )
    )
    repository = PostgresApplicationRepository(
        connection, cast(PostgresDurableRepository, Durable())
    )

    events = repository.list_events("job-1", "7", 2)
    assert [event.event_id for event in events] == ["8", "9"]
    assert [event.event_type for event in events] == [
        EventType.JOB_CREATED,
        EventType.CANCEL_REQUESTED,
    ]
    sql, params = connection.calls[0]
    assert "id>%s" in sql and "ORDER BY id LIMIT %s" in sql
    assert params == ("job-1", 7, [item.value for item in EventType], 2)


class Winner:
    def __init__(self, artifact: VisibleArtifact) -> None:
        self.artifact = artifact

    def visible_winner(
        self, job_id: str, namespace_id: Optional[str] = None
    ) -> Sequence[VisibleArtifact]:
        del namespace_id
        return (self.artifact,) if job_id == self.artifact.job_id else ()


class Storage:
    def __init__(self) -> None:
        self.keys: list[str] = []

    def open_reader(self, storage_key: str) -> BinaryIO:
        self.keys.append(storage_key)
        return io.BytesIO(b"artifact")


def test_published_catalog_composes_visible_winner_evidence_with_storage_reader() -> None:
    artifact = VisibleArtifact(
        "artifact-1",
        "publication-1",
        "job-1",
        "attempt-1",
        ArtifactKind.OUTPUT,
        "blob-1",
        hashlib.sha256(b"artifact").hexdigest(),
        8,
        "blobs/sha256/00/object",
        "audio/wav",
    )
    storage = Storage()
    catalog = PublishedArtifactCatalog(Winner(artifact), storage)
    visible = tuple(catalog.list_visible("job-1"))
    with catalog.open_reader(visible[0]) as reader:
        assert reader.read() == b"artifact"
    assert storage.keys == [artifact.storage_key]
    with pytest.raises(NotFoundError):
        catalog.open_reader(replace(artifact, storage_key="private/forged"))


class UploadOnlyRepository:
    def create_job(self, *args: object) -> JobRecord:
        raise AssertionError("not used")

    def get_job(self, job_id: str) -> JobRecord:
        raise AssertionError(job_id)

    def request_cancel(self, job_id: str) -> JobRecord:
        raise AssertionError(job_id)

    def list_events(self, *args: object) -> Sequence[object]:
        raise AssertionError(args)


class EmptyArtifacts:
    def list_visible(self, job_id: str) -> Sequence[VisibleArtifactEvidence]:
        del job_id
        return ()

    def open_reader(self, artifact: VisibleArtifactEvidence) -> BinaryIO:
        raise AssertionError(artifact)


class Policy:
    def has_scope(self, principal: Principal, scope: str) -> bool:
        return principal.subject == "alice" and scope == "assets:write"

    def allows_namespace(self, principal: Principal, namespace_id: str) -> bool:
        return principal.subject == "alice" and namespace_id == "tenant-a"


class Authenticator:
    def authenticate(self, authorization: str) -> Principal:
        if authorization != "Bearer token":
            raise AuthenticationFailed("invalid")
        return Principal("alice")


class ApiTransport:
    def __init__(self, api: ApplicationApi) -> None:
        self.api = api
        self.calls: list[tuple[str, str, Mapping[str, str], Optional[bytes]]] = []

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        self.calls.append((method, path, headers, body))
        response = self.api.handle(ApiRequest(method, path, headers, body or b""))
        return HttpResponse(response.status, response.headers, response.body)


def wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * 80)
    return output.getvalue()


def test_remote_upload_closes_the_verified_asset_workflow_without_sending_a_path(
    tmp_path: Path,
) -> None:
    repository = InMemoryUploadRepository()
    staging = InMemoryS3CompatibleStaging()
    verifier = UploadVerifier(
        repository,
        staging,
        InMemoryS3CompatibleCanonical(),
        WaveMediaProbe(),
    )
    uploads = DurableAssetUploadWorkflow(repository, staging, verifier)
    service = ApplicationService(
        cast(JobApplicationRepository, UploadOnlyRepository()),
        EmptyArtifacts(),
        Policy(),
        uploads,
    )
    transport = ApiTransport(ApplicationApi(service, Authenticator()))
    source = tmp_path / "private-local-name.wav"
    source.write_bytes(wav())

    result = RemoteClient(transport, "token").upload_asset("tenant-a", source)
    assert result["state"] == "verified"
    assert isinstance(result["asset_id"], str)
    initiation = json.loads(cast(bytes, transport.calls[0][3]))
    assert set(initiation) == {"namespace_id", "expected_size_bytes", "expected_sha256"}
    assert source.name not in cast(bytes, transport.calls[1][3]).decode("latin1")

    with pytest.raises(RemoteNotFound):
        RemoteClient(transport, "token").upload_asset("tenant-b", source)
