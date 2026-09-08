#!/usr/bin/env python3
"""Claude Code instance-identity experiment (Bearer carrier, dual instance).

Proves, with real local Claude Code subprocesses against a loopback-only
scripted Anthropic-compatible fixture, that two concurrently running Claude
Code instances sharing the same project cwd are distinguishable at the
gateway on every request, including streamed main-model requests and real
auxiliary-model requests, using a per-child ANTHROPIC_AUTH_TOKEN
(Authorization: Bearer) as the identity carrier.

Chain of evidence per instance:
  1. main-model request (streamed SSE) -> fixture answers with a real
     tool_use for the built-in `Agent` tool (subagent_type=general-purpose,
     run_in_background=false)
  2. Claude Code launches the subagent, which sends a genuine
     auxiliary-model request carrying CLAUDE_CODE_SUBAGENT_MODEL
  3. subagent final text (AUX-DONE) returns as the tool_result
  4. main-model continuation carries the tool_result -> fixture ends with
     CHAIN-OK, which the child prints as its result

The fixture holds the first main-model response of each identity until both
identities have arrived (2 s cap), forcing verifiable overlap and
interleaving of the two concurrent instances.

Isolation and privacy:
- fixture binds 127.0.0.1 only; child env is an explicit allowlist plus
  child-only vars (no inherited credentials can leak), with HTTP(S)_PROXY
  pointed at a dead loopback port and NO_PROXY for the fixture
- Claude state isolated via CLAUDE_CONFIG_DIR under the run's /tmp workdir;
  HOME is inherited, never modified, and verified unchanged afterwards
- telemetry/error-reporting/bug-command/auto-updater disabled via env
- only sanitized facts are persisted: header names, token hashes, model
  names, role sequences, sizes. No prompt/response bodies, no raw child
  stdout/stderr, no session data, no credentials

Usage:
    python3 experiments/claude-instance-identity/run_experiment.py
    python3 experiments/claude-instance-identity/run_experiment.py --launch-precedence

Exit code 0 iff every assertion below passes:
  both children exit 0 with is_error=false and result CHAIN-OK
  each child sent >=1 streamed SSE main-model request
  each child produced >=1 auxiliary-model request
  the auxiliary tool_result carried the marker and no is_error flag
  every /v1/messages request resolved to exactly one expected instance
  the two instances resolved to distinct identities
  first two main-model requests came from different instances (barrier)
  the two request windows overlapped in time
  no token plaintext appears in any persisted artifact
  the invoking user's real HOME Claude state is unchanged
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat as stat_mod
import subprocess
import sys
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAIN_MODEL = "fixture-main-model"
AUX_MODEL = "fixture-haiku-model"
INSTANCE_IDS = ("a", "b")
CHILD_PROMPT = "Fixture probe. Follow the assistant instructions exactly."
AGENT_TOOL_NAME = "Agent"
AGENT_TOOL_INPUT = {
    "subagent_type": "general-purpose",
    "description": "fixture auxiliary-model probe",
    "prompt": "Reply with exactly: AUX-DONE",
    "run_in_background": False,
}
AUX_FINAL_TEXT = "AUX-DONE"
MAIN_CHAIN_OK = "CHAIN-OK"
MAIN_CHAIN_MISS = "CHAIN-MISS"
MESSAGES_RE = re.compile(r"/v1/messages(\?.*)?$")
BARRIER_TIMEOUT_S = 2.0
CHILD_TIMEOUT_S = 420
ENV_ALLOWLIST = ("PATH", "LANG", "LC_ALL", "TERM", "SHELL", "HOME", "USER")
RECORD_KEYS = {
    "ts",
    "ts_end",
    "local_port",
    "method",
    "path",
    "identity_source",
    "identity_value",
    "headers",
    "body_len",
    "body_sha",
    "request",
    "response",
}


def sha_short(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:12]


class LogStore:
    """Thread-safe sink for sanitized request records."""

    def __init__(self, path: str):
        self.path = path
        self.records: list[dict] = []
        self.lock = threading.Lock()
        with open(self.path, "a", encoding="utf-8"):
            pass  # ensure the artifact exists even if no request arrives

    def add(self, record: dict) -> None:
        with self.lock:
            self.records.append(record)
            with open(self.path, "a", encoding="utf-8") as fh:
                fh.write(json.dumps(record, sort_keys=True) + "\n")


class FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # silence stderr access log
        pass

    # --- identity ------------------------------------------------------

    @property
    def store(self) -> LogStore:
        return self.server.store  # type: ignore[attr-defined]

    def _identity(self):
        auth = self.headers.get("authorization") or ""
        if not auth.lower().startswith("bearer "):
            return None
        token = auth[7:].strip()
        table = self.server.tokens  # type: ignore[attr-defined]
        return table.get(sha_short(token))

    def _sanitized_headers(self) -> dict:
        out = {}
        for key in ("authorization", "x-api-key", "anthropic-version", "anthropic-beta", "user-agent"):
            val = self.headers.get(key)
            if val is None:
                continue
            if key == "authorization":
                out[key] = f"Bearer <{sha_short(val)}>"
            elif key == "x-api-key":
                out[key] = f"<{sha_short(val)}>"
            else:
                out[key] = val
        return out

    # --- request facts (never content) ----------------------------------

    @staticmethod
    def _request_facts(body: bytes) -> dict:
        try:
            j = json.loads(body)
        except Exception:
            return {}
        if not isinstance(j, dict):
            return {}
        messages = j.get("messages") or []
        roles = [m.get("role") for m in messages if isinstance(m, dict)]
        has_assistant = "assistant" in roles
        tool_result_is_error = False
        tool_result_has_marker = False
        for m in messages:
            if not isinstance(m, dict) or m.get("role") != "user":
                continue
            content = m.get("content")
            if not isinstance(content, list):
                continue
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    if block.get("is_error"):
                        tool_result_is_error = True
                    if AUX_FINAL_TEXT in json.dumps(block.get("content")):
                        tool_result_has_marker = True
        system = j.get("system")
        if isinstance(system, list):
            system_desc = "list"
        elif isinstance(system, str):
            system_desc = f"str:{len(system)}"
        else:
            system_desc = None
        metadata = j.get("metadata") or {}
        user_id = metadata.get("user_id") if isinstance(metadata, dict) else None
        return {
            "model": j.get("model"),
            "stream": bool(j.get("stream")),
            "n_messages": len(messages),
            "roles": roles,
            "has_assistant": has_assistant,
            "tool_result_is_error": tool_result_is_error,
            "tool_result_carries_aux_marker": tool_result_has_marker,
            "system": system_desc,
            "n_tools": len(j.get("tools") or []),
            "max_tokens": j.get("max_tokens"),
            "has_cache_control": "cache_control" in body.decode("utf-8", "replace"),
            "metadata_user_id_hash": sha_short(user_id) if user_id else None,
        }

    def _record(self, path, body, response, started, ident, ident_source):
        self.store.add(
            {
                "ts": started,
                "ts_end": time.time(),
                "local_port": self.server.server_address[1],
                "method": self.command,
                "path": path,
                "identity_source": ident_source,
                "identity_value": ident,
                "headers": self._sanitized_headers(),
                "body_len": len(body),
                "body_sha": sha_short(body.decode("utf-8", "replace")),
                "request": self._request_facts(body),
                "response": response,
            }
        )

    # --- concurrency barrier ---------------------------------------------

    def _barrier(self, ident) -> None:
        """Hold the first main-model response of each identity until the other
        identity has been observed (or the cap elapses), forcing overlap."""
        if ident is None or not getattr(self.server, "barrier_enabled", True):
            return
        with self.server.gate_lock:  # type: ignore[attr-defined]
            self.server.seen_main.add(ident)  # type: ignore[attr-defined]
        deadline = time.time() + BARRIER_TIMEOUT_S
        while time.time() < deadline:
            with self.server.gate_lock:  # type: ignore[attr-defined]
                if set(INSTANCE_IDS) <= self.server.seen_main:  # type: ignore[attr-defined]
                    return
            time.sleep(0.02)

    # --- routing ---------------------------------------------------------

    def do_POST(self):  # noqa: N802
        started = time.time()
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        body = self.rfile.read(length) if length > 0 else b""
        path = self.path
        if not MESSAGES_RE.search(path):
            payload = {"type": "error", "error": {"type": "not_found_error", "message": "fixture: unknown path"}}
            out = json.dumps(payload).encode()
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            self._record(path, body, {"status": 404, "kind": "not_found"}, started, self._identity(), "authorization-bearer")
            return
        try:
            parsed = json.loads(body)
        except Exception:
            parsed = None
        facts = self._request_facts(body)
        if facts.get("model") == MAIN_MODEL:
            self._barrier(self._identity())
        blocks, stop_reason = self._decide(parsed, facts)
        wants_stream = isinstance(parsed, dict) and bool(parsed.get("stream"))
        model = facts.get("model")
        if wants_stream:
            self._send_sse(model, blocks, stop_reason)
            response = {"status": 200, "kind": "sse", "stop_reason": stop_reason, "transport": "text/event-stream"}
        else:
            self._send_message_json(model, blocks, stop_reason)
            response = {"status": 200, "kind": "json", "stop_reason": stop_reason, "transport": "application/json"}
        self._record(path, body, response, started, self._identity(), "authorization-bearer")

    @staticmethod
    def _decide(parsed, facts):
        if not isinstance(parsed, dict):
            return [{"type": "text", "text": MAIN_CHAIN_MISS}], "end_turn"
        model = facts.get("model")
        if model == AUX_MODEL:
            return [{"type": "text", "text": AUX_FINAL_TEXT}], "end_turn"
        if model == MAIN_MODEL and not facts.get("has_assistant"):
            return (
                [
                    {
                        "type": "tool_use",
                        "id": "toolu_fixture_probe_01",
                        "name": AGENT_TOOL_NAME,
                        "input": dict(AGENT_TOOL_INPUT),
                    }
                ],
                "tool_use",
            )
        marker = MAIN_CHAIN_OK if facts.get("tool_result_carries_aux_marker") else MAIN_CHAIN_MISS
        return [{"type": "text", "text": marker}], "end_turn"

    # --- responses ---------------------------------------------------------

    def _send_message_json(self, model, blocks, stop_reason) -> None:
        payload = {
            "id": f"msg_fixture_{uuid.uuid4().hex[:8]}",
            "type": "message",
            "role": "assistant",
            "model": model or MAIN_MODEL,
            "content": blocks,
            "stop_reason": stop_reason,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }
        out = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(out)))
        self.end_headers()
        self.wfile.write(out)

    def _send_sse(self, model, blocks, stop_reason) -> None:
        message = {
            "id": f"msg_fixture_{uuid.uuid4().hex[:8]}",
            "type": "message",
            "role": "assistant",
            "model": model or MAIN_MODEL,
            "content": [],
            "stop_reason": None,
            "stop_sequence": None,
            "usage": {"input_tokens": 12, "output_tokens": 4},
        }
        events = [("message_start", {"type": "message_start", "message": message})]
        for index, block in enumerate(blocks):
            if block["type"] == "text":
                events.append(
                    (
                        "content_block_start",
                        {"type": "content_block_start", "index": index, "content_block": {"type": "text", "text": ""}},
                    )
                )
                text = block["text"]
                half = max(1, len(text) // 2)
                for chunk in (text[:half], text[half:]):
                    if chunk:
                        events.append(
                            (
                                "content_block_delta",
                                {"type": "content_block_delta", "index": index, "delta": {"type": "text_delta", "text": chunk}},
                            )
                        )
            else:
                events.append(
                    (
                        "content_block_start",
                        {
                            "type": "content_block_start",
                            "index": index,
                            "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}},
                        },
                    )
                )
                events.append(
                    (
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": index,
                            "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])},
                        },
                    )
                )
            events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
        events.append(
            (
                "message_delta",
                {"type": "message_delta", "delta": {"stop_reason": stop_reason, "stop_sequence": None}, "usage": {"output_tokens": 4}},
            )
        )
        events.append(("message_stop", {"type": "message_stop"}))
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")
        self.end_headers()
        for event, data in events:
            self.wfile.write(f"event: {event}\ndata: {json.dumps(data)}\n\n".encode("utf-8"))
            self.wfile.flush()
        self.close_connection = True


# ------------------------------------------------------------------ children


def build_child_env(base_url: str, token: str, cfg: str, tmp: str, transport: bool = True) -> dict:
    env = {k: v for k, v in os.environ.items() if k in ENV_ALLOWLIST}
    env["TERM"] = "dumb"
    env.update(
        {
            "CLAUDE_CONFIG_DIR": cfg,
            "TMPDIR": tmp,
            "CLAUDE_CODE_SUBAGENT_MODEL": AUX_MODEL,
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_BUG_COMMAND": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_COST_WARNINGS": "1",
            # Belt and braces: requests escaping the disable flags above hit a
            # dead loopback proxy. Fixture traffic bypasses via NO_PROXY.
            "HTTP_PROXY": "http://127.0.0.1:9",
            "HTTPS_PROXY": "http://127.0.0.1:9",
            "NO_PROXY": "127.0.0.1,localhost",
        }
    )
    if transport:
        env["ANTHROPIC_BASE_URL"] = base_url
        env["ANTHROPIC_AUTH_TOKEN"] = token
    return env


def child_cmd(
    claude_bin: str,
    mcp_path: str,
    setting_sources_empty: bool = True,
    settings_json: str | None = None,
) -> list:
    cmd = [
        claude_bin,
        "-p",
        CHILD_PROMPT,
        "--model",
        MAIN_MODEL,
        "--tools",
        AGENT_TOOL_NAME,
        "--allowedTools",
        AGENT_TOOL_NAME,
        "--output-format",
        "json",
        "--max-turns",
        "8",
        "--no-session-persistence",
    ]
    if setting_sources_empty:
        cmd += ["--setting-sources", ""]
    cmd += ["--strict-mcp-config", "--mcp-config", mcp_path]
    if settings_json:
        cmd += ["--settings", settings_json]
    return cmd


def run_child(
    claude_bin,
    base_url,
    token,
    project,
    inst_dir,
    transport: bool = True,
    setting_sources_empty: bool = True,
    settings_json: str | None = None,
):
    """Run one real Claude Code child; keep only selected fields."""
    cfg = os.path.join(inst_dir, "cfg")
    tmp = os.path.join(inst_dir, "tmp")
    for d in (cfg, tmp):
        os.makedirs(d, exist_ok=True)
    with open(os.path.join(cfg, ".claude.json"), "w", encoding="utf-8") as fh:
        json.dump({"hasCompletedOnboarding": True, "theme": "dark"}, fh)
    mcp_path = os.path.join(inst_dir, "mcp-empty.json")
    with open(mcp_path, "w", encoding="utf-8") as fh:
        json.dump({"mcpServers": {}}, fh)
    cmd = child_cmd(claude_bin, mcp_path, setting_sources_empty, settings_json)
    env = build_child_env(base_url, token, cfg, tmp, transport=transport)
    started = time.time()
    try:
        proc = subprocess.run(
            cmd,
            cwd=project,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=CHILD_TIMEOUT_S,
        )
        rc, out, err, timed_out = proc.returncode, proc.stdout, proc.stderr, False
    except subprocess.TimeoutExpired as exc:
        rc, out, err, timed_out = None, exc.stdout or b"", exc.stderr or b"", True
    wall = round(time.time() - started, 2)
    selected: dict = {"returncode": rc, "timed_out": timed_out, "wall_seconds": wall}
    try:
        parsed = json.loads(out.decode("utf-8", "replace"))
        if isinstance(parsed, dict):
            selected.update(
                {
                    "is_error": parsed.get("is_error"),
                    "num_turns": parsed.get("num_turns"),
                    "subtype": parsed.get("subtype"),
                    "result_text": parsed.get("result"),
                    "duration_ms_reported": parsed.get("duration_ms"),
                }
            )
    except Exception:
        selected["stdout_parse_error"] = True
    stderr_text = err.decode("utf-8", "replace")
    selected["stderr_unrecognized_model_sources"] = sorted(
        set(re.findall(r'"query_source":"([^"]+)"', stderr_text))
    )
    selected["stderr_bytes"] = len(err)
    return selected


# ------------------------------------------------------------ home guarding


def snapshot_home_claude_state() -> dict:
    """lstat-based snapshot of ~/.claude.json and ~/.claude (symlink-safe)."""
    snap: dict = {}
    home = os.path.expanduser("~")
    candidates = [os.path.join(home, ".claude.json")]
    cd = os.path.join(home, ".claude")
    if os.path.isdir(cd):
        for root, dirs, files in os.walk(cd):
            if any(part in root for part in ("node_modules", os.path.join("plugins", "cache"))):
                dirs[:] = []
                continue
            for f in files:
                candidates.append(os.path.join(root, f))
    for p in candidates:
        try:
            st = os.lstat(p)
            snap[p] = [st.st_mtime_ns, st.st_size, stat_mod.S_IFMT(st.st_mode)]
        except OSError:
            continue
    return snap


def diff_home_claude_state(before: dict) -> list:
    after = snapshot_home_claude_state()
    changed = [p for p, v in before.items() if after.get(p) != v]
    changed += [p + " (new)" for p in after if p not in before]
    return changed


# ----------------------------------------------------------------- analysis


def analyze(records: list[dict], children: dict) -> dict:
    per_instance = {}
    for inst in INSTANCE_IDS:
        mine = [r for r in records if r.get("identity_value") == inst]
        messages_reqs = [r for r in mine if r["method"] == "POST" and MESSAGES_RE.search(r["path"])]
        per_instance[inst] = {
            "requests_total": len(mine),
            "main_stream_sse": len(
                [
                    r
                    for r in messages_reqs
                    if r["request"].get("model") == MAIN_MODEL
                    and r["request"].get("stream")
                    and r["response"].get("transport") == "text/event-stream"
                ]
            ),
            "aux_model_requests": len([r for r in messages_reqs if r["request"].get("model") == AUX_MODEL]),
            "identity_sources": sorted({r["identity_source"] for r in mine}),
            "child": children.get(inst, {}),
            "first_ts": min((r["ts"] for r in mine), default=None),
            "last_ts": max((r["ts_end"] for r in mine), default=None),
        }
    unattributed = [
        r for r in records if r.get("identity_value") is None and r["method"] == "POST" and MESSAGES_RE.search(r["path"])
    ]
    main_by_ts = sorted(
        [r for r in records if r["method"] == "POST" and MESSAGES_RE.search(r["path"]) and r["request"].get("model") == MAIN_MODEL],
        key=lambda r: r["ts"],
    )
    first_two_distinct = (
        len(main_by_ts) >= 2 and main_by_ts[0]["identity_value"] != main_by_ts[1]["identity_value"] and None not in (main_by_ts[0]["identity_value"], main_by_ts[1]["identity_value"])
    )
    ia, ib = per_instance["a"], per_instance["b"]
    overlap = False
    if None not in (ia["first_ts"], ia["last_ts"], ib["first_ts"], ib["last_ts"]):
        overlap = max(ia["first_ts"], ib["first_ts"]) < min(ia["last_ts"], ib["last_ts"])
    child_a, child_b = children.get("a", {}), children.get("b", {})
    asserts = {
        "children_completed": all(c.get("returncode") == 0 and not c.get("timed_out") for c in (child_a, child_b)),
        "children_not_is_error": all(c.get("is_error") is False for c in (child_a, child_b)),
        "chain_marker_observed": all(c.get("result_text") == MAIN_CHAIN_OK for c in (child_a, child_b)),
        "streaming_main_requests": all(per_instance[i]["main_stream_sse"] >= 1 for i in INSTANCE_IDS),
        "aux_model_requests": all(per_instance[i]["aux_model_requests"] >= 1 for i in INSTANCE_IDS),
        "aux_tool_result_not_error": all(
            any(
                r["request"].get("tool_result_carries_aux_marker") and not r["request"].get("tool_result_is_error")
                for r in records
                if r.get("identity_value") == i
            )
            for i in INSTANCE_IDS
        ),
        "identity_unique_and_stable": all(
            per_instance[i]["requests_total"] > 0 and per_instance[i]["identity_sources"] == ["authorization-bearer"]
            for i in INSTANCE_IDS
        ),
        "instances_distinct": {r["identity_value"] for r in records} >= set(INSTANCE_IDS),
        "first_two_main_requests_distinct_identity": first_two_distinct,
        "concurrent_overlap": overlap,
        "no_unattributed_v1_messages": len(unattributed) == 0,
    }
    return {"per_instance": per_instance, "asserts": asserts, "passed": all(asserts.values()), "unattributed": len(unattributed)}


def observations(records: list[dict], children: dict) -> dict:
    user_ids: dict = {}
    for r in records:
        uid = r["request"].get("metadata_user_id_hash")
        if uid:
            user_ids.setdefault(str(r["identity_value"]), set()).add(uid)
    endpoints: dict = {}
    for r in records:
        key = f'{r["method"]} {r["path"].split("?")[0]}'
        endpoints[key] = endpoints.get(key, 0) + 1
    return {
        "endpoints_hit": endpoints,
        "query_string_seen": sorted({r["path"].split("?")[1] for r in records if "?" in r["path"]}),
        "auth_header_shapes": sorted(
            {
                ("bearer" if "authorization" in r["headers"] else "") + ("+x-api-key" if "x-api-key" in r["headers"] else "") or "none"
                for r in records
                if r["method"] == "POST" and MESSAGES_RE.search(r["path"])
            }
        ),
        "anthropic_version": sorted({r["headers"].get("anthropic-version") for r in records if r["headers"].get("anthropic-version")}),
        "anthropic_beta": sorted({r["headers"].get("anthropic-beta") for r in records if r["headers"].get("anthropic-beta")}),
        "user_agent_samples": sorted({r["headers"].get("user-agent") for r in records if r["headers"].get("user-agent")}),
        "metadata_user_id_distinct_per_identity": {k: len(v) for k, v in user_ids.items()},
        "cache_control_seen": any(r["request"].get("has_cache_control") for r in records),
        "stderr_unrecognized_model_sources": sorted(
            {s for c in children.values() for s in c.get("stderr_unrecognized_model_sources", [])}
        ),
        "roles_main_first_request": next(
            (r["request"].get("roles") for r in records if r["request"].get("model") == MAIN_MODEL and not r["request"].get("has_assistant")),
            None,
        ),
        "roles_aux_request": next((r["request"].get("roles") for r in records if r["request"].get("model") == AUX_MODEL), None),
        "n_tools_seen": sorted({r["request"].get("n_tools") for r in records if r["request"].get("n_tools") is not None}),
    }


# ------------------------------------------------------- launch precedence


def analyze_scenario(name: str, target: str, real_records: list[dict], decoy_records: list[dict], child: dict) -> dict:
    stores = {"real": real_records, "decoy": decoy_records}
    winning = stores[target]
    opposing = stores["decoy" if target == "real" else "real"]
    messages_reqs = [r for r in winning if r["method"] == "POST" and MESSAGES_RE.search(r["path"])]
    main_stream = [
        r
        for r in messages_reqs
        if r["request"].get("model") == MAIN_MODEL
        and r["request"].get("stream")
        and r["response"].get("transport") == "text/event-stream"
    ]
    aux = [r for r in messages_reqs if r["request"].get("model") == AUX_MODEL]
    final_marker = any(r["request"].get("tool_result_carries_aux_marker") for r in messages_reqs)
    opposing_hits = [r for r in opposing if r["method"] == "POST" and MESSAGES_RE.search(r["path"])]
    asserts = {
        "child_completed": child.get("returncode") == 0 and not child.get("timed_out"),
        "child_not_is_error": child.get("is_error") is False,
        "chain_marker_observed": child.get("result_text") == MAIN_CHAIN_OK,
        f"{target}_fixture_received_full_chain": bool(main_stream) and bool(aux) and final_marker,
        "opposing_fixture_received_zero_requests": len(opposing_hits) == 0,
        "winning_traffic_used_settings_source_token": bool(messages_reqs)
        and all(r.get("identity_value") == "child" for r in messages_reqs),
        "aux_model_from_process_env": bool(aux) and all(r["request"].get("model") == AUX_MODEL for r in aux),
    }
    return {
        "scenario": name,
        "expected_winner": target,
        "child": child,
        "winning_fixture": {
            "v1_messages": len(messages_reqs),
            "main_stream_sse": len(main_stream),
            "aux_model_requests": len(aux),
            "final_request_carried_aux_marker": final_marker,
            "bearer_hashes_seen": sorted({r["headers"]["authorization"] for r in messages_reqs if "authorization" in r["headers"]}),
            "identity_values": sorted({str(r.get("identity_value")) for r in messages_reqs}),
            "aux_model_names": sorted({r["request"].get("model") for r in aux}),
        },
        "opposing_fixture": {"v1_messages": len(opposing_hits), "requests_total": len(opposing)},
        "asserts": asserts,
        "passed": all(asserts.values()),
    }


def run_launch_precedence(claude_bin: str, workdir: str) -> dict:
    """Launch-precedence probe with normal setting sources enabled and a
    decoy transport (wrong loopback base URL + token) in the project's
    .claude/settings.json env:

    process-env   intended fixture URL/token only in the child process env
    settings-flag intended fixture URL/token only in an explicit --settings
                  JSON env block (no transport in the process env)

    Observed on Claude Code 2.1.251 (asserted here as drift detectors):
    project settings env overrides the child process env for the keys it
    defines, so the process-env scenario's traffic follows the project's
    decoy transport (decoy token included); an explicit --settings env block
    overrides project settings, so the settings-flag scenario's traffic
    follows the intended fixture. The subagent model set in the process env
    (CLAUDE_CODE_SUBAGENT_MODEL) survives in both scenarios.
    """
    real_token = f"fixture-launch-real-{uuid.uuid4().hex[:16]}"
    decoy_token = f"fixture-launch-decoy-{uuid.uuid4().hex[:16]}"
    real_store = LogStore(os.path.join(workdir, "real-fixture-log.jsonl"))
    decoy_store = LogStore(os.path.join(workdir, "decoy-fixture-log.jsonl"))
    servers = []
    for store, tokens in ((real_store, {sha_short(real_token): "child"}), (decoy_store, {sha_short(decoy_token): "child"})):
        srv = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
        srv.daemon_threads = True
        srv.store = store
        srv.tokens = tokens
        srv.gate_lock = threading.Lock()
        srv.seen_main = set()
        srv.barrier_enabled = False
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        servers.append(srv)
    real_port, decoy_port = servers[0].server_address[1], servers[1].server_address[1]
    real_url = f"http://127.0.0.1:{real_port}"
    decoy_url = f"http://127.0.0.1:{decoy_port}"

    scenarios = []
    try:
        specs = {
            "process-env": {
                "target": "decoy",
                "transport": True,
                "settings_json": None,
            },
            "settings-flag": {
                "target": "real",
                "transport": False,
                "settings_json": json.dumps(
                    {"env": {"ANTHROPIC_BASE_URL": real_url, "ANTHROPIC_AUTH_TOKEN": real_token}}
                ),
            },
        }
        for name, spec in specs.items():
            project = os.path.join(workdir, f"project-{name}")
            os.makedirs(os.path.join(project, ".claude"), exist_ok=True)
            with open(os.path.join(project, ".claude", "settings.json"), "w", encoding="utf-8") as fh:
                json.dump({"env": {"ANTHROPIC_BASE_URL": decoy_url, "ANTHROPIC_AUTH_TOKEN": decoy_token}}, fh)
            child = run_child(
                claude_bin,
                real_url,
                real_token,
                project,
                os.path.join(workdir, f"inst-{name}"),
                transport=spec["transport"],
                setting_sources_empty=False,  # normal setting sources stay enabled
                settings_json=spec["settings_json"],
            )
            scenarios.append(analyze_scenario(name, spec["target"], real_store.records, decoy_store.records, child))
            real_store.records.clear()
            decoy_store.records.clear()
    finally:
        for srv in servers:
            srv.shutdown()
            srv.server_close()

    return {
        "real_fixture_url": real_url,
        "decoy_fixture_url": decoy_url,
        "real_token_hash": sha_short(real_token),
        "decoy_token_hash": sha_short(decoy_token),
        "scenarios": scenarios,
        "passed": all(s["passed"] for s in scenarios),
    }


# --------------------------------------------------------------------- main


def claude_version(claude_bin: str) -> str:
    try:
        out = subprocess.run([claude_bin, "--version"], capture_output=True, timeout=60)
        return out.stdout.decode("utf-8", "replace").strip() or out.stderr.decode("utf-8", "replace").strip()
    except Exception as exc:
        return f"<unavailable: {exc}>"


def main() -> int:
    parser = argparse.ArgumentParser(description="Claude Code instance-identity experiment (Bearer carrier)")
    parser.add_argument("--claude-bin", default="claude")
    parser.add_argument(
        "--launch-precedence",
        action="store_true",
        help="run the launch-precedence probe (settings env vs child env/--settings) instead of the dual-instance run",
    )
    args = parser.parse_args()

    run_id = time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    workdir = os.path.join("/tmp", "qingniao-claude-identity", run_id)
    os.makedirs(workdir, exist_ok=True)

    if args.launch_precedence:
        version = claude_version(args.claude_bin)
        print(f"claude: {args.claude_bin} -> {version}")
        print(f"python: {sys.version.split()[0]}  platform: {sys.platform}")
        print(f"workdir: {workdir}")
        print("probe: normal setting sources enabled; project .claude/settings.json carries a decoy base URL + token")
        home_before = snapshot_home_claude_state()
        result = run_launch_precedence(args.claude_bin, workdir)
        home_changed = diff_home_claude_state(home_before)
        result["home_claude_state_changes"] = home_changed
        result["asserts_home_unchanged"] = not home_changed
        result["passed"] = result["passed"] and not home_changed
        with open(os.path.join(workdir, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2)
        print(
            f"real fixture: {result['real_fixture_url']} (token {result['real_token_hash']})  "
            f"decoy fixture: {result['decoy_fixture_url']} (token {result['decoy_token_hash']})"
        )
        for s in result["scenarios"]:
            c = s["child"]
            w = s["winning_fixture"]
            print(
                f"\nscenario {s['scenario']} (expected winner: {s['expected_winner']}): "
                f"rc={c.get('returncode')} is_error={c.get('is_error')} result={c.get('result_text')!r}"
            )
            print(
                f"  winning fixture: v1_messages={w['v1_messages']} "
                f"main_sse={w['main_stream_sse']} aux={w['aux_model_requests']} "
                f"aux_models={w['aux_model_names']} bearers={w['bearer_hashes_seen']} "
                f"identity={w['identity_values']}"
            )
            print(f"  opposing fixture: v1_messages={s['opposing_fixture']['v1_messages']}")
            for name, ok in s["asserts"].items():
                print(f"  {'PASS' if ok else 'FAIL'}  {name}")
        print(f"\nreal HOME Claude state changes: {len(home_changed)}")
        print(f"report: {os.path.join(workdir, 'report.json')}")
        print(f"OVERALL: {'PASS' if result['passed'] else 'FAIL'}")
        return 0 if result["passed"] else 1

    project = os.path.join(workdir, "project")
    os.makedirs(project, exist_ok=True)
    with open(os.path.join(project, "README.md"), "w", encoding="utf-8") as fh:
        fh.write("Shared project cwd for the instance-identity experiment.\n")

    version = claude_version(args.claude_bin)
    print(f"claude: {args.claude_bin} -> {version}")
    print(f"python: {sys.version.split()[0]}  platform: {sys.platform}")
    print(f"workdir: {workdir}")
    print(f"carrier: per-child ANTHROPIC_AUTH_TOKEN (Authorization: Bearer), same fixture, same project cwd")

    tokens = {i: f"fixture-token-{i}-{uuid.uuid4().hex[:16]}" for i in INSTANCE_IDS}
    token_table = {sha_short(tokens[i]): i for i in INSTANCE_IDS}

    store = LogStore(os.path.join(workdir, "fixture-log.jsonl"))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), FixtureHandler)
    srv.daemon_threads = True
    srv.store = store  # type: ignore[attr-defined]
    srv.tokens = token_table  # type: ignore[attr-defined]
    srv.gate_lock = threading.Lock()  # type: ignore[attr-defined]
    srv.seen_main = set()  # type: ignore[attr-defined]
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base_url = f"http://127.0.0.1:{port}"

    home_before = snapshot_home_claude_state()
    children: dict = {}
    threads = []
    start_barrier = threading.Barrier(len(INSTANCE_IDS) + 1)

    def worker(inst):
        start_barrier.wait()
        children[inst] = run_child(args.claude_bin, base_url, tokens[inst], project, os.path.join(workdir, f"inst-{inst}"))

    try:
        for inst in INSTANCE_IDS:
            t = threading.Thread(target=worker, args=(inst,))
            t.start()
            threads.append(t)
        start_barrier.wait()  # release both children simultaneously
        for t in threads:
            t.join(timeout=CHILD_TIMEOUT_S + 120)
    finally:
        srv.shutdown()
        srv.server_close()

    home_changed = diff_home_claude_state(home_before)
    records = store.records
    analysis = analyze(records, children)
    obs = observations(records, children)

    # Privacy: no token plaintext and no raw-body fields in any persisted artifact.
    persisted_texts = [json.dumps(store.records), json.dumps(children)]
    log_text = open(os.path.join(workdir, "fixture-log.jsonl"), encoding="utf-8").read()
    leaked = [t for t in tokens.values() if any(t in text for text in persisted_texts) or t in log_text]
    record_keys_ok = all(set(r.keys()) <= RECORD_KEYS for r in records)
    asserts = dict(analysis["asserts"])
    asserts["no_token_plaintext_in_artifacts"] = not leaked
    asserts["records_schema_contains_no_body_content"] = record_keys_ok
    asserts["home_claude_state_unchanged"] = not home_changed
    analysis["asserts"] = asserts
    analysis["passed"] = all(asserts.values())

    report = {
        "run_id": run_id,
        "claude_version": version,
        "python_version": sys.version.split()[0],
        "platform": sys.platform,
        "command": "python3 experiments/claude-instance-identity/run_experiment.py",
        "child_argv": child_cmd(args.claude_bin, "<mcp-empty.json>"),
        "base_url": base_url,
        "token_hashes": {i: sha_short(tokens[i]) for i in INSTANCE_IDS},
        "children": children,
        "analysis": analysis,
        "observations": obs,
        "home_claude_state_changes": home_changed,
    }
    with open(os.path.join(workdir, "report.json"), "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2)

    print(f"\nfixture: {base_url} (token-hashed identities: {report['token_hashes']})")
    for inst in INSTANCE_IDS:
        c = children.get(inst, {})
        p = analysis["per_instance"][inst]
        print(
            f"instance {inst}: rc={c.get('returncode')} is_error={c.get('is_error')} turns={c.get('num_turns')} "
            f"result={c.get('result_text')!r} requests={p['requests_total']} main_sse={p['main_stream_sse']} "
            f"aux={p['aux_model_requests']} stderr_sources={c.get('stderr_unrecognized_model_sources')}"
        )
    print()
    for name, ok in asserts.items():
        print(f"  {'PASS' if ok else 'FAIL'}  {name}")
    print("\n== observations ==")
    for k, v in obs.items():
        print(f"  {k}: {v}")
    print(f"\nreport: {os.path.join(workdir, 'report.json')}")
    print(f"OVERALL: {'PASS' if analysis['passed'] else 'FAIL'}")
    return 0 if analysis["passed"] else 1


if __name__ == "__main__":
    sys.exit(main())
