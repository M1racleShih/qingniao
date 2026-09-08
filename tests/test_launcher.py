"""qing run launcher tests using a fake Claude client on real TCP control."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from qingniao import launcher
from tests.test_ack import LiveGateway

FAKE_CLAUDE = """#!/usr/bin/env python3
import json, os, signal, sys, time

settings_index = sys.argv.index("--settings") + 1 if "--settings" in sys.argv else None
out = {"argv": sys.argv[1:], "cwd": os.getcwd()}
if settings_index:
    path = sys.argv[settings_index]
    out["settings_path"] = path
    out["settings_mode"] = oct(os.stat(path).st_mode & 0o777)
    with open(path) as fh:
        out["settings"] = json.load(fh)
out["env"] = dict(os.environ)
with open(os.environ["FAKE_CLAUDE_OUT"], "w") as fh:
    json.dump(out, fh)

behavior = os.environ.get("FAKE_CLAUDE_BEHAVIOR", "exit:0")
if behavior.startswith("exit:"):
    sys.exit(int(behavior.split(":", 1)[1]))
if behavior.startswith(("sleep:", "ignore:")):
    marker = os.environ["FAKE_CLAUDE_OUT"] + ".signals"
    ignoring = behavior.startswith("ignore:")
    received = []

    def record(signum, frame):
        received.append(signum)
        with open(marker, "w") as fh:
            fh.write(",".join(str(s) for s in received))
        if not ignoring:
            sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, record)
    signal.signal(signal.SIGINT, record)
    with open(os.environ["FAKE_CLAUDE_OUT"] + ".ready", "w") as fh:
        fh.write("1")
    time.sleep(float(behavior.split(":", 1)[1].split(":")[-1]))
sys.exit(0)
"""


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    script = bin_dir / "claude"
    script.write_text(FAKE_CLAUDE)
    script.chmod(0o755)
    observations = tmp_path / "observations"
    observations.mkdir()
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")
    monkeypatch.setenv("FAKE_CLAUDE_OUT_DIR", str(observations))
    counter = iter(range(10000))

    def next_out() -> str:
        path = observations / f"obs{next(counter)}.json"
        monkeypatch.setenv("FAKE_CLAUDE_OUT", str(path))
        return path

    return SimpleNamespace(bin_dir=bin_dir, observations=observations, next_out=next_out)


class SimpleNamespace:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def test_settings_file_neutralizes_conflicting_env_l1(tmp_path, monkeypatch, fake_claude):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "shell-oauth-token")
        monkeypatch.setenv("CLAUDE_CODE_USE_VERTEX", "1")
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label=None,
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=[],
        )
        assert code == 0
        obs = json.loads(out_path.read_text())
        env = obs["settings"]["env"]
        assert env["ANTHROPIC_API_KEY"] == ""
        assert env["ANTHROPIC_CUSTOM_HEADERS"] == ""
        assert env["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert env["CLAUDE_CODE_USE_BEDROCK"] == "0"
        assert env["CLAUDE_CODE_USE_VERTEX"] == "0"
        assert env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{live.port}"
        child_env = obs["env"]
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in child_env
        assert "CLAUDE_CODE_USE_VERTEX" not in child_env
        assert "CLAUDE_CODE_USE_BEDROCK" not in child_env
    finally:
        live.stop()


def test_settings_write_failure_after_registration_cleans_up_l2(tmp_path, monkeypatch, fake_claude, capsys):
    live = LiveGateway(tmp_path)
    try:
        def failing_write(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(launcher, "write_settings_file", failing_write)
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label=None,
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=[],
        )
        assert code == launcher.EXIT_USAGE
        assert "cannot prepare or start the client" in capsys.readouterr().err
        instances = live.get("/control/v1/instances?limit=100").json()["instances"]
        assert len(instances) == 1 and instances[0]["state"] == "ended"
    finally:
        monkeypatch.undo()
        live.stop()


def test_setup_failure_after_spawn_cleans_up_l2(tmp_path, monkeypatch, fake_claude, capsys):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "sleep:30")

        def failing_start(self):
            raise RuntimeError("keeper setup exploded")

        monkeypatch.setattr(launcher.LeaseKeeper, "start", failing_start)
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label=None,
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=[],
        )
        assert code == launcher.EXIT_USAGE
        err = capsys.readouterr().err
        assert "launcher setup failed" in err and "RuntimeError" in err
        assert "Traceback" not in err
        instances = live.get("/control/v1/instances?limit=100").json()["instances"]
        assert len(instances) == 1 and instances[0]["state"] == "ended"
    finally:
        monkeypatch.undo()
        live.stop()


def test_child_ignoring_sigterm_is_killed_and_reaped_l3(tmp_path, monkeypatch, fake_claude, capsys):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "ignore:30")
        monkeypatch.setattr(launcher, "CHILD_GRACE_SECONDS", 0.5)
        result = {}

        def run():
            result["code"] = launcher.run_launch(
                state_dir=live.state_dir,
                label=None,
                model=None,
                aux_model=None,
                route_overrides={},
                native_args=[],
                renew_interval=0.1,
                lease_seconds=0.3,
            )

        thread = threading.Thread(target=run)
        thread.start()
        ready = Path(str(out_path) + ".ready")
        deadline = time.monotonic() + 5
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert ready.exists(), "child signal handlers must be installed"
        live.stop()
        thread.join(timeout=30)
        assert not thread.is_alive(), "launcher must not wedge on a SIGTERM-ignoring child"
        assert result["code"] == launcher.EXIT_GATEWAY_LOST
        signals = Path(str(out_path) + ".signals")
        assert signals.exists(), "child must have received SIGTERM before the kill"
        obs = json.loads(out_path.read_text())
        assert not os.path.exists(obs["settings_path"])
        captured = capsys.readouterr().err
        assert "start a new 'qing run'" in captured
    finally:
        live.stop()


def test_env_stripping_and_preservation_unit():
    import os as _os

    saved = {k: _os.environ.get(k) for k in launcher.STRIP_CHILD_ENV}
    try:
        for key in launcher.STRIP_CHILD_ENV:
            _os.environ[key] = f"stale-{key}"
        _os.environ["UNRELATED_KEEP"] = "keep-me"
        env = launcher.build_child_env()
        for key in launcher.STRIP_CHILD_ENV:
            assert key not in env, key
        assert env.get("UNRELATED_KEEP") == "keep-me"
        assert "PATH" in env and "HOME" in env
    finally:
        for key, value in saved.items():
            if value is None:
                _os.environ.pop(key, None)
            else:
                _os.environ[key] = value
        _os.environ.pop("UNRELATED_KEEP", None)


def _env_for(live: LiveGateway):
    return {}


def test_run_registers_settings_file_and_passthrough(tmp_path, monkeypatch, fake_claude):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        monkeypatch.setenv("ANTHROPIC_API_KEY", "stale-ambient-key")
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label="[bold]worker[/bold]",
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=["-p", "--output-format", "json", "--resume", "conv-1"],
        )
        assert code == 0
        obs = json.loads(out_path.read_text())
        argv = obs["argv"]
        assert argv[0] == "--settings"
        settings_path = argv[1]
        assert argv[2:] == ["-p", "--output-format", "json", "--resume", "conv-1"]

        assert obs["settings_mode"] == "0o600"
        settings_env = obs["settings"]["env"]
        assert settings_env["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{live.port}"
        assert settings_env["ANTHROPIC_MODEL"] == "req-main"
        assert settings_env["CLAUDE_CODE_SUBAGENT_MODEL"] == "req-aux"
        token = settings_env["ANTHROPIC_AUTH_TOKEN"]
        assert len(token) >= 43 and token != live.control_token
        assert token not in " ".join(argv)

        child_env = obs["env"]
        assert "ANTHROPIC_API_KEY" not in child_env
        assert "ANTHROPIC_AUTH_TOKEN" not in child_env
        assert "ANTHROPIC_BASE_URL" not in child_env
        assert "CLAUDE_CODE_USE_BEDROCK" not in child_env
        assert "PATH" in child_env and "HOME" in child_env

        assert not os.path.exists(settings_path)

        instances = live.get("/control/v1/instances?limit=100").json()["instances"]
        assert len(instances) == 1
        assert instances[0]["label"] == "[bold]worker[/bold]"
        assert instances[0]["state"] == "ended"
    finally:
        live.stop()


def test_explicit_selections_reach_settings_and_registration(tmp_path, fake_claude):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label=None,
            model="req-aux",
            aux_model="req-aux",
            route_overrides={"req-special": "model-q"},
            native_args=[],
        )
        assert code == 0
        obs = json.loads(out_path.read_text())
        settings_env = obs["settings"]["env"]
        assert settings_env["ANTHROPIC_MODEL"] == "req-aux"
        assert settings_env["CLAUDE_CODE_SUBAGENT_MODEL"] == "req-aux"
        instance = live.get("/control/v1/instances?limit=100").json()["instances"][0]
        assert instance["model"] == "req-aux"
        assert "req-special" in instance["routes"]
        assert instance["routes"]["req-special"]["provider"] == "prov-q"
    finally:
        live.stop()


@pytest.mark.parametrize("bad", [["--model", "x"], ["--model=x"], ["--settings", "f"], ["--bare"]])
def test_conflicting_native_args_rejected_before_anything(tmp_path, fake_claude, bad):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        with pytest.raises(launcher.LaunchError) as excinfo:
            launcher.run_launch(
                state_dir=live.state_dir,
                label=None,
                model=None,
                aux_model=None,
                route_overrides={},
                native_args=bad,
            )
        assert "conflicts with qing run" in excinfo.value.message
        assert not out_path.exists()
        assert live.get("/control/v1/instances").json()["total"] == 0
    finally:
        live.stop()


def test_no_gateway_reports_without_starting_client(tmp_path, fake_claude):
    out_path = fake_claude.next_out()
    with pytest.raises(launcher.LaunchError) as excinfo:
        launcher.run_launch(
            state_dir=tmp_path / "empty-state",
            label=None,
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=[],
        )
    assert "no running gateway" in excinfo.value.message
    assert excinfo.value.exit_code == 2
    assert not out_path.exists()


def test_child_spawn_failure_ends_instance(tmp_path, monkeypatch, capsys):
    live = LiveGateway(tmp_path)
    try:
        empty = tmp_path / "empty-bin"
        empty.mkdir()
        monkeypatch.setenv("PATH", str(empty))
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label=None,
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=[],
        )
        assert code == launcher.EXIT_USAGE
        assert "cannot prepare or start the client" in capsys.readouterr().err
        instances = live.get("/control/v1/instances?limit=100").json()["instances"]
        assert len(instances) == 1 and instances[0]["state"] == "ended"
    finally:
        live.stop()


@pytest.mark.parametrize("behavior,expected", [("exit:0", 0), ("exit:42", 42)])
def test_child_exit_code_passthrough(tmp_path, monkeypatch, fake_claude, behavior, expected):
    live = LiveGateway(tmp_path)
    try:
        fake_claude.next_out()
        monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", behavior)
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label=None,
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=[],
        )
        assert code == expected
    finally:
        live.stop()


def test_resume_and_each_run_get_fresh_identity(tmp_path, fake_claude):
    live = LiveGateway(tmp_path)
    try:
        ids = []
        for run in range(2):
            fake_claude.next_out()
            code = launcher.run_launch(
                state_dir=live.state_dir,
                label=None,
                model=None,
                aux_model=None,
                route_overrides={},
                native_args=["--resume", "same-conversation"],
            )
            assert code == 0
            instances = live.get("/control/v1/instances?limit=100").json()["instances"]
            ids.append(instances[-1]["id"])
        assert ids[0] != ids[1]
    finally:
        live.stop()


def test_gateway_loss_terminates_child_with_reattach_guidance(tmp_path, monkeypatch, fake_claude, capsys):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "sleep:30")
        result = {}

        def run():
            result["code"] = launcher.run_launch(
                state_dir=live.state_dir,
                label=None,
                model=None,
                aux_model=None,
                route_overrides={},
                native_args=[],
                renew_interval=0.1,
                lease_seconds=0.3,
            )

        thread = threading.Thread(target=run)
        thread.start()
        deadline = time.monotonic() + 5
        while not out_path.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert out_path.exists()
        live.stop()
        thread.join(timeout=30)
        assert not thread.is_alive()
        assert result["code"] == launcher.EXIT_GATEWAY_LOST
        captured = capsys.readouterr().err
        assert "instance lost" in captured
        assert "start a new 'qing run'" in captured
        signals = Path(str(out_path) + ".signals")
        assert signals.exists(), "child must have received a termination signal"
        obs = json.loads(out_path.read_text())
        assert not os.path.exists(obs["settings_path"])
    finally:
        live.stop()


class _StubResponse:
    def __init__(self, status_code, code=""):
        self.status_code = status_code
        self._code = code

    def json(self):
        return {"error": {"code": self._code}}


class _StubClient:
    next_response = None

    def __init__(self, **kwargs):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def post(self, path):
        if isinstance(_StubClient.next_response, Exception):
            raise _StubClient.next_response
        return _StubClient.next_response


def _keeper(monkeypatch, response, clock):
    from qingniao.launcher import LeaseKeeper

    monkeypatch.setattr(launcher.httpx, "Client", _StubClient)
    lost = []
    keeper = LeaseKeeper(
        base_url="http://127.0.0.1:1",
        control_token="t",
        instance_id="i-x",
        on_lost=lost.append,
        interval=0.01,
        lease_seconds=0.2,
        clock=clock,
    )
    return keeper, lost


def test_lease_keeper_single_network_error_is_warning_only(monkeypatch):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    calls = []
    calls_lock = threading.Lock()
    failed_once: list = []

    class RecoveringClient(_StubClient):
        def post(self, path):
            with calls_lock:
                calls.append(path)
                first = len(calls) == 1
            if first and not failed_once:
                failed_once.append(True)
                raise httpx.ConnectError("boom")
            return _StubResponse(200)

    monkeypatch.setattr(launcher.httpx, "Client", RecoveringClient)
    lost = []
    from qingniao.launcher import LeaseKeeper

    keeper = LeaseKeeper(
        base_url="http://127.0.0.1:1",
        control_token="t",
        instance_id="i-x",
        on_lost=lost.append,
        interval=0.01,
        lease_seconds=0.2,
        clock=Clock(),
    )
    keeper.start()
    deadline = time.monotonic() + 2
    while len(calls) < 2 and time.monotonic() < deadline:
        time.sleep(0.005)
    keeper.stop()
    assert len(calls) >= 2, "keeper must keep renewing after one failure"
    assert keeper._thread is None, "keeper thread must join cleanly, not die from an exception"
    assert lost == [], "a single failed renewal must not declare the instance lost"


def test_lease_keeper_declares_loss_on_not_found(monkeypatch):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    _StubClient.next_response = _StubResponse(404, "instance_not_found")
    keeper, lost = _keeper(monkeypatch, None, Clock())
    keeper.start()
    time.sleep(0.1)
    keeper.stop()
    assert lost and "no longer knows" in lost[0]


def test_lease_keeper_declares_loss_on_admin_token_change(monkeypatch):
    class Clock:
        now = 0.0

        def __call__(self):
            return self.now

    _StubClient.next_response = _StubResponse(401, "invalid_admin_token")
    keeper, lost = _keeper(monkeypatch, None, Clock())
    keeper.start()
    time.sleep(0.1)
    keeper.stop()
    assert lost and "control identity changed" in lost[0]


def test_lease_keeper_declares_loss_after_sustained_failures(monkeypatch):
    class Clock:
        now = 0.0

        def __call__(self):
            Clock.now += 0.05
            return Clock.now

    _StubClient.next_response = httpx.ConnectError("down")
    keeper, lost = _keeper(monkeypatch, None, Clock())
    keeper.start()
    deadline = time.monotonic() + 2
    while not lost and time.monotonic() < deadline:
        time.sleep(0.01)
    keeper.stop()
    assert lost and "longer than the lease" in lost[0]


def test_thread_start_failure_still_cleans_up_l2(tmp_path, monkeypatch, fake_claude, capsys):
    live = LiveGateway(tmp_path)
    try:
        out_path = fake_claude.next_out()
        monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "sleep:30")

        real_start = threading.Thread.start

        def failing_start(self):
            if self.name == "qing-lease":
                raise RuntimeError("thread start exploded")
            return real_start(self)

        monkeypatch.setattr(threading.Thread, "start", failing_start)
        code = launcher.run_launch(
            state_dir=live.state_dir,
            label=None,
            model=None,
            aux_model=None,
            route_overrides={},
            native_args=[],
        )
        monkeypatch.undo()
        assert code == launcher.EXIT_USAGE
        err = capsys.readouterr().err
        assert "launcher setup failed" in err
        assert "Traceback" not in err
        instances = live.get("/control/v1/instances?limit=100").json()["instances"]
        assert len(instances) == 1 and instances[0]["state"] == "ended"
    finally:
        monkeypatch.undo()
        live.stop()


def test_sigterm_during_setup_window_survives_and_cleans_up(tmp_path, monkeypatch, fake_claude):
    live = LiveGateway(tmp_path)
    launcher_proc = None
    try:
        out_path = fake_claude.next_out()
        monkeypatch.setenv("FAKE_CLAUDE_BEHAVIOR", "sleep:30")
        monkeypatch.setattr(launcher, "CHILD_GRACE_SECONDS", 1.0)
        import sys as _sys

        env = dict(os.environ)
        env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
        launcher_proc = subprocess.Popen(
            [
                _sys.executable, "-m", "qingniao", "run",
                "--state-dir", str(live.state_dir),
            ],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        # Observe the registration line, then signal immediately: this is
        # the setup window between registration and the wait loop.
        deadline = time.monotonic() + 10
        registered = False
        while time.monotonic() < deadline and not registered:
            if launcher_proc.poll() is not None:
                break
            # poll stderr non-destructively is not possible with pipes;
            # wait for the fake child to spawn instead, then signal.
            if out_path.exists():
                registered = True
                break
            time.sleep(0.005)
        assert registered, "child never started"
        launcher_proc.send_signal(signal_module.SIGTERM)
        stdout, stderr = launcher_proc.communicate(timeout=30)
        assert launcher_proc.returncode == 128 + signal_module.SIGTERM, (
            f"wrapper must survive the setup window with the signal exit code, got {launcher_proc.returncode}"
        )
        assert "instance lost" not in stderr
        instances = live.get("/control/v1/instances?limit=100").json()["instances"]
        assert len(instances) == 1 and instances[0]["state"] == "ended"
        obs = json.loads(out_path.read_text())
        assert not os.path.exists(obs["settings_path"])
    finally:
        if launcher_proc is not None and launcher_proc.poll() is None:
            launcher_proc.kill()
            launcher_proc.wait()
        monkeypatch.undo()
        live.stop()


import signal as signal_module


def test_run_json_absent_gateway_emits_ndjson_error(tmp_path):
    import subprocess as _sp
    import sys as _sys

    env = dict(os.environ)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parent.parent / "src")
    proc = _sp.Popen(
        [_sys.executable, "-m", "qingniao", "run", "--json", "--state-dir", str(tmp_path / "absent")],
        stdout=_sp.PIPE,
        stderr=_sp.PIPE,
        text=True,
        env=env,
    )
    stdout, stderr = proc.communicate(timeout=30)
    assert proc.returncode == 2
    assert stdout == ""
    event = json.loads(stderr.strip().splitlines()[-1])
    assert event["event"] == "error"
    assert event["code"] == "gateway_unreachable"
    assert "start one with 'qing serve'" in event["message"]


E3_CLEANUP_HELPER = r'''
import importlib.util, os, signal, subprocess, sys, time

spec = importlib.util.spec_from_file_location("launcher_experiment", sys.argv[1])
experiment = importlib.util.module_from_spec(spec)
spec.loader.exec_module(experiment)

assert experiment._become_child_subreaper(), "subreaper setup failed"
SLEEPER = "import time; time.sleep(300)"

owned_procs = []
owned_pids = []


def spawn_sleeper():
    proc = subprocess.Popen(
        [sys.executable, "-c", SLEEPER],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    owned_procs.append(proc)
    return proc


def spawn_wrapper(code):
    proc = subprocess.Popen(
        [sys.executable, "-c", code],
        stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
    )
    owned_procs.append(proc)
    return proc


def cleanup():
    # The helper never leaks its own test processes on assertion failure
    # or watchdog timeout.
    for pid, token in owned_pids:
        experiment._reap_owned(pid, token)
    for proc in owned_procs:
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def watchdog(signum, frame):
    raise SystemExit("helper watchdog timeout")


signal.signal(signal.SIGALRM, watchdog)
signal.alarm(90)
try:
    bystander = spawn_sleeper()

    # Case 1: a live wrapper ignores SIGTERM; teardown must force-kill
    # it and still reap its real descendant.
    wrapper_code = (
        "import subprocess, signal, sys, time; "
        "signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); "
        "print('child', child.pid, flush=True); "
        "time.sleep(300)"
    )
    wrapper = spawn_wrapper(wrapper_code)
    descendant_pid = int(wrapper.stdout.readline().split()[1])
    descendant_token = experiment._proc_start_token(descendant_pid)
    wrapper_token = experiment._proc_start_token(wrapper.pid)
    assert descendant_token and wrapper_token
    owned_pids.append((descendant_pid, descendant_token))
    owned_pids.append((wrapper.pid, wrapper_token))

    # Fail closed: an unverified pid must never be signalled.
    experiment._reap_owned(bystander.pid, None)
    assert experiment._proc_start_token(bystander.pid) is not None, "unverified pid was signalled"

    handled = experiment._teardown_owned(wrapper, wait_timeout=1.0)
    assert wrapper.poll() is not None, "wrapper not reaped"
    assert experiment._proc_start_token(wrapper.pid) != wrapper_token, "wrapper leftover"
    assert experiment._proc_start_token(descendant_pid) != descendant_token, "descendant leftover"
    handled_pids = {pid for pid, _ in handled}
    assert descendant_pid in handled_pids and wrapper.pid in handled_pids
    assert experiment._leftover_pids(handled) == [], "live/zombie leftover"
    print("E3_CASE_LIVE_PASS")

    # Case 2: the wrapper is force-killed and reaped BEFORE teardown; its
    # child is adopted by this subreaper process and must still be
    # collected, while direct spawns outside the known set are untouched.
    wrapper2 = spawn_wrapper(
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); "
        "print('child', child.pid, flush=True); "
        "time.sleep(300)"
    )
    descendant2_pid = int(wrapper2.stdout.readline().split()[1])
    descendant2_token = experiment._proc_start_token(descendant2_pid)
    assert descendant2_token
    owned_pids.append((descendant2_pid, descendant2_token))
    wrapper2.kill()
    wrapper2.wait(timeout=10)
    deadline = time.time() + 5
    while descendant2_pid not in experiment._child_pids(os.getpid()):
        assert time.time() < deadline, "descendant never adopted"
        time.sleep(0.05)
    handled2 = experiment._collect_adopted_children({wrapper2.pid, bystander.pid})
    assert descendant2_pid in {pid for pid, _ in handled2}, "adopted child not collected"
    assert experiment._proc_start_token(descendant2_pid) != descendant2_token, "adopted child leftover"
    try:
        os.waitpid(descendant2_pid, os.WNOHANG)
        raise AssertionError("adopted child was not collected by the sweep")
    except ChildProcessError:
        pass
    assert experiment._leftover_pids(handled2) == []
    assert experiment._proc_start_token(bystander.pid) is not None, "bystander killed"
    print("E3_CASE_ADOPTED_PASS")

    # Case 3: nested adoption. After the wrapper dies, its child is
    # adopted while still holding a live grandchild; collecting the child
    # must also reap the grandchild newly adopted by the child's death.
    wrapper3 = spawn_wrapper(
        "import subprocess, sys, time; "
        "child = subprocess.Popen([sys.executable, '-c', "
        "\"import subprocess, sys, time; "
        "grand = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(300)']); "
        "print('grand', grand.pid, flush=True); "
        "time.sleep(300)\"]); "
        "print('child', child.pid, flush=True); "
        "time.sleep(300)"
    )
    child3_pid = int(wrapper3.stdout.readline().split()[1])
    grand3_pid = int(wrapper3.stdout.readline().split()[1])
    child3_token = experiment._proc_start_token(child3_pid)
    grand3_token = experiment._proc_start_token(grand3_pid)
    assert child3_token and grand3_token
    owned_pids.append((child3_pid, child3_token))
    owned_pids.append((grand3_pid, grand3_token))
    wrapper3.kill()
    wrapper3.wait(timeout=10)
    deadline = time.time() + 5
    while child3_pid not in experiment._child_pids(os.getpid()):
        assert time.time() < deadline, "nested child never adopted"
        time.sleep(0.05)
    assert grand3_pid not in experiment._child_pids(os.getpid()), "grandchild unexpectedly adopted early"
    handled3 = experiment._collect_adopted_children({wrapper3.pid, bystander.pid})
    collected3 = {pid for pid, _ in handled3}
    assert child3_pid in collected3, "adopted child not collected"
    assert grand3_pid in collected3, "newly adopted grandchild not collected"
    assert experiment._proc_start_token(child3_pid) != child3_token, "child leftover"
    assert experiment._proc_start_token(grand3_pid) != grand3_token, "grandchild leftover"
    assert experiment._leftover_pids(handled3) == []
    assert experiment._proc_start_token(bystander.pid) is not None, "bystander killed"
    print("E3_CASE_NESTED_PASS")
finally:
    cleanup()
'''


def _load_launcher_experiment():
    import importlib.util

    path = (
        Path(__file__).resolve().parent.parent
        / "experiments" / "claude-launcher-verification" / "run_experiment.py"
    )
    spec = importlib.util.spec_from_file_location("qingniao_launcher_experiment", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_experiment_barrier_rejects_sequential_snapshots():
    """E1: the same-snapshot pair predicate and strict completed-interval
    overlap reject entirely sequential A-then-B feeds, and observations
    from separate reads never combine into a barrier."""
    module = _load_launcher_experiment()
    a, b = {"id": "inst-a"}, {"id": "inst-b"}

    def rec(inst, outcome, started=0.0, finished=None):
        return {
            "instance_id": inst["id"],
            "outcome": outcome,
            "started_at": started,
            "finished_at": finished,
        }

    sequential_snapshots = [
        [rec(a, "in_progress", 0.0), rec(b, "success", 10.0, 11.0)],
        [rec(a, "success", 0.0, 1.0), rec(b, "in_progress", 10.0)],
    ]
    for records in sequential_snapshots:
        assert not module.concurrent_instance_pair(records, {a["id"], b["id"]})
        assert not module.completed_interval_overlap(records, {a["id"], b["id"]})

    # Completed but disjoint or merely touching spans are not overlap.
    assert not module.completed_interval_overlap(
        [rec(a, "success", 0.0, 1.0), rec(b, "success", 2.0, 3.0)], {a["id"], b["id"]}
    )
    assert not module.completed_interval_overlap(
        [rec(a, "success", 0.0, 5.0), rec(b, "success", 5.0, 9.0)], {a["id"], b["id"]}
    )

    # Two separate reads each holding one active instance are no barrier;
    # only one snapshot with both in flight is.
    assert not module.concurrent_instance_pair([rec(a, "in_progress")], {a["id"], b["id"]})
    assert not module.concurrent_instance_pair([rec(b, "in_progress")], {a["id"], b["id"]})
    assert module.concurrent_instance_pair(
        [rec(a, "in_progress"), rec(b, "in_progress")], {a["id"], b["id"]}
    )

    # Completed intervals with strict positive overlap qualify.
    assert module.completed_interval_overlap(
        [rec(a, "success", 0.0, 10.0), rec(b, "success", 5.0, 6.0)], {a["id"], b["id"]}
    )


@pytest.mark.skipif(
    not sys.platform.startswith("linux"),
    reason="owned-descendant cleanup verification uses Linux /proc and child-subreaper",
)
def test_experiment_cleanup_force_kill_and_adopted_descendants(tmp_path):
    """E3: experiment-owned cleanup survives a SIGTERM-ignoring wrapper
    (bounded force-kill + descendant reap), a wrapper that died before
    teardown (its child is adopted by the subreaper helper and still
    collected), and nested adoption (the adopted child's own live
    grandchild is reaped too). An unverified pid is never signalled and
    an unrelated bystander is left running. The child-subreaper flag is
    set only in the helper subprocess, so pytest itself is unaffected;
    the helper cleans up its own test processes on assertion failure or
    watchdog timeout."""
    experiment_path = (
        Path(__file__).resolve().parent.parent
        / "experiments" / "claude-launcher-verification" / "run_experiment.py"
    )
    helper = tmp_path / "e3_cleanup_helper.py"
    helper.write_text(E3_CLEANUP_HELPER)
    proc = subprocess.run(
        [sys.executable, str(helper), str(experiment_path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert proc.returncode == 0, f"helper failed:\n{proc.stdout}\n{proc.stderr}"
    assert "E3_CASE_LIVE_PASS" in proc.stdout
    assert "E3_CASE_ADOPTED_PASS" in proc.stdout
    assert "E3_CASE_NESTED_PASS" in proc.stdout
