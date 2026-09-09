"""Self-reference detection and secret-leak scanning across all outputs.

Synthetic secrets may exist only in the source settings file and the
private credential store; every other artifact — terminal, JSON, logs,
exceptions, the normal configuration, transaction records, argv — must
stay free of them. The self-reference check covers the gateway's own
loopback-equivalent endpoint and the qingniao token prefix; reverse
proxy aliases that loop back through another host name cannot be
detected offline and are documented as a limitation instead.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from qingniao import state as state_mod
from qingniao.claude_source import parse_claude_settings
from qingniao.config import ConfigStore
from qingniao.credentials import CredentialStore
from qingniao.gateway import Gateway
from qingniao.importing import ImportDecisions, PlanError, build_plan, source_digest
from qingniao.tokens import new_bearer_token, new_control_token

from tests.test_import_e2e import (
    _minimal_env,
    _run_cli,
    _start_serve,
    _start_upstream,
    _stop,
    _write_settings,
)
from tests.test_tcp_smoke import _wait_discovery

SECRET = "sk-ant-api03-SYNTHETIC-SCAN-SECRET"


def _parsed(url: str, token: str = SECRET):
    payload = {
        "env": {
            "ANTHROPIC_BASE_URL": url,
            "ANTHROPIC_AUTH_TOKEN": token,
            "ANTHROPIC_MODEL": "claude-main",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
        }
    }
    raw = json.dumps(payload).encode()
    return parse_claude_settings(raw, source=Path("/tmp/settings.json")), raw


def _plan_against(url, current, gateway_endpoint=None):
    parsed, raw = _parsed(url)
    return build_plan(
        parsed,
        source_digest=source_digest(raw),
        current=current,
        credential_store=CredentialStore(Path("/tmp/scan")),
        decisions=ImportDecisions(),
        operation_id="op_" + "2" * 32,
        gateway_endpoint=gateway_endpoint,
    )


@pytest.fixture
def empty_current(tmp_path):
    return ConfigStore(tmp_path / "config.json").load()


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9400",
        "http://127.0.0.1:9400/",
        "http://localhost:9400",
        "http://LOCALHOST:9400",
        "http://[::1]:9400",
        "http://127.8.8.8:9400",  # 127.0.0.0/8
        "http://0.0.0.0:9400",
    ],
)
def test_loopback_equivalent_endpoints_rejected(url, empty_current):
    with pytest.raises(PlanError) as excinfo:
        _plan_against(url, empty_current, gateway_endpoint="http://127.0.0.1:9400")
    assert excinfo.value.code == "self_reference"
    assert SECRET not in excinfo.value.message


@pytest.mark.parametrize(
    "url",
    [
        "http://127.0.0.1:9401",  # loopback host, different gateway port
        "https://api.anthropic.test",
        "http://localhost.example:9400",  # not localhost
    ],
)
def test_other_endpoints_allowed(url, empty_current):
    plan = _plan_against(url, empty_current, gateway_endpoint="http://127.0.0.1:9400")
    assert plan.status in ("apply", "provider_only")


@pytest.mark.parametrize("token_factory", [new_bearer_token, new_control_token])
def test_gateway_tokens_rejected_as_upstream_credentials(token_factory, empty_current):
    token = token_factory()
    parsed, raw = _parsed("https://api.anthropic.test", token=token)
    with pytest.raises(PlanError) as excinfo:
        build_plan(
            parsed,
            source_digest=source_digest(raw),
            current=empty_current,
            credential_store=CredentialStore(Path("/tmp/scan")),
            decisions=ImportDecisions(),
            operation_id="op_" + "2" * 32,
        )
    assert excinfo.value.code == "self_reference"
    assert token not in excinfo.value.message


@pytest.fixture
def scan_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return home, state_dir, _minimal_env(home)


def _scan_state_artifacts(state_dir: Path) -> dict[str, object]:
    artifacts: dict[str, object] = {}
    config_path = state_dir / "config.json"
    if config_path.exists():
        artifacts["config.json"] = config_path.read_bytes()
    tx_dir = state_dir / "transactions"
    if tx_dir.is_dir():
        for record in tx_dir.iterdir():
            artifacts[f"transactions/{record.name}"] = record.read_bytes()
    creds_dir = state_dir / "credentials"
    if creds_dir.is_dir():
        for cred in creds_dir.iterdir():
            artifacts[f"credentials-name/{cred.name}"] = cred.name
            if cred.is_file():
                artifacts[f"credentials-content/{cred.name}"] = cred.read_bytes()
    discovery = state_dir / "gateway.json"
    if discovery.exists():
        artifacts["gateway.json"] = discovery.read_bytes()
    log = state_dir / "serve.log"
    if log.exists():
        artifacts["serve.log"] = log.read_bytes()
    return artifacts


def test_secret_lives_only_in_declared_locations(scan_env, tmp_path):
    home, state_dir, env = scan_env
    upstream = _start_upstream()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        _write_settings(
            home,
            {
                "env": {
                    "ANTHROPIC_BASE_URL": upstream_url,
                    "ANTHROPIC_AUTH_TOKEN": SECRET,
                    "ANTHROPIC_MODEL": "claude-main",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
                }
            },
        )
        imported = _run_cli(
            ["config", "import-claude", "--apply", "--json", "--state-dir", str(state_dir)], env
        )
        assert imported.returncode == 0, imported.stdout + imported.stderr
        combined_output = imported.stdout + imported.stderr
        assert SECRET not in combined_output

        proc = _start_serve(state_dir, env)
        try:
            _wait_discovery(state_dir)
            # the running gateway serves status and config without secrets
            import httpx

            discovery = json.loads((state_dir / "gateway.json").read_text())
            with httpx.Client(
                base_url=f"http://127.0.0.1:{discovery['port']}",
                headers={"authorization": f"Bearer {discovery['control_token']}"},
                trust_env=False,
            ) as client:
                config_response = client.get("/control/v1/config")
                assert SECRET not in config_response.text
        finally:
            _stop(proc)

        artifacts = _scan_state_artifacts(state_dir)
        assert artifacts, "expected committed artifacts"
        for name, content in artifacts.items():
            if name.startswith("credentials-content/"):
                continue  # the one declared location for the secret
            text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
            assert SECRET not in text, name
    finally:
        upstream.shutdown()
        upstream.server_close()


def test_failure_paths_stay_sanitized(scan_env):
    home, state_dir, env = scan_env
    _write_settings(
        home,
        {
            "env": {
                "ANTHROPIC_BASE_URL": "https://api.anthropic.test",
                "ANTHROPIC_AUTH_TOKEN": SECRET,
                "ANTHROPIC_MODEL": "claude-main",
                "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
            }
        },
    )
    # break the private store: the apply must fail without echoing the secret
    (state_dir / "credentials").mkdir(mode=0o755)
    failed = _run_cli(
        ["config", "import-claude", "--apply", "--json", "--state-dir", str(state_dir)], env
    )
    assert failed.returncode != 0
    assert SECRET not in failed.stdout + failed.stderr
    assert not (state_dir / "config.json").exists()


@pytest.mark.anyio
async def test_malformed_wire_plan_secret_not_echoed(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        payload = {
            "operation_id": "op_" + "3" * 32,
            "plan": {"operation_id": "op_" + "3" * 32, "secret": SECRET, "models": "garbage"},
        }
        r = await ctx.client.post("/control/v1/imports", headers=ctx.admin_headers, json=payload)
        assert r.status_code == 400
        assert SECRET not in r.text


def test_child_argv_carries_no_tokens_or_secrets(scan_env, tmp_path):
    """qing run passes only the settings path to the client; tokens travel
    inside the 0600 file, never on the command line."""
    home, state_dir, env = scan_env
    upstream = _start_upstream()
    try:
        upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"
        _write_settings(
            home,
            {
                "env": {
                    "ANTHROPIC_BASE_URL": upstream_url,
                    "ANTHROPIC_AUTH_TOKEN": SECRET,
                    "ANTHROPIC_MODEL": "claude-main",
                    "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
                }
            },
        )
        imported = _run_cli(["config", "import-claude", "--apply", "--state-dir", str(state_dir)], env)
        assert imported.returncode == 0, imported.stdout + imported.stderr

        marker = tmp_path / "argv.txt"
        fake = tmp_path / "fake-claude"
        fake.write_text(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > " + str(marker) + "\nexit 0\n"
        )
        fake.chmod(0o755)
        proc = _start_serve(state_dir, env)
        try:
            _wait_discovery(state_dir)
            from qingniao import launcher

            code = launcher.run_launch(
                state_dir=state_dir,
                label=None,
                model=None,
                aux_model=None,
                route_overrides={},
                native_args=[],
                claude_bin=str(fake),
                machine_events=False,
            )
            assert code == 0
            argv_text = marker.read_text()
            assert SECRET not in argv_text
            assert "qn_" not in argv_text
            assert "--settings" in argv_text
        finally:
            _stop(proc)
    finally:
        upstream.shutdown()
        upstream.server_close()
