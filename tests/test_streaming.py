from __future__ import annotations

import asyncio
import json
import threading
import time as _time

from tests.conftest import make_config_dict, sse_event


def _upstream_url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


async def _create_instance(ctx, **kwargs) -> tuple[str, str]:
    r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json=kwargs)
    assert r.status_code == 201, r.text
    body = r.json()
    return body["instance"]["id"], body["token"]


def _headers(token: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219",
    }


def _body(stream: bool = True) -> dict:
    payload = {"model": "req-main", "max_tokens": 8, "messages": [{"role": "user", "content": "probe"}]}
    if stream:
        payload["stream"] = True
    return payload


async def _list_records(ctx, instance_id: str) -> list[dict]:
    r = await ctx.client.get(
        "/control/v1/requests", headers=ctx.admin_headers, params={"instance_id": instance_id, "limit": 100}
    )
    assert r.status_code == 200, r.text
    return r.json()["requests"]


FULL_SCRIPT = (
    sse_event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10, "output_tokens": 1}}})
    + sse_event("content_block_delta", {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "你好"}})
    + sse_event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 5}})
    + sse_event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 8}})
    + sse_event("message_stop", {"type": "message_stop"})
)


async def test_stream_relayed_verbatim_with_cumulative_usage(make_gateway_app, upstream_factory, monkeypatch):
    import json as _json

    upstream = upstream_factory()
    upstream.sse = [
        FULL_SCRIPT[:80],
        FULL_SCRIPT[80:200],
        FULL_SCRIPT[200:],
    ]
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages?beta=true", headers=_headers(token), json=_body())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert r.content == FULL_SCRIPT

        seen = upstream.requests[0]
        assert seen["path"] == "/v1/messages?beta=true"
        assert seen["headers"]["authorization"] == "Bearer upstream-secret-p"
        forwarded = _json.loads(seen["body"])
        assert forwarded["model"] == "vendor/p"
        assert forwarded["stream"] is True
        assert forwarded["messages"] == [{"role": "user", "content": "probe"}]

        records = await _list_records(ctx, instance_id)
        assert len(records) == 1
        record = records[0]
        assert record["outcome"] == "success"
        assert record["status_code"] == 200
        assert record["usage"] == {"input_tokens": 10, "output_tokens": 8}
        assert record["route_revision"] == 0
        assert record["provider_id"] == "prov-p" and record["upstream_model"] == "vendor/p"
        assert record["finished_at"] is not None


async def test_stream_missing_message_stop_is_incomplete(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    upstream.sse = [
        sse_event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 3}}}),
        sse_event("message_delta", {"type": "message_delta", "usage": {"output_tokens": 6}}),
    ]
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_headers(token), json=_body())
        assert r.status_code == 200
        records = await _list_records(ctx, instance_id)
        assert records[0]["outcome"] == "incomplete"
        assert records[0]["error_code"] is None
        assert records[0]["usage"] == {"input_tokens": 3, "output_tokens": 6}


async def test_stream_sse_error_event_is_failed(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    upstream.sse = [
        sse_event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 2}}}),
        sse_event("error", {"type": "error", "error": {"type": "overloaded_error"}}),
    ]
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_headers(token), json=_body())
        assert r.status_code == 200
        records = await _list_records(ctx, instance_id)
        assert records[0]["outcome"] == "failed"
        assert records[0]["error_code"] == "upstream_sse_error"
        assert records[0]["usage"] == {"input_tokens": 2}


async def test_stream_upstream_http_error_relays_and_fails(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory((500, {"type": "error", "error": {"type": "api_error"}, "usage": {"input_tokens": 4}}))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_headers(token), json=_body())
        assert r.status_code == 500
        assert r.json()["error"]["type"] == "api_error"
        records = await _list_records(ctx, instance_id)
        assert records[0]["outcome"] == "failed"
        assert records[0]["status_code"] == 500
        assert records[0]["usage"] == {"input_tokens": 4}


async def test_stream_request_non_sse_200_buffered(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory((200, {"id": "msg_x", "usage": {"input_tokens": 7, "output_tokens": 0}}))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_headers(token), json=_body())
        assert r.status_code == 200
        assert r.json()["id"] == "msg_x"
        records = await _list_records(ctx, instance_id)
        assert records[0]["outcome"] == "success"
        assert records[0]["usage"] == {"input_tokens": 7, "output_tokens": 0}


async def test_count_tokens_usage_recorded(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory((200, {"input_tokens": 42}))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        r = await ctx.client.post(
            "/v1/messages/count_tokens",
            headers=_headers(token),
            json={"model": "req-main", "messages": [{"role": "user", "content": "probe"}]},
        )
        assert r.status_code == 200
        records = await _list_records(ctx, instance_id)
        assert records[0]["outcome"] == "success"
        assert records[0]["usage"] == {"input_tokens": 42}


async def test_gzip_encoded_sse_is_decoded_for_downstream_and_usage(make_gateway_app, upstream_factory, monkeypatch):
    """Compressed SSE must keep decoded event semantics downstream (S6)."""
    import gzip

    upstream = upstream_factory()
    upstream.responses = [
        (200, gzip.compress(FULL_SCRIPT), "text/event-stream", {"Content-Encoding": "gzip"})
    ]
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_headers(token), json=_body())
        assert r.status_code == 200
        assert r.headers["content-type"].startswith("text/event-stream")
        assert "content-encoding" not in r.headers
        assert r.content == FULL_SCRIPT
        records = await _list_records(ctx, instance_id)
        assert records[0]["outcome"] == "success"
        assert records[0]["usage"] == {"input_tokens": 10, "output_tokens": 8}


async def test_in_progress_record_visible_then_finalized(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    gate = threading.Event()
    upstream.sse = [
        sse_event("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 11}}}),
        sse_event("message_stop", {"type": "message_stop"}),
    ]
    upstream.chunk_gates = [None, gate]
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        task = asyncio.create_task(ctx.client.post("/v1/messages", headers=_headers(token), json=_body()))
        try:
            deadline = _time.monotonic() + 5
            in_progress = None
            while _time.monotonic() < deadline:
                records = await _list_records(ctx, instance_id)
                if records:
                    in_progress = records[0]
                    break
                await asyncio.sleep(0.01)
            assert in_progress is not None, "record never appeared"
            assert in_progress["outcome"] == "in_progress"
            assert in_progress["finished_at"] is None
            assert in_progress["started_at"] is not None
            # usage observed so far is visible while still in progress
            assert in_progress["usage"] == {"input_tokens": 11}
            gate.set()
            r = await asyncio.wait_for(task, timeout=10)
            assert r.status_code == 200
            records = await _list_records(ctx, instance_id)
            assert records[0]["outcome"] == "success"
            assert records[0]["finished_at"] is not None
            assert records[0]["usage"] == {"input_tokens": 11}
        finally:
            gate.set()
            if not task.done():
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):
                    pass
