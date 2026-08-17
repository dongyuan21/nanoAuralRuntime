# pyright: reportMissingImports=false
"""Real PostgreSQL Phase 3A migration and invariant tests.

Set ``NANO_AURAL_POSTGRES_BIN`` to a directory containing ``initdb``,
``postgres`` and ``pg_ctl``. The test launches an isolated Unix-socket cluster;
it never uses a developer's existing database server.
"""

from __future__ import annotations

import io
import os
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from datetime import datetime, timedelta, timezone
from importlib import resources
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Sequence, cast
from uuid import uuid4

import pytest

from nano_aural_runtime import (
    AdapterRegistry,
    FakeAudioAdapter,
    ModelDeployment,
    ModelInvocation,
    Runtime,
)
from nano_aural_runtime.durable.domain import (
    ArtifactKind,
    AssetRecord,
    BlobRecord,
    BlobState,
    DeploymentRecord,
    DeploymentState,
    EventType,
    JobInput,
    JobRecord,
    JobState,
    WorkerRecord,
)
from nano_aural_runtime.durable.errors import (
    DurableInvariantError,
    IdempotencyConflictError,
    StateTransitionError,
)
from nano_aural_runtime.durable.materialization import (
    AttemptInputMaterializer,
    LeaseAuthority,
    MaterializedInput,
)
from nano_aural_runtime.durable.migrations import apply_postgres_migrations
from nano_aural_runtime.durable.postgres_repository import PostgresDurableRepository
from nano_aural_runtime.durable.postgres_uploads import PostgresUploadRepository
from nano_aural_runtime.durable.publication import (
    PostgresPublicationRepository,
    PublicationSpec,
    PublicationState,
)
from nano_aural_runtime.durable.queue import PostgresLeaseQueue
from nano_aural_runtime.durable.queue_worker import FakeLeaseWorker
from nano_aural_runtime.durable.runtime_worker import DurableRuntimeWorker
from nano_aural_runtime.durable.storage import LocalBlobStore
from nano_aural_runtime.durable.uploads import (
    UploadMode,
    UploadSession,
    UploadState,
    WaveMediaProbe,
)

POSTGRES_BIN = os.environ.get("NANO_AURAL_POSTGRES_BIN")
PSYCOPG_AVAILABLE = False
if not POSTGRES_BIN:
    pytestmark = pytest.mark.skip(reason="NANO_AURAL_POSTGRES_BIN is not set")
else:
    REQUIRED_BINARIES = ("initdb", "postgres", "pg_ctl")
    missing = [name for name in REQUIRED_BINARIES if not (Path(POSTGRES_BIN) / name).is_file()]
    if missing:
        pytestmark = pytest.mark.skip(
            reason="PostgreSQL binaries are missing: {0}".format(", ".join(missing))
        )
    else:
        if find_spec("psycopg") is None:
            pytestmark = pytest.mark.skip(
                reason="psycopg is missing; install the project with .[postgres-test]"
            )
        else:
            PSYCOPG_AVAILABLE = True


@unittest.skipUnless(
    POSTGRES_BIN and not globals().get("missing", []) and PSYCOPG_AVAILABLE,
    "PostgreSQL binaries or psycopg unavailable",
)
class PostgresMigrationTests(unittest.TestCase):
    postgres_bin: Path
    psycopg: Any
    maintenance: Any

    @classmethod
    def setUpClass(cls) -> None:
        if POSTGRES_BIN is None:
            raise RuntimeError("NANO_AURAL_POSTGRES_BIN is required")
        import psycopg

        cls.psycopg = psycopg
        cls.postgres_bin = Path(POSTGRES_BIN)
        cls.tempdir = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.tempdir.name) / "data"
        cls.socket_dir = Path(cls.tempdir.name) / "socket"
        cls.socket_dir.mkdir()
        # A Unix-socket-only cluster does not need an application TCP listener.
        # The sandbox disallows binding probe sockets, so use this isolated test
        # port together with ``listen_addresses=''`` below.
        cls.port = 55432
        cls._run(
            "initdb",
            "-D",
            str(cls.data_dir),
            "-A",
            "trust",
            "-U",
            "postgres",
            "--no-locale",
            "--encoding=UTF8",
        )
        cls._run(
            "pg_ctl",
            "-D",
            str(cls.data_dir),
            "-o",
            "-k {0} -p {1} -h ''".format(cls.socket_dir, cls.port),
            "start",
        )
        # Do not capture pg_ctl's descriptors: its child postgres process keeps
        # inherited descriptors open, which makes subprocess.run wait forever.
        # Instead, start it and prove readiness through the socket ourselves.
        for _ in range(40):
            try:
                cls.maintenance = psycopg.connect(
                    cls._dsn("postgres"), autocommit=True, connect_timeout=1
                )
                break
            except psycopg.OperationalError:
                time.sleep(0.25)
        else:
            raise RuntimeError("temporary PostgreSQL cluster did not become ready")

    @classmethod
    def tearDownClass(cls) -> None:
        if hasattr(cls, "maintenance"):
            cls.maintenance.close()
        if hasattr(cls, "data_dir"):
            cls._run(
                "pg_ctl", "-D", str(cls.data_dir), "-m", "immediate", "-w", "stop", check=False
            )
        if hasattr(cls, "tempdir"):
            cls.tempdir.cleanup()

    @classmethod
    def _run(cls, binary: str, *args: str, check: bool = True) -> None:
        subprocess.run(
            [str(cls.postgres_bin / binary), *args],
            check=check,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )

    @classmethod
    def _dsn(cls, database: str) -> str:
        return "dbname={0} user=postgres host={1} port={2}".format(
            database, cls.socket_dir, cls.port
        )

    def setUp(self) -> None:
        self.database = "p3a_" + uuid4().hex
        self.maintenance.execute("CREATE DATABASE " + self.database)
        self.connection = self.psycopg.connect(self._dsn(self.database))
        apply_postgres_migrations(self.connection)

    def tearDown(self) -> None:
        self.connection.close()
        self.maintenance.execute("DROP DATABASE " + self.database)

    def _id(self) -> str:
        return str(uuid4())

    def _deployment(self) -> str:
        identifier = self._id()
        self.connection.execute(
            "INSERT INTO model_deployments (id, name, adapter_id, fingerprint, state) VALUES (%s,%s,'fake',%s,'ready')",
            (identifier, "deployment-" + identifier, "fingerprint-" + identifier),
        )
        self.connection.commit()
        return identifier

    def _job(self, deployment_id: str, required: tuple[str, ...] = ("output",)) -> str:
        identifier = self._id()
        self.connection.execute(
            """INSERT INTO jobs (id, namespace_id, idempotency_key, request_sha256, request_json,
               model_deployment_id, required_artifact_kinds) VALUES (%s,'ns',%s,%s,'{}',%s,%s)""",
            (identifier, "key-" + identifier, "a" * 64, deployment_id, list(required)),
        )
        self.connection.commit()
        return identifier

    def _worker(self, deployment_id: str) -> str:
        identifier = self._id()
        self.connection.execute(
            "INSERT INTO workers (id, model_deployment_id) VALUES (%s,%s)",
            (identifier, deployment_id),
        )
        self.connection.commit()
        return identifier

    def _validated_output(self, connection: Any, lease: Any, digest: str = "8" * 64) -> Any:
        repository = PostgresPublicationRepository(connection)
        reserved = repository.reserve(
            lease, PublicationSpec(ArtifactKind.OUTPUT, "audio/wav", 4, digest, 4)
        )
        written = repository.record_object(lease, reserved.publication_id, 0, digest, 4)
        return repository.record_validated(
            lease,
            written.publication_id,
            written.version,
            BlobRecord(
                self._id(),
                digest,
                4,
                "blobs/sha256/{0}/{1}/{2}".format(digest[:2], digest[2:4], digest),
                BlobState.VERIFIED,
                "audio/wav",
            ),
            {"probe": "ok"},
        )

    def _attempt(self, job_id: str, worker_id: str, state: str = "active", epoch: int = 1) -> str:
        identifier = self._id()
        with self.connection.transaction():
            self.connection.execute(
                """INSERT INTO job_attempts
                   (id,job_id,worker_id,attempt_no,lease_epoch,state,finished_at,heartbeat_at,lease_expires_at)
                   VALUES (%s,%s,%s,1,%s,%s,CASE WHEN %s='active' THEN NULL ELSE clock_timestamp() END,
                     CASE WHEN %s='active' THEN clock_timestamp() ELSE NULL END,
                     CASE WHEN %s='active' THEN clock_timestamp()+interval '60 seconds' ELSE NULL END)""",
                (identifier, job_id, worker_id, epoch, state, state, state, state),
            )
            if state == "active":
                self.connection.execute(
                    "UPDATE jobs SET state='running',current_attempt_id=%s,lease_epoch=%s WHERE id=%s",
                    (identifier, epoch, job_id),
                )
        return identifier

    def _blob(self) -> str:
        identifier = self._id()
        digest = uuid4().hex + uuid4().hex
        self.connection.execute(
            "INSERT INTO blobs (id, sha256, size_bytes, storage_key, content_type, state) VALUES (%s,%s,1,%s,'application/octet-stream','verified')",
            (
                identifier,
                digest,
                "blobs/sha256/{0}/{1}/{2}".format(digest[:2], digest[2:4], digest),
            ),
        )
        self.connection.commit()
        return identifier

    def _verified_asset(self, blob_id: str) -> str:
        identifier = self._id()
        self.connection.execute(
            "INSERT INTO assets (id, namespace_id, blob_id, kind, state) VALUES (%s,'ns',%s,'input','verified')",
            (identifier, blob_id),
        )
        self.connection.commit()
        return identifier

    def test_runner_is_ledgered_and_repeat_is_noop(self) -> None:
        apply_postgres_migrations(self.connection)
        count = self.connection.execute(
            "SELECT count(*) FROM nano_aural_schema_migrations"
        ).fetchone()[0]
        self.assertEqual(5, count)

    def test_development_migration_mirror_matches_packaged_resource(self) -> None:
        for filename in (
            "0001_durable_foundation.sql",
            "0002_verified_uploads.sql",
            "0003_queue_leases.sql",
            "0004_artifact_publications.sql",
            "0005_upload_staging_cleanups.sql",
        ):
            root_migration = Path(__file__).resolve().parents[1] / "migrations" / filename
            packaged = resources.files("nano_aural_runtime.durable").joinpath("sql/" + filename)
            self.assertEqual(root_migration.read_bytes(), packaged.read_bytes())

    def test_queue_two_connections_claim_once_and_fence_old_epoch(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        first_worker = self._worker(deployment)
        second_worker = self._worker(deployment)
        first = PostgresLeaseQueue(self.connection)
        second_connection = self.psycopg.connect(self._dsn(self.database))
        try:
            second = PostgresLeaseQueue(second_connection)
            lease = first.claim_next(first_worker, 30)
            self.assertIsNotNone(lease)
            self.assertIsNone(second.claim_next(second_worker, 30))
            assert lease is not None
            # DB clock expiry is authoritative; a reaper gives the retry a new epoch.
            self.connection.execute(
                """UPDATE job_attempts SET heartbeat_at=clock_timestamp()-interval '2 seconds',
                   lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s""",
                (lease.attempt_id,),
            )
            self.connection.commit()
            self.assertEqual(1, second.reap_expired(retry_delay_seconds=1))
            self.connection.execute("UPDATE jobs SET retry_not_before=NULL WHERE id=%s", (job,))
            self.connection.commit()
            replacement = second.claim_next(second_worker, 30)
            self.assertIsNotNone(replacement)
            assert replacement is not None
            self.assertGreater(replacement.lease_epoch, lease.lease_epoch)
            with self.assertRaises(StateTransitionError):
                first.heartbeat(lease, 30)
        finally:
            second_connection.close()

    def test_queue_cancel_fences_active_attempt_without_success(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        queue = PostgresLeaseQueue(self.connection)
        lease = queue.claim_next(worker, 30)
        assert lease is not None
        self.assertTrue(queue.request_cancel(job))
        with self.assertRaises(StateTransitionError):
            queue.heartbeat(lease, 30)
        queue.cancel_current(lease)
        row = self.connection.execute(
            "SELECT state,current_attempt_id,winning_attempt_id FROM jobs WHERE id=%s", (job,)
        ).fetchone()
        self.assertEqual(("cancelled", None, None), tuple(row))

    def test_zero_input_job_is_idempotent_claimable_and_materializes_empty(self) -> None:
        repository = PostgresDurableRepository(self.connection)
        deployment = repository.register_deployment(
            DeploymentRecord(
                self._id(), "zero-" + self._id(), "fake", "zero-fingerprint-" + self._id()
            )
        )
        worker = repository.register_worker(WorkerRecord(self._id(), deployment.deployment_id))
        job = repository.create_job(
            "zero-ns", "zero-key", {"prompt": "one"}, deployment.deployment_id, ()
        )
        same = repository.create_job(
            "zero-ns", "zero-key", {"prompt": "one"}, deployment.deployment_id, ()
        )
        self.assertEqual(job.job_id, same.job_id)
        with self.assertRaises(IdempotencyConflictError):
            repository.create_job(
                "zero-ns", "zero-key", {"prompt": "two"}, deployment.deployment_id, ()
            )
        queue = PostgresLeaseQueue(self.connection)
        lease = queue.claim_next(worker.worker_id, 30)
        assert lease is not None
        loaded_job, attempt, evidence = queue.load_verified_input_evidence(lease)
        self.assertEqual((), loaded_job.inputs)
        self.assertEqual((), evidence)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalBlobStore(Path(directory) / "canonical")
            with AttemptInputMaterializer(
                loaded_job,
                attempt,
                lease,
                evidence,
                store,
                cast(LeaseAuthority, queue),
                Path(directory) / "attempts",
                WaveMediaProbe(),
            ) as materializer:
                self.assertEqual((), materializer.inputs)
            self.assertEqual([], list((Path(directory) / "attempts").iterdir()))

    def test_zero_input_runtime_worker_real_postgres_keeps_active_candidate(self) -> None:
        repository = PostgresDurableRepository(self.connection)
        adapter = FakeAudioAdapter()
        deployment = repository.register_deployment(
            DeploymentRecord(self._id(), "runtime-zero", "fake", "runtime-zero-fingerprint")
        )
        worker = repository.register_worker(WorkerRecord(self._id(), deployment.deployment_id))
        job = repository.create_job(
            "runtime-zero-ns", "runtime-zero-key", {"prompt": "text"}, deployment.deployment_id, ()
        )
        registry = AdapterRegistry()
        registry.register(adapter)

        class Builder:
            def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
                return ModelDeployment(
                    deployment.deployment_id, adapter.descriptor, deployment.fingerprint
                )

            def build(
                self,
                deployment: DeploymentRecord,
                job: JobRecord,
                inputs: tuple[MaterializedInput, ...],
            ) -> ModelInvocation:
                del deployment, job
                self_test.assertEqual((), inputs)
                return ModelInvocation("pg-runtime-zero", "fake")

        self_test = self
        monitor_connections: list[Any] = []

        def monitor_factory() -> PostgresLeaseQueue:
            connection = self.psycopg.connect(self._dsn(self.database))
            monitor_connections.append(connection)
            return PostgresLeaseQueue(connection)

        with tempfile.TemporaryDirectory() as directory:
            store = LocalBlobStore(Path(directory) / "canonical")
            runtime_worker = DurableRuntimeWorker(
                PostgresLeaseQueue(self.connection),
                worker.worker_id,
                30,
                Runtime(registry),
                Builder(),
                store,
                Path(directory) / "attempts",
                WaveMediaProbe(),
                monitor_factory,
            )
            candidate = runtime_worker.run_once()
            self.assertIsNotNone(candidate)
            runtime_worker.close()
            self.assertEqual([], list((Path(directory) / "attempts").iterdir()))
        self.assertTrue(monitor_connections)
        self.assertTrue(all(connection.closed for connection in monitor_connections))
        row = self.connection.execute(
            "SELECT state,current_attempt_id,winning_attempt_id FROM jobs WHERE id=%s",
            (job.job_id,),
        ).fetchone()
        self.assertEqual("running", row[0])
        self.assertIsNotNone(row[1])
        self.assertIsNone(row[2])
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM artifacts WHERE job_id=%s", (job.job_id,)
            ).fetchone()[0],
        )

    def test_queue_claim_skips_concurrently_locked_candidate_job(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        first_worker, second_worker = self._worker(deployment), self._worker(deployment)
        locker = self.psycopg.connect(self._dsn(self.database))
        claimant = self.psycopg.connect(self._dsn(self.database))
        try:
            locker.execute("SELECT id FROM jobs WHERE id=%s FOR UPDATE", (job,))
            self.assertIsNone(PostgresLeaseQueue(claimant).claim_next(second_worker, 30))
            self.assertEqual(
                "ready",
                claimant.execute(
                    "SELECT state FROM workers WHERE id=%s", (second_worker,)
                ).fetchone()[0],
            )
            locker.rollback()
            self.assertIsNotNone(PostgresLeaseQueue(claimant).claim_next(first_worker, 30))
        finally:
            locker.close()
            claimant.close()

    def test_reaper_skips_concurrently_locked_worker_then_recovers(self) -> None:
        deployment = self._deployment()
        self._job(deployment)
        worker = self._worker(deployment)
        owner = PostgresLeaseQueue(self.connection)
        lease = owner.claim_next(worker, 30)
        assert lease is not None
        self.connection.execute(
            """UPDATE job_attempts SET heartbeat_at=clock_timestamp()-interval '2 seconds',
               lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s""",
            (lease.attempt_id,),
        )
        self.connection.commit()
        locker = self.psycopg.connect(self._dsn(self.database))
        reaper_connection = self.psycopg.connect(self._dsn(self.database))
        try:
            locker.execute("SELECT id FROM workers WHERE id=%s FOR UPDATE", (worker,))
            self.assertEqual(0, PostgresLeaseQueue(reaper_connection).reap_expired())
            locker.rollback()
            self.assertEqual(1, PostgresLeaseQueue(reaper_connection).reap_expired())
        finally:
            locker.close()
            reaper_connection.close()

    def test_materialized_queue_worker_keeps_candidate_in_memory(self) -> None:
        content = io.BytesIO()
        with wave.open(content, "wb") as output:
            output.setnchannels(1)
            output.setsampwidth(2)
            output.setframerate(8000)
            output.writeframes(b"\x00\x00" * 80)
        with tempfile.TemporaryDirectory() as directory:
            store = LocalBlobStore(Path(directory) / "canonical")
            stored = store.put_immutable(content.getvalue())
            repository = PostgresDurableRepository(self.connection)
            deployment = repository.register_deployment(
                DeploymentRecord(
                    self._id(),
                    "mat-" + self._id(),
                    "fake",
                    "mat-" + self._id(),
                    DeploymentState.READY,
                )
            )
            blob = repository.register_blob(
                BlobRecord(self._id(), stored.sha256, stored.size_bytes, stored.storage_key)
            )
            media = WaveMediaProbe().probe(io.BytesIO(content.getvalue()))
            asset = repository.create_asset(
                AssetRecord(
                    self._id(),
                    blob.blob_id,
                    "mat-ns",
                    metadata={
                        "media_type": media.media_type,
                        "duration_seconds": media.duration_seconds,
                    },
                )
            )
            job = repository.create_job(
                "mat-ns",
                "mat-key",
                {"task": "materialize"},
                deployment.deployment_id,
                (JobInput("audio", asset.asset_id),),
            )
            worker = repository.register_worker(WorkerRecord(self._id(), deployment.deployment_id))
            queue = PostgresLeaseQueue(self.connection)
            fake = FakeLeaseWorker(queue, worker.worker_id, 30, lambda lease: b"unused")
            result = fake.run_materialized_once(
                store,
                Path(directory) / "attempts",
                lambda lease, inputs: Path(inputs[0].path).read_bytes(),
                WaveMediaProbe(),
            )
            self.assertIsNotNone(result)
            assert result is not None
            self.assertEqual(content.getvalue(), result.value)
            self.assertEqual([], list((Path(directory) / "attempts").iterdir()))
            state = self.connection.execute(
                "SELECT state,current_attempt_id,winning_attempt_id FROM jobs WHERE id=%s",
                (job.job_id,),
            ).fetchone()
            self.assertEqual("running", state[0])
            self.assertEqual(result.lease.attempt_id, str(state[1]))
            self.assertIsNone(state[2])
            self.assertEqual(
                0,
                self.connection.execute(
                    "SELECT count(*) FROM artifacts WHERE job_id=%s", (job.job_id,)
                ).fetchone()[0],
            )

    def test_postgres_repository_fake_worker_and_idempotency(self) -> None:
        repository = PostgresDurableRepository(self.connection)
        deployment = repository.register_deployment(
            DeploymentRecord(
                self._id(),
                "repo-deployment-" + self._id(),
                "fake",
                "repo-fingerprint-" + self._id(),
                DeploymentState.READY,
            )
        )
        worker = repository.register_worker(WorkerRecord(self._id(), deployment.deployment_id))
        quarantined = repository.register_blob(
            BlobRecord(
                self._id(),
                "c" * 64,
                0,
                "blobs/sha256/cc/cc/" + "c" * 64,
                BlobState.QUARANTINED,
            )
        )
        with self.assertRaises(DurableInvariantError):
            repository.create_asset(AssetRecord(self._id(), quarantined.blob_id, "repo-ns"))
        with tempfile.TemporaryDirectory() as directory:
            store = LocalBlobStore(Path(directory))
            stored = store.put_immutable(b"postgres-repository-input")
            blob = repository.register_blob(
                BlobRecord(self._id(), stored.sha256, stored.size_bytes, stored.storage_key)
            )
            asset = repository.create_asset(AssetRecord(self._id(), blob.blob_id, "repo-ns"))
            for invalid_kinds in (
                (ArtifactKind.OUTPUT, ArtifactKind.OUTPUT),
                cast(Sequence[ArtifactKind], ("output",)),
            ):
                with self.assertRaises(ValueError):
                    repository.create_job(
                        "repo-ns",
                        "invalid-" + str(len(invalid_kinds)),
                        {"prompt": "a"},
                        deployment.deployment_id,
                        (JobInput("input", asset.asset_id),),
                        invalid_kinds,
                    )
            for namespace_id, idempotency_key in ((" ", "space-namespace"), ("repo-ns", " ")):
                with self.assertRaises(ValueError):
                    repository.create_job(
                        namespace_id,
                        idempotency_key,
                        {"prompt": "a"},
                        deployment.deployment_id,
                        (JobInput("input", asset.asset_id),),
                    )
            self.assertEqual(
                0,
                self.connection.execute(
                    "SELECT count(*) FROM jobs WHERE namespace_id='repo-ns'"
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                self.connection.execute("SELECT count(*) FROM job_events").fetchone()[0],
            )
            job = repository.create_job(
                "repo-ns",
                "same-key",
                {"prompt": "a"},
                deployment.deployment_id,
                (JobInput("input", asset.asset_id),),
            )
            same = repository.create_job(
                "repo-ns",
                "same-key",
                {"prompt": "a"},
                deployment.deployment_id,
                (JobInput("input", asset.asset_id),),
            )
            self.assertEqual(job.job_id, same.job_id)
            with self.assertRaises(IdempotencyConflictError):
                repository.create_job(
                    "repo-ns",
                    "same-key",
                    {"prompt": "different"},
                    deployment.deployment_id,
                    (JobInput("input", asset.asset_id),),
                )
            # Once 0003 is applied the former P3A execution API is deliberately
            # sealed: only the fenced queue may create/finish attempts.
            with self.assertRaises(StateTransitionError):
                repository.start_attempt(job.job_id, worker.worker_id)
            with self.assertRaises(StateTransitionError):
                repository._finish_non_success(  # type: ignore[attr-defined]
                    job.job_id,
                    "missing",
                    "failed_terminal",
                    JobState.FAILED,
                    EventType.ATTEMPT_FAILED,
                )

    def test_postgres_upload_session_cas_and_verified_asset(self) -> None:
        repository = PostgresUploadRepository(self.connection)
        session_id = self._id()
        session = repository.create_session(
            UploadSession(
                session_id,
                "upload-ns",
                UploadMode.MULTIPART,
                3,
                "staging/" + session_id,
                datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        uploaded = repository.mark_uploaded(session.session_id, session.version)
        verifying = repository.claim_verification(uploaded.session_id, uploaded.version)
        blob = BlobRecord(
            self._id(), "d" * 64, 3, "blobs/sha256/dd/dd/" + "d" * 64, BlobState.VERIFIED
        )
        asset = AssetRecord(self._id(), blob.blob_id, "upload-ns")
        verified = repository.finalize_verified(
            verifying.session_id, verifying.version, blob, asset
        )
        self.assertEqual(UploadState.VERIFIED, verified.state)
        with self.assertRaises(StateTransitionError):
            repository.reject(verifying.session_id, verifying.version, "late")

    def test_upload_expiry_shape_guards(self) -> None:
        repository = PostgresUploadRepository(self.connection)
        session_id = self._id()
        expired = repository.create_session(
            UploadSession(
                session_id,
                "expiry-ns",
                UploadMode.SINGLE,
                1,
                "staging/" + session_id,
                datetime.now(timezone.utc) + timedelta(milliseconds=10),
            )
        )
        self.connection.execute("SELECT pg_sleep(0.05)")
        with self.assertRaises(StateTransitionError):
            repository.mark_uploaded(expired.session_id, expired.version)

    def test_upload_raw_shape_constraints_reject_partial_terminal_facts(self) -> None:
        blob_id = self._blob()
        session_id = self._id()
        base = (
            session_id,
            "shape-ns",
            "staging/" + session_id,
            datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        statements = (
            (
                """INSERT INTO upload_sessions
                   (id,namespace_id,mode,expected_size_bytes,staging_key,expires_at,verified_blob_id)
                   VALUES (%s,%s,'single',1,%s,%s,%s)""",
                (*base, blob_id),
            ),
            (
                """INSERT INTO upload_sessions
                   (id,namespace_id,mode,expected_size_bytes,staging_key,expires_at,finalized_at)
                   VALUES (%s,%s,'single',1,%s,%s,clock_timestamp())""",
                base,
            ),
            (
                """INSERT INTO upload_sessions
                   (id,namespace_id,mode,expected_size_bytes,staging_key,expires_at,rejection_reason)
                   VALUES (%s,%s,'single',1,%s,%s,'not-rejected')""",
                base,
            ),
            (
                """INSERT INTO upload_sessions
                   (id,namespace_id,mode,expected_size_bytes,staging_key,expires_at,verification_started_at)
                   VALUES (%s,%s,'single',1,%s,%s,clock_timestamp())""",
                base,
            ),
        )
        for statement, parameters in statements:
            with self.assertRaises(self.psycopg.errors.CheckViolation):
                self.connection.execute(statement, parameters)
            self.connection.rollback()

    def test_upload_claim_cas_rejects_second_connection(self) -> None:
        repository = PostgresUploadRepository(self.connection)
        session_id = self._id()
        session = repository.create_session(
            UploadSession(
                session_id,
                "cas-ns",
                UploadMode.SINGLE,
                1,
                "staging/" + session_id,
                datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        uploaded = repository.mark_uploaded(session_id, session.version)
        first = repository.claim_verification(session_id, uploaded.version)
        # ``get_session`` is deliberately a read on the caller's connection;
        # commit it before a second connection performs the competing CAS.
        self.connection.commit()
        second = PostgresUploadRepository(self.psycopg.connect(self._dsn(self.database)))
        try:
            with self.assertRaises(StateTransitionError):
                second.claim_verification(session_id, uploaded.version)
        finally:
            second._connection.close()
        self.assertEqual(UploadState.VERIFYING, first.state)

    def test_upload_raw_verifying_requires_reclaimable_timestamp(self) -> None:
        repository = PostgresUploadRepository(self.connection)
        session_id = self._id()
        created = repository.create_session(
            UploadSession(
                session_id,
                "reclaim-ns",
                UploadMode.SINGLE,
                1,
                "staging/" + session_id,
                datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        uploaded = repository.mark_uploaded(session_id, created.version)
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.connection.execute(
                "UPDATE upload_sessions SET state='verifying',version=version+1 WHERE id=%s",
                (session_id,),
            )
        self.connection.rollback()
        verifying = repository.claim_verification(session_id, uploaded.version)
        reclaimed = repository.reclaim_verification(
            session_id,
            verifying.version,
            datetime.now(timezone.utc) + timedelta(minutes=1),
        )
        self.assertEqual(UploadState.VERIFYING, reclaimed.state)
        self.assertEqual(verifying.version + 1, reclaimed.version)

    def test_upload_two_sessions_same_digest_deduplicates_across_connections(self) -> None:
        first_repository = PostgresUploadRepository(self.connection)
        second_connection = self.psycopg.connect(self._dsn(self.database))
        second_repository = PostgresUploadRepository(second_connection)
        digest = "e" * 64
        storage_key = "blobs/sha256/ee/ee/" + digest
        try:
            sessions = []
            for repository in (first_repository, second_repository):
                session_id = self._id()
                created = repository.create_session(
                    UploadSession(
                        session_id,
                        "shared-upload-ns",
                        UploadMode.SINGLE,
                        3,
                        "staging/" + session_id,
                        datetime.now(timezone.utc) + timedelta(minutes=1),
                    )
                )
                uploaded = repository.mark_uploaded(created.session_id, created.version)
                sessions.append(
                    repository.claim_verification(uploaded.session_id, uploaded.version)
                )
            for repository, session, suffix in (
                (first_repository, sessions[0], "one"),
                (second_repository, sessions[1], "two"),
            ):
                blob = BlobRecord(
                    self._id(), digest, 3, storage_key, BlobState.VERIFIED, "audio/wav"
                )
                asset = AssetRecord(
                    self._id(), blob.blob_id, "shared-upload-ns", metadata={"source": suffix}
                )
                verified = repository.finalize_verified(
                    session.session_id, session.version, blob, asset
                )
                self.assertEqual(UploadState.VERIFIED, verified.state)
                if repository is first_repository:
                    self.connection.commit()
                else:
                    second_connection.commit()
            self.assertEqual(
                1,
                self.connection.execute(
                    "SELECT count(*) FROM blobs WHERE sha256=%s", (digest,)
                ).fetchone()[0],
            )
            self.assertEqual(
                2,
                self.connection.execute(
                    "SELECT count(*) FROM assets WHERE namespace_id='shared-upload-ns'"
                ).fetchone()[0],
            )
        finally:
            second_connection.close()

    def test_upload_expiry_janitor_cannot_reclaim_or_advance_terminal_session(self) -> None:
        repository = PostgresUploadRepository(self.connection)
        session_id = self._id()
        created = repository.create_session(
            UploadSession(
                session_id,
                "janitor-ns",
                UploadMode.SINGLE,
                1,
                "staging/" + session_id,
                datetime.now(timezone.utc) + timedelta(milliseconds=20),
            )
        )
        uploaded = repository.mark_uploaded(created.session_id, created.version)
        verifying = repository.claim_verification(uploaded.session_id, uploaded.version)
        self.connection.execute("SELECT pg_sleep(0.05)")
        expired = repository.expire_before(datetime.now(timezone.utc) - timedelta(days=1))
        self.assertEqual([session_id], [item.session_id for item in expired])
        terminal = repository.get_session(session_id)
        self.assertEqual(UploadState.EXPIRED, terminal.state)
        self.assertEqual(terminal.version, verifying.version + 1)
        with self.assertRaises(StateTransitionError):
            repository.reclaim_verification(
                session_id, verifying.version, datetime.now(timezone.utc) + timedelta(minutes=1)
            )
        with self.assertRaises(StateTransitionError):
            repository.reject(session_id, terminal.version, "late")
        self.assertIn(
            session_id,
            [item.session_id for item in repository.terminal_staging_candidates()],
        )

    def test_upload_verified_asset_can_be_submitted_only_in_its_namespace(self) -> None:
        uploads = PostgresUploadRepository(self.connection)
        session_id = self._id()
        created = uploads.create_session(
            UploadSession(
                session_id,
                "job-upload-ns",
                UploadMode.SINGLE,
                3,
                "staging/" + session_id,
                datetime.now(timezone.utc) + timedelta(minutes=1),
            )
        )
        uploaded = uploads.mark_uploaded(session_id, created.version)
        verifying = uploads.claim_verification(session_id, uploaded.version)
        digest = "f" * 64
        blob = BlobRecord(
            self._id(), digest, 3, "blobs/sha256/ff/ff/" + digest, BlobState.VERIFIED, "audio/wav"
        )
        asset = AssetRecord(self._id(), blob.blob_id, "job-upload-ns")
        uploads.finalize_verified(session_id, verifying.version, blob, asset)
        durable = PostgresDurableRepository(self.connection)
        deployment_id = self._id()
        durable.register_deployment(
            DeploymentRecord(deployment_id, "upload-deployment", "fake", "upload-fingerprint")
        )
        job = durable.create_job(
            "job-upload-ns",
            "upload-job-key",
            {"prompt": "upload"},
            deployment_id,
            (JobInput("input", asset.asset_id),),
        )
        self.assertEqual(asset.asset_id, job.inputs[0].asset_id)
        with self.assertRaises(self.psycopg.errors.RaiseException):
            durable.create_job(
                "foreign-upload-ns",
                "foreign-upload-job-key",
                {"prompt": "upload"},
                deployment_id,
                (JobInput("input", asset.asset_id),),
            )

    def test_cross_job_current_and_winner_attempts_are_rejected(self) -> None:
        deployment = self._deployment()
        worker = self._worker(deployment)
        first_job, second_job = self._job(deployment), self._job(deployment)
        attempt = self._attempt(first_job, worker)
        constraints = self.connection.execute(
            """SELECT pg_get_constraintdef(oid) FROM pg_constraint
               WHERE conname IN ('jobs_current_attempt_fk', 'jobs_winning_attempt_fk')"""
        ).fetchall()
        self.assertEqual(2, len(constraints))
        definitions = [
            item[0].decode() if isinstance(item[0], bytes) else item[0] for item in constraints
        ]
        self.assertTrue(
            all(
                "FOREIGN KEY (id," in item and "job_attempts(job_id, id)" in item
                for item in definitions
            )
        )
        # A RUNNING shape permits a current attempt, so the deferred composite
        # foreign key (rather than a lifecycle-shape guard) rejects this at
        # commit.  A winning pointer is also rejected (by the success guard
        # before the deferred FK can commit), which is still a safe failure.
        with self.assertRaises(self.psycopg.errors.ForeignKeyViolation):
            with self.connection.transaction():
                self.connection.execute(
                    "UPDATE jobs SET state='running',current_attempt_id=%s,lease_epoch=1 WHERE id=%s",
                    (attempt, second_job),
                )
                self.connection.execute("SET CONSTRAINTS jobs_current_attempt_fk IMMEDIATE")
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                "UPDATE jobs SET winning_attempt_id=%s WHERE id=%s", (attempt, second_job)
            )
        self.connection.rollback()

    def test_artifact_attempt_job_mismatch_is_rejected(self) -> None:
        deployment = self._deployment()
        worker = self._worker(deployment)
        first_job, second_job = self._job(deployment), self._job(deployment)
        attempt, blob = self._attempt(first_job, worker), self._blob()
        with self.assertRaises(self.psycopg.errors.NotNullViolation):
            with self.connection.transaction():
                self.connection.execute(
                    "INSERT INTO artifacts (id, job_id, attempt_id, blob_id, kind, state) VALUES (%s,%s,%s,%s,'output','ready')",
                    (self._id(), second_job, attempt, blob),
                )

    def test_unverified_asset_job_input_is_rejected(self) -> None:
        deployment, blob = self._deployment(), self._blob()
        job, asset = self._job(deployment), self._id()
        self.connection.execute(
            "INSERT INTO assets (id, namespace_id, blob_id, kind, state) VALUES (%s,'ns',%s,'input','rejected')",
            (asset, blob),
        )
        self.connection.commit()
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                "INSERT INTO job_inputs (job_id, role, asset_id) VALUES (%s,'input',%s)",
                (job, asset),
            )
        self.connection.rollback()

    def test_asset_and_required_artifact_domain_checks(self) -> None:
        deployment, blob = self._deployment(), self._blob()
        self.connection.execute("UPDATE blobs SET state='quarantined' WHERE id=%s", (blob,))
        self.connection.commit()
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                "INSERT INTO assets (id,namespace_id,blob_id,kind,state) VALUES (%s,'ns',%s,'input','verified')",
                (self._id(), blob),
            )
        self.connection.rollback()
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self._job(deployment, ("output", "output"))
        self.connection.rollback()
        job, worker = self._job(deployment), self._worker(deployment)
        attempt = self._attempt(job, worker)
        verified_blob = self._blob()
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.connection.execute(
                "INSERT INTO artifacts (id,job_id,attempt_id,blob_id,kind,state,publication_id) VALUES (%s,%s,%s,%s,'other','ready',%s)",
                (self._id(), job, attempt, verified_blob, self._id()),
            )
        self.connection.rollback()
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                "INSERT INTO artifacts (id,job_id,attempt_id,blob_id,kind,state,publication_id) VALUES (%s,%s,%s,%s,'manifest','ready',%s)",
                (self._id(), job, attempt, blob, self._id()),
            )
        self.connection.rollback()

    def test_success_trigger_requires_complete_winner_artifacts(self) -> None:
        deployment = self._deployment()
        worker = self._worker(deployment)
        job = self._job(deployment, ("output", "manifest"))
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 60)
        assert lease is not None
        publications = PostgresPublicationRepository(self.connection)
        digest = "d" * 64
        blob = BlobRecord(
            self._id(),
            digest,
            1,
            "blobs/sha256/dd/dd/" + digest,
            BlobState.VERIFIED,
            "application/octet-stream",
        )

        def validated(kind: ArtifactKind):
            reserved = publications.reserve(
                lease,
                PublicationSpec(kind, blob.content_type, 1, digest, 1, {"kind": kind.value}),
            )
            written = publications.record_object(lease, reserved.publication_id, 0, digest, 1)
            return publications.record_validated(
                lease, written.publication_id, written.version, blob, {"probe": "ok"}
            )

        output = validated(ArtifactKind.OUTPUT)
        with self.assertRaises(DurableInvariantError):
            publications.finalize(lease, (output,))
        manifest = validated(ArtifactKind.MANIFEST)
        visible = publications.finalize(lease, (output, manifest))
        self.assertEqual({ArtifactKind.OUTPUT, ArtifactKind.MANIFEST}, {i.kind for i in visible})
        attempt = lease.attempt_id
        blob_id = visible[0].blob_id

        guarded_statements = (
            ("UPDATE jobs SET current_attempt_id=%s WHERE id=%s", (attempt, job)),
            ("UPDATE jobs SET required_artifact_kinds=ARRAY['output'] WHERE id=%s", (job,)),
            ("UPDATE jobs SET request_json='{\"mutated\":true}' WHERE id=%s", (job,)),
            ("UPDATE jobs SET state='failed' WHERE id=%s", (job,)),
            ("UPDATE job_attempts SET state='failed_retryable' WHERE id=%s", (attempt,)),
            ("UPDATE job_attempts SET finished_at=now() WHERE id=%s", (attempt,)),
            ("UPDATE artifacts SET state='rejected' WHERE job_id=%s AND kind='output'", (job,)),
            ("UPDATE blobs SET state='quarantined' WHERE id=%s", (blob_id,)),
        )
        for statement, parameters in guarded_statements:
            with self.assertRaises(self.psycopg.errors.RaiseException):
                self.connection.execute(statement, parameters)
            self.connection.rollback()
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                "INSERT INTO artifacts (id,job_id,attempt_id,blob_id,kind,state) VALUES (%s,%s,%s,%s,'output','ready')",
                (self._id(), job, attempt, blob_id),
            )
        self.connection.rollback()

    def test_pre_success_artifact_bypass_requires_publication(self) -> None:
        deployment = self._deployment()
        worker = self._worker(deployment)
        job = self._job(deployment)
        attempt = self._attempt(job, worker)
        blob = self._blob()
        with self.assertRaises(self.psycopg.errors.NotNullViolation):
            self.connection.execute(
                "INSERT INTO artifacts (id,job_id,attempt_id,blob_id,kind,state) VALUES (%s,%s,%s,%s,'output','ready')",
                (self._id(), job, attempt, blob),
            )
        self.connection.rollback()

    def test_active_job_input_cannot_be_downgraded(self) -> None:
        deployment, blob = self._deployment(), self._blob()
        job, asset = self._job(deployment), self._verified_asset(blob)
        self.connection.execute(
            "INSERT INTO job_inputs (job_id, role, asset_id) VALUES (%s,'input',%s)", (job, asset)
        )
        self.connection.commit()
        for statement, identifier in (
            ("UPDATE assets SET state='rejected' WHERE id=%s", asset),
            ("UPDATE blobs SET state='quarantined' WHERE id=%s", blob),
        ):
            with self.assertRaises(self.psycopg.errors.RaiseException):
                self.connection.execute(statement, (identifier,))
            self.connection.rollback()
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute("DELETE FROM job_inputs WHERE job_id=%s", (job,))
        self.connection.rollback()
        with self.assertRaises(self.psycopg.errors.ForeignKeyViolation):
            self.connection.execute("DELETE FROM assets WHERE id=%s", (asset,))
        self.connection.rollback()

    def test_persistence_identities_and_events_are_immutable(self) -> None:
        deployment, blob = self._deployment(), self._blob()
        asset = self._verified_asset(blob)
        worker, job = self._worker(deployment), self._job(deployment)
        attempt = self._attempt(job, worker)
        other_worker = self._worker(deployment)
        other_deployment = self._deployment()
        self.connection.execute(
            "INSERT INTO job_events (job_id,event_type) VALUES (%s,'audit')", (job,)
        )
        self.connection.commit()
        guarded = (
            ("UPDATE model_deployments SET name='changed' WHERE id=%s", (deployment,)),
            (
                "UPDATE model_deployments SET manifest='{\"changed\":true}' WHERE id=%s",
                (deployment,),
            ),
            ("UPDATE blobs SET storage_key='changed' WHERE id=%s", (blob,)),
            ("UPDATE assets SET kind='changed' WHERE id=%s", (asset,)),
            ("UPDATE job_attempts SET worker_id=%s WHERE id=%s", (other_worker, attempt)),
            ("UPDATE workers SET model_deployment_id=%s WHERE id=%s", (other_deployment, worker)),
            ("UPDATE job_events SET event_type='changed' WHERE job_id=%s", (job,)),
            ("DELETE FROM job_events WHERE job_id=%s", (job,)),
        )
        for statement, parameters in guarded:
            with self.assertRaises(self.psycopg.errors.RaiseException):
                self.connection.execute(statement, parameters)
            self.connection.rollback()

    def test_fingerprint_and_active_attempt_constraints(self) -> None:
        deployment = self._deployment()
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                "UPDATE model_deployments SET fingerprint='other' WHERE id=%s", (deployment,)
            )
        self.connection.rollback()

    def test_job_lifecycle_shape_is_not_bypassable(self) -> None:
        deployment = self._deployment()
        worker = self._worker(deployment)
        job = self._job(deployment)
        attempt = self._attempt(job, worker)
        for statement, parameters in (
            ("UPDATE jobs SET state='queued' WHERE id=%s", (job,)),
            ("UPDATE jobs SET state='failed',current_attempt_id=%s WHERE id=%s", (attempt, job)),
        ):
            with self.assertRaises(self.psycopg.errors.RaiseException):
                self.connection.execute(statement, parameters)
            self.connection.rollback()
        with self.assertRaises(self.psycopg.errors.RaiseException):
            with self.connection.transaction():
                self.connection.execute(
                    "UPDATE job_attempts SET state='failed_retryable' WHERE id=%s", (attempt,)
                )
                self.connection.execute("SET CONSTRAINTS attempts_job_linkage_guard IMMEDIATE")
        second_job = self._job(deployment)
        second_worker = self._worker(deployment)
        with self.assertRaises(self.psycopg.errors.RaiseException):
            with self.connection.transaction():
                self.connection.execute(
                    """INSERT INTO job_attempts (id,job_id,worker_id,attempt_no,lease_epoch,state,heartbeat_at,lease_expires_at)
                       VALUES (%s,%s,%s,1,1,'active',clock_timestamp(),clock_timestamp()+interval '60 seconds')""",
                    (self._id(), second_job, second_worker),
                )
                self.connection.execute("SET CONSTRAINTS attempts_job_linkage_guard IMMEDIATE")
        worker, job = self._worker(deployment), self._job(deployment)
        self._attempt(job, worker)
        with self.assertRaises(self.psycopg.errors.UniqueViolation):
            self.connection.execute(
                """INSERT INTO job_attempts (id,job_id,worker_id,attempt_no,lease_epoch,state,heartbeat_at,lease_expires_at)
                   VALUES (%s,%s,%s,2,2,'active',clock_timestamp(),clock_timestamp()+interval '60 seconds')""",
                (self._id(), job, worker),
            )
        self.connection.rollback()

    def test_two_connections_observe_unique_and_active_attempt_contention(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        first = self.psycopg.connect(self._dsn(self.database))
        second = self.psycopg.connect(self._dsn(self.database))
        try:
            first.execute("SET lock_timeout = '100ms'")
            second.execute("SET lock_timeout = '100ms'")
            first.commit()
            second.commit()
            first_attempt = self._id()
            first.execute(
                """INSERT INTO job_attempts (id,job_id,worker_id,attempt_no,lease_epoch,state,heartbeat_at,lease_expires_at)
                   VALUES (%s,%s,%s,1,1,'active',clock_timestamp(),clock_timestamp()+interval '60 seconds')""",
                (first_attempt, job, worker),
            )
            first.execute(
                "UPDATE jobs SET state='running',current_attempt_id=%s,lease_epoch=1 WHERE id=%s",
                (first_attempt, job),
            )
            with self.assertRaises(self.psycopg.errors.LockNotAvailable):
                second.execute(
                    """INSERT INTO job_attempts (id,job_id,worker_id,attempt_no,lease_epoch,state,heartbeat_at,lease_expires_at)
                       VALUES (%s,%s,%s,2,2,'active',clock_timestamp(),clock_timestamp()+interval '60 seconds')""",
                    (self._id(), job, worker),
                )
            second.rollback()
            first.commit()

            first.execute(
                """INSERT INTO jobs (id, namespace_id, idempotency_key, request_sha256, request_json,
                   model_deployment_id) VALUES (%s,'concurrent','same-key',%s,'{}',%s)""",
                (self._id(), "b" * 64, deployment),
            )
            with self.assertRaises(self.psycopg.errors.LockNotAvailable):
                second.execute(
                    """INSERT INTO jobs (id, namespace_id, idempotency_key, request_sha256, request_json,
                       model_deployment_id) VALUES (%s,'concurrent','same-key',%s,'{}',%s)""",
                    (self._id(), "b" * 64, deployment),
                )
            second.rollback()
            first.commit()
        finally:
            first.rollback()
            second.rollback()
            first.close()
            second.close()

    def test_publication_happy_path_has_one_visible_exact_winner(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 60)
        assert lease is not None
        repository = PostgresPublicationRepository(self.connection)
        digest = "1" * 64
        reserved = repository.reserve(
            lease,
            PublicationSpec(
                ArtifactKind.OUTPUT,
                "audio/wav",
                4,
                digest,
                4,
                {"source": "fake-runtime"},
            ),
        )
        self.assertEqual(PublicationState.RESERVED, reserved.state)
        self.assertEqual(4, reserved.max_size_bytes)
        with self.assertRaises(StateTransitionError):
            repository.reserve(
                lease,
                PublicationSpec(
                    ArtifactKind.OUTPUT,
                    "audio/wav",
                    5,
                    digest,
                    4,
                    {"source": "fake-runtime"},
                ),
            )
        self.assertEqual(
            reserved,
            repository.reserve(
                lease,
                PublicationSpec(
                    ArtifactKind.OUTPUT,
                    "audio/wav",
                    4,
                    digest,
                    4,
                    {"source": "fake-runtime"},
                ),
            ),
        )
        written = repository.record_object(lease, reserved.publication_id, 0, digest, 4)
        self.assertEqual(
            "attempts/{0}/{1}/epoch-1/output/{2}".format(
                job, lease.attempt_id, reserved.publication_id
            ),
            written.attempt_object_key,
        )
        self.assertEqual((), repository.visible_winner(job))
        blob = BlobRecord(
            self._id(),
            digest,
            4,
            "blobs/sha256/11/11/" + digest,
            BlobState.VERIFIED,
            "audio/wav",
        )
        validated = repository.record_validated(
            lease,
            written.publication_id,
            written.version,
            blob,
            {"duration_seconds": 0.1, "media_type": "audio/wav"},
        )
        self.assertEqual("audio/wav", validated.observed_content_type)
        self.assertEqual("audio/wav", validated.validator_metadata["media_type"])
        visible = repository.finalize(lease, (validated,))
        self.assertEqual(1, len(visible))
        self.assertEqual(digest, visible[0].sha256)
        self.assertEqual((), repository.visible_winner(job, "other-namespace"))
        self.assertEqual(1, len(repository.visible_winner(job, "ns")))
        row = self.connection.execute(
            "SELECT state,current_attempt_id,winning_attempt_id FROM jobs WHERE id=%s", (job,)
        ).fetchone()
        self.assertEqual(("succeeded", None, lease.attempt_id), (row[0], row[1], str(row[2])))
        self.assertEqual(
            1,
            self.connection.execute(
                "SELECT count(*) FROM job_events WHERE job_id=%s AND event_type='job_succeeded'",
                (job,),
            ).fetchone()[0],
        )
        candidates = repository.cleanup_candidates(
            datetime.now(timezone.utc) + timedelta(seconds=1)
        )
        self.assertEqual(
            (validated.publication_id,), tuple(item.publication_id for item in candidates)
        )
        cleaned = repository.record_cleanup(validated.publication_id, validated.version + 1)
        self.assertIsNotNone(cleaned.attempt_object_deleted_at)
        self.assertEqual(
            (), repository.cleanup_candidates(datetime.now(timezone.utc) + timedelta(seconds=1))
        )

    def test_publication_finalize_rolls_back_when_lease_expires_mid_transaction(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 1)
        assert lease is not None
        repository = PostgresPublicationRepository(self.connection)
        digest = "9" * 64
        reserved = repository.reserve(
            lease, PublicationSpec(ArtifactKind.OUTPUT, "audio/wav", 4, digest, 4)
        )
        written = repository.record_object(lease, reserved.publication_id, 0, digest, 4)
        validated = repository.record_validated(
            lease,
            written.publication_id,
            written.version,
            BlobRecord(
                self._id(),
                digest,
                4,
                "blobs/sha256/99/99/" + digest,
                BlobState.VERIFIED,
                "audio/wav",
            ),
            {"probe": "ok"},
        )
        self.connection.execute(
            """CREATE FUNCTION test_delay_publication_finalize() RETURNS trigger LANGUAGE plpgsql AS $$
               BEGIN
                   IF NEW.state = 'finalized' THEN PERFORM pg_sleep(1.1); END IF;
                   RETURN NEW;
               END;
               $$"""
        )
        self.connection.execute(
            """CREATE TRIGGER zz_test_delay_publication_finalize
               AFTER UPDATE ON artifact_publications
               FOR EACH ROW EXECUTE FUNCTION test_delay_publication_finalize()"""
        )
        self.connection.commit()
        try:
            with self.assertRaises(StateTransitionError):
                repository.finalize(lease, (validated,))
            self.assertEqual(
                PublicationState.VALIDATED, repository.get(validated.publication_id).state
            )
            self.assertEqual((), repository.visible_winner(job))
            self.assertEqual(
                0,
                self.connection.execute(
                    "SELECT count(*) FROM artifacts WHERE job_id=%s", (job,)
                ).fetchone()[0],
            )
            state = self.connection.execute(
                "SELECT state,winning_attempt_id FROM jobs WHERE id=%s", (job,)
            ).fetchone()
            self.assertEqual(("running", None), state)
        finally:
            self.connection.execute(
                "DROP TRIGGER zz_test_delay_publication_finalize ON artifact_publications"
            )
            self.connection.execute("DROP FUNCTION test_delay_publication_finalize()")
            self.connection.commit()

    def test_publication_observed_size_cannot_exceed_sealed_max(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 60)
        assert lease is not None
        repository = PostgresPublicationRepository(self.connection)
        reserved = repository.reserve(lease, PublicationSpec(ArtifactKind.OUTPUT, "audio/wav", 4))
        with self.assertRaises(DurableInvariantError):
            repository.record_object(lease, reserved.publication_id, 0, "5" * 64, 5)
        current = repository.get(reserved.publication_id)
        self.assertEqual(PublicationState.RESERVED, current.state)
        self.assertIsNone(current.observed_size_bytes)
        key = PostgresPublicationRepository.attempt_object_key(current)
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.connection.execute(
                """UPDATE artifact_publications SET state='object_written',version=1,
                   attempt_object_key=%s,observed_sha256=%s,observed_size_bytes=5
                   WHERE id=%s""",
                (key, "5" * 64, current.publication_id),
            )
        self.connection.rollback()
        current = repository.get(reserved.publication_id)
        self.assertEqual(PublicationState.RESERVED, current.state)
        self.assertEqual(0, current.version)
        self.assertEqual((), repository.visible_winner(job))
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM artifacts WHERE job_id=%s", (job,)
            ).fetchone()[0],
        )

    def test_publication_raw_guards_and_stale_abandon_recovery(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 60)
        assert lease is not None
        late_id, early_id = self._id(), self._id()
        with self.assertRaises(self.psycopg.errors.CheckViolation):
            self.connection.execute(
                """INSERT INTO artifact_publications
                   (id,job_id,attempt_id,worker_id,lease_epoch,kind,expected_content_type,
                    max_size_bytes,expected_size_bytes)
                   VALUES (%s,%s,%s,%s,%s,'output','audio/wav',16,17)""",
                (self._id(), job, lease.attempt_id, worker, lease.lease_epoch),
            )
        self.connection.rollback()
        self.connection.execute(
            """INSERT INTO artifact_publications
               (id,job_id,attempt_id,worker_id,lease_epoch,kind,expected_content_type,
                max_size_bytes,created_at,updated_at)
               VALUES (%s,%s,%s,%s,%s,'output','audio/wav',16,
                         clock_timestamp()-interval '6 minutes',clock_timestamp()-interval '6 minutes'),
                      (%s,%s,%s,%s,%s,'manifest','application/json',16,
                         clock_timestamp()-interval '6 minutes',clock_timestamp()-interval '6 minutes')""",
            (
                late_id,
                job,
                lease.attempt_id,
                worker,
                lease.lease_epoch,
                early_id,
                job,
                lease.attempt_id,
                worker,
                lease.lease_epoch,
            ),
        )
        self.connection.commit()
        inserted = self.connection.execute(
            "SELECT created_at,updated_at,max_size_bytes FROM artifact_publications WHERE id=%s",
            (late_id,),
        ).fetchone()
        self.assertGreater(inserted[0], datetime.now(timezone.utc) - timedelta(seconds=5))
        self.assertGreater(inserted[1], datetime.now(timezone.utc) - timedelta(seconds=5))
        self.assertEqual(16, inserted[2])
        for statement, parameters in (
            (
                "UPDATE artifact_publications SET id=%s,version=1 WHERE id=%s",
                (self._id(), late_id),
            ),
            (
                "UPDATE artifact_publications SET kind='manifest',version=1 WHERE id=%s",
                (late_id,),
            ),
            (
                "UPDATE artifact_publications SET max_size_bytes=32,version=1 WHERE id=%s",
                (late_id,),
            ),
            (
                "UPDATE artifact_publications SET state='object_written',version=2,attempt_object_key='wrong',observed_sha256=%s,observed_size_bytes=1 WHERE id=%s",
                ("2" * 64, late_id),
            ),
            (
                """UPDATE artifact_publications SET state='rejected',version=1,
                   terminal_reason='invalid cleanup',terminal_at=clock_timestamp(),
                   attempt_object_deleted_at=clock_timestamp() WHERE id=%s""",
                (late_id,),
            ),
            ("DELETE FROM artifact_publications WHERE id=%s", (late_id,)),
        ):
            with self.assertRaises(self.psycopg.errors.RaiseException):
                self.connection.execute(statement, parameters)
            self.connection.rollback()
        self.connection.execute(
            """UPDATE job_attempts SET heartbeat_at=clock_timestamp()-interval '2 seconds',
               lease_expires_at=clock_timestamp()-interval '1 second' WHERE id=%s""",
            (lease.attempt_id,),
        )
        self.connection.commit()
        repository = PostgresPublicationRepository(self.connection)
        with self.assertRaises(StateTransitionError, msg="first stale observation starts DB grace"):
            repository.abandon(late_id, 0, "stale recovery")
        marked = repository.get(late_id)
        self.assertEqual(PublicationState.RESERVED, marked.state)
        self.assertEqual(1, marked.version)
        self.assertIsNotNone(marked.stale_since)
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                """UPDATE artifact_publications SET stale_since=clock_timestamp()-interval '6 minutes',
                   version=2 WHERE id=%s""",
                (late_id,),
            )
        self.connection.rollback()
        with self.assertRaises(StateTransitionError, msg="DB grace cannot be bypassed immediately"):
            repository.abandon(late_id, 1, "too early")
        self.assertEqual(
            "reserved",
            self.connection.execute(
                "SELECT state FROM artifact_publications WHERE id=%s", (late_id,)
            ).fetchone()[0],
        )
        self.assertEqual((), PostgresPublicationRepository(self.connection).visible_winner(job))
        self.assertEqual(
            0,
            self.connection.execute(
                "SELECT count(*) FROM artifacts WHERE job_id=%s", (job,)
            ).fetchone()[0],
        )

    def test_publication_version_cancel_and_expiry_fences_all_progress(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        queue = PostgresLeaseQueue(self.connection)
        lease = queue.claim_next(worker, 60)
        assert lease is not None
        repository = PostgresPublicationRepository(self.connection)
        reserved = repository.reserve(
            lease, PublicationSpec(ArtifactKind.OUTPUT, "audio/wav", 3, "3" * 64, 3)
        )
        with self.assertRaises(StateTransitionError):
            repository.record_object(lease, reserved.publication_id, 1, "3" * 64, 3)
        self.assertTrue(queue.request_cancel(job))
        with self.assertRaises(StateTransitionError):
            repository.record_object(lease, reserved.publication_id, 0, "3" * 64, 3)
        with self.assertRaises(self.psycopg.errors.RaiseException):
            self.connection.execute(
                """UPDATE artifact_publications SET state='object_written',version=1,
                   attempt_object_key=%s,observed_sha256=%s,observed_size_bytes=3 WHERE id=%s""",
                (
                    PostgresPublicationRepository.attempt_object_key(reserved),
                    "3" * 64,
                    reserved.publication_id,
                ),
            )
        self.connection.rollback()
        self.assertEqual((), repository.visible_winner(job))

    def test_publication_finalize_vs_cancel_overlap_has_one_legal_winner(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 60)
        assert lease is not None
        validated = self._validated_output(self.connection, lease, "6" * 64)
        final_connection = self.psycopg.connect(self._dsn(self.database))
        cancel_connection = self.psycopg.connect(self._dsn(self.database))
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def finalize() -> None:
            try:
                barrier.wait()
                PostgresPublicationRepository(final_connection).finalize(lease, (validated,))
                outcomes.append("finalized")
            except StateTransitionError:
                outcomes.append("finalize_stale")
            except BaseException as error:
                errors.append(error)

        def cancel() -> None:
            try:
                barrier.wait()
                queue = PostgresLeaseQueue(cancel_connection)
                if queue.request_cancel(job):
                    queue.cancel_current(lease)
                    outcomes.append("cancelled")
                else:
                    outcomes.append("cancel_noop")
            except BaseException as error:
                errors.append(error)

        threads = (threading.Thread(target=finalize), threading.Thread(target=cancel))
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], errors)
            self.assertIn(
                sorted(outcomes),
                (sorted(("finalized", "cancel_noop")), sorted(("finalize_stale", "cancelled"))),
            )
            state, winner = self.connection.execute(
                "SELECT state,winning_attempt_id FROM jobs WHERE id=%s", (job,)
            ).fetchone()
            publication_state = (
                PostgresPublicationRepository(self.connection).get(validated.publication_id).state
            )
            artifact_count = self.connection.execute(
                "SELECT count(*) FROM artifacts WHERE job_id=%s", (job,)
            ).fetchone()[0]
            success_events = self.connection.execute(
                "SELECT count(*) FROM job_events WHERE job_id=%s AND event_type='job_succeeded'",
                (job,),
            ).fetchone()[0]
            if state == "succeeded":
                self.assertEqual(str(lease.attempt_id), str(winner))
                self.assertEqual(PublicationState.FINALIZED, publication_state)
                self.assertEqual((1, 1), (artifact_count, success_events))
            else:
                self.assertEqual(("cancelled", None), (state, winner))
                self.assertEqual(PublicationState.VALIDATED, publication_state)
                self.assertEqual((0, 0), (artifact_count, success_events))
        finally:
            final_connection.close()
            cancel_connection.close()

    def test_publication_finalize_vs_reaper_overlap_has_one_legal_winner(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 1)
        assert lease is not None
        validated = self._validated_output(self.connection, lease, "7" * 64)
        delay = (lease.expires_at - datetime.now(timezone.utc)).total_seconds()
        time.sleep(max(0.0, delay) + 0.05)
        final_connection = self.psycopg.connect(self._dsn(self.database))
        reaper_connection = self.psycopg.connect(self._dsn(self.database))
        barrier = threading.Barrier(2)
        outcomes: list[str] = []
        errors: list[BaseException] = []

        def finalize() -> None:
            try:
                barrier.wait()
                PostgresPublicationRepository(final_connection).finalize(lease, (validated,))
                outcomes.append("finalized")
            except StateTransitionError:
                outcomes.append("finalize_stale")
            except BaseException as error:
                errors.append(error)

        def reap() -> None:
            try:
                with reaper_connection.transaction():
                    reaper_connection.execute(
                        "SELECT id FROM workers WHERE id=%s AND state='busy' FOR UPDATE", (worker,)
                    ).fetchone()
                    barrier.wait()
                    time.sleep(0.05)
                    count = PostgresLeaseQueue(reaper_connection).reap_expired(
                        retry_delay_seconds=1
                    )
                outcomes.append("reaped-{0}".format(count))
            except BaseException as error:
                errors.append(error)

        threads = (threading.Thread(target=finalize), threading.Thread(target=reap))
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual([], errors)
            self.assertEqual(["finalize_stale", "reaped-1"], sorted(outcomes))
            state = self.connection.execute(
                "SELECT state,current_attempt_id,winning_attempt_id FROM jobs WHERE id=%s", (job,)
            ).fetchone()
            self.assertEqual(("queued", None, None), tuple(state))
            self.assertEqual(
                PublicationState.VALIDATED,
                PostgresPublicationRepository(self.connection).get(validated.publication_id).state,
            )
            self.assertEqual(
                0,
                self.connection.execute(
                    "SELECT count(*) FROM artifacts WHERE job_id=%s", (job,)
                ).fetchone()[0],
            )
            self.assertEqual(
                0,
                self.connection.execute(
                    "SELECT count(*) FROM job_events WHERE job_id=%s AND event_type='job_succeeded'",
                    (job,),
                ).fetchone()[0],
            )
        finally:
            final_connection.close()
            reaper_connection.close()

    def test_publication_two_connection_finalize_race_has_one_winner(self) -> None:
        deployment = self._deployment()
        job = self._job(deployment)
        worker = self._worker(deployment)
        lease = PostgresLeaseQueue(self.connection).claim_next(worker, 60)
        assert lease is not None
        repository = PostgresPublicationRepository(self.connection)
        digest = "4" * 64
        reserved = repository.reserve(
            lease, PublicationSpec(ArtifactKind.OUTPUT, "audio/wav", 4, digest, 4)
        )
        written = repository.record_object(lease, reserved.publication_id, 0, digest, 4)
        validated = repository.record_validated(
            lease,
            written.publication_id,
            written.version,
            BlobRecord(
                self._id(),
                digest,
                4,
                "blobs/sha256/44/44/" + digest,
                BlobState.VERIFIED,
                "audio/wav",
            ),
            {"probe": "ok"},
        )
        connections = [
            self.psycopg.connect(self._dsn(self.database)),
            self.psycopg.connect(self._dsn(self.database)),
        ]
        barrier = threading.Barrier(2)
        outcomes: list[str] = []

        def finalize(connection: Any) -> None:
            barrier.wait()
            try:
                PostgresPublicationRepository(connection).finalize(lease, (validated,))
            except StateTransitionError:
                outcomes.append("stale")
            else:
                outcomes.append("winner")

        threads = [
            threading.Thread(target=finalize, args=(connection,)) for connection in connections
        ]
        try:
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)
            self.assertTrue(all(not thread.is_alive() for thread in threads))
            self.assertEqual(["stale", "winner"], sorted(outcomes))
            self.assertEqual(
                1,
                self.connection.execute(
                    "SELECT count(*) FROM artifacts WHERE job_id=%s", (job,)
                ).fetchone()[0],
            )
            self.assertEqual(
                1,
                self.connection.execute(
                    "SELECT count(*) FROM job_events WHERE job_id=%s AND event_type='job_succeeded'",
                    (job,),
                ).fetchone()[0],
            )
        finally:
            for connection in connections:
                connection.close()


if __name__ == "__main__":
    unittest.main()
