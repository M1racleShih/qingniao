"""End-to-end import acceptance: temp HOME, synthetic settings, no provider
environment variables, CLI import, a real gateway process and a fake Claude
client that issues one request through `qing run`, then a gateway restart.

Everything here runs against local fixtures; passing proves the local flow
only, never real Claude or provider compatibility.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import httpx
import pytest

from qingniao import launcher, state as state_mod
from tests.conftest import _UpstreamHandler
from tests.test_tcp_smoke import _wait_discovery

TOKEN = "sk-ant-api03-SYNTHETIC-BEARER-E2E"
API_KEY = "sk-ant-api03-SYNTHETIC-APIKEY-E2E"

FAKE_CLAUDE = '''#!/usr/bin/env python3
import json, sys, urllib.request
args = sys.argv[1:]
settings_path = args[args.index("--settings") + 1]
with open(settings_path) as fh:
    env = json.load(fh)["env"]
body = json.dumps({"model": env["ANTHROPIC_MODEL"], "stream": False}).encode()
req = urllib.request.Request(
    env["ANTHROPIC_BASE_URL"].rstrip("/") + "/v1/messages",
    data=body,
    headers={
        "content-type": "application/json",
        "authorization": "Bearer " + env["ANTHROPIC_AUTH_TOKEN"],
    },
)
with urllib.request.urlopen(req, timeout=15) as resp:
    sys.stderr.write(resp.read().decode())
'''


def _start_upstream() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    server.daemon_threads = True
    server.requests = []
    server.responses = []
    server.write_errors = []
    server.eof_observed = []
    server.gate_timeouts = []
    server.gate_waiters = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _minimal_env(home: Path) -> dict:
    # No ANTHROPIC_*, CLAUDE_* or provider variables exist in this environment.
    env = {"HOME": str(home), "PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if "TMPDIR" in os.environ:
        env["TMPDIR"] = os.environ["TMPDIR"]
    return env


def _write_settings(home: Path, payload: dict) -> bytes:
    claude_dir = home / ".claude"
    claude_dir.mkdir(parents=True, exist_ok=True)
    raw = json.dumps(payload, indent=2).encode()
    (claude_dir / "settings.json").write_bytes(raw)
    return raw


def _run_cli(args: list[str], env: dict) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "qingniao", *args],
        capture_output=True,
        text=True,
        env=env,
        timeout=60,
    )


def _start_serve(state_dir: Path, env: dict) -> subprocess.Popen:
    log = open(state_dir / "serve.log", "a")
    return subprocess.Popen(
        [sys.executable, "-m", "qingniao", "serve", "--state-dir", str(state_dir)],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )


def _stop(proc: subprocess.Popen) -> None:
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)


def _fake_claude_bin(tmp_path: Path) -> str:
    path = tmp_path / "fake-claude"
    path.write_text(FAKE_CLAUDE)
    path.chmod(0o755)
    return str(path)


@pytest.fixture
def e2e(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return home, state_dir, _minimal_env(home)


def test_import_to_first_request_bearer_and_restart(e2e, tmp_path, capsys):
    home, state_dir, env = e2e
    upstream = _start_upstream()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        settings_bytes = _write_settings(
            home,
            {
                "env": {
                    "ANTHROPIC_BASE_URL": upstream_url,
                    "ANTHROPIC_AUTH_TOKEN": TOKEN,
                    "ANTHROPIC_MODEL": "claude-main-e2e",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux-e2e",
                }
            },
        )

        imported = _run_cli(
            ["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env
        )
        assert imported.returncode == 0, imported.stderr + imported.stdout
        assert TOKEN not in imported.stdout + imported.stderr
        assert "connection not verified" in imported.stdout

        # source file is untouched, no provider env var was needed
        assert (home / ".claude" / "settings.json").read_bytes() == settings_bytes
        config = json.loads((state_dir / "config.json").read_text())
        provider = config["providers"]["127-0-0-1"]
        assert provider["auth"] == "bearer"
        assert provider["credential_id"].startswith("cred_")
        secret_file = state_dir / "credentials" / provider["credential_id"]
        assert secret_file.read_text() == TOKEN
        assert oct(secret_file.stat().st_mode & 0o777) == "0o600"

        # first request through qing run with a fake client
        proc = _start_serve(state_dir, env)
        try:
            discovery = _wait_discovery(state_dir)
            assert discovery is not None
            code = launcher.run_launch(
                state_dir=state_dir,
                label=None,
                model=None,
                aux_model=None,
                route_overrides={},
                native_args=[],
                claude_bin=_fake_claude_bin(tmp_path),
                machine_events=True,
            )
            assert code == 0
            events = capsys.readouterr().err
            assert "registered" in events
            assert TOKEN not in events

            assert len(upstream.requests) == 1
            request = upstream.requests[0]
            assert request["headers"].get("authorization") == f"Bearer {TOKEN}"
            assert json.loads(request["body"])["model"] == "claude-main-e2e"
        finally:
            _stop(proc)

        # restart: the private credential is still readable and used
        proc = _start_serve(state_dir, env)
        try:
            discovery = _wait_discovery(state_dir)
            base = f"http://127.0.0.1:{discovery['port']}"
            admin = {"authorization": f"Bearer {discovery['control_token']}"}
            with httpx.Client(base_url=base, timeout=15.0, trust_env=False) as client:
                created = client.post("/control/v1/instances", headers=admin, json={})
                assert created.status_code == 201, created.text
                token = created.json()["token"]
                sent = client.post(
                    "/v1/messages",
                    headers={"authorization": f"Bearer {token}"},
                    json={"model": "claude-main-e2e"},
                )
                assert sent.status_code == 200, sent.text
            assert len(upstream.requests) == 2
            assert upstream.requests[1]["headers"].get("authorization") == f"Bearer {TOKEN}"
        finally:
            _stop(proc)
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_import_to_first_request_x_api_key(e2e, tmp_path, capsys):
    home, state_dir, env = e2e
    upstream = _start_upstream()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        _write_settings(
            home,
            {
                "env": {
                    "ANTHROPIC_BASE_URL": upstream_url,
                    "ANTHROPIC_API_KEY": API_KEY,
                    "ANTHROPIC_MODEL": "claude-main-e2e",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux-e2e",
                }
            },
        )
        imported = _run_cli(
            ["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env
        )
        assert imported.returncode == 0, imported.stderr + imported.stdout
        assert API_KEY not in imported.stdout + imported.stderr

        proc = _start_serve(state_dir, env)
        try:
            _wait_discovery(state_dir)
            code = launcher.run_launch(
                state_dir=state_dir,
                label=None,
                model=None,
                aux_model=None,
                route_overrides={},
                native_args=[],
                claude_bin=_fake_claude_bin(tmp_path),
                machine_events=True,
            )
            assert code == 0
            capsys.readouterr()
            assert len(upstream.requests) == 1
            request = upstream.requests[0]
            assert request["headers"].get("x-api-key") == API_KEY
            assert "authorization" not in request["headers"]
            assert json.loads(request["body"])["model"] == "claude-main-e2e"
        finally:
            _stop(proc)
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_preview_writes_nothing_and_leaves_no_temp_files(e2e):
    home, state_dir, env = e2e
    upstream = _start_upstream()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        settings_bytes = _write_settings(
            home,
            {
                "env": {
                    "ANTHROPIC_BASE_URL": upstream_url,
                    "ANTHROPIC_AUTH_TOKEN": TOKEN,
                    "ANTHROPIC_MODEL": "claude-main-e2e",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux-e2e",
                }
            },
        )
        previewed = _run_cli(
            ["config", "import-claude", "--state-dir", str(state_dir)], env
        )
        assert previewed.returncode == 0, previewed.stderr
        assert "preview only" in previewed.stdout
        assert TOKEN not in previewed.stdout + previewed.stderr
        assert (home / ".claude" / "settings.json").read_bytes() == settings_bytes
        assert list(state_dir.iterdir()) == []
        assert not upstream.requests
    finally:
        upstream.shutdown()
        upstream.server_close()
