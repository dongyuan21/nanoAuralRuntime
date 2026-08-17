# pyright: reportMissingImports=false
from __future__ import annotations

import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from nano_aural_runtime.durable.domain import DeploymentRecord, JobRecord
from nano_aural_runtime.durable.materialization import MaterializedInput
from nano_aural_runtime.durable.uploads import MediaInfo
from nano_aural_runtime_controlfoley.adapter import ControlFoleyAdapter
from nano_aural_runtime_controlfoley.tasks import ControlFoleyTaskKind
from nano_aural_runtime_workers.controlfoley import (
    ControlFoleyDurableBindingError,
    ControlFoleyDurableInvocationBuilder,
)


def _operator_configuration() -> dict[str, str]:
    return {
        "manifest_path": "/operator/manifest.json",
        "deployment_manifest_sha256": "a" * 64,
        "source_dir": "/operator/source",
        "weights_dir": "/operator/weights",
        "upstream_repository": "https://github.com/xiaomi-research/controlfoley",
        "source_revision": "6858cd12a48d141201e3266e7abe1f38357a133e",
        "variant": "large_44k",
        "precision": "fp32",
        "checkpoint_sha256": "b" * 64,
    }


def _identity(configuration: dict[str, str]) -> dict[str, str]:
    return {
        key: configuration[key]
        for key in (
            "deployment_manifest_sha256",
            "upstream_repository",
            "source_revision",
            "variant",
            "precision",
            "checkpoint_sha256",
        )
    }


def _deployment(configuration: dict[str, str]) -> DeploymentRecord:
    return DeploymentRecord(
        str(uuid4()),
        "sealed-controlfoley",
        "controlfoley",
        configuration["deployment_manifest_sha256"],
        manifest=_identity(configuration),
    )


def _job(deployment: DeploymentRecord, task: ControlFoleyTaskKind) -> JobRecord:
    prompt = None if task in (ControlFoleyTaskKind.V2A, ControlFoleyTaskKind.AC_V2A) else "rain"
    return JobRecord(
        str(uuid4()),
        "tenant",
        "key-" + task.value,
        "c" * 64,
        {
            "task": task.value,
            "prompt": prompt,
            "duration_seconds": 8.0,
            "num_steps": 25,
            "guidance_scale": 4.5,
            "seed": 7,
        },
        deployment.deployment_id,
        (),
    )


def _input(root: Path, role: str, media_type: str) -> MaterializedInput:
    workspace = root / "attempt-server"
    workspace.mkdir(exist_ok=True)
    path = workspace / ("input-" + role + ".bin")
    path.write_bytes(b"server-owned")
    return MaterializedInput(
        role,
        str(uuid4()),
        str(uuid4()),
        "d" * 64,
        path.stat().st_size,
        path,
        MediaInfo(media_type, 1.0),
    )


@pytest.mark.parametrize(
    ("task", "roles"),
    (
        (ControlFoleyTaskKind.V2A, ("video",)),
        (ControlFoleyTaskKind.TV2A, ("video",)),
        (ControlFoleyTaskKind.TC_V2A, ("video",)),
        (ControlFoleyTaskKind.AC_V2A, ("video", "reference_audio")),
        (ControlFoleyTaskKind.T2A, ()),
    ),
)
def test_builds_exact_task_shapes_from_server_materializations(
    tmp_path: Path, task: ControlFoleyTaskKind, roles: tuple[str, ...]
) -> None:
    configuration = _operator_configuration()
    builder = ControlFoleyDurableInvocationBuilder(ControlFoleyAdapter(), configuration)
    deployment = _deployment(configuration)
    supplied = tuple(
        _input(tmp_path, role, "video/mp4" if role == "video" else "audio/wav") for role in roles
    )
    invocation = builder.build(deployment, _job(deployment, task), supplied)
    assert invocation.operation == "upstream_parity"
    assert invocation.inputs["task"] == task.value
    assert invocation.inputs["num_steps"] == 25
    assert invocation.inputs["guidance_scale"] == 4.5
    assert invocation.inputs["seed"] == 7
    assert (invocation.inputs["video_path"] is not None) is ("video" in roles)
    assert (invocation.inputs["reference_audio_path"] is not None) is ("reference_audio" in roles)
    assert (invocation.inputs["prompt"] is not None) is (
        task not in (ControlFoleyTaskKind.V2A, ControlFoleyTaskKind.AC_V2A)
    )


def test_rejects_missing_extra_and_wrong_media_roles(tmp_path: Path) -> None:
    configuration = _operator_configuration()
    builder = ControlFoleyDurableInvocationBuilder(ControlFoleyAdapter(), configuration)
    deployment = _deployment(configuration)
    job = _job(deployment, ControlFoleyTaskKind.AC_V2A)
    video = _input(tmp_path, "video", "video/mp4")
    audio = _input(tmp_path, "reference_audio", "audio/wav")
    with pytest.raises(ControlFoleyDurableBindingError):
        builder.build(deployment, job, (video,))
    with pytest.raises(ControlFoleyDurableBindingError):
        builder.build(deployment, job, (video, audio, _input(tmp_path, "extra", "audio/wav")))
    with pytest.raises(ControlFoleyDurableBindingError):
        builder.build(deployment, job, (_input(tmp_path, "video", "audio/wav"), audio))


def test_request_parameters_and_path_bearing_fields_are_strict(tmp_path: Path) -> None:
    configuration = _operator_configuration()
    builder = ControlFoleyDurableInvocationBuilder(ControlFoleyAdapter(), configuration)
    deployment = _deployment(configuration)
    job = _job(deployment, ControlFoleyTaskKind.TV2A)
    video = _input(tmp_path, "video", "video/mp4")
    bad_request = dict(job.request)
    bad_request["source_dir"] = "/private/not-allowed"
    bad_job = JobRecord(
        job.job_id,
        job.namespace_id,
        job.idempotency_key,
        job.request_sha256,
        bad_request,
        job.deployment_id,
        job.inputs,
    )
    with pytest.raises(ControlFoleyDurableBindingError) as error:
        builder.build(deployment, bad_job, (video,))
    assert "/private/not-allowed" not in str(error.value)
    for name, value in (
        ("duration_seconds", 0),
        ("num_steps", 24),
        ("guidance_scale", 4.0),
        ("seed", True),
    ):
        changed = dict(job.request)
        changed[name] = value
        invalid = JobRecord(
            str(uuid4()),
            job.namespace_id,
            job.idempotency_key + name,
            job.request_sha256,
            changed,
            job.deployment_id,
            job.inputs,
        )
        with pytest.raises((TypeError, ValueError)):
            builder.build(deployment, invalid, (video,))


def test_binds_operator_configuration_to_exact_durable_identity() -> None:
    configuration = _operator_configuration()
    builder = ControlFoleyDurableInvocationBuilder(ControlFoleyAdapter(), configuration)
    deployment = _deployment(configuration)
    core = builder.core_deployment(deployment)
    assert core.deployment_id == deployment.deployment_id
    assert core.fingerprint == deployment.fingerprint
    with pytest.raises(TypeError):
        builder.operator_configuration["source_dir"] = "mutated"  # type: ignore[index]
    mismatched = DeploymentRecord(
        deployment.deployment_id,
        deployment.name,
        deployment.adapter_id,
        "e" * 64,
        manifest=_identity(configuration),
    )
    with pytest.raises(ControlFoleyDurableBindingError):
        builder.core_deployment(mismatched)
    changed_identity = dict(_identity(configuration))
    changed_identity["precision"] = "bf16"
    with pytest.raises(ControlFoleyDurableBindingError):
        builder.core_deployment(
            DeploymentRecord(
                deployment.deployment_id,
                deployment.name,
                deployment.adapter_id,
                deployment.fingerprint,
                manifest=changed_identity,
            )
        )


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_controlfoley_durable_runtime_worker_gpu_smoke_is_explicitly_conditional() -> None:
    """Exercise P3C claim/materialization through P3D Runtime without publication.

    The sealed JSON is intentionally operator-local.  It identifies a private
    PostgreSQL T2A job with zero binary inputs, a canonical root, and the sealed
    source/weight deployment; this repository never supplies source paths,
    weights, media, credentials, or a synthetic result.
    """

    raw = os.environ.get("CONTROLFOLEY_P3D_GPU_CONFIG")
    if raw is None:
        pytest.skip("CONTROLFOLEY_P3D_GPU_CONFIG with sealed worker inputs is not set")
    assert raw is not None
    try:
        config = json.loads(raw)
    except json.JSONDecodeError as error:
        pytest.fail("CONTROLFOLEY_P3D_GPU_CONFIG is not valid JSON: {0}".format(error.msg))
    required = {
        "postgres_dsn",
        "worker_id",
        "job_id",
        "lease_seconds",
        "canonical_root",
        "workspace_root",
        "operator_configuration",
    }
    if not isinstance(config, dict) or set(config) != required:
        pytest.fail("CONTROLFOLEY_P3D_GPU_CONFIG is incomplete")
    try:
        import psycopg

        from nano_aural_runtime import AdapterRegistry, Runtime
        from nano_aural_runtime.durable.queue import PostgresLeaseQueue
        from nano_aural_runtime.durable.runtime_worker import DurableRuntimeWorker
        from nano_aural_runtime.durable.storage import LocalBlobStore
        from nano_aural_runtime.durable.uploads import WaveMediaProbe
    except ImportError as error:
        pytest.fail("P3D worker host is missing required test dependencies: {0}".format(error.name))
    if not isinstance(config["operator_configuration"], dict):
        pytest.fail("CONTROLFOLEY_P3D_GPU_CONFIG operator configuration is invalid")
    adapter = ControlFoleyAdapter()
    binding = ControlFoleyDurableInvocationBuilder(adapter, config["operator_configuration"])

    registry = AdapterRegistry()
    registry.register(adapter)
    runtime = Runtime(registry)
    connection = psycopg.connect(config["postgres_dsn"])
    try:
        queue = PostgresLeaseQueue(connection)
        next_job = connection.execute(
            """SELECT j.id,j.request_json,
                      (SELECT count(*) FROM job_inputs i WHERE i.job_id=j.id)
               FROM workers w JOIN jobs j ON j.model_deployment_id=w.model_deployment_id
               WHERE w.id=%s AND j.state='queued' AND j.cancel_requested_at IS NULL
                 AND (j.retry_not_before IS NULL OR j.retry_not_before<=clock_timestamp())
               ORDER BY j.created_at,j.id LIMIT 1""",
            (config["worker_id"],),
        ).fetchone()
        assert next_job is not None
        assert str(next_job[0]) == config["job_id"]
        assert next_job[1]["task"] == "T2A"
        assert next_job[2] == 0
        worker = DurableRuntimeWorker(
            queue,
            config["worker_id"],
            config["lease_seconds"],
            runtime,
            binding,
            LocalBlobStore(Path(config["canonical_root"])),
            Path(config["workspace_root"]),
            WaveMediaProbe(),
            lambda: PostgresLeaseQueue(psycopg.connect(config["postgres_dsn"])),
        )
        candidate = worker.run_once()
        assert candidate is not None
        assert candidate.lease.job_id == config["job_id"]
        queue.assert_current(candidate.lease)
        row = connection.execute(
            "SELECT state,winning_attempt_id FROM jobs WHERE id=%s", (candidate.lease.job_id,)
        ).fetchone()
        assert row == ("running", None)
        assert (
            connection.execute(
                "SELECT count(*) FROM artifacts WHERE job_id=%s", (candidate.lease.job_id,)
            ).fetchone()[0]
            == 0
        )
    finally:
        connection.close()
