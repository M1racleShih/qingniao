from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from types import SimpleNamespace

from qingniao import state as state_mod
from qingniao.cli import CliError, _submit_import_online
from tests.conftest import _UpstreamHandler
from tests.test_import_e2e import (
    _minimal_env,
    _run_cli,
    _start_serve,
    _start_upstream,
    _stop,
    _write_settings,
)
from tests.test_tcp_smoke import _wait_discovery

TOKEN_1 = "sk-ant-api03-SYNTHETIC-FIRST"
TOKEN_2 = "sk-ant-api03-SYNTHETIC-SECOND"
OP_ID = "op_" + "9" * 32


def test_online_import_applies_in_running_gateway(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    upstream = _start_upstream()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        _write_settings(
            home,
            {
                "env": {
                    "ANTHROPIC_BASE_URL": upstream_url,
                    "ANTHROPIC_AUTH_TOKEN": TOKEN_1,
                    "ANTHROPIC_MODEL": "claude-main",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
                }
            },
        )
        first = _run_cli(["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env)
        assert first.returncode == 0, first.stdout + first.stderr
        assert "configuration saved" in first.stdout

        second_source = tmp_path / "second-settings.json"
        second_source.write_text(
            json.dumps(
                {
                    "env": {
                        "ANTHROPIC_BASE_URL": "https://other.example",
                        "ANTHROPIC_AUTH_TOKEN": TOKEN_2,
                        "ANTHROPIC_MODEL": "claude-main-2",
                        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux-2",
                    }
                }
            )
        )
        before = (state_dir / "config.json").read_bytes()

        proc = _start_serve(state_dir, env)
        try:
            _wait_discovery(state_dir)
            online = _run_cli(
                [
                    "config",
                    "import-claude",
                    "--apply",
                    "--source",
                    str(second_source),
                    "--state-dir",
                    str(state_dir),
                ],
                env,
            )
            assert online.returncode == 0, online.stdout + online.stderr
            assert "applied by the running gateway" in online.stdout
            assert "connection not verified" in online.stdout
            assert TOKEN_2 not in online.stdout + online.stderr
            assert (state_dir / "config.json").read_bytes() != before

            config = json.loads((state_dir / "config.json").read_text())
            assert "other-example" in config["providers"]
            assert config["defaults"]["model"] == "claude-main-2"

            # retrying the same source is a content no-op under a fresh operation id
            retry = _run_cli(
                [
                    "config",
                    "import-claude",
                    "--apply",
                    "--source",
                    str(second_source),
                    "--state-dir",
                    str(state_dir),
                ],
                env,
            )
            assert retry.returncode == 0, retry.stdout + retry.stderr
            assert "no changes" in retry.stdout
            creds = list((state_dir / "credentials").iterdir())
            assert len(creds) == 2  # one per distinct secret, no duplicates
        finally:
            _stop(proc)
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_locked_directory_without_reachable_gateway_never_writes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    _write_settings(
        home,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.test",
                "ANTHROPIC_AUTH_TOKEN": TOKEN_1,
                "ANTHROPIC_MODEL": "claude-main",
                "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
            }
        },
    )
    # first import succeeds offline so there is real state to protect
    first = _run_cli(["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env)
    assert first.returncode == 0, first.stdout + first.stderr
    before = (state_dir / "config.json").read_bytes()

    # a lock holder with an unreachable control endpoint: no offline fallback
    lock = state_mod.GatewayLock(state_dir)
    lock.acquire()
    state_mod.write_discovery(state_dir, port=9, pid=1, control_token="qn_deadbeef")
    try:
        blocked = _run_cli(
            ["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env
        )
        assert blocked.returncode != 0
        assert (state_dir / "config.json").read_bytes() == before
    finally:
        lock.release()
        state_mod.remove_discovery(state_dir, 1)


def _fake_plan():
    from qingniao.importing import ImportPlan, PlannedModel
    from qingniao.config import Defaults
    from pathlib import Path as _P

    return ImportPlan(
        operation_id=OP_ID,
        source=_P("/tmp/settings.json"),
        source_digest="0" * 64,
        expected_generation=3,
        status="apply",
        provider_id="api-anthropic-test",
        base_url="https://api.anthropic.test",
        auth="bearer",
        secret="sk-secret",
        models=(PlannedModel(model_id="m", upstream_model="m"),),
        defaults=Defaults(model="m", aux_model="m", routes={"m": "m"}),
    )


def test_unconfirmed_timeout_recovers_via_operation_query(monkeypatch, tmp_path):
    from qingniao import cli

    calls = []

    def fake_call(state_dir, method, path, **kwargs):
        calls.append((method, path))
        if method == "POST":
            raise CliError("unconfirmed", "gateway did not confirm the import submit in time")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"operation_id": OP_ID, "status": "committed", "generation": 4},
        )

    monkeypatch.setattr(cli, "_call", fake_call)
    result = _submit_import_online(tmp_path, _fake_plan())
    assert result["status"] == "committed"
    assert result["applied"] is True
    assert result["recovered"] is True
    assert calls[1] == ("GET", f"/control/v1/operations/{OP_ID}")


def test_unconfirmed_timeout_stays_unconfirmed_when_query_is_pending(monkeypatch, tmp_path):
    from qingniao import cli

    def fake_call(state_dir, method, path, **kwargs):
        if method == "POST":
            raise CliError("unconfirmed", "gateway did not confirm the import submit in time")
        return SimpleNamespace(
            status_code=200,
            json=lambda: {"operation_id": OP_ID, "status": "in_progress", "generation": None},
        )

    monkeypatch.setattr(cli, "_call", fake_call)
    result = _submit_import_online(tmp_path, _fake_plan())
    assert result["status"] == "unconfirmed"
    assert OP_ID in result["message"]
