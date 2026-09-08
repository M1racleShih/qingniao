"""Terminal acceptance: real PTY widths, NO_COLOR/TERM=dumb/redirect behavior,
literal text, complete IDs and Unknown-vs-zero usage."""

from __future__ import annotations

import fcntl
import json
import os
import pty
import select
import struct
import subprocess
import sys
import termios
import time
from pathlib import Path

from tests.test_ack import LiveGateway


def run_cli(args: list[str], *, cols: int | None = None, env_extra: dict | None = None, pipe: bool = False) -> tuple[int, str]:
    env = dict(os.environ)
    if env_extra:
        env.update(env_extra)
    if pipe:
        proc = subprocess.Popen(
            [sys.executable, "-m", "qingniao", *args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            text=True,
        )
        out, err = proc.communicate(timeout=60)
        return proc.returncode, out + err

    master, slave = pty.openpty()
    if cols is not None:
        fcntl.ioctl(slave, termios.TIOCSWINSZ, struct.pack("HHHH", 24, cols, 0, 0))
    proc = subprocess.Popen(
        [sys.executable, "-m", "qingniao", *args],
        stdout=slave,
        stderr=slave,
        stdin=slave,
        env=env,
        close_fds=True,
    )
    os.close(slave)
    chunks: list[bytes] = []
    deadline = time.monotonic() + 60
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            proc.kill()
            break
        ready, _, _ = select.select([master], [], [], min(1.0, remaining))
        if not ready:
            if proc.poll() is not None:
                break
            continue
        try:
            data = os.read(master, 65536)
        except OSError:
            break
        if not data:
            break
        chunks.append(data)
    os.close(master)
    proc.wait(timeout=30)
    return proc.returncode, b"".join(chunks).decode("utf-8", "replace")


def _make_records(gateway, instance_id: str) -> None:
    zero_id = gateway.begin_request(
        instance_id=instance_id, request_model="req-main", provider_id="prov-p",
        upstream_model="vendor/p-zero", route_revision=0,
    )
    gateway.finish_request(zero_id, outcome="success", status_code=200, usage={"input_tokens": 3, "output_tokens": 0})
    null_id = gateway.begin_request(
        instance_id=instance_id, request_model="req-main", provider_id="prov-p",
        upstream_model="vendor/p-null", route_revision=1,
    )
    gateway.finish_request(null_id, outcome="in_progress" if False else "success", status_code=200, usage=None)


def test_pty_widths_keep_full_ids_and_routes(tmp_path):
    live = LiveGateway(tmp_path)
    try:
        instance, _ = live.gateway.create_instance(label="[bold]lit[/bold]")
        instance_id = instance.id
        for cols in (40, 80, 120):
            code, out = run_cli(
                ["instances", "--state-dir", str(live.state_dir)], cols=cols
            )
            assert code == 0, (cols, out)
            assert instance_id in out, f"full instance ID lost at {cols} columns"
            assert "prov-p/vendor/p" in out.replace("\n", ""), f"provider/upstream lost at {cols} columns"
            assert "[bold]lit[/bold]" in out, f"literal label mangled at {cols} columns"
    finally:
        live.stop()


def test_pty_requests_columns_and_usage_semantics(tmp_path):
    live = LiveGateway(tmp_path)
    try:
        instance, _ = live.gateway.create_instance(label="r")
        _make_records(live.gateway, instance.id)
        for cols in (40, 80, 120):
            code, out = run_cli(["requests", "--state-dir", str(live.state_dir)], cols=cols)
            assert code == 0, (cols, out)
            assert "prov-p" in out
            assert "vendor/p-zero" in out, f"upstream model lost at {cols} columns"
            assert "vendor/p-null" in out
            flat = out.replace("\n", "")
            assert "output=0" in flat, f"real zero usage must display 0 at {cols} columns"
            assert "unknown" in flat, f"missing usage must display unknown at {cols} columns"
    finally:
        live.stop()


def test_no_color_and_dumb_and_redirect_emit_no_ansi(tmp_path):
    live = LiveGateway(tmp_path)
    try:
        instance, _ = live.gateway.create_instance(label="plain")
        for env_extra in ({"NO_COLOR": "1"}, {"TERM": "dumb"}):
            code, out = run_cli(
                ["instances", "--state-dir", str(live.state_dir)], cols=80, env_extra=env_extra
            )
            assert code == 0, out
            assert instance.id in out
            assert "\x1b[" not in out, f"ANSI escapes with {env_extra}"
        code, out = run_cli(["instances", "--state-dir", str(live.state_dir)], pipe=True)
        assert code == 0, out
        assert instance.id in out
        assert "\x1b[" not in out, "ANSI escapes when redirected"
    finally:
        live.stop()


def test_literal_markup_text_not_interpreted(tmp_path):
    live = LiveGateway(tmp_path)
    try:
        live.gateway.create_instance(label="[red]not-red[/red]")
        live.gateway.create_instance(label="[bold]x[/bold]")
        code, out = run_cli(["instances", "--state-dir", str(live.state_dir)], cols=80)
        assert code == 0, out
        assert "[red]not-red[/red]" in out
        assert "[bold]x[/bold]" in out
    finally:
        live.stop()


def test_route_set_output_shows_full_details(tmp_path):
    live = LiveGateway(tmp_path)
    try:
        instance, _ = live.gateway.create_instance(label="switch-me")
        live.gateway.set_route(instance.id, "req-main", "model-q", 0)  # make before != after
        from typer.testing import CliRunner
        from qingniao.cli import app as cli_app

        runner = CliRunner()
        result = runner.invoke(
            cli_app,
            [
                "route", "set", "req-aux", "model-q",
                "--instance", "switch-me",
                "--state-dir", str(live.state_dir),
            ],
            env={"COLUMNS": "80", "TERM": "dumb"},
        )
        assert result.exit_code == 0, result.output
        out = result.output
        assert instance.id in out
        assert "provider prov-p (upstream vendor/p)" in out, "before provider/upstream missing"
        assert "provider prov-q (upstream vendor/q)" in out, "after provider/upstream missing"
        assert "applied revision: 2" in out
        assert "in-flight requests keep the previous snapshot" in out
    finally:
        live.stop()


def test_json_output_is_parseable_and_errors_are_structured(tmp_path):
    live = LiveGateway(tmp_path)
    try:
        instance, _ = live.gateway.create_instance(label="j")
        _make_records(live.gateway, instance.id)
        code, out = run_cli(["instances", "--json", "--state-dir", str(live.state_dir)], pipe=True)
        assert code == 0, out
        payload = json.loads(out)
        assert payload["total"] == 1 and payload["instances"][0]["id"] == instance.id

        code, out = run_cli(
            ["route", "set", "req-main", "model-q", "--instance", "missing", "--json", "--state-dir", str(live.state_dir)],
            pipe=True,
        )
        assert code == 1, out
        error = json.loads(out)["error"]
        assert error["code"] == "cli_error"
        assert "missing" in error["message"]

        code, out = run_cli(["config", "apply", "/nonexistent.json", "--json"], pipe=True)
        assert code == 1, out
        error = json.loads(out)["error"]
        assert error["code"] == "cli_error"

        # transport failures are structured under --json too
        stale = tmp_path / "stale-state"
        import socket as _socket

        sock = _socket.socket()
        sock.bind(("127.0.0.1", 0))
        dead_port = sock.getsockname()[1]
        sock.close()
        from qingniao import state as state_mod

        state_mod.prepare_state_dir(stale)
        state_mod.write_discovery(stale, port=dead_port, pid=1, control_token="stale")
        code, out = run_cli(
            ["route", "set", "req-main", "model-q", "--instance", "x", "--json", "--state-dir", str(stale)],
            pipe=True,
        )
        assert code == 1, out
        error = json.loads(out)["error"]
        assert error["code"] == "gateway_unreachable"
    finally:
        live.stop()


def test_catalog_stale_marker_visible(tmp_path):
    live = LiveGateway(tmp_path)
    try:
        instance, _ = live.gateway.create_instance(label="stale")
        edited = json.loads(json.dumps(_base_config()))
        edited["providers"]["prov-p"]["base_url"] = "http://127.0.0.1:9999"
        live.gateway.apply_config(edited)
        code, out = run_cli(["instances", "--state-dir", str(live.state_dir)], cols=80)
        assert code == 0, out
        assert "*" in out
        assert "catalog entry changed or was removed" in out
    finally:
        live.stop()


def _base_config() -> dict:
    from tests.conftest import make_config_dict

    return make_config_dict()
