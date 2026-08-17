"""Standard-library remote API client with verified atomic downloads."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import stat
import time
from dataclasses import dataclass
from pathlib import Path
from typing import BinaryIO, Callable, Mapping, Optional, Protocol, Tuple, cast
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from uuid import uuid4


class RemoteApiError(Exception):
    """A remote request failed without exposing credentials or response bodies."""


class RemoteNotFound(RemoteApiError):
    pass


class RemoteConflict(RemoteApiError):
    pass


class RemoteIntegrityError(RemoteApiError):
    pass


@dataclass
class HttpResponse:
    status: int
    headers: Mapping[str, str]
    body: BinaryIO


@dataclass(frozen=True)
class RemoteEventPage:
    events: Tuple[Mapping[str, object], ...]
    next_cursor: Optional[str]


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Optional[bytes] = None,
    ) -> HttpResponse: ...


class _RejectRedirects(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        del request, fp, code, msg, headers, newurl
        return None


class UrllibTransport:
    """Small production transport; authentication policy stays in RemoteClient."""

    def __init__(
        self,
        base_url: str,
        timeout_seconds: float = 30.0,
        *,
        allow_loopback_http: bool = False,
    ) -> None:
        if not isinstance(base_url, str):
            raise TypeError("base_url must be a string")
        parsed = urlsplit(base_url)
        if (
            not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ValueError("base_url must be an origin without credentials")
        if parsed.scheme != "https":
            if (
                parsed.scheme != "http"
                or not allow_loopback_http
                or not self._loopback(parsed.hostname)
            ):
                raise ValueError(
                    "base_url must use HTTPS; HTTP is explicit loopback development only"
                )
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._timeout = timeout_seconds
        self._opener = build_opener(_RejectRedirects())

    def request(
        self,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: Optional[bytes] = None,
    ) -> HttpResponse:
        if not path.startswith("/") or path.startswith("//"):
            raise ValueError("path must be an absolute application path")
        request = Request(
            self._base_url + path,
            data=body,
            headers=dict(headers),
            method=method,
        )
        try:
            response = self._opener.open(request, timeout=self._timeout)
            return HttpResponse(
                int(response.status),
                {str(key).lower(): str(value) for key, value in response.headers.items()},
                response,
            )
        except HTTPError as error:
            return HttpResponse(
                error.code,
                {str(key).lower(): str(value) for key, value in error.headers.items()},
                cast(BinaryIO, error),
            )
        except URLError as error:
            raise RemoteApiError("remote transport unavailable") from error

    @staticmethod
    def _loopback(hostname: str) -> bool:
        if hostname.lower() == "localhost":
            return True
        try:
            return ipaddress.ip_address(hostname).is_loopback
        except ValueError:
            return False


class RemoteClient:
    _TERMINAL = frozenset(("succeeded", "failed", "cancelled"))
    _SUBMIT_FIELDS = frozenset(
        (
            "namespace_id",
            "idempotency_key",
            "deployment_id",
            "request",
            "inputs",
            "required_artifact_kinds",
        )
    )
    _PROHIBITED_REQUEST_KEYS = frozenset(
        ("device", "module", "python_module", "source_dir", "weights_dir", "weights_path")
    )
    _IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    def __init__(
        self,
        transport: HttpTransport,
        bearer_token: str,
        *,
        max_download_bytes: int = 1024 * 1024 * 1024,
        max_upload_bytes: int = 1024 * 1024,
        sleeper: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if not isinstance(bearer_token, str) or not bearer_token.strip():
            raise ValueError("bearer_token must be non-empty")
        if (
            isinstance(max_download_bytes, bool)
            or not isinstance(max_download_bytes, int)
            or max_download_bytes < 1
        ):
            raise ValueError("max_download_bytes must be a positive integer")
        if (
            isinstance(max_upload_bytes, bool)
            or not isinstance(max_upload_bytes, int)
            or max_upload_bytes < 1
        ):
            raise ValueError("max_upload_bytes must be a positive integer")
        self._transport = transport
        self._token = bearer_token
        self._max_download_bytes = max_download_bytes
        self._max_upload_bytes = max_upload_bytes
        self._sleep = sleeper
        self._monotonic = monotonic

    def submit(self, command: Mapping[str, object]) -> Mapping[str, object]:
        self._validate_submit(command)
        return self._json(
            "POST",
            "/v1/jobs",
            command,
            expected=(200, 201, 202),
            headers={"Idempotency-Key": cast(str, command["idempotency_key"])},
        )

    def status(self, job_id: str) -> Mapping[str, object]:
        return self._json("GET", self._job_path(job_id), expected=(200,))

    def cancel(self, job_id: str) -> Mapping[str, object]:
        return self._json("POST", self._job_path(job_id) + "/cancel", expected=(200, 202))

    def events(
        self, job_id: str, *, cursor: Optional[str] = None, limit: int = 50
    ) -> RemoteEventPage:
        if cursor is not None and (
            not isinstance(cursor, str)
            or len(cursor) > 20
            or not cursor.isascii()
            or not cursor.isdecimal()
            or cursor != str(int(cursor))
        ):
            raise ValueError("event cursor is invalid")
        if isinstance(limit, bool) or not isinstance(limit, int) or limit < 1 or limit > 100:
            raise ValueError("event limit must be between 1 and 100")
        path = self._job_path(job_id) + "/events?limit=" + str(limit)
        if cursor is not None:
            path += "&cursor=" + quote(cursor, safe="")
        payload = self._json("GET", path, expected=(200,))
        events = payload.get("events")
        if not isinstance(events, list) or not all(isinstance(item, dict) for item in events):
            raise RemoteApiError("remote events response has an invalid shape")
        next_cursor = payload.get("next_cursor")
        if next_cursor is not None and not isinstance(next_cursor, str):
            raise RemoteApiError("remote events cursor has an invalid shape")
        return RemoteEventPage(tuple(events), next_cursor)

    def artifacts(self, job_id: str) -> Tuple[Mapping[str, object], ...]:
        payload = self._json("GET", self._job_path(job_id) + "/artifacts", expected=(200,))
        artifacts = payload.get("artifacts")
        if not isinstance(artifacts, list) or not all(isinstance(item, dict) for item in artifacts):
            raise RemoteApiError("remote artifact response has an invalid shape")
        return tuple(artifacts)

    def upload_asset(self, namespace_id: str, source: Path) -> Mapping[str, object]:
        """Upload a bounded local file and return its verified durable asset id."""

        if not isinstance(namespace_id, str) or not namespace_id.strip():
            raise ValueError("namespace_id must be non-empty")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(str(Path(source)), flags)
        try:
            information = os.fstat(descriptor)
            if not stat.S_ISREG(information.st_mode):
                raise ValueError("upload source must be a regular file")
            if information.st_size > self._max_upload_bytes:
                raise ValueError("upload source exceeds configured size limit")
            chunks = []
            digest, size = hashlib.sha256(), 0
            while True:
                chunk = os.read(descriptor, min(1024 * 1024, self._max_upload_bytes + 1 - size))
                if not chunk:
                    break
                chunks.append(chunk)
                digest.update(chunk)
                size += len(chunk)
                if size > self._max_upload_bytes:
                    raise ValueError("upload source exceeds configured size limit")
            if size != information.st_size:
                raise ValueError("upload source changed while it was read")
        finally:
            os.close(descriptor)
        content = b"".join(chunks)
        initiated = self._json(
            "POST",
            "/v1/assets/uploads",
            {
                "namespace_id": namespace_id,
                "expected_size_bytes": size,
                "expected_sha256": digest.hexdigest(),
            },
            expected=(201,),
        )
        session_id = initiated.get("session_id")
        if not isinstance(session_id, str) or not session_id:
            raise RemoteApiError("remote upload response has an invalid session id")
        response = self._request(
            "PUT",
            "/v1/assets/uploads/" + self._segment(session_id),
            content,
            content_type="application/octet-stream",
        )
        completed = self._decode_json(response, expected=(200,))
        if completed.get("state") != "verified" or not isinstance(completed.get("asset_id"), str):
            raise RemoteIntegrityError("remote upload did not produce a verified asset")
        return completed

    def wait(
        self,
        job_id: str,
        *,
        interval_seconds: float = 1.0,
        timeout_seconds: Optional[float] = None,
    ) -> Mapping[str, object]:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        started = self._monotonic()
        while True:
            status = self.status(job_id)
            state = status.get("state")
            if state in self._TERMINAL:
                return status
            if not isinstance(state, str):
                raise RemoteApiError("remote job response has an invalid state")
            if timeout_seconds is not None and self._monotonic() - started >= timeout_seconds:
                raise TimeoutError("remote job wait timed out")
            self._sleep(interval_seconds)

    def download(
        self,
        job_id: str,
        artifact_id: str,
        destination: Path,
        *,
        chunk_size: int = 1024 * 1024,
    ) -> Path:
        if isinstance(chunk_size, bool) or not isinstance(chunk_size, int) or chunk_size < 1:
            raise ValueError("chunk_size must be a positive integer")
        target = Path(destination)
        try:
            parent_descriptor = self._open_directory(target.parent)
        except FileNotFoundError as error:
            raise ValueError("download destination parent must exist") from error
        try:
            if target.name in ("", ".", ".."):
                raise ValueError("download destination must name a file")
            try:
                os.stat(target.name, dir_fd=parent_descriptor, follow_symlinks=False)
            except FileNotFoundError:
                pass
            else:
                raise FileExistsError(str(target))
            response = self._request(
                "GET",
                self._job_path(job_id) + "/artifacts/" + self._segment(artifact_id),
            )
            if response.status != 200:
                self._close(response.body)
                self._raise_status(response.status)
            headers = {str(key).lower(): str(value) for key, value in response.headers.items()}
            expected_sha = headers.get("x-content-sha256", "")
            try:
                expected_size = int(headers.get("content-length", ""))
            except ValueError as error:
                self._close(response.body)
                raise RemoteIntegrityError("download is missing a valid content length") from error
            if len(expected_sha) != 64 or expected_sha.lower() != expected_sha or expected_size < 0:
                self._close(response.body)
                raise RemoteIntegrityError("download is missing full-file integrity evidence")
            if expected_size > self._max_download_bytes:
                self._close(response.body)
                raise RemoteIntegrityError("download exceeds configured size limit")
            try:
                int(expected_sha, 16)
            except ValueError as error:
                self._close(response.body)
                raise RemoteIntegrityError("download SHA-256 is invalid") from error

            temporary = ".{0}.{1}.part".format(target.name, uuid4().hex)
            try:
                descriptor = os.open(
                    temporary,
                    os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
                    0o600,
                    dir_fd=parent_descriptor,
                )
            except BaseException:
                self._close(response.body)
                raise
            digest, size = hashlib.sha256(), 0
            try:
                try:
                    output_stream = os.fdopen(descriptor, "wb")
                except BaseException:
                    os.close(descriptor)
                    raise
                with output_stream as output:
                    while True:
                        chunk = response.body.read(chunk_size)
                        if not chunk:
                            break
                        if not isinstance(chunk, bytes):
                            raise RemoteIntegrityError("download stream returned non-bytes")
                        size += len(chunk)
                        if size > expected_size or size > self._max_download_bytes:
                            raise RemoteIntegrityError("download exceeds permitted size")
                        digest.update(chunk)
                        output.write(chunk)
                    output.flush()
                    os.fsync(output.fileno())
                if size != expected_size or digest.hexdigest() != expected_sha:
                    raise RemoteIntegrityError("download differs from full-file integrity evidence")
                try:
                    os.link(
                        temporary,
                        target.name,
                        src_dir_fd=parent_descriptor,
                        dst_dir_fd=parent_descriptor,
                        follow_symlinks=False,
                    )
                except FileExistsError:
                    raise FileExistsError(str(target)) from None
                os.unlink(temporary, dir_fd=parent_descriptor)
                os.fsync(parent_descriptor)
                return target
            finally:
                self._close(response.body)
                try:
                    os.unlink(temporary, dir_fd=parent_descriptor)
                except FileNotFoundError:
                    pass
        finally:
            os.close(parent_descriptor)

    @staticmethod
    def _open_directory(directory: Path) -> int:
        """Open every directory component without following a symlink."""

        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open("/" if directory.is_absolute() else ".", flags)
        parts = directory.parts[1:] if directory.is_absolute() else directory.parts
        try:
            for part in parts:
                if part in ("", "."):
                    continue
                if part == "..":
                    raise ValueError("download destination cannot traverse parent directories")
                child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
            return descriptor
        except BaseException:
            os.close(descriptor)
            raise

    def _json(
        self,
        method: str,
        path: str,
        payload: Optional[Mapping[str, object]] = None,
        *,
        expected: Tuple[int, ...],
        headers: Optional[Mapping[str, str]] = None,
    ) -> Mapping[str, object]:
        body = None
        if payload is not None:
            body = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        response = self._request(method, path, body, headers)
        return self._decode_json(response, expected=expected)

    def _decode_json(
        self, response: HttpResponse, *, expected: Tuple[int, ...]
    ) -> Mapping[str, object]:
        try:
            if response.status not in expected:
                self._raise_status(response.status)
            raw = response.body.read(1024 * 1024 + 1)
            if len(raw) > 1024 * 1024:
                raise RemoteApiError("remote JSON response is too large")
            value = json.loads(raw.decode("utf-8"))
            if not isinstance(value, dict):
                raise RemoteApiError("remote JSON response must be an object")
            return value
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise RemoteApiError("remote JSON response is invalid") from error
        finally:
            self._close(response.body)

    def _request(
        self,
        method: str,
        path: str,
        body: Optional[bytes] = None,
        extra_headers: Optional[Mapping[str, str]] = None,
        *,
        content_type: str = "application/json",
    ) -> HttpResponse:
        headers = {
            "Accept": "application/json",
            "Authorization": "Bearer " + self._token,
        }
        if body is not None:
            headers["Content-Type"] = content_type
            headers["Content-Length"] = str(len(body))
        elif method.upper() in {"POST", "PUT", "PATCH"}:
            headers["Content-Length"] = "0"
        if extra_headers is not None:
            for key, value in extra_headers.items():
                if key.lower() in {item.lower() for item in headers}:
                    raise ValueError("extra header conflicts with a reserved client header")
                headers[key] = value
        try:
            return self._transport.request(method, path, headers, body)
        except RemoteApiError:
            raise
        except Exception as error:
            raise RemoteApiError("remote transport failed") from error

    @staticmethod
    def _close(stream: BinaryIO) -> None:
        try:
            stream.close()
        except Exception:
            pass

    @classmethod
    def _job_path(cls, job_id: str) -> str:
        return "/v1/jobs/" + cls._segment(job_id)

    @staticmethod
    def _segment(value: str) -> str:
        if not isinstance(value, str) or not value.strip():
            raise ValueError("resource id must be non-empty")
        return quote(value, safe="")

    @staticmethod
    def _raise_status(status: int) -> None:
        if status == 404:
            raise RemoteNotFound("remote resource not found")
        if status == 409:
            raise RemoteConflict("remote request conflicts with current state")
        raise RemoteApiError("remote request failed with status {0}".format(status))

    @classmethod
    def _validate_submit(cls, command: Mapping[str, object]) -> None:
        if not isinstance(command, Mapping) or set(command) != cls._SUBMIT_FIELDS:
            raise ValueError("submit command has an invalid shape")
        for name in ("namespace_id", "idempotency_key", "deployment_id"):
            value = command[name]
            if not isinstance(value, str) or not value.strip():
                raise ValueError("{0} must be non-empty".format(name))
        if not cls._IDEMPOTENCY_KEY.fullmatch(cast(str, command["idempotency_key"])):
            raise ValueError("idempotency_key has an invalid format or length")
        request = command["request"]
        if not isinstance(request, Mapping):
            raise ValueError("request must be an object")
        cls._validate_remote_value(request)
        inputs = command["inputs"]
        if not isinstance(inputs, (tuple, list)):
            raise ValueError("inputs must be a list")
        roles = set()
        for item in inputs:
            if not isinstance(item, Mapping) or set(item) != {"role", "asset_id"}:
                raise ValueError("each input must contain role and asset_id")
            role, asset_id = item["role"], item["asset_id"]
            if not isinstance(role, str) or not role.strip():
                raise ValueError("input role must be non-empty")
            if not isinstance(asset_id, str) or not asset_id.strip():
                raise ValueError("input asset_id must be non-empty")
            if role in roles:
                raise ValueError("input roles must be unique")
            roles.add(role)
        required = command["required_artifact_kinds"]
        if (
            not isinstance(required, (tuple, list))
            or not required
            or not all(isinstance(item, str) and item.strip() for item in required)
            or len(set(required)) != len(required)
        ):
            raise ValueError("required_artifact_kinds must be unique non-empty strings")

    @classmethod
    def _validate_remote_value(cls, value: object) -> None:
        if value is None or isinstance(value, (bool, int, str)):
            return
        if isinstance(value, float):
            if not math.isfinite(value):
                raise ValueError("request cannot contain non-finite numbers")
            return
        if isinstance(value, Mapping):
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("request keys must be strings")
                normalized = key.lower()
                if normalized in cls._PROHIBITED_REQUEST_KEYS or normalized.endswith("_path"):
                    raise ValueError("request contains a prohibited server-local field")
                cls._validate_remote_value(item)
            return
        if isinstance(value, (tuple, list)):
            for item in value:
                cls._validate_remote_value(item)
            return
        raise ValueError("request must contain only JSON values")
