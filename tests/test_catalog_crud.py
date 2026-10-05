"""Unit tests for entry-level catalog editing and the credential catalog
schema extension (format-v1 ``credentials`` section)."""

from __future__ import annotations

import json

import pytest

from qingniao import catalog as cat
from qingniao.config import ConfigStore, validate_config


def _config(providers=None, models=None, defaults=None, credentials=None) -> dict:
    return {
        "schema_version": 1,
        "generation": 3,
        "last_operation": None,
        "providers": providers or {},
        "models": models or {},
        "defaults": defaults or {"model": None, "aux_model": None, "routes": {}},
        "credentials": credentials or {},
    }


PRIV = "cred_" + "a" * 32


def _full_config() -> dict:
    return _config(
        providers={
            "p1": {"base_url": "https://one.example", "credential_env": "ONE", "auth": "bearer"},
            "p2": {"base_url": "https://two.example", "credential_id": "shared", "auth": "x-api-key"},
            "p3": {"base_url": "https://three.example", "credential_id": PRIV, "auth": "bearer"},
        },
        models={
            "m1": {"provider": "p1", "upstream_model": "vendor/one"},
            "m2": {"provider": "p2", "upstream_model": "vendor/two"},
        },
        defaults={"model": "r1", "aux_model": "r2", "routes": {"r1": "m1", "r2": "m1"}},
        credentials={
            "shared": {"source": "env", "env": "TWO"},
            PRIV: {"source": "private"},
        },
    )


# ------------------------------------------------------------ config schema


def test_credentials_section_round_trips(tmp_path):
    config = validate_config(_full_config())
    assert config.credentials["shared"].source == "env"
    assert config.credentials["shared"].env == "TWO"
    assert config.credentials[PRIV].source == "private"
    persisted = json.loads(json.dumps(config.to_json()))
    assert persisted["credentials"] == {
        "shared": {"source": "env", "env": "TWO"},
        PRIV: {"source": "private"},
    }
    store = ConfigStore(tmp_path / "config.json")
    store.apply(config.to_json())
    loaded = store.load()
    assert loaded.credentials["shared"].env == "TWO"


def test_empty_credentials_section_is_optional_and_defaults_empty():
    config = _config()
    parsed = validate_config(config)
    assert parsed.credentials == {}
    # an empty catalog is serialized without the credentials key so configs
    # without catalog credentials stay readable by older builds
    assert "credentials" not in parsed.to_json()

def test_credentials_section_serialized_only_when_non_empty():
    config = _config(credentials={"k": {"source": "env", "env": "X"}})
    parsed = validate_config(config)
    assert parsed.to_json()["credentials"] == {"k": {"source": "env", "env": "X"}}
    # removing the last entry drops the section again on write
    empty = _config()
    assert "credentials" not in validate_config(empty).to_json()


def test_legacy_credential_id_without_section_entry_still_valid():
    # import-created private ids are referenced directly, no catalog entry.
    config = _config(
        providers={
            "p": {"base_url": "https://x.example", "credential_id": PRIV, "auth": "bearer"}
        }
    )
    parsed = validate_config(config)
    assert parsed.providers["p"].credential_id == PRIV


@pytest.mark.parametrize(
    "mutate, expected",
    [
        (lambda c: c["credentials"].update({"bad": {"source": "env", "env": "1BAD"}}), "env"),
        (lambda c: c["credentials"].update({"bad": {"source": "magic"}}), "source"),
        (lambda c: c["credentials"].update({PRIV: {"source": "private", "env": "X"}}), "no env field"),
        (lambda c: c["credentials"].update({"not-hex": {"source": "private"}}), "cred_<hex>"),
        (lambda c: c["credentials"].update({"shared": {"source": "env"}}), "env"),
        (lambda c: c["credentials"].update({PRIV: {"source": "env", "env": "X"}}), "reserved for private"),
        (lambda c: c["credentials"].update({"": {"source": "env", "env": "X"}}), "non-empty"),
        (lambda c: c["credentials"].update({"shared": {"source": "env", "env": "TWO", "x": 1}}), "unknown"),
    ],
)
def test_invalid_credential_catalog_entries_rejected(mutate, expected):
    config = _full_config()
    mutate(config)
    with pytest.raises(Exception) as excinfo:
        validate_config(config)
    problems = getattr(excinfo.value, "problems", [("", str(excinfo.value))])
    text = " ".join(f"{p}: {m}" for p, m in problems)
    assert expected in text


@pytest.mark.parametrize(
    "credential_id",
    ["shared", PRIV, "cred_" + "b" * 32],
)
def test_provider_credential_id_accepts_catalog_and_legacy_ids(credential_id):
    config = _config(
        credentials={"shared": {"source": "env", "env": "TWO"}},
        providers={"p": {"base_url": "https://x.example", "credential_id": credential_id, "auth": "bearer"}},
    )
    assert validate_config(config).providers["p"].credential_id == credential_id


def test_provider_credential_id_rejects_unknown_id():
    config = _config(
        providers={"p": {"base_url": "https://x.example", "credential_id": "nope", "auth": "bearer"}}
    )
    with pytest.raises(Exception) as excinfo:
        validate_config(config)
    assert any("credential_id" in p for p, _ in getattr(excinfo.value, "problems", []))


# ---------------------------------------------------------------- catalog.py


def test_provider_and_model_views_match_config_shape():
    config = _full_config()
    p2 = cat.provider_view(config, "p2")
    assert p2 == {"id": "p2", "base_url": "https://two.example", "auth": "x-api-key", "credential_id": "shared"}
    assert cat.provider_view(config, "missing") is None
    m1 = cat.model_view(config, "m1")
    assert m1 == {"id": "m1", "provider": "p1", "upstream_model": "vendor/one"}
    assert cat.model_view(config, "missing") is None


def test_list_shapes():
    config = _full_config()
    assert [p["id"] for p in cat.list_providers(config)] == ["p1", "p2", "p3"]
    assert [m["id"] for m in cat.list_models(config)] == ["m1", "m2"]


def test_referential_integrity_helpers():
    config = _full_config()
    assert cat.provider_referenced_by_models(config, "p1") == ["m1"]
    assert cat.provider_referenced_by_models(config, "p2") == ["m2"]
    assert cat.provider_referenced_by_models(config, "nope") == []
    assert cat.model_referenced_by(config, "m1") == ["defaults.routes.r1", "defaults.routes.r2"]
    assert cat.model_referenced_by(config, "m2") == []
    assert cat.credential_referenced_by(config, "shared") == ["p2"]
    assert cat.credential_referenced_by(config, PRIV) == ["p3"]
    assert cat.credential_referenced_by(config, "nope") == []


def test_model_referenced_by_defaults_and_routes():
    config = _config(
        models={
            "m1": {"provider": "p", "upstream_model": "u1"},
            "m2": {"provider": "p", "upstream_model": "u2"},
        },
        defaults={"model": "m1", "aux_model": "m1", "routes": {"m1": "m1", "k": "m2"}},
    )
    assert cat.model_referenced_by(config, "m1") == [
        "defaults.model",
        "defaults.aux_model",
        "defaults.routes.m1",
    ]
    assert cat.model_referenced_by(config, "m2") == ["defaults.routes.k"]


def test_resolve_credential_target():
    config = _full_config()
    assert cat.resolve_credential_target(config, "shared") == ("env", "TWO")
    assert cat.resolve_credential_target(config, PRIV) == ("private", PRIV)
    legacy = "cred_" + "c" * 32
    assert cat.resolve_credential_target(config, legacy) == ("private", legacy)
    assert cat.resolve_credential_target(config, "nope") is None
    assert cat.credential_label(config, "shared") == "env TWO"
    assert cat.credential_label(config, PRIV) == f"private {PRIV}"


def test_credentials_list_includes_derived_private_entries():
    # a private id referenced by a provider without a section entry is
    # listed as a derived entry so the view stays complete
    legacy = "cred_" + "d" * 32
    config = _config(
        providers={"p": {"base_url": "https://x.example", "credential_id": legacy, "auth": "bearer"}},
        models={"m": {"provider": "p", "upstream_model": "u"}},
        credentials={"shared": {"source": "env", "env": "TWO"}},
    )
    entries = cat.list_credentials(config)
    by_id = {e["id"]: e for e in entries}
    assert by_id["shared"]["source"] == "env"
    assert by_id[legacy]["source"] == "private"
    assert by_id[legacy]["derived"] is True
    assert by_id[legacy]["referenced_by"] == ["p"]
    view = cat.credential_view(config, legacy)
    assert view is not None and view["derived"]
    assert cat.credential_view(config, "missing") is None


def test_validate_edited_config_reuses_schema_rules():
    config = _full_config()
    assert cat.validate_edited_config(config) == []
    bad = json.loads(json.dumps(config))
    bad["providers"]["p1"]["base_url"] = "ftp://nope"
    problems = cat.validate_edited_config(bad)
    assert any("base_url" in p for p, _ in problems)


def test_empty_config_views():
    config = _config()
    assert cat.list_providers(config) == []
    assert cat.list_models(config) == []
    assert cat.list_credentials(config) == []
    assert cat.provider_view(config, "x") is None
