"""CLI matrix for entry-level catalog commands against a real gateway
process: exit codes 0/1/2, stable JSON structures, dry-run zero writes,
stable error codes, width/NO_COLOR behaviour, help snapshots, concurrency,
and the human/agent contract. Everything runs against local fixtures only."""

from __future__ import annotations

import json
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from tests.test_import_e2e import _minimal_env, _run_cli, _start_serve, _stop
from tests.test_tcp_smoke import _wait_discovery

P_TOKEN = "sk-ant-api03-CATALOG-P-ENV"
Q_TOKEN = "sk-ant-api03-CATALOG-Q-XKEY"


@pytest.fixture
def catalog_env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    return home, state_dir, _minimal_env(home)


@pytest.fixture
def live_gateway(catalog_env):
    _, state_dir, env = catalog_env
    gw_env = dict(env)
    gw_env["QING_CAT_P"] = P_TOKEN
    gw_env["QING_CAT_Q"] = Q_TOKEN
    proc = _start_serve(state_dir, gw_env)
    try:
        _wait_discovery(state_dir)
        yield state_dir, env
    finally:
        _stop(proc)


def _json_cli(args: list[str], env: dict) -> dict:
    result = _run_cli(args, env)
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload.get("ok") is True
    return payload


def _base(state_dir: Path) -> list[str]:
    return ["--state-dir", str(state_dir), "--json"]


def _run_cli_input(args: list[str], env: dict, text: str):
    return subprocess.run(
        [sys.executable, "-m", "qingniao", *args],
        capture_output=True,
        text=True,
        env=env,
        input=text,
        timeout=60,
    )


def test_provider_crud_json_contract(live_gateway):
    state_dir, env = live_gateway

    added = _json_cli(
        [
            "provider",
            "add",
            "prov-a",
            "--base-url",
            "http://127.0.0.1:9101",
            "--auth",
            "bearer",
            "--credential-env",
            "QING_CAT_P",
            *_base(state_dir),
        ],
        env,
    )
    assert added["applied"] is True
    assert isinstance(added["revision"], int)
    assert added["provider"] == {
        "id": "prov-a",
        "base_url": "http://127.0.0.1:9101",
        "auth": "bearer",
        "credential_env": "QING_CAT_P",
    }

    listed = _json_cli(["provider", "list", *_base(state_dir)], env)
    assert listed["providers"] == [added["provider"]]

    shown = _json_cli(["provider", "show", "prov-a", *_base(state_dir)], env)
    assert shown["provider"] == added["provider"]

    updated = _json_cli(
        ["provider", "set", "prov-a", "--base-url", "http://127.0.0.1:9103", *_base(state_dir)],
        env,
    )
    assert updated["provider"]["base_url"] == "http://127.0.0.1:9103"
    assert updated["provider"]["credential_env"] == "QING_CAT_P"

    removed = _json_cli(["provider", "rm", "prov-a", *_base(state_dir)], env)
    assert removed["removed"] == "prov-a"
    assert _json_cli(["provider", "list", *_base(state_dir)], env)["providers"] == []


def test_model_crud_json_contract(live_gateway):
    state_dir, env = live_gateway
    _json_cli(
        ["provider", "add", "prov-a", "--base-url", "https://a.example", "--auth", "bearer", "--credential-env", "QING_CAT_P", *_base(state_dir)], env
    )
    added = _json_cli(
        ["model", "add", "model-a", "--provider", "prov-a", "--upstream-model", "vendor/a", *_base(state_dir)],
        env,
    )
    assert added["model"] == {"id": "model-a", "provider": "prov-a", "upstream_model": "vendor/a"}
    assert _json_cli(["model", "list", *_base(state_dir)], env)["models"] == [added["model"]]
    assert _json_cli(["model", "show", "model-a", *_base(state_dir)], env)["model"] == added["model"]
    updated = _json_cli(
        ["model", "set", "model-a", "--upstream-model", "vendor/new", *_base(state_dir)], env
    )
    assert updated["model"]["upstream_model"] == "vendor/new"
    assert _json_cli(["model", "rm", "model-a", *_base(state_dir)], env)["removed"] == "model-a"


def test_credential_catalog_env_and_private(live_gateway):
    state_dir, env = live_gateway
    added = _json_cli(
        ["credential", "add", "shared-key", "--env", "QING_CAT_Q", *_base(state_dir)], env
    )
    assert added["credential"] == {
        "id": "shared-key",
        "source": "env",
        "env": "QING_CAT_Q",
        "referenced_by": [],
    }
    listed = _json_cli(["credential", "list", *_base(state_dir)], env)
    assert listed["credentials"] == [added["credential"]]
    shown = _json_cli(["credential", "show", "shared-key", *_base(state_dir)], env)
    assert shown["credential"] == added["credential"]

    # provider referencing the catalog id stores the id (gateway resolves env)
    _json_cli(
        ["provider", "add", "prov-q", "--base-url", "https://q.example", "--auth", "x-api-key", "--credential-id", "shared-key", *_base(state_dir)],
        env,
    )
    shown = _json_cli(["credential", "show", "shared-key", *_base(state_dir)], env)
    assert shown["credential"]["referenced_by"] == ["prov-q"]

    # private credential via stdin; value never echoed
    result = _run_cli_input(
        ["credential", "add", "--from-stdin", *_base(state_dir)],
        env,
        "sk-ant-api03-STDIN-SECRET\n",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    payload = json.loads(result.stdout)
    assert payload["credential"]["source"] == "private"
    priv_id = payload["credential"]["id"]
    assert priv_id.startswith("cred_")
    assert "sk-ant-api03-STDIN-SECRET" not in result.stdout + result.stderr
    assert (state_dir / "credentials" / priv_id).read_text() == "sk-ant-api03-STDIN-SECRET"

    # provider referencing the private id
    _json_cli(
        ["provider", "add", "prov-priv", "--base-url", "https://priv.example", "--auth", "bearer", "--credential-id", priv_id, *_base(state_dir)],
        env,
    )
    # credential rm fails while referenced, listing providers
    failed = _run_cli(["credential", "rm", priv_id, *_base(state_dir)], env)
    assert failed.returncode == 1
    err = json.loads(failed.stdout)["error"]
    assert err["code"] == "entry_in_use"
    assert err["references"] == ["prov-priv"]

    # after removing the reference, rm succeeds (private store version stays)
    _json_cli(["provider", "rm", "prov-priv", *_base(state_dir)], env)
    store_file = state_dir / "credentials" / priv_id
    assert store_file.exists()  # immutable versions are never deleted by rm
    assert _json_cli(["credential", "rm", priv_id, *_base(state_dir)], env)["removed"] == priv_id
    # the catalog entry is gone but the immutable version remains
    assert store_file.exists()


def test_error_codes_machine_distinguishable(live_gateway):
    state_dir, env = live_gateway
    _json_cli(
        ["provider", "add", "prov-a", "--base-url", "https://a.example", "--auth", "bearer", "--credential-env", "QING_CAT_P", *_base(state_dir)], env
    )
    _json_cli(
        ["model", "add", "model-a", "--provider", "prov-a", "--upstream-model", "u", *_base(state_dir)], env
    )

    def _code(args):
        result = _run_cli(args, env)
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]["code"] and payload["error"]["message"]
        return payload["error"]["code"]

    assert _code(["provider", "add", "prov-a", "--base-url", "https://x", "--auth", "bearer", "--credential-env", "E", *_base(state_dir)]) == "entry_exists"
    assert _code(["provider", "show", "ghost", *_base(state_dir)]) == "entry_not_found"
    assert _code(["model", "show", "ghost", *_base(state_dir)]) == "entry_not_found"
    assert _code(["model", "add", "m", "--provider", "ghost", "--upstream-model", "u", *_base(state_dir)]) == "entry_not_found"
    assert _code(["provider", "rm", "prov-a", *_base(state_dir)]) == "entry_in_use"
    # duplicate env credential add
    first = _run_cli(["credential", "add", "dup-key", "--env", "QING_CAT_P", *_base(state_dir)], env)
    assert first.returncode == 0, first.stdout
    assert _code(["credential", "add", "dup-key", "--env", "QING_CAT_P", *_base(state_dir)]) == "entry_exists"
    # unknown card catalog id on provider add
    assert _code(["provider", "add", "prov-x", "--base-url", "https://x", "--auth", "bearer", "--credential-id", "ghost", *_base(state_dir)]) == "entry_not_found"
    # invalid argument: unknown auth mode and malformed URL are usage errors
    # (exit 2) with the invalid_argument code in both modes
    def _usage_code(args):
        result = _run_cli(args, env)
        assert result.returncode == 2
        payload = json.loads(result.stdout)
        assert payload["ok"] is False
        assert payload["error"]["code"] == "invalid_argument"
        assert payload["error"]["message"]
        return payload["error"]

    err = _usage_code(["provider", "add", "prov-x", "--base-url", "https://x", "--auth", "nope", "--credential-env", "E", *_base(state_dir)])
    assert any(e["path"] == "--auth" for e in err.get("errors", []))
    # invalid argument: bad base url is caught by the gateway schema rules locally
    err = _usage_code(["provider", "set", "prov-a", "--base-url", "ftp://nope", *_base(state_dir)])
    assert err.get("errors")
    # mutually exclusive credential sources stay usage errors in json mode
    err = _usage_code(["provider", "set", "prov-a", "--base-url", "https://x", "--credential-env", "E", "--credential-id", "c", *_base(state_dir)])
    assert any("--credential-env" in e["path"] for e in err.get("errors", []))


def test_dry_run_writes_nothing(live_gateway):
    state_dir, env = live_gateway
    # establish a baseline persisted configuration first
    _json_cli(
        ["provider", "add", "base", "--base-url", "https://base.example", "--auth", "bearer", "--credential-env", "QING_CAT_P", *_base(state_dir)], env
    )
    before = (state_dir / "config.json").read_bytes()

    def dry_cmd(args):
        result = _run_cli(args, env)
        assert result.returncode == 0, result.stdout + result.stderr
        payload = json.loads(result.stdout)
        assert payload["ok"] is True
        assert payload["dry_run"] is True
        return payload

    dry_cmd(["provider", "add", "prov-a", "--base-url", "https://a.example", "--auth", "bearer", "--credential-env", "QING_CAT_P", "--dry-run", *_base(state_dir)])
    # commit the provider so the later model dry run has a real target
    _json_cli(
        ["provider", "add", "prov-a", "--base-url", "https://a.example", "--auth", "bearer", "--credential-env", "QING_CAT_P", *_base(state_dir)], env
    )
    after_commit = (state_dir / "config.json").read_bytes()
    assert after_commit != before
    dry_cmd(["model", "add", "m", "--provider", "prov-a", "--upstream-model", "u", "--dry-run", *_base(state_dir)])
    dry_cmd(["credential", "add", "k", "--env", "QING_CAT_Q", "--dry-run", *_base(state_dir)])
    # private credential dry run must not read stdin and must not store
    dry_cmd(["credential", "add", "--from-stdin", "--dry-run", *_base(state_dir)])

    # the dry runs above wrote nothing once the baseline is fixed
    assert (state_dir / "config.json").read_bytes() == after_commit


def test_usage_and_exit_codes_for_missing_and_conflicting(live_gateway):
    state_dir, env = live_gateway
    st = str(state_dir)

    missing = _run_cli(["provider", "add", "p", "--auth", "bearer", "--credential-env", "E", "--state-dir", st], env)
    assert missing.returncode == 2
    assert "--base-url" in missing.stderr
    # parser-level missing option stays a click usage error (exit 2, cli_error)
    missing_json = _run_cli(["provider", "add", "p", "--auth", "bearer", "--credential-env", "E", "--json", "--state-dir", st], env)
    assert missing_json.returncode == 2
    assert json.loads(missing_json.stdout)["error"]["code"] == "cli_error"

    # function-level usage errors exit 2 in json mode with invalid_argument
    both = _run_cli(
        ["provider", "add", "p", "--base-url", "https://x", "--auth", "bearer", "--credential-env", "E", "--credential-id", "c", "--state-dir", st],
        env,
    )
    assert both.returncode == 2
    both_json = _run_cli(
        ["provider", "add", "p", "--base-url", "https://x", "--auth", "bearer", "--credential-env", "E", "--credential-id", "c", "--json", "--state-dir", st],
        env,
    )
    assert both_json.returncode == 2
    both_error = json.loads(both_json.stdout)["error"]
    assert both_error["code"] == "invalid_argument"
    assert both_error["errors"]

    nosource = _run_cli(["credential", "add", "k", "--state-dir", st], env)
    assert nosource.returncode == 2
    assert "source" in nosource.stderr
    nosource_json = _run_cli(["credential", "add", "k", "--json", "--state-dir", st], env)
    assert nosource_json.returncode == 2
    nosource_error = json.loads(nosource_json.stdout)["error"]
    assert nosource_error["code"] == "invalid_argument"
    assert any("source" in e["path"] for e in nosource_error.get("errors", []))


def test_set_noop_is_reported_without_writing(live_gateway):
    state_dir, env = live_gateway
    _json_cli(
        ["provider", "add", "prov-a", "--base-url", "https://a.example", "--auth", "bearer", "--credential-env", "QING_CAT_P", *_base(state_dir)], env
    )
    gen_before = _json_cli(["provider", "show", "prov-a", *_base(state_dir)], env)
    # show has no generation; read it from a full list? use provider set noop
    r = _run_cli(["provider", "set", "prov-a", "--base-url", "https://a.example", "--json", "--state-dir", str(state_dir)], env)
    assert r.returncode == 0
    payload = json.loads(r.stdout)
    assert payload["ok"] is True and payload["changed"] is False and payload["applied"] is False


def test_help_lists_commands_with_examples():
    for group in ("provider", "credential", "model"):
        result = subprocess.run(
            [sys.executable, "-m", "qingniao", group, "--help"],
            capture_output=True,
            text=True,
            env={"HOME": str(Path.home())},
            timeout=60,
        )
        assert result.returncode == 0
        assert "Commands" in result.stdout
        assert "add" in result.stdout and "list" in result.stdout and "rm" in result.stdout
        assert "qing %s add" % group in result.stdout or "Examples" in result.stdout


def test_widths_and_no_color(live_gateway, catalog_env):
    state_dir, env = live_gateway
    _json_cli(
        ["provider", "add", "long-provider-name-0123456789", "--base-url", "https://api.example-with-a-long-host.test", "--auth", "bearer", "--credential-env", "QING_CAT_P", *_base(state_dir)], env
    )
    for width in (40, 80, 120):
        full = dict(env)
        full["COLUMNS"] = str(width)
        result = _run_cli(["provider", "show", "long-provider-name-0123456789", "--state-dir", str(state_dir)], full)
        assert result.returncode == 0
        flat = result.stdout.replace("\n", "")
        assert "long-provider-name-0123456789" in flat
        assert "api.example-with-a-long-host.test" in flat
        assert "\x1b[" not in result.stdout

    plain = dict(env)
    plain["NO_COLOR"] = "1"
    result = _run_cli(["provider", "list", "--state-dir", str(state_dir)], plain)
    assert result.returncode == 0
    assert "\x1b[" not in result.stdout + result.stderr


def test_concurrent_read_modify_write_exactly_one_winner(live_gateway, catalog_env):
    from qingniao.cli import CliError, _get_config, _put_config

    state_dir, env = live_gateway
    config, generation = _get_config(state_dir)

    # two writers both based on the same generation; exactly one commits
    edit_a = json.loads(json.dumps(config))
    edit_a["providers"]["alpha"] = {"base_url": "https://alpha.example", "auth": "bearer", "credential_env": "QING_CAT_P"}
    edit_b = json.loads(json.dumps(config))
    edit_b["providers"]["beta"] = {"base_url": "https://beta.example", "auth": "bearer", "credential_env": "QING_CAT_P"}

    outcomes: list[str] = []
    lock = threading.Lock()

    def writer(edit: dict) -> None:
        try:
            _put_config(state_dir, edit, generation, "concurrent edit")
            with lock:
                outcomes.append("ok")
        except CliError as exc:
            with lock:
                outcomes.append(exc.code)

    t1 = threading.Thread(target=writer, args=(edit_a,))
    t2 = threading.Thread(target=writer, args=(edit_b,))
    t1.start()
    t2.start()
    t1.join(timeout=30)
    t2.join(timeout=30)
    assert sorted(outcomes) == ["generation_conflict", "ok"]

    final = _json_cli(["provider", "list", *_base(state_dir)], env)
    committed = {p["id"] for p in final["providers"]}
    assert committed == {"alpha"} or committed == {"beta"}
    # whichever won, the other writer's edit was not silently merged
    assert len(committed & {"alpha", "beta"}) == 1


def test_sequential_stale_generation_fails_with_stable_code(live_gateway):
    from qingniao.cli import CliError, _get_config, _put_config

    state_dir, env = live_gateway
    config1, gen1 = _get_config(state_dir)
    edit = json.loads(json.dumps(config1))
    edit["providers"]["first"] = {"base_url": "https://first.example", "auth": "bearer", "credential_env": "QING_CAT_P"}
    _put_config(state_dir, edit, gen1, "first edit")

    config2, gen2 = _get_config(state_dir)
    assert gen2 == gen1 + 1
    stale = json.loads(json.dumps(config2))
    stale["providers"]["second"] = {"base_url": "https://second.example", "auth": "bearer", "credential_env": "QING_CAT_P"}
    with pytest.raises(CliError) as excinfo:
        _put_config(state_dir, stale, gen1, "stale edit")
    assert excinfo.value.code == "generation_conflict"
    assert "current_generation" in excinfo.value.details
