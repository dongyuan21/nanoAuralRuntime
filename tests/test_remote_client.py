# pyright: reportMissingImports=false
from __future__ import annotations

import hashlib
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Mapping, Optional, Sequence, cast
from unittest.mock import patch

import pytest

from nano_aural_runtime_remote.cli import run
from nano_aural_runtime_remote.client import (
    HttpResponse,
    RemoteClient,
    RemoteEventPage,
    RemoteIntegrityError,
    RemoteNotFound,
)


@pytest.mark.parametrize(
    "module",
    ("nano_aural_runtime_remote", "nano_aural_runtime_remote.cli"),
)
def test_remote_cli_module_exposes_an_executable_help_entrypoint(module: str) -> None:
    environment = dict(os.environ)
    environment.pop("NANO_AURAL_API_URL", None)
    environment.pop("NANO_AURAL_API_TOKEN", None)
    completed = subprocess.run(
        [sys.executable, "-m", module, "--help"],
        check=False,
        capture_output=True,
        text=True,
        env=environment,
    )
    assert completed.returncode == 0
    assert "nano-aural-remote" in completed.stdout


class Transport:
    def __init__(self, responses: Sequence[HttpResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, str], Optional[bytes]]] = []

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        self.calls.append((method, path, headers, body))
        return self.responses.pop(0)


class TrackingBody(io.BytesIO):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.read_calls = 0

    def read(self, size: int = -1) -> bytes:
        self.read_calls += 1
        return super().read(size)


def response(value: object, status: int = 200) -> HttpResponse:
    return HttpResponse(
        status, {"content-type": "application/json"}, io.BytesIO(json.dumps(value).encode())
    )


def test_submit_status_cancel_events_and_safe_resource_encoding() -> None:
    transport = Transport(
        [
            response({"job_id": "job/one", "state": "queued"}, 201),
            response({"job_id": "job/one", "state": "running"}),
            response({"job_id": "job/one", "state": "cancelled"}, 202),
            response({"events": [{"type": "job_created"}], "next_cursor": "7"}),
        ]
    )
    client = RemoteClient(transport, "secret-token")
    command = {
        "namespace_id": "tenant-a",
        "idempotency_key": "idem-1",
        "deployment_id": "deployment-1",
        "request": {"seed": 7},
        "inputs": [],
        "required_artifact_kinds": ["output"],
    }

    assert client.submit(command)["state"] == "queued"
    assert client.status("job/one")["state"] == "running"
    assert client.cancel("job/one")["state"] == "cancelled"
    assert client.events("job/one") == RemoteEventPage(({"type": "job_created"},), "7")

    method, path, headers, body = transport.calls[0]
    assert (method, path) == ("POST", "/v1/jobs")
    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["Idempotency-Key"] == "idem-1"
    assert json.loads(cast(bytes, body)) == command
    assert transport.calls[1][1] == "/v1/jobs/job%2Fone"


def test_submit_rejects_server_local_paths_before_transport() -> None:
    transport = Transport([])
    client = RemoteClient(transport, "token")
    command = {
        "namespace_id": "tenant-a",
        "idempotency_key": "idem-1",
        "deployment_id": "deployment-1",
        "request": {"nested": {"source_path": "/private/source"}},
        "inputs": [],
        "required_artifact_kinds": ["output"],
    }
    with pytest.raises(ValueError, match="prohibited"):
        client.submit(command)
    assert transport.calls == []


@pytest.mark.parametrize("key", ("", "contains space", "slash/not-allowed", "x" * 129))
def test_submit_rejects_invalid_idempotency_key_format_and_length(key: str) -> None:
    transport = Transport([])
    client = RemoteClient(transport, "token")
    command = {
        "namespace_id": "tenant-a",
        "idempotency_key": key,
        "deployment_id": "deployment-1",
        "request": {},
        "inputs": [],
        "required_artifact_kinds": ["output"],
    }
    with pytest.raises(ValueError, match="idempotency"):
        client.submit(command)
    assert transport.calls == []


def test_wait_polls_until_terminal_with_injected_clock() -> None:
    transport = Transport(
        [
            response({"job_id": "job", "state": "queued"}),
            response({"job_id": "job", "state": "running"}),
            response({"job_id": "job", "state": "succeeded"}),
        ]
    )
    sleeps: list[float] = []
    clock = iter((0.0, 0.1, 0.2))
    client = RemoteClient(
        transport,
        "token",
        sleeper=sleeps.append,
        monotonic=lambda: next(clock),
    )
    assert client.wait("job", interval_seconds=0.25, timeout_seconds=10)["state"] == "succeeded"
    assert sleeps == [0.25, 0.25]


def test_errors_do_not_include_bearer_token_or_remote_body() -> None:
    transport = Transport([HttpResponse(404, {}, io.BytesIO(b'{"detail":"private"}'))])
    client = RemoteClient(transport, "super-secret")
    with pytest.raises(RemoteNotFound) as caught:
        client.status("missing")
    assert "super-secret" not in str(caught.value)
    assert "private" not in str(caught.value)


def test_download_streams_valid_sha_to_atomic_no_overwrite_destination(tmp_path: Path) -> None:
    content = b"verified-remote-artifact" * 50
    body = io.BytesIO(content)
    transport = Transport(
        [
            HttpResponse(
                200,
                {
                    "Content-Length": str(len(content)),
                    "X-Content-SHA256": hashlib.sha256(content).hexdigest(),
                },
                body,
            )
        ]
    )
    client = RemoteClient(transport, "token")
    target = tmp_path / "result.wav"

    assert client.download("job", "artifact", target, chunk_size=13) == target
    assert target.read_bytes() == content
    assert body.closed
    assert list(tmp_path.glob(".*.part")) == []


def test_download_integrity_failure_leaves_destination_absent_and_cleans_temp(
    tmp_path: Path,
) -> None:
    content = b"corrupt"
    body = io.BytesIO(content)
    transport = Transport(
        [
            HttpResponse(
                200,
                {
                    "content-length": str(len(content)),
                    "x-content-sha256": "0" * 64,
                },
                body,
            )
        ]
    )
    target = tmp_path / "must-not-exist.wav"
    with pytest.raises(RemoteIntegrityError):
        RemoteClient(transport, "token").download("job", "artifact", target, chunk_size=2)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
    assert body.closed


def test_download_global_size_limit_rejects_declared_body_before_streaming(
    tmp_path: Path,
) -> None:
    body = TrackingBody(b"not-read")
    transport = Transport(
        [
            HttpResponse(
                200,
                {"content-length": "9", "x-content-sha256": "0" * 64},
                body,
            )
        ]
    )
    with pytest.raises(RemoteIntegrityError, match="size limit"):
        RemoteClient(transport, "token", max_download_bytes=8).download(
            "job", "artifact", tmp_path / "result"
        )
    assert body.read_calls == 0
    assert body.closed
    assert list(tmp_path.iterdir()) == []


def test_download_never_overwrites_existing_file_or_symlink(tmp_path: Path) -> None:
    existing = tmp_path / "existing.wav"
    existing.write_bytes(b"keep")
    transport = Transport([])
    client = RemoteClient(transport, "token")
    with pytest.raises(FileExistsError):
        client.download("job", "artifact", existing)
    assert existing.read_bytes() == b"keep"
    assert transport.calls == []

    source = tmp_path / "source.wav"
    source.write_bytes(b"source")
    link = tmp_path / "link.wav"
    link.symlink_to(source)
    with pytest.raises(FileExistsError):
        client.download("job", "artifact", link)
    assert source.read_bytes() == b"source"

    directory = tmp_path / "directory"
    directory.mkdir()
    linked_directory = tmp_path / "linked-directory"
    linked_directory.symlink_to(directory, target_is_directory=True)
    with pytest.raises(OSError):
        client.download("job", "artifact", linked_directory / "result.wav")
    assert transport.calls == []


def test_download_closes_response_when_temporary_file_cannot_be_created(tmp_path: Path) -> None:
    content = b"data"
    body = io.BytesIO(content)
    transport = Transport(
        [
            HttpResponse(
                200,
                {
                    "content-length": str(len(content)),
                    "x-content-sha256": hashlib.sha256(content).hexdigest(),
                },
                body,
            )
        ]
    )
    real_open = os.open

    def deny_write(path, flags, *args, **kwargs):
        if flags & os.O_WRONLY:
            raise PermissionError("denied")
        return real_open(path, flags, *args, **kwargs)

    with patch("nano_aural_runtime_remote.client.os.open", side_effect=deny_write):
        with pytest.raises(PermissionError):
            RemoteClient(transport, "token").download("job", "artifact", tmp_path / "result")
    assert body.closed
    assert list(tmp_path.iterdir()) == []


class CliClient:
    def __init__(self) -> None:
        self.submitted: Optional[Mapping[str, object]] = None

    def submit(self, command: Mapping[str, object]) -> Mapping[str, object]:
        self.submitted = command
        return {"job_id": "job-1", "state": "queued"}

    def status(self, job_id: str) -> Mapping[str, object]:
        return {"job_id": job_id, "state": "succeeded"}

    def wait(self, job_id: str, **kwargs: object) -> Mapping[str, object]:
        return {"job_id": job_id, "state": "succeeded", **kwargs}

    def cancel(self, job_id: str) -> Mapping[str, object]:
        return {"job_id": job_id, "state": "cancelled"}

    def events(self, job_id: str, **kwargs: object) -> RemoteEventPage:
        del kwargs
        return RemoteEventPage(({"job_id": job_id, "type": "job_created"},), None)

    def artifacts(self, job_id: str):
        return ({"job_id": job_id, "artifact_id": "artifact-1"},)

    def upload_asset(self, namespace_id: str, source: Path) -> Mapping[str, object]:
        return {"namespace_id": namespace_id, "source_name": source.name, "asset_id": "asset-1"}

    def download(self, job_id: str, artifact_id: str, destination: Path) -> Path:
        del job_id, artifact_id
        return destination


def test_cli_submit_builds_remote_asset_ids_without_local_paths() -> None:
    client, stdout, stderr = CliClient(), io.StringIO(), io.StringIO()
    exit_code = run(
        (
            "submit",
            "--namespace",
            "tenant-a",
            "--idempotency-key",
            "idem",
            "--deployment",
            "deployment-1",
            "--request-json",
            '{"operation":"generate"}',
            "--input",
            "audio=asset-1",
        ),
        cast(RemoteClient, client),
        stdout,
        stderr,
    )
    assert exit_code == 0
    assert stderr.getvalue() == ""
    assert client.submitted is not None
    assert client.submitted["inputs"] == [{"role": "audio", "asset_id": "asset-1"}]
    assert json.loads(stdout.getvalue())["job_id"] == "job-1"


def test_cli_reports_invalid_request_without_calling_client() -> None:
    client, stdout, stderr = CliClient(), io.StringIO(), io.StringIO()
    exit_code = run(
        (
            "submit",
            "--namespace",
            "tenant-a",
            "--idempotency-key",
            "idem",
            "--deployment",
            "deployment-1",
            "--request-json",
            "[]",
        ),
        cast(RemoteClient, client),
        stdout,
        stderr,
    )
    assert exit_code == 2
    assert client.submitted is None
    assert stdout.getvalue() == ""
    assert "object" in stderr.getvalue()
