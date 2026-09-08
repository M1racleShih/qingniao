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
