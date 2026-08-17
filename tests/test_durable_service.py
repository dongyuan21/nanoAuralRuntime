# pyright: reportMissingImports=false
"""Production durable-service composition and PostgreSQL HTTP E2E coverage."""

from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import tempfile
import threading
import time
import unittest
import wave
from datetime import datetime, timedelta, timezone
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from unittest.mock import patch
from uuid import uuid4

from nano_aural_runtime.durable.artifact_publication import (
    ArtifactPublicationService,
    PublicationCandidate,
)
from nano_aural_runtime.durable.artifact_storage import (
    LocalAttemptArtifactStore,
    attempt_key,
)
from nano_aural_runtime.durable.artifact_validation import StreamingMediaArtifactValidator
from nano_aural_runtime.durable.domain import ArtifactKind, DeploymentRecord, WorkerRecord
from nano_aural_runtime.durable.postgres_repository import PostgresDurableRepository
from nano_aural_runtime.durable.postgres_uploads import PostgresUploadRepository
from nano_aural_runtime.durable.publication import (
    PostgresPublicationRepository,
    PublicationSpec,
)
from nano_aural_runtime.durable.queue import PostgresLeaseQueue
from nano_aural_runtime.durable.recovery import _dry_run_attempts
from nano_aural_runtime.durable.recovery import main as recovery_main
from nano_aural_runtime.durable.reference_worker import ReferenceWorker, ReferenceWorkerConfig
from nano_aural_runtime.durable.service import DurableService, DurableServiceConfig, main
from nano_aural_runtime.durable.storage import LocalBlobStore
from nano_aural_runtime.durable.uploads import (
    LocalStagingBlobStore,
    UploadMode,
    UploadSession,
    WaveMediaProbe,
)
from nano_aural_runtime.durable.wiring import TokenGrant
from nano_aural_runtime_remote.client import RemoteClient, RemoteNotFound, UrllibTransport

POSTGRES_BIN = os.environ.get("NANO_AURAL_POSTGRES_BIN")
POSTGRES_READY = False
if POSTGRES_BIN:
    REQUIRED_BINARIES = ("initdb", "postgres", "pg_ctl")
    missing = [name for name in REQUIRED_BINARIES if not (Path(POSTGRES_BIN) / name).is_file()]
    POSTGRES_READY = not missing and find_spec("psycopg") is not None


def _wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * 400)
    return output.getvalue()


def _grant(token: str, subject: str, namespaces: tuple[str, ...]) -> TokenGrant:
    return TokenGrant.from_token(
        token,
        subject,
        scopes=("assets:write", "jobs:submit", "jobs:read", "jobs:cancel", "artifacts:read"),
        namespaces=namespaces,
    )


class _Connection:
    def __init__(self) -> None:
        self.autocommit = False
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_operator_config_uses_digests_and_service_closes_owned_resources(tmp_path: Path) -> None:
    token = "operator-test-token"
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    environment = {
        "NANO_AURAL_DATABASE_DSN": "postgresql://operator@db/service",
        "NANO_AURAL_CANONICAL_BLOB_ROOT": str(tmp_path / "canonical"),
        "NANO_AURAL_STAGING_BLOB_ROOT": str(tmp_path / "staging"),
        "NANO_AURAL_ATTEMPT_ARTIFACT_ROOT": str(tmp_path / "attempts"),
        "NANO_AURAL_TOKEN_GRANTS_JSON": (
            '[{{"token_sha256":"{0}","subject":"operator",'
            '"scopes":["jobs:read"],"namespaces":["namespace-a"]}}]'
        ).format(digest),
        "NANO_AURAL_API_PORT": "0",
    }
    config = DurableServiceConfig.from_environment(environment)
    assert config.token_grants[0].token_sha256 == digest
    assert config.port == 0
    assert environment["NANO_AURAL_DATABASE_DSN"] not in repr(config)

    connection = _Connection()
    service = DurableService(config, connection)
    assert connection.autocommit
    assert service.metrics.snapshot() == ()
    service.close()
    service.close()
    assert connection.closed


@unittest.skipUnless(
    POSTGRES_READY,
    "PostgreSQL binaries or psycopg unavailable",
)
class DurableServicePostgresE2ETests(unittest.TestCase):
    """Isolated PG16-style cluster, real local stores, and loopback HTTP API."""

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
        cls.cluster = tempfile.TemporaryDirectory()
        cls.data_dir = Path(cls.cluster.name) / "data"
        cls.socket_dir = Path(cls.cluster.name) / "socket"
        cls.socket_dir.mkdir()
        cls.port = 55433
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
        if hasattr(cls, "cluster"):
            cls.cluster.cleanup()

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
        self.database = "p3e_service_" + uuid4().hex
        self.maintenance.execute("CREATE DATABASE " + self.database)
        self.connection = self.psycopg.connect(self._dsn(self.database))
        with patch.dict(
            os.environ, {"NANO_AURAL_DATABASE_DSN": self._dsn(self.database)}, clear=True
        ):
            self.assertEqual(0, main(("--migrate-only",)))
            self.assertEqual(0, main(("--migrate-only",)))
        # The durable repositories delimit each mutating command explicitly;
        # keep this independent queue/publication connection from retaining a
        # read transaction between worker-side checkpoints.
        self.connection.autocommit = True
        self.durable_repository = PostgresDurableRepository(self.connection)
        self.publication_repository = PostgresPublicationRepository(self.connection)
        self.storage = tempfile.TemporaryDirectory()
        self.downloads = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.writer_token = "writer-token"
        config = DurableServiceConfig(
            self._dsn(self.database),
            Path(self.storage.name) / "canonical",
            Path(self.storage.name) / "staging",
            Path(self.storage.name) / "attempts",
            (
                _grant(self.writer_token, "writer", ("namespace-a",)),
                _grant("foreign-token", "foreign", ("namespace-b",)),
            ),
            port=0,
        )
        self.deployment = self.durable_repository.register_deployment(
            DeploymentRecord(str(uuid4()), "e2e", "fake", "e2e-fingerprint")
        )
        self.service_connection = self.psycopg.connect(self._dsn(self.database))
        self.service = DurableService(config, self.service_connection)
        self.server = self.service.create_server()
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        host, port = self.server.server_address[:2]
        transport = UrllibTransport("http://{0}:{1}".format(host, port), allow_loopback_http=True)
        self.client = RemoteClient(transport, self.writer_token)
        self.foreign_client = RemoteClient(transport, "foreign-token")

    def tearDown(self) -> None:
        if hasattr(self, "server"):
            self.server.shutdown()
            self.thread.join(timeout=5)
            self.server.server_close()
        if hasattr(self, "service"):
            self.service.close()
        if hasattr(self, "connection"):
            self.connection.close()
        if hasattr(self, "maintenance") and hasattr(self, "database"):
            self.maintenance.execute("DROP DATABASE " + self.database)
        if hasattr(self, "storage"):
            self.storage.cleanup()
        if hasattr(self, "downloads"):
            self.downloads.cleanup()

    def _command(self, key: str, asset_id: str) -> dict[str, object]:
        return {
            "namespace_id": "namespace-a",
            "idempotency_key": key,
            "deployment_id": self.deployment.deployment_id,
            "request": {"operation": "fake"},
            "inputs": [{"role": "source", "asset_id": asset_id}],
            "required_artifact_kinds": ["output"],
        }

    def test_loopback_api_uses_real_postgres_for_upload_jobs_events_cancel_and_download(
        self,
    ) -> None:
        content = _wav()
        source = Path(self.storage.name) / "source.wav"
        source.write_bytes(content)
        uploaded = self.client.upload_asset("namespace-a", source)
        asset_id = uploaded["asset_id"]
        self.assertIsInstance(asset_id, str)
        assert isinstance(asset_id, str)
        session = PostgresUploadRepository(self.connection).get_session(str(uploaded["session_id"]))
        self.assertEqual("verified", session.state.value)
        row = self.connection.execute(
            "SELECT a.state,b.sha256,b.storage_key FROM assets a JOIN blobs b ON b.id=a.blob_id WHERE a.id=%s",
            (asset_id,),
        ).fetchone()
        self.assertEqual(("verified", hashlib.sha256(content).hexdigest(), row[2]), tuple(row))
        with self.service.canonical_store.open_reader(str(row[2])) as stored:
            self.assertEqual(content, stored.read())

        command = self._command("cancel-idempotency", asset_id)
        submitted = self.client.submit(command)
        repeated = self.client.submit(command)
        self.assertEqual(submitted["job_id"], repeated["job_id"])
        job_id = str(submitted["job_id"])
        self.assertEqual("queued", self.client.status(job_id)["state"])
        self.assertTrue(
            any(
                sample.name == "nano_aural_api_requests_total" and sample.labels["route"] == "job"
                for sample in self.service.metrics.snapshot()
            )
        )
        self.assertEqual("job_created", self.client.events(job_id).events[0]["type"])
        self.assertEqual("cancelled", self.client.cancel(job_id)["state"])
        self.assertEqual("cancelled", self.client.status(job_id)["state"])
        self.assertEqual("cancel_requested", self.client.events(job_id).events[-1]["type"])
        with self.assertRaises(RemoteNotFound):
            self.foreign_client.status(job_id)

        winner = self.client.submit(self._command("publish-idempotency", asset_id))
        winner_job_id = str(winner["job_id"])
        worker = self.durable_repository.register_worker(
            WorkerRecord(str(uuid4()), self.deployment.deployment_id)
        )
        queue = PostgresLeaseQueue(self.connection)
        lease = queue.claim_next(worker.worker_id, 60)
        self.assertIsNotNone(lease)
        assert lease is not None
        ArtifactPublicationService(
            self.publication_repository,
            self.service.attempt_store,
            self.service.canonical_store,
            StreamingMediaArtifactValidator(WaveMediaProbe()),
            queue,
        ).publish(
            lease,
            (
                PublicationCandidate(
                    ArtifactKind.OUTPUT,
                    "audio/wav",
                    lambda: (content,),
                    len(content),
                    hashlib.sha256(content).hexdigest(),
                    len(content),
                ),
            ),
        )
        artifacts = self.client.artifacts(winner_job_id)
        self.assertEqual(1, len(artifacts))
        target = Path(self.downloads.name) / "downloaded.wav"
        self.assertEqual(
            target, self.client.download(winner_job_id, str(artifacts[0]["artifact_id"]), target)
        )
        self.assertEqual(content, target.read_bytes())
        with self.assertRaises(RemoteNotFound):
            self.foreign_client.artifacts(winner_job_id)

    def test_cpu_reference_worker_claims_runtime_and_publishes_visible_wav(self) -> None:
        deployment_id, worker_id = str(uuid4()), str(uuid4())
        config = ReferenceWorkerConfig(
            self._dsn(self.database),
            Path(self.storage.name) / "canonical",
            Path(self.storage.name) / "attempts",
            Path(self.storage.name) / "reference-workspaces",
            deployment_id,
            worker_id,
            lease_seconds=3,
            idle_seconds=0.01,
        )
        with ReferenceWorker.connect(config) as worker:
            command = {
                "namespace_id": "namespace-a",
                "idempotency_key": "cpu-reference-publish",
                "deployment_id": deployment_id,
                "request": {"operation": "cpu-reference-fake"},
                "inputs": [],
                "required_artifact_kinds": ["output"],
            }
            submitted = self.client.submit(command)
            job_id = str(submitted["job_id"])
            self.assertTrue(worker.run_once())
            self.assertFalse(worker.run_once())
            self.assertEqual("succeeded", self.client.status(job_id)["state"])
            artifacts = self.client.artifacts(job_id)
            self.assertEqual(1, len(artifacts))
            target = Path(self.downloads.name) / "cpu-reference.wav"
            self.client.download(job_id, str(artifacts[0]["artifact_id"]), target)
            with wave.open(str(target), "rb") as source:
                self.assertEqual(8000, source.getframerate())
                self.assertGreater(source.getnframes(), 0)

    def test_upload_recovery_has_one_global_limit_and_cleanup_progress(self) -> None:
        limit = 2
        staging_root = Path(self.storage.name) / "recovery-staging"
        staging = LocalStagingBlobStore(staging_root)
        repository = PostgresUploadRepository(self.connection)
        overdue_ids = []
        present_ids = set()
        for index in range(5):
            session_id = str(uuid4())
            repository.create_session(
                UploadSession(
                    session_id,
                    "namespace-a",
                    UploadMode.SINGLE,
                    1,
                    "staging/" + session_id,
                    datetime.now(timezone.utc) + timedelta(milliseconds=250 + index * 10),
                )
            )
            overdue_ids.append(session_id)
            # The first terminal page has no objects.  Cleanup evidence must
            # advance beyond it instead of starving the later existing bytes.
            if index >= limit:
                staging.write_stream(session_id, (b"x",))
                present_ids.add(session_id)
        time.sleep(0.4)

        current_id = str(uuid4())
        current = repository.create_session(
            UploadSession(
                current_id,
                "namespace-a",
                UploadMode.SINGLE,
                1,
                "staging/" + current_id,
                datetime.now(timezone.utc) + timedelta(minutes=5),
            )
        )
        staging.write_stream(current_id, (b"v",))
        uploaded = repository.mark_uploaded(current_id, current.version)
        repository.claim_verification(current_id, uploaded.version)
        staging.close()

        environment = {
            "NANO_AURAL_DATABASE_DSN": self._dsn(self.database),
            "NANO_AURAL_STAGING_BLOB_ROOT": str(staging_root),
        }
        for _ in range(10):
            before_expired = self.connection.execute(
                "SELECT count(*) FROM upload_sessions WHERE id=ANY(%s::uuid[]) AND state='expired'",
                (overdue_ids,),
            ).fetchone()[0]
            before_cleaned = self.connection.execute(
                "SELECT count(*) FROM upload_staging_cleanups WHERE upload_session_id=ANY(%s::uuid[])",
                (overdue_ids,),
            ).fetchone()[0]
            with patch.dict(os.environ, environment, clear=True):
                self.assertEqual(
                    0,
                    recovery_main(("--expire-uploads", "--limit", str(limit))),
                )
            after_expired = self.connection.execute(
                "SELECT count(*) FROM upload_sessions WHERE id=ANY(%s::uuid[]) AND state='expired'",
                (overdue_ids,),
            ).fetchone()[0]
            after_cleaned = self.connection.execute(
                "SELECT count(*) FROM upload_staging_cleanups WHERE upload_session_id=ANY(%s::uuid[])",
                (overdue_ids,),
            ).fetchone()[0]
            self.assertLessEqual(
                (after_expired - before_expired) + (after_cleaned - before_cleaned),
                limit,
            )
            if after_expired == len(overdue_ids) and after_cleaned == len(overdue_ids):
                break
        else:
            self.fail("bounded upload recovery did not converge")

        staged = LocalStagingBlobStore(staging_root)
        try:
            for session_id in present_ids:
                self.assertFalse(staged.exists("staging/" + session_id))
            self.assertTrue(staged.exists("staging/" + current_id))
        finally:
            staged.close()
        self.assertEqual("verifying", repository.get_session(current_id).state.value)
        self.assertIsNone(
            self.connection.execute(
                "SELECT 1 FROM upload_staging_cleanups WHERE upload_session_id=%s",
                (current_id,),
            ).fetchone()
        )

        # With all eligible rows ledgered, a repeat is a true no-op.
        with patch.dict(os.environ, environment, clear=True):
            self.assertEqual(
                0,
                recovery_main(("--expire-uploads", "--limit", str(limit))),
            )
        self.assertEqual(
            len(overdue_ids),
            self.connection.execute(
                "SELECT count(*) FROM upload_staging_cleanups WHERE upload_session_id=ANY(%s::uuid[])",
                (overdue_ids,),
            ).fetchone()[0],
        )

    def test_attempt_recovery_dry_run_and_sweep_preserve_active_grace_and_canonical(
        self,
    ) -> None:
        attempt_root = Path(self.storage.name) / "recovery-attempts"
        canonical_root = Path(self.storage.name) / "recovery-canonical"
        attempt_store = LocalAttemptArtifactStore(attempt_root)
        canonical_store = LocalBlobStore(canonical_root)
        canonical = canonical_store.put_immutable(b"canonical-must-survive")

        unknown_old_key = attempt_key(
            str(uuid4()), str(uuid4()), 1, ArtifactKind.OUTPUT, str(uuid4())
        )
        unknown_young_key = attempt_key(
            str(uuid4()), str(uuid4()), 1, ArtifactKind.OUTPUT, str(uuid4())
        )

        worker = self.durable_repository.register_worker(
            WorkerRecord(str(uuid4()), self.deployment.deployment_id)
        )
        active_job = self.durable_repository.create_job(
            "namespace-a",
            "active-orphan-recovery",
            {"operation": "recovery-proof"},
            self.deployment.deployment_id,
            (),
        )
        queue = PostgresLeaseQueue(self.connection)
        lease = queue.claim_next(worker.worker_id, 1200)
        self.assertIsNotNone(lease)
        assert lease is not None
        publication = self.publication_repository.reserve(
            lease,
            PublicationSpec(ArtifactKind.OUTPUT, "audio/wav", 64),
        )
        active_key = self.publication_repository.attempt_object_key(publication)
        active_object = attempt_store.put_stream(active_key, (b"active-attempt",))
        self.publication_repository.record_object(
            lease,
            publication.publication_id,
            publication.version,
            active_object.sha256,
            active_object.size_bytes,
        )
        # The known ACTIVE object is deliberately first in the persistent
        # inventory.  A limit-one scan must advance past it on the next run.
        attempt_store.put_stream(unknown_old_key, (b"old-orphan",))
        attempt_store.put_stream(unknown_young_key, (b"young-orphan",))

        old_timestamp = time.time() - 900
        os.utime(attempt_root / unknown_old_key, (old_timestamp, old_timestamp))
        os.utime(attempt_root / active_key, (old_timestamp, old_timestamp))
        outside_attempt_namespace = attempt_root / "operator-sentinel"
        outside_attempt_namespace.write_bytes(b"outside-attempt-namespace")
        os.utime(outside_attempt_namespace, (old_timestamp, old_timestamp))
        attempt_store.close()
        canonical_store.close()

        environment = {
            "NANO_AURAL_DATABASE_DSN": self._dsn(self.database),
            "NANO_AURAL_ATTEMPT_ARTIFACT_ROOT": str(attempt_root),
        }
        dry_run_counts = []
        for _ in range(2):
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stderr", new_callable=io.StringIO) as dry_run_log,
            ):
                self.assertEqual(
                    0,
                    recovery_main(
                        (
                            "--attempt-orphans-dry-run",
                            "--grace-seconds",
                            "300",
                            "--limit",
                            "1",
                        )
                    ),
                )
            dry_run_counts.append(json.loads(dry_run_log.getvalue())["count"])
        self.assertEqual([0, 1], dry_run_counts)
        self.assertTrue((attempt_root / unknown_old_key).is_file())
        self.assertTrue((attempt_root / unknown_young_key).is_file())
        self.assertTrue((attempt_root / active_key).is_file())

        sweep_counts = []
        for _ in range(2):
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("sys.stderr", new_callable=io.StringIO) as sweep_log,
            ):
                self.assertEqual(
                    0,
                    recovery_main(
                        (
                            "--sweep-attempt-orphans",
                            "--grace-seconds",
                            "300",
                            "--limit",
                            "1",
                        )
                    ),
                )
            sweep_counts.append(json.loads(sweep_log.getvalue())["count"])
        self.assertEqual([0, 1], sweep_counts)
        self.assertFalse((attempt_root / unknown_old_key).exists())
        self.assertTrue((attempt_root / unknown_young_key).is_file())
        self.assertTrue((attempt_root / active_key).is_file())
        self.assertTrue(outside_attempt_namespace.is_file())
        canonical_store = LocalBlobStore(canonical_root)
        try:
            with canonical_store.open_reader(canonical.storage_key) as source:
                self.assertEqual(b"canonical-must-survive", source.read())
        finally:
            canonical_store.close()
        self.assertEqual(active_job.job_id, lease.job_id)
        active_record = self.publication_repository.get(publication.publication_id)
        self.assertEqual("object_written", active_record.state.value)
        self.assertIsNone(active_record.stale_since)

        # A real PostgreSQL cleanup page exactly equal to the limit must return
        # without issuing a prohibited LIMIT 0 orphan query or touching local
        # inventory.  Use a future inspection cutoff only to avoid a wall-clock
        # wait in this isolated regression.
        terminal = self.publication_repository.reject(
            lease,
            publication.publication_id,
            active_record.version,
            "bounded_dry_run_regression",
        )
        empty_inventory_root = Path(self.storage.name) / "zero-slot-inventory"
        self.assertEqual(
            1,
            _dry_run_attempts(
                self.connection,
                empty_inventory_root,
                datetime.now(timezone.utc) + timedelta(seconds=1),
                1,
            ),
        )
        self.assertEqual("rejected", terminal.state.value)
        self.assertFalse(
            (empty_inventory_root / ".attempt-inventory-orphan_dry_run.cursor").exists()
        )
