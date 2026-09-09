from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from tests.test_import_e2e import _minimal_env, _run_cli, _start_serve, _stop, _write_settings
from tests.test_tcp_smoke import _wait_discovery


def _settings_payload() -> dict:
    return {
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.test",
            "ANTHROPIC_AUTH_TOKEN": "sk-ant-preview-token",
            "ANTHROPIC_MODEL": "claude-main",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
        },
        "hooks": {"PreToolUse": [{"hooks": [{"command": "echo hook"}]}]},
        "permissions": {"allow": ["Bash"]},
    }


def test_run_preview_registers_and_writes_nothing(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    settings_bytes = _write_settings(home, _settings_payload())

    imported = _run_cli(["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env)
    assert imported.returncode == 0, imported.stdout + imported.stderr

    proc = _start_serve(state_dir, env)
    try:
        _wait_discovery(state_dir)
        preview = _run_cli(
            ["run", "--preview", "--state-dir", str(state_dir), "--route", "opus=claude-main"],
            env,
        )
        assert preview.returncode == 0, preview.stdout + preview.stderr
        out = preview.stdout
        assert "gateway address: http://127.0.0.1:" in out
        assert "main model: claude-main" in out
        assert "aux model: claude-aux" in out
        assert "route override: opus -> claude-main" in out
        assert "original Claude settings: untouched" in out
        assert "after exit" in out
        assert "no instance is registered" in out
        assert "qn_" not in out  # no tokens exist to leak

        # zero side effects
        assert (home / ".claude" / "settings.json").read_bytes() == settings_bytes
        import os

        leftovers = [
            p
            for p in os.listdir("/tmp")
            if p.startswith("qingniao-instance-")
        ]
        assert leftovers == []

        # no instance was created on the gateway
        discovery = json.loads((state_dir / "gateway.json").read_text())
        import httpx

        with httpx.Client(
            base_url=f"http://127.0.0.1:{discovery['port']}",
            headers={"authorization": f"Bearer {discovery['control_token']}"},
            trust_env=False,
        ) as client:
            listed = client.get("/control/v1/instances")
            assert listed.json()["total"] == 0
    finally:
        _stop(proc)


def test_run_preview_without_gateway_fails(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    result = _run_cli(["run", "--preview", "--state-dir", str(state_dir)], env)
    assert result.returncode != 0
