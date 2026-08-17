# pyright: reportMissingImports=false
"""Real PostgreSQL Phase 3E Runtime-to-visible publication tests.

Set ``NANO_AURAL_POSTGRES_BIN`` to a PostgreSQL binary directory.  These tests
start an isolated Unix-socket cluster and never connect to an existing server.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Iterator
from datetime import datetime, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Callable, Optional
from uuid import uuid4

import pytest

from nano_aural_runtime import (
    AdapterRegistry,
    FakeAudioAdapter,
    InvocationCancelledError,
    InvocationResult,
    ModelDeployment,
    ModelInvocation,
    ProducedArtifact,
    Runtime,
)
from nano_aural_runtime.durable.application import ApplicationService
from nano_aural_runtime.durable.application_adapters import (
    PostgresApplicationRepository,
    PublishedArtifactCatalog,
)
from nano_aural_runtime.durable.artifact_publication import (
    ArtifactPublicationService,
    PublicationProgressHook,
    PublicationProgressPoint,
)
from nano_aural_runtime.durable.artifact_storage import LocalAttemptArtifactStore
from nano_aural_runtime.durable.artifact_validation import StreamingMediaArtifactValidator
from nano_aural_runtime.durable.domain import (
    DeploymentRecord,
    JobRecord,
    WorkerRecord,
)
from nano_aural_runtime.durable.errors import StateTransitionError
from nano_aural_runtime.durable.materialization import MaterializedInput
from nano_aural_runtime.durable.migrations import apply_postgres_migrations
from nano_aural_runtime.durable.postgres_repository import PostgresDurableRepository
from nano_aural_runtime.durable.publication import PostgresPublicationRepository
from nano_aural_runtime.durable.publishing_worker import (
    DurablePublishingWorker,
    PublishedRuntimeCandidate,
    SingleOutputArtifactPlanner,
)
from nano_aural_runtime.durable.queue import Lease, PostgresLeaseQueue
from nano_aural_runtime.durable.runtime_worker import DurableRuntimeWorker, RuntimeCandidate
from nano_aural_runtime.durable.storage import LocalBlobStore
from nano_aural_runtime.durable.uploads import WaveMediaProbe
from nano_aural_runtime.durable.wiring import (
    StaticAuthorizationPolicy,
    StaticTokenAuthenticator,
    TokenGrant,
)

POSTGRES_BIN = os.environ.get("NANO_AURAL_POSTGRES_BIN")
if not POSTGRES_BIN:
    pytestmark = pytest.mark.skip(reason="NANO_AURAL_POSTGRES_BIN is not set")
elif any(not (Path(POSTGRES_BIN) / name).is_file() for name in ("initdb", "postgres", "pg_ctl")):
    pytestmark = pytest.mark.skip(reason="required PostgreSQL binaries are missing")
elif find_spec("psycopg") is None:
    pytestmark = pytest.mark.skip(
        reason="psycopg is missing; install the project with .[postgres-test]"
    )


def _wav(frames: int = 400) -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * frames)
    return output.getvalue()


class _PostgresCluster:
    def __init__(self, postgres_bin: Path, psycopg: Any) -> None:
        self.postgres_bin = postgres_bin
        self.psycopg = psycopg
        self.temporary = tempfile.TemporaryDirectory(prefix="p3e-publish-pg-")
        self.data = Path(self.temporary.name) / "data"
        self.socket = Path(self.temporary.name) / "socket"
        self.socket.mkdir()
        self.port = 55439
        self._run(
            "initdb",
            "-D",
            str(self.data),
            "-A",
            "trust",
            "-U",
            "postgres",
            "--no-locale",
            "--encoding=UTF8",
        )
        self._run(
            "pg_ctl",
            "-D",
            str(self.data),
            "-o",
            "-k {0} -p {1} -h ''".format(self.socket, self.port),
            "start",
        )
        for _ in range(40):
            try:
                self.maintenance = self.connect("postgres", autocommit=True)
                break
            except psycopg.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("temporary PostgreSQL cluster did not become ready")

    def _run(self, binary: str, *args: str, check: bool = True) -> None:
        subprocess.run(
            [str(self.postgres_bin / binary), *args],
            check=check,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )

    def dsn(self, database: str) -> str:
        return "dbname={0} user=postgres host={1} port={2}".format(database, self.socket, self.port)

    def connect(self, database: str, *, autocommit: bool = False) -> Any:
        return self.psycopg.connect(self.dsn(database), autocommit=autocommit)

    def close(self) -> None:
        self.maintenance.close()
        self._run("pg_ctl", "-D", str(self.data), "-m", "immediate", "-w", "stop", check=False)
        self.temporary.cleanup()


@pytest.fixture(scope="module")
def postgres_cluster() -> Iterator[_PostgresCluster]:
    if POSTGRES_BIN is None or find_spec("psycopg") is None:
        pytest.skip("PostgreSQL test dependencies unavailable")
    import psycopg

    assert POSTGRES_BIN is not None
    cluster = _PostgresCluster(Path(POSTGRES_BIN), psycopg)
    try:
        yield cluster
    finally:
        cluster.close()


@pytest.fixture
def postgres_database(postgres_cluster: _PostgresCluster) -> Iterator[tuple[str, Any]]:
    database = "p3e_" + uuid4().hex
    postgres_cluster.maintenance.execute("CREATE DATABASE " + database)
    connection = postgres_cluster.connect(database)
    apply_postgres_migrations(connection)
    try:
        yield database, connection
    finally:
        connection.close()
        postgres_cluster.maintenance.execute("DROP DATABASE " + database)


class _ZeroInputBuilder:
    def __init__(self, adapter: FakeAudioAdapter) -> None:
        self._adapter = adapter

    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
        return ModelDeployment(
            deployment.deployment_id,
            self._adapter.descriptor,
            deployment.fingerprint,
        )

    def build(
        self,
        deployment: DeploymentRecord,
        job: JobRecord,
        inputs: tuple[MaterializedInput, ...],
    ) -> ModelInvocation:
        del deployment, job
        assert inputs == ()
        return ModelInvocation("invoke-" + uuid4().hex, "cpu-fake")


class _OneShotSource:
    def __init__(self, candidate: RuntimeCandidate) -> None:
        self._candidate: Optional[RuntimeCandidate] = candidate

    def run_once(self) -> Optional[RuntimeCandidate]:
        candidate, self._candidate = self._candidate, None
        return candidate


class _CountingMonitor:
    def __init__(self, queue: PostgresLeaseQueue, heartbeats: list[int]) -> None:
        self._queue = queue
        self._heartbeats = heartbeats

    def cancellation_requested(self, lease: Lease) -> bool:
        return self._queue.cancellation_requested(lease)

    def heartbeat(self, lease: Lease, lease_seconds: int) -> Lease:
        self._heartbeats.append(1)
        return self._queue.heartbeat(lease, lease_seconds)

    def close(self) -> None:
        self._queue.close()


def _registered_job(connection: Any) -> tuple[DeploymentRecord, WorkerRecord, JobRecord]:
    durable = PostgresDurableRepository(connection)
    deployment = durable.register_deployment(
        DeploymentRecord(str(uuid4()), "P3E CPU fake", "fake", "p3e-cpu-fingerprint")
    )
    worker = durable.register_worker(WorkerRecord(str(uuid4()), deployment.deployment_id))
    job = durable.create_job(
        "tenant-a",
        "publish-" + uuid4().hex,
        {"task": "text-to-audio", "prompt": "CPU fixture"},
        deployment.deployment_id,
        (),
    )
    # ``get_job`` performs read-only evidence loads after the command
    # transaction; close psycopg's implicit read transaction before a monitor
    # on another connection must observe subsequent claim commits.
    connection.commit()
    return deployment, worker, job


def _publication_factory(
    connection: Any,
    queue: PostgresLeaseQueue,
    attempts: LocalAttemptArtifactStore,
    canonical: LocalBlobStore,
    hook_wrapper: Optional[Callable[[PublicationProgressHook], PublicationProgressHook]] = None,
) -> Callable[[PublicationProgressHook], ArtifactPublicationService]:
    repository = PostgresPublicationRepository(connection)

    def build(progress_hook: PublicationProgressHook) -> ArtifactPublicationService:
        actual = hook_wrapper(progress_hook) if hook_wrapper is not None else progress_hook
        return ArtifactPublicationService(
            repository,
            attempts,
            canonical,
            StreamingMediaArtifactValidator(WaveMediaProbe()),
            queue,
            progress_hook=actual,
        )

    return build


def test_real_runtime_claim_publish_success_and_authorized_download(
    postgres_cluster: _PostgresCluster,
    postgres_database: tuple[str, Any],
    tmp_path: Path,
) -> None:
    database, connection = postgres_database
    _deployment, worker_record, job = _registered_job(connection)
    content = _wav(1200)
    adapter = FakeAudioAdapter(
        lambda _session, invocation, _context: InvocationResult(
            invocation.invocation_id,
            (ProducedArtifact("private-backend-name.wav", "audio/wav", content),),
        )
    )
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime_monitors: list[Any] = []
    publication_heartbeats: list[int] = []
    publication_points: list[PublicationProgressPoint] = []
    delayed = False

    def runtime_monitor() -> PostgresLeaseQueue:
        monitor = PostgresLeaseQueue(postgres_cluster.connect(database))
        runtime_monitors.append(monitor)
        return monitor

    def publication_monitor() -> _CountingMonitor:
        queue = PostgresLeaseQueue(postgres_cluster.connect(database))
        return _CountingMonitor(queue, publication_heartbeats)

    def slow_first_write(progress: PublicationProgressHook) -> PublicationProgressHook:
        def observed(
            lease: Lease,
            point: PublicationProgressPoint,
            bytes_processed: int,
        ) -> None:
            nonlocal delayed
            progress(lease, point, bytes_processed)
            publication_points.append(point)
            if point is PublicationProgressPoint.ATTEMPT_WRITE and not delayed:
                delayed = True
                # Longer than the configured lease: only the independent DB
                # heartbeat can keep finalization eligible during this I/O.
                time.sleep(1.2)

        return observed

    workspace = tmp_path / "workspaces"
    canonical = LocalBlobStore(tmp_path / "canonical")
    attempts = LocalAttemptArtifactStore(tmp_path / "attempt-objects")
    queue = PostgresLeaseQueue(connection)
    runtime_worker = DurableRuntimeWorker(
        queue,
        worker_record.worker_id,
        2,
        Runtime(registry),
        _ZeroInputBuilder(adapter),
        canonical,
        workspace,
        WaveMediaProbe(),
        runtime_monitor,
    )
    try:
        publishing = DurablePublishingWorker(
            runtime_worker,
            queue,
            publication_monitor,
            _publication_factory(
                connection,
                queue,
                attempts,
                canonical,
                slow_first_write,
            ),
            SingleOutputArtifactPlanner(4 * 1024 * 1024, chunk_size=67),
            lease_seconds=1,
            heartbeat_interval_seconds=0.01,
        )
        published = publishing.run_once()
        assert isinstance(published, PublishedRuntimeCandidate)
        assert len(published.visible) == 1
        assert dict(published.visible[0].metadata) == {}
        assert "private-backend-name" not in repr(published.visible[0])
        runtime_worker.close()

        grant = TokenGrant.from_token(
            "download-secret",
            "operator",
            scopes=("jobs:read", "artifacts:read"),
            namespaces=("tenant-a",),
        )
        authenticator = StaticTokenAuthenticator((grant,))
        policy = StaticAuthorizationPolicy((grant,))
        principal = authenticator.authenticate("Bearer download-secret")
        service = ApplicationService(
            PostgresApplicationRepository(connection),
            PublishedArtifactCatalog(
                PostgresPublicationRepository(connection),
                canonical,
            ),
            policy,
        )
        artifact = service.artifacts(principal, job.job_id)[0]
        download = service.download(principal, job.job_id, artifact.artifact_id)
        try:
            assert download.reader.read() == content
        finally:
            download.reader.close()
        assert PostgresDurableRepository(connection).get_job(job.job_id).winning_attempt_id
        assert attempts.list_before(datetime.now(timezone.utc)) == ()
        assert workspace.exists() and list(workspace.iterdir()) == []
        assert len(publication_heartbeats) >= 3
        assert {
            PublicationProgressPoint.ATTEMPT_WRITE,
            PublicationProgressPoint.VALIDATION_READ,
            PublicationProgressPoint.BEFORE_PROBE,
            PublicationProgressPoint.AFTER_PROBE,
            PublicationProgressPoint.CANONICAL_PROMOTION,
            PublicationProgressPoint.CANONICAL_VERIFY,
            PublicationProgressPoint.BEFORE_FINALIZE,
        }.issubset(publication_points)
    finally:
        attempts.close()
        canonical.close()


@pytest.mark.parametrize("race", ("cancel", "reaper"))
def test_real_finalize_fence_race_never_exposes_stale_attempt(
    race: str,
    postgres_cluster: _PostgresCluster,
    postgres_database: tuple[str, Any],
    tmp_path: Path,
) -> None:
    database, connection = postgres_database
    _deployment, worker, job = _registered_job(connection)
    queue = PostgresLeaseQueue(connection)
    lease = queue.claim_next(worker.worker_id, 2)
    assert lease is not None
    candidate = RuntimeCandidate(
        lease,
        InvocationResult(
            "race-" + uuid4().hex,
            (ProducedArtifact("result.wav", "audio/wav", _wav(800)),),
        ),
    )
    entered, release = threading.Event(), threading.Event()
    attempt_store = LocalAttemptArtifactStore(tmp_path / "race-attempts")
    canonical = LocalBlobStore(tmp_path / "race-canonical")

    def wrap(progress: PublicationProgressHook) -> PublicationProgressHook:
        def blocked(
            lease: Lease,
            point: PublicationProgressPoint,
            bytes_processed: int,
        ) -> None:
            if point is PublicationProgressPoint.BEFORE_FINALIZE:
                entered.set()
                assert release.wait(5)
            progress(lease, point, bytes_processed)

        return blocked

    def monitor_factory() -> PostgresLeaseQueue:
        return PostgresLeaseQueue(postgres_cluster.connect(database))

    publishing = DurablePublishingWorker(
        _OneShotSource(candidate),
        queue,
        monitor_factory,
        _publication_factory(connection, queue, attempt_store, canonical, wrap),
        SingleOutputArtifactPlanner(4 * 1024 * 1024),
        lease_seconds=2,
        heartbeat_interval_seconds=0.25 if race == "reaper" else 0.01,
    )
    outcome: list[BaseException] = []

    def execute() -> None:
        try:
            publishing.run_once()
        except BaseException as error:
            outcome.append(error)

    thread = threading.Thread(target=execute)
    thread.start()
    try:
        assert entered.wait(5)
        competing = postgres_cluster.connect(database)
        try:
            competing_queue = PostgresLeaseQueue(competing)
            if race == "cancel":
                assert competing_queue.request_cancel(job.job_id)
            else:
                competing.execute(
                    """UPDATE job_attempts SET heartbeat_at=clock_timestamp()-interval '2 seconds',
                       lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s""",
                    (lease.attempt_id,),
                )
                competing.commit()
                assert competing_queue.reap_expired() == 1
        finally:
            competing.close()
        # Give the monitor a chance to observe the authoritative state before
        # the pre-finalize progress fence is released.
        time.sleep(0.35 if race == "reaper" else 0.05)
        release.set()
        thread.join(5)
        assert not thread.is_alive()
        assert outcome and isinstance(outcome[0], (InvocationCancelledError, StateTransitionError))
        assert PostgresPublicationRepository(connection).visible_winner(job.job_id) == ()
        row = connection.execute(
            "SELECT state,winning_attempt_id FROM jobs WHERE id=%s", (job.job_id,)
        ).fetchone()
        assert row[1] is None
        assert row[0] == ("cancelled" if race == "cancel" else "queued")
    finally:
        release.set()
        thread.join(5)
        attempt_store.close()
        canonical.close()
