"""CLI interaction matrix: exit codes, JSON, width 40/80/120, NO_COLOR,
redirection, and zero writes when a required choice is missing."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

from tests.test_import_e2e import _minimal_env, _run_cli, _write_settings

SECRET = "sk-ant-api03-SYNTHETIC-MATRIX"
LONG_MODEL = "claude-main-with-a-very-long-upstream-name-0123456789"


def _settings(home: Path, *, dual_auth: bool = False) -> bytes:
    env_block = {
        "ANTHROPIC_BASE_URL": "https://api.anthropic.test",
        "ANTHROPIC_MODEL": LONG_MODEL,
        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
    }
    if dual_auth:
        env_block["ANTHROPIC_AUTH_TOKEN"] = SECRET
        env_block["ANTHROPIC_API_KEY"] = "sk-ant-api03-MATRIX-KEY"
    else:
        env_block["ANTHROPIC_AUTH_TOKEN"] = SECRET
    return _write_settings(home, {"env": env_block})


def _run_cli_env(args, env, *, columns=None, no_color=False, redirect=False):
    full_env = dict(env)
    if columns is not None:
        full_env["COLUMNS"] = str(columns)
    if no_color:
        full_env["NO_COLOR"] = "1"
    stdout = subprocess.PIPE if not redirect else open(os.devnull, "w")
    result = subprocess.run(
        [sys.executable, "-m", "qingniao", *args],
        stdout=stdout if redirect else subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=full_env,
        timeout=60,
    )
    if redirect:
        result.stdout = ""
    return result


def test_exit_codes_preview_apply_and_usage(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    _settings(home)

    preview = _run_cli(["config", "import-claude", "--state-dir", str(state_dir)], env)
    assert preview.returncode == 0

    applied = _run_cli(["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env)
    assert applied.returncode == 0

    missing_file = _run_cli(
        ["config", "import-claude", "--source", str(tmp_path / "nope.json"), "--state-dir", str(state_dir)],
        env,
    )
    assert missing_file.returncode == 1
    assert SECRET not in missing_file.stdout + missing_file.stderr

    bad_flag = _run_cli(["config", "import-claude", "--conflict", "nope", "--state-dir", str(state_dir)], env)
    assert bad_flag.returncode == 2  # usage error, click semantics preserved


def test_missing_choice_fails_with_zero_writes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    _settings(home, dual_auth=True)

    failed = _run_cli(
        ["config", "import-claude", "--apply", "--json", "--state-dir", str(state_dir)], env
    )
    assert failed.returncode == 1
    payload = json.loads(failed.stdout)
    assert payload["error"]["code"] == "auth_choice_required"
    assert SECRET not in failed.stdout + failed.stderr
    assert list(state_dir.iterdir()) == []

    # with the decision supplied the same invocation succeeds
    ok = _run_cli(
        [
            "config",
            "import-claude",
            "--apply",
            "--auth",
            "x-api-key",
            "--state-dir",
            str(state_dir),
        ],
        env,
    )
    assert ok.returncode == 0, ok.stdout + ok.stderr
    config = json.loads((state_dir / "config.json").read_text())
    assert config["providers"]["api-anthropic-test"]["auth"] == "x-api-key"


def test_preview_json_is_valid_and_sanitized(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    _settings(home)
    result = _run_cli(
        ["config", "import-claude", "--json", "--state-dir", str(state_dir)], env
    )
    assert result.returncode == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "apply"
    assert payload["written"] is False
    assert SECRET not in result.stdout


def test_widths_40_80_120_keep_values_intact(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    _settings(home)
    for width in (40, 80, 120):
        result = _run_cli_env(
            ["config", "import-claude", "--state-dir", str(state_dir)],
            env,
            columns=width,
        )
        assert result.returncode == 0, (width, result.stderr)
        # soft-wrapping is allowed; values must survive complete and
        # untruncated once the inserted newlines are removed
        flat = result.stdout.replace("\n", "")
        assert LONG_MODEL in flat, width
        assert "api.anthropic.test" in flat, width
    base = _run_cli(["config", "import-claude", "--state-dir", str(state_dir)], env)
    flat_base = base.stdout.replace("\n", "")
    assert "status: apply" in base.stdout
    assert f"{LONG_MODEL} -> upstream {LONG_MODEL}" in flat_base


def test_no_color_and_redirect_emit_no_escape_codes(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    env = _minimal_env(home)
    _settings(home)
    plain = _run_cli_env(
        ["config", "import-claude", "--apply", "--state-dir", str(state_dir)],
        env,
        no_color=True,
    )
    assert plain.returncode == 0
    assert "\x1b[" not in plain.stdout + plain.stderr

    redirected = _run_cli_env(
        ["config", "import-claude", "--state-dir", str(state_dir)],
        env,
        redirect=True,
    )
    assert redirected.returncode == 0
    assert redirected.stderr == ""
