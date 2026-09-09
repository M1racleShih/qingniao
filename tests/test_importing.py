from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from qingniao import errors
from qingniao.claude_source import parse_claude_settings
from qingniao.config import ConfigStore, validate_config
from qingniao.credentials import CredentialStore
from qingniao.importing import (
    _derive_provider_id,
    ImportDecisions,
    PlanError,
    build_plan,
    plan_preview,
    source_digest,
    verify_source_unchanged,
)

TOKEN = "sk-ant-api03-SYNTHETIC-TOKEN"
API_KEY = "sk-ant-api03-SYNTHETIC-KEY"
BASE_URL = "https://api.anthropic.test"
OP_ID = "op_" + "1" * 32


def settings_payload(**env_overrides) -> dict:
    env = {
        "ANTHROPIC_BASE_URL": BASE_URL,
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_MODEL": "claude-main",
        "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-opus-upstream",
        "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-upstream",
        "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-haiku-upstream",
        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux-upstream",
    }
    env.update(env_overrides)
    return {"env": env}


def parsed(payload: dict, source: Path = Path("/tmp/settings.json")):
    return parse_claude_settings(json.dumps(payload).encode(), source=source)


def build(payload: dict, current, store, *, decisions=None, digest=None, operation_id=OP_ID, **kw):
    return build_plan(
        parsed(payload),
        source_digest=digest or source_digest(json.dumps(payload).encode()),
        current=current,
        credential_store=store,
        decisions=decisions or ImportDecisions(),
        operation_id=operation_id,
        **kw,
    )


@pytest.fixture
def store(tmp_path):
    return CredentialStore(tmp_path)


@pytest.fixture
def current(tmp_path):
    return ConfigStore(tmp_path / "config.json").load()


# ---------------------------------------------------------------- happy path


def test_full_plan_with_exact_mapping_and_defaults(current, store):
    plan = build(settings_payload(), current, store)
    assert plan.status == "apply"
    assert plan.base_url == BASE_URL
    assert plan.auth == "bearer"
    assert plan.secret == TOKEN
    assert plan.reuse_credential_id is None
    ids = {m.model_id for m in plan.models}
    assert ids == {"claude-main", "claude-aux-upstream", "claude-opus-upstream",
                   "claude-sonnet-upstream", "claude-haiku-upstream"}
    assert all(m.model_id == m.upstream_model for m in plan.models)
    tier_routes = {k: v for k, v in plan.defaults.routes.items() if k in ("opus", "sonnet", "haiku")}
    assert tier_routes == {
        "opus": "claude-opus-upstream",
        "sonnet": "claude-sonnet-upstream",
        "haiku": "claude-haiku-upstream",
    }
    assert plan.defaults.model == "claude-main"
    assert plan.defaults.aux_model == "claude-aux-upstream"
    assert plan.defaults.routes["claude-main"] == "claude-main"
    assert plan.defaults.routes["claude-aux-upstream"] == "claude-aux-upstream"


def test_plan_preview_and_repr_carry_no_secret(current, store):
    plan = build(settings_payload(), current, store)

    def walk(value):
        if isinstance(value, dict):
            for v in value.values():
                walk(v)
        elif isinstance(value, list):
            for v in value:
                walk(v)

    preview = plan_preview(plan)
    walk(preview)
    assert TOKEN not in json.dumps(preview)
    assert TOKEN not in repr(plan)
    assert preview["status"] == "apply"
    assert preview["provider"]["credential"] == "private file (created on apply)"
    by_id = {m["id"]: m for m in preview["models"]}
    assert by_id["claude-opus-upstream"]["upstream_model"] == "claude-opus-upstream"
    assert by_id["claude-opus-upstream"]["request_keys"] == ["opus"]


def test_building_a_plan_writes_nothing(current, store, tmp_path):
    build(settings_payload(), current, store)
    assert not (tmp_path / "config.json").exists()
    assert not (tmp_path / "credentials").exists()


# ---------------------------------------------------------------- decisions


def test_dual_auth_requires_explicit_choice(current, store):
    payload = settings_payload(ANTHROPIC_API_KEY=API_KEY)
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store)
    assert excinfo.value.code == "auth_choice_required"
    plan = build(payload, current, store, decisions=ImportDecisions(auth_kind="x-api-key"))
    assert plan.auth == "x-api-key"
    assert plan.secret == API_KEY


def test_primary_model_conflict_requires_explicit_choice(current, store):
    payload = settings_payload()
    payload["model"] = "claude-top-level"
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store)
    assert excinfo.value.code == "primary_choice_required"
    plan = build(payload, current, store, decisions=ImportDecisions(primary_source="settings.model"))
    assert plan.defaults.model == "claude-top-level"


def test_missing_aux_keeps_provider_only_unless_explicitly_same(current, store):
    payload = settings_payload(CLAUDE_CODE_SUBAGENT_MODEL=None)
    del payload["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]
    plan = build(payload, current, store)
    assert plan.status == "provider_only"
    assert plan.defaults is None
    assert any("aux" in n for n in plan.incomplete)
    plan = build(payload, current, store, decisions=ImportDecisions(aux_same_as_primary=True))
    assert plan.status == "apply"
    assert plan.defaults.aux_model == "claude-main"


def test_missing_base_url_or_auth_blocks_plan(current, store):
    payload = settings_payload(ANTHROPIC_BASE_URL=None)
    del payload["env"]["ANTHROPIC_BASE_URL"]
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store)
    assert excinfo.value.code == "base_url_missing"
    payload = settings_payload(ANTHROPIC_AUTH_TOKEN=None)
    del payload["env"]["ANTHROPIC_AUTH_TOKEN"]
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store)
    assert excinfo.value.code == "auth_missing"
    assert TOKEN not in excinfo.value.message


# ---------------------------------------------------------------- dedup & conflicts


def _current_with_provider(tmp_path, provider: dict, models: dict | None = None, defaults: dict | None = None):
    data = {
        "schema_version": 1,
        "generation": 3,
        "providers": {"existing": provider},
        "models": models or {},
        "defaults": defaults or {"model": None, "aux_model": None, "routes": {}},
    }
    return validate_config(data)


def test_repeat_import_of_same_connection_is_unchanged(tmp_path, store):
    cred_id = store.create(TOKEN)
    current = _current_with_provider(
        tmp_path,
        {"base_url": BASE_URL, "credential_id": cred_id, "auth": "bearer"},
        models={
            "claude-main": {"provider": "existing", "upstream_model": "claude-main"},
            "claude-aux-upstream": {"provider": "existing", "upstream_model": "claude-aux-upstream"},
            "claude-opus-upstream": {"provider": "existing", "upstream_model": "claude-opus-upstream"},
            "claude-sonnet-upstream": {"provider": "existing", "upstream_model": "claude-sonnet-upstream"},
            "claude-haiku-upstream": {"provider": "existing", "upstream_model": "claude-haiku-upstream"},
        },
        defaults={
            "model": "claude-main",
            "aux_model": "claude-aux-upstream",
            "routes": {
                "claude-main": "claude-main",
                "claude-aux-upstream": "claude-aux-upstream",
                "opus": "claude-opus-upstream",
                "sonnet": "claude-sonnet-upstream",
                "haiku": "claude-haiku-upstream",
            },
        },
    )
    plan = build(settings_payload(), current, store)
    assert plan.status == "unchanged"
    assert plan.reuse_credential_id == cred_id
    assert plan.secret == TOKEN  # in-memory comparison used, no new credential planned


def test_dedup_normalizes_endpoint_and_trailing_slash(tmp_path, store):
    cred_id = store.create(TOKEN)
    current = _current_with_provider(
        tmp_path, {"base_url": BASE_URL + "/", "credential_id": cred_id, "auth": "bearer"}
    )
    # same connection (trailing slash normalized), directory has no models yet:
    # purely additive completion reuses the existing credential without a choice
    plan = build(settings_payload(), current, store)
    assert plan.status == "apply"
    assert plan.provider_id == "existing"
    assert plan.reuse_credential_id == cred_id
    assert plan.secret == TOKEN


def test_env_provider_with_same_url_is_not_equal(tmp_path, store):
    current = _current_with_provider(
        tmp_path, {"base_url": BASE_URL, "credential_env": "SOME_ENV", "auth": "bearer"}
    )
    plan = build(settings_payload(), current, store)
    assert plan.status == "apply"
    assert plan.provider_id != "existing"


def test_same_id_different_content_requires_choice_then_skip_update_new(tmp_path, store):
    current = _current_with_provider(
        tmp_path,
        {
            "base_url": "https://other.example",
            "credential_env": "OTHER",
            "auth": "bearer",
        },
    )
    # rebuild so the existing provider occupies the id derived from the import host
    data = current.to_json()
    data["providers"]["api-anthropic-test"] = data["providers"].pop("existing")
    current = validate_config(data)
    payload = settings_payload()
    payload["env"]["ANTHROPIC_BASE_URL"] = "https://api.anthropic.test"
    proposed = _derive_provider_id("https://api.anthropic.test")
    assert proposed == "api-anthropic-test"
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store)
    assert excinfo.value.code == "conflict_choice_required"

    plan = build(payload, current, store, decisions=ImportDecisions(conflict="skip"))
    assert plan.status == "skipped"

    plan = build(payload, current, store, decisions=ImportDecisions(conflict="update"))
    assert plan.status == "apply"
    assert plan.provider_id == proposed

    plan = build(
        payload,
        current,
        store,
        decisions=ImportDecisions(conflict="new", new_provider_id="fresh-id"),
    )
    assert plan.status == "apply"
    assert plan.provider_id == "fresh-id"


def test_conflicting_model_ids_get_unique_ids_under_new(tmp_path, store):
    current = _current_with_provider(
        tmp_path,
        {"base_url": "https://other.example", "credential_env": "OTHER", "auth": "bearer"},
        models={"claude-main": {"provider": "existing", "upstream_model": "other-main"}},
    )
    plan = build(
        settings_payload(),
        current,
        store,
        decisions=ImportDecisions(conflict="new", new_provider_id="fresh-id"),
    )
    assert plan.status == "apply"
    ids = [m.model_id for m in plan.models]
    assert "claude-main" not in ids
    assert any(i.startswith("claude-main--") for i in ids)
    route_to = {m.upstream_model: m.model_id for m in plan.models}
    assert plan.defaults.routes["claude-main"] == route_to["claude-main"]


# ---------------------------------------------------------------- source recheck


def test_source_recheck_detects_changes():
    raw = json.dumps(settings_payload()).encode()
    digest = source_digest(raw)
    verify_source_unchanged(digest, raw)
    raw2 = json.dumps(settings_payload(ANTHROPIC_MODEL="claude-changed")).encode()
    with pytest.raises(PlanError) as excinfo:
        verify_source_unchanged(digest, raw2)
    assert excinfo.value.code == "source_changed"
    assert TOKEN not in excinfo.value.message
    assert hashlib.sha256(raw).hexdigest() == digest


# ---------------------------------------------------------------- plan metadata


def test_plan_records_expected_generation_and_source(current, store):
    plan = build(settings_payload(), current, store)
    assert plan.expected_generation == current.generation
    assert plan.operation_id == OP_ID


def test_unsupported_auth_blocks_plan_with_guidance(current, store):
    payload = settings_payload()
    payload["apiKeyHelper"] = "/bin/false"
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store)
    assert excinfo.value.code == "auth_unsupported"
    assert "api-key-helper" in excinfo.value.message or "apiKeyHelper" in excinfo.value.message


def test_gateway_token_secret_is_rejected(current, store):
    payload = settings_payload(ANTHROPIC_AUTH_TOKEN="qn_" + "a" * 43)
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store)
    assert excinfo.value.code == "self_reference"
    assert "qn_" not in excinfo.value.message or "instance" in excinfo.value.message


def test_gateway_endpoint_self_reference_is_rejected(current, store):
    payload = settings_payload(ANTHROPIC_BASE_URL="http://localhost:9400/")
    with pytest.raises(PlanError) as excinfo:
        build(payload, current, store, gateway_endpoint="http://127.0.0.1:9400")
    assert excinfo.value.code == "self_reference"


def test_plan_preview_mentions_missing_aux_guidance(current, store):
    payload = settings_payload(CLAUDE_CODE_SUBAGENT_MODEL=None)
    del payload["env"]["CLAUDE_CODE_SUBAGENT_MODEL"]
    preview = plan_preview(build(payload, current, store))
    assert preview["status"] == "provider_only"
    assert preview["defaults"] is None
    assert any("aux" in n for n in preview["incomplete"])
