"""Framework-neutral HTTP-shaped adapter for the Phase 3E application service."""

from __future__ import annotations

import io
import json
import re
from dataclasses import dataclass
from typing import BinaryIO, Mapping, Optional, Tuple
from urllib.parse import parse_qs, unquote, urlsplit

from .application import (
    AccessDenied,
    ApplicationService,
    ArtifactView,
    AuthenticationFailed,
    Authenticator,
    EventView,
    InvalidRequest,
    JobView,
    ResourceNotFound,
    SubmitJob,
    UploadView,
)
from .domain import ArtifactKind, JobInput
from .errors import IdempotencyConflictError, StateTransitionError


@dataclass(frozen=True)
class ApiRequest:
    method: str
    path: str
    headers: Mapping[str, str]
    body: bytes = b""


@dataclass
class ApiResponse:
    status: int
    headers: Mapping[str, str]
    body: BinaryIO


class ApplicationApi:
    """Translate HTTP-shaped values without importing an HTTP framework."""

    _MAX_JSON_BYTES = 1024 * 1024
    _IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

    def __init__(
        self,
        service: ApplicationService,
        authenticator: Authenticator,
        *,
        max_body_bytes: int = _MAX_JSON_BYTES,
    ) -> None:
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 1
        ):
            raise ValueError("max_body_bytes must be a positive integer")
        self._service = service
        self._authenticator = authenticator
        self._max_body_bytes = max_body_bytes

    def handle(self, request: ApiRequest) -> ApiResponse:
        try:
            method = request.method.upper()
            self._validate_body_envelope(method, request.headers, request.body)
            principal = self._authenticate(request.headers)
            segments = self._segments(request.path)
            if method == "POST" and segments == ("v1", "assets", "uploads"):
                namespace, size, sha256 = self._upload_request(request.body)
                return self._json(
                    201,
                    self._upload(self._service.initiate_upload(principal, namespace, size, sha256)),
                )
            if (
                method == "PUT"
                and len(segments) == 4
                and segments[:3]
                == (
                    "v1",
                    "assets",
                    "uploads",
                )
            ):
                return self._json(
                    200,
                    self._upload(self._service.upload_asset(principal, segments[3], request.body)),
                )
            if method == "POST" and segments == ("v1", "jobs"):
                return self._json(
                    201, self._job(self._service.submit(principal, self._submit(request)))
                )
            if len(segments) >= 3 and segments[:2] == ("v1", "jobs"):
                job_id = segments[2]
                if method == "GET" and len(segments) == 3:
                    return self._json(200, self._job(self._service.get_job(principal, job_id)))
                if method == "POST" and segments[3:] == ("cancel",):
                    return self._json(202, self._job(self._service.cancel(principal, job_id)))
                if method == "GET" and segments[3:] == ("events",):
                    cursor, limit = self._event_query(request.path)
                    page = self._service.events(principal, job_id, cursor=cursor, limit=limit)
                    return self._json(
                        200,
                        {
                            "events": [self._event(event) for event in page.events],
                            "next_cursor": page.next_cursor,
                        },
                    )
                if method == "GET" and segments[3:] == ("artifacts",):
                    artifacts = self._service.artifacts(principal, job_id)
                    return self._json(
                        200, {"artifacts": [self._artifact(artifact) for artifact in artifacts]}
                    )
                if method == "GET" and len(segments) == 5 and segments[3] == "artifacts":
                    download = self._service.download(principal, job_id, segments[4])
                    return ApiResponse(
                        200,
                        {
                            "Content-Length": str(download.artifact.size_bytes),
                            "Content-Type": download.artifact.content_type,
                            "X-Content-SHA256": download.artifact.sha256,
                        },
                        download.reader,
                    )
            raise ResourceNotFound("route not found")
        except AuthenticationFailed:
            response = self._error(401, "authentication failed")
            response.headers = {
                **response.headers,
                "WWW-Authenticate": 'Bearer realm="nano-aural-runtime"',
            }
            return response
        except AccessDenied:
            return self._error(403, "access denied")
        except ResourceNotFound:
            return self._error(404, "resource not found")
        except (InvalidRequest, TypeError, ValueError):
            return self._error(400, "invalid request")
        except (IdempotencyConflictError, StateTransitionError):
            return self._error(409, "request conflicts with current state")

    def _validate_body_envelope(self, method: str, headers: Mapping[str, str], body: bytes) -> None:
        if not isinstance(body, bytes):
            raise InvalidRequest("request body must be bytes")
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        raw_length = normalized.get("content-length")
        if method in {"POST", "PUT", "PATCH"}:
            if raw_length is None or not raw_length.isascii() or not raw_length.isdecimal():
                raise InvalidRequest("Content-Length is required")
            length = int(raw_length)
            if raw_length != str(length) or length != len(body):
                raise InvalidRequest("Content-Length does not match the request body")
            if length > self._max_body_bytes:
                raise InvalidRequest("request is too large")
        elif body:
            raise InvalidRequest("request method does not accept a body")

    def _authenticate(self, headers: Mapping[str, str]):
        normalized = {str(key).lower(): str(value) for key, value in headers.items()}
        authorization = normalized.get("authorization")
        if authorization is None:
            raise AuthenticationFailed("authorization is missing")
        try:
            return self._authenticator.authenticate(authorization)
        except AuthenticationFailed:
            raise
        except Exception as error:
            raise AuthenticationFailed("authorization is invalid") from error

    def _submit(self, request: ApiRequest) -> SubmitJob:
        if len(request.body) > self._MAX_JSON_BYTES:
            raise InvalidRequest("request is too large")
        try:
            payload = json.loads(request.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidRequest("request JSON is invalid") from error
        required = {
            "namespace_id",
            "idempotency_key",
            "deployment_id",
            "request",
            "inputs",
            "required_artifact_kinds",
        }
        if not isinstance(payload, dict) or set(payload) != required:
            raise InvalidRequest("submit body has an invalid shape")
        if not isinstance(payload["request"], dict):
            raise InvalidRequest("request must be an object")
        raw_inputs = payload["inputs"]
        if not isinstance(raw_inputs, list):
            raise InvalidRequest("inputs must be a list")
        inputs = []
        for item in raw_inputs:
            if not isinstance(item, dict) or set(item) != {"role", "asset_id"}:
                raise InvalidRequest("input has an invalid shape")
            inputs.append(JobInput(item["role"], item["asset_id"]))
        raw_kinds = payload["required_artifact_kinds"]
        if not isinstance(raw_kinds, list):
            raise InvalidRequest("required artifact kinds must be a list")
        headers = {str(key).lower(): str(value) for key, value in request.headers.items()}
        header_key = headers.get("idempotency-key")
        body_key = payload["idempotency_key"]
        if (
            not isinstance(body_key, str)
            or header_key is None
            or header_key != body_key
            or self._IDEMPOTENCY_KEY.fullmatch(body_key) is None
        ):
            raise InvalidRequest("Idempotency-Key is missing, invalid, or conflicts with body")
        return SubmitJob(
            payload["namespace_id"],
            payload["idempotency_key"],
            payload["deployment_id"],
            payload["request"],
            tuple(inputs),
            tuple(ArtifactKind(item) for item in raw_kinds),
        )

    @staticmethod
    def _upload_request(body: bytes) -> Tuple[str, int, Optional[str]]:
        try:
            payload = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise InvalidRequest("request JSON is invalid") from error
        if not isinstance(payload, dict) or set(payload) != {
            "namespace_id",
            "expected_size_bytes",
            "expected_sha256",
        }:
            raise InvalidRequest("upload initiation body has an invalid shape")
        namespace = payload["namespace_id"]
        size = payload["expected_size_bytes"]
        sha256 = payload["expected_sha256"]
        if not isinstance(namespace, str) or not namespace.strip():
            raise InvalidRequest("namespace_id must be non-empty")
        if isinstance(size, bool) or not isinstance(size, int) or size < 0:
            raise InvalidRequest("expected_size_bytes must be non-negative")
        if sha256 is not None and (
            not isinstance(sha256, str) or len(sha256) != 64 or sha256 != sha256.lower()
        ):
            raise InvalidRequest("expected_sha256 must be a lowercase full SHA-256")
        if isinstance(sha256, str):
            try:
                int(sha256, 16)
            except ValueError as error:
                raise InvalidRequest("expected_sha256 must be hexadecimal") from error
        return namespace, size, sha256

    @staticmethod
    def _segments(path: str) -> Tuple[str, ...]:
        if not isinstance(path, str) or not path.startswith("/"):
            raise ResourceNotFound("route not found")
        parsed = urlsplit(path)
        if parsed.scheme or parsed.netloc or parsed.fragment:
            raise ResourceNotFound("route not found")
        segments = tuple(unquote(item) for item in parsed.path.split("/") if item)
        if any(not item or "/" in item or "\\" in item for item in segments):
            raise ResourceNotFound("route not found")
        return segments

    @staticmethod
    def _event_query(path: str) -> Tuple[Optional[str], int]:
        query = parse_qs(urlsplit(path).query, keep_blank_values=True, strict_parsing=True)
        if set(query) - {"cursor", "limit"} or any(len(values) != 1 for values in query.values()):
            raise InvalidRequest("event query is invalid")
        cursor = query.get("cursor", [None])[0]
        try:
            limit = int(query.get("limit", ["50"])[0])
        except (TypeError, ValueError) as error:
            raise InvalidRequest("event limit is invalid") from error
        return cursor, limit

    @staticmethod
    def _job(view: JobView) -> Mapping[str, object]:
        return {
            "job_id": view.job_id,
            "namespace_id": view.namespace_id,
            "state": view.state.value,
            "cancel_requested": view.cancel_requested,
            "winning_attempt_id": view.winning_attempt_id,
        }

    @staticmethod
    def _event(view: EventView) -> Mapping[str, object]:
        return {
            "event_id": view.event_id,
            "type": view.event_type.value,
            "attempt_id": view.attempt_id,
            "payload": dict(view.payload),
        }

    @staticmethod
    def _artifact(view: ArtifactView) -> Mapping[str, object]:
        return {
            "artifact_id": view.artifact_id,
            "kind": view.kind.value,
            "sha256": view.sha256,
            "size_bytes": view.size_bytes,
            "content_type": view.content_type,
        }

    @staticmethod
    def _upload(view: UploadView) -> Mapping[str, object]:
        return {
            "session_id": view.session_id,
            "namespace_id": view.namespace_id,
            "expected_size_bytes": view.expected_size_bytes,
            "version": view.version,
            "state": view.state,
            "asset_id": view.asset_id,
        }

    @staticmethod
    def _json(status: int, payload: Mapping[str, object]) -> ApiResponse:
        body = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        return ApiResponse(status, {"Content-Type": "application/json"}, io.BytesIO(body))

    @classmethod
    def _error(cls, status: int, message: str) -> ApiResponse:
        return cls._json(status, {"error": message})
