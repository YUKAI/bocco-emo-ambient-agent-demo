"""Small loopback HTTP server used by Hermes adapter tests."""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any


@dataclass(frozen=True, slots=True)
class CapturedRequest:
    path: str
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        return json.loads(self.body)


@dataclass(frozen=True, slots=True)
class FakeResponse:
    status: int = 200
    body: bytes = b"{}"
    content_type: str = "application/json"
    delay_seconds: float = 0.0

    @classmethod
    def json(
        cls,
        payload: Any,
        *,
        status: int = 200,
        delay_seconds: float = 0.0,
    ) -> FakeResponse:
        return cls(
            status=status,
            body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            delay_seconds=delay_seconds,
        )


@dataclass(slots=True)
class FakeServerState:
    responder: Callable[[CapturedRequest], FakeResponse]
    requests: list[CapturedRequest] = field(default_factory=list)


@contextmanager
def fake_server(
    responder: Callable[[CapturedRequest], FakeResponse],
) -> Iterator[tuple[str, FakeServerState]]:
    state = FakeServerState(responder=responder)

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - stdlib callback name
            length = int(self.headers.get("Content-Length", "0"))
            request = CapturedRequest(
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=self.rfile.read(length),
            )
            state.requests.append(request)
            response = state.responder(request)
            if response.delay_seconds:
                time.sleep(response.delay_seconds)

            try:
                self.send_response(response.status)
                self.send_header("Content-Type", response.content_type)
                self.send_header("Content-Length", str(len(response.body)))
                self.end_headers()
                self.wfile.write(response.body)
            except (BrokenPipeError, ConnectionResetError):
                # Expected when a timeout test closes the socket first.
                pass

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address
        yield f"http://{host}:{port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)
