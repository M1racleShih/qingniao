from __future__ import annotations

import json

from qingniao.config import validate_config
from qingniao.gateway import Gateway, sanitize_usage
from tests.conftest import FakeClock, make_config_dict

ALLOWED_RECORD_KEYS = {
    "id",
    "instance_id",
    "request_model",
    "provider_id",
    "upstream_model",
    "route_revision",
    "outcome",
    "started_at",
    "finished_at",
    "status_code",
    "error_code",
    "usage",
}


def test_sanitize_usage_garbage_types():
    assert sanitize_usage("not a dict") is None
    assert sanitize_usage([1, 2]) is None
    assert sanitize_usage({"output_tokens": "eight"}) == {"output_tokens": None}
    assert sanitize_usage({"output_tokens": True}) == {"output_tokens": None}
    assert sanitize_usage({"input_tokens": 3, "output_tokens": 0}) == {"input_tokens": 3, "output_tokens": 0}
    assert sanitize_usage({}) is None
    assert sanitize_usage({"weird": 1}) is None


def test_record_retention_single_bounded_set(tmp_path):
    """Deque and id index form one retention set: at most 1000 distinct
    records total, including when long-lived active records age out."""
    from qingniao.config import ConfigStore

    gateway = Gateway(
        validate_config(make_config_dict()), ConfigStore(tmp_path / "config.json"), clock=FakeClock()
    )
    instance, _ = gateway.create_instance()
    other, _ = gateway.create_instance()

    long_lived = gateway.begin_request(instance_id=instance.id, request_model="req-main")
    ids = [long_lived]
    for _ in range(1000):
        record_id = gateway.begin_request(
            instance_id=instance.id, request_model="req-main", provider_id="prov-p"
        )
        gateway.finish_request(record_id, outcome="success", status_code=200)
        ids.append(record_id)

    assert len(gateway.records) == 1000
    retained_ids = {r.id for r in gateway.records}
    # the id index only ever points at records still retained by the deque
    assert set(gateway._records_by_id) <= retained_ids
    assert long_lived not in retained_ids, "evicted active record must not linger in the index"
    assert long_lived not in gateway._records_by_id

    # late completion of the evicted active record is harmless
    gateway.finish_request(long_lived, outcome="success", status_code=200)
    assert len(gateway.records) == 1000
    assert all(r.id != long_lived for r in gateway.records)

    # a still-retained active record can complete normally
    active = gateway.begin_request(instance_id=instance.id, request_model="req-main")
    assert active in {r.id for r in gateway.records}
    gateway.finish_request(active, outcome="success", status_code=200)
    assert any(r.id == active and r.outcome == "success" for r in gateway.records)

    page, total, next_offset = gateway.list_requests(offset=0, limit=50)
    assert total == 1000 and len(page) == 50 and next_offset == 50
    filtered, total_f, _ = gateway.list_requests(instance_id=other.id)
    assert total_f == 0 and filtered == []


def test_note_request_usage_publishes_to_in_progress_only(tmp_path):
    from qingniao.config import ConfigStore

    gateway = Gateway(
        validate_config(make_config_dict()), ConfigStore(tmp_path / "config.json"), clock=FakeClock()
    )
    instance, _ = gateway.create_instance()
    record_id = gateway.begin_request(instance_id=instance.id, request_model="req-main")
    assert gateway.records[-1].usage is None
    gateway.note_request_usage(record_id, {"input_tokens": 5})
    assert gateway.records[-1].usage == {"input_tokens": 5}
    gateway.finish_request(record_id, outcome="success", usage={"input_tokens": 5, "output_tokens": 2})
    gateway.note_request_usage(record_id, {"input_tokens": 999})
    assert gateway.records[-1].usage == {"input_tokens": 5, "output_tokens": 2}


async def test_requests_endpoint_auth_pagination_and_filter(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.get("/control/v1/requests")
        assert r.status_code == 401
        created = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={"label": "a"})
        instance_token = created.json()["token"]
        r = await ctx.client.get(
            "/control/v1/requests", headers={"authorization": f"Bearer {instance_token}"}
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "instance_token_not_admin"

        r = await ctx.client.get(
            "/control/v1/requests",
            headers=ctx.admin_headers,
            params={"limit": 500, "instance_id": "i-nonexistent"},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["limit"] == 100 and body["total"] == 0 and body["requests"] == []
        assert body["next_offset"] is None

        r = await ctx.client.get("/control/v1/requests", headers=ctx.admin_headers, params={"offset": -1})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_pagination"


async def test_rejected_request_record_shape_no_secrets(make_gateway_app):
    async with make_gateway_app() as ctx:
        created = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={})
        token = created.json()["token"]

        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}"},
            json={"model": [], "messages": []},
        )
        assert r.status_code == 400

        r = await ctx.client.get("/control/v1/requests", headers=ctx.admin_headers)
        records = r.json()["requests"]
        assert len(records) == 1
        record = records[0]
        assert set(record.keys()) == ALLOWED_RECORD_KEYS
        assert record["outcome"] == "failed"
        assert record["error_code"] == "invalid_request"
        assert record["request_model"] == "<invalid>"
        assert record["provider_id"] is None and record["usage"] is None

        serialized = json.dumps(records)
        assert token not in serialized
        assert ctx.admin_token not in serialized


async def test_failed_stream_does_not_persist_raw_error_bodies(make_gateway_app, upstream_factory, monkeypatch):
    from tests.conftest import sse_event

    upstream = upstream_factory((500, {"type": "error", "error": {"type": "api_error", "message": "secret-ish detail"}}))
    monkeypatch.setenv("QING_TEST_P", "credential-value-not-in-records")
    config = make_config_dict(p_base_url=f"http://127.0.0.1:{upstream.server_address[1]}")

    async with make_gateway_app(config) as ctx:
        created = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={})
        token = created.json()["token"]
        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "req-main", "stream": True, "messages": []},
        )
        assert r.status_code == 500
        records = (await ctx.client.get("/control/v1/requests", headers=ctx.admin_headers)).json()["requests"]
        serialized = json.dumps(records)
        assert "secret-ish detail" not in serialized
        assert "credential-value-not-in-records" not in serialized
        assert token not in serialized
        assert records[0]["error_code"] is None or records[0]["error_code"] == "upstream_error"
        assert records[0]["outcome"] == "failed"
