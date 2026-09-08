from __future__ import annotations

import json

from tests.conftest import FakeClock, make_config_dict


async def test_admin_auth_required_and_instance_token_separated(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.get("/control/v1/config")
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "invalid_admin_token"

        r = await ctx.client.get("/control/v1/config", headers={"authorization": "Bearer wrong-token"})
        assert r.status_code == 401

        r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={"label": "a"})
        assert r.status_code == 201
        instance_token = r.json()["token"]

        r = await ctx.client.get("/control/v1/config", headers={"authorization": f"Bearer {instance_token}"})
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "instance_token_not_admin"

        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        assert r.status_code == 200


async def test_config_get_put_roundtrip_and_persistence(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        r = await ctx.client.put("/control/v1/config", headers=ctx.admin_headers, json=make_config_dict())
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True
        persisted = json.loads((tmp_path / "config.json").read_text())
        assert persisted["defaults"]["model"] == "req-main"

        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        assert r.json()["config"] == persisted

        bad = make_config_dict()
        bad["providers"]["prov-p"]["base_url"] = "http://localhost:70000"
        r = await ctx.client.put("/control/v1/config", headers=ctx.admin_headers, json=bad)
        assert r.status_code == 400
        err = r.json()["error"]
        assert err["code"] == "invalid_config"
        assert any(e["path"] == "providers.prov-p.base_url" for e in err["errors_"])
        assert json.loads((tmp_path / "config.json").read_text()) == persisted


async def test_instance_lifecycle_endpoints(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={"label": "a"})
        assert r.status_code == 201
        created = r.json()
        instance_id = created["instance"]["id"]
        assert created["instance"]["state"] == "active"
        assert isinstance(created["token"], str) and len(created["token"]) >= 43

        r = await ctx.client.get(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
        assert r.status_code == 200
        assert "token" not in json.dumps(r.json())

        r = await ctx.client.post(f"/control/v1/instances/{instance_id}/renew", headers=ctx.admin_headers)
        assert r.status_code == 200

        r = await ctx.client.delete(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
        assert r.status_code == 200
        assert r.json()["ended"] is True

        r = await ctx.client.post(f"/control/v1/instances/{instance_id}/renew", headers=ctx.admin_headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "instance_ended"

        r = await ctx.client.get("/control/v1/instances/missing", headers=ctx.admin_headers)
        assert r.status_code == 404


async def test_instance_creation_invalid_selection_no_partial(make_gateway_app):
    async with make_gateway_app() as ctx:
        for payload in (
            {"routes": {"req-main": "model-missing"}},
            {"model": []},
            {"aux_model": {}},
            {"model": ""},
        ):
            r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json=payload)
            assert r.status_code == 400
            assert r.json()["error"]["code"] == "invalid_selection"
        r = await ctx.client.get("/control/v1/instances", headers=ctx.admin_headers)
        assert r.json()["total"] == 0


async def test_instances_pagination_bounds(make_gateway_app):
    async with make_gateway_app() as ctx:
        for i in range(105):
            r = await ctx.client.post(
                "/control/v1/instances", headers=ctx.admin_headers, json={"label": f"n{i}"}
            )
            assert r.status_code == 201

        r = await ctx.client.get("/control/v1/instances", headers=ctx.admin_headers)
        body = r.json()
        assert body["total"] == 105 and len(body["instances"]) == 50
        assert body["next_offset"] == 50

        r = await ctx.client.get("/control/v1/instances", headers=ctx.admin_headers, params={"limit": 500})
        body = r.json()
        assert body["limit"] == 100 and len(body["instances"]) == 100

        r = await ctx.client.get("/control/v1/instances", headers=ctx.admin_headers, params={"offset": 100})
        body = r.json()
        assert len(body["instances"]) == 5 and body["next_offset"] is None

        r = await ctx.client.get("/control/v1/instances", headers=ctx.admin_headers, params={"offset": -1})
        assert r.status_code == 400
        assert r.json()["error"]["code"] == "invalid_pagination"


async def test_route_put_cas_conflict_and_rejection(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={})
        instance_id = r.json()["instance"]["id"]

        r = await ctx.client.put(
            f"/control/v1/instances/{instance_id}/routes",
            headers=ctx.admin_headers,
            json={"request_model": "req-main", "model": "model-q", "expected_revision": 5},
        )
        assert r.status_code == 409
        err = r.json()["error"]
        assert err["code"] == "revision_conflict" and err["current_revision"] == 0

        r = await ctx.client.put(
            f"/control/v1/instances/{instance_id}/routes",
            headers=ctx.admin_headers,
            json={"request_model": "req-main", "model": "model-q", "expected_revision": 0},
        )
        assert r.status_code == 200
        body = r.json()
        assert body["applied"] is True and body["revision"] == 1
        assert body["route"]["provider"] == "prov-q"

        await ctx.client.delete(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
        r = await ctx.client.put(
            f"/control/v1/instances/{instance_id}/routes",
            headers=ctx.admin_headers,
            json={"request_model": "req-main", "model": "model-q", "expected_revision": 1},
        )
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "instance_ended"


async def test_route_put_unknown_destination_no_mutation(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={})
        instance_id = r.json()["instance"]["id"]
        r = await ctx.client.put(
            f"/control/v1/instances/{instance_id}/routes",
            headers=ctx.admin_headers,
            json={"request_model": "req-main", "model": "model-missing", "expected_revision": 0},
        )
        assert r.status_code == 400
        r = await ctx.client.get(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
        assert r.json()["instance"]["revision"] == 0


async def test_config_put_persist_failure_stable_json_error(make_gateway_app, tmp_path):
    """config.json replaced by a directory: PUT must return the stable JSON
    error shape and leave the previous applied state untouched."""
    from tests.conftest import make_config_dict

    (tmp_path / "config.json").mkdir()
    async with make_gateway_app() as ctx:
        r = await ctx.client.put(
            "/control/v1/config", headers=ctx.admin_headers, json=make_config_dict()
        )
        assert r.status_code == 500
        assert r.headers["content-type"].startswith("application/json")
        assert r.json()["error"]["code"] == "config_persist_failed"
        assert "Traceback" not in r.text

        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        assert r.status_code == 200
        assert r.json()["revision"] == 1
        assert r.json()["config"]["defaults"]["model"] == "req-main"


async def test_config_put_persist_failure_preserves_full_state(make_gateway_app, tmp_path, monkeypatch):
    """Injected pre-replace persistence failure through the API: file bytes,
    config revision, in-memory catalog values and an existing instance's
    route snapshot must all be unchanged."""
    import os

    from tests.conftest import make_config_dict

    async with make_gateway_app() as ctx:
        r = await ctx.client.put("/control/v1/config", headers=ctx.admin_headers, json=make_config_dict())
        assert r.status_code == 200
        applied_revision = r.json()["revision"]

        r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={"label": "a"})
        instance_id = r.json()["instance"]["id"]
        before_status = (await ctx.client.get(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)).json()[
            "instance"
        ]
        persisted_bytes = (tmp_path / "config.json").read_bytes()

        def failing_replace(src, dst):
            raise OSError("injected pre-replace failure")

        monkeypatch.setattr(os, "replace", failing_replace)
        changed = make_config_dict()
        changed["providers"]["prov-p"]["base_url"] = "http://127.0.0.1:9999"
        changed["models"]["model-p"]["upstream_model"] = "vendor/CHANGED"
        changed["defaults"]["routes"]["req-main"] = "model-q"
        r = await ctx.client.put("/control/v1/config", headers=ctx.admin_headers, json=changed)
        monkeypatch.undo()

        assert r.status_code == 500
        assert r.json()["error"]["code"] == "config_persist_failed"
        assert (tmp_path / "config.json").read_bytes() == persisted_bytes

        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        body = r.json()
        assert body["revision"] == applied_revision
        assert body["config"]["providers"]["prov-p"]["base_url"] == "http://127.0.0.1:9101"
        assert body["config"]["models"]["model-p"]["upstream_model"] == "vendor/p"
        assert body["config"]["defaults"]["routes"]["req-main"] == "model-p"

        r = await ctx.client.get(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
        after_status = r.json()["instance"]
        assert after_status["revision"] == before_status["revision"]
        assert after_status["routes"] == before_status["routes"]


async def test_instance_state_shows_expired_with_controlled_clock(make_gateway_app):
    clock = FakeClock()
    async with make_gateway_app(clock=clock) as ctx:
        r = await ctx.client.post("/control/v1/instances", headers=ctx.admin_headers, json={})
        instance_id = r.json()["instance"]["id"]
        clock.advance(31)
        r = await ctx.client.get(f"/control/v1/instances/{instance_id}", headers=ctx.admin_headers)
        assert r.json()["instance"]["state"] == "expired"
        r = await ctx.client.post(f"/control/v1/instances/{instance_id}/renew", headers=ctx.admin_headers)
        assert r.status_code == 403
        assert r.json()["error"]["code"] == "instance_expired"
