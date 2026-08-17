# pyright: reportMissingImports=false
from __future__ import annotations

import io
from dataclasses import dataclass, replace
from typing import Mapping, Optional, Sequence

import pytest

from nano_aural_runtime.durable.application import (
    AccessDenied,
    ApplicationService,
    InvalidRequest,
    Principal,
    ResourceNotFound,
    SubmitJob,
    VisibleArtifactEvidence,
)
from nano_aural_runtime.durable.domain import (
    ArtifactKind,
    EventType,
    JobEventRecord,
    JobInput,
    JobRecord,
    JobState,
    canonical_request_sha256,
)
from nano_aural_runtime.durable.errors import NotFoundError


class Repository:
    def __init__(self) -> None:
        self.jobs: dict[str, JobRecord] = {}
        self.events_by_job: dict[str, tuple[JobEventRecord, ...]] = {}
        self.create_calls: list[tuple[object, ...]] = []

    def create_job(
        self,
        namespace_id: str,
        idempotency_key: str,
        request: Mapping[str, object],
        deployment_id: str,
        inputs: Sequence[JobInput],
        required_artifact_kinds: Sequence[ArtifactKind],
    ) -> JobRecord:
        normalized_inputs = tuple(inputs)
        required = tuple(required_artifact_kinds)
        self.create_calls.append(
            (namespace_id, idempotency_key, request, deployment_id, normalized_inputs, required)
        )
        job = JobRecord(
            "job-{0}".format(len(self.jobs) + 1),
            namespace_id,
            idempotency_key,
            canonical_request_sha256(request, deployment_id, normalized_inputs, required),
            request,
            deployment_id,
            normalized_inputs,
            required,
        )
        self.jobs[job.job_id] = job
        self.events_by_job[job.job_id] = (JobEventRecord("1", job.job_id, EventType.JOB_CREATED),)
        return job

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError as error:
            raise NotFoundError("not found") from error

    def request_cancel(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        updated = replace(job, state=JobState.CANCELLED, cancel_requested=True)
        self.jobs[job_id] = updated
        return updated

    def list_events(
        self, job_id: str, after_event_id: Optional[str], limit: int
    ) -> Sequence[JobEventRecord]:
        self.get_job(job_id)
        cursor = int(after_event_id) if after_event_id is not None else 0
        return tuple(
            event for event in self.events_by_job.get(job_id, ()) if int(event.event_id) > cursor
        )[:limit]


class Policy:
    def __init__(self) -> None:
        self.scopes: dict[str, set[str]] = {}
        self.namespaces: dict[str, set[str]] = {}

    def has_scope(self, principal: Principal, scope: str) -> bool:
        return scope in self.scopes.get(principal.subject, set())

    def allows_namespace(self, principal: Principal, namespace_id: str) -> bool:
        return namespace_id in self.namespaces.get(principal.subject, set())


class Artifacts:
    def __init__(self) -> None:
        self.by_job: dict[str, tuple[VisibleArtifactEvidence, ...]] = {}
        self.content: dict[str, bytes] = {}
        self.opens: list[str] = []

    def list_visible(self, job_id: str) -> Sequence[VisibleArtifactEvidence]:
        return self.by_job.get(job_id, ())

    def open_reader(self, artifact: VisibleArtifactEvidence) -> io.BytesIO:
        self.opens.append(artifact.artifact_id)
        return io.BytesIO(self.content[artifact.artifact_id])


@dataclass(frozen=True)
class Visible:
    artifact_id: str
    job_id: str
    attempt_id: str
    kind: ArtifactKind
    sha256: str
    size_bytes: int
    content_type: str
    storage_key: str


def service() -> tuple[ApplicationService, Repository, Policy, Artifacts, Principal, Principal]:
    repository, policy, artifacts = Repository(), Policy(), Artifacts()
    alice, bob = Principal("alice"), Principal("bob")
    all_scopes = {"jobs:submit", "jobs:read", "jobs:cancel", "artifacts:read"}
    policy.scopes = {alice.subject: set(all_scopes), bob.subject: set(all_scopes)}
    policy.namespaces = {alice.subject: {"tenant-a"}, bob.subject: {"tenant-b"}}
    return (
        ApplicationService(repository, artifacts, policy),
        repository,
        policy,
        artifacts,
        alice,
        bob,
    )


def submit(application: ApplicationService, principal: Principal, namespace: str = "tenant-a"):
    return application.submit(
        principal,
        SubmitJob(
            namespace,
            "idem-1",
            "deployment-1",
            {"operation": "generate", "nested": {"seed": 7}},
            (JobInput("audio", "asset-1"),),
        ),
    )


def test_submit_status_cancel_and_events_use_framework_free_protocols() -> None:
    application, repository, _policy, _artifacts, alice, _bob = service()
    created = submit(application, alice)
    assert created.state == JobState.QUEUED
    assert repository.create_calls[0][0:2] == ("tenant-a", "idem-1")

    assert application.get_job(alice, created.job_id) == created
    events = application.events(alice, created.job_id)
    assert [event.event_type for event in events.events] == [EventType.JOB_CREATED]
    cancelled = application.cancel(alice, created.job_id)
    assert cancelled.state == JobState.CANCELLED
    assert cancelled.cancel_requested


def test_events_are_cursor_paginated_and_payload_is_allowlisted() -> None:
    application, repository, _policy, _artifacts, alice, _bob = service()
    created = submit(application, alice)
    repository.events_by_job[created.job_id] = (
        JobEventRecord(
            "1",
            created.job_id,
            EventType.JOB_CREATED,
            payload={"reason": "accepted", "private_path": "/srv/secret"},
        ),
        JobEventRecord("2", created.job_id, EventType.CANCEL_REQUESTED),
    )

    first = application.events(alice, created.job_id, limit=1)
    assert first.next_cursor == "1"
    assert first.events[0].payload == {"reason": "accepted"}
    second = application.events(alice, created.job_id, cursor=first.next_cursor, limit=1)
    assert [event.event_id for event in second.events] == ["2"]
    assert second.next_cursor is None

    repository.events_by_job[created.job_id] = (
        JobEventRecord(
            "3",
            created.job_id,
            EventType.JOB_CREATED,
            payload={"reason": "Bearer secret-token at /srv/private"},
        ),
    )
    redacted = application.events(alice, created.job_id)
    assert redacted.events[0].payload == {}

    for cursor in ("", "01", "-1", "1" * 21):
        with pytest.raises(InvalidRequest):
            application.events(alice, created.job_id, cursor=cursor)
    with pytest.raises(InvalidRequest):
        application.events(alice, created.job_id, limit=101)


def test_scope_denial_is_distinct_but_cross_namespace_and_missing_ids_are_both_404() -> None:
    application, _repository, policy, _artifacts, alice, bob = service()
    created = submit(application, alice)

    policy.scopes[bob.subject].remove("jobs:read")
    with pytest.raises(AccessDenied):
        application.get_job(bob, created.job_id)
    policy.scopes[bob.subject].add("jobs:read")

    with pytest.raises(ResourceNotFound) as cross_tenant:
        application.get_job(bob, created.job_id)
    with pytest.raises(ResourceNotFound) as missing:
        application.get_job(bob, "missing-job")
    assert str(cross_tenant.value) == str(missing.value) == "job not found"
    with pytest.raises(ResourceNotFound):
        application.cancel(bob, created.job_id)
    with pytest.raises(ResourceNotFound):
        application.events(bob, created.job_id)
    with pytest.raises(ResourceNotFound):
        application.artifacts(bob, created.job_id)


def test_submit_hides_foreign_namespace_and_rejects_server_local_fields_recursively() -> None:
    application, repository, _policy, _artifacts, alice, _bob = service()
    with pytest.raises(ResourceNotFound):
        submit(application, alice, "tenant-b")
    with pytest.raises(InvalidRequest):
        application.submit(
            alice,
            SubmitJob(
                "tenant-a",
                "unsafe",
                "deployment-1",
                {"nested": {"weights_path": "/srv/private/weights"}},
            ),
        )
    with pytest.raises(InvalidRequest):
        application.submit(
            alice,
            SubmitJob("tenant-a", "nan", "deployment-1", {"guidance": float("nan")}),
        )
    assert repository.create_calls == []


def test_only_ready_winning_artifacts_are_visible_and_downloadable() -> None:
    application, repository, _policy, artifacts, alice, _bob = service()
    created = submit(application, alice)
    winner = "attempt-winning"
    repository.jobs[created.job_id] = replace(
        repository.jobs[created.job_id],
        state=JobState.SUCCEEDED,
        winning_attempt_id=winner,
    )
    sha = "b" * 64
    legal = Visible(
        "artifact-visible",
        created.job_id,
        winner,
        ArtifactKind.OUTPUT,
        sha,
        7,
        "audio/wav",
        "attempts/winner/output",
    )
    loser = replace(legal, artifact_id="artifact-loser", attempt_id="attempt-loser")
    artifacts.by_job[created.job_id] = (legal, loser)
    artifacts.content[legal.artifact_id] = b"content"

    assert [item.artifact_id for item in application.artifacts(alice, created.job_id)] == [
        legal.artifact_id
    ]
    download = application.download(alice, created.job_id, legal.artifact_id)
    with download.reader:
        assert download.reader.read() == b"content"
    assert download.artifact.sha256 == sha
    with pytest.raises(ResourceNotFound):
        application.download(alice, created.job_id, loser.artifact_id)
    assert artifacts.opens == [legal.artifact_id]


def test_cross_namespace_artifact_probe_is_404_and_never_opens_storage() -> None:
    application, repository, _policy, artifacts, alice, bob = service()
    created = submit(application, alice)
    repository.jobs[created.job_id] = replace(
        repository.jobs[created.job_id],
        state=JobState.SUCCEEDED,
        winning_attempt_id="winner",
    )
    artifact = Visible(
        "artifact-secret",
        created.job_id,
        "winner",
        ArtifactKind.OUTPUT,
        "c" * 64,
        1,
        "audio/wav",
        "attempts/winner/output",
    )
    artifacts.by_job[created.job_id] = (artifact,)
    with pytest.raises(ResourceNotFound):
        application.download(bob, created.job_id, artifact.artifact_id)
    assert artifacts.opens == []
