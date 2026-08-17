"""Startable standard-library WSGI adapter for the framework-free API."""

from __future__ import annotations

import json
import time
from http import HTTPStatus
from typing import Callable, Iterable, Mapping, MutableMapping, Optional, Protocol, Tuple, cast
from wsgiref.simple_server import WSGIServer, make_server

from .api import ApiRequest, ApiResponse, ApplicationApi

StartResponse = Callable[[str, list[Tuple[str, str]]], object]


class HttpObserver(Protocol):
    def record(self, method: str, path: str, status: int, duration_ms: float) -> None: ...


class WsgiApplication:
    """Bound request reader which rejects oversized bodies before reading them."""

    def __init__(
        self,
        api: ApplicationApi,
        *,
        max_body_bytes: int = 1024 * 1024,
        observer: Optional[HttpObserver] = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (
            isinstance(max_body_bytes, bool)
            or not isinstance(max_body_bytes, int)
            or max_body_bytes < 1
        ):
            raise ValueError("max_body_bytes must be a positive integer")
        self._api = api
        self._max_body_bytes = max_body_bytes
        self._observer = observer
        self._monotonic = monotonic

    def __call__(
        self, environ: MutableMapping[str, object], start_response: StartResponse
    ) -> Iterable[bytes]:
        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        started = self._monotonic()
        path = str(environ.get("PATH_INFO", ""))
        query = str(environ.get("QUERY_STRING", ""))
        if query:
            path += "?" + query
        try:
            body = self._read_body(method, environ)
        except _EnvelopeError as error:
            response = self._error(error.status, error.message)
        else:
            request = ApiRequest(method, path, self._headers(environ), body)
            response = self._api.handle(request)
        self._observe(method, path, response.status, started)
        return self._respond(response, start_response)

    def _observe(self, method: str, path: str, status: int, started: float) -> None:
        if self._observer is None:
            return
        try:
            elapsed = max(0.0, (self._monotonic() - started) * 1000.0)
            self._observer.record(method, path, status, elapsed)
        except Exception:
            # Metrics/log export must not change the API result.  Operator
            # supervision can detect a broken sink independently.
            return

    def _read_body(self, method: str, environ: Mapping[str, object]) -> bytes:
        if environ.get("HTTP_TRANSFER_ENCODING") not in (None, ""):
            raise _EnvelopeError(400, "Transfer-Encoding is not accepted")
        raw = environ.get("CONTENT_LENGTH")
        if method not in {"POST", "PUT", "PATCH"}:
            if raw not in (None, "", "0"):
                raise _EnvelopeError(400, "request method does not accept a body")
            return b""
        if not isinstance(raw, str) or not raw.isascii() or not raw.isdecimal():
            raise _EnvelopeError(411, "Content-Length is required")
        length = int(raw)
        if raw != str(length):
            raise _EnvelopeError(400, "Content-Length is invalid")
        if length > self._max_body_bytes:
            raise _EnvelopeError(413, "request is too large")
        stream = environ.get("wsgi.input")
        if stream is None or not hasattr(stream, "read"):
            raise _EnvelopeError(400, "request body is unavailable")
        body = cast(object, stream).read(length)  # type: ignore[attr-defined]
        if not isinstance(body, bytes) or len(body) != length:
            raise _EnvelopeError(400, "request body is truncated")
        return body

    @staticmethod
    def _headers(environ: Mapping[str, object]) -> Mapping[str, str]:
        headers: dict[str, str] = {}
        for name, value in environ.items():
            if name.startswith("HTTP_"):
                headers[name[5:].replace("_", "-")] = str(value)
        for name in ("CONTENT_LENGTH", "CONTENT_TYPE"):
            value = environ.get(name)
            if value not in (None, ""):
                headers[name.replace("_", "-")] = str(value)
        return headers

    @staticmethod
    def _respond(response: ApiResponse, start_response: StartResponse) -> Iterable[bytes]:
        phrase = HTTPStatus(response.status).phrase
        start_response(
            "{0} {1}".format(response.status, phrase),
            [(str(key), str(value)) for key, value in response.headers.items()],
        )

        def chunks() -> Iterable[bytes]:
            try:
                while True:
                    chunk = response.body.read(64 * 1024)
                    if not chunk:
                        break
                    yield chunk
            finally:
                response.body.close()

        return chunks()

    @staticmethod
    def _error(status: int, message: str) -> ApiResponse:
        from io import BytesIO

        body = json.dumps({"error": message}, separators=(",", ":"), sort_keys=True).encode()
        return ApiResponse(
            status,
            {"Content-Length": str(len(body)), "Content-Type": "application/json"},
            BytesIO(body),
        )


class _EnvelopeError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def create_server(
    api: ApplicationApi,
    *,
    host: str = "127.0.0.1",
    port: int = 0,
    max_body_bytes: int = 1024 * 1024,
    observer: Optional[HttpObserver] = None,
) -> WSGIServer:
    """Create a real WSGI server; callers own shutdown and close."""

    return make_server(
        host,
        port,
        WsgiApplication(api, max_body_bytes=max_body_bytes, observer=observer),
    )


def serve(
    api: ApplicationApi,
    *,
    host: str = "127.0.0.1",
    port: int = 8080,
    max_body_bytes: int = 1024 * 1024,
    observer: Optional[HttpObserver] = None,
) -> None:
    """Run the standard-library deployment adapter until interrupted."""

    with create_server(
        api,
        host=host,
        port=port,
        max_body_bytes=max_body_bytes,
        observer=observer,
    ) as server:
        server.serve_forever()
