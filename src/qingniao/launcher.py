"""qing run: register an instance, launch Claude attached to the gateway.

Lifecycle: register via the control API (fresh identity per launch and per
--resume), write a per-run 0600 JSON --settings file carrying the gateway
transport and the selected request models (the file outranks project
settings env, so the connection cannot be hijacked), spawn Claude with the
user's normal environment minus transport-conflicting variables, renew the
30 s lease every 10 s, forward SIGINT/SIGTERM, reap the child, end the
registration and delete the temporary file on every exit path.

The instance bearer appears only inside the temporary settings file and the
child process environment it produces — never in argv text, logs, status
output, request records or the shared configuration.
"""

from __future__ import annotations

import contextlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Callable, Optional

import httpx

from . import state as state_mod

LEASE_SECONDS = 30.0
RENEW_INTERVAL = 10.0
RENEW_TIMEOUT = 5.0
CHILD_GRACE_SECONDS = 10.0
FIRST_REQUEST_POLL = 2.0

# Native Claude arguments that would fight the gateway transport or the
# launcher's model injection. Everything else after `--` is passed through.
CONFLICTING_NATIVE_ARGS = ("--settings", "--model", "--bare")

# Inherited environment that could bypass the gateway transport. The
# temporary settings file already outranks project settings env; stripping
# these prevents stale ambient credentials and third-party provider
# switches from even reaching the child.
STRIP_CHILD_ENV = (
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_CUSTOM_HEADERS",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_SMALL_FAST_MODEL",
    "ANTHROPIC_BEDROCK_BASE_URL",
    "ANTHROPIC_VERTEX_BASE_URL",
    "CLAUDE_CODE_OAUTH_TOKEN",
    "CLAUDE_CODE_SUBAGENT_MODEL",
    "CLAUDE_CODE_USE_BEDROCK",
    "CLAUDE_CODE_USE_VERTEX",
    "CLAUDE_CODE_USE_FOUNDRY",
    "CLAUDE_CODE_USE_MANTLE",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD",
    "CLAUDE_CODE_USE_GATEWAY",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH",
)

# Conflicting credentials and provider switches neutralized inside the
# temporary settings file itself, so a project's settings.env cannot
# re-enable them (the file outranks project settings env). Empty strings
# neutralize credential values; "0" neutralizes boolean provider switches.
# The gateway switch is neutralized too: qing already provides the gateway
# path, and a second gateway layer would bypass this instance's routing.
NEUTRALIZED_ENV = {
    "ANTHROPIC_API_KEY": "",
    "ANTHROPIC_CUSTOM_HEADERS": "",
    "ANTHROPIC_SMALL_FAST_MODEL": "",
    "ANTHROPIC_BEDROCK_BASE_URL": "",
    "ANTHROPIC_VERTEX_BASE_URL": "",
    "CLAUDE_CODE_OAUTH_TOKEN": "",
    "CLAUDE_CODE_USE_BEDROCK": "0",
    "CLAUDE_CODE_USE_VERTEX": "0",
    "CLAUDE_CODE_USE_FOUNDRY": "0",
    "CLAUDE_CODE_USE_MANTLE": "0",
    "CLAUDE_CODE_USE_ANTHROPIC_AWS": "0",
    "CLAUDE_CODE_USE_ANTHROPIC_GOOGLE_CLOUD": "0",
    "CLAUDE_CODE_USE_GATEWAY": "0",
    "CLAUDE_CODE_SKIP_BEDROCK_AUTH": "0",
    "CLAUDE_CODE_SKIP_VERTEX_AUTH": "0",
}

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_GATEWAY_LOST = 3


class LaunchError(Exception):
    def __init__(self, message: str, exit_code: int = EXIT_USAGE, code: str = "launch_error"):
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.code = code


_MACHINE_EVENTS = False


def _emit(event: str, message: Optional[str] = None, **fields) -> None:
    """Launcher diagnostics. Human mode prints text; machine mode emits one
    NDJSON event per line on stderr. Claude's own stderr may interleave;
    the combined stderr is never claimed to be a pure JSON document."""
    if _MACHINE_EVENTS:
        line = {"event": event}
        if message is not None:
            line["message"] = message
        line.update(fields)
        print(json.dumps(line, sort_keys=True), file=sys.stderr, flush=True)
    else:
        print(message if message is not None else event, file=sys.stderr, flush=True)


def _stderr(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def check_native_args(args: list[str]) -> None:
    """Reject native arguments that conflict with the launcher transport."""
    for arg in args:
        name = arg.split("=", 1)[0]
        if name in CONFLICTING_NATIVE_ARGS:
            equivalents = {
                "--settings": "the gateway transport is injected via a temporary settings file",
                "--model": "use 'qing run --model' / '--aux-model' / '--route'",
                "--bare": "it disables the bearer transport the gateway relies on",
            }
            raise LaunchError(
                f"native argument {name!r} conflicts with qing run ({equivalents[name]}); "
                "pass non-conflicting native arguments after '--'",
                code="conflicting_native_arg",
            )


def _endpoint(state_dir: Path) -> tuple[str, str]:
    discovery = state_mod.read_discovery(state_dir)
    if discovery is None:
        raise LaunchError(
            f"no running gateway found in state directory {state_dir}; start one with 'qing serve'",
            code="gateway_unreachable",
        )
    return f"http://127.0.0.1:{discovery['port']}", discovery["control_token"]


def register_instance(
    base_url: str,
    control_token: str,
    *,
    label: Optional[str],
    model: Optional[str],
    aux_model: Optional[str],
    route_overrides: dict[str, str],
) -> tuple[dict, str]:
    payload: dict = {}
    if label is not None:
        payload["label"] = label
    if model is not None:
        payload["model"] = model
    if aux_model is not None:
        payload["aux_model"] = aux_model
    if route_overrides:
        payload["routes"] = route_overrides
    try:
        with httpx.Client(
            base_url=base_url,
            headers={"authorization": f"Bearer {control_token}"},
            timeout=15.0,
            trust_env=False,
        ) as client:
            response = client.post("/control/v1/instances", json=payload)
    except httpx.HTTPError as exc:
        raise LaunchError(
            f"cannot reach gateway to register the instance ({type(exc).__name__}); "
            "the client was not started",
            code="gateway_unreachable",
        ) from exc
    if response.status_code >= 400:
        try:
            err = response.json().get("error", {})
        except ValueError:
            err = {}
        raise LaunchError(
            f"gateway rejected the instance registration: {err.get('code', response.status_code)}: "
            f"{err.get('message', response.text[:200])}; the client was not started",
            code="registration_failed",
        )
    body = response.json()
    return body["instance"], body["token"]


def write_settings_file(token: str, base_url: str, model: str, aux_model: str) -> str:
    fd, path = tempfile.mkstemp(prefix="qingniao-instance-", suffix=".json")
    try:
        env = dict(NEUTRALIZED_ENV)
        env.update(
            {
                "ANTHROPIC_BASE_URL": base_url,
                "ANTHROPIC_AUTH_TOKEN": token,
                "ANTHROPIC_MODEL": model,
                "CLAUDE_CODE_SUBAGENT_MODEL": aux_model,
            }
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"env": env}, fh, indent=2)
        os.chmod(path, 0o600)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(path)
        raise
    return path


def build_child_env() -> dict:
    env = dict(os.environ)
    for key in STRIP_CHILD_ENV:
        env.pop(key, None)
    return env


class LeaseKeeper:
    """Renews the instance lease and reports explicit loss.

    A single failed renewal is a warning, never a verdict: the instance is
    only declared lost on an explicit not-found/ended/invalid-admin answer
    (gateway restart) or once failures outlast the lease. The keeper never
    re-registers: after a gateway restart the old identity is invalid and
    the user must launch a new 'qing run'.
    """

    def __init__(
        self,
        *,
        base_url: str,
        control_token: str,
        instance_id: str,
        on_lost: Callable[[str], None],
        interval: float = RENEW_INTERVAL,
        lease_seconds: float = LEASE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ):
        self._base_url = base_url
        self._token = control_token
        self._instance_id = instance_id
        self._on_lost = on_lost
        self._interval = interval
        self._lease = lease_seconds
        self._clock = clock
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.lost_reason: str | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="qing-lease", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            with contextlib.suppress(RuntimeError):
                thread.join(timeout=self._interval + 2.0)
        self._thread = None

    def _run(self) -> None:
        last_success = self._clock()
        warned = False
        while not self._stop.wait(self._interval):
            ok, reason = self._renew_once()
            now = self._clock()
            if ok:
                last_success = now
                warned = False
                continue
            if reason is not None:
                self._declare_lost(reason)
                return
            if not warned:
                _emit(
                    "warning",
                    "qingniao: warning: lease renewal failed; effect unknown — the gateway may "
                    "be momentarily unreachable",
                )
                warned = True
            if now - last_success > self._lease:
                self._declare_lost(
                    "lease renewals have failed for longer than the lease window; "
                    "the registration is considered lost"
                )
                return

    def _renew_once(self) -> tuple[bool, Optional[str]]:
        try:
            with httpx.Client(
                base_url=self._base_url,
                headers={"authorization": f"Bearer {self._token}"},
                timeout=RENEW_TIMEOUT,
                trust_env=False,
            ) as client:
                response = client.post(f"/control/v1/instances/{self._instance_id}/renew")
        except httpx.HTTPError:
            return False, None
        if response.status_code == 200:
            return True, None
        try:
            code = response.json().get("error", {}).get("code", "")
        except ValueError:
            code = ""
        if code in ("instance_not_found", "instance_ended"):
            return False, f"the gateway no longer knows instance {self._instance_id} (restarted or ended)"
        if code == "invalid_admin_token":
            return False, "the gateway control identity changed (restart); the old instance is invalid"
        if code == "instance_expired":
            return False, "the instance lease expired on the gateway side"
        return False, None

    def _declare_lost(self, reason: str) -> None:
        self.lost_reason = reason
        self._on_lost(reason)


def end_instance(base_url: str, control_token: str, instance_id: str) -> None:
    with contextlib.suppress(Exception):
        with httpx.Client(
            base_url=base_url,
            headers={"authorization": f"Bearer {control_token}"},
            timeout=5.0,
            trust_env=False,
        ) as client:
            client.delete(f"/control/v1/instances/{instance_id}")


def watch_first_request(
    base_url: str, control_token: str, instance_id: str, stop: threading.Event
) -> None:
    """Report once when the gateway observes the first attributed request.

    This proves gateway-side request observation only; it explicitly does
    not claim upstream readiness.
    """

    def poll() -> None:
        while not stop.wait(FIRST_REQUEST_POLL):
            try:
                with httpx.Client(
                    base_url=base_url,
                    headers={"authorization": f"Bearer {control_token}"},
                    timeout=5.0,
                    trust_env=False,
                ) as client:
                    response = client.get(
                        "/control/v1/requests", params={"instance_id": instance_id, "limit": 1}
                    )
                if response.status_code == 200 and response.json().get("total", 0) > 0:
                    _emit(
                        "first_request_observed",
                        f"qingniao: gateway observed the first request for instance {instance_id}",
                        instance_id=instance_id,
                    )
                    return
            except Exception:
                continue

    threading.Thread(target=poll, name="qing-first-request", daemon=True).start()


def run_launch(
    *,
    state_dir: Path,
    label: Optional[str],
    model: Optional[str],
    aux_model: Optional[str],
    route_overrides: dict[str, str],
    native_args: list[str],
    claude_bin: str = "claude",
    renew_interval: float = RENEW_INTERVAL,
    lease_seconds: float = LEASE_SECONDS,
    machine_events: bool = False,
) -> int:
    global _MACHINE_EVENTS
    _MACHINE_EVENTS = machine_events
    check_native_args(native_args)
    if label is not None and not label.strip():
        raise LaunchError("label must be a non-empty string", code="invalid_selection")
    for name, value in (("model", model), ("aux-model", aux_model)):
        if value is not None and not value.strip():
            raise LaunchError(f"--{name} must be a non-empty request model string", code="invalid_selection")
    for request_model in route_overrides:
        if not request_model.strip():
            raise LaunchError("--route request models must be non-empty strings", code="invalid_selection")

    base_url, control_token = _endpoint(state_dir)
    instance, token = register_instance(
        base_url,
        control_token,
        label=label,
        model=model,
        aux_model=aux_model,
        route_overrides=route_overrides,
    )
    instance_id = instance["id"]
    label_text = f" (label {label!r})" if label else ""

    # One lifecycle cleanup scope owns everything created after the
    # registration: settings file, child process, keeper/watch threads and
    # signal handlers. Control-flow events exist before any callback can
    # run (L2), child termination is bounded so a SIGTERM-ignoring child
    # can never wedge the launcher (L3), and signal ownership starts
    # before the registered message so a signal observed after that
    # message can never hit default disposition (setup window).
    settings_path: str | None = None
    child: subprocess.Popen | None = None
    keeper: LeaseKeeper | None = None
    stop_requested = threading.Event()
    lost = threading.Event()
    first_request_stop = threading.Event()
    previous_handlers: dict = {}

    def _forward(signum, _frame):
        stop_requested.set()
        if child is not None:
            with contextlib.suppress(OSError, ProcessLookupError):
                child.send_signal(signum)

    for signum in (signal.SIGINT, signal.SIGTERM):
        with contextlib.suppress(ValueError, OSError):
            previous_handlers[signum] = signal.signal(signum, _forward)

    _emit(
        "registered",
        f"qingniao: instance {instance_id} registered{label_text}",
        instance_id=instance_id,
        label=label,
    )

    def on_lost(reason: str) -> None:
        _emit("instance_lost", f"qingniao: instance lost — {reason}", instance_id=instance_id)
        _emit(
            "note",
            "qingniao: ending the child process; start a new 'qing run' to reconnect "
            "(Claude's own --resume can restore the conversation with a fresh identity)",
        )
        lost.set()
        stop_requested.set()

    exit_code = EXIT_OK
    try:
        settings_path = write_settings_file(token, base_url, instance["model"], instance["aux_model"])
        argv = [claude_bin, "--settings", settings_path] + native_args
        child = subprocess.Popen(argv, env=build_child_env())
        _emit("child_started", f"qingniao: {claude_bin} started (pid {child.pid})", pid=child.pid)
        _emit(
            "note",
            "qingniao: gateway readiness is only proven once the gateway observes a request; "
            "no upstream connection is claimed yet",
        )

        keeper = LeaseKeeper(
            base_url=base_url,
            control_token=control_token,
            instance_id=instance_id,
            on_lost=on_lost,
            interval=renew_interval,
            lease_seconds=lease_seconds,
        )
        keeper.start()
        watch_first_request(base_url, control_token, instance_id, first_request_stop)

        grace_deadline: float | None = None
        while True:
            try:
                returncode = child.wait(timeout=0.2)
                break
            except subprocess.TimeoutExpired:
                pass
            except KeyboardInterrupt:
                stop_requested.set()
                with contextlib.suppress(OSError, ProcessLookupError):
                    child.send_signal(signal.SIGINT)
            if stop_requested.is_set():
                if grace_deadline is None:
                    grace_deadline = time.monotonic() + CHILD_GRACE_SECONDS
                    with contextlib.suppress(OSError, ProcessLookupError):
                        child.terminate()
                elif time.monotonic() > grace_deadline:
                    with contextlib.suppress(OSError, ProcessLookupError):
                        child.kill()
        if lost.is_set():
            exit_code = EXIT_GATEWAY_LOST
        elif returncode < 0:
            exit_code = 128 + (-returncode)
        else:
            exit_code = returncode
    except OSError as exc:
        _emit(
            "error",
            f"qingniao: cannot prepare or start the client ({type(exc).__name__}); the instance was ended",
            code="client_start_failed",
            instance_id=instance_id,
        )
        exit_code = EXIT_USAGE
    except Exception as exc:
        _emit(
            "error",
            f"qingniao: launcher setup failed ({type(exc).__name__}); the instance was ended",
            code="launcher_setup_failed",
            instance_id=instance_id,
        )
        exit_code = EXIT_USAGE
    finally:
        for signum, handler in previous_handlers.items():
            with contextlib.suppress(ValueError, OSError):
                signal.signal(signum, handler)
        if child is not None and child.poll() is None:
            with contextlib.suppress(OSError, ProcessLookupError):
                child.terminate()
            try:
                child.wait(timeout=CHILD_GRACE_SECONDS)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError, ProcessLookupError):
                    child.kill()
                with contextlib.suppress(Exception):
                    child.wait()
        first_request_stop.set()
        if keeper is not None:
            keeper.stop()
        end_instance(base_url, control_token, instance_id)
        if settings_path is not None:
            with contextlib.suppress(OSError):
                os.unlink(settings_path)
    return exit_code
