"""Standard-library-only smoke executed from a true P5C omission snapshot."""

from __future__ import annotations

import importlib
import io
import json
import sys
import tempfile
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from typing import Mapping, Optional, Sequence
from unittest.mock import patch


def _bootstrap() -> tuple[Path, Path, frozenset[str]]:
    if len(sys.argv) != 4:
        raise RuntimeError("expected snapshot root, forbidden repository root, and package list")
    snapshot = Path(sys.argv[1]).resolve()
    forbidden = Path(sys.argv[2]).resolve()
    expected = frozenset(filter(None, sys.argv[3].split(",")))
    if sys.flags.no_site != 1:
        raise RuntimeError("omission smoke requires Python -S isolation")
    if any(str(forbidden) in entry for entry in sys.path):
        raise RuntimeError("repository path leaked into isolated sys.path")
    if "PYTHONPATH" in __import__("os").environ:
        raise RuntimeError("PYTHONPATH must be absent")
    source = snapshot / "src"
    if not source.is_dir():
        raise RuntimeError("snapshot source tree is unavailable")
    sys.path.insert(0, str(snapshot))
    sys.path.insert(0, str(source))
    return snapshot, forbidden, expected


SNAPSHOT, FORBIDDEN_ROOT, EXPECTED_INTEGRATIONS = _bootstrap()

from nano_aural_runtime import (  # noqa: E402
    AdapterRegistry,
    EchoAdapter,
    ExecutionContext,
    ModelDeployment,
    ModelDescriptor,
    ModelInvocation,
    Runtime,
)
from nano_aural_runtime.durable.api import ApiRequest, ApplicationApi  # noqa: E402
from nano_aural_runtime.durable.application import (  # noqa: E402
    ApplicationService,
    AuthenticationFailed,
    Principal,
    SubmitJob,
)
from nano_aural_runtime.durable.domain import (  # noqa: E402
    ArtifactKind,
    DeploymentRecord,
    EventType,
    JobEventRecord,
    JobInput,
    JobRecord,
    JobState,
    WorkerRecord,
    canonical_request_sha256,
)
from nano_aural_runtime.durable.errors import NotFoundError  # noqa: E402
from nano_aural_runtime.durable.fake_worker import FakeRuntimeWorker  # noqa: E402
from nano_aural_runtime.durable.repository import InMemoryDurableRepository  # noqa: E402
from nano_aural_runtime.durable.storage import LocalBlobStore  # noqa: E402
from nano_aural_runtime_controlfoley.adapter import ControlFoleyAdapter  # noqa: E402
from nano_aural_runtime_controlfoley.cli import main as local_cli_main  # noqa: E402
from nano_aural_runtime_workers.controlfoley import (  # noqa: E402
    ControlFoleyDurableInvocationBuilder,
)


class _Runner:
    def validate(self, configuration: Mapping[str, object]) -> Mapping[str, object]:
        return {
            "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
            "source_revision": configuration["source_revision"],
            "variant": configuration["variant"],
            "precision": configuration["precision"],
            "checkpoint_sha256": configuration["checkpoint_sha256"],
            "checkpoint_size_bytes": 1,
            "external_weight_sha256": {"ext_weights/v1-44.pth": "c" * 64},
        }

    def invoke(
        self,
        configuration: Mapping[str, object],
        request: object,
        context: ExecutionContext,
    ) -> tuple[bytes, Mapping[str, object]]:
        del request
        context.cancellation_token.raise_if_cancelled()
        content = b"FLAC-p5c-headless"
        return content, {
            "sha256": sha256(content).hexdigest(),
            "size_bytes": len(content),
            "source_revision": configuration["source_revision"],
            "deployment_manifest_sha256": configuration["deployment_manifest_sha256"],
            "variant": configuration["variant"],
            "precision": configuration["precision"],
            "checkpoint_sha256": configuration["checkpoint_sha256"],
            "format": "flac",
            "worker": {
                "wall_time_seconds": 0.01,
                "peak_allocated_bytes": 0,
                "peak_reserved_bytes": 0,
            },
        }


def _controlfoley_configuration() -> dict[str, str]:
    return {
        "manifest_path": "unused",
        "deployment_manifest_sha256": "a" * 64,
        "source_dir": "unused",
        "weights_dir": "unused",
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": "6858cd12a48d141201e3266e7abe1f38357a133e",
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": "b" * 64,
    }


def _core_smoke() -> None:
    adapter = EchoAdapter()
    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    session = runtime.load(
        ModelDeployment(
            "echo-deployment",
            ModelDescriptor("echo", "echo", "1"),
            "echo-fingerprint",
        )
    )
    result = runtime.invoke(session, ModelInvocation("echo-1", "echo", {"value": "headless"}))
    assert result.artifacts[0].content == b"headless"
    runtime.unload(session)


def _local_cli_smoke() -> None:
    adapter = ControlFoleyAdapter(_Runner())
    configuration = _controlfoley_configuration()
    deployment = ModelDeployment(
        "controlfoley-test",
        adapter.descriptor,
        configuration["deployment_manifest_sha256"],
        configuration,
    )
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        output = root / "result.flac"
        arguments = (
            "controlfoley",
            "local",
            "--manifest",
            str(root / "manifest.json"),
            "--source-dir",
            str(root),
            "--weights-dir",
            str(root),
            "--task",
            "T2A",
            "--prompt",
            "headless",
            "--output",
            str(output),
        )
        with (
            patch(
                "nano_aural_runtime_controlfoley.cli.ControlFoleyAdapter",
                return_value=adapter,
            ),
            patch(
                "nano_aural_runtime_controlfoley.cli.controlfoley_local_deployment",
                return_value=deployment,
            ),
        ):
            assert local_cli_main(arguments) == 0
        assert output.read_bytes() == b"FLAC-p5c-headless"


class _Repository:
    def __init__(self) -> None:
        self.jobs: dict[str, JobRecord] = {}

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
        job = JobRecord(
            "job-1",
            namespace_id,
            idempotency_key,
            canonical_request_sha256(request, deployment_id, normalized_inputs, required),
            request,
            deployment_id,
            normalized_inputs,
            required,
        )
        self.jobs[job.job_id] = job
        return job

    def get_job(self, job_id: str) -> JobRecord:
        try:
            return self.jobs[job_id]
        except KeyError:
            raise NotFoundError("job not found") from None

    def request_cancel(self, job_id: str) -> JobRecord:
        job = self.get_job(job_id)
        cancelled = replace(job, state=JobState.CANCELLED, cancel_requested=True)
        self.jobs[job_id] = cancelled
        return cancelled

    def list_events(
        self, job_id: str, after_event_id: Optional[str], limit: int
    ) -> Sequence[JobEventRecord]:
        self.get_job(job_id)
        if after_event_id is not None:
            return ()
        return (JobEventRecord("1", job_id, EventType.JOB_CREATED),)[:limit]


class _Authorization:
    def has_scope(self, principal: Principal, scope: str) -> bool:
        del principal
        return scope in {"jobs:submit", "jobs:read", "jobs:cancel", "artifacts:read"}

    def allows_namespace(self, principal: Principal, namespace_id: str) -> bool:
        return principal.subject == "alice" and namespace_id == "tenant-a"


class _Artifacts:
    def list_visible(self, job_id: str) -> tuple[object, ...]:
        del job_id
        return ()

    def open_reader(self, artifact: object) -> io.BytesIO:
        del artifact
        return io.BytesIO()


class _Authenticator:
    def authenticate(self, authorization: str) -> Principal:
        if authorization != "Bearer alice":
            raise AuthenticationFailed("invalid authorization")
        return Principal("alice")


def _service_and_api_smoke() -> None:
    repository = _Repository()
    service = ApplicationService(repository, _Artifacts(), _Authorization())  # type: ignore[arg-type]
    principal = Principal("alice")
    command = SubmitJob("tenant-a", "idem-1", "deployment-1", {"operation": "generate"})
    created = service.submit(principal, command)
    assert service.get_job(principal, created.job_id).state == JobState.QUEUED
    assert service.events(principal, created.job_id).events[0].event_type == EventType.JOB_CREATED

    api = ApplicationApi(service, _Authenticator())  # type: ignore[arg-type]
    body = json.dumps(
        {
            "namespace_id": "tenant-a",
            "idempotency_key": "idem-2",
            "deployment_id": "deployment-1",
            "request": {"operation": "generate"},
            "inputs": [],
            "required_artifact_kinds": ["output"],
        }
    ).encode("utf-8")
    response = api.handle(
        ApiRequest(
            "POST",
            "/v1/jobs",
            {
                "Authorization": "Bearer alice",
                "Content-Length": str(len(body)),
                "Content-Type": "application/json",
                "Idempotency-Key": "idem-2",
            },
            body,
        )
    )
    assert response.status == 201
    with response.body:
        payload = json.loads(response.body.read())
    assert payload["state"] == "queued"


def _worker_smoke() -> None:
    repository = InMemoryDurableRepository()
    repository.register_deployment(DeploymentRecord("fake-deployment", "CPU fake", "fake", "f"))
    repository.register_worker(WorkerRecord("worker-1", "fake-deployment"))
    job = repository.create_job(
        "tenant-a",
        "worker-1",
        {"operation": "zero-input"},
        "fake-deployment",
        (),
    )
    with tempfile.TemporaryDirectory() as directory:
        store = LocalBlobStore(Path(directory))
        try:
            state = FakeRuntimeWorker(repository, store).execute(job.job_id, "worker-1")
        finally:
            store.close()
    assert state == JobState.SUCCEEDED
    assert len(repository.list_artifacts(job.job_id)) == 1

    adapter = ControlFoleyAdapter(_Runner())
    configuration = _controlfoley_configuration()
    manifest = {
        name: configuration[name]
        for name in (
            "deployment_manifest_sha256",
            "upstream_repository",
            "source_revision",
            "variant",
            "precision",
            "checkpoint_sha256",
        )
    }
    deployment = DeploymentRecord(
        "controlfoley-deployment",
        "ControlFoley",
        adapter.descriptor.adapter_id,
        configuration["deployment_manifest_sha256"],
        manifest=manifest,
    )
    durable_job = JobRecord(
        "controlfoley-job",
        "tenant-a",
        "controlfoley-worker-1",
        "d" * 64,
        {
            "task": "T2A",
            "prompt": "headless",
            "duration_seconds": 8.0,
            "num_steps": 25,
            "guidance_scale": 4.5,
            "seed": 42,
        },
        deployment.deployment_id,
        (),
    )
    invocation = ControlFoleyDurableInvocationBuilder(adapter, configuration).build(
        deployment, durable_job, ()
    )
    assert invocation.operation == "upstream_parity"


def _assert_snapshot_imports() -> None:
    headless_prefixes = (
        "nano_aural_runtime",
        "nano_aural_runtime_controlfoley",
        "nano_aural_runtime_remote",
        "nano_aural_runtime_workers",
    )
    assert not any(
        name == "integrations" or name.startswith("integrations.") for name in sys.modules
    )
    assert not any(name == "comfy" or name.startswith("comfy.") for name in sys.modules)
    for name, module in tuple(sys.modules.items()):
        if not name.startswith(headless_prefixes):
            continue
        raw = getattr(module, "__file__", None)
        if isinstance(raw, str):
            path = Path(raw).resolve()
            assert str(FORBIDDEN_ROOT) not in str(path)
            path.relative_to((SNAPSHOT / "src").resolve())

    all_optional = {"comfyui", "comfyui_remote"}
    for package in sorted(all_optional):
        qualified = "integrations." + package
        if package in EXPECTED_INTEGRATIONS:
            imported = importlib.import_module(qualified)
            imported_file = imported.__file__
            assert isinstance(imported_file, str)
            assert str(FORBIDDEN_ROOT) not in str(Path(imported_file).resolve())
        else:
            try:
                importlib.import_module(qualified)
            except ModuleNotFoundError:
                pass
            else:
                raise AssertionError("omitted optional integration was importable: " + qualified)
    assert not any(name == "comfy" or name.startswith("comfy.") for name in sys.modules)


_core_smoke()
_local_cli_smoke()
_service_and_api_smoke()
_worker_smoke()
_assert_snapshot_imports()
print("P5C_HEADLESS_OK")
