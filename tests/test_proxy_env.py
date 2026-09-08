"""Proxy-environment robustness: the gateway must start and behave honestly
under synthetic socks5h proxy settings, without any real network access."""

from __future__ import annotations

import importlib.util
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from tests.conftest import make_config_dict

DEAD_SOCKS_PROXY = "socks5h://127.0.0.1:9"


def _upstream_url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


async def _create_instance(ctx) -> str:
    r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={})
    assert r.status_code == 201, r.text
    return r.json()["token"]


async def test_app_starts_and_fails_closed_with_socks5h_env(
    make_gateway_app, upstream_factory, monkeypatch
):
    upstream = upstream_factory()
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    monkeypatch.setenv("ALL_PROXY", DEAD_SOCKS_PROXY)
    monkeypatch.delenv("NO_PROXY", raising=False)
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        assert r.status_code == 200

        token = await _create_instance(ctx)
        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "req-main", "stream": False, "messages": []},
        )
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_error"
        assert len(upstream.requests) == 0


async def test_no_proxy_bypasses_socks_env_for_loopback(
    make_gateway_app, upstream_factory, monkeypatch
):
    upstream = upstream_factory((200, {"ok": True}))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    monkeypatch.setenv("ALL_PROXY", DEAD_SOCKS_PROXY)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        token = await _create_instance(ctx)
        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "req-main", "stream": False, "messages": []},
        )
        assert r.status_code == 200
        assert len(upstream.requests) == 1


def test_launcher_experiment_control_get_never_reaches_proxy(monkeypatch):
    """E2: the launcher experiment's control client must ignore ambient
    proxies entirely — no control request (and never the control token)
    may reach a proxy endpoint. A capture proxy records any request that
    a proxy-honoring client would have sent."""
    experiment = (
        Path(__file__).resolve().parent.parent
        / "experiments" / "claude-launcher-verification" / "run_experiment.py"
    )
    spec = importlib.util.spec_from_file_location("qingniao_launcher_experiment", experiment)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    captured: list[tuple[str, bool]] = []

    class CaptureProxy(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            # Record only the path and header presence; never the value.
            captured.append((self.path, "authorization" in self.headers))
            body = b'{"error":{"code":"proxy_capture"}}'
            self.send_response(403)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    class LoopbackControl(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *args):
            pass

        def do_GET(self):
            body = b'{"served":"direct"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    proxy = ThreadingHTTPServer(("127.0.0.1", 0), CaptureProxy)
    control = ThreadingHTTPServer(("127.0.0.1", 0), LoopbackControl)
    proxy.daemon_threads = control.daemon_threads = True
    servers = (proxy, control)
    threads = [threading.Thread(target=s.serve_forever, daemon=True) for s in servers]
    for thread in threads:
        thread.start()
    try:
        proxy_url = f"http://127.0.0.1:{proxy.server_address[1]}"
        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
            monkeypatch.setenv(var, proxy_url)
        for var in ("NO_PROXY", "no_proxy"):
            monkeypatch.delenv(var, raising=False)

        # Load the experiment module only under the hostile proxy env: an
        # opener built at import time from a plain build_opener() would
        # capture the dead proxy here and fail this test. The explicit
        # ProxyHandler({}) must ignore the environment instead.
        experiment = (
            Path(__file__).resolve().parent.parent
            / "experiments" / "claude-launcher-verification" / "run_experiment.py"
        )
        spec = importlib.util.spec_from_file_location("qingniao_launcher_experiment", experiment)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        payload = module.control_get(
            control.server_address[1], "synthetic-control-token", "/control/v1/config"
        )
        assert payload == {"served": "direct"}
        assert captured == [], f"control traffic reached the capture proxy: {captured}"
    finally:
        for server in servers:
            server.shutdown()
            server.server_close()
