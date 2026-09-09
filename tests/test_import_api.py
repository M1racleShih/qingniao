from __future__ import annotations

import json
from pathlib import Path

import pytest

from qingniao import errors
from qingniao.claude_source import parse_claude_settings
from qingniao.config import ConfigStore
from qingniao.credentials import CredentialStore
from qingniao.importing import (
    ImportDecisions,
    build_plan,
    plan_from_wire,
    plan_preview,
    plan_to_wire,
    source_digest,
)

TOKEN = "sk-ant-api03-SYNTHETIC-API-SECRET"
OP_ID = "op_" + "7" * 32

SOURCE = {
    "env": {
        "ANTHROPIC_BASE_URL": "https://api.anthropic.test",
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_MODEL": "claude-main",
        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
    }
}


def wire_plan(state_dir: Path, *, operation_id: str = OP_ID, current=None) -> dict:
    if current is None:
        current = ConfigStore(state_dir / "config.json").load()
    parsed = parse_claude_settings(json.dumps(SOURCE).encode(), source=Path("/tmp/settings.json"))
    plan = build_plan(
        parsed,
        source_digest=source_digest(json.dumps(SOURCE).encode()),
        current=current,
        credential_store=CredentialStore(state_dir),
        decisions=ImportDecisions(),
        operation_id=operation_id,
    )
    return plan_to_wire(plan)


# ---------------------------------------------------------------- wire format


def test_wire_roundtrip_preserves_plan_except_repr_safety(tmp_path):
    payload = wire_plan(tmp_path)
    assert payload["secret"] == TOKEN
    plan = plan_from_wire(payload)
    assert plan.secret == TOKEN
    assert plan.defaults.model == "claude-main"
    assert TOKEN not in repr(plan)


def test_wire_rejects_malformed_plans_without_echo():
    with pytest.raises(Exception):
        plan_from_wire({"plan": "nope"})
    with pytest.raises(Exception):
        plan_from_wire(None)
    bad = wire_plan(Path("/tmp"))
    bad["models"] = "nope"
    with pytest.raises(Exception):
        plan_from_wire(bad)



async def _persist_base(ctx) -> None:
    """Persist the fixture's in-memory config so imports write onto real disk state."""
    r = await ctx.client.put(
        "/control/v1/config", headers=ctx.admin_headers, json=ctx.gateway.config.to_json()
    )
    assert r.status_code == 200, r.text

# ---------------------------------------------------------------- generation conditions


@pytest.mark.anyio
async def test_get_config_returns_generation(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        assert r.status_code == 200
        assert r.json()["generation"] == 0  # legacy in-memory config
        r = await ctx.client.put(
            "/control/v1/config", headers=ctx.admin_headers, json=ctx.gateway.config.to_json()
        )
        assert r.status_code == 200
        assert r.json()["generation"] == 1


@pytest.mark.anyio
async def test_put_with_stale_generation_conflicts(make_gateway_app):
    async with make_gateway_app() as ctx:
        base = ctx.gateway.config.to_json()
        r = await ctx.client.put(
            "/control/v1/config",
            headers=ctx.admin_headers,
            params={"expected_generation": 0},
            json=base,
        )
        assert r.status_code == 200
        stale = ctx.gateway.config.to_json()
        stale["defaults"]["aux_model"] = "req-main"
        r = await ctx.client.put(
            "/control/v1/config",
            headers=ctx.admin_headers,
            params={"expected_generation": 0},
            json=stale,
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "generation_conflict"
        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        assert r.json()["generation"] == 1
        assert r.json()["config"]["defaults"]["aux_model"] == "req-aux"


@pytest.mark.anyio
async def test_put_without_generation_is_unconditional(make_gateway_app):
    async with make_gateway_app() as ctx:
        r = await ctx.client.put(
            "/control/v1/config", headers=ctx.admin_headers, json=ctx.gateway.config.to_json()
        )
        assert r.status_code == 200
        r = await ctx.client.put(
            "/control/v1/config", headers=ctx.admin_headers, json=ctx.gateway.config.to_json()
        )
        assert r.status_code == 200  # legacy last-write-wins client


@pytest.mark.anyio
async def test_import_races_with_config_apply(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        await _persist_base(ctx)
        payload = {
            "operation_id": OP_ID,
            "plan": wire_plan(tmp_path, current=ctx.gateway.config),
        }
        r = await ctx.client.post("/control/v1/imports", headers=ctx.admin_headers, json=payload)
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["status"] == "committed"
        assert body["applied"] is True
        assert TOKEN not in r.text

        # the catalog is live immediately
        r = await ctx.client.get("/control/v1/config", headers=ctx.admin_headers)
        assert r.json()["config"]["providers"]["api-anthropic-test"]["auth"] == "bearer"

        # an apply still carrying the pre-import generation conflicts
        stale = json.loads(json.dumps(r.json()["config"]))
        stale["generation"] = 1
        r = await ctx.client.put(
            "/control/v1/config",
            headers=ctx.admin_headers,
            params={"expected_generation": 1},
            json=stale,
        )
        assert r.status_code == 409


# ---------------------------------------------------------------- auth & sanitization


@pytest.mark.anyio
async def test_import_requires_admin_token(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        payload = {"operation_id": OP_ID, "plan": wire_plan(tmp_path, current=ctx.gateway.config)}
        r = await ctx.client.post("/control/v1/imports", json=payload)
        assert r.status_code == 401
        created = await ctx.client.post(
            "/control/v1/instances", headers=ctx.admin_headers, json={}
        )
        instance_token = created.json()["token"]
        r = await ctx.client.post(
            "/control/v1/imports",
            headers={"authorization": f"Bearer {instance_token}"},
            json=payload,
        )
        assert r.status_code == 403
        r = await ctx.client.get(f"/control/v1/operations/{OP_ID}")
        assert r.status_code == 401


@pytest.mark.anyio
async def test_import_response_is_sanitized(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        await _persist_base(ctx)
        payload = {"operation_id": OP_ID, "plan": wire_plan(tmp_path, current=ctx.gateway.config)}
        r = await ctx.client.post("/control/v1/imports", headers=ctx.admin_headers, json=payload)
        assert TOKEN not in r.text
        assert r.status_code == 200, r.text
        assert r.json()["credential_id"].startswith("cred_")


@pytest.mark.anyio
async def test_import_body_size_limit(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        plan = wire_plan(tmp_path, current=ctx.gateway.config)
        plan["ignored"] = ["x" * 300_000]
        r = await ctx.client.post(
            "/control/v1/imports", headers=ctx.admin_headers, json={"plan": plan}
        )
        assert r.status_code == 413
        assert r.json()["error"]["code"] == "request_too_large"


# ---------------------------------------------------------------- operation query & retries


@pytest.mark.anyio
async def test_same_operation_retry_and_plan_mismatch(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        await _persist_base(ctx)
        payload = {"operation_id": OP_ID, "plan": wire_plan(tmp_path, current=ctx.gateway.config)}
        r = await ctx.client.post("/control/v1/imports", headers=ctx.admin_headers, json=payload)
        assert r.json()["status"] == "committed"
        first_credential = r.json()["credential_id"]

        r = await ctx.client.post("/control/v1/imports", headers=ctx.admin_headers, json=payload)
        body = r.json()
        assert body["duplicate"] is True
        assert body["credential_id"] == first_credential
        assert list((tmp_path / "credentials").iterdir()) == [tmp_path / "credentials" / first_credential]

        other = wire_plan(tmp_path, current=ctx.gateway.config)
        other["base_url"] = "https://other.example"
        other["provider_id"] = "other"
        r = await ctx.client.post(
            "/control/v1/imports",
            headers=ctx.admin_headers,
            json={"operation_id": OP_ID, "plan": other},
        )
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "import_plan_mismatch"


@pytest.mark.anyio
async def test_operation_query(make_gateway_app, tmp_path):
    async with make_gateway_app() as ctx:
        await _persist_base(ctx)
        r = await ctx.client.get(f"/control/v1/operations/{OP_ID}", headers=ctx.admin_headers)
        assert r.status_code == 404
        payload = {"operation_id": OP_ID, "plan": wire_plan(tmp_path, current=ctx.gateway.config)}
        await ctx.client.post("/control/v1/imports", headers=ctx.admin_headers, json=payload)
        r = await ctx.client.get(f"/control/v1/operations/{OP_ID}", headers=ctx.admin_headers)
        body = r.json()
        assert body["operation_id"] == OP_ID
        assert body["status"] == "committed"
        assert body["generation"] == 2
        assert TOKEN not in r.text
