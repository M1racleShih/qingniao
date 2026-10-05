"""End-to-end catalog acceptance: entries created through the CLI against a
real gateway process, routes switched live, synthetic upstreams receiving
the correct model strings; plus synthetic-secret scans across every output
and state artifact. Local fixtures only — no real provider is contacted."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from qingniao import state as state_mod
from tests.test_import_e2e import _minimal_env, _run_cli, _start_serve, _start_upstream, _stop
from tests.test_tcp_smoke import _wait_discovery

E2E_P1 = "sk-ant-api03-E2E-P1-BEARER"
E2E_P2 = "sk-ant-api03-E2E-P2-XKEY"
SECRET = "sk-ant-api03-SYNTHETIC-CATALOG-SCAN"


@pytest.fixture
def e2e(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return home, state_dir, _minimal_env(home)


def _gateway_env(env: dict, **extra: str) -> dict:
    out = dict(env)
    out["QING_E2E_P1"] = E2E_P1
    out["QING_E2E_P2"] = E2E_P2
    out.update(extra)
    return out


def _scan_state_artifacts(state_dir: Path) -> dict[str, bytes]:
    artifacts: dict[str, bytes] = {}
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
            artifacts[f"credentials-name/{cred.name}"] = cred.name.encode()
            if cred.is_file():
                artifacts[f"credentials-content/{cred.name}"] = cred.read_bytes()
    discovery = state_dir / "gateway.json"
    if discovery.exists():
        artifacts["gateway.json"] = discovery.read_bytes()
    log = state_dir / "serve.log"
    if log.exists():
        artifacts["serve.log"] = log.read_bytes()
    return artifacts


def _send(discovery: dict, instance_token: str, request_model: str) -> httpx.Response:
    with httpx.Client(
        base_url=f"http://127.0.0.1:{discovery['port']}", timeout=15.0, trust_env=False
    ) as client:
        return client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {instance_token}"},
            json={"model": request_model, "stream": False},
        )


def test_two_providers_two_models_route_switch_reaches_expected_upstream(e2e, tmp_path):
    _, state_dir, env = e2e
    upstream1 = _start_upstream()
    upstream2 = _start_upstream()
    try:
        url1 = f"http://127.0.0.1:{upstream1.server_address[1]}"
        url2 = f"http://127.0.0.1:{upstream2.server_address[1]}"
        proc = _start_serve(state_dir, _gateway_env(env))
        try:
            discovery = _wait_discovery(state_dir)
            st = str(state_dir)

            # one env-type catalog credential, referenced by id by p2
            added = _run_cli(
                ["credential", "add", "p2-key", "--env", "QING_E2E_P2", "--state-dir", st, "--json"], _gateway_env(env)
            )
            assert added.returncode == 0, added.stdout + added.stderr

            def add_provider(pid, url, auth, credential) -> None:
                args = ["provider", "add", pid, "--base-url", url, "--auth", auth]
                args.extend(credential)
                result = _run_cli(args + ["--state-dir", st, "--json"], _gateway_env(env))
                assert result.returncode == 0, result.stdout + result.stderr

            add_provider("prov-one", url1, "bearer", ["--credential-env", "QING_E2E_P1"])
            add_provider("prov-two", url2, "x-api-key", ["--credential-id", "p2-key"])

            def add_model(mid, pid, upstream) -> None:
                result = _run_cli(
                    ["model", "add", mid, "--provider", pid, "--upstream-model", upstream, "--state-dir", st, "--json"],
                    _gateway_env(env),
                )
                assert result.returncode == 0, result.stdout + result.stderr

            add_model("model-one", "prov-one", "vendor/one")
            add_model("model-two", "prov-two", "vendor/two")

            # register an instance whose request model routes to model-one
            admin = {"authorization": f"Bearer {discovery['control_token']}"}
            with httpx.Client(
                base_url=f"http://127.0.0.1:{discovery['port']}", timeout=15.0, trust_env=False
            ) as client:
                created = client.post(
                    "/control/v1/instances",
                    headers=admin,
                    json={
                        "model": "req-main",
                        "aux_model": "req-aux",
                        "routes": {"req-main": "model-one", "req-aux": "model-one"},
                    },
                )
                assert created.status_code == 201, created.text
                instance_token = created.json()["token"]
                instance_id = created.json()["instance"]["id"]

                # request stays on model-one / prov-one
                first = _send(discovery, instance_token, "req-main")
                assert first.status_code == 200, first.text

            assert len(upstream1.requests) == 1
            first_request = upstream1.requests[0]
            assert json.loads(first_request["body"])["model"] == "vendor/one"
            assert first_request["headers"].get("authorization") == f"Bearer {E2E_P1}"
            assert not upstream2.requests

            # switch the live route to model-two / prov-two
            switched = _run_cli(
                ["route", "set", "req-main", "model-two", "--instance", instance_id, "--state-dir", st],
                _gateway_env(env),
            )
            assert switched.returncode == 0, switched.stdout + switched.stderr
            assert "switched" in switched.stdout

            second = _send(discovery, instance_token, "req-main")
            assert second.status_code == 200, second.text
            assert len(upstream2.requests) == 1
            second_request = upstream2.requests[0]
            assert json.loads(second_request["body"])["model"] == "vendor/two"
            assert second_request["headers"].get("x-api-key") == E2E_P2
            # catalog env credential resolves to x-api-key, not a bearer
            assert "authorization" not in second_request["headers"]
        finally:
            _stop(proc)
    finally:
        upstream1.shutdown()
        upstream1.server_close()
        upstream2.shutdown()
        upstream2.server_close()


def test_catalog_secret_only_in_declared_locations(e2e, tmp_path):
    _, state_dir, env = e2e
    secret_file = tmp_path / "secret.txt"
    secret_file.write_text(SECRET + "\n")
    proc = _start_serve(state_dir, _gateway_env(env))
    try:
        _wait_discovery(state_dir)
        st = str(state_dir)

        stored = _run_cli(
            ["credential", "add", "--from-file", str(secret_file), "--state-dir", st, "--json"],
            _gateway_env(env),
        )
        assert stored.returncode == 0, stored.stdout + stored.stderr
        combined = stored.stdout + stored.stderr
        assert SECRET not in combined
        payload = json.loads(stored.stdout)
        cred_id = payload["credential"]["id"]
        assert (state_dir / "credentials" / cred_id).read_text() == SECRET
    finally:
        _stop(proc)

    artifacts = _scan_state_artifacts(state_dir)
    assert artifacts, "expected committed artifacts"
    for name, content in artifacts.items():
        if name.startswith("credentials-content/"):
            continue  # the one declared secret location
        text = content.decode("utf-8", "replace") if isinstance(content, bytes) else str(content)
        assert SECRET not in text, name


def test_catalog_secret_absent_from_failure_and_argv_paths(e2e, tmp_path):
    _, state_dir, env = e2e
    st = str(state_dir)

    # argv: passing a value as an option is rejected as a usage error
    bad = subprocess.run(
        [sys.executable, "-m", "qingniao", "credential", "add", "k", "--value", SECRET, "--state-dir", st, "--json"],
        capture_output=True,
        text=True,
        env=_gateway_env(env),
        timeout=60,
    )
    assert bad.returncode == 2
    assert SECRET not in bad.stdout + bad.stderr

    # unreachable gateway: the stdin secret is read but never echoed
    dead_state = tmp_path / "dead"
    dead_state.mkdir()
    # write a discovery pointing at an unused port so the CLI tries to reach it
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()
    state_mod.prepare_state_dir(dead_state)
    state_mod.write_discovery(dead_state, port=dead_port, pid=1, control_token="qn_stale")
    failed = subprocess.run(
        [sys.executable, "-m", "qingniao", "credential", "add", "--from-stdin", "--state-dir", str(dead_state), "--json"],
        capture_output=True,
        text=True,
        env=_gateway_env(env),
        input=SECRET + "\n",
        timeout=60,
    )
    assert failed.returncode == 1
    assert SECRET not in failed.stdout + failed.stderr
    assert "unreachable" in failed.stdout or "unreachable" in failed.stderr


def test_private_credential_dry_run_reads_nothing_and_writes_nothing(e2e):
    _, state_dir, env = e2e
    proc = _start_serve(state_dir, _gateway_env(env))
    try:
        _wait_discovery(state_dir)
        # stdin is left empty; a dry run must not consume it or fail on it
        result = subprocess.run(
            [sys.executable, "-m", "qingniao", "credential", "add", "--from-stdin", "--dry-run", "--state-dir", str(state_dir), "--json"],
            capture_output=True,
            text=True,
            env=_gateway_env(env),
            input="",
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["dry_run"] is True
        assert not (state_dir / "credentials").exists() or list((state_dir / "credentials").iterdir()) == []
    finally:
        _stop(proc)


def test_credential_through_catalog_survives_gateway_restart(e2e):
    """A provider referencing an env-type catalog credential resolves after
    a gateway restart (the mapping lives in the configuration)."""
    _, state_dir, env = e2e
    st = str(state_dir)

    def ensure(first: bool = False):
        r = _run_cli(
            ["provider", "add", "prov-a", "--base-url", "https://restart.example", "--auth", "bearer", "--credential-id", "restart-key", "--state-dir", st, "--json"],
            _gateway_env(env),
        )
        return r

    proc = _start_serve(state_dir, _gateway_env(env))
    try:
        _wait_discovery(state_dir)
        cred = _run_cli(["credential", "add", "restart-key", "--env", "QING_E2E_P1", "--state-dir", st, "--json"], _gateway_env(env))
        assert cred.returncode == 0, cred.stdout + cred.stderr
        result = ensure()
        assert result.returncode == 0, result.stdout + result.stderr
    finally:
        _stop(proc)

    # restart: the configuration retains the credential catalog mapping
    proc = _start_serve(state_dir, _gateway_env(env))
    try:
        discovery = _wait_discovery(state_dir)
        show = _run_cli(["provider", "show", "prov-a", "--state-dir", st, "--json"], _gateway_env(env))
        assert show.returncode == 0
        assert json.loads(show.stdout)["provider"]["credential_id"] == "restart-key"
        # the resolved env label is visible in the human view
        human = _run_cli(["provider", "show", "prov-a", "--state-dir", st], _gateway_env(env))
        assert "env QING_E2E_P1" in human.stdout
        assert discovery is not None
    finally:
        _stop(proc)
