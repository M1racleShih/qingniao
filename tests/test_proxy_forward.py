from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import FakeClock, make_config_dict


def _upstream_url(server) -> str:
    return f"http://127.0.0.1:{server.server_address[1]}"


async def _create_instance(ctx, **kwargs) -> tuple[str, str]:
    r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json=kwargs)
    assert r.status_code == 201, r.text
    body = r.json()
    return body["instance"]["id"], body["token"]


def _message_headers(token: str) -> dict:
    return {
        "authorization": f"Bearer {token}",
        "anthropic-version": "2023-06-01",
        "anthropic-beta": "claude-code-20250219",
        "content-type": "application/json",
    }


def _message_body() -> dict:
    return {
        "model": "req-main",
        "stream": False,
        "max_tokens": 16,
        "messages": [{"role": "user", "content": "probe"}],
    }


async def test_forward_rewrites_model_and_injects_bearer(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory((200, {"id": "msg_1", "usage": {"input_tokens": 3, "output_tokens": 1}}))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        r = await ctx.client.post(
            "/v1/messages?beta=true", headers=_message_headers(token), json=_message_body()
        )
        assert r.status_code == 200
        assert r.json()["id"] == "msg_1"

        assert len(upstream.requests) == 1
        request = upstream.requests[0]
        assert request["method"] == "POST"
        assert request["path"] == "/v1/messages?beta=true"
        assert request["headers"]["authorization"] == "Bearer upstream-secret-p"
        assert "x-api-key" not in request["headers"]
        assert request["headers"]["anthropic-version"] == "2023-06-01"
        assert request["headers"]["anthropic-beta"] == "claude-code-20250219"
        body = json.loads(request["body"])
        assert body["model"] == "vendor/p"
        assert body["messages"] == [{"role": "user", "content": "probe"}]
        assert body["stream"] is False

        record = ctx.gateway.records[-1]
        assert record.outcome == "success"
        assert record.provider_id == "prov-p" and record.upstream_model == "vendor/p"
        assert record.usage == {"input_tokens": 3, "output_tokens": 1}


async def test_forward_x_api_key_auth(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    monkeypatch.setenv("QING_TEST_P", "upstream-key-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream), p_auth="x-api-key")

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
        assert r.status_code == 200
        request = upstream.requests[0]
        assert request["headers"].get("x-api-key") == "upstream-key-p"
        assert "authorization" not in request["headers"]


async def test_unknown_model_rejected_zero_upstream(make_gateway_app, upstream_factory):
    upstream = upstream_factory()
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        body = _message_body()
        body["model"] = "claude-not-configured"
        r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=body)
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "unknown_model" and err["request_model"] == "claude-not-configured"
        assert len(upstream.requests) == 0


async def test_stream_request_now_forwarded(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))
    upstream.responses = [(200, {"id": "msg_s", "usage": {"input_tokens": 2, "output_tokens": 1}})]

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        body = _message_body()
        body["stream"] = True
        r = await ctx.client.post("/v1/messages?beta=true", headers=_message_headers(token), json=body)
        assert r.status_code == 200
        assert len(upstream.requests) == 1
        assert upstream.requests[0]["path"] == "/v1/messages?beta=true"


async def test_invalid_token_rejected_zero_upstream(make_gateway_app, upstream_factory):
    upstream = upstream_factory()
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        for token in ("", "wrong-token", ctx.admin_token):
            headers = _message_headers(token) if token else {"content-type": "application/json"}
            r = await ctx.client.post("/v1/messages", headers=headers, json=_message_body())
            assert r.status_code == 401
            assert r.json()["error"]["code"] == "invalid_instance_token"
        assert len(upstream.requests) == 0


async def test_ended_and_expired_fail_closed_zero_upstream(make_gateway_app, upstream_factory):
    upstream = upstream_factory()
    clock = FakeClock()
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config, clock=clock) as ctx:
        ended_id, ended_token = await _create_instance(ctx)
        await ctx.client.delete(f"/control/v1/instances/{ended_id}", headers=ctx.admin_headers)
        expired_id, expired_token = await _create_instance(ctx)
        clock.advance(31)

        for token, code in ((ended_token, "instance_ended"), (expired_token, "instance_expired")):
            r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
            assert r.status_code == 403
            assert r.json()["error"]["code"] == code
        assert len(upstream.requests) == 0


async def test_missing_credential_env_fails_before_upstream(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    monkeypatch.delenv("QING_TEST_P", raising=False)
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
        assert r.status_code == 500
        err = r.json()["error"]
        assert err["code"] == "credential_missing" and err["credential_env"] == "QING_TEST_P"
        assert len(upstream.requests) == 0


async def test_invalid_credential_relayed_honestly(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory((401, {"type": "error", "error": {"type": "authentication_error"}}))
    monkeypatch.setenv("QING_TEST_P", "wrong-upstream-secret")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
        assert r.status_code == 401
        assert r.json()["error"]["type"] == "authentication_error"
        assert ctx.gateway.records[-1].outcome == "failed"
        assert ctx.gateway.records[-1].status_code == 401


async def test_upstream_401_json_without_code_relayed_verbatim(make_gateway_app, upstream_factory, monkeypatch):
    """A relayed upstream 401 is not the gateway Error shape: same status as
    the gateway's invalid_instance_token, but the body has no stable code."""
    upstream = upstream_factory((401, {"detail": "invalid api key supplied"}))
    monkeypatch.setenv("QING_TEST_P", "wrong-upstream-secret")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
        assert r.status_code == 401
        assert r.headers["content-type"].startswith("application/json")
        body = r.json()
        assert body == {"detail": "invalid api key supplied"}
        assert "error" not in body or "code" not in body.get("error", {})


async def test_upstream_400_text_plain_relayed_verbatim(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory((400, b"bad request shape", "text/plain"))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
        assert r.status_code == 400
        assert r.headers["content-type"].startswith("text/plain")
        assert r.text == "bad request shape"


async def test_instance_ended_during_body_upload_zero_upstream(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)

        async def slow_body():
            await ctx.client.delete(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
            yield json.dumps(_message_body()).encode()

        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
            content=slow_body(),
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "instance_ended"
        assert len(upstream.requests) == 0


async def test_instance_expired_during_body_upload_zero_upstream(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory()
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    clock = FakeClock()
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config, clock=clock) as ctx:
        _, token = await _create_instance(ctx)

        async def slow_body():
            clock.advance(30)
            yield json.dumps(_message_body()).encode()

        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}", "content-type": "application/json"},
            content=slow_body(),
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "instance_expired"
        assert len(upstream.requests) == 0


async def test_already_admitted_request_survives_later_end(make_gateway_app, upstream_factory, monkeypatch):
    """Admission is proven by the observed upstream request, not by a sleep:
    the fixture holds its response until released, the instance is ended
    while the request is in flight, and only then is the response released."""
    import contextlib
    import threading
    import time as _time

    upstream = upstream_factory((200, {"ok": True}))
    upstream.hold = threading.Event()
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        instance_id, token = await _create_instance(ctx)
        task = asyncio.create_task(
            ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
        )
        try:
            deadline = _time.monotonic() + 5
            while not upstream.requests and _time.monotonic() < deadline:
                await asyncio.sleep(0.01)
            assert upstream.requests, "gateway never forwarded the admitted request"

            await ctx.client.delete(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
            upstream.hold.set()
            r = await asyncio.wait_for(task, timeout=10)
            assert r.status_code == 200
            assert len(upstream.requests) == 1

            r = await ctx.client.post("/v1/messages", headers=_message_headers(token), json=_message_body())
            assert r.status_code == 403
            assert r.json()["error"]["code"] == "instance_ended"
            assert len(upstream.requests) == 1
        finally:
            upstream.hold.set()
            if not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task


async def test_count_tokens_forwarded(make_gateway_app, upstream_factory, monkeypatch):
    upstream = upstream_factory((200, {"input_tokens": 42}))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    config = make_config_dict(p_base_url=_upstream_url(upstream))

    async with make_gateway_app(config) as ctx:
        _, token = await _create_instance(ctx)
        r = await ctx.client.post(
            "/v1/messages/count_tokens",
            headers=_message_headers(token),
            json={"model": "req-main", "messages": [{"role": "user", "content": "probe"}]},
        )
        assert r.status_code == 200
        assert r.json()["input_tokens"] == 42
        request = upstream.requests[0]
        assert request["path"] == "/v1/messages/count_tokens"
        assert json.loads(request["body"])["model"] == "vendor/p"


async def test_route_switch_snapshot_semantics_per_request(make_gateway_app, upstream_factory, monkeypatch):
    upstream_p = upstream_factory((200, {"which": "p"}))
    upstream_q = upstream_factory((200, {"which": "q"}))
    monkeypatch.setenv("QING_TEST_P", "upstream-secret-p")
    monkeypatch.setenv("QING_TEST_Q", "upstream-secret-q")
    config = make_config_dict(
        p_base_url=_upstream_url(upstream_p), q_base_url=_upstream_url(upstream_q)
    )

    async with make_gateway_app(config) as ctx:
        a_id, a_token = await _create_instance(ctx, label="a")
        b_id, b_token = await _create_instance(ctx, label="b")

        r = await ctx.client.put(
            f"/control/v1/instances/{a_id}/routes",
            headers=ctx.admin_headers,
            json={"request_model": "req-main", "model": "model-q", "expected_revision": 0},
        )
        assert r.status_code == 200 and r.json()["applied"] is True

        ra = await ctx.client.post("/v1/messages", headers=_message_headers(a_token), json=_message_body())
        rb = await ctx.client.post("/v1/messages", headers=_message_headers(b_token), json=_message_body())
        assert ra.json()["which"] == "q"
        assert rb.json()["which"] == "p"
        assert len(upstream_p.requests) == 1
        assert len(upstream_q.requests) == 1
        assert upstream_q.requests[0]["headers"]["authorization"] == "Bearer upstream-secret-q"
