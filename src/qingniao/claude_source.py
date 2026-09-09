"""Parsing of one Claude settings file into import candidates.

The parser receives already-read bytes and only inspects the allowed
fields; it never executes hooks, permission actions or ``apiKeyHelper``,
never scans other configuration layers, and does not claim to rebuild
Claude's final effective configuration. Secrets are carried in fields
excluded from ``repr`` so that default logging of parsed candidates
cannot leak them. Every failure is a :class:`SourceError` carrying a
stable category and a fix hint, never source text.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

SETTINGS_NAME = "settings.json"

_KNOWN_ENV_KEYS = {
    "ANTHROPIC_BASE_URL",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_MODEL",
    "ANTHROPIC_DEFAULT_OPUS_MODEL",
    "ANTHROPIC_DEFAULT_SONNET_MODEL",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL",
    "CLAUDE_CODE_SUBAGENT_MODEL",
}
_CLOUD_SWITCHES = {
    "CLAUDE_CODE_USE_BEDROCK": "bedrock",
    "CLAUDE_CODE_USE_VERTEX": "vertex",
    "CLAUDE_CODE_USE_FOUNDRY": "foundry",
}
_TIER_ENV_KEYS = {
    "ANTHROPIC_DEFAULT_OPUS_MODEL": "opus",
    "ANTHROPIC_DEFAULT_SONNET_MODEL": "sonnet",
    "ANTHROPIC_DEFAULT_HAIKU_MODEL": "haiku",
}


class SourceError(Exception):
    """Categorized source failure; the message never echoes source text."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class AuthCandidate:
    kind: str  # "bearer" | "x-api-key"
    env_name: str
    value: str = field(repr=False)


@dataclass(frozen=True)
class ModelCandidate:
    source: str
    value: str


@dataclass(frozen=True)
class ParsedClaudeSettings:
    source: Path = field(repr=False)
    base_url: str | None = None
    auth: tuple[AuthCandidate, ...] = ()
    primary_models: tuple[ModelCandidate, ...] = ()
    tier_models: dict[str, str] = field(default_factory=dict)
    aux_model: str | None = None
    unsupported_auth: tuple[str, ...] = ()
    incomplete: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()


def resolve_source(explicit: Path | None) -> Path:
    """Explicit path wins, then CLAUDE_CONFIG_DIR, then HOME."""
    if explicit is not None:
        return Path(explicit)
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / SETTINGS_NAME
    return Path.home() / ".claude" / SETTINGS_NAME


def read_source(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except FileNotFoundError as exc:
        raise SourceError(
            "source_missing",
            "no Claude settings file was found at the selected source; "
            "create one or pass an explicit --source path",
        ) from exc
    except OSError as exc:
        raise SourceError(
            "source_unreadable",
            f"the Claude settings file cannot be read ({type(exc).__name__}); "
            "check the file permissions of the selected source",
        ) from exc


def _string_env(env: dict, name: str) -> str | None:
    value = env.get(name)
    if value is None:
        return None
    if not isinstance(value, str):
        raise SourceError(
            "field_invalid",
            f"settings field env.{name} has the wrong type; expected a string",
        )
    value = value.strip()
    return value or None


def _validate_base_url(raw: str) -> str:
    try:
        parts = urlsplit(raw)
        parts.port
    except ValueError as exc:
        raise SourceError(
            "base_url_invalid", "ANTHROPIC_BASE_URL is a malformed URL"
        ) from exc
    if parts.scheme not in ("http", "https"):
        raise SourceError(
            "base_url_invalid", "ANTHROPIC_BASE_URL scheme must be http or https"
        )
    if not parts.hostname:
        raise SourceError("base_url_invalid", "ANTHROPIC_BASE_URL has no host")
    if parts.username is not None or parts.password is not None:
        raise SourceError(
            "base_url_invalid", "ANTHROPIC_BASE_URL contains userinfo; remove it"
        )
    if parts.query or parts.fragment:
        raise SourceError(
            "base_url_invalid", "ANTHROPIC_BASE_URL contains a query or fragment; remove it"
        )
    return raw.rstrip("/")


def parse_claude_settings(raw: bytes, *, source: Path = Path("settings.json")) -> ParsedClaudeSettings:
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise SourceError(
            "source_invalid_json", "the Claude settings file is not valid UTF-8 JSON"
        ) from exc
    try:
        payload = json.loads(text)
    except ValueError as exc:
        raise SourceError(
            "source_invalid_json",
            "the Claude settings file is not valid JSON; fix the file or choose another source",
        ) from exc
    if not isinstance(payload, dict):
        raise SourceError(
            "source_not_object", "the Claude settings file must contain a JSON object"
        )

    env = payload.get("env", {})
    if not isinstance(env, dict):
        raise SourceError("field_invalid", "settings field env must be an object")

    ignored: list[str] = []
    unsupported: list[str] = []
    incomplete: list[str] = []

    base_url: str | None = None
    raw_base = _string_env(env, "ANTHROPIC_BASE_URL")
    if raw_base is not None:
        base_url = _validate_base_url(raw_base)

    auth: list[AuthCandidate] = []
    if "apiKeyHelper" in payload:
        unsupported.append("api-key-helper")
    raw_token = _string_env(env, "ANTHROPIC_AUTH_TOKEN")
    if raw_token is not None and not unsupported:
        auth.append(AuthCandidate(kind="bearer", env_name="ANTHROPIC_AUTH_TOKEN", value=raw_token))
    elif raw_token is None and "ANTHROPIC_AUTH_TOKEN" in env:
        incomplete.append("env.ANTHROPIC_AUTH_TOKEN is empty")
    raw_api_key = _string_env(env, "ANTHROPIC_API_KEY")
    if raw_api_key is not None and not unsupported:
        auth.append(AuthCandidate(kind="x-api-key", env_name="ANTHROPIC_API_KEY", value=raw_api_key))
    elif raw_api_key is None and "ANTHROPIC_API_KEY" in env:
        incomplete.append("env.ANTHROPIC_API_KEY is empty")
    oauth = _string_env(env, "CLAUDE_CODE_OAUTH_TOKEN")
    if oauth is not None:
        unsupported.append("oauth")
    for switch, label in _CLOUD_SWITCHES.items():
        value = _string_env(env, switch)
        if value is not None and value.lower() not in ("0", "false"):
            unsupported.append(label)

    primary: list[ModelCandidate] = []
    raw_model = payload.get("model")
    if raw_model is not None:
        if not isinstance(raw_model, str) or not raw_model.strip():
            raise SourceError("field_invalid", "settings field model must be a non-empty string")
        primary.append(ModelCandidate(source="settings.model", value=raw_model.strip()))
    env_model = _string_env(env, "ANTHROPIC_MODEL")
    if env_model is not None:
        primary.append(ModelCandidate(source="env.ANTHROPIC_MODEL", value=env_model))

    tiers: dict[str, str] = {}
    for env_name, tier in _TIER_ENV_KEYS.items():
        value = _string_env(env, env_name)
        if value is not None:
            tiers[tier] = value

    aux_model = _string_env(env, "CLAUDE_CODE_SUBAGENT_MODEL")

    for name in payload:
        if name not in ("env", "model", "apiKeyHelper"):
            ignored.append(name)
    for name in env:
        if name in _KNOWN_ENV_KEYS or name in _CLOUD_SWITCHES or name == "CLAUDE_CODE_OAUTH_TOKEN":
            continue
        ignored.append(f"env.{name}")

    return ParsedClaudeSettings(
        source=Path(source),
        base_url=base_url,
        auth=tuple(auth) if not unsupported else (),
        primary_models=tuple(primary),
        tier_models=tiers,
        aux_model=aux_model,
        unsupported_auth=tuple(unsupported),
        incomplete=tuple(incomplete),
        ignored=tuple(ignored),
    )
