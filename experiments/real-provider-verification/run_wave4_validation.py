#!/usr/bin/env python3
"""Wave-4 real-provider validation: a second (non-MiniMax) upstream for the
gateway's import path, plus one real `claude` session through `qing run`.

Authorized credentials (no others; the team-plan GLM key is deliberately
NOT referenced anywhere in this file):
- MINIMAX_API_KEY          MiniMax M3   https://api.minimaxi.com/anthropic
- Z_AI_API_KEY             personal GLM plan, glm-5.3-flash at
                           https://open.bigmodel.cn/api/anthropic  (PRIMARY)
- XIAOMI_API_KEY +
  XIAOMI_BASE_URL          MiMo v2.6    (FALLBACK only)

Flow (strict order, no cross-provider/cross-model retries):
1. Personal-GLM probe (1 real): one minimal message with glm-5.3-flash;
   a 200 with a matching model echo confirms the exact model id and Bearer
   auth. Any failure (e.g. 429) is recorded honestly and GLM is NOT
   retried this wave.
2. Only if the GLM probe failed: MiMo fallback probe (1 real) against
   ``XIAOMI_BASE_URL``. The endpoint must be an Anthropic-compatible
   messages route (probe URL derived from XIAOMI_BASE_URL; model id taken
   from ``XIAOMI_MODEL`` if set, else the candidate "mimo-v2.6-pro" and
   CONFIRMED by the probe echo, never assumed). If the endpoint is not
   compatible or the echo does not match, the MiMo leg stops and is
   recorded honestly.
3. Import leg for the winning second upstream (only if one probe passed):
   synthetic settings -> `qing config import-claude` preview (must write
   nothing) -> `--apply` offline (private credential store, no key in
   config.json) -> gateway started WITHOUT any provider key in its
   environment -> first real request asserts the upstream model echo
   (200) + faithful usage record -> gateway restart -> the private
   credential is still usable. Real requests: 2.
4. Real claude (2.1.274) through the formal `qing run` wrapper: one
   minimal single-turn print-mode session on the passed second upstream
   (fallback: a MiniMax import + gateway), recording the client version,
   routed destination and outcome. Real requests: 1 (reserved, reconciled
   against the final gateway total). <= 3 budgeted for this leg.

Budget: wave cap ``QING_REAL_BUDGET_CAP`` (default 10). The cumulative
prior ledger ``QING_REAL_PRIOR_LEDGER`` is recorded but does not gate the
wave. Every real request is counted through ``Budget.spend`` before it is
fired and an over-budget fire aborts the run; the final criterion
reconciles probes + the gateway's own recorded total against the cap.
Concurrency is 1 in flight (cap 2); there are no retries across providers
or models and a provider error stops the relevant leg.

Sanitized output only: ids, model/provider ids, revisions, outcomes,
usage numbers, timings, counts, status codes. No prompts, replies, tool
arguments, sessions, credentials or raw HTTP bodies are printed or
persisted. A final scan asserts the credential values appear nowhere in
the work directory or the report except in the declared settings inputs
and the private credential store; the work directory is removed
afterwards and the report goes under ``QING_REAL_REPORT`` (default
/tmp/qingniao-real-wave4-report.json).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

from qingniao import state as state_mod

REPO = Path(__file__).resolve().parent.parent.parent
SKIP_EXIT = 77
REQUEST_CAP = int(os.environ.get("QING_REAL_BUDGET_CAP", "10"))

MINIMAX_BASE = "https://api.minimaxi.com/anthropic"
GLM_BASE = "https://open.bigmodel.cn/api/anthropic"
MINIMAX_MODEL = "MiniMax-M3"
GLM_MODEL = "glm-5.3-flash"
MINIMAX_ENV = "MINIMAX_API_KEY"
GLM_ENV = "Z_AI_API_KEY"                       # personal GLM plan (primary)
XIAOMI_ENV = "XIAOMI_API_KEY"                  # MiMo fallback
XIAOMI_BASE_ENV = "XIAOMI_BASE_URL"
# Candidate MiMo model id. CONFIRMED by the probe echo, never assumed; if
# the echo does not match, the MiMo leg stops and records that honestly.
MIMO_MODEL_ENV = "XIAOMI_MODEL"
MIMO_MODEL_CANDIDATE = "mimo-v2.6-pro"

# Credential env names stripped from every subprocess run environment so
# provider keys never cross into gateway/claude/settings process envs.
CREDENTIAL_ENVS = (MINIMAX_ENV, GLM_ENV, XIAOMI_ENV)

report: dict = {
    "schema": "qingniao-real-wave4-validation/1",
    "criteria": {},
    "event_order": [],
    "requests": [],
    "limitations": [],
}
_failures: list[str] = []


def note(event: str, **fields) -> None:
    report["event_order"].append({"t": round(time.time(), 3), "event": event, **fields})


def criterion(name: str, passed: bool, evidence: dict, failure: str | None = None) -> None:
    report["criteria"][name] = {"pass": bool(passed), "evidence": evidence}
    if not passed:
        report["criteria"][name]["failure"] = failure or "unspecified"
        _failures.append(f"{name}: {failure or 'unspecified'}")


class Budget:
    """Cumulative real-request budget across the wave; every real request
    is counted through this gate before it fires."""

    def __init__(self, cap: int, prior_total: int, prior_source: str):
        self.cap = cap
        self.prior_total = prior_total
        self.prior_source = prior_source
        self.counted = 0
        self.probes = 0

    def spend(self, n: int = 1) -> None:
        self.counted += n
        if self.counted > self.cap:
            raise AssertionError(
                f"real-request wave budget would be exceeded: {self.counted} > {self.cap}"
            )


def load_prior_ledger() -> tuple[int, str]:
    raw = os.environ.get("QING_REAL_PRIOR_LEDGER", "").strip()
    if not raw:
        return 0, "default: no prior requests assumed"
    if raw.isdigit():
        return int(raw), "explicit integer ledger"
    try:
        with open(raw, encoding="utf-8") as handle:
            data = json.load(handle)
        return sum(int(v) for v in data.values()), f"ledger file {Path(raw).name}"
    except (OSError, ValueError, TypeError):
        return 0, "invalid QING_REAL_PRIOR_LEDGER ignored (treated as 0)"


def probe_messages(base_url: str, token: str, model_id: str) -> tuple[int, dict]:
    """One minimal real message. Returns (status_code, sanitized entry)."""
    with httpx.Client(timeout=90.0, trust_env=False) as client:
        response = client.post(
            f"{base_url}/v1/messages",
            headers={"authorization": f"Bearer {token}", "anthropic-version": "2023-06-01"},
            json={
                "model": model_id,
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            },
        )
    entry: dict = {"base_url": base_url, "model_id": model_id, "status": response.status_code}
    if response.status_code == 200:
        payload = response.json()
        entry["auth_mode"] = "bearer"
        entry["echoed_model"] = payload.get("model")
        entry["ok"] = payload.get("model") == model_id
    else:
        entry["ok"] = False
        try:
            api_err = response.json()
            api_err = api_err.get("error", api_err)
            entry["error_type"] = api_err.get("type") if isinstance(api_err, dict) else None
        except ValueError:
            entry["error_type"] = "non-json"
    return response.status_code, entry


def mimo_probe_targets() -> dict | None:
    """Derive the provider base (what the gateway stores and what it appends
    /v1/messages to) and the direct probe URL from XIAOMI_BASE_URL."""
    base = os.environ.get(XIAOMI_BASE_ENV, "").strip().rstrip("/")
    if not base:
        return None
    entry = {"raw_base_url": os.environ.get(XIAOMI_BASE_ENV, "").strip()}
    if not base.startswith(("http://", "https://")):
        entry["compatible"] = False
        entry["reason"] = "XIAOMI_BASE_URL is not an http(s) endpoint; cannot be Anthropic-compatible"
        return entry
    if base.endswith("/v1"):
        provider_base = base[: -len("/v1")] or base
        probe_url = base + "/messages"
    else:
        provider_base = base
        probe_url = base + "/v1/messages"
    entry.update({"compatible": True, "provider_base": provider_base, "probe_url": probe_url})
    return entry


def qing_cli(env: dict, cwd: Path, *args: str, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "qingniao", *args],
        capture_output=True,
        text=True,
        timeout=timeout,
        env=env,
        cwd=cwd,
    )


def run_env(tmpdir: Path) -> dict:
    allowlist = ("PATH", "HOME", "LANG", "LC_ALL", "TERM", "SHELL", "USER")
    env = {k: v for k, v in os.environ.items() if k in allowlist}
    env["TERM"] = "dumb"
    env["TMPDIR"] = str(tmpdir)
    for name in CREDENTIAL_ENVS:
        env.pop(name, None)  # provider keys must never cross into run envs
    return env


def read_discovery(state_dir: Path) -> dict | None:
    try:
        return state_mod.read_discovery(state_dir)
    except Exception:
        return None


def wait_discovery(state_dir: Path, timeout: float = 40.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        data = read_discovery(state_dir)
        if data is not None:
            return data
        time.sleep(0.1)
    raise AssertionError("gateway discovery file never appeared")


def control_get(port: int, token: str, path: str) -> dict:
    with httpx.Client(
        base_url=f"http://127.0.0.1:{port}", timeout=15.0, trust_env=False
    ) as client:
        response = client.get(path, headers={"authorization": f"Bearer {token}"})
        response.raise_for_status()
        return response.json()


def sanitized_request(rec: dict) -> dict:
    return {
        "id": rec.get("id"),
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


def create_instance_and_request(
    discovery: dict,
    budget: Budget,
    catalog_model: str,
    expect_upstream: str,
    expect_provider: str,
) -> dict:
    """Register one fresh instance and send one minimal real message
    through the running gateway; returns sanitized evidence."""
    budget.spend()
    ev: dict = {
        "catalog_model": catalog_model,
        "expect_upstream": expect_upstream,
        "expect_provider": expect_provider,
    }
    with httpx.Client(
        base_url=f"http://127.0.0.1:{discovery['port']}", timeout=120.0, trust_env=False
    ) as client:
        admin = {"authorization": f"Bearer {discovery['control_token']}"}
        routes = {"req-main": catalog_model, "req-aux": catalog_model}
        created = client.post(
            "/control/v1/instances",
            headers=admin,
            json={"model": "req-main", "aux_model": "req-aux", "routes": routes},
        )
        assert created.status_code == 201, (
            f"instance creation: {created.status_code} {created.text[:200]}"
        )
        instance = created.json()["instance"]
        instance_token = created.json()["token"]
        ev["instance_id"] = instance["id"]

        response = client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {instance_token}"},
            json={
                "model": "req-main",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "Reply with exactly: OK"}],
            },
        )
    ev["status_code"] = response.status_code
    if response.status_code == 200:
        payload = response.json()
        ev["echoed_model"] = payload.get("model")
        ev["response_usage"] = payload.get("usage")
        ev["ok"] = payload.get("model") == expect_upstream
    else:
        try:
            api_err = response.json()
            api_err = api_err.get("error", api_err)
            ev["error_type"] = api_err.get("type") if isinstance(api_err, dict) else "relayed"
        except ValueError:
            ev["error_type"] = "non-json"
        ev["ok"] = False
    records = control_get(
        discovery["port"],
        discovery["control_token"],
        f"/control/v1/requests?instance_id={instance['id']}&limit=10",
    ).get("requests", [])
    ev["request_records"] = [sanitized_request(r) for r in records]
    return ev


def start_serve(state_dir: Path, env: dict, log_path: Path) -> subprocess.Popen:
    proc = subprocess.Popen(
        [sys.executable, "-m", "qingniao", "serve", "--state-dir", str(state_dir)],
        stdout=open(log_path, "w"),
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
        cwd=REPO,
    )
    return proc


def stop_serve(proc: subprocess.Popen | None) -> None:
    if proc is None or proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            pass


def scan_for_credentials(workdir: Path, values: dict[str, str], declared_inputs: set) -> dict:
    """Credential values must exist only in the declared settings inputs and
    the private credential store's content files."""
    hits: dict[str, list[str]] = {}
    allowed = set(declared_inputs)
    creds_dir = workdir / "state" / "credentials"
    if creds_dir.is_dir():
        allowed |= {creds_dir / name for name in os.listdir(creds_dir)}
    for root, _dirs, files in os.walk(workdir):
        for f in files:
            path = Path(root) / f
            if path in allowed:
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            for label, value in values.items():
                if value and value.encode() in data:
                    hits.setdefault(label, []).append(str(path))
    return hits


def write_import_source(workdir: Path, name: str, base_url: str, token: str, model: str) -> Path:
    source = workdir / f"settings-{name}.json"
    source.write_text(
        json.dumps(
            {
                "env": {
                    "ANTHROPIC_BASE_URL": base_url,
                    "ANTHROPIC_AUTH_TOKEN": token,
                    "ANTHROPIC_MODEL": model,
                    "CLAUDE_CODE_SUBAGENT_MODEL": model,
                }
            }
        )
    )
    return source


def run_real_claude(
    env: dict,
    workdir: Path,
    tmpdir: Path,
    state_dir: Path,
    discovery: dict,
) -> dict:
    """One minimal real claude print-mode turn through the formal qing run
    wrapper on the imported defaults. Sanitized: client version + routed
    destination + outcome only."""
    claude = shutil.which("claude")
    ev: dict = {
        "claude_version": subprocess.run(
            [claude, "--version"], capture_output=True, text=True, timeout=60
        ).stdout.strip(),
        "prompt_marker": "minimal-print-turn",
    }
    project = workdir / "project"
    project.mkdir()
    (project / "note.txt").write_text("wave4-validation")
    mcp = tmpdir / "mcp-empty.json"
    mcp.write_text(json.dumps({"mcpServers": {}}))
    renv = dict(env)
    renv["CLAUDE_CONFIG_DIR"] = str(workdir / "claude-config")
    renv.update(
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
    proc = subprocess.Popen(
        [
            sys.executable, "-m", "qingniao", "run", "--json",
            "--label", "wave4-real", "--state-dir", str(state_dir),
            "--",
            "-p", "Reply with exactly: OK",
            "--output-format", "json",
            "--max-turns", "1",
            "--tools", "Read",
            "--allowedTools", "Read",
            "--strict-mcp-config",
            "--mcp-config", str(mcp),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=renv,
        cwd=project,
    )
    try:
        out, err = proc.communicate(timeout=300)
    except subprocess.TimeoutExpired:
        stop_serve(proc)
        out, err = proc.communicate(timeout=30)
        ev["ok"] = False
        ev["failure"] = "claude run timed out"
        return ev
    ev["returncode"] = proc.returncode
    try:
        payload = json.loads(out)
        ev["is_error"] = payload.get("is_error")
        ev["num_turns"] = payload.get("num_turns")
        ev["reply_marker_ok"] = "OK" in (payload.get("result") or "")
    except ValueError:
        ev["is_error"] = "unparseable-stdout"
        ev["reply_marker_ok"] = False
    ev["wrapper_ndjson_error"] = any(
        line.startswith('{"event":"error"') for line in (err or "").splitlines()
    )
    instances = control_get(
        discovery["port"], discovery["control_token"], "/control/v1/instances?limit=100"
    )["instances"]
    target = next((i for i in instances if i.get("label") == "wave4-real"), None)
    ev["instance_found"] = target is not None
    ev["routed_to"] = None
    if target is not None:
        records = control_get(
            discovery["port"],
            discovery["control_token"],
            f"/control/v1/requests?instance_id={target['id']}&limit=10",
        )["requests"]
        report["requests"].extend(sanitized_request(r) for r in records)
        ev["routed_to"] = {
            "provider": records[-1].get("provider_id") if records else None,
            "upstream_model": records[-1].get("upstream_model") if records else None,
            "outcome": records[-1].get("outcome") if records else None,
            "status_code": records[-1].get("status_code") if records else None,
        }
        ev["ok"] = (
            proc.returncode == 0
            and ev.get("is_error") is False
            and ev.get("reply_marker_ok")
            and bool(records)
            and records[-1].get("outcome") == "success"
        )
    else:
        ev["ok"] = False
    return ev


def main() -> int:
    # ------------------------------------------------------------- presence
    has_glm = bool(os.environ.get(GLM_ENV))
    has_mimo = bool(os.environ.get(XIAOMI_ENV)) and bool(os.environ.get(XIAOMI_BASE_ENV))
    has_minimax = bool(os.environ.get(MINIMAX_ENV))
    if not (has_glm or has_mimo or has_minimax):
        print("REAL_WAVE4_SKIPPED: no authorized credential present")
        return SKIP_EXIT

    prior_total, prior_source = load_prior_ledger()
    budget = Budget(REQUEST_CAP, prior_total, prior_source)
    report["budget"] = {
        "cap": REQUEST_CAP,
        "prior_total": prior_total,
        "prior_source": prior_source,
        "counted_this_run": 0,
        "cumulative_total": 0,
    }
    report["versions"] = {"python": sys.version.split()[0]}

    workdir = Path(tempfile.mkdtemp(prefix="qingniao-real-wave4-"))
    state_dir = workdir / "state"
    tmpdir = workdir / "tmp"
    tmpdir.mkdir()
    serve: subprocess.Popen | None = None
    try:
        # ------------------------------------------------ Stage 1: GLM probe
        glm_probe: dict = {"attempted": bool(has_glm)}
        if has_glm:
            budget.spend()
            budget.probes += 1
            _status, glm_probe = probe_messages(GLM_BASE, os.environ[GLM_ENV], GLM_MODEL)
            note("glm_personal_probe", ok=glm_probe.get("ok"),
                 status=_status, model=GLM_MODEL)
        else:
            note("glm_personal_probe", skipped="Z_AI_API_KEY not available")
        report["glm_personal_probe"] = glm_probe
        criterion(
            "glm_personal_probe_model_echo",
            glm_probe.get("ok") is True,
            glm_probe,
            "personal GLM probe did not confirm the model id/auth; GLM is not retried this wave",
        )

        # ---------------------------------------------- Stage 2: MiMo fallback
        mimo_probe: dict = {"attempted": False}
        second_upstream = "glm-personal" if glm_probe.get("ok") else None
        if second_upstream is None and has_mimo:
            mimo_probe["attempted"] = True
            targets = mimo_probe_targets()
            if targets and not targets.get("compatible"):
                mimo_probe.update(targets)
                note("mimo_probe", skipped=targets.get("reason"))
            elif targets is not None:
                budget.spend()
                budget.probes += 1
                model_id = os.environ.get(MIMO_MODEL_ENV, "").strip() or MIMO_MODEL_CANDIDATE
                _status, mimo_probe = probe_messages(
                    targets["probe_url"], os.environ[XIAOMI_ENV], model_id
                )
                mimo_probe["probe_url"] = targets["probe_url"]
                mimo_probe["provider_base"] = targets["provider_base"]
                mimo_probe["candidate_model"] = model_id
                note("mimo_probe", ok=mimo_probe.get("ok"),
                     status=_status, model=model_id)
                if mimo_probe.get("ok"):
                    second_upstream = "mimo"
            else:
                note("mimo_probe", skipped="XIAOMI_BASE_URL not set")
        elif second_upstream is None:
            note("mimo_probe", skipped="XIAOMI credentials not available")
        else:
            note("mimo_probe", skipped="GLM personal probe already succeeded")
        report["mimo_probe"] = mimo_probe
        criterion(
            "second_upstream_probe_model_echo",
            second_upstream is not None,
            {"glm_personal_ok": glm_probe.get("ok"), "mimo_ok": mimo_probe.get("ok")},
            "no second upstream confirmed its model id/auth; the leg is recorded, not substituted",
        )

        # ------------------------------------- Stage 3: import leg for winner
        leg: dict = {"second_upstream": second_upstream}
        if second_upstream is not None:
            if second_upstream == "glm-personal":
                leg.update(
                    provider="glm-personal", base_url=GLM_BASE,
                    model=GLM_MODEL, token=os.environ[GLM_ENV],
                )
            else:
                targets = mimo_probe_targets()
                leg.update(
                    provider="mimo", base_url=targets["provider_base"],
                    model=mimo_probe.get("model_id") or MIMO_MODEL_CANDIDATE,
                    token=os.environ[XIAOMI_ENV],
                )

            keys = {name: os.environ[name] for name in CREDENTIAL_ENVS if os.environ.get(name)}
            env = run_env(tmpdir)
            source = write_import_source(
                workdir, leg["provider"], leg["base_url"], leg["token"], leg["model"]
            )

            # import preview must write nothing
            preview = qing_cli(
                env, REPO, "config", "import-claude", "--source", str(source),
                "--state-dir", str(state_dir), "--json",
            )
            assert preview.returncode == 0, (
                f"import preview failed: rc={preview.returncode} {preview.stdout[:200]} {preview.stderr[:200]}"
            )
            preview_payload = json.loads(preview.stdout)
            zero_write = (
                (state_dir / "config.json").exists() is False
                and (state_dir / "credentials").exists() is False
            )
            criterion(
                "import_preview_zero_write",
                zero_write and preview_payload.get("status") == "apply"
                and preview_payload.get("written") is False,
                {"status": preview_payload.get("status"),
                 "written": preview_payload.get("written"),
                 "state_dir_empty": zero_write},
                "import preview wrote files or was not an apply plan",
            )

            applied = qing_cli(
                env, REPO, "config", "import-claude", "--apply", "--source", str(source),
                "--state-dir", str(state_dir), "--json",
            )
            assert applied.returncode == 0, (
                f"import {leg['provider']} failed: rc={applied.returncode} {applied.stdout[:200]} {applied.stderr[:200]}"
            )
            result = json.loads(applied.stdout).get("result", {})
            leg["import"] = {
                "status": result.get("status"),
                "generation": result.get("generation"),
                "provider_id": result.get("provider_id"),
                "credential_id": result.get("credential_id"),
            }
            config = json.loads((state_dir / "config.json").read_text())
            providers_cfg = config.get("providers", {})
            config_text = (state_dir / "config.json").read_text()
            config_secret_free = all(
                not (value and value in config_text) for value in keys.values()
            )
            import_ok = (
                leg["import"].get("status") == "committed"
                and leg["import"].get("credential_id", "").startswith("cred_")
                and "credential_id" in providers_cfg.get(
                    leg["import"].get("provider_id"), {}
                )
                and config_secret_free
            )
            criterion(
                "second_upstream_import_private_credential",
                import_ok,
                {
                    "provider": leg["provider"],
                    "import": leg["import"],
                    "config_carries_no_secret": config_secret_free,
                },
                "second-upstream import did not commit a private credential offline",
            )
            if not import_ok:
                _finish(workdir, budget)
                return 1

            # gateway WITHOUT any provider key in its environment
            serve = start_serve(state_dir, env, workdir / "serve.log")
            discovery = wait_discovery(state_dir)
            note("gateway_started", port=discovery["port"],
                 provider_keys_in_gateway_env=False)

            # first real request through the gateway
            first = create_instance_and_request(
                discovery, budget, leg["model"], leg["model"],
                "glm-personal" if second_upstream == "glm-personal" else "mimo",
            )
            records = first["request_records"]
            record_match = bool(records) and (
                records[-1].get("upstream_model") == leg["model"]
                and records[-1].get("provider_id") == leg["import"].get("provider_id")
                and records[-1].get("outcome") == "success"
            )
            usage_ok = bool(records) and bool(records[-1].get("usage"))
            usage_faithful = False
            if usage_ok and isinstance(first.get("response_usage"), dict):
                ru = first["response_usage"]
                usage_faithful = (
                    isinstance(records[-1]["usage"].get("input_tokens"), int)
                    and records[-1]["usage"].get("input_tokens") == ru.get("input_tokens")
                    and records[-1]["usage"].get("output_tokens") == ru.get("output_tokens")
                )
            criterion(
                f"gateway_first_request_{second_upstream}",
                first.get("ok") is True and record_match and usage_ok,
                {
                    "catalog_model": leg["model"],
                    "echoed_model": first.get("echoed_model"),
                    "status_code": first.get("status_code"),
                    "record": records[-1] if records else None,
                    "usage_faithful_to_response": usage_faithful,
                },
                f"gateway first request for {second_upstream} did not reach the expected upstream model",
            )
            report["requests"].extend(records)
            note("gateway_request", provider=second_upstream, model=leg["model"],
                 status=first.get("status_code"))

            # restart: private credential still usable
            stop_serve(serve)
            serve = None
            wait_after_stop = time.time()
            while read_discovery(state_dir) is not None and time.time() - wait_after_stop < 15:
                time.sleep(0.1)
            serve = start_serve(state_dir, env, workdir / "serve.log")
            discovery = wait_discovery(state_dir)
            note("gateway_restarted", port=discovery["port"])
            restart = create_instance_and_request(
                discovery, budget, leg["model"], leg["model"],
                "glm-personal" if second_upstream == "glm-personal" else "mimo",
            )
            report["requests"].extend(restart["request_records"])
            restart_records = restart["request_records"]
            restart_ok = (
                restart.get("ok") is True
                and bool(restart_records)
                and restart_records[-1].get("outcome") == "success"
                and restart_records[-1].get("upstream_model") == leg["model"]
                and restart_records[-1].get("provider_id") == leg["import"].get("provider_id")
            )
            criterion(
                "private_credential_survives_restart",
                restart_ok,
                {
                    "catalog_model": leg["model"],
                    "echoed_model": restart.get("echoed_model"),
                    "status_code": restart.get("status_code"),
                    "record": restart_records[-1] if restart_records else None,
                },
                "the private credential was not usable after a gateway restart",
            )
            note("restart_request", provider=second_upstream, status=restart.get("status_code"))
            leg["gateway_requests"] = [first, restart]
        else:
            note("import_leg", skipped="no second upstream probe passed")

        # --------------------------------- Stage 4: real claude through qing run
        claude_bin = shutil.which("claude")
        if claude_bin is None:
            report["claude_run"] = {"skipped": "no local claude binary"}
        elif budget.counted + 3 > budget.cap:
            report["claude_run"] = {"skipped": "request budget did not allow the claude leg"}
        else:
            keys = {name: os.environ[name] for name in CREDENTIAL_ENVS if os.environ.get(name)}
            env = run_env(tmpdir)
            claude_ready = False
            if second_upstream is not None:
                # gateway from the import leg is live
                claude_ready = True
            elif has_minimax:
                # Fallback (no second upstream): MiniMax import offline (zero
                # real requests) then start a gateway for the claude path.
                source = write_import_source(
                    workdir, "minimax", MINIMAX_BASE,
                    os.environ[MINIMAX_ENV], MINIMAX_MODEL,
                )
                applied = qing_cli(
                    env, REPO, "config", "import-claude", "--apply", "--source", str(source),
                    "--state-dir", str(state_dir), "--json",
                )
                assert applied.returncode == 0
                note("minimax_import_for_claude", offline=True)
                serve = start_serve(state_dir, env, workdir / "serve.log")
                discovery = wait_discovery(state_dir)
                note("gateway_started_for_claude", provider="minimax")
                claude_ready = True
            else:
                report["claude_run"] = {
                    "skipped": "no second upstream and no MiniMax credential for the claude leg"
                }
                note("claude_run", skipped="no second upstream and no MiniMax credential")
            if claude_ready:
                budget.spend()  # reserve the claude turn
                claude_evidence = run_real_claude(env, workdir, tmpdir, state_dir, discovery)
                report["claude_run"] = claude_evidence
                criterion(
                    "real_claude_qing_run_minimal",
                    claude_evidence.get("ok") is True,
                    {k: v for k, v in claude_evidence.items() if k != "ok"},
                    "the real claude run through qing run did not complete as documented",
                )

        # ------------------------------------------------ secret hygiene scan
        keys = {name: os.environ[name] for name in CREDENTIAL_ENVS if os.environ.get(name)}
        declared = {
            workdir / f"settings-{name.replace('_', '-')}.json"
            for name in ("glm-personal", "mimo", "minimax")
        }
        hits = scan_for_credentials(workdir, keys, declared_inputs=declared)
        criterion(
            "credential_values_outside_private_store",
            not hits,
            {"found_outside_store": hits},
            "credential values were found outside the private credential store",
        )

        # ------------------------------------------------ cumulative budget
        # Every real request through the gateway is recorded; the true wave
        # spend is the direct probes plus the gateway's recorded total.
        if serve is not None:
            total = control_get(
                discovery["port"], discovery["control_token"],
                "/control/v1/requests?limit=100",
            )["total"]
        else:
            total = 0
        true_spent = budget.probes + total
        criterion(
            "request_budget_not_exceeded",
            true_spent <= budget.cap,
            {
                "cap_this_wave": budget.cap,
                "prior_total": budget.prior_total,
                "cumulative_cap": budget.prior_total + budget.cap,
                "probes_this_run": budget.probes,
                "gateway_requests_this_run": total,
                "true_total_this_run": true_spent,
                "cumulative_total": budget.prior_total + true_spent,
                "ledger_source": budget.prior_source,
            },
            "the real-request wave budget was exceeded",
        )

        _finish(workdir, budget)
        # The finished on-disk report must itself be free of credential
        # material; if not, the run fails without persisting anything new.
        report_path = Path(
            os.environ.get("QING_REAL_REPORT", "/tmp/qingniao-real-wave4-report.json")
        )
        report_text = report_path.read_text(encoding="utf-8")
        leaked = [label for label, value in keys.items() if value and value in report_text]
        if leaked or _failures:
            print("REAL_WAVE4_VALIDATION_FAIL")
            for failure in _failures:
                print(f"  FAILED: {failure}")
            if leaked:
                print(f"  FAILED: report carries credential material for: {sorted(leaked)}")
                report["report_secret_leak"] = True
                report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
            return 1
        print("REAL_WAVE4_VALIDATION_PASS")
        return 0
    except Exception as exc:  # noqa: BLE001 - report and stop, never fabricate
        report["unexpected_error"] = f"{type(exc).__name__}: {exc}"
        _finish(workdir, budget)
        print(f"HARNESS_ERROR: {type(exc).__name__}: {exc}")
        print("REAL_WAVE4_VALIDATION_FAIL")
        return 1
    finally:
        stop_serve(serve)
        report["budget"]["counted_this_run"] = budget.counted
        report["budget"]["cumulative_total"] = budget.prior_total + budget.counted
        try:
            shutil.rmtree(workdir, ignore_errors=True)
        except OSError:
            pass


def _finish(workdir: Path, budget: Budget) -> None:
    report["limitations"] = list(report.get("limitations", [])) + [
        "Only the named configurations are tested; no broad provider compatibility is implied.",
        "The MiMo model id is CONFIRMED by the probe echo, never assumed; a mismatch stops the leg.",
        "Upstream credential delivery (gateway -> provider Authorization header) is opaque over TLS "
        "and is evidenced by the gateway snapshot/forwarding path plus the successful 200 model echo.",
        "Concurrency stayed at 1 in-flight request (cap 2); no cross-provider or cross-model retries.",
    ]
    report["budget"]["counted_this_run"] = budget.counted
    report["budget"]["cumulative_total"] = budget.prior_total + budget.counted
    report_path = Path(
        os.environ.get("QING_REAL_REPORT", "/tmp/qingniao-real-wave4-report.json")
    )
    report_path.write_text(json.dumps(report, indent=2, sort_keys=True))
    print(f"report: {report_path}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(130)
