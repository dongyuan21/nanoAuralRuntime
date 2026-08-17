"""CPU, loopback, and conditional hardware contracts for Roadmap Phase 5B."""

from __future__ import annotations

import ast
import hashlib
import json
import os
import subprocess
import sys
import threading
import traceback
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import ModuleType
from typing import Any, Iterator, Mapping, Optional
from uuid import UUID

import pytest  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from integrations.comfyui_remote.bootstrap import (  # noqa: E402
    REMOTE_CONFIG_ENV,
    RemoteOperatorConfig,
    RemoteOperatorConfigError,
    load_remote_operator_config,
)
from integrations.comfyui_remote.nodes import (  # noqa: E402
    NODE_CLASS_MAPPINGS,
    RemoteAssetBinding,
    RemoteAssetBundle,
    RemoteAssetBundleNode,
    RemoteJobRef,
    RemoteNodeCancelled,
    RemoteNodeExecutionError,
    RemoteNodeValidationError,
    RemoteWaitNode,
    configure_remote_client_for_host,
    teardown_remote_client,
)
from nano_aural_runtime_remote import RemoteEventPage  # noqa: E402

CONTENT = b"verified remote audio"
CONTENT_SHA256 = hashlib.sha256(CONTENT).hexdigest()
ASSET_VIDEO_ID = "00000000-0000-4000-8000-000000000001"
ASSET_AUDIO_ID = "00000000-0000-4000-8000-000000000002"
JOB_ID = "00000000-0000-4000-8000-000000000010"
ARTIFACT_ID = "00000000-0000-4000-8000-000000000020"


def _assert_traceback_redacted(caught: Any, *sensitive_values: str) -> None:
    rendered = "".join(
        traceback.format_exception(caught.type, caught.value, caught.value.__traceback__)
    )
    assert caught.value.__cause__ is None
    assert caught.value.__suppress_context__ is True
    for value in sensitive_values:
        assert value not in rendered


class _NeverCancelled:
    def is_cancelled(self) -> bool:
        return False


class _Cancelled:
    def is_cancelled(self) -> bool:
        return True


class _BlockedCancellation:
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def is_cancelled(self) -> bool:
        self.started.set()
        self.release.wait(timeout=2)
        return False


class _NonBooleanCancellation:
    def is_cancelled(self):  # type: ignore[no-untyped-def]
        return 1


class _HostCancellationModule(ModuleType):
    def throw_exception_if_processing_interrupted(self) -> None:
        return None


class _FakeRemoteClient:
    def __init__(self, *, wait_timeouts: int = 1) -> None:
        self.uploads: list[tuple[str, Path]] = []
        self.submissions: list[Mapping[str, object]] = []
        self.statuses: list[str] = []
        self.cancellations: list[str] = []
        self.event_calls: list[tuple[str, Optional[str], int]] = []
        self.wait_calls = 0
        self.wait_timeouts = wait_timeouts

    def upload_asset(self, namespace_id: str, source: Path) -> Mapping[str, object]:
        assert source.is_file()
        self.uploads.append((namespace_id, source))
        return {
            "state": "verified",
            "asset_id": (ASSET_VIDEO_ID, ASSET_AUDIO_ID)[len(self.uploads) - 1],
        }

    def submit(self, command: Mapping[str, object]) -> Mapping[str, object]:
        self.submissions.append(command)
        return {"job_id": JOB_ID, "state": "queued"}

    def status(self, job_id: str) -> Mapping[str, object]:
        self.statuses.append(job_id)
        return {"job_id": job_id, "state": "running"}

    def wait(
        self,
        job_id: str,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: Optional[float] = None,
    ) -> Mapping[str, object]:
        assert interval_seconds > 0 and timeout_seconds is not None
        self.wait_calls += 1
        if self.wait_calls <= self.wait_timeouts:
            raise TimeoutError("bounded fake wait")
        return {"job_id": job_id, "state": "succeeded"}

    def cancel(self, job_id: str) -> Mapping[str, object]:
        self.cancellations.append(job_id)
        return {"job_id": job_id, "state": "cancelled"}

    def events(
        self, job_id: str, *, cursor: Optional[str] = None, limit: int = 50
    ) -> RemoteEventPage:
        self.event_calls.append((job_id, cursor, limit))
        event_id = str(len(self.event_calls))
        return RemoteEventPage(
            ({"event_id": event_id, "type": "attempt_started", "payload": {}},),
            None,
        )

    def artifacts(self, job_id: str) -> tuple[Mapping[str, object], ...]:
        return (
            {
                "job_id": job_id,
                "artifact_id": ARTIFACT_ID,
                "kind": "output",
                "sha256": CONTENT_SHA256,
                "size_bytes": len(CONTENT),
                "content_type": "audio/flac",
            },
        )

    def download(self, job_id: str, artifact_id: str, destination: Path) -> Path:
        assert (job_id, artifact_id) == (JOB_ID, ARTIFACT_ID)
        if destination.exists():
            raise FileExistsError(str(destination))
        destination.write_bytes(CONTENT)
        return destination


def _config(tmp_path: Path, *, max_polls: int = 8) -> RemoteOperatorConfig:
    downloads = tmp_path / "downloads"
    downloads.mkdir(exist_ok=True)
    return RemoteOperatorConfig(
        base_url="https://not-exposed.example",
        token_env="NANO_AURAL_TEST_TOKEN",
        allow_loopback_http=False,
        download_dir=downloads,
        transport_timeout_seconds=1.0,
        max_upload_bytes=1024 * 1024,
        max_download_bytes=1024 * 1024,
        max_wait_seconds=30.0,
        max_poll_iterations=max_polls,
    )


@pytest.fixture(autouse=True)
def _reset_remote_owner() -> Iterator[None]:
    teardown_remote_client()
    yield
    teardown_remote_client()


def test_fake_remote_nodes_cover_multi_asset_status_wait_events_download_and_output(
    tmp_path: Path,
) -> None:
    fake = _FakeRemoteClient(wait_timeouts=1)
    configure_remote_client_for_host(lambda: fake, _config(tmp_path))
    video = tmp_path / "video.mp4"
    audio = tmp_path / "reference.wav"
    video.write_bytes(b"video")
    audio.write_bytes(b"audio")

    upload = NODE_CLASS_MAPPINGS["NanoAuralRemoteUpload"]()
    bundler = NODE_CLASS_MAPPINGS["NanoAuralRemoteAssetBundle"]()
    submit = NODE_CLASS_MAPPINGS["NanoAuralRemoteSubmit"]()
    status = NODE_CLASS_MAPPINGS["NanoAuralRemoteStatus"]()
    cancel = NODE_CLASS_MAPPINGS["NanoAuralRemoteCancel"]()
    events = NODE_CLASS_MAPPINGS["NanoAuralRemoteEvents"]()
    wait = NODE_CLASS_MAPPINGS["NanoAuralRemoteWait"]()
    artifacts = NODE_CLASS_MAPPINGS["NanoAuralRemoteArtifacts"]()
    download = NODE_CLASS_MAPPINGS["NanoAuralRemoteDownload"]()
    output = NODE_CLASS_MAPPINGS["NanoAuralRemoteOutput"]()

    video_binding = upload.upload("tenant-a", "video", str(video))[0]
    audio_binding = upload.upload("tenant-a", "reference_audio", str(audio))[0]
    bundle = bundler.bundle(video_binding)[0]
    bundle = bundler.bundle(audio_binding, bundle)[0]
    job = submit.submit(
        "tenant-a",
        "ac-v2a-1",
        "deployment-1",
        '{"task":"AC-V2A"}',
        "output",
        bundle,
    )[0]
    assert fake.submissions[0]["inputs"] == [
        {"role": "video", "asset_id": ASSET_VIDEO_ID},
        {"role": "reference_audio", "asset_id": ASSET_AUDIO_ID},
    ]
    assert status.status(job)[0].state == "running"
    assert events.events(job)[0].event_types == ("attempt_started",)
    waited = wait.wait(
        job,
        interval_seconds=0.05,
        timeout_seconds=2,
        cancellation_source=_NeverCancelled(),
    )
    final_job = waited["result"][0]
    assert final_job.state == "succeeded"
    assert waited["ui"]["nano_aural_remote_job"][0]["event_count"] == 2
    collection = artifacts.artifacts(final_job)[0]
    downloaded = download.download(collection, "result.flac")[0]
    presented = output.present(downloaded)
    assert presented["result"] == ()
    assert (_config(tmp_path).download_dir / "result.flac").read_bytes() == CONTENT
    serialized = json.dumps(presented)
    assert "not-exposed.example" not in serialized
    assert str(tmp_path) not in serialized
    assert cancel.cancel(job)[0].state == "cancelled"
    assert fake.cancellations == [JOB_ID]


def test_zero_input_submit_and_bounded_unique_asset_bundles(tmp_path: Path) -> None:
    fake = _FakeRemoteClient(wait_timeouts=0)
    configure_remote_client_for_host(lambda: fake, _config(tmp_path))
    submit = NODE_CLASS_MAPPINGS["NanoAuralRemoteSubmit"]()
    job = submit.submit("tenant-a", "t2a-1", "deployment-1", '{"task":"T2A"}')[0]
    assert job.state == "queued"
    assert fake.submissions[0]["inputs"] == []
    assert submit.INPUT_TYPES()["required"]["required_artifact_kind"] == (("output", "manifest"),)
    with pytest.raises(RemoteNodeValidationError, match="artifact kind"):
        submit.submit(
            "tenant-a",
            "t2a-2",
            "deployment-1",
            '{"task":"T2A"}',
            "private/server/path",
        )
    assert len(fake.submissions) == 1

    bundler = RemoteAssetBundleNode()
    first = bundler.bundle(RemoteAssetBinding("video", ASSET_VIDEO_ID))[0]
    with pytest.raises(RemoteNodeValidationError, match="unique"):
        bundler.bundle(RemoteAssetBinding("video", ASSET_AUDIO_ID), first)
    with pytest.raises(RemoteNodeValidationError, match="allowed"):
        RemoteAssetBinding("storage_key", "private/server/key")
    with pytest.raises(RemoteNodeValidationError, match="invalid"):
        RemoteAssetBundle(
            tuple(RemoteAssetBinding("audio", str(UUID(int=index + 100))) for index in range(9))
        )


def test_remote_response_identifiers_are_canonical_and_fail_closed(
    tmp_path: Path,
) -> None:
    leaked = "https://private.example/token-file"

    class _MaliciousIdentifierClient(_FakeRemoteClient):
        def upload_asset(self, namespace_id: str, source: Path) -> Mapping[str, object]:
            del namespace_id, source
            return {"state": "verified", "asset_id": leaked}

        def submit(self, command: Mapping[str, object]) -> Mapping[str, object]:
            del command
            return {"job_id": leaked, "state": "queued"}

        def artifacts(self, job_id: str) -> tuple[Mapping[str, object], ...]:
            return (
                {
                    "job_id": job_id,
                    "artifact_id": leaked,
                    "kind": "output",
                    "sha256": CONTENT_SHA256,
                    "size_bytes": len(CONTENT),
                    "content_type": "audio/flac",
                },
            )

    source = tmp_path / "source.wav"
    source.write_bytes(b"audio")
    configure_remote_client_for_host(
        lambda: _MaliciousIdentifierClient(wait_timeouts=0), _config(tmp_path)
    )
    operations = (
        lambda: NODE_CLASS_MAPPINGS["NanoAuralRemoteUpload"]().upload(
            "tenant-a", "audio", str(source)
        ),
        lambda: NODE_CLASS_MAPPINGS["NanoAuralRemoteSubmit"]().submit(
            "tenant-a", "malicious-1", "deployment-1", "{}"
        ),
        lambda: NODE_CLASS_MAPPINGS["NanoAuralRemoteArtifacts"]().artifacts(
            RemoteJobRef(JOB_ID, "succeeded")
        ),
    )
    for operation in operations:
        with pytest.raises(RemoteNodeExecutionError, match="canonical") as caught:
            operation()
        assert leaked not in str(caught.value)


def test_wait_converts_host_cancel_to_public_cancel_and_bounds_checks(tmp_path: Path) -> None:
    fake = _FakeRemoteClient(wait_timeouts=100)
    config = _config(tmp_path, max_polls=2)
    configure_remote_client_for_host(lambda: fake, config)
    owner = NODE_CLASS_MAPPINGS["NanoAuralRemoteStatus"]()._owner
    node = RemoteWaitNode(owner, cancellation_source_factory=_NeverCancelled)
    job = RemoteJobRef(JOB_ID, "queued")

    with pytest.raises(RemoteNodeCancelled):
        node.wait(job, timeout_seconds=1, cancellation_source=_Cancelled())
    assert fake.cancellations == [JOB_ID]
    with pytest.raises(RemoteNodeExecutionError, match="poll limit"):
        node.wait(job, interval_seconds=0.01, timeout_seconds=1)
    assert fake.wait_calls == 2


def test_wait_uses_authoritative_next_cursor_and_validates_clock(tmp_path: Path) -> None:
    class _CursorClient(_FakeRemoteClient):
        def events(
            self, job_id: str, *, cursor: Optional[str] = None, limit: int = 50
        ) -> RemoteEventPage:
            self.event_calls.append((job_id, cursor, limit))
            if len(self.event_calls) == 1:
                return RemoteEventPage(
                    ({"event_id": "1", "type": "attempt_started", "payload": {}},),
                    "7",
                )
            return RemoteEventPage(
                ({"event_id": "8", "type": "job_succeeded", "payload": {}},),
                None,
            )

    fake = _CursorClient(wait_timeouts=1)
    configure_remote_client_for_host(lambda: fake, _config(tmp_path))
    owner = NODE_CLASS_MAPPINGS["NanoAuralRemoteStatus"]()._owner
    result = RemoteWaitNode(owner, cancellation_source_factory=_NeverCancelled).wait(
        RemoteJobRef(JOB_ID, "queued"), interval_seconds=0.01, timeout_seconds=1
    )
    resolved = result["result"]
    assert isinstance(resolved, tuple) and isinstance(resolved[0], RemoteJobRef)
    assert resolved[0].event_types == ("attempt_started", "job_succeeded")
    assert fake.event_calls == [(JOB_ID, None, 50), (JOB_ID, "7", 50)]

    for readings in ((float("inf"),), (10.0, float("nan")), (10.0, 9.0)):
        values = iter(readings)
        node = RemoteWaitNode(
            owner,
            cancellation_source_factory=_NeverCancelled,
            monotonic=lambda current_values=values: next(current_values),
        )
        wait_calls = fake.wait_calls
        with pytest.raises(RemoteNodeExecutionError, match="monotonic clock"):
            node.wait(RemoteJobRef(JOB_ID, "queued"), timeout_seconds=1)
        assert fake.wait_calls == wait_calls


@pytest.mark.parametrize(
    ("event_id", "event_type", "next_cursor", "message"),
    (
        ("../../secret", "attempt_started", None, "event cursor"),
        ("1", "token_dump", None, "event type"),
        ("1", "attempt_started", "01", "event cursor"),
    ),
)
def test_remote_event_page_values_are_allowlisted(
    tmp_path: Path,
    event_id: str,
    event_type: str,
    next_cursor: Optional[str],
    message: str,
) -> None:
    class _InvalidEventClient(_FakeRemoteClient):
        def events(
            self, job_id: str, *, cursor: Optional[str] = None, limit: int = 50
        ) -> RemoteEventPage:
            del job_id, cursor, limit
            return RemoteEventPage(
                ({"event_id": event_id, "type": event_type, "payload": {}},),
                next_cursor,
            )

    configure_remote_client_for_host(
        lambda: _InvalidEventClient(wait_timeouts=0), _config(tmp_path)
    )
    node = NODE_CLASS_MAPPINGS["NanoAuralRemoteEvents"]()
    with pytest.raises(RemoteNodeExecutionError, match=message) as caught:
        node.events(RemoteJobRef(JOB_ID, "running"))
    assert "secret" not in str(caught.value)
    assert "token_dump" not in str(caught.value)


def test_blocked_cancellation_check_fails_before_remote_wait(tmp_path: Path) -> None:
    fake = _FakeRemoteClient(wait_timeouts=0)
    configure_remote_client_for_host(lambda: fake, _config(tmp_path))
    source = _BlockedCancellation()
    node = NODE_CLASS_MAPPINGS["NanoAuralRemoteWait"]()
    with pytest.raises(RemoteNodeExecutionError, match="check blocked"):
        node.wait(
            RemoteJobRef(JOB_ID, "queued"),
            timeout_seconds=1,
            cancellation_source=source,
        )
    assert source.started.is_set()
    source.release.set()
    assert fake.wait_calls == 0


def test_wait_factory_timeout_none_invalid_and_nonbool_fail_before_remote_wait(
    tmp_path: Path,
) -> None:
    fake = _FakeRemoteClient(wait_timeouts=0)
    configure_remote_client_for_host(lambda: fake, _config(tmp_path))
    owner = NODE_CLASS_MAPPINGS["NanoAuralRemoteStatus"]()._owner
    started = threading.Event()
    release = threading.Event()

    def blocked_factory():  # type: ignore[no-untyped-def]
        started.set()
        release.wait(timeout=2)
        return _NeverCancelled()

    with pytest.raises(RemoteNodeExecutionError, match="factory blocked"):
        RemoteWaitNode(owner, blocked_factory).wait(
            RemoteJobRef(JOB_ID, "queued"), timeout_seconds=1
        )
    assert started.is_set()
    release.set()

    for factory in (lambda: None, lambda: object()):
        with pytest.raises(RemoteNodeExecutionError, match="invalid data"):
            RemoteWaitNode(owner, factory).wait(  # type: ignore[arg-type]
                RemoteJobRef(JOB_ID, "queued"), timeout_seconds=1
            )
    with pytest.raises(RemoteNodeExecutionError, match="invalid value"):
        RemoteWaitNode(owner, _NeverCancelled).wait(
            RemoteJobRef(JOB_ID, "queued"),
            timeout_seconds=1,
            cancellation_source=_NonBooleanCancellation(),  # type: ignore[arg-type]
        )
    assert fake.wait_calls == 0


def test_remote_wait_and_cancellation_tracebacks_are_redacted(tmp_path: Path) -> None:
    leaked = "Bearer trace-secret https://private.example " + str(tmp_path / "private.wav")

    class _LeakyRemoteClient(_FakeRemoteClient):
        def status(self, job_id: str) -> Mapping[str, object]:
            del job_id
            raise RuntimeError(leaked)

        def wait(
            self,
            job_id: str,
            *,
            interval_seconds: float = 1.0,
            timeout_seconds: Optional[float] = None,
        ) -> Mapping[str, object]:
            del job_id, interval_seconds, timeout_seconds
            raise RuntimeError(leaked)

        def cancel(self, job_id: str) -> Mapping[str, object]:
            del job_id
            raise RuntimeError(leaked)

    class _LeakyCancellation:
        def is_cancelled(self) -> bool:
            raise RuntimeError(leaked)

    def leaky_factory() -> _NeverCancelled:
        raise RuntimeError(leaked)

    fake = _LeakyRemoteClient(wait_timeouts=0)
    configure_remote_client_for_host(lambda: fake, _config(tmp_path))
    owner = NODE_CLASS_MAPPINGS["NanoAuralRemoteStatus"]()._owner
    job = RemoteJobRef(JOB_ID, "running")
    sensitive = ("trace-secret", "private.example", str(tmp_path), "private.wav")

    with pytest.raises(RemoteNodeExecutionError) as status_error:
        NODE_CLASS_MAPPINGS["NanoAuralRemoteStatus"]().status(job)
    _assert_traceback_redacted(status_error, *sensitive)

    with pytest.raises(RemoteNodeExecutionError) as cancel_error:
        NODE_CLASS_MAPPINGS["NanoAuralRemoteCancel"]().cancel(job)
    _assert_traceback_redacted(cancel_error, *sensitive)

    with pytest.raises(RemoteNodeExecutionError, match="factory failed") as factory_error:
        RemoteWaitNode(owner, leaky_factory).wait(job, timeout_seconds=1)
    _assert_traceback_redacted(factory_error, *sensitive)

    with pytest.raises(RemoteNodeExecutionError, match="check failed") as check_error:
        RemoteWaitNode(owner, _NeverCancelled).wait(
            job,
            timeout_seconds=1,
            cancellation_source=_LeakyCancellation(),
        )
    _assert_traceback_redacted(check_error, *sensitive)

    with pytest.raises(RemoteNodeExecutionError, match="remote wait failed") as wait_error:
        RemoteWaitNode(owner, _NeverCancelled).wait(job, timeout_seconds=1)
    _assert_traceback_redacted(wait_error, *sensitive)


def test_operator_config_is_strict_and_does_not_store_a_token(tmp_path: Path) -> None:
    downloads = tmp_path / "downloads"
    downloads.mkdir()
    config_path = tmp_path / "remote.json"
    value = {
        "schema_version": 1,
        "base_url": "https://example.test",
        "token_env": "NANO_AURAL_API_TOKEN",
        "allow_loopback_http": False,
        "download_dir": str(downloads),
        "transport_timeout_seconds": 30,
        "max_upload_bytes": 1024,
        "max_download_bytes": 2048,
        "max_wait_seconds": 60,
        "max_poll_iterations": 100,
    }
    config_path.write_text(json.dumps(value), encoding="utf-8")
    config = load_remote_operator_config(config_path)
    assert config.token_env == "NANO_AURAL_API_TOKEN"
    assert not hasattr(config, "token")

    value["token"] = "must-not-escape"
    config_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RemoteOperatorConfigError) as caught:
        load_remote_operator_config(config_path)
    assert "must-not-escape" not in str(caught.value)
    del value["token"]
    value["schema_version"] = True
    config_path.write_text(json.dumps(value), encoding="utf-8")
    with pytest.raises(RemoteOperatorConfigError, match="schema_version"):
        load_remote_operator_config(config_path)


def test_operator_config_tracebacks_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    leaked = "Bearer config-secret https://config.example " + str(tmp_path / "sealed.json")
    config_path = tmp_path / "remote.json"
    config_path.write_text("{}", encoding="utf-8")
    original_open = Path.open

    def leaky_open(path: Path, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def]
        if path == config_path:
            raise OSError(leaked)
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", leaky_open)
    with pytest.raises(RemoteOperatorConfigError) as loader_error:
        load_remote_operator_config(config_path)
    sensitive = ("config-secret", "config.example", str(tmp_path), "sealed.json")
    _assert_traceback_redacted(loader_error, *sensitive)

    def leaky_bootstrap():  # type: ignore[no-untyped-def]
        raise RemoteOperatorConfigError(leaked)

    monkeypatch.setattr(
        "integrations.comfyui_remote.nodes.remote_operator_config_from_environment",
        leaky_bootstrap,
    )
    with pytest.raises(RemoteNodeValidationError) as bootstrap_error:
        NODE_CLASS_MAPPINGS["NanoAuralRemoteStatus"]()
    _assert_traceback_redacted(bootstrap_error, *sensitive)


def test_download_rejects_paths_and_client_errors_are_sanitized(tmp_path: Path) -> None:
    class _LeakyClient(_FakeRemoteClient):
        def download(self, job_id: str, artifact_id: str, destination: Path) -> Path:
            raise FileExistsError("secret-token https://private.example " + str(destination))

    fake = _LeakyClient(wait_timeouts=0)
    configure_remote_client_for_host(lambda: fake, _config(tmp_path))
    artifacts = NODE_CLASS_MAPPINGS["NanoAuralRemoteArtifacts"]().artifacts(
        RemoteJobRef(JOB_ID, "succeeded")
    )[0]
    node = NODE_CLASS_MAPPINGS["NanoAuralRemoteDownload"]()
    with pytest.raises(RemoteNodeValidationError, match="must not contain a path"):
        node.download(artifacts, "../escape.flac")
    with pytest.raises(RemoteNodeExecutionError) as caught:
        node.download(artifacts, "safe.flac")
    _assert_traceback_redacted(
        caught,
        "secret-token",
        "private.example",
        str(tmp_path),
    )


def test_example_is_a_standard_linked_host_workflow() -> None:
    workflow = json.loads(
        (ROOT / "integrations/comfyui_remote/examples/remote_controlfoley_v2a.json").read_text(
            encoding="utf-8"
        )
    )
    nodes = {node["id"]: node for node in workflow["nodes"]}
    assert set(nodes) == set(range(1, 8))
    assert all(node["type"] in NODE_CLASS_MAPPINGS for node in nodes.values())
    assert len(workflow["links"]) == 6
    assert NODE_CLASS_MAPPINGS[nodes[7]["type"]].OUTPUT_NODE is True
    serialized = json.dumps(workflow)
    assert "storage_key" not in serialized
    assert "bearer" not in serialized.casefold()

    prohibited_inputs = {
        "base_url",
        "bearer_token",
        "operator_config",
        "server_path",
        "storage_key",
        "source_dir",
        "weights_path",
    }
    for node_type in NODE_CLASS_MAPPINGS.values():
        declared = node_type.INPUT_TYPES()
        names = set(declared.get("required", {})) | set(declared.get("optional", {}))
        assert names.isdisjoint(prohibited_inputs)


def test_remote_package_has_no_model_cuda_durable_or_comfy_imports() -> None:
    package = ROOT / "integrations/comfyui_remote"
    prohibited = {
        "torch",
        "comfy",
        "nano_aural_runtime",
        "nano_aural_runtime_controlfoley",
        "nano_aural_runtime_workers",
        "nano_aural_runtime.durable",
    }
    for path in package.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                imports.append(node.module)
        for imported in imports:
            assert imported == "nano_aural_runtime_remote" or not any(
                imported == name or imported.startswith(name + ".") for name in prohibited
            )

    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            "import integrations.comfyui_remote; import sys; "
            "assert 'torch' not in sys.modules; "
            "assert 'nano_aural_runtime_controlfoley' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr


class _LoopbackHandler(BaseHTTPRequestHandler):
    uploads: list[bytes] = []
    submissions: list[Mapping[str, object]] = []
    authorizations: list[str] = []

    def _body(self) -> bytes:
        return self.rfile.read(int(self.headers.get("Content-Length", "0")))

    def _json(self, status: int, value: object) -> None:
        body = json.dumps(value).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _record_auth(self) -> None:
        self.authorizations.append(self.headers.get("Authorization", ""))

    def do_POST(self) -> None:
        self._record_auth()
        body = self._body()
        if self.path == "/v1/assets/uploads":
            self._json(201, {"session_id": "session-1"})
        elif self.path == "/v1/jobs":
            value = json.loads(body)
            assert isinstance(value, dict)
            self.submissions.append(value)
            self._json(201, {"job_id": JOB_ID, "state": "queued"})
        elif self.path == "/v1/jobs/{0}/cancel".format(JOB_ID):
            self._json(202, {"job_id": JOB_ID, "state": "cancelled"})
        else:
            self._json(404, {"error": "missing"})

    def do_PUT(self) -> None:
        self._record_auth()
        body = self._body()
        if self.path == "/v1/assets/uploads/session-1":
            self.uploads.append(body)
            self._json(200, {"state": "verified", "asset_id": ASSET_VIDEO_ID})
        else:
            self._json(404, {"error": "missing"})

    def do_GET(self) -> None:
        self._record_auth()
        if self.path == "/v1/jobs/{0}".format(JOB_ID):
            self._json(200, {"job_id": JOB_ID, "state": "succeeded"})
        elif self.path.startswith("/v1/jobs/{0}/events?".format(JOB_ID)):
            self._json(
                200,
                {
                    "events": [{"event_id": "1", "type": "job_succeeded", "payload": {}}],
                    "next_cursor": None,
                },
            )
        elif self.path == "/v1/jobs/{0}/artifacts".format(JOB_ID):
            self._json(
                200,
                {
                    "artifacts": [
                        {
                            "artifact_id": ARTIFACT_ID,
                            "kind": "output",
                            "sha256": CONTENT_SHA256,
                            "size_bytes": len(CONTENT),
                            "content_type": "audio/flac",
                        }
                    ]
                },
            )
        elif self.path == "/v1/jobs/{0}/artifacts/{1}".format(JOB_ID, ARTIFACT_ID):
            self.send_response(200)
            self.send_header("Content-Type", "audio/flac")
            self.send_header("Content-Length", str(len(CONTENT)))
            self.send_header("X-Content-SHA256", CONTENT_SHA256)
            self.end_headers()
            self.wfile.write(CONTENT)
        else:
            self._json(404, {"error": "missing"})

    def log_message(self, format: str, *args: object) -> None:
        del format, args


@contextmanager
def _loopback() -> Iterator[ThreadingHTTPServer]:
    _LoopbackHandler.uploads = []
    _LoopbackHandler.submissions = []
    _LoopbackHandler.authorizations = []
    server = ThreadingHTTPServer(("127.0.0.1", 0), _LoopbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_loopback_public_client_executes_full_discovered_node_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _loopback() as server:
        port = server.server_address[1]
        downloads = tmp_path / "downloads"
        downloads.mkdir()
        source = tmp_path / "video.mp4"
        source.write_bytes(b"video-content")
        config_path = tmp_path / "remote.json"
        config_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "base_url": "http://127.0.0.1:{0}".format(port),
                    "token_env": "NANO_AURAL_LOOPBACK_TOKEN",
                    "allow_loopback_http": True,
                    "download_dir": str(downloads),
                    "transport_timeout_seconds": 5,
                    "max_upload_bytes": 1024,
                    "max_download_bytes": 1024,
                    "max_wait_seconds": 10,
                    "max_poll_iterations": 10,
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setenv(REMOTE_CONFIG_ENV, str(config_path))
        monkeypatch.setenv("NANO_AURAL_LOOPBACK_TOKEN", "loopback-secret")
        monkeypatch.setitem(
            sys.modules,
            "comfy.model_management",
            _HostCancellationModule("comfy.model_management"),
        )

        upload = NODE_CLASS_MAPPINGS["NanoAuralRemoteUpload"]()
        binding = upload.upload("tenant-a", "video", str(source))[0]
        bundle = NODE_CLASS_MAPPINGS["NanoAuralRemoteAssetBundle"]().bundle(binding)[0]
        job = NODE_CLASS_MAPPINGS["NanoAuralRemoteSubmit"]().submit(
            "tenant-a", "loopback-1", "deployment-1", '{"task":"V2A"}', "output", bundle
        )[0]
        waited = NODE_CLASS_MAPPINGS["NanoAuralRemoteWait"]().wait(
            job, interval_seconds=0.05, timeout_seconds=2
        )
        final = waited["result"][0]
        artifacts = NODE_CLASS_MAPPINGS["NanoAuralRemoteArtifacts"]().artifacts(final)[0]
        downloaded = NODE_CLASS_MAPPINGS["NanoAuralRemoteDownload"]().download(
            artifacts, "loopback.flac"
        )[0]
        presented = NODE_CLASS_MAPPINGS["NanoAuralRemoteOutput"]().present(downloaded)

        assert (downloads / "loopback.flac").read_bytes() == CONTENT
        assert _LoopbackHandler.uploads == [b"video-content"]
        assert _LoopbackHandler.submissions[0]["inputs"] == [
            {"role": "video", "asset_id": ASSET_VIDEO_ID}
        ]
        assert set(_LoopbackHandler.authorizations) == {"Bearer loopback-secret"}
        summary = json.dumps(presented)
        assert "loopback-secret" not in summary
        assert str(config_path) not in summary
        assert "127.0.0.1" not in summary
        with pytest.raises(RemoteNodeExecutionError):
            NODE_CLASS_MAPPINGS["NanoAuralRemoteDownload"]().download(artifacts, "loopback.flac")


@pytest.mark.gpu
@pytest.mark.controlfoley
def test_conditional_remote_gpu_ui_smoke_through_discovered_nodes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    raw = os.environ.get("CONTROLFOLEY_COMFYUI_REMOTE_GPU_CONFIG")
    if not isinstance(raw, str):
        pytest.skip(
            "CONTROLFOLEY_COMFYUI_REMOTE_GPU_CONFIG is not set; RTX 4090 remote UI smoke is deferred"
        )
    assert raw is not None
    try:
        config: Mapping[str, Any] = json.loads(raw)
        required = {
            "operator_config_path",
            "namespace_id",
            "idempotency_key",
            "deployment_id",
            "request_json",
            "source",
            "role",
            "output_name",
        }
        if set(config) != required:
            pytest.fail("remote GPU smoke config has missing or unexpected fields")
        monkeypatch.setenv(REMOTE_CONFIG_ENV, str(config["operator_config_path"]))
        monkeypatch.setitem(
            sys.modules,
            "comfy.model_management",
            _HostCancellationModule("comfy.model_management"),
        )
        binding = NODE_CLASS_MAPPINGS["NanoAuralRemoteUpload"]().upload(
            str(config["namespace_id"]), str(config["role"]), str(config["source"])
        )[0]
        bundle = NODE_CLASS_MAPPINGS["NanoAuralRemoteAssetBundle"]().bundle(binding)[0]
        job = NODE_CLASS_MAPPINGS["NanoAuralRemoteSubmit"]().submit(
            str(config["namespace_id"]),
            str(config["idempotency_key"]),
            str(config["deployment_id"]),
            str(config["request_json"]),
            "output",
            bundle,
        )[0]
        waited = NODE_CLASS_MAPPINGS["NanoAuralRemoteWait"]().wait(
            job, interval_seconds=1, timeout_seconds=300
        )
        artifacts = NODE_CLASS_MAPPINGS["NanoAuralRemoteArtifacts"]().artifacts(
            waited["result"][0]
        )[0]
        downloaded = NODE_CLASS_MAPPINGS["NanoAuralRemoteDownload"]().download(
            artifacts, str(config["output_name"])
        )[0]
        presented = NODE_CLASS_MAPPINGS["NanoAuralRemoteOutput"]().present(downloaded)
    except (OSError, RuntimeError, TypeError, ValueError) as error:
        pytest.fail("remote ControlFoley GPU UI smoke failed: {0}".format(error))
    assert presented["result"] == ()
