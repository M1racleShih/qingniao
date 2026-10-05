"""Clean-install acceptance inside a minimal Ubuntu 24.04 container:
install from the wheel (done in the image), confirm version consistency,
start the gateway, serve one local synthetic first request, then uninstall
and confirm the CLI is gone while the state-dir user data is preserved.

Runs with the wheel's own virtualenv interpreter, so httpx is available.
Only local fixtures; no real provider is contacted.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import httpx

STATE = "/root/qing-state"
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = 18081
EXPECTED_VERSION = "0.1.0"
UPSTREAM_MODEL = "docker/upstream"

requests_seen: list[dict] = []
results: list[tuple[str, str]] = []


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):  # noqa
        pass

    def do_POST(self):  # noqa
        body = self.rfile.read(int(self.headers.get("Content-Length") or 0))
        payload = json.loads(body)
        requests_seen.append(payload)
        response = {
            "id": "msg_docker_synthetic",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "docker-ok"}],
            "model": payload.get("model", UPSTREAM_MODEL),
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 3, "output_tokens": 1},
        }
        data = json.dumps(response).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def sh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(list(args), capture_output=True, text=True, check=check)


def step(name: str, fn) -> None:
    try:
        fn()
        results.append((name, "PASS"))
        print(f"[PASS] {name}", flush=True)
    except Exception as exc:  # noqa: BLE001
        results.append((name, f"FAIL: {exc}"))
        print(f"[FAIL] {name}: {exc}", flush=True)
        raise


def wait_discovery(path: str, timeout: float = 30.0) -> dict:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data.get("port"), int) and data.get("control_token"):
                return data
        except (OSError, ValueError):
            pass
        time.sleep(0.1)
    raise AssertionError("gateway discovery file never appeared")


def main() -> int:
    server = ThreadingHTTPServer((UPSTREAM_HOST, UPSTREAM_PORT), Upstream)
    threading.Thread(target=server.serve_forever, daemon=True).start()

    def check_version() -> None:
        out = sh("qing", "--version").stdout.strip()
        assert out == EXPECTED_VERSION, f"qing --version = {out!r}, expected {EXPECTED_VERSION!r}"

    def check_first_request() -> None:
        os.makedirs(STATE, exist_ok=True)
        config = {
            "providers": {
                "docker": {
                    "base_url": f"http://{UPSTREAM_HOST}:{UPSTREAM_PORT}",
                    "credential_env": "QING_DOCKER_TOKEN",
                    "auth": "bearer",
                }
            },
            "models": {"m1": {"provider": "docker", "upstream_model": UPSTREAM_MODEL}},
            "defaults": {"model": "req-main", "aux_model": "req-aux", "routes": {"req-main": "m1", "req-aux": "m1"}},
        }
        with open(os.path.join(STATE, "config-in.json"), "w", encoding="utf-8") as handle:
            json.dump(config, handle)

        serve = subprocess.Popen(
            ["qing", "serve", "--state-dir", STATE],
            stdout=open(os.path.join(STATE, "serve.log"), "w"),
            stderr=subprocess.STDOUT,
            text=True,
        )
        try:
            discovery = wait_discovery(os.path.join(STATE, "gateway.json"))
            # Apply the configuration through the documented CLI command.
            config_path = os.path.join(STATE, "config-in.json")
            applied = sh("qing", "config", "apply", config_path, "--state-dir", STATE)
            assert applied.returncode == 0, f"qing config apply: {applied.stdout} {applied.stderr}"
            base = f"http://127.0.0.1:{discovery['port']}"
            with httpx.Client(base_url=base, timeout=15.0, trust_env=False) as client:
                created = client.post(
                    "/control/v1/instances",
                    headers={"authorization": f"Bearer {discovery['control_token']}"},
                    json={"model": "req-main", "aux_model": "req-aux", "routes": {"req-main": "m1", "req-aux": "m1"}},
                )
                assert created.status_code == 201, f"instance: {created.text[:200]}"
                instance_token = created.json()["token"]
                sent = client.post(
                    "/v1/messages",
                    headers={"authorization": f"Bearer {instance_token}"},
                    json={"model": "req-main", "max_tokens": 8, "messages": [{"role": "user", "content": "hi"}]},
                )
                assert sent.status_code == 200, f"request: {sent.status_code} {sent.text[:200]}"
            assert requests_seen, "the synthetic upstream never saw a request"
            assert requests_seen[-1]["model"] == UPSTREAM_MODEL, requests_seen
            # the first request path (and therefore the local flow) worked
            print(f"    upstream saw model: {requests_seen[-1]['model']}", flush=True)
        finally:
            serve.terminate()
            try:
                serve.wait(timeout=10)
            except subprocess.TimeoutExpired:
                serve.kill()

    def check_uninstall_preserves_data() -> None:
        assert os.path.exists(os.path.join(STATE, "config.json")), "state config missing before uninstall"
        # uninstall per the documented command; removes only the tool
        removed = sh("uv", "tool", "uninstall", "qingniao")
        assert removed.returncode == 0, f"uv tool uninstall failed: {removed.stdout} {removed.stderr}"
        # qing must no longer resolve after uninstall (subprocess raises
        # FileNotFoundError when the executable is absent, which is the
        # expected success condition here).
        still_present = False
        try:
            gone = subprocess.run(["qing", "--version"], capture_output=True, text=True, timeout=30)
            still_present = gone.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            still_present = False
        assert not still_present, "qing is still on PATH after uninstall"
        assert not os.path.exists("/root/.local/bin/qing"), "qing entry point still present"
        # user data in the explicit state dir is preserved by default
        assert os.path.exists(os.path.join(STATE, "config.json")), "state config lost on uninstall"
        print(f"    state dir preserved: {sorted(os.listdir(STATE))}", flush=True)

    step("version_consistent", check_version)
    step("first_local_request", check_first_request)
    step("uninstall_preserves_user_data", check_uninstall_preserves_data)

    server.shutdown()
    server.server_close()
    if all(status == "PASS" for _, status in results):
        print("DOCKER_INSTALL_PASS", flush=True)
        return 0
    print("DOCKER_INSTALL_FAIL", flush=True)
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"DOCKER_INSTALL_FAIL: {type(exc).__name__}: {exc}", flush=True)
        sys.exit(1)
