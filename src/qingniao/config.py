"""Shared configuration schema, validation and atomic persistence.

The shared JSON configuration has three sections:

- providers: id -> {base_url, credential_env, auth}
- models: id -> {provider, upstream_model}
- defaults: {model, aux_model, routes}

Credential values never appear in the configuration; providers reference an
environment variable name that the gateway process provides.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from . import errors

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_AUTH_MODES = ("bearer", "x-api-key")
_TOP_LEVEL_KEYS = {"providers", "models", "defaults"}
_PROVIDER_KEYS = {"base_url", "credential_env", "auth"}
_MODEL_KEYS = {"provider", "upstream_model"}
_DEFAULTS_KEYS = {"model", "aux_model", "routes"}


@dataclass(frozen=True)
class Provider:
    base_url: str
    credential_env: str
    auth: str


@dataclass(frozen=True)
class ModelEntry:
    provider: str
    upstream_model: str


@dataclass(frozen=True)
class Defaults:
    model: str | None
    aux_model: str | None
    routes: Mapping[str, str]


@dataclass(frozen=True)
class SharedConfig:
    providers: Mapping[str, Provider]
    models: Mapping[str, ModelEntry]
    defaults: Defaults

    def to_json(self) -> dict:
        return {
            "providers": {
                pid: {
                    "base_url": p.base_url,
                    "credential_env": p.credential_env,
                    "auth": p.auth,
                }
                for pid, p in self.providers.items()
            },
            "models": {
                mid: {"provider": m.provider, "upstream_model": m.upstream_model}
                for mid, m in self.models.items()
            },
            "defaults": {
                "model": self.defaults.model,
                "aux_model": self.defaults.aux_model,
                "routes": dict(self.defaults.routes),
            },
        }


EMPTY_CONFIG = SharedConfig(
    providers={},
    models={},
    defaults=Defaults(model=None, aux_model=None, routes={}),
)


class ConfigValidationError(Exception):
    def __init__(self, problems: list[tuple[str, str]]):
        super().__init__("; ".join(f"{path}: {msg}" for path, msg in problems))
        self.problems = [(path, msg) for path, msg in problems]

    def to_api_error(self) -> errors.ApiError:
        return errors.ApiError(
            400,
            errors.INVALID_CONFIG,
            "shared configuration validation failed",
            errors_=[
                {"path": path, "message": msg} for path, msg in self.problems
            ],
        )


def _validate_provider(pid: str, value: object, problems: list[tuple[str, str]]) -> Provider | None:
    path = f"providers.{pid}"
    if not isinstance(pid, str) or not pid.strip():
        problems.append(("providers", "provider ids must be non-empty strings"))
        return None
    if not isinstance(value, dict):
        problems.append((path, "must be an object"))
        return None
    unknown = set(value) - _PROVIDER_KEYS
    if unknown:
        problems.append((path, f"unknown fields: {', '.join(sorted(unknown))}"))
        return None
    missing = _PROVIDER_KEYS - set(value)
    if missing:
        problems.append((path, f"missing fields: {', '.join(sorted(missing))}"))
        return None
    base_url = value["base_url"]
    if not isinstance(base_url, str) or not base_url.strip():
        problems.append((f"{path}.base_url", "must be a non-empty string"))
        return None
    try:
        parts = urlsplit(base_url)
        parts.port
    except ValueError as exc:
        problems.append((f"{path}.base_url", f"malformed URL: {exc}"))
        return None
    if parts.scheme not in ("http", "https"):
        problems.append((f"{path}.base_url", "scheme must be http or https"))
        return None
    if not parts.hostname:
        problems.append((f"{path}.base_url", "host is required"))
        return None
    if parts.username is not None or parts.password is not None:
        problems.append((f"{path}.base_url", "userinfo is not allowed"))
        return None
    if parts.query or parts.fragment:
        problems.append((f"{path}.base_url", "query and fragment are not allowed"))
        return None
    credential_env = value["credential_env"]
    if not isinstance(credential_env, str) or not _ENV_NAME_RE.match(credential_env):
        problems.append((f"{path}.credential_env", "must be an environment variable name"))
        return None
    auth = value["auth"]
    if auth not in _AUTH_MODES:
        problems.append((f"{path}.auth", f"must be one of: {', '.join(_AUTH_MODES)}"))
        return None
    return Provider(base_url=base_url.rstrip("/"), credential_env=credential_env, auth=auth)


def validate_config(data: object) -> SharedConfig:
    problems: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        raise ConfigValidationError([("", "configuration must be a JSON object")])
    unknown = set(data) - _TOP_LEVEL_KEYS
    if unknown:
        problems.append(("", f"unknown top-level fields: {', '.join(sorted(unknown))}"))

    providers: dict[str, Provider] = {}
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        problems.append(("providers", "must be an object"))
    else:
        for pid, value in raw_providers.items():
            provider = _validate_provider(pid, value, problems)
            if provider is not None:
                providers[pid] = provider

    models: dict[str, ModelEntry] = {}
    raw_models = data.get("models", {})
    if not isinstance(raw_models, dict):
        problems.append(("models", "must be an object"))
    else:
        for mid, value in raw_models.items():
            path = f"models.{mid}"
            if not isinstance(mid, str) or not mid.strip():
                problems.append(("models", "model ids must be non-empty strings"))
                continue
            if not isinstance(value, dict):
                problems.append((path, "must be an object"))
                continue
            unknown_fields = set(value) - _MODEL_KEYS
            if unknown_fields:
                problems.append((path, f"unknown fields: {', '.join(sorted(unknown_fields))}"))
                continue
            missing = _MODEL_KEYS - set(value)
            if missing:
                problems.append((path, f"missing fields: {', '.join(sorted(missing))}"))
                continue
            provider = value["provider"]
            if not isinstance(provider, str) or provider not in providers:
                problems.append((f"{path}.provider", f"unknown provider {provider!r}"))
                continue
            upstream = value["upstream_model"]
            if not isinstance(upstream, str) or not upstream.strip():
                problems.append((f"{path}.upstream_model", "must be a non-empty string"))
                continue
            models[mid] = ModelEntry(provider=provider, upstream_model=upstream)

    raw_defaults = data.get("defaults")
    if not isinstance(raw_defaults, dict):
        problems.append(("defaults", "must be an object with model, aux_model and routes"))
        raw_defaults = {}
    else:
        unknown_fields = set(raw_defaults) - _DEFAULTS_KEYS
        if unknown_fields:
            problems.append(("defaults", f"unknown fields: {', '.join(sorted(unknown_fields))}"))
        missing = _DEFAULTS_KEYS - set(raw_defaults)
        if missing:
            problems.append(("defaults", f"missing fields: {', '.join(sorted(missing))}"))

    routes: dict[str, str] = {}
    raw_routes = raw_defaults.get("routes", {}) if isinstance(raw_defaults, dict) else {}
    if not isinstance(raw_routes, dict):
        problems.append(("defaults.routes", "must be an object"))
    else:
        for request_model, dest in raw_routes.items():
            path = f"defaults.routes.{request_model}"
            if not isinstance(request_model, str) or not request_model:
                problems.append(("defaults.routes", "request model keys must be non-empty strings"))
                continue
            if not isinstance(dest, str) or dest not in models:
                problems.append((path, f"unknown destination {dest!r}"))
                continue
            routes[request_model] = dest

    defaults_model = raw_defaults.get("model") if isinstance(raw_defaults, dict) else None
    defaults_aux = raw_defaults.get("aux_model") if isinstance(raw_defaults, dict) else None
    if defaults_model is not None:
        if not isinstance(defaults_model, str) or not defaults_model:
            problems.append(("defaults.model", "must be null or a non-empty string"))
        elif defaults_model not in routes:
            problems.append(("defaults.model", f"{defaults_model!r} must exist in defaults.routes"))
    if defaults_aux is not None:
        if not isinstance(defaults_aux, str) or not defaults_aux:
            problems.append(("defaults.aux_model", "must be null or a non-empty string"))
        elif defaults_aux not in routes:
            problems.append(("defaults.aux_model", f"{defaults_aux!r} must exist in defaults.routes"))

    if problems:
        raise ConfigValidationError(problems)
    return SharedConfig(
        providers=providers,
        models=models,
        defaults=Defaults(model=defaults_model, aux_model=defaults_aux, routes=routes),
    )


def atomic_write_json(path: Path, payload: dict, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, sort_keys=True)
            fh.write("\n")
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp_name, mode)
        os.replace(tmp_name, path)
    except BaseException:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    try:
        dir_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass


class ConfigStore:
    """Loads and atomically persists the shared configuration."""

    def __init__(self, path: Path):
        self.path = path

    def load(self) -> SharedConfig:
        if not self.path.exists():
            return EMPTY_CONFIG
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise errors.ApiError(
                500,
                errors.CONFIG_LOAD_FAILED,
                f"cannot read shared configuration at {self.path}: {exc}",
            ) from exc
        try:
            return validate_config(data)
        except ConfigValidationError as exc:
            raise errors.ApiError(
                500,
                errors.CONFIG_LOAD_FAILED,
                f"shared configuration at {self.path} is invalid",
                errors_=[{"path": p, "message": m} for p, m in exc.problems],
            ) from exc

    def apply(self, data: object) -> SharedConfig:
        try:
            config = validate_config(data)
        except ConfigValidationError as exc:
            raise exc.to_api_error() from exc
        try:
            atomic_write_json(self.path, config.to_json())
        except OSError as exc:
            # Persistence failed before os.replace took effect, so the prior
            # on-disk configuration is intact; the in-memory catalog, config
            # revision and instances are only swapped after a successful
            # write and therefore remain unchanged here.
            raise errors.ApiError(
                500,
                errors.CONFIG_PERSIST_FAILED,
                f"cannot persist shared configuration to {self.path}",
            ) from exc
        return config
