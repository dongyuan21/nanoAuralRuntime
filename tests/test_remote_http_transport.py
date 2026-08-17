# pyright: reportMissingImports=false
from __future__ import annotations

from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from typing import Iterator, Type

import pytest

from nano_aural_runtime_remote.client import UrllibTransport


@contextmanager
def running(handler: Type[BaseHTTPRequestHandler]) -> Iterator[ThreadingHTTPServer]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def test_transport_requires_https_unless_loopback_http_is_explicit() -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        UrllibTransport("http://127.0.0.1:8080")
    with pytest.raises(ValueError, match="HTTPS"):
        UrllibTransport("http://example.test", allow_loopback_http=True)
    UrllibTransport("http://localhost:8080", allow_loopback_http=True)
    UrllibTransport("https://example.test")


def test_real_cross_origin_redirect_is_not_followed_and_cannot_leak_authorization() -> None:
    destination_requests: list[str] = []

    class Destination(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            destination_requests.append(self.headers.get("Authorization", ""))
            self.send_response(200)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            del format, args

    with running(Destination) as destination:
        destination_port = destination.server_address[1]

        class Redirect(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                self.send_response(302)
                self.send_header("Location", "http://127.0.0.1:{0}/stolen".format(destination_port))
                self.end_headers()

            def log_message(self, format: str, *args: object) -> None:
                del format, args

        with running(Redirect) as redirect:
            redirect_port = redirect.server_address[1]
            transport = UrllibTransport(
                "http://127.0.0.1:{0}".format(redirect_port),
                allow_loopback_http=True,
            )
            response = transport.request(
                "GET", "/redirect", {"Authorization": "Bearer should-not-leak"}
            )
            try:
                assert response.status == 302
            finally:
                response.body.close()

    assert destination_requests == []
