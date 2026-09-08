"""End-to-end TCP smoke: real serve subprocess, real HTTP client, real upstream."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer

import httpx
import pytest

from qingniao import state as state_mod
from tests.conftest import _UpstreamHandler, make_config_dict


def _start_serve(state_dir, env_extra: dict | None = None):
    env = dict(os.environ)
    env.pop("QING_TEST_P", None)
    if env_extra:
        env.update(env_extra)
    return subprocess.Popen(
        [sys.executable, "-m", "qingniao", "serve", "--state-dir", str(state_dir)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _stop(proc) -> str:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    return proc.stderr.read()


def _wait_discovery(state_dir, timeout=20.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = state_mod.read_discovery(state_dir)
        if data is not None:
            return data
        time.sleep(0.05)
    raise TimeoutError(f"gateway discovery file never appeared in {state_dir}")


def test_tcp_end_to_end_forward_restart_and_guard(tmp_path):
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    upstream.daemon_threads = True
    upstream.requests = []
    upstream.responses = [(200, {"id": "msg_tcp_1", "usage": {"input_tokens": 5, "output_tokens": 2}})]
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"

    state_dir = tmp_path / "state"
    proc = _start_serve(state_dir, {"QING_TEST_P": "tcp-upstream-secret"})
    try:
        discovery = _wait_discovery(state_dir)
        base = f"http://127.0.0.1:{discovery['port']}"
        admin = {"authorization": f"Bearer {discovery['control_token']}"}

        with httpx.Client(base_url=base, timeout=15.0) as client:
            config = make_config_dict(p_base_url=upstream_url)
            r = client.put("/control/v1/config", headers=admin, json=config)
            assert r.status_code == 200, r.text

            r = client.post("/control/v1/instances", headers=admin, json={"label": "smoke-a"})
            assert r.status_code == 201, r.text
            instance = r.json()["instance"]
            token = r.json()["token"]

            r = client.post(
                "/v1/messages?beta=true",
                headers={
                    "authorization": f"Bearer {token}",
                    "anthropic-version": "2023-06-01",
                    "anthropic-beta": "claude-code-20250219",
                },
                json={"model": "req-main", "stream": False, "max_tokens": 8, "messages": []},
            )
            assert r.status_code == 200, r.text
            assert r.json()["id"] == "msg_tcp_1"

            assert len(upstream.requests) == 1
            seen = upstream.requests[0]
            assert seen["path"] == "/v1/messages?beta=true"
            assert seen["headers"]["authorization"] == "Bearer tcp-upstream-secret"
            assert b'"model":"vendor/p"' in seen["body"] or '"model": "vendor/p"' in seen["body"].decode()

            r = client.delete(f"/control/v1/instances/{instance['id']}", headers=admin)
            assert r.status_code == 200
            r = client.post(
                "/v1/messages",
                headers={"authorization": f"Bearer {token}"},
                json={"model": "req-main", "messages": []},
            )
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "instance_ended"
            assert len(upstream.requests) == 1

            old_admin = dict(admin)
            old_instance_token = token
    finally:
        _stop(proc)

    assert not (state_dir / state_mod.DISCOVERY_NAME).exists()

    proc = _start_serve(state_dir, {"QING_TEST_P": "tcp-upstream-secret"})
    try:
        discovery2 = _wait_discovery(state_dir)
        assert discovery2["control_token"] != discovery["control_token"]
        base = f"http://127.0.0.1:{discovery2['port']}"
        with httpx.Client(base_url=base, timeout=15.0) as client:
            r = client.get("/control/v1/config", headers=old_admin)
            assert r.status_code == 401

            r = client.get(
                "/control/v1/config",
                headers={"authorization": f"Bearer {discovery2['control_token']}"},
            )
            assert r.status_code == 200
            assert r.json()["config"]["providers"]["prov-p"]["base_url"] == upstream_url

            r = client.post(
                "/v1/messages",
                headers={"authorization": f"Bearer {old_instance_token}"},
                json={"model": "req-main", "messages": []},
            )
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "invalid_instance_token"
            assert len(upstream.requests) == 1
    finally:
        _stop(proc)

    upstream.shutdown()
    upstream.server_close()


def test_single_gateway_guard(tmp_path):
    state_dir = tmp_path / "state"
    proc = _start_serve(state_dir, {"QING_TEST_P": "x"})
    try:
        discovery = _wait_discovery(state_dir)
        second = _start_serve(state_dir, {"QING_TEST_P": "x"})
        stderr = second.communicate(timeout=20)[1]
        assert second.returncode == 2
        assert "already running" in stderr

        still = state_mod.read_discovery(state_dir)
        assert still is not None and still["pid"] == discovery["pid"]
        with httpx.Client(
            base_url=f"http://127.0.0.1:{discovery['port']}",
            headers={"authorization": f"Bearer {discovery['control_token']}"},
            timeout=15.0,
        ) as client:
            r = client.get("/control/v1/config")
            assert r.status_code == 200
    finally:
        _stop(proc)
    assert not (state_dir / state_mod.DISCOVERY_NAME).exists()


def test_serve_startup_failure_no_listening_claim(tmp_path):
    """Lifespan startup failure after a valid config load: serve exits
    nonzero, never announces listening, and leaves no discovery file (E4).

    The unsupported proxy scheme makes the shared AsyncClient construction
    fail inside the application lifespan startup — after the persisted
    config has loaded successfully, so this exercises the actual readiness
    claim path instead of the earlier config-load failure exit."""
    state_dir = tmp_path / "state"
    proc = _start_serve(state_dir, {"ALL_PROXY": "unsupported-proxy://127.0.0.1:9"})
    try:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode != 0
        assert "listening" not in out
        assert not (state_dir / state_mod.DISCOVERY_NAME).exists()
    finally:
        if proc.poll() is None:
            _stop(proc)


def test_serve_config_load_failure_no_listening(tmp_path):
    """Invalid persisted config: serve exits nonzero with a clean error,
    never announces listening, and leaves no discovery file."""
    state_dir = tmp_path / "state"
    state_dir.mkdir(parents=True)
    (state_dir / "config.json").write_text('{"providers": "not-an-object"}')
    proc = _start_serve(state_dir)
    try:
        out, err = proc.communicate(timeout=30)
        assert proc.returncode != 0
        assert "listening" not in out
        assert not (state_dir / state_mod.DISCOVERY_NAME).exists()
    finally:
        if proc.poll() is None:
            _stop(proc)
