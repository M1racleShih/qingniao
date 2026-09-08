from __future__ import annotations

import json
import threading
from contextlib import asynccontextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from types import SimpleNamespace

import httpx
import pytest

from qingniao.config import ConfigStore, validate_config
from qingniao.gateway import Gateway
from qingniao.tokens import token_hash


class FakeClock:
    def __init__(self, now: float = 1000.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


def make_config_dict(
    p_base_url: str = "http://127.0.0.1:9101",
    q_base_url: str = "http://127.0.0.1:9102",
    p_env: str = "QING_TEST_P",
    p_auth: str = "bearer",
    q_auth: str = "bearer",
    main_dest: str = "model-p",
) -> dict:
    return {
        "providers": {
            "prov-p": {"base_url": p_base_url, "credential_env": p_env, "auth": p_auth},
            "prov-q": {"base_url": q_base_url, "credential_env": "QING_TEST_Q", "auth": q_auth},
        },
        "models": {
            "model-p": {"provider": "prov-p", "upstream_model": "vendor/p"},
            "model-q": {"provider": "prov-q", "upstream_model": "vendor/q"},
        },
        "defaults": {
            "model": "req-main",
            "aux_model": "req-aux",
            "routes": {"req-main": main_dest, "req-aux": main_dest},
        },
    }


class _FixtureHoldTimeout(OSError):
    """A fixture hold timed out; the test must fail, never progress."""


class _UpstreamHandler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _handle(self):
        import time as _time

        sse = getattr(self.server, "sse", None)
        if sse is not None:
            self._handle_sse(sse)
            return
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        hold = getattr(self.server, "hold", None)
        if hold is not None:
            hold.wait(timeout=10)
        delay = getattr(self.server, "delay", 0)
        if delay:
            _time.sleep(delay)
        status, payload, *rest = self.server.responses.pop(0) if self.server.responses else (200, {"ok": True})
        content_type = rest[0] if rest else "application/json"
        extra_headers = rest[1] if len(rest) > 1 else {}
        out = payload if isinstance(payload, bytes) else __import__("json").dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        for key, value in extra_headers.items():
            self.send_header(key, value)
        if not any(k.lower() == "transfer-encoding" for k in extra_headers):
            self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        try:
            self.wfile.write(out)
        except OSError as exc:
            self.server.write_errors.append((-1, type(exc).__name__))
            self.close_connection = True
            return
        self._probe_closed(-1)

    def _probe_closed(self, index: int) -> bool:
        """Non-blocking EOF/RST observation on the peer socket."""
        import socket as _socket

        try:
            data = self.connection.recv(1, _socket.MSG_PEEK | _socket.MSG_DONTWAIT)
        except BlockingIOError:
            return False
        except OSError as exc:
            self.server.write_errors.append((index, type(exc).__name__))
            return True
        if data == b"":
            self.server.eof_observed.append(index)
            return True
        return False

    def _wait_hold(self, hold, request_index: int, chunk_index: int) -> None:
        """Wait on a hold event; a timeout ABORTS instead of silently
        releasing, so a test can never mistake a timeout for progress."""
        if hold is None:
            return
        self.server.gate_waiters += 1
        try:
            ok = hold.wait(timeout=getattr(self.server, "gate_timeout", 10))
        finally:
            self.server.gate_waiters -= 1
        if not ok:
            self.server.gate_timeouts.append((request_index, chunk_index))
            raise _FixtureHoldTimeout(f"request {request_index}, chunk {chunk_index}")

    def _handle_sse(self, chunks):
        """Stream a scripted SSE response.

        server.sse: list of byte chunks.
        server.before_headers: optional Event held before status/headers.
        server.chunk_gates: optional list (aligned with chunks) of Events
            held before writing each chunk (applies to every request).
        server.request_chunk_gates: optional {request_index: [Event|None]}
            per-request chunk gates; a request listed here uses that list
            instead of server.chunk_gates, so concurrent requests to the
            same upstream can hold one stream without blocking another.
        server.abort_after: optional index; the connection closes abruptly
        after that chunk without message_stop.
        Hold timeouts are recorded in server.gate_timeouts and abort the
        connection; write failures are recorded in server.write_errors.
        """
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        request_index = len(self.server.requests)
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "headers": {k.lower(): v for k, v in self.headers.items()},
                "body": body,
            }
        )
        try:
            self._wait_hold(getattr(self.server, "before_headers", None), request_index, -1)
            chunked = bool(getattr(self.server, "sse_chunked", False))
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            if chunked:
                self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            per_request = getattr(self.server, "request_chunk_gates", None) or {}
            gates = per_request.get(request_index)
            if gates is None:
                gates = getattr(self.server, "chunk_gates", None) or []
            abort_after = getattr(self.server, "abort_after", None)
            for index, chunk in enumerate(chunks):
                self._wait_hold(gates[index] if index < len(gates) else None, request_index, index)
                if chunked:
                    frame = f"{len(chunk):x}\r\n".encode() + chunk + b"\r\n"
                    self.wfile.write(frame)
                    self.wfile.flush()
                else:
                    self.wfile.write(chunk)
                    self.wfile.flush()
                if self._probe_closed(index):
                    self.close_connection = True
                    return
                if abort_after is not None and index == abort_after:
                    self.close_connection = True
                    return
        except OSError as exc:
            if not isinstance(exc, _FixtureHoldTimeout):
                self.server.write_errors.append((-1, type(exc).__name__))
            self.close_connection = True

    do_POST = _handle
    do_GET = _handle


@pytest.fixture(autouse=True)
def _isolate_proxy_env(monkeypatch):
    """Default local fixtures must not depend on ambient proxy settings.

    Proxy behaviour is tested explicitly in test_proxy_env.py; tests that
    need a proxy set the variables themselves after this runs.
    """
    for var in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "no_proxy",
    ):
        monkeypatch.delenv(var, raising=False)


def sse_event(name: str, data: dict) -> bytes:
    return f"event: {name}\ndata: {json.dumps(data)}\n\n".encode()


@pytest.fixture
def upstream_factory():
    servers = []

    def start(response: tuple[int, object] = (200, {"ok": True})):
        server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
        server.daemon_threads = True
        server.requests = []
        server.responses = [response]
        server.write_errors = []
        server.eof_observed = []
        server.gate_timeouts = []
        server.gate_waiters = 0
        threading.Thread(target=server.serve_forever, daemon=True).start()
        servers.append(server)
        return server

    yield start
    for server in servers:
        server.shutdown()
        server.server_close()


@pytest.fixture
def make_gateway_app(tmp_path):
    @asynccontextmanager
    async def _make(config: dict | None = None, clock=None):
        from qingniao.app import build_app

        store = ConfigStore(tmp_path / "config.json")
        gateway = Gateway(
            validate_config(config if config is not None else make_config_dict()),
            store,
            clock=clock or FakeClock(),
        )
        admin_token = "test-admin-token"
        app = build_app(gateway, admin_token_hash=token_hash(admin_token))
        async with app.router.lifespan_context(app):
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://gateway.test") as client:
                yield SimpleNamespace(
                    gateway=gateway,
                    client=client,
                    admin_token=admin_token,
                    admin_headers={"authorization": f"Bearer {admin_token}"},
                )

    return _make
