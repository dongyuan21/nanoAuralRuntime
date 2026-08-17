# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path
from typing import Mapping, Optional, cast

import pytest

from nano_aural_runtime.durable.api import ApiRequest, ApplicationApi
from nano_aural_runtime.durable.application import (
    AccessDenied,
    ArtifactDownload,
    ArtifactView,
    AuthenticationFailed,
    EventPage,
    EventView,
    JobView,
    Principal,
    ResourceNotFound,
    SubmitJob,
)
from nano_aural_runtime.durable.domain import ArtifactKind, EventType, JobState
from nano_aural_runtime_remote.client import HttpResponse, RemoteClient, RemoteNotFound

CONTENT = b"content"
CONTENT_SHA256 = hashlib.sha256(CONTENT).hexdigest()


class Authenticator:
    def authenticate(self, authorization: str) -> Principal:
        if not authorization.startswith("Bearer ") or authorization == "Bearer invalid":
            raise AuthenticationFailed("invalid bearer")
        return Principal(authorization.removeprefix("Bearer "))


class Service:
    def __init__(self) -> None:
        self.submissions: list[tuple[Principal, SubmitJob]] = []

    def submit(self, principal: Principal, command: SubmitJob) -> JobView:
        self.submissions.append((principal, command))
        return JobView("job-1", command.namespace_id, JobState.QUEUED, False, None)

    def get_job(self, principal: Principal, job_id: str) -> JobView:
        if principal.subject == "no-scope":
            raise AccessDenied("missing scope")
        if principal.subject == "foreign" or job_id == "missing":
            raise ResourceNotFound("job not found")
        return JobView(job_id, "tenant-a", JobState.SUCCEEDED, False, "winner")

    def cancel(self, principal: Principal, job_id: str) -> JobView:
        del principal
        return JobView(job_id, "tenant-a", JobState.CANCELLED, True, None)

    def events(
        self,
        principal: Principal,
        job_id: str,
        *,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> EventPage:
        del principal, job_id, cursor, limit
        return EventPage((EventView("1", EventType.JOB_CREATED, None, {}),), None)

    def artifacts(self, principal: Principal, job_id: str):
        del principal, job_id
        return (ArtifactView("artifact-1", ArtifactKind.OUTPUT, CONTENT_SHA256, 7, "audio/wav"),)

    def download(self, principal: Principal, job_id: str, artifact_id: str) -> ArtifactDownload:
        del principal, job_id
        if artifact_id != "artifact-1":
            raise ResourceNotFound("artifact not found")
        return ArtifactDownload(
            ArtifactView("artifact-1", ArtifactKind.OUTPUT, CONTENT_SHA256, 7, "audio/wav"),
            io.BytesIO(CONTENT),
        )


class ApiTransport:
    def __init__(self, api: ApplicationApi) -> None:
        self.api = api

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        response = self.api.handle(ApiRequest(method, path, headers, body or b""))
        return HttpResponse(response.status, response.headers, response.body)


def api() -> tuple[ApplicationApi, Service]:
    service = Service()
    return ApplicationApi(cast(object, service), Authenticator()), service  # type: ignore[arg-type]


def read_json(response) -> Mapping[str, object]:
    with response.body:
        value = json.loads(response.body.read())
    assert isinstance(value, dict)
    return value


def command() -> Mapping[str, object]:
    return {
        "namespace_id": "tenant-a",
        "idempotency_key": "idem-1",
        "deployment_id": "deployment-1",
        "request": {"operation": "generate"},
        "inputs": [],
        "required_artifact_kinds": ["output"],
    }


def test_remote_client_and_application_api_shapes_match_end_to_end(tmp_path: Path) -> None:
    application_api, service = api()
    client = RemoteClient(ApiTransport(application_api), "alice")

    assert client.submit(command())["state"] == "queued"
    assert service.submissions[0][0] == Principal("alice")
    assert client.status("job-1")["state"] == "succeeded"
    assert client.cancel("job-1")["state"] == "cancelled"
    assert client.events("job-1").events == (
        {"attempt_id": None, "event_id": "1", "payload": {}, "type": "job_created"},
    )
    assert client.artifacts("job-1")[0]["artifact_id"] == "artifact-1"

    target = tmp_path / "result.wav"
    assert client.download("job-1", "artifact-1", target) == target
    assert target.read_bytes() == CONTENT


def test_auth_scope_and_idor_map_to_401_403_and_generic_404() -> None:
    application_api, _service = api()
    missing_auth = application_api.handle(ApiRequest("GET", "/v1/jobs/job-1", {}))
    assert missing_auth.status == 401
    assert read_json(missing_auth) == {"error": "authentication failed"}
    assert missing_auth.headers["WWW-Authenticate"].startswith("Bearer ")

    denied = application_api.handle(
        ApiRequest("GET", "/v1/jobs/job-1", {"Authorization": "Bearer no-scope"})
    )
    assert denied.status == 403

    foreign = application_api.handle(
        ApiRequest("GET", "/v1/jobs/job-1", {"Authorization": "Bearer foreign"})
    )
    missing = application_api.handle(
        ApiRequest("GET", "/v1/jobs/missing", {"Authorization": "Bearer alice"})
    )
    assert foreign.status == missing.status == 404
    assert read_json(foreign) == read_json(missing) == {"error": "resource not found"}
    with pytest.raises(RemoteNotFound):
        RemoteClient(ApiTransport(application_api), "foreign").status("job-1")


def test_submit_is_strict_and_artifact_download_headers_are_integrity_evidence() -> None:
    application_api, _service = api()
    invalid = application_api.handle(
        ApiRequest(
            "POST",
            "/v1/jobs",
            {"Authorization": "Bearer alice"},
            b'{"unexpected":true}',
        )
    )
    assert invalid.status == 400

    listing = application_api.handle(
        ApiRequest("GET", "/v1/jobs/job-1/artifacts", {"Authorization": "Bearer alice"})
    )
    assert read_json(listing)["artifacts"] == [
        {
            "artifact_id": "artifact-1",
            "content_type": "audio/wav",
            "kind": "output",
            "sha256": CONTENT_SHA256,
            "size_bytes": 7,
        }
    ]
    download = application_api.handle(
        ApiRequest(
            "GET",
            "/v1/jobs/job-1/artifacts/artifact-1",
            {"Authorization": "Bearer alice"},
        )
    )
    assert download.status == 200
    assert download.headers["X-Content-SHA256"] == CONTENT_SHA256
    assert download.headers["Content-Length"] == "7"
    with download.body:
        assert download.body.read() == CONTENT


def test_submit_requires_matching_canonical_idempotency_header() -> None:
    application_api, service = api()
    body = json.dumps(command()).encode()
    response = application_api.handle(
        ApiRequest(
            "POST",
            "/v1/jobs",
            {
                "Authorization": "Bearer alice",
                "Content-Length": str(len(body)),
                "Idempotency-Key": "different",
            },
            body,
        )
    )
    assert response.status == 400
    assert service.submissions == []
