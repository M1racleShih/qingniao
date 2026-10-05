from __future__ import annotations

import json
import threading
import time

import pytest
import uvicorn
from http.server import ThreadingHTTPServer
from typer.testing import CliRunner

from qingniao import state as state_mod
from qingniao.app import build_app
from qingniao.cli import app, _call, _resolve_instance
from qingniao.config import validate_config
from qingniao.gateway import Gateway
from qingniao.tokens import new_control_token, token_hash
from tests.conftest import _UpstreamHandler, make_config_dict


def _wait_started(server, timeout=15.0):
    deadline = time.time() + timeout
    while not server.started:
        if time.time() > deadline:
            raise TimeoutError("uvicorn did not start")
        time.sleep(0.02)


def test_control_token_never_sent_through_ambient_proxy(tmp_path, monkeypatch):
    capture = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    capture.daemon_threads = True
    capture.requests = []
    capture.responses = []
    threading.Thread(target=capture.serve_forever, daemon=True).start()
    proxy_url = f"http://127.0.0.1:{capture.server_address[1]}"

    gateway = Gateway(validate_config(make_config_dict()))
    control_token = new_control_token()
    uvicorn_server = uvicorn.Server(
        uvicorn.Config(
            build_app(gateway, admin_token_hash=token_hash(control_token)),
            host="127.0.0.1",
            port=0,
            log_level="warning",
            access_log=False,
            lifespan="on",
        )
    )
    threading.Thread(target=uvicorn_server.run, daemon=True).start()
    try:
        _wait_started(uvicorn_server)
        port = uvicorn_server.servers[0].sockets[0].getsockname()[1]
        state_dir = tmp_path / "state"
        state_mod.prepare_state_dir(state_dir)
        state_mod.write_discovery(state_dir, port=port, pid=1, control_token=control_token)
        instance, _ = gateway.create_instance(label="cli-x")

        for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setenv("HTTP_PROXY", proxy_url)
        monkeypatch.setenv("HTTPS_PROXY", proxy_url)
        monkeypatch.setenv("ALL_PROXY", proxy_url)

        response = _call(state_dir, "GET", "/control/v1/config")
        assert response.status_code == 200

        resolved = _resolve_instance((f"http://127.0.0.1:{port}", control_token), "cli-x")
        assert resolved["id"] == instance.id

        assert len(capture.requests) == 0
    finally:
        uvicorn_server.should_exit = True
        capture.shutdown()
        capture.server_close()


def test_route_set_gateway_down_concise_error(tmp_path):
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    dead_port = sock.getsockname()[1]
    sock.close()

    state_dir = tmp_path / "state"
    state_mod.prepare_state_dir(state_dir)
    state_mod.write_discovery(state_dir, port=dead_port, pid=1, control_token="stale-token")

    runner = CliRunner()
    result = runner.invoke(
        app,
        [
            "route",
            "set",
            "req-main",
            "model-p",
            "--instance",
            "x",
            "--state-dir",
            str(state_dir),
        ],
    )
    combined = result.output
    try:
        combined += result.stderr or ""
    except Exception:
        pass
    assert result.exit_code == 1
    assert "cannot reach gateway" in combined
    assert "Traceback" not in combined


def _run_cli_main(monkeypatch, argv):
    """Invoke the real module entry (parser errors included), returning
    its exit code with captured stdout/stderr."""
    import sys as _sys

    from qingniao import cli as cli_module

    monkeypatch.setattr(_sys, "argv", ["qing", *argv])
    return cli_module.main()


def test_parser_error_instances_limit_bad_json_outputs_management_error(monkeypatch, capsys):
    """E5: `instances --json --limit bad` keeps the parser exit code 2 but
    reports the error as machine-identifiable JSON on stdout."""
    code = _run_cli_main(monkeypatch, ["instances", "--json", "--limit", "bad"])
    out, err = capsys.readouterr()
    assert code == 2
    payload = json.loads(out)
    assert payload["error"]["code"] == "cli_error"
    assert "--limit" in payload["error"]["message"]
    assert "event" not in payload  # management JSON, not run-wrapper NDJSON


def test_parser_error_route_set_missing_instance_json_outputs_management_error(monkeypatch, capsys):
    code = _run_cli_main(monkeypatch, ["route", "set", "req-main", "dev-main", "--json"])
    out, err = capsys.readouterr()
    assert code == 2
    payload = json.loads(out)
    assert payload["error"]["code"] == "cli_error"
    assert "--instance" in payload["error"]["message"]


def test_parser_error_run_label_missing_value_emits_ndjson_on_stderr(monkeypatch, capsys):
    """E5: `run --json --label` (missing option value) keeps exit code 2
    and emits a wrapper NDJSON error event on stderr; stdout stays empty."""
    code = _run_cli_main(monkeypatch, ["run", "--json", "--label"])
    out, err = capsys.readouterr()
    assert code == 2
    assert out == ""
    event = json.loads(err.strip().splitlines()[-1])
    assert event["event"] == "error"
    assert event["code"] == "cli_error"
    assert event["message"]


def test_run_native_json_after_separator_is_not_machine_mode(monkeypatch, capsys, tmp_path):
    """A `--json` among native child arguments after '--' is never
    interpreted as the wrapper JSON flag: the launch fails in human mode
    with no NDJSON event on stderr."""
    code = _run_cli_main(
        monkeypatch, ["run", "--state-dir", str(tmp_path / "absent"), "--", "--json"]
    )
    out, err = capsys.readouterr()
    assert code == 2
    assert out == ""
    assert "event" not in err.splitlines()[-1]
    with pytest.raises(json.JSONDecodeError):
        json.loads(err.strip().splitlines()[-1])


def test_version_option_reports_package_version(monkeypatch, capsys):
    code = _run_cli_main(monkeypatch, ["--version"])
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.strip() == "0.1.0"
    # short form works too
    code = _run_cli_main(monkeypatch, ["-V"])
    out, _ = capsys.readouterr()
    assert code == 0
    assert out.strip() == "0.1.0"


def test_version_consistent_with_package_metadata():
    from importlib.metadata import version as metadata_version

    from qingniao import __version__

    # release-readiness contract: the CLI, the package metadata and the
    # release artifact must agree on the version
    try:
        installed = metadata_version("qingniao-gateway")
    except Exception:
        installed = None
    if installed is not None:
        assert installed == __version__ == "0.1.0"
