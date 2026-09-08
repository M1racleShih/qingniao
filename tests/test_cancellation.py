"""Cancellation and resource-release semantics of the proxy internals (S7)."""

from __future__ import annotations

import asyncio
import contextlib

import pytest

from qingniao.proxy_api import _close_quietly, _race_disconnect


class RecordingCloser:
    def __init__(self):
        self.calls = 0

    async def __call__(self):
        self.calls += 1


async def _hang_forever(started: asyncio.Event, finished: list):
    started.set()
    try:
        await asyncio.Event().wait()
    finally:
        finished.append("op-done")


async def test_external_cancellation_cancels_operation_and_closes():
    started = asyncio.Event()
    finished: list = []
    closer = RecordingCloser()
    disconnect = asyncio.ensure_future(asyncio.Event().wait())
    outer = asyncio.ensure_future(
        _race_disconnect(_hang_forever(started, finished), disconnect, closer)
    )
    await started.wait()
    await asyncio.sleep(0.01)
    outer.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await outer
    assert finished == ["op-done"], "operation task must have been cancelled and awaited"
    assert closer.calls == 1


async def test_disconnect_cancels_operation_and_closes():
    started = asyncio.Event()
    finished: list = []
    closer = RecordingCloser()
    disconnect_done = asyncio.Event()

    async def disconnect_soon():
        await started.wait()
        await asyncio.sleep(0.01)
        disconnect_done.set()

    outer = asyncio.ensure_future(
        _race_disconnect(_hang_forever(started, finished), asyncio.ensure_future(disconnect_soon()), closer)
    )
    with pytest.raises(asyncio.CancelledError):
        await outer
    assert finished == ["op-done"]
    assert closer.calls == 1


async def test_operation_exception_propagates_without_race_close():
    async def boom():
        raise ValueError("upstream blew up")

    closer = RecordingCloser()
    disconnect = asyncio.ensure_future(asyncio.Event().wait())
    try:
        await _race_disconnect(boom(), disconnect, closer)
        raise AssertionError("expected ValueError")
    except ValueError:
        pass
    finally:
        disconnect.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await disconnect
    assert closer.calls == 0  # the owner releases on exception paths


async def test_close_quietly_is_safe_to_call_twice():
    class CM:
        def __init__(self):
            self.closed = 0

        async def __aexit__(self, *args):
            self.closed += 1

    cm = CM()
    await _close_quietly(cm)
    await _close_quietly(cm)
    assert cm.closed == 2


async def test_aread_error_returns_502_and_releases(make_gateway_app, upstream_factory, monkeypatch):
    """Truncated chunked JSON body: the buffered read fails, the gateway
    answers 502 with a failed record and stays usable."""
    upstream = upstream_factory()
    upstream.responses = [
        (200, b"5\r\nab", "application/json", {"Transfer-Encoding": "chunked"}),
        (200, {"ok": True}),
    ]
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    from tests.conftest import make_config_dict

    config = make_config_dict(p_base_url=f"http://127.0.0.1:{upstream.server_address[1]}")

    async with make_gateway_app(config) as ctx:
        created = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={})
        token = created.json()["token"]
        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "req-main", "messages": []},
        )
        assert r.status_code == 502
        assert r.json()["error"]["code"] == "upstream_error"

        r = await ctx.client.get("/control/v1/requests", headers=ctx.admin_headers)
        record = r.json()["requests"][-1]
        assert record["outcome"] == "failed"
        assert record["error_code"] == "upstream_error"

        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "req-main", "messages": []},
        )
        assert r.status_code == 200
        assert r.json()["ok"] is True
