from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest

from qingniao.claude_source import parse_claude_settings
from qingniao.config import atomic_write_json, validate_config
from qingniao.credentials import CredentialStore
from qingniao.importing import ImportDecisions, build_plan, source_digest
from tests.conftest import sse_event

OLD_KEY = "sk-ant-api03-SYNTHETIC-OLD-KEY"
NEW_KEY = "sk-ant-api03-SYNTHETIC-NEW-KEY"
BASE_URL = "https://rotation.example"


def _write_private_config(tmp_path: Path, secret: str, *, provider_id: str = "rotation-example") -> str:
    creds = CredentialStore(tmp_path)
    cred_id = creds.create(secret)
    config = validate_config(
        {
            "schema_version": 1,
            "generation": 1,
            "providers": {
                provider_id: {"base_url": BASE_URL, "credential_id": cred_id, "auth": "bearer"}
            },
            "models": {
                "claude-main": {"provider": provider_id, "upstream_model": "claude-main"},
                "claude-aux": {"provider": provider_id, "upstream_model": "claude-aux"},
            },
            "defaults": {
                "model": "claude-main",
                "aux_model": "claude-aux",
                "routes": {"claude-main": "claude-main", "claude-aux": "claude-aux"},
            },
        }
    )
    atomic_write_json(tmp_path / "config.json", config.to_json())
    return cred_id


def _rotation_plan(tmp_path: Path, current, *, secret: str = NEW_KEY):
    source = {
        "env": {
            "ANTHROPIC_BASE_URL": BASE_URL,
            "ANTHROPIC_AUTH_TOKEN": secret,
            "ANTHROPIC_MODEL": "claude-main",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux",
        }
    }
    raw = json.dumps(source).encode()
    parsed = parse_claude_settings(raw, source=Path("/tmp/settings.json"))
    return build_plan(
        parsed,
        source_digest=source_digest(raw),
        current=current,
        credential_store=CredentialStore(tmp_path),
        decisions=ImportDecisions(conflict="update"),
        operation_id="op_" + "5" * 32,
    )


@pytest.fixture
def private_state(tmp_path):
    old_cred = _write_private_config(tmp_path, OLD_KEY)
    return tmp_path, old_cred


def _config_for(tmp_path: Path, base_url: str) -> dict:
    config = json.loads((tmp_path / "config.json").read_text())
    config["providers"]["rotation-example"]["base_url"] = base_url
    return config


@pytest.mark.anyio
async def test_status_shows_private_reference_kind_and_id(private_state, make_gateway_app):
    tmp_path, old_cred = private_state
    async with make_gateway_app(config=json.loads((tmp_path / "config.json").read_text())) as ctx:
        instance, _ = ctx.gateway.create_instance()
        route = ctx.gateway.instance_status(instance)["routes"]["claude-main"]
        assert route["credential_id"] == old_cred
        assert "credential_env" not in route


@pytest.mark.anyio
async def test_rotation_keeps_old_snapshots_new_instances_use_new_key(private_state, make_gateway_app):
    tmp_path, old_cred = private_state
    async with make_gateway_app(config=json.loads((tmp_path / "config.json").read_text())) as ctx:
        a, _ = ctx.gateway.create_instance(label="a")
        b, _ = ctx.gateway.create_instance(label="b")
        result = ctx.gateway.submit_import(_rotation_plan(tmp_path, ctx.gateway.config))
        assert result["status"] == "committed"
        new_cred = result["credential_id"]
        assert new_cred != old_cred
        assert CredentialStore(tmp_path).read(old_cred) == OLD_KEY  # old version kept

        for target in (a, b):
            status = ctx.gateway.instance_status(ctx.gateway.get_instance(target.id))
            assert status["routes"]["claude-main"]["credential_id"] == old_cred

        c, _ = ctx.gateway.create_instance(label="c")
        assert (
            ctx.gateway.instance_status(c)["routes"]["claude-main"]["credential_id"] == new_cred
        )

        # explicit switch of one instance only
        ctx.gateway.set_route(a.id, "claude-main", "claude-main", a.revision)
        assert (
            ctx.gateway.instance_status(ctx.gateway.get_instance(a.id))
            .get("routes")["claude-main"]["credential_id"]
            == new_cred
        )
        assert (
            ctx.gateway.instance_status(ctx.gateway.get_instance(b.id))["routes"]["claude-main"][
                "credential_id"
            ]
            == old_cred
        )


@pytest.mark.anyio
async def test_revoked_old_key_fails_without_switching(private_state, make_gateway_app, upstream_factory):
    tmp_path, old_cred = private_state
    upstream = upstream_factory((401, {"error": "revoked"}))
    async with make_gateway_app(
        config=_config_for(tmp_path, f"http://127.0.0.1:{upstream.server_address[1]}")
    ) as ctx:
        instance, token = ctx.gateway.create_instance()
        # a newer key exists in the catalog, but the instance keeps its snapshot
        result = ctx.gateway.submit_import(
            _rotation_plan(tmp_path, ctx.gateway.config, secret=NEW_KEY)
        )
        assert result["status"] == "committed"

        r = await ctx.client.post(
            "/v1/messages",
            headers={"authorization": f"Bearer {token}"},
            json={"model": "claude-main"},
        )
        assert r.status_code == 401  # honest failure, no fallback credential
        assert len(upstream.requests) == 1
        assert upstream.requests[0]["headers"].get("authorization") == f"Bearer {OLD_KEY}"


@pytest.mark.anyio
async def test_in_flight_stream_survives_rotation(private_state, make_gateway_app, upstream_factory):
    tmp_path, old_cred = private_state
    upstream = upstream_factory()
    gate = threading.Event()
    upstream.sse = [sse_event("message_start", {}), sse_event("message_stop", {})]
    upstream.chunk_gates = [gate, None]
    async with make_gateway_app(
        config=_config_for(tmp_path, f"http://127.0.0.1:{upstream.server_address[1]}")
    ) as ctx:
        instance, token = ctx.gateway.create_instance()
        task = asyncio.create_task(
            ctx.client.post(
                "/v1/messages",
                headers={"authorization": f"Bearer {token}"},
                json={"model": "claude-main"},
            )
        )
        deadline = time.monotonic() + 5
        while not upstream.requests and time.monotonic() < deadline:
            await asyncio.sleep(0.01)
        assert upstream.requests, "request never reached the upstream"
        result = ctx.gateway.submit_import(
            _rotation_plan(tmp_path, ctx.gateway.config, secret=NEW_KEY)
        )
        assert result["status"] == "committed"
        gate.set()
        r = await asyncio.wait_for(task, timeout=10)
        assert r.status_code == 200
        assert b"message_stop" in r.content
        assert len(upstream.requests) == 1
        assert upstream.requests[0]["headers"].get("authorization") == f"Bearer {OLD_KEY}"
