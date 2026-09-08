#!/usr/bin/env python3
"""Formal-launcher verification: real Claude Code through `qing run`.

Two same-cwd real `claude` child processes attach to one shared gateway via
the formal `python -m qingniao run` entry, against loopback-only synthetic
upstreams (no real providers). Verifies what the launcher itself must do:
per-run temporary 0600 --settings files (tokens never in argv), distinct
fresh identities per run, the full main SSE -> actual Agent-subagent
auxiliary request -> tool-result continuation attributed to each instance,
conflicting project settings neutralized by the file, and a same-snapshot
in_progress pair of the two instances (the synthetic upstream holds each
instance's initial main request until the gateway shows the pair, so a
sequential run fails), cleanup with owned-descendant collection on exit,
and no modification of the user's real HOME Claude state.

Test-only isolation (never part of the product launcher): allowlisted child
environment, nonessential-traffic disables, a dead loopback proxy with a
loopback NO_PROXY, Agent-only tools, empty strict MCP config, a per-run
TMPDIR so only our own settings files are ever inspected.

Sanitized output only: bearer tokens are hashed before comparison; no
prompts, responses, credentials, session data or raw child stderr are
printed or persisted. Absence of a local claude CLI is a SKIP (exit 77),
never a pass.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
MAIN_REQ = "req-main"
AUX_REQ = "req-aux"
MAIN_UPSTREAM = "fixture-main-lv1"
AUX_UPSTREAM = "fixture-aux-lv1"
AUX_TEXT = "AUX-DONE"
CHAIN_OK = "CHAIN-OK"
SKIP_EXIT = 77
RECORDS: list[dict] = []
ENV_ALLOWLIST = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "SHELL", "USER")


def sha(v: str) -> str:
    return hashlib.sha256(v.encode()).hexdigest()


# E1: the initial main request of each instance is held at the upstream
# until the gateway shows both instances in_progress in one snapshot.
MAIN_HOLD_BARRIER = threading.Event()
MAIN_HOLD_LOCK = threading.Lock()
MAIN_HOLD_LIMIT = 2
MAIN_HOLD_TIMEOUT = 30.0
_holds_issued = 0


def concurrent_instance_pair(records: list[dict], expected_ids: set[str]) -> bool:
    """True only when ONE /control/v1/requests response shows in_progress
    records for two distinct expected instances. Observations from
    different HTTP reads are never combined."""
    active = {
        record.get("instance_id")
        for record in records
        if record.get("outcome") == "in_progress" and record.get("instance_id") in expected_ids
    }
    return len(active) >= 2


def completed_interval_overlap(records: list[dict], expected_ids: set[str]) -> bool:
    """True only when final completed request intervals of two distinct
    expected instances (one snapshot) intersect with strict positive
    overlap; entirely sequential A-then-B spans never qualify."""
    spans: dict[str, list[tuple[float, float]]] = {}
    for record in records:
        instance_id = record.get("instance_id")
        if instance_id not in expected_ids or record.get("finished_at") is None:
            continue
        spans.setdefault(instance_id, []).append(
            (float(record["started_at"]), float(record["finished_at"]))
        )
    if len(expected_ids) != 2 or set(spans) != expected_ids:
        return False
    first, second = sorted(expected_ids)
    return any(
        start_a < end_b and start_b < end_a
        for start_a, end_a in spans[first]
        for start_b, end_b in spans[second]
    )


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(n) if n else b""
        try:
            j = json.loads(body)
        except Exception:
            j = {}
        auth = self.headers.get("authorization") or ""
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else None
        model = j.get("model")
        msgs = j.get("messages") or []
        tr_ok = False
        tr_error = False
        for m in msgs:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            for b in m.get("content") if isinstance(m.get("content"), list) else []:
                if isinstance(b, dict) and b.get("type") == "tool_result":
                    if b.get("is_error"):
                        tr_error = True
                    if AUX_TEXT in json.dumps(b.get("content")):
                        tr_ok = True
        has_asst = any(isinstance(m, dict) and m.get("role") == "assistant" for m in msgs)
        if model == MAIN_UPSTREAM and not has_asst:
            # E1: hold each instance's initial main request until the
            # observer proves the concurrent pair from one snapshot; a
            # barrier timeout fails the request instead of releasing a
            # success stream that could mask a sequential run.
            global _holds_issued
            with MAIN_HOLD_LOCK:
                held = _holds_issued < MAIN_HOLD_LIMIT
                _holds_issued += 1
            if held and not MAIN_HOLD_BARRIER.wait(timeout=MAIN_HOLD_TIMEOUT):
                body = b'{"error":{"type":"barrier_timeout"}}'
                self.send_response(500)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                self.close_connection = True
                return
        if model == AUX_UPSTREAM:
            blocks, stop = [{"type": "text", "text": AUX_TEXT}], "end_turn"
        elif model == MAIN_UPSTREAM and not has_asst:
            blocks, stop = [
                {
                    "type": "tool_use",
                    "id": "toolu_launcher_1",
                    "name": "Agent",
                    "input": {
                        "subagent_type": "general-purpose",
                        "description": "launcher aux probe",
                        "prompt": f"Reply with exactly: {AUX_TEXT}",
                        "run_in_background": False,
                    },
                }
            ], "tool_use"
        else:
            blocks, stop = [{"type": "text", "text": "CHAIN-OK" if tr_ok else "CHAIN-MISS"}], "end_turn"
        RECORDS.append(
            {
                "token_sha": sha(token) if token else None,
                "model": model,
                "path": self.path,
                "tr_ok": tr_ok,
                "tr_error": tr_error,
            }
        )
        message = {
            "id": f"msg_{uuid.uuid4().hex[:8]}",
            "type": "message",
            "role": "assistant",
            "model": model,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 10, "output_tokens": 3},
        }
        events = [("message_start", {"type": "message_start", "message": message})]
        for i, b in enumerate(blocks):
            if b["type"] == "text":
                events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}))
                events.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": b["text"]}}))
            else:
                events.append(("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "tool_use", "id": b["id"], "name": b["name"], "input": {}}}))
                events.append(("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "input_json_delta", "partial_json": json.dumps(b["input"])}}))
            events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
        events.append(("message_delta", {"type": "message_delta", "delta": {"stop_reason": stop, "stop_sequence": None}, "usage": {"output_tokens": 3}}))
        events.append(("message_stop", {"type": "message_stop"}))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for ev, data in events:
            self.wfile.write(f"event: {ev}\ndata: {json.dumps(data)}\n\n".encode())
            self.wfile.flush()
        self.close_connection = True


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


def child_env(workdir: Path, tmpdir: Path) -> dict:
    """Isolated, allowlisted test environment (test-only isolation)."""
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    env["TERM"] = "dumb"
    env["TMPDIR"] = str(tmpdir)
    env["QING_LAUNCH_FIXTURE_TOKEN"] = _FIXTURE_TOKEN
    env["CLAUDE_CONFIG_DIR"] = str(workdir / "claude-config")
    env.update(
        {
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_BUG_COMMAND": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_COST_WARNINGS": "1",
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    return env


_FIXTURE_TOKEN = f"fixture-launch-{uuid.uuid4().hex[:12]}"


# Control traffic stays on loopback and must never follow an ambient
# proxy (E2): an empty ProxyHandler pins this opener to no proxies at
# all, so the control token can never be sent to a proxy endpoint.
_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def control_get(port: int, control_token: str, path: str) -> dict:
    request = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}",
        headers={"authorization": f"Bearer {control_token}"},
    )
    with _OPENER.open(request, timeout=5) as resp:
        return json.loads(resp.read())


def _proc_start_token(pid: int) -> str | None:
    """starttime field of /proc/<pid>/stat, used to detect pid reuse."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as handle:
            fields = handle.read().rsplit(b")", 1)[-1].split()
        return fields[19].decode()
    except (OSError, IndexError, ValueError):
        return None


def _ppid_map() -> dict[int, int]:
    """pid -> ppid for every visible process via /proc (Linux); empty when
    /proc is unavailable."""
    mapping: dict[int, int] = {}
    try:
        entries = os.listdir("/proc")
    except OSError:
        return mapping
    for entry in entries:
        if not entry.isdigit():
            continue
        try:
            with open(f"/proc/{entry}/stat", "rb") as handle:
                ppid = int(handle.read().rsplit(b")", 1)[-1].split()[1])
        except (OSError, IndexError, ValueError):
            continue
        mapping[int(entry)] = ppid
    return mapping


def _child_pids(pid: int) -> list[int]:
    """Direct children of pid; empty when /proc is unavailable."""
    ppid = _ppid_map()
    return [child for child, parent in ppid.items() if parent == pid]


def _descendant_pids(root_pid: int) -> list[int]:
    """Descendants of root_pid discovered via /proc (Linux); empty when
    /proc is unavailable."""
    children: dict[int, list[int]] = {}
    for child, parent in _ppid_map().items():
        children.setdefault(parent, []).append(child)
    found: list[int] = []
    stack = [root_pid]
    while stack:
        current = stack.pop()
        for child in children.get(current, ()):
            found.append(child)
            stack.append(child)
    return found


def _become_child_subreaper() -> bool:
    """Adopt orphaned descendants of force-killed wrappers (Linux prctl
    PR_SET_CHILD_SUBREAPER) so they can be collected with waitpid.
    Returns False when the platform does not support it."""
    try:
        import ctypes

        libc = ctypes.CDLL("libc.so.6", use_errno=True)
        PR_SET_CHILD_SUBREAPER = 36
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (OSError, ImportError, AttributeError):
        return False


def _waitpid_once(pid: int) -> bool:
    """Collect one specific adopted child if it is a zombie. Never waits
    on -1 and never touches pids that are not our children."""
    try:
        waited, _status = os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        return False
    return waited == pid


def _wait_until_gone(pid: int, start_token: str, timeout: float) -> bool:
    """Bounded wait until a pinned pid has left /proc or has been
    collected as an adopted zombie; False while it is still present."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _proc_start_token(pid) != start_token:
            return True
        if _waitpid_once(pid):
            return True
        time.sleep(0.05)
    return _proc_start_token(pid) != start_token or _waitpid_once(pid)


def _reap_owned(pid: int, start_token: str | None, timeout: float = 5.0) -> None:
    """Terminate one owned leftover process and collect it. The pid is
    pinned by its /proc starttime: when /proc evidence is unavailable or
    the pid was reused, this fails closed and never signals. Escalation
    is bounded (SIGTERM, then SIGKILL), and every signal is followed by
    a bounded wait with waitpid collection for subreaper-adopted
    descendants. Never waits on -1."""
    if start_token is None or _proc_start_token(pid) != start_token:
        return
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.kill(pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        if _wait_until_gone(pid, start_token, timeout):
            break
    _waitpid_once(pid)


def _teardown_owned(proc: subprocess.Popen | None, wait_timeout: float = 10.0) -> list[tuple[int, str | None]]:
    """Gracefully stop an owned wrapper, reap it, and reap only the
    descendants recorded while the wrapper was still alive (E3). Returns
    the owned identities it handled so cleanup can be verified. Never
    kills system or user processes outside the experiment's own tree."""
    if proc is None:
        return []
    owned = [(pid, _proc_start_token(pid)) for pid in _descendant_pids(proc.pid)]
    owned.append((proc.pid, _proc_start_token(proc.pid)))
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=wait_timeout)
        except subprocess.TimeoutExpired:
            proc.kill()
            try:
                proc.wait(timeout=wait_timeout)
            except subprocess.TimeoutExpired:
                pass
    for pid, token in owned:
        if pid == proc.pid:
            continue  # reaped above via Popen.wait
        _reap_owned(pid, token)
    return owned


def _leftover_pids(handled: list[tuple[int, str | None]]) -> list[int]:
    """Owned pids still present in /proc with their recorded identity
    (alive or unreaped zombie) after cleanup."""
    leftovers = []
    for pid, token in handled:
        if token is None:
            continue  # identity was never verifiable; nothing to claim
        if _proc_start_token(pid) == token:
            leftovers.append(pid)
    return leftovers


def _collect_adopted_children(known_spawn_pids: set[int], rounds: int = 5) -> list[tuple[int, str | None]]:
    """Reap this process's adopted children that are not its direct
    spawns. Child-subreaper mode means only descendants of the
    experiment's own wrappers can ever be adopted here, so this sweep
    stays inside experiment-owned processes. Each round snapshots every
    adopted root's full descendant identities before terminating it and
    then reaps the recorded tree — including descendants newly adopted
    when their parent dies — while the bounded round loop drains
    anything a previous round's deaths adopted afterwards. Never signals
    a pid whose identity cannot be verified, and never waits on -1.
    Returns handled identities."""
    handled: list[tuple[int, str | None]] = []
    for _ in range(rounds):
        roots = [pid for pid in _child_pids(os.getpid()) if pid not in known_spawn_pids]
        if not roots:
            break
        for root in roots:
            token = _proc_start_token(root)
            subtree: list[tuple[int, str | None]] = []
            if token is not None:
                subtree = [(pid, _proc_start_token(pid)) for pid in _descendant_pids(root)]
            _reap_owned(root, token)
            handled.append((root, token))
            for pid, member_token in subtree:
                if member_token is not None and _proc_start_token(pid) == member_token:
                    _reap_owned(pid, member_token)
                handled.append((pid, member_token))
    return handled


def main() -> int:
    claude = shutil.which("claude")
    if not claude:
        print("LAUNCHER_VERIFICATION_SKIPPED: no local claude CLI on PATH")
        return SKIP_EXIT
    version = subprocess.run([claude, "--version"], capture_output=True, text=True, timeout=60).stdout.strip()
    print(f"claude: {version}")

    # E2 regression condition: control traffic must stay loopback even
    # under a hostile ambient proxy. Point this process at a dead proxy
    # and drop every no_proxy exemption, so a proxy-honoring control
    # client would fail its very first call. Child processes receive
    # their own explicit allowlisted environment and are unaffected.
    for var in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        os.environ[var] = "http://127.0.0.1:9"
    for var in ("NO_PROXY", "no_proxy"):
        os.environ.pop(var, None)

    # E3: adopt orphans of force-killed wrappers so cleanup can collect
    # them with waitpid instead of leaving live or zombie leftovers.
    # Without this guarantee the experiment must not claim verified
    # owned-process cleanup at all.
    if not _become_child_subreaper():
        print(
            "LAUNCHER_VERIFICATION_SKIPPED: child-subreaper mode unavailable; "
            "owned-descendant cleanup cannot be verified on this platform"
        )
        return SKIP_EXIT

    workdir = Path(tempfile.mkdtemp(prefix="qingniao-launcher-verify-"))
    tmpdir = workdir / "tmp"
    tmpdir.mkdir()
    upstream = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    upstream.daemon_threads = True
    threading.Thread(target=upstream.serve_forever, daemon=True).start()
    upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}"

    state_dir = workdir / "state"
    project = workdir / "project"
    (project / ".claude").mkdir(parents=True)
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
                    "CLAUDE_CODE_USE_FOUNDRY": "1",
                }
            }
        )
    )
    project_settings_before = project_settings.read_bytes()
    mcp_empty = workdir / "mcp-empty.json"
    mcp_empty.write_text(json.dumps({"mcpServers": {}}))

    config = {
        "providers": {
            "fixture": {
                "base_url": upstream_url,
                "credential_env": "QING_LAUNCH_FIXTURE_TOKEN",
                "auth": "bearer",
            }
        },
        "models": {
            "main": {"provider": "fixture", "upstream_model": MAIN_UPSTREAM},
            "aux": {"provider": "fixture", "upstream_model": AUX_UPSTREAM},
        },
        "defaults": {"model": MAIN_REQ, "aux_model": AUX_REQ, "routes": {MAIN_REQ: "main", AUX_REQ: "aux"}},
    }
    config_file = workdir / "config.json"
    config_file.write_text(json.dumps(config))

    env = child_env(workdir, tmpdir)
    home_before = snapshot_home()
    runs: list[subprocess.Popen] = []
    serve: subprocess.Popen | None = None
    try:
        serve = subprocess.Popen(
            [sys.executable, "-m", "qingniao", "serve", "--state-dir", str(state_dir)],
            stdout=open(workdir / "serve.log", "w"),
            stderr=subprocess.STDOUT,
            text=True,
            env=env,
            cwd=REPO,
        )
        deadline = time.time() + 20
        discovery = None
        while time.time() < deadline:
            p = state_dir / "gateway.json"
            if p.exists():
                try:
                    discovery = json.loads(p.read_text())
                    break
                except ValueError:
                    pass
            time.sleep(0.05)
        assert discovery, "gateway discovery file never appeared"

        # E2: this call only succeeds because control_get ignores the
        # hostile ambient proxy configured above.
        control_get(discovery["port"], discovery["control_token"], "/control/v1/config")

        with subprocess.Popen(
            [sys.executable, "-m", "qingniao", "config", "apply", str(config_file), "--state-dir", str(state_dir)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, env=env, cwd=REPO,
        ) as applier:
            assert applier.wait(timeout=30) == 0

        native_args = [
            "-p", "Fixture probe. Follow the assistant instructions exactly.",
            "--output-format", "json",
            "--tools", "Agent",
            "--allowedTools", "Agent",
            "--strict-mcp-config",
            "--mcp-config", str(mcp_empty),
        ]
        for name in ("a", "b"):
            runs.append(
                subprocess.Popen(
                    [
                        sys.executable, "-m", "qingniao", "run",
                        "--label", f"verify-{name}",
                        "--state-dir", str(state_dir),
                        "--", *native_args,
                    ],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                    env=env,
                    cwd=project,
                )
            )

        # Observe only our own per-experiment TMPDIR settings files.
        observed_modes = set()
        token_hashes = set()
        # E1 evidence of real cross-instance concurrency: ONE global
        # /control/v1/requests snapshot must show in_progress records for
        # two distinct expected instance ids (the upstream holds the
        # initial main requests until then), or a single snapshot's final
        # completed intervals must strictly overlap. Sequential A-then-B
        # observations from different reads are never combined.
        instance_ids: dict[str, str] = {}
        overlap_signal: str | None = None
        overlap_observed = False
        deadline = time.time() + 120
        while time.time() < deadline and any(p.poll() is None for p in runs):
            for tmp in tmpdir.glob("qingniao-instance-*.json"):
                try:
                    observed_modes.add(oct(tmp.stat().st_mode & 0o777))
                    data = json.loads(tmp.read_text())["env"]
                    token_hashes.add(sha(data["ANTHROPIC_AUTH_TOKEN"]))
                    assert data["ANTHROPIC_BASE_URL"] == f"http://127.0.0.1:{discovery['port']}"
                    assert data["ANTHROPIC_MODEL"] == MAIN_REQ
                    assert data["CLAUDE_CODE_SUBAGENT_MODEL"] == AUX_REQ
                    assert data["ANTHROPIC_API_KEY"] == ""
                    assert data["CLAUDE_CODE_USE_VERTEX"] == "0"
                    assert data["CLAUDE_CODE_USE_FOUNDRY"] == "0"
                    assert data["CLAUDE_CODE_OAUTH_TOKEN"] == ""
                except (OSError, ValueError, KeyError):
                    pass
            if not overlap_observed:
                try:
                    if len(instance_ids) < 2:
                        for inst in control_get(
                            discovery["port"], discovery["control_token"], "/control/v1/instances?limit=100"
                        )["instances"]:
                            if inst.get("label") in ("verify-a", "verify-b"):
                                instance_ids[inst["label"]] = inst["id"]
                    if len(instance_ids) == 2:
                        expected = set(instance_ids.values())
                        records = control_get(
                            discovery["port"], discovery["control_token"], "/control/v1/requests?limit=100"
                        )["requests"]
                        if concurrent_instance_pair(records, expected):
                            overlap_signal = "same-snapshot in_progress pair"
                        elif completed_interval_overlap(records, expected):
                            overlap_signal = "completed-interval overlap"
                        if overlap_signal is not None:
                            overlap_observed = True
                            MAIN_HOLD_BARRIER.set()
                except Exception:
                    pass
            if len(RECORDS) >= 6 and overlap_observed:
                break
            time.sleep(0.05)

        results = []
        for proc in runs:
            out, err = proc.communicate(timeout=180)
            results.append((proc.returncode, out, err))
        assert overlap_observed, (
            "no verified request overlap between verify-a and verify-b: no single "
            "/control/v1/requests snapshot showed in_progress records for both expected "
            "instances and no single snapshot's completed intervals strictly overlapped; "
            "an entirely sequential A-then-B run is rejected"
        )
        assert observed_modes == {"0o600"}, f"settings file modes: {sorted(observed_modes)}"
        assert len(token_hashes) == 2, f"expected 2 distinct instance tokens, saw {len(token_hashes)}"
        for rc, out, _err in results:
            assert rc == 0, f"qing run exited {rc}"
            parsed = json.loads(out)
            assert parsed.get("is_error") is False, "claude reported an error result"
            assert parsed.get("result") == CHAIN_OK, f"claude result: {parsed.get('result')!r}"
        leftovers = list(tmpdir.glob("qingniao-instance-*.json"))
        assert not leftovers, f"temporary settings files not cleaned: {leftovers}"

        upstream_credentials = {r["token_sha"] for r in RECORDS}
        assert upstream_credentials == {sha(_FIXTURE_TOKEN)}, "upstream must see exactly the provider credential"
        finals = [r for r in RECORDS if r["model"] == MAIN_UPSTREAM and r["tr_ok"] and not r["tr_error"]]
        assert len(finals) == 2, f"expected one clean tool-result continuation per instance, saw {len(finals)}"
        assert sum(1 for r in RECORDS if r["model"] == AUX_UPSTREAM) == 2
        assert not any(r["tr_error"] for r in RECORDS), "an error tool_result was observed"

        instances = control_get(discovery["port"], discovery["control_token"], "/control/v1/instances?limit=100")["instances"]
        assert len(instances) == 2 and all(i["state"] == "ended" for i in instances)
        assert sorted(i["label"] for i in instances) == ["verify-a", "verify-b"]
        for inst in instances:
            records = control_get(
                discovery["port"], discovery["control_token"], f"/control/v1/requests?instance_id={inst['id']}&limit=100"
            )["requests"]
            assert len(records) >= 3
            models = [r["request_model"] for r in records]
            assert models.count(AUX_REQ) == 1 and models[-1] == MAIN_REQ, models
            assert all(r["outcome"] == "success" for r in records), [r["outcome"] for r in records]

        home_after = snapshot_home()
        changed = [p for p, v in home_before.items() if home_after.get(p) != v]
        changed += [p for p in home_after if p not in home_before]
        assert not changed, f"real HOME Claude state changed: {changed[:5]}"
        projects_dir = (workdir / "claude-config" / "projects")
        assert projects_dir.is_dir() and any(projects_dir.iterdir()), "no session history preserved"
        assert (workdir / "claude-config" / ".claude.json").exists()
        assert project_settings.read_bytes() == project_settings_before, "project settings bytes changed"

        print(f"claude: {version}")
        print(f"upstream requests: {len(RECORDS)}; provider credential uniform: yes")
        print("per-instance gateway chain: main(tool_use) -> aux -> tool_result(not-error) success")
        print(f"settings files: modes {sorted(observed_modes)}, distinct tokens {len(token_hashes)}, cleaned: yes")
        print(f"concurrent overlap observed: yes ({overlap_signal}); instances ended 2/2; real HOME unchanged; project settings byte-identical")
        print("LAUNCHER_VERIFICATION_PASS")
        return 0
    finally:
        handled: list[tuple[int, str | None]] = []
        for proc in runs:
            handled.extend(_teardown_owned(proc))
        # The gateway is fully reaped before its state directory is
        # removed, so it never writes into a deleted directory.
        handled.extend(_teardown_owned(serve))
        upstream.shutdown()
        upstream.server_close()
        shutil.rmtree(workdir, ignore_errors=True)
        # Wrappers that died before teardown leave their descendants
        # adopted by this process (child subreaper): collect exactly
        # those experiment-owned children too.
        known_spawn_pids = {proc.pid for proc in runs}
        if serve is not None:
            known_spawn_pids.add(serve.pid)
        handled.extend(_collect_adopted_children(known_spawn_pids))
        # E3 verification: no owned live or zombie process may survive.
        leftovers = _leftover_pids(handled)
        if leftovers:
            print(f"owned-process cleanup left behind: {leftovers}", file=sys.stderr)
            if sys.exc_info()[0] is None:
                raise AssertionError(f"owned-process cleanup left live/zombie processes: {leftovers}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except AssertionError:
        import traceback

        traceback.print_exc()
        print(
            "SANITIZED SUMMARY:",
            json.dumps(
                {
                    "records": len(RECORDS),
                    "models": sorted({r["model"] for r in RECORDS}),
                    "tr_ok": sum(1 for r in RECORDS if r["tr_ok"]),
                    "tr_error": sum(1 for r in RECORDS if r["tr_error"]),
                }
            ),
        )
        sys.exit(1)
