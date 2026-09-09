from __future__ import annotations

import json

import pytest

from qingniao.config import EMPTY_CONFIG, ConfigStore, validate_config
from qingniao.errors import ApiError


def base_config() -> dict:
    return {
        "providers": {
            "prov-p": {
                "base_url": "http://127.0.0.1:9101/",
                "credential_env": "QING_TEST_P",
                "auth": "bearer",
            },
            "prov-q": {
                "base_url": "https://api.example.com",
                "credential_env": "QING_TEST_Q",
                "auth": "x-api-key",
            },
        },
        "models": {
            "model-p": {"provider": "prov-p", "upstream_model": "vendor/p"},
            "model-q": {"provider": "prov-q", "upstream_model": "vendor/q"},
        },
        "defaults": {
            "model": "req-main",
            "aux_model": "req-aux",
            "routes": {"req-main": "model-p", "req-aux": "model-p"},
        },
    }


def problems_for(mutate) -> list[str]:
    data = base_config()
    mutate(data)
    with pytest.raises(Exception) as excinfo:
        validate_config(data)
    assert isinstance(excinfo.value, ApiError) or hasattr(excinfo.value, "problems")
    if hasattr(excinfo.value, "problems"):
        return [f"{p}: {m}" for p, m in excinfo.value.problems]
    return [excinfo.value.message]


def test_valid_config_normalizes_trailing_slash():
    config = validate_config(base_config())
    assert config.providers["prov-p"].base_url == "http://127.0.0.1:9101"
    assert config.defaults.routes["req-main"] == "model-p"


def test_rejects_userinfo_query_fragment_and_bad_scheme():
    msgs = problems_for(lambda d: d["providers"]["prov-p"].update(base_url="http://user:pw@127.0.0.1:9101"))
    assert any("userinfo" in m for m in msgs)
    msgs = problems_for(lambda d: d["providers"]["prov-p"].update(base_url="http://127.0.0.1:9101/?x=1"))
    assert any("query" in m for m in msgs)
    msgs = problems_for(lambda d: d["providers"]["prov-p"].update(base_url="ftp://127.0.0.1"))
    assert any("scheme" in m for m in msgs)


def test_rejects_unknown_references_and_bad_enums():
    msgs = problems_for(lambda d: d["models"]["model-p"].update(provider="prov-missing"))
    assert any("unknown provider" in m for m in msgs)
    msgs = problems_for(lambda d: d["defaults"]["routes"].update({"req-main": "model-missing"}))
    assert any("unknown destination" in m for m in msgs)
    msgs = problems_for(lambda d: d["providers"]["prov-p"].update(auth="basic"))
    assert any("auth" in m for m in msgs)
    msgs = problems_for(lambda d: d["providers"]["prov-p"].update(credential_env="1BAD"))
    assert any("credential_env" in m for m in msgs)


def test_rejects_malformed_urls_and_invalid_ports():
    for bad in ("http://[invalid", "http://localhost:not-a-port", "http://localhost:70000"):
        msgs = problems_for(lambda d, b=bad: d["providers"]["prov-p"].update(base_url=b))
        assert any("base_url" in m for m in msgs), bad


def test_credential_env_rejects_trailing_newline():
    msgs = problems_for(lambda d: d["providers"]["prov-p"].update(credential_env="FIXTURE_KEY\n"))
    assert any("credential_env" in m for m in msgs)
    msgs = problems_for(lambda d: d["providers"]["prov-p"].update(credential_env="FIXTURE_KEY tail"))
    assert any("credential_env" in m for m in msgs)


def test_defaults_models_must_exist_in_routes():
    msgs = problems_for(lambda d: d["defaults"].update(model="req-not-routed"))
    assert any("must exist in defaults.routes" in m for m in msgs)


def test_unknown_top_level_field_rejected():
    msgs = problems_for(lambda d: d.update(extra=1))
    assert any("unknown top-level" in m for m in msgs)


def test_store_load_missing_returns_empty(tmp_path):
    store = ConfigStore(tmp_path / "config.json")
    assert store.load() == EMPTY_CONFIG


def test_store_apply_persists_and_rejects_atomically(tmp_path):
    path = tmp_path / "sub" / "config.json"
    store = ConfigStore(path)
    store.apply(base_config())
    good = json.loads(path.read_text())

    data = base_config()
    data["providers"]["prov-p"]["base_url"] = "http://user@127.0.0.1:9101"
    with pytest.raises(ApiError) as excinfo:
        store.apply(data)
    assert excinfo.value.code == "invalid_config"
    assert json.loads(path.read_text()) == good
    assert store.load().providers["prov-p"].base_url == "http://127.0.0.1:9101"


def test_persisted_file_is_0600(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.apply(base_config())
    mode = path.stat().st_mode & 0o777
    assert mode == 0o600


def _v1_config(**extra: object) -> dict:
    data: dict = {"schema_version": 1, "generation": 1}
    data.update(base_config())
    data.update(extra)
    return data


def _private_v1_config() -> dict:
    data = _v1_config()
    provider = data["providers"]["prov-p"]
    del provider["credential_env"]
    provider["credential_id"] = "cred_" + "0" * 32
    return data


def problems_for_dict(data: dict) -> list[str]:
    with pytest.raises(Exception) as excinfo:
        validate_config(data)
    assert isinstance(excinfo.value, ApiError) or hasattr(excinfo.value, "problems")
    if hasattr(excinfo.value, "problems"):
        return [f"{p}: {m}" for p, m in excinfo.value.problems]
    return [excinfo.value.message]


def test_legacy_file_loads_read_only_with_zero_writes(tmp_path):
    path = tmp_path / "config.json"
    legacy_text = json.dumps(base_config(), indent=2) + "\n"
    path.write_text(legacy_text, encoding="utf-8")
    store = ConfigStore(path)
    config = store.load()
    assert config.providers["prov-p"].credential_env == "QING_TEST_P"
    assert config.providers["prov-p"].credential_id is None
    assert config.generation == 0
    assert config.last_operation is None
    assert path.read_text(encoding="utf-8") == legacy_text


def test_legacy_apply_upgrades_to_v1_starting_at_generation_1(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    config = store.apply(base_config())
    persisted = json.loads(path.read_text())
    assert persisted["schema_version"] == 1
    assert persisted["generation"] == 1
    assert persisted["last_operation"] is None
    assert persisted["providers"]["prov-p"]["credential_env"] == "QING_TEST_P"
    assert config.generation == 1
    store.apply(base_config())
    assert json.loads(path.read_text())["generation"] == 2


def test_v1_config_round_trips_through_validate_and_load(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.apply(_private_v1_config())
    persisted = json.loads(path.read_text())
    assert persisted["providers"]["prov-p"]["credential_id"] == "cred_" + "0" * 32
    assert "credential_env" not in persisted["providers"]["prov-p"]
    loaded = store.load()
    assert loaded.providers["prov-p"].credential_id == "cred_" + "0" * 32
    assert loaded.providers["prov-p"].credential_env is None
    assert loaded.generation == 1


def test_v1_provider_requires_exactly_one_credential_source():
    both = _v1_config()
    both["providers"]["prov-p"]["credential_id"] = "cred_" + "0" * 32
    msgs = problems_for_dict(both)
    assert any("exactly one of credential_env or credential_id" in m for m in msgs)
    neither = _private_v1_config()
    del neither["providers"]["prov-p"]["credential_id"]
    msgs = problems_for_dict(neither)
    assert any("exactly one of credential_env or credential_id" in m for m in msgs)


def test_v1_credential_id_rejects_traversal_and_bad_format():
    for bad in ("../secrets", "cred_short", "cred_" + "0" * 31, "cred_" + "G" * 32, 7):
        data = _private_v1_config()
        data["providers"]["prov-p"]["credential_id"] = bad
        msgs = problems_for_dict(data)
        assert any("credential_id" in m for m in msgs), bad


def test_v1_generation_required_positive_integer():
    data = _v1_config()
    del data["generation"]
    msgs = problems_for_dict(data)
    assert any("generation" in m for m in msgs)
    for bad in (0, -1, "3", 1.5, True):
        data = _v1_config()
        data["generation"] = bad
        msgs = problems_for_dict(data)
        assert any("generation" in m for m in msgs), bad


def test_v1_last_operation_charset_restricted():
    data = _v1_config()
    data["last_operation"] = "op_" + "1" * 32
    assert validate_config(data).last_operation == "op_" + "1" * 32
    data["last_operation"] = None
    assert validate_config(data).last_operation is None
    for bad in ("not-an-op", "op_../x", "op_" + "Z" * 32, 5):
        data = _v1_config()
        data["last_operation"] = bad
        msgs = problems_for_dict(data)
        assert any("last_operation" in m for m in msgs), bad


def test_legacy_config_cannot_carry_v1_fields():
    data = base_config()
    data["generation"] = 1
    msgs = problems_for_dict(data)
    assert any("unknown top-level" in m for m in msgs)


def test_unknown_future_schema_version_rejected_without_overwrite(tmp_path):
    path = tmp_path / "config.json"
    data = _v1_config()
    data["schema_version"] = 99
    future_text = json.dumps(data, indent=2) + "\n"
    path.write_text(future_text, encoding="utf-8")
    with pytest.raises(ApiError) as excinfo:
        ConfigStore(path).load()
    assert excinfo.value.code == "config_load_failed"
    assert any("schema version" in e["message"] for e in excinfo.value.details["errors_"])
    assert path.read_text(encoding="utf-8") == future_text
    with pytest.raises(Exception) as direct:
        validate_config(data)
    assert hasattr(direct.value, "problems")


def test_schema_version_must_be_plain_integer():
    for bad in ("1", 1.5, True):
        data = _v1_config()
        data["schema_version"] = bad
        msgs = problems_for_dict(data)
        assert any("schema_version" in m for m in msgs), bad


def test_apply_preserves_last_operation_and_increments_generation(tmp_path):
    path = tmp_path / "config.json"
    store = ConfigStore(path)
    store.apply(base_config())
    data = json.loads(path.read_text())
    data["last_operation"] = "op_" + "2" * 32
    store.apply(data)
    persisted = json.loads(path.read_text())
    assert persisted["last_operation"] == "op_" + "2" * 32
    assert persisted["generation"] == 2


def test_apply_persist_failure_returns_stable_api_error(tmp_path, monkeypatch):
    import os

    store = ConfigStore(tmp_path / "config.json")
    store.apply(base_config())
    persisted = (tmp_path / "config.json").read_text()

    def failing_replace(src, dst):
        raise OSError("injected replace failure")

    monkeypatch.setattr(os, "replace", failing_replace)
    changed = base_config()
    changed["models"]["model-p"]["upstream_model"] = "vendor/p2"
    with pytest.raises(ApiError) as excinfo:
        store.apply(changed)
    assert excinfo.value.code == "config_persist_failed"
    assert excinfo.value.status_code == 500
    monkeypatch.undo()
    assert (tmp_path / "config.json").read_text() == persisted
    assert store.load().models["model-p"].upstream_model == "vendor/p"
