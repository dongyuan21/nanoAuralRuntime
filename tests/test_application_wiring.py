# pyright: reportMissingImports=false
from __future__ import annotations

import io
from dataclasses import dataclass
from threading import Thread
from typing import BinaryIO, Mapping, Optional, Sequence

import pytest

from nano_aural_runtime.durable.application import (
    AssetUploadWorkflow,
    AuthenticationFailed,
    Principal,
    VisibleArtifactEvidence,
)
from nano_aural_runtime.durable.domain import (
    ArtifactKind,
    JobEventRecord,
    JobInput,
    JobRecord,
    canonical_request_sha256,
)
from nano_aural_runtime.durable.errors import NotFoundError
from nano_aural_runtime.durable.server import WsgiApplication, create_server
from nano_aural_runtime.durable.wiring import (
    StaticAuthorizationPolicy,
    StaticTokenAuthenticator,
    TokenGrant,
    build_application,
)
from nano_aural_runtime_remote.client import RemoteClient, UrllibTransport


class Repository:
    def __init__(self) -> None:
        required = (ArtifactKind.OUTPUT,)
        self.job = JobRecord(
            "job-1",
            "tenant-a",
            "idem-1",
            canonical_request_sha256({}, "deployment-1", (), required),
            {},
            "deployment-1",
            (),
            required,
        )

    def create_job(
        self,
        namespace_id: str,
        idempotency_key: str,
        request: Mapping[str, object],
        deployment_id: str,
        inputs: Sequence[JobInput],
        required_artifact_kinds: Sequence[ArtifactKind],
    ) -> JobRecord:
        del namespace_id, idempotency_key, request, deployment_id, inputs, required_artifact_kinds
        return self.job

    def get_job(self, job_id: str) -> JobRecord:
        if job_id != self.job.job_id:
            raise NotFoundError("not found")
        return self.job

    def request_cancel(self, job_id: str) -> JobRecord:
        return self.get_job(job_id)

    def list_events(
        self, job_id: str, after_event_id: Optional[str], limit: int
    ) -> Sequence[JobEventRecord]:
        del job_id, after_event_id, limit
        return ()


class EmptyCatalog:
    def list_visible(self, job_id: str) -> Sequence[VisibleArtifactEvidence]:
        del job_id
        return ()

    def open_reader(self, artifact: VisibleArtifactEvidence) -> BinaryIO:
        del artifact
        raise AssertionError("no artifacts")


@dataclass
class Dependencies:
    repository: Repository
    artifacts: EmptyCatalog
    authenticator: StaticTokenAuthenticator
    authorization: StaticAuthorizationPolicy
    uploads: Optional[AssetUploadWorkflow] = None


def grant() -> TokenGrant:
    return TokenGrant.from_token(
        "secret-token",
        "alice",
        scopes=("jobs:read",),
        namespaces=("tenant-a",),
    )


def test_concrete_token_auth_policy_and_startable_wsgi_server() -> None:
    configured = grant()
    dependencies = Dependencies(
        Repository(),
        EmptyCatalog(),
        StaticTokenAuthenticator((configured,)),
        StaticAuthorizationPolicy((configured,)),
    )
    server = create_server(build_application(dependencies))
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    try:
        transport = UrllibTransport("http://{0}:{1}".format(host, port), allow_loopback_http=True)
        result = RemoteClient(transport, "secret-token").status("job-1")
        assert result["namespace_id"] == "tenant-a"
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()

    authenticator = dependencies.authenticator
    with pytest.raises(AuthenticationFailed):
        authenticator.authenticate("Bearer wrong")
    authenticated = authenticator.authenticate("Bearer secret-token")
    assert dependencies.authorization.has_scope(authenticated, "jobs:read")
    assert not dependencies.authorization.allows_namespace(authenticated, "tenant-b")


def test_same_subject_tokens_do_not_merge_scopes_or_namespaces() -> None:
    low = TokenGrant.from_token(
        "low-token",
        "same-user",
        scopes=("jobs:read",),
        namespaces=("tenant-a",),
    )
    high = TokenGrant.from_token(
        "high-token",
        "same-user",
        scopes=("jobs:submit",),
        namespaces=("tenant-b",),
    )
    authenticator = StaticTokenAuthenticator((low, high))
    policy = StaticAuthorizationPolicy((low, high))
    low_principal = authenticator.authenticate("Bearer low-token")

    assert policy.has_scope(low_principal, "jobs:read")
    assert policy.allows_namespace(low_principal, "tenant-a")
    assert not policy.has_scope(low_principal, "jobs:submit")
    assert not policy.allows_namespace(low_principal, "tenant-b")
    assert not policy.has_scope(Principal("same-user"), "jobs:read")


class CountingBody(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return super().read(size)


def test_wsgi_rejects_oversized_content_length_before_reading_body() -> None:
    body = CountingBody(b"not-read")
    application = WsgiApplication(
        build_application(
            Dependencies(
                Repository(),
                EmptyCatalog(),
                StaticTokenAuthenticator((grant(),)),
                StaticAuthorizationPolicy((grant(),)),
            )
        ),
        max_body_bytes=4,
    )
    status: list[str] = []

    response = application(
        {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs",
            "CONTENT_LENGTH": "8",
            "wsgi.input": body,
        },
        lambda value, headers: status.append(value),
    )
    assert b"request is too large" in b"".join(response)
    assert status == ["413 Request Entity Too Large"]
    assert body.read_calls == 0


def test_wsgi_rejects_transfer_encoding_before_reading_body() -> None:
    body = CountingBody(b"not-read")
    application = WsgiApplication(
        build_application(
            Dependencies(
                Repository(),
                EmptyCatalog(),
                StaticTokenAuthenticator((grant(),)),
                StaticAuthorizationPolicy((grant(),)),
            )
        )
    )
    status: list[str] = []
    response = application(
        {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs",
            "CONTENT_LENGTH": "8",
            "HTTP_TRANSFER_ENCODING": "chunked",
            "wsgi.input": body,
        },
        lambda value, headers: status.append(value),
    )
    assert b"Transfer-Encoding" in b"".join(response)
    assert status == ["400 Bad Request"]
    assert body.read_calls == 0


def test_wsgi_requires_content_length_before_reading_body() -> None:
    body = CountingBody(b"not-read")
    application = WsgiApplication(
        build_application(
            Dependencies(
                Repository(),
                EmptyCatalog(),
                StaticTokenAuthenticator((grant(),)),
                StaticAuthorizationPolicy((grant(),)),
            )
        )
    )
    status: list[str] = []
    response = application(
        {
            "REQUEST_METHOD": "POST",
            "PATH_INFO": "/v1/jobs",
            "wsgi.input": body,
        },
        lambda value, headers: status.append(value),
    )
    assert b"Content-Length is required" in b"".join(response)
    assert status == ["411 Length Required"]
    assert body.read_calls == 0


def test_wsgi_reports_only_to_injected_observer_and_ignores_sink_failure() -> None:
    calls: list[tuple[str, str, int, float]] = []

    class Observer:
        def record(self, method: str, path: str, status: int, duration_ms: float) -> None:
            calls.append((method, path, status, duration_ms))
            raise RuntimeError("operator sink unavailable")

    clock = iter((1.0, 1.025))
    configured = grant()
    application = WsgiApplication(
        build_application(
            Dependencies(
                Repository(),
                EmptyCatalog(),
                StaticTokenAuthenticator((configured,)),
                StaticAuthorizationPolicy((configured,)),
            )
        ),
        observer=Observer(),
        monotonic=lambda: next(clock),
    )
    status: list[str] = []
    response = application(
        {
            "REQUEST_METHOD": "GET",
            "PATH_INFO": "/v1/jobs/job-1",
            "HTTP_AUTHORIZATION": "Bearer secret-token",
            "wsgi.input": io.BytesIO(),
        },
        lambda value, headers: status.append(value),
    )
    assert b"job-1" in b"".join(response)
    assert status == ["200 OK"]
    assert calls == [("GET", "/v1/jobs/job-1", 200, pytest.approx(25.0))]
