"""Real-TCP streaming acceptance: snapshot integrity across a route switch,
cancellation cleanup before headers and mid-stream, and upstream aborts.

Ordering is proven by observed barriers (upstream request arrival, first
downstream chunk, control-plane record state), never by fixed sleeps.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import httpx
import pytest

from qingniao import state as state_mod
from tests.conftest import _UpstreamHandler, make_config_dict, sse_event
from tests.test_tcp_smoke import _wait_discovery
from http.server import ThreadingHTTPServer


def _start_upstream():
    server = ThreadingHTTPServer(("127.0.0.1", 0), _UpstreamHandler)
    server.daemon_threads = True
    server.requests = []
    server.responses = []
    server.write_errors = []
    server.eof_observed = []
    server.gate_timeouts = []
    server.gate_waiters = 0
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


def _start_serve(state_dir: Path, log_path: Path, env_extra: dict):
    env = dict(os.environ)
    for var in ("QING_P1", "QING_P2", "QING_Q"):
        env.pop(var, None)
    env.update(env_extra)
    log = open(log_path, "w")
    proc = subprocess.Popen(
        [sys.executable, "-m", "qingniao", "serve", "--state-dir", str(state_dir)],
        stdout=log,
        stderr=subprocess.STDOUT,
        text=True,
        env=env,
    )
    return proc, log


def _stop(proc, log):
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    log.close()


async def _wait_for(predicate, timeout=8.0, what="condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.01)
    raise TimeoutError(f"timed out waiting for {what}")


class GatewayHandle:
    def __init__(self, base_url: str, control_token: str):
        self.base = base_url
        self.admin = {"authorization": f"Bearer {control_token}"}

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(base_url=self.base, timeout=15.0, trust_env=False)

    async def put_config(self, client, config):
        r = await client.put("/control/v1/config", headers=self.admin, json=config)
        assert r.status_code == 200, r.text
        return r.json()

    async def create_instance(self, client, **kwargs):
        r = await client.post("/control/v1/instances", headers=self.admin, json=kwargs)
        assert r.status_code == 201, r.text
        body = r.json()
        return body["instance"], body["token"]

    async def status(self, client, instance_id):
        r = await client.get(f"/control/v1/instances/{instance_id}", headers=self.admin)
        assert r.status_code == 200, r.text
        return r.json()["instance"]

    async def records(self, client, instance_id):
        r = await client.get(
            "/control/v1/requests", headers=self.admin, params={"instance_id": instance_id, "limit": 100}
        )
        assert r.status_code == 200, r.text
        return r.json()["requests"]

    async def set_route(self, client, instance_id, request_model, model, expected_revision):
        r = await client.put(
            f"/control/v1/instances/{instance_id}/routes",
            headers=self.admin,
            json={"request_model": request_model, "model": model, "expected_revision": expected_revision},
        )
        return r


def _config_v1(p1_url: str, q_url: str) -> dict:
    return {
        "providers": {
            "prov-p": {"base_url": p1_url, "credential_env": "QING_P1", "auth": "bearer"},
            "prov-q": {"base_url": q_url, "credential_env": "QING_Q", "auth": "bearer"},
        },
        "models": {
            "model-p": {"provider": "prov-p", "upstream_model": "vendor/p"},
            "model-q": {"provider": "prov-q", "upstream_model": "vendor/q"},
            "model-x": {"provider": "prov-q", "upstream_model": "vendor/x"},
        },
        "defaults": {
            "model": "req-main",
            "aux_model": "req-aux",
            "routes": {"req-main": "model-p", "req-aux": "model-p"},
        },
    }


A1_SCRIPT = (
    sse_event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}})
    + sse_event("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "part1"}})
    + sse_event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 9}})
    + sse_event("message_stop", {"type": "message_stop"})
)
SIMPLE_SCRIPT = (
    sse_event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 4, "output_tokens": 2}}})
    + sse_event("message_stop", {"type": "message_stop"})
)
STREAM_BODY = {"model": "req-main", "stream": True, "messages": [{"role": "user", "content": "probe"}]}


def _headers(token: str) -> dict:
    return {"authorization": f"Bearer {token}", "anthropic-version": "2023-06-01", "anthropic-beta": "claude-code-20250219"}


async def _collect_request(client, token: str, request_model: str) -> bytes:
    chunks = []
    async with client.stream(
        "POST",
        "/v1/messages",
        headers=_headers(token),
        json={"model": request_model, "stream": True, "messages": []},
    ) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
    return b"".join(chunks)


async def _collect_stream(client, url, token):
    chunks = []
    async with client.stream("POST", url, headers=_headers(token), json=STREAM_BODY) as response:
        response.raise_for_status()
        async for chunk in response.aiter_bytes():
            chunks.append(chunk)
    return b"".join(chunks)


async def test_a1_a2_b1_snapshot_integrity_across_catalog_edit_and_route_switch(tmp_path):
    p1, q, p2 = _start_upstream(), _start_upstream(), _start_upstream()
    p1.sse = [A1_SCRIPT[:120], A1_SCRIPT[120:]]
    gate = threading.Event()
    # A1 is p1's FIRST request and only that request is held; B1 (p1's
    # second request) runs unblocked on the same upstream.
    p1.request_chunk_gates = {0: [None, gate]}
    q.sse = [SIMPLE_SCRIPT]
    p2.sse = [SIMPLE_SCRIPT]

    state_dir = tmp_path / "state"
    proc, log = _start_serve(
        state_dir,
        tmp_path / "serve.log",
        {"QING_P1": "old-secret-p1", "QING_P2": "new-secret-p2", "QING_Q": "secret-q"},
    )
    a1 = None
    try:
        discovery = await asyncio.to_thread(_wait_discovery, state_dir)
        gw = GatewayHandle(f"http://127.0.0.1:{discovery['port']}", discovery["control_token"])
        async with gw.client() as client:
            await gw.put_config(client, _config_v1(_url(p1), _url(q)))
            instance_a, token_a = await gw.create_instance(client, label="a")
            instance_b, token_b = await gw.create_instance(client, label="b")
            instance_d, token_d = await gw.create_instance(
                client, label="d", routes={"req-x": "model-x"}
            )

            # A1 admitted and in progress on the OLD P endpoint; the hold is
            # observed, not assumed (the fixture handler is parked on the
            # gate for A1's second chunk).
            a1 = asyncio.create_task(_collect_stream(client, "/v1/messages?beta=true", token_a))
            await _wait_for(lambda: len(p1.requests) == 1, what="A1 reaching upstream P1")
            await _wait_for(lambda: p1.gate_waiters >= 1, what="A1 held on the fixture gate")
            await _wait_for(
                lambda: any(r["outcome"] == "in_progress" for r in _sync_records(gw, instance_a["id"])),
                what="A1 record in_progress",
            )

            # Shared catalog edit: endpoint, credential env, auth style AND upstream model all change.
            v2 = _config_v1(_url(p2), _url(q))
            v2["providers"]["prov-p"] = {
                "base_url": _url(p2),
                "credential_env": "QING_P2",
                "auth": "x-api-key",
            }
            v2["models"]["model-p"] = {"provider": "prov-p", "upstream_model": "vendor/p2-new"}
            await gw.put_config(client, v2)

            status_a = await gw.status(client, instance_a["id"])
            snap_a = status_a["routes"]["req-main"]
            assert snap_a["base_url"] == _url(p1)
            assert (snap_a["credential_env"], snap_a["auth"], snap_a["upstream_model"]) == ("QING_P1", "bearer", "vendor/p")
            assert snap_a["catalog_present"] is False
            status_b = await gw.status(client, instance_b["id"])
            assert status_b["routes"]["req-main"]["base_url"] == _url(p1)
            assert status_b["routes"]["req-main"]["catalog_present"] is False

            # A NEW instance resolves against the NEW catalog.
            instance_c, token_c = await gw.create_instance(client, label="c")
            snap_c = (await gw.status(client, instance_c["id"]))["routes"]["req-main"]
            assert (snap_c["base_url"], snap_c["credential_env"], snap_c["auth"], snap_c["upstream_model"]) == (
                _url(p2), "QING_P2", "x-api-key", "vendor/p2-new",
            )
            assert snap_c["catalog_present"] is True

            # Explicit switch A -> Q with a real applied acknowledgement.
            r = await gw.set_route(client, instance_a["id"], "req-main", "model-q", 0)
            assert r.status_code == 200
            assert r.json()["applied"] is True and r.json()["revision"] == 1

            # A2 uses the NEW route; B1 and D1 keep their OLD snapshots —
            # all while A1 is still held in progress on the old endpoint.
            a2 = await _collect_stream(client, "/v1/messages?beta=true", token_a)
            assert a2 == SIMPLE_SCRIPT
            b1 = await _collect_stream(client, "/v1/messages", token_b)
            assert b1 == A1_SCRIPT  # same scripted P stream; B1 is unblocked
            assert p1.gate_waiters >= 1, "B1 finished while A1 was not held on its gate"
            assert not a1.done(), "B1 finishing must not release A1"
            d1 = await _collect_request(client, token_d, "req-x")
            assert d1 == SIMPLE_SCRIPT

            # Deleting a catalog entry leaves old snapshots usable and new
            # selections rejected.
            v3 = _config_v1(_url(p2), _url(q))
            v3["providers"]["prov-p"] = {
                "base_url": _url(p2),
                "credential_env": "QING_P2",
                "auth": "x-api-key",
            }
            v3["models"]["model-p"] = {"provider": "prov-p", "upstream_model": "vendor/p2-new"}
            v3["models"].pop("model-x")
            await gw.put_config(client, v3)
            status_d = await gw.status(client, instance_d["id"])
            assert status_d["routes"]["req-x"]["upstream_model"] == "vendor/x"
            assert status_d["routes"]["req-x"]["catalog_present"] is False
            d2 = await _collect_request(client, token_d, "req-x")
            assert d2 == SIMPLE_SCRIPT

            r = await client.post(
                "/control/v1/instances",
                headers=gw.admin,
                json={"label": "rejected", "routes": {"req-x": "model-x"}},
            )
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_selection"
            r = await gw.set_route(client, instance_b["id"], "req-main", "model-x", 0)
            assert r.status_code == 400
            assert (await gw.status(client, instance_b["id"]))["revision"] == 0

            # A1 has been held throughout A2/B1/D1/D2 and every catalog
            # change above: still in progress, still un-finished.
            assert not a1.done(), "A1 must still be held while later requests complete"
            records_a_mid = await gw.records(client, instance_a["id"])
            assert any(r["outcome"] == "in_progress" for r in records_a_mid)
            assert p1.gate_timeouts == [], "a fixture hold timed out instead of being released explicitly"

            # A1 (already received) completes on the OLD snapshot afterwards.
            gate.set()
            body = await asyncio.wait_for(a1, timeout=10)
            assert body == A1_SCRIPT

            # C resolves to the edited catalog entry.
            c1 = await _collect_stream(client, "/v1/messages", token_c)
            assert c1 == SIMPLE_SCRIPT

            assert len(p1.requests) == 2
            assert p1.requests[0]["headers"]["authorization"] == "Bearer old-secret-p1"
            assert p1.requests[1]["headers"]["authorization"] == "Bearer old-secret-p1"
            assert json.loads(p1.requests[0]["body"])["model"] == "vendor/p"

            assert len(q.requests) == 3
            assert q.requests[0]["headers"]["authorization"] == "Bearer secret-q"
            assert q.requests[0]["path"] == "/v1/messages?beta=true"
            assert json.loads(q.requests[0]["body"])["model"] == "vendor/q"
            assert json.loads(q.requests[1]["body"])["model"] == "vendor/x"
            assert json.loads(q.requests[2]["body"])["model"] == "vendor/x"

            assert len(p2.requests) == 1
            assert p2.requests[0]["headers"].get("x-api-key") == "new-secret-p2"
            assert "authorization" not in p2.requests[0]["headers"]
            assert json.loads(p2.requests[0]["body"])["model"] == "vendor/p2-new"

            records_a = await gw.records(client, instance_a["id"])
            by_revision = {r["route_revision"]: r for r in records_a}
            assert by_revision[0]["outcome"] == "success"
            assert by_revision[0]["usage"] == {"input_tokens": 10, "output_tokens": 9}
            assert by_revision[1]["provider_id"] == "prov-q"
            assert by_revision[1]["upstream_model"] == "vendor/q"
            records_b = await gw.records(client, instance_b["id"])
            assert records_b[0]["route_revision"] == 0
            assert records_b[0]["upstream_model"] == "vendor/p"
            records_d = await gw.records(client, instance_d["id"])
            assert all(r["upstream_model"] == "vendor/x" and r["outcome"] == "success" for r in records_d)
            assert len(records_d) == 2
    finally:
        gate.set()
        if a1 is not None and not a1.done():
            a1.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await a1
        _stop(proc, log)
        for server in (p1, q, p2):
            server.shutdown()
            server.server_close()


def _sync_records(gw: GatewayHandle, instance_id: str) -> list[dict]:
    with httpx.Client(
        base_url=gw.base, headers=gw.admin, timeout=10.0, trust_env=False
    ) as client:
        r = client.get("/control/v1/requests", params={"instance_id": instance_id, "limit": 100})
        assert r.status_code == 200
        return r.json()["requests"]


async def test_cancel_while_awaiting_upstream_headers_releases_connection(tmp_path):
    upstream = _start_upstream()
    upstream.sse = [SIMPLE_SCRIPT]
    upstream.before_headers = threading.Event()

    state_dir = tmp_path / "state"
    proc, log = _start_serve(state_dir, tmp_path / "serve.log", {"QING_P1": "secret-p1"})
    try:
        discovery = await asyncio.to_thread(_wait_discovery, state_dir)
        gw = GatewayHandle(f"http://127.0.0.1:{discovery['port']}", discovery["control_token"])
        async with gw.client() as client:
            await gw.put_config(client, _config_v1(_url(upstream), _url(upstream)))
            instance, token = await gw.create_instance(client)

            task = asyncio.create_task(_collect_stream(client, "/v1/messages", token))
            await _wait_for(lambda: len(upstream.requests) == 1, what="upstream request arrival")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await task
            # Barrier: the gateway has finished cancellation cleanup (record
            # outcome cancelled) before the held headers are released.
            await _wait_for(
                lambda: any(r["outcome"] == "cancelled" for r in _sync_records(gw, instance["id"])),
                what="cancelled record after disconnect",
            )

            # Release the held headers: the upstream write must fail because
            # the gateway already closed its connection.
            upstream.before_headers.set()
            await _wait_for(lambda: bool(upstream.write_errors or upstream.eof_observed), what="upstream observing closed connection")

            # The gateway stays healthy for subsequent requests.
            body = await _collect_stream(client, "/v1/messages", token)
            assert body == SIMPLE_SCRIPT

            records = await gw.records(client, instance["id"])
            outcomes = sorted(r["outcome"] for r in records)
            assert "cancelled" in outcomes
            assert "success" in outcomes
    finally:
        _stop(proc, log)
        upstream.shutdown()
        upstream.server_close()


async def test_cancel_during_sse_stream_releases_connection(tmp_path):
    upstream = _start_upstream()
    upstream.sse = [A1_SCRIPT[:120], A1_SCRIPT[120:]]
    gate = threading.Event()
    upstream.chunk_gates = [None, gate]
    hang = asyncio.Event()

    state_dir = tmp_path / "state"
    proc, log = _start_serve(state_dir, tmp_path / "serve.log", {"QING_P1": "secret-p1"})
    try:
        discovery = await asyncio.to_thread(_wait_discovery, state_dir)
        gw = GatewayHandle(f"http://127.0.0.1:{discovery['port']}", discovery["control_token"])
        async with gw.client() as client:
            await gw.put_config(client, _config_v1(_url(upstream), _url(upstream)))
            instance, token = await gw.create_instance(client)

            async def read_then_hang(received: list):
                async with client.stream(
                    "POST", "/v1/messages", headers=_headers(token), json=STREAM_BODY
                ) as response:
                    async for chunk in response.aiter_bytes():
                        received.append(chunk)
                        if len(received) == 1:
                            await hang.wait()  # never set: cancellation point
                return b"".join(received)

            received: list = []
            task = asyncio.create_task(read_then_hang(received))
            await _wait_for(lambda: len(upstream.requests) == 1, what="upstream request arrival")
            await _wait_for(lambda: len(received) >= 1, what="first downstream chunk relayed")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await task
            # Barrier: cancellation processed gateway-side before the gate opens.
            await _wait_for(
                lambda: any(r["outcome"] == "cancelled" for r in _sync_records(gw, instance["id"])),
                what="cancelled record after mid-stream cancel",
            )

            gate.set()
            await _wait_for(lambda: bool(upstream.write_errors or upstream.eof_observed), what="upstream observing closed connection")

            body = await _collect_stream(client, "/v1/messages", token)
            assert body == A1_SCRIPT

            records = await gw.records(client, instance["id"])
            outcomes = sorted(r["outcome"] for r in records)
            assert "cancelled" in outcomes
            assert "success" in outcomes
    finally:
        hang.set()
        _stop(proc, log)
        upstream.shutdown()
        upstream.server_close()


async def test_disconnect_during_non_sse_body_read_releases_connection(tmp_path):
    """Downstream disconnect while the gateway reads a buffered 200 JSON body."""
    upstream = _start_upstream()
    upstream.responses = [(200, {"ok": True})]
    upstream.delay = 1.0

    state_dir = tmp_path / "state"
    proc, log = _start_serve(state_dir, tmp_path / "serve.log", {"QING_P1": "secret-p1"})
    try:
        discovery = await asyncio.to_thread(_wait_discovery, state_dir)
        gw = GatewayHandle(f"http://127.0.0.1:{discovery['port']}", discovery["control_token"])
        async with gw.client() as client:
            await gw.put_config(client, _config_v1(_url(upstream), _url(upstream)))
            instance, token = await gw.create_instance(client)

            task = asyncio.create_task(
                _collect_stream(client, "/v1/messages", token)
            )
            await _wait_for(lambda: len(upstream.requests) == 1, what="upstream request arrival")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await task
            await _wait_for(
                lambda: any(r["outcome"] == "cancelled" for r in _sync_records(gw, instance["id"])),
                what="cancelled record after buffered-body disconnect",
            )
            await _wait_for(lambda: bool(upstream.write_errors or upstream.eof_observed), what="upstream observing closed connection")

            upstream.delay = 0
            body = await _collect_stream(client, "/v1/messages", token)
            assert b'"ok"' in body
    finally:
        _stop(proc, log)
        upstream.shutdown()
        upstream.server_close()


async def test_disconnect_during_http_error_body_read_releases_connection(tmp_path):
    upstream = _start_upstream()
    upstream.responses = [(500, {"type": "error", "error": {"type": "api_error"}})]
    upstream.delay = 1.0

    state_dir = tmp_path / "state"
    proc, log = _start_serve(state_dir, tmp_path / "serve.log", {"QING_P1": "secret-p1"})
    try:
        discovery = await asyncio.to_thread(_wait_discovery, state_dir)
        gw = GatewayHandle(f"http://127.0.0.1:{discovery['port']}", discovery["control_token"])
        async with gw.client() as client:
            await gw.put_config(client, _config_v1(_url(upstream), _url(upstream)))
            instance, token = await gw.create_instance(client)

            task = asyncio.create_task(_collect_stream(client, "/v1/messages", token))
            await _wait_for(lambda: len(upstream.requests) == 1, what="upstream request arrival")
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, httpx.HTTPError):
                await task
            await _wait_for(
                lambda: any(r["outcome"] == "cancelled" for r in _sync_records(gw, instance["id"])),
                what="cancelled record after error-body disconnect",
            )
            await _wait_for(lambda: bool(upstream.write_errors or upstream.eof_observed), what="upstream observing closed connection")
    finally:
        _stop(proc, log)
        upstream.shutdown()
        upstream.server_close()


async def test_upstream_aborts_mid_stream_marks_incomplete_and_stays_healthy(tmp_path):
    upstream = _start_upstream()
    upstream.sse = [A1_SCRIPT[:120], A1_SCRIPT[120:]]
    upstream.sse_chunked = True
    upstream.abort_after = 0  # terminate the chunked body mid-frame

    state_dir = tmp_path / "state"
    proc, log = _start_serve(state_dir, tmp_path / "serve.log", {"QING_P1": "secret-p1"})
    try:
        discovery = await asyncio.to_thread(_wait_discovery, state_dir)
        gw = GatewayHandle(f"http://127.0.0.1:{discovery['port']}", discovery["control_token"])
        async with gw.client() as client:
            await gw.put_config(client, _config_v1(_url(upstream), _url(upstream)))
            instance, token = await gw.create_instance(client)

            with pytest.raises((httpx.RemoteProtocolError, httpx.ReadError, httpx.StreamError)):
                await _collect_stream(client, "/v1/messages", token)

            await _wait_for(
                lambda: any(r["outcome"] == "incomplete" for r in _sync_records(gw, instance["id"])),
                what="incomplete record",
            )
            records = await gw.records(client, instance["id"])
            record = next(r for r in records if r["outcome"] == "incomplete")
            assert record["error_code"] == "upstream_error"
            assert record["usage"] == {"input_tokens": 10, "output_tokens": 1}

            # The gateway and upstream stay usable afterwards.
            upstream.sse_chunked = False
            upstream.abort_after = None
            body = await _collect_stream(client, "/v1/messages", token)
            assert body == A1_SCRIPT
            assert any(r["outcome"] == "success" for r in await gw.records(client, instance["id"]))
    finally:
        _stop(proc, log)
        upstream.shutdown()
        upstream.server_close()
