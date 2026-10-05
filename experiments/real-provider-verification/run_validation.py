#!/usr/bin/env python3
"""Real-provider validation: two Claude Code processes through `qing run`
against two authorized real upstreams.

Authorized configurations (no others):
- MiniMax M3  at https://api.minimaxi.com/anthropic   (env MINIMAX_API_KEY)
- glm-5.3-flash at https://open.bigmodel.cn/api/anthropic (env Z_AI_API_KEY; personal GLM plan)

Scenario (single loopback gateway, one shared catalog, same-cwd runs):
1. Preflight: one minimal real message per provider to confirm the exact
   model IDs and the auth-header mode (Bearer). Skippable with
   QING_REAL_SKIP_PREFLIGHT=1 when reusing audited prior evidence.
2. Two persistent stream-json Claude sessions run concurrently through
   the formal `qing run` entry: verify-a (explicit --model m3) and
   verify-b (no override; it inherits the current default). verify-a's
   first main request (A1) is delayed by a local CONNECT hold (timing
   only; TLS content stays opaque and is never synthesized). While A1 is
   held in_progress, the real `qing route set` CLI receives its applied
   ACK; A1 then completes on MiniMax-M3 under the old revision, and the
   tool-result continuation (A2) crosses the route boundary to
   glm-5.3-flash on the new revision, asserted from sanitized native
   stream-json tool facts (Read tool_use matched to its tool_result by
   opaque ID hash, is_error false) plus the reply marker.
3. Still-running verify-b then requests after A's update and again after
   a `qing defaults set` update, and must keep its inherited old-default
   snapshot (MiniMax-M3) across both.
4. A fresh run with no explicit model must inherit the new defaults
   (glm-5.3-flash).
5. A further run with explicit --model m3 must override the new default
   and run on MiniMax-M3.

Budget: the intended cap is 30 provider message requests, checked
between scenario stages (a budget guard, not a hard per-message
guarantee); at most 2 concurrent provider requests are allowed, and the
accepted run observed 6 requests with concurrency 1. Small
output/turn budgets are used where supported, and there are no retries
across providers or models. Provider errors are reported, never worked
around by substitution.

Sanitized output: only ids, labels, model/provider ids, revisions,
outcomes, usage numbers, timings and harmless success markers are kept.
No prompts, replies, tool arguments, session data, credentials or raw
HTTP error bodies are printed or persisted. Absent credentials exit 77.
"""

from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import socket
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

import httpx

from qingniao import state as state_mod

REPO = Path(__file__).resolve().parent.parent.parent
SKIP_EXIT = 77
REQUEST_CAP = 30
HOLD_DEADLINE = 45.0


# Fresh reproductions start with no prior spend. Resumed attempts may pass
# an explicit prior ledger via QING_REAL_PRIOR_LEDGER: either an integer
# count or a path to a JSON object mapping attempt names to counts. (The
# 2026-09-08 acceptance spent 24 message requests cumulatively across all
# documented attempts; that history is preserved in the project docs and
# the acceptance report, never baked into default behavior.)
def _load_prior_ledger() -> tuple[dict, int, str]:
    raw = os.environ.get("QING_REAL_PRIOR_LEDGER", "").strip()
    if not raw:
        return {}, 0, "default: no prior requests assumed"
    if raw.isdigit():
        return {"explicit_prior": int(raw)}, int(raw), "explicit integer ledger"
    try:
        with open(raw, encoding="utf-8") as handle:
            data = json.load(handle)
        attempts = {str(k): int(v) for k, v in data.items()}
        return attempts, sum(attempts.values()), f"explicit ledger file {Path(raw).name}"
    except (OSError, ValueError, TypeError):
        return {}, 0, "invalid QING_REAL_PRIOR_LEDGER ignored (treated as 0)"


PRIOR_ATTEMPT_MESSAGE_REQUESTS, PRIOR_TOTAL, PRIOR_LEDGER_SOURCE = _load_prior_ledger()

# Preflight evidence banked from the prior runs (identical content in
# both run reports; both providers answered a real minimal message).
BANKED_PREFLIGHT = {
    "minimax": {
        "base_url": "https://api.minimaxi.com/anthropic",
        "credential_env": "MINIMAX_API_KEY",
        "model_id": "MiniMax-M3",
        "auth_mode": "bearer",
        "status": 200,
        "echoed_model": "MiniMax-M3",
        "ok": True,
    },
    "zai": {
        "base_url": "https://open.bigmodel.cn/api/anthropic",
        "credential_env": "Z_AI_API_KEY",
        "model_id": "glm-5.3-flash",
        "auth_mode": "bearer",
        "status": 200,
        "echoed_model": "glm-5.3-flash",
        "ok": True,
    },
}

MINIMAX_BASE = "https://api.minimaxi.com/anthropic"
ZAI_BASE = "https://open.bigmodel.cn/api/anthropic"
MINIMAX_MODEL = "MiniMax-M3"
ZAI_MODEL = "glm-5.3-flash"

report: dict = {
    "schema": "qingniao-real-provider-validation/1",
    "criteria": {},
    "event_order": [],
    "instances": [],
    "requests": [],
    "limitations": [],
}
_failures: list[str] = []


def note(event: str, **fields) -> None:
    report["event_order"].append({"t": round(time.time(), 3), "event": event, **fields})


def criterion(name: str, passed: bool, evidence: dict, failure: str | None = None) -> None:
    entry = {"pass": bool(passed), "evidence": evidence}
    report["criteria"][name] = entry
    if not passed:
        # Failure text is reported only when the criterion actually failed.
        entry["failure"] = failure or "unspecified"
        _failures.append(f"{name}: {entry['failure']}")


# ---------------------------------------------------------------- preflight


def preflight() -> dict:
    """One minimal real message per provider; confirm model id + auth mode."""
    out = {}
    with httpx.Client(timeout=90.0, trust_env=False) as client:
        for name, base, env_name, model in (
            ("minimax", MINIMAX_BASE, "MINIMAX_API_KEY", MINIMAX_MODEL),
            ("zai", ZAI_BASE, "Z_AI_API_KEY", ZAI_MODEL),
        ):
            key = os.environ[env_name]
            entry: dict = {"base_url": base, "credential_env": env_name, "model_id": model}
            r = client.post(
                f"{base}/v1/messages",
                headers={"authorization": f"Bearer {key}", "anthropic-version": "2023-06-01"},
                json={
                    "model": model,
                    "max_tokens": 16,
                    "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
                },
            )
            report["request_budget"]["preflight_message_requests"] += 1
            entry["status"] = r.status_code
            if r.status_code == 200:
                payload = r.json()
                entry["auth_mode"] = "bearer"
                entry["echoed_model"] = payload.get("model")
                entry["ok"] = payload.get("model") == model
            else:
                try:
                    api_err = r.json()
                    api_err = api_err.get("error", api_err)
                    entry["error_type"] = api_err.get("type") if isinstance(api_err, dict) else None
                except ValueError:
                    entry["error_type"] = "non-json"
                entry["ok"] = False
            out[name] = entry
            note("preflight", provider=name, ok=entry.get("ok"), status=r.status_code)
    return out


# ------------------------------------------------------------- hold proxy


class HoldState:
    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.hold_consumed = False
        self.hold_started = threading.Event()
        self.release = threading.Event()
        self.events: list[dict] = []


STATE = HoldState()


class ConnectHandler(socketserver.BaseRequestHandler):
    """Timing-only CONNECT middleware: the first api.minimaxi.com tunnel
    is delayed until released. Bytes are relayed verbatim in both
    directions; TLS content is never read, stored or synthesized."""

    def handle(self) -> None:
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = self.request.recv(4096)
            if not chunk:
                return
            data += chunk
        first_line = data.split(b"\r\n", 1)[0].decode("latin-1")
        parts = first_line.split()
        if len(parts) < 2 or not parts[0].upper().startswith("CONNECT"):
            self.request.sendall(b"HTTP/1.1 405 Method Not Allowed\r\n\r\n")
            return
        host, _, port = parts[1].rpartition(":")
        held = False
        started = time.time()
        if host == "api.minimaxi.com":
            with STATE.lock:
                first = not STATE.hold_consumed
                STATE.hold_consumed = True
            if first:
                held = True
                STATE.hold_started.set()
                deadline = time.time() + HOLD_DEADLINE
                while not STATE.release.is_set() and time.time() < deadline:
                    time.sleep(0.05)
        released = time.time()
        try:
            upstream = socket.create_connection((host, int(port)), timeout=20)
        except OSError:
            STATE.events.append({"host": host, "held": held, "error": "connect_failed"})
            self.request.sendall(b"HTTP/1.1 502 Bad Gateway\r\n\r\n")
            return
        self.request.sendall(b"HTTP/1.1 200 Connection established\r\n\r\n")
        STATE.events.append(
            {
                "host": host,
                "held": held,
                "held_seconds": round(released - started, 3) if held else 0.0,
            }
        )
        relay(self.request, upstream)
        try:
            upstream.close()
        except OSError:
            pass


def relay(a: socket.socket, b: socket.socket) -> None:
    def pump(src: socket.socket, dst: socket.socket) -> None:
        try:
            while True:
                chunk = src.recv(65536)
                if not chunk:
                    break
                dst.sendall(chunk)
        except OSError:
            pass
        finally:
            try:
                dst.shutdown(socket.SHUT_WR)
            except OSError:
                pass

    ta = threading.Thread(target=pump, args=(a, b), daemon=True)
    tb = threading.Thread(target=pump, args=(b, a), daemon=True)
    ta.start()
    tb.start()
    ta.join(timeout=600)
    tb.join(timeout=600)


class HoldServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


# ------------------------------------------------------------ environment


ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "SHELL", "USER")
CREDENTIAL_ENVS = ("MINIMAX_API_KEY", "Z_AI_API_KEY")  # personal GLM plan; the team key is no longer authorized


def serve_env(hold_port: int) -> dict:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST or k in CREDENTIAL_ENVS}
    # The gateway's real-upstream client intentionally honors proxy env:
    # MiniMax tunnels through the loopback hold middleware, Zai connects
    # directly. Control traffic is unaffected (CLI uses trust_env=False).
    env["HTTPS_PROXY"] = f"http://127.0.0.1:{hold_port}"
    env["NO_PROXY"] = "open.bigmodel.cn,localhost,127.0.0.1"
    return env


def run_env(tmpdir: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    env["TERM"] = "dumb"
    env["TMPDIR"] = str(tmpdir)
    env.update(
        {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_BUG_COMMAND": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_COST_WARNINGS": "1",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    for name in CREDENTIAL_ENVS:
        env.pop(name, None)  # provider keys stay in the gateway process only
    return env


# ------------------------------------------------------------- cli/control

_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def control_get(port: int, token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"authorization": f"Bearer {token}"},
    )
    with _OPENER.open(request, timeout=10) as resp:
        return json.loads(resp.read())


def qing_cli(env: dict, cwd: Path, *args: str, timeout: float = 60) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "qingniao", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=REPO,
    )


def _session_natives(tmpdir: Path) -> list[str]:
    return [
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        # print-mode stream-json output requires --verbose; without it the
        # claude process exits before sending any request.
        "--verbose",
        "--tools", "Read",
        "--allowedTools", "Read",
        "--strict-mcp-config",
        "--mcp-config", str(tmpdir / "mcp-empty.json"),
    ]


def start_session(env: dict, cwd: Path, workdir: Path, tmpdir: Path, label: str, extra: list[str]) -> tuple[subprocess.Popen, "queue.Queue"]:
    """Formal `qing run` wrapper hosting ONE persistent Claude stream-json
    session. stdin stays open, so later turns reuse the same registered
    instance across route and defaults updates."""
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "qingniao", "run", "--json",
            "--label", label, "--state-dir", str(workdir / "state"),
            *extra, "--", *_session_natives(tmpdir),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=open(workdir / f"run-{label}.stderr", "w"),
        text=True,
        env=env,
        cwd=cwd,
    )
    lines: queue.Queue = queue.Queue()
    threading.Thread(target=_pump_lines, args=(proc, lines), daemon=True).start()
    return proc, lines


def _pump_lines(proc: subprocess.Popen, lines: queue.Queue) -> None:
    try:
        for line in proc.stdout:
            lines.put(line.rstrip("\n"))
    finally:
        lines.put(None)


def send_turn(proc: subprocess.Popen, text: str) -> None:
    """Feed one user turn into the still-open persistent session."""
    if proc.poll() is not None or proc.stdin is None or proc.stdin.closed:
        raise AssertionError("persistent session is not alive when sending a turn")
    proc.stdin.write(
        json.dumps(
            {"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
        )
        + "\n"
    )
    proc.stdin.flush()


def read_result(lines: "queue.Queue", timeout: float) -> tuple[dict, str]:
    """Read stream-json lines until this turn's result event, capturing
    sanitized native tool facts only: the tool_use name and a hash of the
    opaque tool ID, plus matched tool_result ID hashes with is_error.
    Raw arguments, content, prompts and replies are never kept. Returns
    sanitized fields plus the raw result text; callers persist marker
    booleans only, never the text itself."""
    deadline = time.time() + timeout
    tool_use = None
    tool_results: list[dict] = []
    while True:
        remaining = deadline - time.time()
        if remaining <= 0:
            raise TimeoutError("stream-json result event not observed in time")
        line = lines.get(timeout=remaining)
        if line is None:
            raise EOFError("claude session stdout closed before a result event")
        try:
            payload = json.loads(line)
        except ValueError:
            continue
        if not isinstance(payload, dict):
            continue
        kind = payload.get("type")
        if kind == "assistant":
            content = payload.get("message", {}).get("content") if isinstance(payload.get("message"), dict) else None
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_use":
                    tool_use = {
                        "name": block.get("name"),
                        "id_hash": hashlib.sha256(str(block.get("id", "")).encode()).hexdigest()[:12],
                    }
        elif kind == "user":
            message = payload.get("message") if isinstance(payload.get("message"), dict) else {}
            content = message.get("content")
            for block in content if isinstance(content, list) else []:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    # The native tool_result omits is_error on success;
                    # absent normalizes to False.
                    tool_results.append(
                        {
                            "id_hash": hashlib.sha256(str(block.get("tool_use_id", "")).encode()).hexdigest()[:12],
                            "is_error": bool(block.get("is_error", False)),
                        }
                    )
        elif kind == "result":
            return (
                {
                    "is_error": payload.get("is_error"),
                    "num_turns": payload.get("num_turns"),
                    "usage": payload.get("usage"),
                    "tool_use": tool_use,
                    "tool_results": tool_results,
                },
                payload.get("result") or "",
            )


def close_session(proc: subprocess.Popen, timeout: float = 60) -> int:
    """End a persistent session through the native stdin EOF path; the
    wrapper then reaps claude and ends the registration."""
    if proc.stdin and not proc.stdin.closed:
        proc.stdin.close()
    return proc.wait(timeout=timeout)


def qing_run(env: dict, cwd: Path, workdir: Path, tmpdir: Path, state_dir: Path, label: str, extra: list[str], prompt: str, max_turns: int) -> subprocess.Popen:
    """One-shot formal run: a fresh registered instance and a single turn."""
    return subprocess.Popen(
        [
            sys.executable, "-m", "qingniao", "run", "--json",
            "--label", label, "--state-dir", str(state_dir),
            *extra, "--",
            "-p", prompt,
            "--output-format", "json",
            "--max-turns", str(max_turns),
            "--tools", "Read",
            "--allowedTools", "Read",
            "--strict-mcp-config",
            "--mcp-config", str(tmpdir / "mcp-empty.json"),
        ],
        stdout=subprocess.PIPE,
        stderr=open(workdir / f"run-{label}.stderr", "w"),
        text=True,
        env=env,
        cwd=cwd,
    )


def collect_claude_result(proc: subprocess.Popen, timeout: float) -> tuple[dict, str]:
    out, _ = proc.communicate(timeout=timeout)
    parsed: dict = {"returncode": proc.returncode}
    try:
        payload = json.loads(out)
        parsed["is_error"] = payload.get("is_error")
        parsed["num_turns"] = payload.get("num_turns")
        parsed["usage"] = payload.get("usage")
        result_text = payload.get("result") or ""
    except ValueError:
        parsed["is_error"] = "unparseable-stdout"
        parsed["num_turns"] = None
        result_text = ""
    return parsed, result_text


def sanitized_request(rec: dict) -> dict:
    return {
        "id": rec.get("id"),
        "instance_id": rec.get("instance_id"),
        "request_model": rec.get("request_model"),
        "provider_id": rec.get("provider_id"),
        "upstream_model": rec.get("upstream_model"),
        "route_revision": rec.get("route_revision"),
        "outcome": rec.get("outcome"),
        "status_code": rec.get("status_code"),
        "started_at": rec.get("started_at"),
        "finished_at": rec.get("finished_at"),
        "usage": rec.get("usage"),
    }


def wait_for_predicate(poll, predicate, timeout: float, interval: float = 0.2):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            last = poll()
            if predicate(last):
                return last, True
        except Exception:
            pass
        time.sleep(interval)
    return last, False


def snapshot_home() -> dict:
    import stat as stat_mod

    snap = {}
    home = Path.home()
    paths = [home / ".claude.json"]
    claude_dir = home / ".claude"
    if claude_dir.is_dir():
        for root, dirs, files in os.walk(claude_dir):
            if any(x in root for x in ("node_modules", os.path.join("plugins", "cache"))):
                dirs[:] = []
                continue
            for f in files:
                paths.append(Path(root) / f)
    for p in paths:
        try:
            st = p.lstat()
            snap[str(p)] = [st.st_mtime_ns, st.st_size, stat_mod.S_IFMT(st.st_mode)]
        except OSError:
            pass
    return snap


def terminate(proc: subprocess.Popen) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=15)
        except subprocess.TimeoutExpired:
            pass


def main() -> int:
    claude = shutil.which("claude")
    if not claude:
        print("REAL_PROVIDER_SKIPPED: no local claude CLI")
        return SKIP_EXIT
    missing = [name for name in CREDENTIAL_ENVS if not os.environ.get(name)]
    if missing:
        print(f"REAL_PROVIDER_SKIPPED: credential env not available: {sorted(missing)}")
        return SKIP_EXIT

    report["request_budget"] = {
        "cap": REQUEST_CAP,
        "preflight_message_requests": 0,
        "gateway_message_requests": 0,
    }
    version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    report["versions"] = {"python": sys.version.split()[0], "claude": version}
    print(f"claude: {version}")

    workdir = Path(tempfile.mkdtemp(prefix="qingniao-real-provider-"))
    tmpdir = workdir / "tmp"
    tmpdir.mkdir()
    (tmpdir / "mcp-empty.json").write_text(json.dumps({"mcpServers": {}}))
    project = workdir / "project"
    (project / ".claude").mkdir(parents=True)
    (project / "note-a.txt").write_text("QING-REAL-A-OK\nsecond line\n")
    project_settings = project / ".claude" / "settings.json"
    project_settings.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": "http://127.0.0.1:9",
                    "ANTHROPIC_AUTH_TOKEN": "project-decoy-token",
                    "ANTHROPIC_API_KEY": "project-decoy-key",
                    "CLAUDE_CODE_OAUTH_TOKEN": "project-decoy-oauth",
                    "CLAUDE_CODE_USE_VERTEX": "1",
                }
            }
        )
    )
    project_settings_before = project_settings.read_bytes()
    home_before = snapshot_home()

    provider_keys = {name: os.environ[name] for name in CREDENTIAL_ENVS}
    runs: list[subprocess.Popen] = []
    serve: subprocess.Popen | None = None
    server: HoldServer | None = None
    try:
        # --- hold middleware (loopback only, timing-only) ---
        server = HoldServer(("127.0.0.1", 0), ConnectHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        hold_port = server.server_address[1]
        note("hold_proxy_listening", port=hold_port)

        # --- preflight: reuse banked evidence or spend 2 real messages ---
        if os.environ.get("QING_REAL_SKIP_PREFLIGHT") == "1":
            report["providers"] = {
                name: {
                    **data,
                    "evidence_kind": "historical — recorded during the audited 2026-09-08 "
                                     "acceptance runs; status is not a fresh check of the "
                                     "current credentials",
                }
                for name, data in BANKED_PREFLIGHT.items()
            }
            report["providers_source"] = (
                "preflight SKIPPED (QING_REAL_SKIP_PREFLIGHT=1): the statuses above are "
                "historical acceptance evidence, not fresh HTTP checks"
            )
            note("preflight_skipped_reusing_historical_evidence")
        else:
            report["providers"] = preflight()
            if not all(p.get("ok") for p in report["providers"].values()):
                criterion("preflight_named_models", False, report["providers"],
                          "a named provider/model could not be used; no substitution attempted")
                _finish(workdir)
                return 1

        # --- gateway + shared catalog ---
        senv = serve_env(hold_port)
        state_dir = workdir / "state"
        serve = subprocess.Popen(
            [sys.executable, "-m", "qingniao", "serve", "--state-dir", str(state_dir)],
            stdout=open(workdir / "serve.log", "w"),
            stderr=subprocess.STDOUT,
            text=True,
            env=senv,
            cwd=REPO,
        )
        discovery, ok = wait_for_predicate(
            lambda: _read_discovery(state_dir), lambda d: d is not None, 30
        )
        assert ok, "gateway discovery file never appeared"
        port, control_token = discovery["port"], discovery["control_token"]

        config = {
            "providers": {
                "minimax": {"base_url": MINIMAX_BASE, "credential_env": "MINIMAX_API_KEY", "auth": "bearer"},
                "zai": {"base_url": ZAI_BASE, "credential_env": "Z_AI_API_KEY", "auth": "bearer"},
            },
            "models": {
                "mini-m3": {"provider": "minimax", "upstream_model": MINIMAX_MODEL},
                "zai-glm": {"provider": "zai", "upstream_model": ZAI_MODEL},
            },
            "defaults": {"model": "m3", "aux_model": "glm", "routes": {"m3": "mini-m3", "glm": "zai-glm"}},
        }
        config_file = workdir / "config.json"
        config_file.write_text(json.dumps(config))
        cli_env = run_env(tmpdir)
        applied = qing_cli(cli_env, REPO, "config", "apply", str(config_file), "--state-dir", str(state_dir), "--json")
        assert applied.returncode == 0, f"config apply failed: rc={applied.returncode}"
        config_revision = json.loads(applied.stdout).get("revision")
        note("config_applied", revision=config_revision)

        # --- persistent sessions: verify-a and verify-b alive together ---
        renv = run_env(tmpdir)
        renv["CLAUDE_CONFIG_DIR"] = str(workdir / "claude-config")
        proc_a, lines_a = start_session(renv, project, workdir, tmpdir, "verify-a", ["--model", "m3"])
        runs.append(proc_a)
        note("run_a_started")
        reg_a, ok = wait_for_predicate(
            lambda: _instance_summary(port, control_token, "verify-a"), lambda r: r is not None, 60
        )
        assert ok and reg_a, "verify-a never registered with the gateway"
        note("run_a_registered", instance_id=reg_a["id"])

        proc_b, lines_b = start_session(renv, project, workdir, tmpdir, "verify-b", [])
        runs.append(proc_b)
        note("run_b_started", explicit_model=False)
        reg_b, ok = wait_for_predicate(
            lambda: _instance_summary(port, control_token, "verify-b"), lambda r: r is not None, 60
        )
        assert ok and reg_b, "verify-b never registered with the gateway"
        note("run_b_registered", instance_id=reg_b["id"])

        _collect_settings_evidence(tmpdir, provider_keys, want=2)
        liveness_together = {
            "a_alive": proc_a.poll() is None,
            "b_alive": proc_b.poll() is None,
            "distinct_instance_ids": reg_a["id"] != reg_b["id"],
            "concurrent_settings_files": len(report.get("settings_evidence", {})),
        }
        note("sessions_alive_together", **liveness_together)
        assert liveness_together["a_alive"] and liveness_together["b_alive"], \
            "the two persistent sessions are not alive together"

        # --- stage 1: A turn 1 held; route ACK arrives while A1 in flight ---
        send_turn(proc_a, "Use the Read tool to read note-a.txt in the current directory, then reply with exactly the first line of that file.")
        note("a_turn1_sent")
        first_records, ok = wait_for_predicate(
            lambda: _instance_records(port, control_token, "verify-a"),
            lambda recs: bool(recs), 60,
        )
        assert ok, "verify-a produced no gateway request record (claude never reached the gateway)"
        note("a1_admitted", request_id=first_records[0].get("id"))
        hold_seen, _ = wait_for_predicate(lambda: STATE.hold_started.is_set(), lambda x: x, 30)
        assert hold_seen, "A1 was never observed entering the hold middleware"
        note("a1_hold_started")

        in_progress_snap, ok = wait_for_predicate(
            lambda: _instance_inflight(port, control_token, "verify-a"),
            lambda res: res is not None, 30,
        )
        a1_inflight = in_progress_snap
        assert ok and a1_inflight, "no in_progress request for verify-a during the hold"

        # Both persistent sessions must still be alive at the ACK moment.
        liveness_at_ack = {"a_alive": proc_a.poll() is None, "b_alive": proc_b.poll() is None}
        note("liveness_at_route_ack", **liveness_at_ack)

        route_ack = qing_cli(cli_env, REPO, "route", "set", "m3", "zai-glm", "--instance", "verify-a",
                             "--state-dir", str(state_dir), "--json")
        ack_ok = route_ack.returncode == 0
        ack_payload = {}
        if ack_ok:
            ack_payload = json.loads(route_ack.stdout)
        note("route_set_acked", applied=ack_payload.get("applied"), revision=ack_payload.get("revision"))
        assert ack_ok and ack_payload.get("applied") is True, "route set did not return an applied ACK during the hold"

        STATE.release.set()
        note("a1_hold_released")
        a_turn1, a_text = read_result(lines_a, timeout=300)
        note("a_turn1_finished", is_error=a_turn1.get("is_error"), num_turns=a_turn1.get("num_turns"))

        records_a = _instance_records(port, control_token, "verify-a")
        report["requests"].extend(sanitized_request(r) for r in records_a)
        a1 = records_a[0] if records_a else {}
        a2 = records_a[1] if len(records_a) > 1 else {}
        a_cross = (
            a1.get("provider_id") == "minimax" and a1.get("upstream_model") == MINIMAX_MODEL
            and a2.get("provider_id") == "zai" and a2.get("upstream_model") == ZAI_MODEL
            and a1.get("route_revision") is not None and a2.get("route_revision") == a1["route_revision"] + 1
            and all(r.get("outcome") == "success" for r in records_a)
            and all(r.get("instance_id") == reg_a["id"] for r in records_a)
        )
        tool_roundtrip = (
            a_turn1.get("is_error") is False
            and "QING-REAL-A-OK" in a_text
            and len(records_a) >= 2
        )
        native_tool = a_turn1.get("tool_use") or {}
        native_result = next(
            (tr for tr in a_turn1.get("tool_results", []) if tr.get("id_hash") == native_tool.get("id_hash")),
            None,
        )
        tool_facts_ok = (
            bool(native_tool)
            and native_tool.get("name") == "Read"
            and native_result is not None
            and native_result.get("is_error") is False
        )
        criterion("route_update_ack_while_a1_held",
                  ack_ok and bool(a1_inflight) and a_cross and all(liveness_at_ack.values()),
                  {"ack": {"applied": ack_payload.get("applied"), "revision": ack_payload.get("revision")},
                   "a1_in_progress_during_hold": a1_inflight,
                   "liveness_at_ack": liveness_at_ack,
                   "hold_events": STATE.events},
                  "route ACK during held A1, live sessions, or post-hold routing did not hold")
        criterion("cross_model_tool_result_continuation", tool_roundtrip and a_cross and tool_facts_ok,
                  {"path": "genuine Claude Read-tool roundtrip: main tool_use on MiniMax-M3, "
                           "tool-result continuation of the same claude process routed to glm-5.3-flash",
                   "a1": sanitized_request(a1), "a2": sanitized_request(a2),
                   "claude_result_marker": "QING-REAL-A-OK" in a_text,
                   "native_tool_evidence": {
                       "tool_use": native_tool,
                       "tool_result": native_result,
                       "matched_by_id_hash": native_result is not None,
                       "is_error": native_result.get("is_error") if native_result else None,
                   }},
                  "A1/A2 did not cross providers with a successful, natively evidenced tool roundtrip")

        # --- stage 2: B receives its first request only AFTER A's update,
        # while both persistent sessions are still alive ---
        assert proc_a.poll() is None and proc_b.poll() is None, \
            "persistent sessions must still be alive before B's post-update request"
        send_turn(proc_b, "Reply with exactly: B-OK")
        note("b_turn1_sent_after_route_update")
        b1_turn, b1_text = read_result(lines_b, timeout=240)
        note("b_turn1_finished", is_error=b1_turn.get("is_error"))
        records_b = _instance_records(port, control_token, "verify-b")
        report["requests"].extend(sanitized_request(r) for r in records_b)
        b1_unchanged = (
            records_b
            and all(
                r.get("provider_id") == "minimax" and r.get("upstream_model") == MINIMAX_MODEL
                and r.get("route_revision") == a1.get("route_revision")
                and r.get("instance_id") == reg_b["id"]
                for r in records_b
            )
            and b1_turn.get("is_error") is False
            and "B-OK" in b1_text
        )
        criterion("b_live_unchanged_after_route_update", b1_unchanged,
                  {"b_instance_id": reg_b["id"],
                   "explicit_model": False,
                   "inherited_default_at_spawn": "m3",
                   "b_records": [sanitized_request(r) for r in records_b],
                   "sessions_alive_when_sent": proc_a.poll() is None and proc_b.poll() is None},
                  "the still-running verify-b did not stay on MiniMax-M3 after A's update")

        # Budget guard (cumulative across attempts): remaining stages are
        # skipped as failed/incomplete rather than exceeding the cap.
        spent_total = PRIOR_TOTAL + control_get(port, control_token, "/control/v1/requests?limit=1")["total"]
        if spent_total + 2 > REQUEST_CAP:
            for name in ("existing_instance_unchanged_across_defaults_update",
                         "defaults_new_instances_only", "explicit_override_precedence"):
                criterion(name, False, {}, "request budget cap reached before this stage")
            _finish(workdir)
            print("REAL_PROVIDER_VALIDATION_FAIL")
            return 1

        # --- stage 3: defaults update while the existing sessions live ---
        assert proc_a.poll() is None and proc_b.poll() is None, \
            "persistent sessions must stay alive across the defaults update"
        defaults = qing_cli(cli_env, REPO, "defaults", "set", "--model", "glm", "--aux-model", "glm",
                            "--route", "glm=zai-glm", "--route", "m3=mini-m3",
                            "--state-dir", str(state_dir), "--json")
        defaults_ok = defaults.returncode == 0
        defaults_revision = json.loads(defaults.stdout).get("revision") if defaults_ok else None
        note("defaults_set", applied_revision=defaults_revision)
        assert defaults_ok, "defaults set failed"

        # The still-running verify-b asks again: same instance, original
        # snapshot, while the new defaults only affect future instances.
        send_turn(proc_b, "Reply with exactly: B2-OK")
        note("b_turn2_sent_after_defaults_update")
        b2_turn, b2_text = read_result(lines_b, timeout=240)
        note("b_turn2_finished", is_error=b2_turn.get("is_error"))
        records_b_all = _instance_records(port, control_token, "verify-b")
        b2 = records_b_all[-1] if records_b_all else {}
        report["requests"].append(sanitized_request(b2))
        b_across_defaults = (
            b2.get("provider_id") == "minimax" and b2.get("upstream_model") == MINIMAX_MODEL
            and b2.get("route_revision") == a1.get("route_revision")
            and b2.get("instance_id") == reg_b["id"]
            and b2_turn.get("is_error") is False
            and "B2-OK" in b2_text
        )
        criterion("existing_instance_unchanged_across_defaults_update", defaults_ok and b_across_defaults,
                  {"b_instance_id": reg_b["id"], "b_alive_at_defaults_update": proc_b.poll() is None,
                   "explicit_model": False,
                   "inherited_default_at_spawn": "m3",
                   "defaults_revision": defaults_revision, "b2": sanitized_request(b2)},
                  "the live instance did not keep its original snapshot across the defaults update")

        # Close both persistent sessions through the native stdin EOF path.
        a_exit = close_session(proc_a)
        b_exit = close_session(proc_b)
        note("sessions_closed", a_returncode=a_exit, b_returncode=b_exit)
        criterion("concurrent_live_instances",
                  liveness_together["distinct_instance_ids"]
                  and liveness_together["concurrent_settings_files"] >= 2
                  and a_exit == 0 and b_exit == 0,
                  {"liveness_together": liveness_together,
                   "a_instance_id": reg_a["id"], "b_instance_id": reg_b["id"],
                   "a_returncode": a_exit, "b_returncode": b_exit},
                  "two concurrent live same-cwd instances were not proven")

        # --- stage 4: new instance inherits the new defaults ---
        proc_c = qing_run(renv, project, workdir, tmpdir, state_dir, "verify-c", [], "Reply with exactly: C-OK", max_turns=2)
        runs.append(proc_c)
        note("run_c_started")
        _collect_settings_evidence(tmpdir, provider_keys, want=3)
        c_result, c_text = collect_claude_result(proc_c, timeout=240)
        records_c = _instance_records(port, control_token, "verify-c")
        report["requests"].extend(sanitized_request(r) for r in records_c)
        c_new_defaults = (
            records_c
            and all(r.get("provider_id") == "zai" and r.get("upstream_model") == ZAI_MODEL for r in records_c)
            and c_result.get("returncode") == 0 and c_result.get("is_error") is False
            and "C-OK" in c_text
        )
        criterion("defaults_new_instances_only", defaults_ok and c_new_defaults and b_across_defaults,
                  {"defaults_revision": defaults_revision,
                   "c_records": [sanitized_request(r) for r in records_c],
                   "live_b_still_on_original": b2.get("provider_id") == "minimax"},
                  "new instance did not inherit the new defaults or the live instance changed")

        # --- stage 5: explicit override beats the new default ---
        proc_d = qing_run(renv, project, workdir, tmpdir, state_dir, "verify-d", ["--model", "m3"],
                          "Reply with exactly: D-OK", max_turns=2)
        runs.append(proc_d)
        note("run_d_started")
        _collect_settings_evidence(tmpdir, provider_keys, want=4)
        d_result, d_text = collect_claude_result(proc_d, timeout=240)
        records_d = _instance_records(port, control_token, "verify-d")
        report["requests"].extend(sanitized_request(r) for r in records_d)
        d_explicit = (
            records_d
            and all(r.get("provider_id") == "minimax" and r.get("upstream_model") == MINIMAX_MODEL for r in records_d)
            and d_result.get("returncode") == 0 and d_result.get("is_error") is False
            and "D-OK" in d_text
        )
        criterion("explicit_override_precedence", d_explicit,
                  {"d_records": [sanitized_request(r) for r in records_d],
                   "defaults_model_at_spawn": "glm"},
                  "explicit --model m3 did not override the new default glm")

        # --- shared catalog + distinct identity ---
        instances = control_get(port, control_token, "/control/v1/instances?limit=100")["instances"]
        by_label = {i["label"]: i for i in instances}
        distinct = (
            len(by_label) == 4
            and len({by_label[l]["id"] for l in ("verify-a", "verify-b", "verify-c", "verify-d")}) == 4
            and all(by_label[l]["state"] == "ended" for l in by_label)
        )
        settings = report.get("settings_evidence", {})
        identity = (
            distinct
            and len({s["token_sha256"] for s in settings.values()}) == 4
            and all(s["distinct_from_provider_credentials"] and s["mode"] == "0o600" for s in settings.values())
        )
        criterion("shared_catalog_distinct_identity", identity,
                  {"config_revision": config_revision,
                   "instances": [{"label": l, "id": by_label[l]["id"], "state": by_label[l]["state"]}
                                 for l in sorted(by_label)],
                   "settings_evidence": settings},
                  "instances did not share one gateway catalog with distinct 0600 identities")

        # --- cleanup + preservation ---
        leftovers = sorted(tmpdir.glob("qingniao-instance-*.json"))
        home_after = snapshot_home()
        changed_home = [p for p, v in home_before.items() if home_after.get(p) != v]
        changed_home += [p for p in home_after if p not in home_before]
        projects_dir = workdir / "claude-config" / "projects"
        cleanup_ok = (
            not leftovers
            and not changed_home
            and project_settings.read_bytes() == project_settings_before
            and projects_dir.is_dir() and any(projects_dir.iterdir())
        )
        criterion("cleanup_and_preservation", cleanup_ok,
                  {"temp_settings_leftovers": [p.name for p in leftovers],
                   "real_home_changed": changed_home[:5],
                   "project_settings_byte_identical": project_settings.read_bytes() == project_settings_before,
                   "session_history_preserved": projects_dir.is_dir() and any(projects_dir.iterdir())},
                  "cleanup or preservation check failed")

        # --- cumulative budget accounting ---
        total = control_get(port, control_token, "/control/v1/requests?limit=100")["total"]
        budget = report["request_budget"]
        budget["gateway_message_requests"] = total
        budget["prior_attempts"] = PRIOR_ATTEMPT_MESSAGE_REQUESTS
        budget["prior_total"] = PRIOR_TOTAL
        budget["prior_ledger_source"] = PRIOR_LEDGER_SOURCE
        budget["this_run_preflight"] = budget.get("preflight_message_requests", 0)
        budget["this_run_gateway"] = total
        budget["cumulative_total"] = PRIOR_TOTAL + budget["this_run_preflight"] + total
        note("budget", **budget)

        _finish(workdir)
        if _failures:
            print("REAL_PROVIDER_VALIDATION_FAIL")
            for f in _failures:
                print(f"  FAILED: {f}")
            return 1
        print("REAL_PROVIDER_VALIDATION_PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - report and stop, never fabricate
        report["unexpected_error"] = f"{type(exc).__name__}: {exc}"
        report["diagnostics"] = _dump_diagnostics(workdir)
        criterion("harness_completed", False, {}, f"unexpected harness error: {type(exc).__name__}")
        _finish(workdir)
        print(f"HARNESS_ERROR: {type(exc).__name__}: {exc}")
        print("REAL_PROVIDER_VALIDATION_FAIL")
        return 1
    finally:
        for proc in runs:
            terminate(proc)
        terminate(serve)
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(workdir, ignore_errors=True)


def _finish(workdir: Path) -> None:
    report["limitations"] = list(report.get("limitations", [])) + [
        "Only the two named configurations are tested; no broad provider compatibility is implied.",
        "Upstream credential delivery (gateway -> provider Authorization header) is opaque over TLS "
        "and is evidenced by the gateway snapshot/forwarding path, not by provider-side observation.",
        "Concurrent provider load stayed at 1 in-flight request by scenario design (cap 2).",
    ]
    report_path = Path(os.environ.get("QING_REAL_REPORT", "/tmp/qingniao-real-provider-report.json"))
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"report: {report_path}")


def _read_discovery(state_dir: Path):
    try:
        return state_mod.read_discovery(state_dir)
    except Exception:
        return None


def _instance_records(port: int, token: str, label: str) -> list[dict]:
    instances = control_get(port, token, "/control/v1/instances?limit=100")["instances"]
    inst = next((i for i in instances if i.get("label") == label), None)
    if inst is None:
        return []
    payload = control_get(port, token, f"/control/v1/requests?instance_id={inst['id']}&limit=100")
    return payload.get("requests", [])


def _instance_summary(port: int, token: str, label: str) -> dict | None:
    instances = control_get(port, token, "/control/v1/instances?limit=100")["instances"]
    inst = next((i for i in instances if i.get("label") == label), None)
    if inst is None:
        return None
    return {
        "id": inst.get("id"),
        "state": inst.get("state"),
        "model": inst.get("model"),
        "revision": inst.get("revision"),
    }


def _ndjson_events(path: Path) -> list[dict]:
    """Wrapper-originated NDJSON events only; non-JSON lines (native child
    stderr) are never included."""
    events: list[dict] = []
    try:
        for line in path.read_text(errors="replace").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except ValueError:
                continue
            if isinstance(payload, dict) and "event" in payload:
                events.append({k: payload.get(k) for k in ("event", "code", "message")})
    except OSError:
        pass
    return events


def _dump_diagnostics(workdir: Path) -> dict:
    diag: dict = {
        "hold_events": STATE.events,
        "hold_consumed": STATE.hold_consumed,
        "settings_evidence_found": sorted(report.get("settings_evidence", {})),
        "wrapper_events": [],
    }
    for path in sorted(workdir.glob("run-*.stderr")):
        events = _ndjson_events(path)
        if events:
            diag["wrapper_events"].append({"file": path.name, "events": events[-6:]})
    serve_log = workdir / "serve.log"
    if serve_log.exists():
        try:
            diag["serve_log_tail"] = serve_log.read_text(errors="replace")[-1500:]
        except OSError:
            pass
    return diag


def _instance_inflight(port: int, token: str, label: str):
    for rec in _instance_records(port, token, label):
        if rec.get("outcome") == "in_progress":
            return sanitized_request(rec)
    return None


def _collect_settings_evidence(tmpdir: Path, provider_keys: dict, want: int, timeout: float = 90) -> None:
    """Observe this run's own temporary settings files (keyed by file
    name): mode 0600, distinct token hashes, none equal to a provider
    credential. With two persistent sessions alive, `want=2` proves two
    concurrent distinct identities. Sanitized."""
    evidence = report.setdefault("settings_evidence", {})
    deadline = time.time() + timeout
    while time.time() < deadline and len(evidence) < want:
        for path in tmpdir.glob("qingniao-instance-*.json"):
            key = path.name
            if key in evidence:
                continue
            try:
                data = json.loads(path.read_text())["env"]
                token = data["ANTHROPIC_AUTH_TOKEN"]
                token_sha = hashlib.sha256(token.encode()).hexdigest()
                if token_sha in {v["token_sha256"] for v in evidence.values()}:
                    continue
                evidence[key] = {
                    "mode": oct(path.stat().st_mode & 0o777),
                    "token_sha256": token_sha,
                    "distinct_from_provider_credentials": all(
                        token != value for value in provider_keys.values()
                    ),
                }
            except (OSError, ValueError, KeyError):
                continue
        time.sleep(0.1)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
