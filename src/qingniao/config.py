"""Shared configuration schema, validation and atomic persistence.

The shared JSON configuration is written in format version 1:

- schema_version: 1, generation: positive integer, last_operation: optional
- providers: id -> {base_url, auth, credential_env | credential_id}
- models: id -> {provider, upstream_model}
- defaults: {model, aux_model, routes}

Files without a schema_version are the legacy format (environment-variable
credentials only); they are read as-is and reading never rewrites them. Any
apply upgrades what it writes to version 1 starting at generation 1. Unknown
future versions are rejected, never migrated or overwritten.

Credential values never appear in the configuration; providers reference
either an environment variable name that the gateway process provides or an
id in the private credential store.
"""

from __future__ import annotations

import dataclasses
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from urllib.parse import urlsplit

from . import errors
from .tokens import CREDENTIAL_ID_RE, OPERATION_ID_RE

SCHEMA_VERSION = 1

_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*\Z")
_AUTH_MODES = ("bearer", "x-api-key")
_CREDENTIAL_SOURCES = ("env", "private")
_TOP_LEVEL_KEYS = {"providers", "models", "defaults"}
_TOP_LEVEL_KEYS_V1 = {"schema_version", "generation", "last_operation", "providers", "models", "defaults", "credentials"}
_PROVIDER_KEYS = {"base_url", "credential_env", "auth"}
_PROVIDER_KEYS_V1 = {"base_url", "credential_env", "credential_id", "auth"}
_MODEL_KEYS = {"provider", "upstream_model"}
_DEFAULTS_KEYS = {"model", "aux_model", "routes"}
_CREDENTIAL_KEYS = {"source", "env"}


@dataclass(frozen=True)
class CredentialEntry:
    """A catalog entry for a named credential source.

    ``source`` is ``"env"`` (the credential is read from an environment
    variable named ``env`` in the gateway process) or ``"private"`` (the
    credential is an immutable version in the private store; the secret
    never appears in the configuration). Providers reference a catalog
    entry through ``credential_id``; the gateway resolves the entry at
    request time.
    """

    source: str
    env: str | None = None

    def to_json(self) -> dict:
        if self.source == "private":
            return {"source": "private"}
        return {"source": "env", "env": self.env}


@dataclass(frozen=True)
class Provider:
    base_url: str
    credential_env: str | None = None
    auth: str = ""
    credential_id: str | None = None


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
    credentials: Mapping[str, CredentialEntry] = field(default_factory=dict)
    schema_version: int = SCHEMA_VERSION
    generation: int = 0
    last_operation: str | None = None

    def to_json(self) -> dict:
        return {
            "schema_version": self.schema_version,
            "generation": self.generation,
            "last_operation": self.last_operation,
            "providers": {
                pid: {
                    "base_url": p.base_url,
                    **(
                        {"credential_env": p.credential_env}
                        if p.credential_env is not None
                        else {"credential_id": p.credential_id}
                    ),
                    "auth": p.auth,
                }
                for pid, p in self.providers.items()
            },
            "models": {
                mid: {"provider": m.provider, "upstream_model": m.upstream_model}
                for mid, m in self.models.items()
            },
            "credentials": {
                cid: c.to_json() for cid, c in self.credentials.items()
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
    credentials={},
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


def _validate_provider(
    pid: str, value: object, problems: list[tuple[str, str]], *, versioned: bool, credential_ids: set[str]
) -> Provider | None:
    path = f"providers.{pid}"
    if not isinstance(pid, str) or not pid.strip():
        problems.append(("providers", "provider ids must be non-empty strings"))
        return None
    if not isinstance(value, dict):
        problems.append((path, "must be an object"))
        return None
    allowed = _PROVIDER_KEYS_V1 if versioned else _PROVIDER_KEYS
    unknown = set(value) - allowed
    if unknown:
        problems.append((path, f"unknown fields: {', '.join(sorted(unknown))}"))
        return None
    required = {"base_url", "auth"}
    if not versioned:
        required.add("credential_env")
    missing = required - set(value)
    if missing:
        problems.append((path, f"missing fields: {', '.join(sorted(missing))}"))
        return None
    if versioned:
        has_env = "credential_env" in value
        has_id = "credential_id" in value
        if has_env == has_id:
            problems.append((path, "exactly one of credential_env or credential_id is required"))
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
    credential_env = value.get("credential_env")
    if credential_env is not None and (
        not isinstance(credential_env, str) or not _ENV_NAME_RE.match(credential_env)
    ):
        problems.append((f"{path}.credential_env", "must be an environment variable name"))
        return None
    credential_id = value.get("credential_id")
    if credential_id is not None and not _valid_provider_credential_id(credential_id, credential_ids):
        problems.append((f"{path}.credential_id", "must reference a credential catalog id or a private credential id (cred_<hex>)"))
        return None
    auth = value["auth"]
    if auth not in _AUTH_MODES:
        problems.append((f"{path}.auth", f"must be one of: {', '.join(_AUTH_MODES)}"))
        return None
    return Provider(
        base_url=base_url.rstrip("/"),
        credential_env=credential_env,
        auth=auth,
        credential_id=credential_id,
    )


def _valid_provider_credential_id(credential_id: object, credential_ids: set[str]) -> bool:
    """A provider may reference any id in the credential catalog plus legacy
    private store ids directly (immutable versions created by imports)."""
    if not isinstance(credential_id, str):
        return False
    return credential_id in credential_ids or bool(CREDENTIAL_ID_RE.match(credential_id))


def _validate_credentials(data: dict, problems: list[tuple[str, str]]) -> dict[str, CredentialEntry]:
    raw_credentials = data.get("credentials", {})
    if not isinstance(raw_credentials, dict):
        problems.append(("credentials", "must be an object"))
        return {}
    credentials: dict[str, CredentialEntry] = {}
    for cid, value in raw_credentials.items():
        path = f"credentials.{cid}"
        if not isinstance(cid, str) or not cid.strip():
            problems.append(("credentials", "credential ids must be non-empty strings"))
            continue
        if not isinstance(value, dict):
            problems.append((path, "must be an object"))
            continue
        unknown = set(value) - _CREDENTIAL_KEYS
        if unknown:
            problems.append((path, f"unknown fields: {', '.join(sorted(unknown))}"))
            continue
        source = value.get("source")
        if source not in _CREDENTIAL_SOURCES:
            problems.append((path, f"source must be one of: {', '.join(_CREDENTIAL_SOURCES)}"))
            continue
        env = value.get("env")
        if source == "env":
            if CREDENTIAL_ID_RE.match(cid):
                problems.append((path, "cred_<hex> ids are reserved for private-source credentials"))
                continue
            if env is None or not isinstance(env, str) or not _ENV_NAME_RE.match(env):
                problems.append((f"{path}.env", "must be an environment variable name"))
                continue
            credentials[cid] = CredentialEntry(source="env", env=env)
        else:
            if not CREDENTIAL_ID_RE.match(cid):
                problems.append((path, "private credential ids must be of the form cred_<hex>"))
                continue
            if env is not None:
                problems.append((path, "private credentials carry no env field"))
                continue
            credentials[cid] = CredentialEntry(source="private")
    return credentials


def validate_config(data: object) -> SharedConfig:
    problems: list[tuple[str, str]] = []
    if not isinstance(data, dict):
        raise ConfigValidationError([("", "configuration must be a JSON object")])
    versioned = "schema_version" in data
    if versioned:
        raw_version = data["schema_version"]
        if isinstance(raw_version, bool) or not isinstance(raw_version, int):
            problems.append(("schema_version", "must be an integer"))
        elif raw_version != SCHEMA_VERSION:
            problems.append(
                (
                    "schema_version",
                    f"unknown schema version {raw_version}; this build reads version "
                    f"{SCHEMA_VERSION} and refuses to migrate or overwrite it",
                )
            )
    allowed_top = _TOP_LEVEL_KEYS_V1 if versioned else _TOP_LEVEL_KEYS
    unknown = set(data) - allowed_top
    if unknown:
        problems.append(("", f"unknown top-level fields: {', '.join(sorted(unknown))}"))

    generation = 0
    last_operation: str | None = None
    if versioned:
        raw_generation = data.get("generation")
        if raw_generation is None:
            problems.append(("generation", "is required and must be a non-negative integer"))
        elif isinstance(raw_generation, bool) or not isinstance(raw_generation, int) or raw_generation < 0:
            problems.append(("generation", "must be a non-negative integer"))
        else:
            generation = raw_generation
        raw_operation = data.get("last_operation")
        if raw_operation is not None:
            if not isinstance(raw_operation, str) or not OPERATION_ID_RE.match(raw_operation):
                problems.append(("last_operation", "must be an operation id of the form op_<hex>"))
            else:
                last_operation = raw_operation

    providers: dict[str, Provider] = {}
    credentials = _validate_credentials(data, problems)
    raw_providers = data.get("providers", {})
    if not isinstance(raw_providers, dict):
        problems.append(("providers", "must be an object"))
    else:
        for pid, value in raw_providers.items():
            provider = _validate_provider(pid, value, problems, versioned=versioned, credential_ids=set(credentials))
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
        credentials=credentials,
        schema_version=SCHEMA_VERSION,
        generation=generation,
        last_operation=last_operation,
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

    BACKUP_NAME = "config.backup.pre-v1.json"

    def __init__(self, path: Path):
        self.path = path
        # Backup filename when the most recent apply upgraded a legacy file;
        # None otherwise. Read right after apply under the same lock.
        self.last_upgrade: str | None = None

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

    def persisted_generation(self) -> int:
        """Generation of the on-disk file; 0 when absent, legacy or unreadable."""
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return 0
        if not isinstance(data, dict):
            return 0
        generation = data.get("generation")
        if isinstance(generation, bool) or not isinstance(generation, int) or generation < 1:
            return 0
        return generation

    def _legacy_bytes_on_disk(self) -> bytes | None:
        if not self.path.exists():
            return None
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if isinstance(data, dict) and "schema_version" not in data:
            return self.path.read_bytes()
        return None

    def backup_legacy_now(self) -> tuple[str, bool] | None:
        """Snapshot a legacy file before the first versioned write.

        Returns (backup filename, created-by-this-call) or None when the
        on-disk file is already versioned or absent. A pre-existing backup
        is never overwritten.
        """
        legacy = self._legacy_bytes_on_disk()
        if legacy is None:
            return None
        backup = self.path.parent / self.BACKUP_NAME
        if backup.exists():
            return (self.BACKUP_NAME, False)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(self.path.parent), prefix=f".{self.BACKUP_NAME}.", suffix=".tmp"
        )
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(legacy)
                fh.flush()
                os.fsync(fh.fileno())
            os.chmod(tmp_name, 0o600)
            os.replace(tmp_name, backup)
        except BaseException:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
            raise
        return (self.BACKUP_NAME, True)

    def apply(
        self,
        data: object,
        *,
        expected_generation: int | None = None,
        operation_id: str | None = None,
    ) -> SharedConfig:
        try:
            config = validate_config(data)
        except ConfigValidationError as exc:
            raise exc.to_api_error() from exc
        if expected_generation is not None and (
            isinstance(expected_generation, bool) or not isinstance(expected_generation, int)
        ):
            raise errors.ApiError(400, errors.INVALID_REQUEST, "expected_generation must be an integer")
        if operation_id is not None and not OPERATION_ID_RE.match(operation_id):
            raise errors.ApiError(400, errors.INVALID_REQUEST, "operation_id must be of the form op_<hex>")
        persisted = self.persisted_generation()
        if expected_generation is not None and expected_generation != persisted:
            raise errors.ApiError(
                409,
                errors.GENERATION_CONFLICT,
                "the configuration changed concurrently; re-read it and retry",
                current_generation=persisted,
            )
        self.last_upgrade = None
        # Generation strictly increases with every persisted write, starting
        # at 1 when a legacy or absent configuration is first upgraded.
        config = dataclasses.replace(config, generation=persisted + 1)
        if operation_id is not None:
            config = dataclasses.replace(config, last_operation=operation_id)
        try:
            upgraded = self.backup_legacy_now()
            if upgraded is not None:
                self.last_upgrade = upgraded[0]
            atomic_write_json(self.path, config.to_json())
        except OSError as exc:
            # Persistence failed before os.replace took effect, so the prior
            # on-disk configuration is intact; the in-memory catalog, config
            # revision and instances are only swapped after a successful
            # write and therefore remain unchanged here.
            self.last_upgrade = None
            raise errors.ApiError(
                500,
                errors.CONFIG_PERSIST_FAILED,
                f"cannot persist shared configuration to {self.path}",
            ) from exc
        return config
