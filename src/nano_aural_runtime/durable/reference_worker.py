"""Executable CPU-only durable reference worker for Phase 3E operations.

The process deliberately registers the Core ``FakeAudioAdapter`` as a visible
test deployment.  It proves the queue -> Runtime -> fenced publication loop;
it is not a real model and must never be presented as a model-quality or GPU
validation path.
"""

from __future__ import annotations

import argparse
import io
import json
import math
import os
import signal
import sys
import wave
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Any, Callable, Mapping, Optional, Sequence, Tuple
from uuid import UUID

from nano_aural_runtime import (
    AdapterRegistry,
    FakeAudioAdapter,
    InvocationResult,
    ModelDeployment,
    ModelInvocation,
    ProducedArtifact,
    Runtime,
)

from .artifact_publication import ArtifactPublicationService, PublicationProgressHook
from .artifact_storage import LocalAttemptArtifactStore
from .artifact_validation import StreamingMediaArtifactValidator
from .domain import DeploymentRecord, JobRecord
from .materialization import MaterializedInput
from .observability import DurableMetrics, StructuredEventLogger
from .publication import PostgresPublicationRepository
from .publishing_worker import DurablePublishingWorker, SingleOutputArtifactPlanner
from .queue import PostgresLeaseQueue
from .runtime_worker import DurableRuntimeWorker
from .storage import LocalBlobStore
from .uploads import WaveMediaProbe

_FINGERPRINT = "nano-aural-cpu-reference-fake-v1"


def _positive_int(value: object, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("{0} must be a positive integer".format(name))
    return value


def _positive_float(value: object, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(float(value))
        or float(value) <= 0
    ):
        raise ValueError("{0} must be positive".format(name))
    return float(value)


def _absolute_path(value: str, name: str) -> Path:
    path = Path(value)
    if not value or not path.is_absolute():
        raise ValueError("{0} must be an absolute operator-owned path".format(name))
    return path


def _uuid(value: str, name: str) -> str:
    try:
        normalized = str(UUID(value))
    except (TypeError, ValueError, AttributeError) as error:
        raise ValueError("{0} must be a canonical UUID".format(name)) from error
    if normalized != value:
        raise ValueError("{0} must be a canonical UUID".format(name))
    return normalized


def _connect(database_dsn: str) -> Any:
    try:
        import psycopg  # type: ignore[import-not-found]
    except ImportError as error:
        raise RuntimeError(
            "psycopg is required for the CPU reference worker; install the postgres runtime dependency"
        ) from error
    return psycopg.connect(database_dsn, autocommit=True)


def _silent_wav() -> bytes:
    output = io.BytesIO()
    with wave.open(output, "wb") as sink:
        sink.setnchannels(1)
        sink.setsampwidth(2)
        sink.setframerate(8000)
        sink.writeframes(b"\x00\x00" * 400)
    return output.getvalue()


@dataclass(frozen=True)
class ReferenceWorkerConfig:
    database_dsn: str = field(repr=False)
    canonical_blob_root: Path
    attempt_artifact_root: Path
    workspace_root: Path
    deployment_id: str
    worker_id: str
    lease_seconds: int = 10
    idle_seconds: float = 1.0
    max_output_bytes: int = 4 * 1024 * 1024

    def __post_init__(self) -> None:
        if not isinstance(self.database_dsn, str) or not self.database_dsn.strip():
            raise ValueError("database_dsn must be non-empty")
        for value, name in (
            (self.canonical_blob_root, "canonical_blob_root"),
            (self.attempt_artifact_root, "attempt_artifact_root"),
            (self.workspace_root, "workspace_root"),
        ):
            if not isinstance(value, Path) or not value.is_absolute():
                raise ValueError("{0} must be an absolute path".format(name))
        _uuid(self.deployment_id, "deployment_id")
        _uuid(self.worker_id, "worker_id")
        _positive_int(self.lease_seconds, "lease_seconds")
        _positive_float(self.idle_seconds, "idle_seconds")
        _positive_int(self.max_output_bytes, "max_output_bytes")

    @classmethod
    def from_environment(
        cls, environ: Optional[Mapping[str, str]] = None
    ) -> "ReferenceWorkerConfig":
        source = os.environ if environ is None else environ
        names = (
            "NANO_AURAL_DATABASE_DSN",
            "NANO_AURAL_CANONICAL_BLOB_ROOT",
            "NANO_AURAL_ATTEMPT_ARTIFACT_ROOT",
            "NANO_AURAL_WORKSPACE_ROOT",
            "NANO_AURAL_REFERENCE_DEPLOYMENT_ID",
            "NANO_AURAL_REFERENCE_WORKER_ID",
        )
        missing = [name for name in names if not source.get(name)]
        if missing:
            raise ValueError(
                "missing required operator configuration: {0}".format(", ".join(missing))
            )
        return cls(
            source[names[0]],
            _absolute_path(source[names[1]], names[1]),
            _absolute_path(source[names[2]], names[2]),
            _absolute_path(source[names[3]], names[3]),
            _uuid(source[names[4]], names[4]),
            _uuid(source[names[5]], names[5]),
            int(source.get("NANO_AURAL_REFERENCE_LEASE_SECONDS", "10")),
            float(source.get("NANO_AURAL_REFERENCE_IDLE_SECONDS", "1")),
            int(source.get("NANO_AURAL_REFERENCE_MAX_OUTPUT_BYTES", str(4 * 1024 * 1024))),
        )


class _ReferenceInvocationBuilder:
    def __init__(self, adapter: FakeAudioAdapter) -> None:
        self._adapter = adapter

    def core_deployment(self, deployment: DeploymentRecord) -> ModelDeployment:
        if deployment.adapter_id != "fake" or deployment.fingerprint != _FINGERPRINT:
            raise ValueError("durable deployment is not the CPU reference fake")
        return ModelDeployment(
            deployment.deployment_id,
            self._adapter.descriptor,
            deployment.fingerprint,
            {"reference_process": True},
        )

    def build(
        self,
        deployment: DeploymentRecord,
        job: JobRecord,
        inputs: Tuple[MaterializedInput, ...],
    ) -> ModelInvocation:
        del deployment
        attempt_id = job.current_attempt_id
        if attempt_id is None:
            raise ValueError("reference invocation requires a current attempt")
        return ModelInvocation(
            "reference-" + attempt_id,
            "cpu_reference_fake",
            {
                "input_count": len(inputs),
                "input_roles": tuple(item.role for item in inputs),
                "request_sha256": job.request_sha256,
            },
        )


class ReferenceWorker:
    """Own every connection/store/session in one CPU reference process."""

    def __init__(
        self,
        config: ReferenceWorkerConfig,
        connection: Any,
        connector: Callable[[str], Any] = _connect,
    ) -> None:
        self.config = config
        self._connection = connection
        self._connector = connector
        self._closed = False
        self.metrics = DurableMetrics()
        self.events = StructuredEventLogger(sys.stderr)
        try:
            if hasattr(connection, "autocommit"):
                connection.autocommit = True
            self.canonical_store = LocalBlobStore(config.canonical_blob_root)
            self.attempt_store = LocalAttemptArtifactStore(config.attempt_artifact_root)
            self.queue = PostgresLeaseQueue(connection)
            self._ensure_reference_records()
            output = _silent_wav()
            adapter = FakeAudioAdapter(
                lambda _session, invocation, _context: InvocationResult(
                    invocation.invocation_id,
                    (ProducedArtifact("cpu-reference.wav", "audio/wav", output),),
                )
            )
            registry = AdapterRegistry()
            registry.register(adapter)

            def monitor_queue() -> PostgresLeaseQueue:
                return PostgresLeaseQueue(self._connector(config.database_dsn))

            self.runtime_worker = DurableRuntimeWorker(
                self.queue,
                config.worker_id,
                config.lease_seconds,
                Runtime(registry),
                _ReferenceInvocationBuilder(adapter),
                self.canonical_store,
                config.workspace_root,
                WaveMediaProbe(),
                monitor_queue,
            )
            publications = PostgresPublicationRepository(connection)

            def publication_factory(
                progress_hook: PublicationProgressHook,
            ) -> ArtifactPublicationService:
                return ArtifactPublicationService(
                    publications,
                    self.attempt_store,
                    self.canonical_store,
                    StreamingMediaArtifactValidator(WaveMediaProbe()),
                    self.queue,
                    progress_hook=progress_hook,
                )

            self.worker = DurablePublishingWorker(
                self.runtime_worker,
                self.queue,
                monitor_queue,
                publication_factory,
                SingleOutputArtifactPlanner(config.max_output_bytes),
                lease_seconds=config.lease_seconds,
            )
        except BaseException:
            self.close()
            raise

    @classmethod
    def connect(
        cls,
        config: ReferenceWorkerConfig,
        connector: Callable[[str], Any] = _connect,
    ) -> "ReferenceWorker":
        connection = connector(config.database_dsn)
        try:
            return cls(config, connection, connector)
        except BaseException:
            close = getattr(connection, "close", None)
            if callable(close):
                close()
            raise

    def _ensure_reference_records(self) -> None:
        manifest = {"cpu_reference_fake": True, "model_quality": False}
        with self._connection.transaction():
            self._connection.execute(
                """INSERT INTO model_deployments
                   (id,name,adapter_id,fingerprint,manifest,state)
                   VALUES (%s,%s,'fake',%s,%s::jsonb,'ready')
                   ON CONFLICT (id) DO NOTHING""",
                (
                    self.config.deployment_id,
                    "CPU reference fake " + self.config.deployment_id,
                    _FINGERPRINT,
                    json.dumps(manifest, sort_keys=True),
                ),
            )
            deployment = self._connection.execute(
                """SELECT adapter_id,fingerprint,manifest,state FROM model_deployments
                   WHERE id=%s FOR UPDATE""",
                (self.config.deployment_id,),
            ).fetchone()
            if deployment is None or tuple(deployment) != (
                "fake",
                _FINGERPRINT,
                manifest,
                "ready",
            ):
                raise RuntimeError("reference deployment id has conflicting durable identity")
            self._connection.execute(
                """INSERT INTO workers (id,model_deployment_id,state)
                   VALUES (%s,%s,'ready') ON CONFLICT (id) DO NOTHING""",
                (self.config.worker_id, self.config.deployment_id),
            )
            worker = self._connection.execute(
                "SELECT model_deployment_id,state FROM workers WHERE id=%s FOR UPDATE",
                (self.config.worker_id,),
            ).fetchone()
            if worker is None or str(worker[0]) != self.config.deployment_id:
                raise RuntimeError("reference worker id has conflicting durable identity")
            if str(worker[1]) not in ("ready", "busy"):
                raise RuntimeError("reference worker is not eligible to recover or claim work")

    def run_once(self) -> bool:
        reaped = self.queue.reap_expired()
        if reaped:
            self.metrics.increment(
                "nano_aural_lease_events_total",
                {"event": "reaped", "outcome": "success"},
                reaped,
            )
        return self.worker.run_once() is not None

    def reap_only(self) -> int:
        return self.queue.reap_expired()

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        first_error: Optional[BaseException] = None
        for resource in (
            getattr(self, "runtime_worker", None),
            getattr(self, "attempt_store", None),
            getattr(self, "canonical_store", None),
            getattr(self, "queue", None),
        ):
            close = getattr(resource, "close", None)
            if callable(close):
                try:
                    close()
                except BaseException as error:
                    if first_error is None:
                        first_error = error
        if first_error is not None:
            raise RuntimeError("CPU reference worker resource close failed") from first_error

    def __enter__(self) -> "ReferenceWorker":
        return self

    def __exit__(self, *_unused: object) -> None:
        self.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the CPU-only fake durable publication reference worker.",
        epilog="This process proves infrastructure recovery only; it is not real-model or GPU validation.",
    )
    actions = parser.add_mutually_exclusive_group()
    actions.add_argument("--run-once", action="store_true", help="claim at most one job and exit")
    actions.add_argument(
        "--reap-only", action="store_true", help="reap expired attempts without claiming work"
    )
    options = parser.parse_args(argv)
    try:
        config = ReferenceWorkerConfig.from_environment()
        worker = ReferenceWorker.connect(config)
    except ValueError:
        sys.stderr.write(
            "CPU reference worker configuration failed; check required NANO_AURAL_* settings\n"
        )
        return 2
    except RuntimeError:
        sys.stderr.write("CPU reference worker configuration failed; check runtime dependencies\n")
        return 2
    except Exception:
        sys.stderr.write("CPU reference worker could not connect to PostgreSQL\n")
        return 2
    stop = Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop.set()

    if not options.run_once and not options.reap_only:
        signal.signal(signal.SIGTERM, request_stop)
        signal.signal(signal.SIGINT, request_stop)
    try:
        with worker:
            worker.events.emit(component="worker", outcome="started")
            if options.reap_only:
                reaped = worker.reap_only()
                worker.events.emit(component="queue", outcome="success", numeric={"count": reaped})
                return 0
            if options.run_once:
                worker.run_once()
                return 0
            while not stop.is_set():
                worked = worker.run_once()
                if not worked:
                    stop.wait(config.idle_seconds)
    except Exception:
        try:
            worker.events.emit(component="worker", outcome="failed", reason_code="worker_process")
        except Exception:
            pass
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
