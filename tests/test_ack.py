"""CLI acknowledgement semantics: applied-only success, unconfirmed timeouts,
CAS conflicts and malformed 200 bodies."""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from typer.testing import CliRunner

from qingniao import state as state_mod
from qingniao.app import build_app
from qingniao import cli
from qingniao.config import ConfigStore, validate_config
from qingniao.gateway import Gateway
from qingniao.tokens import new_control_token, token_hash
from tests.conftest import make_config_dict

runner = CliRunner()


def _wait_started(server, timeout=15.0):
    deadline = time.time() + timeout
    while not server.started:
        if time.time() > deadline:
            raise TimeoutError("uvicorn did not start")
        time.sleep(0.02)


class LiveGateway:
    def __init__(self, tmp_path: Path, wrap_app=None):
        self.gateway = Gateway(validate_config(make_config_dict()), ConfigStore(tmp_path / "config.json"))
        self.control_token = new_control_token()
        app = build_app(self.gateway, admin_token_hash=token_hash(self.control_token))
        if wrap_app is not None:
            app = wrap_app(app, self.gateway)
        self.server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", access_log=False, lifespan="on")
        )
        threading.Thread(target=self.server.run, daemon=True).start()
        _wait_started(self.server)
        self.port = self.server.servers[0].sockets[0].getsockname()[1]
        self.state_dir = tmp_path / "state"
        state_mod.prepare_state_dir(self.state_dir)
        state_mod.write_discovery(self.state_dir, port=self.port, pid=1, control_token=self.control_token)

    def stop(self):
        self.server.should_exit = True
        deadline = time.time() + 10
        while self.server.started and time.time() < deadline:
            time.sleep(0.02)

    def get(self, path: str) -> httpx.Response:
        with httpx.Client(
            base_url=f"http://127.0.0.1:{self.port}",
            headers={"authorization": f"Bearer {self.control_token}"},
            timeout=10.0,
            trust_env=False,
        ) as client:
            return client.get(path)


def _delay_put_responses(delay: float):
    def wrap(app, gateway):
        async def asgi(scope, receive, send):
            if scope["type"] == "http" and scope["method"] == "PUT" and scope["path"].endswith("/routes"):
                async def delayed_send(message):
                    if message["type"] == "http.response.start":
                        # The route change is already applied server-side;
                        # only the acknowledgement travels late.
                        await asyncio.sleep(delay)
                    await send(message)

                await app(scope, receive, delayed_send)
                return
            await app(scope, receive, send)

        return asgi

    return wrap


def _bump_route_before_put(app, gateway: Gateway):
    async def asgi(scope, receive, send):
        if scope["type"] == "http" and scope["method"] == "PUT" and scope["path"].endswith("/routes"):
            for instance in gateway._instances.values():
                if not instance.ended:
                    gateway.set_route(instance.id, "req-main", "model-q", instance.revision)
        await app(scope, receive, send)

    return asgi


def test_applied_then_delayed_ack_timeout_unconfirmed_with_applied_readback(tmp_path, monkeypatch):
    live = LiveGateway(tmp_path, wrap_app=_delay_put_responses(0.8))
    try:
        instance, _token = live.gateway.create_instance(label="ack")
        monkeypatch.setattr(cli, "CLI_TIMEOUT", 0.3)
        result = runner.invoke(
            cli.app,
            [
                "route", "set", "req-main", "model-q",
                "--instance", "ack",
                "--state-dir", str(live.state_dir),
            ],
        )
        combined = result.output
        try:
            combined += result.stderr or ""
        except Exception:
            pass
        assert result.exit_code == 1
        assert "unconfirmed" in combined
        assert "switched" not in combined

        response = live.get(f"/control/v1/instances/{instance.id}")
        status = response.json()["instance"]
        assert status["revision"] == 1
        assert status["routes"]["req-main"]["provider"] == "prov-q"
        assert status["routes"]["req-main"]["upstream_model"] == "vendor/q"
    finally:
        live.stop()


def test_cas_conflict_surfaces_to_cli_without_second_mutation(tmp_path):
    live = LiveGateway(tmp_path, wrap_app=_bump_route_before_put)
    try:
        instance, _token = live.gateway.create_instance(label="cas")
        result = runner.invoke(
            cli.app,
            [
                "route", "set", "req-main", "model-q",
                "--instance", "cas",
                "--state-dir", str(live.state_dir),
            ],
        )
        assert result.exit_code != 0
        combined = result.output
        try:
            combined += result.stderr or ""
        except Exception:
            pass
        assert "revision_conflict" in combined
        status = live.get(f"/control/v1/instances/{instance.id}").json()["instance"]
        assert status["revision"] == 1  # exactly the injected bump, nothing more
        assert status["routes"]["req-main"]["provider"] == "prov-q"
    finally:
        live.stop()


def _static_asgi(routes: dict):
    """Minimal fake gateway on real TCP answering exact (method, path) pairs."""

    async def asgi(scope, receive, send):
        if scope["type"] != "http":
            return
        key = (scope["method"], scope["path"])
        status, body = routes.get(key, (404, b'{"error":{"code":"not_found"}}'))
        if scope["method"] == "PUT":
            more = True
            while more:
                message = await receive()
                more = message.get("more_body", False)
        await send(
            {
                "type": "http.response.start",
                "status": status,
                "headers": [(b"content-type", b"application/json")],
            }
        )
        await send({"type": "http.response.body", "body": body})

    return asgi


@pytest.mark.parametrize(
    "ack_body",
    [
        b'{"applied": false, "revision": 1}',
        b'{"revision": 1}',
        b"not json at all",
        # S5: well-formed but mismatched acknowledgement (wrong instance,
        # wrong request model, wrong destination, stale revision)
        b'{"applied": true, "revision": 0, "instance": {"id": "i-other"}, "route": {"request_model": "other", "provider": "p", "upstream_model": "u"}}',
    ],
)
def test_malformed_acknowledgement_not_reported_as_success(tmp_path, ack_body):
    server = uvicorn.Server(
        uvicorn.Config(
            _static_asgi(
                {
                    ("GET", "/control/v1/instances"): (
                        200,
                        b'{"instances": [{"id": "i-fake", "label": "x", "revision": 0, "routes": {}}], "total": 1, "next_offset": null}',
                    ),
                    ("PUT", "/control/v1/instances/i-fake/routes"): (200, ack_body),
                }
            ),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    threading.Thread(target=server.run, daemon=True).start()
    _wait_started(server)
    port = server.servers[0].sockets[0].getsockname()[1]
    state_dir = tmp_path / "state"
    state_mod.prepare_state_dir(state_dir)
    state_mod.write_discovery(state_dir, port=port, pid=1, control_token="fake-token")
    try:
        result = runner.invoke(
            cli.app,
            [
                "route", "set", "req-main", "model-q",
                "--instance", "x",
                "--state-dir", str(state_dir),
            ],
            env={"COLUMNS": "200", "TERM": "dumb"},
        )
        combined = result.output
        try:
            combined += result.stderr or ""
        except Exception:
            pass
        assert result.exit_code != 0
        assert (
            "not a valid applied acknowledgement" in combined
            or "acknowledgement mismatched the intended change" in combined
        )
        assert "switched" not in combined
        assert "applied revision" not in combined
    finally:
        server.should_exit = True


def test_gateway_stopped_route_set_reports_connection_not_success(tmp_path):
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    state_dir = tmp_path / "state"
    state_mod.prepare_state_dir(state_dir)
    state_mod.write_discovery(state_dir, port=dead_port, pid=1, control_token="stale")
    result = runner.invoke(
        cli.app,
        ["route", "set", "req-main", "model-q", "--instance", "x", "--state-dir", str(state_dir)],
    )
    combined = result.output
    try:
        combined += result.stderr or ""
    except Exception:
        pass
    assert result.exit_code == 1
    assert "cannot reach gateway" in combined
    assert "switched" not in combined
