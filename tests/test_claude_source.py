from __future__ import annotations

import json
from pathlib import Path

import pytest

from qingniao.claude_source import (
    AuthCandidate,
    SourceError,
    parse_claude_settings,
    resolve_source,
)

TOKEN = "sk-ant-api03-SYNTHETIC-TOKEN-VALUE"
API_KEY = "sk-ant-api03-SYNTHETIC-APIKEY-VALUE"


def settings_bytes(payload: object) -> bytes:
    return json.dumps(payload).encode("utf-8")


def full_settings() -> dict:
    return {
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.anthropic.test/",
            "ANTHROPIC_AUTH_TOKEN": TOKEN,
            "ANTHROPIC_API_KEY": API_KEY,
            "ANTHROPIC_MODEL": "claude-main-env",
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "claude-upstream-opus",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-upstream-sonnet",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "claude-upstream-haiku",
            "CLAUDE_CODE_SUBAGENT_MODEL": "claude-upstream-aux",
        },
        "model": "claude-main-top",
    }


# ---------------------------------------------------------------- source resolution


def test_explicit_source_beats_config_dir_and_home(tmp_path, monkeypatch):
    explicit = tmp_path / "explicit.json"
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfgdir"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_source(explicit) == explicit


def test_config_dir_beats_home(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfgdir"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_source(None) == tmp_path / "cfgdir" / "settings.json"


def test_home_fallback_without_config_dir(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    assert resolve_source(None) == tmp_path / "home" / ".claude" / "settings.json"


# ---------------------------------------------------------------- parsing


def test_parse_full_settings_extracts_all_candidates():
    parsed = parse_claude_settings(settings_bytes(full_settings()))
    assert parsed.base_url == "https://api.anthropic.test"
    assert [(c.kind, c.env_name) for c in parsed.auth] == [
        ("bearer", "ANTHROPIC_AUTH_TOKEN"),
        ("x-api-key", "ANTHROPIC_API_KEY"),
    ]
    assert [(c.source, c.value) for c in parsed.primary_models] == [
        ("settings.model", "claude-main-top"),
        ("env.ANTHROPIC_MODEL", "claude-main-env"),
    ]
    assert parsed.tier_models == {
        "opus": "claude-upstream-opus",
        "sonnet": "claude-upstream-sonnet",
        "haiku": "claude-upstream-haiku",
    }
    assert parsed.aux_model == "claude-upstream-aux"


def test_parse_single_auth_and_no_models():
    payload = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9301", "ANTHROPIC_API_KEY": API_KEY}}
    parsed = parse_claude_settings(settings_bytes(payload))
    assert [(c.kind, c.env_name) for c in parsed.auth] == [("x-api-key", "ANTHROPIC_API_KEY")]
    assert parsed.primary_models == ()
    assert parsed.tier_models == {}
    assert parsed.aux_model is None


def test_primary_model_from_top_level_only():
    payload = full_settings()
    del payload["env"]["ANTHROPIC_MODEL"]
    parsed = parse_claude_settings(settings_bytes(payload))
    assert [(c.source, c.value) for c in parsed.primary_models] == [
        ("settings.model", "claude-main-top")
    ]


def test_identical_primary_models_keep_both_sources():
    payload = full_settings()
    payload["env"]["ANTHROPIC_MODEL"] = "claude-main-top"
    parsed = parse_claude_settings(settings_bytes(payload))
    assert len(parsed.primary_models) == 2
    assert {c.value for c in parsed.primary_models} == {"claude-main-top"}


def test_empty_auth_values_are_not_candidates_but_noted():
    payload = {"env": {"ANTHROPIC_BASE_URL": "http://127.0.0.1:9301", "ANTHROPIC_AUTH_TOKEN": "  "}}
    parsed = parse_claude_settings(settings_bytes(payload))
    assert parsed.auth == ()
    assert any("ANTHROPIC_AUTH_TOKEN" in note for note in parsed.incomplete)


# ---------------------------------------------------------------- error categories


def test_missing_source_raises_categorized_error_without_echo(tmp_path):
    from qingniao.claude_source import read_source

    with pytest.raises(SourceError) as excinfo:
        read_source(tmp_path / "absent.json")
    assert excinfo.value.code == "source_missing"
    assert "absent.json" not in excinfo.value.message or "absent" in excinfo.value.message
    assert TOKEN not in excinfo.value.message


def test_malformed_json_is_categorized_and_never_echoed():
    with pytest.raises(SourceError) as excinfo:
        parse_claude_settings(b'{"env": {"ANTHROPIC_AUTH_TOKEN": "' + TOKEN.encode() + b'", oops')
    assert excinfo.value.code == "source_invalid_json"
    assert TOKEN not in excinfo.value.message


def test_non_utf8_bytes_are_categorized():
    with pytest.raises(SourceError) as excinfo:
        parse_claude_settings(b"\xff\xfe{")
    assert excinfo.value.code == "source_invalid_json"


def test_non_object_json_rejected():
    with pytest.raises(SourceError) as excinfo:
        parse_claude_settings(b'["env"]')
    assert excinfo.value.code == "source_not_object"


def test_wrong_field_types_are_categorized():
    with pytest.raises(SourceError) as excinfo:
        parse_claude_settings(settings_bytes({"env": "not-an-object"}))
    assert excinfo.value.code == "field_invalid"
    with pytest.raises(SourceError) as excinfo:
        parse_claude_settings(settings_bytes({"env": {"ANTHROPIC_BASE_URL": 5}}))
    assert excinfo.value.code == "field_invalid"
    with pytest.raises(SourceError) as excinfo:
        parse_claude_settings(settings_bytes({"model": ["a"], "env": {}}))
    assert excinfo.value.code == "field_invalid"


@pytest.mark.parametrize(
    "url",
    [
        "https://user:pw@api.anthropic.test",
        "https://api.anthropic.test/?q=1",
        "https://api.anthropic.test/#frag",
        "ftp://api.anthropic.test",
        "not a url",
    ],
)
def test_invalid_base_url_rejected_categorically(url):
    payload = {"env": {"ANTHROPIC_BASE_URL": url, "ANTHROPIC_AUTH_TOKEN": TOKEN}}
    with pytest.raises(SourceError) as excinfo:
        parse_claude_settings(settings_bytes(payload))
    assert excinfo.value.code == "base_url_invalid"
    assert "userinfo" in excinfo.value.message or "query" in excinfo.value.message or "scheme" in excinfo.value.message or "malformed" in excinfo.value.message or "host" in excinfo.value.message


# ---------------------------------------------------------------- unsupported auth & ignored fields


def test_api_key_helper_marked_unsupported_and_never_executed():
    payload = full_settings()
    payload["apiKeyHelper"] = "/usr/local/bin/fetch-key.sh"
    parsed = parse_claude_settings(settings_bytes(payload))
    assert "api-key-helper" in parsed.unsupported_auth
    assert parsed.auth == ()


def test_oauth_and_cloud_switches_marked_unsupported():
    payload = full_settings()
    del payload["env"]["ANTHROPIC_AUTH_TOKEN"]
    del payload["env"]["ANTHROPIC_API_KEY"]
    payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] = "oauth-value"
    payload["env"]["CLAUDE_CODE_USE_BEDROCK"] = "1"
    parsed = parse_claude_settings(settings_bytes(payload))
    assert "oauth" in parsed.unsupported_auth
    assert "bedrock" in parsed.unsupported_auth


def test_disabled_cloud_switch_is_not_unsupported():
    payload = full_settings()
    del payload["env"]["ANTHROPIC_AUTH_TOKEN"]
    payload["env"]["CLAUDE_CODE_USE_VERTEX"] = "0"
    parsed = parse_claude_settings(settings_bytes(payload))
    assert parsed.unsupported_auth == ()


def test_ignored_fields_are_listed_by_name_only():
    payload = full_settings()
    payload["hooks"] = {"PreToolUse": [{"hooks": [{"command": "rm -rf /"}]}]}
    payload["permissions"] = {"allow": ["Bash"]}
    payload["env"]["ANTHROPIC_SMALL_FAST_MODEL"] = "claude-old-small"
    payload["env"]["ANTHROPIC_CUSTOM_HEADERS"] = "X-Secret: abc"
    parsed = parse_claude_settings(settings_bytes(payload))
    assert "hooks" in parsed.ignored
    assert "permissions" in parsed.ignored
    assert "env.ANTHROPIC_SMALL_FAST_MODEL" in parsed.ignored
    assert "env.ANTHROPIC_CUSTOM_HEADERS" in parsed.ignored
    joined = " ".join(parsed.ignored) + " ".join(parsed.unsupported_auth) + " ".join(parsed.incomplete)
    assert "rm -rf" not in joined and "abc" not in joined


# ---------------------------------------------------------------- secret hygiene


def test_parsed_repr_never_contains_secrets():
    parsed = parse_claude_settings(settings_bytes(full_settings()))
    text = repr(parsed)
    assert TOKEN not in text and API_KEY not in text
    for candidate in parsed.auth:
        assert TOKEN not in repr(candidate) and API_KEY not in repr(candidate)


def test_auth_candidate_equality_uses_value():
    assert AuthCandidate(kind="bearer", env_name="ANTHROPIC_AUTH_TOKEN", value=TOKEN) == AuthCandidate(
        kind="bearer", env_name="ANTHROPIC_AUTH_TOKEN", value=TOKEN
    )
