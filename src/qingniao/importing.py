"""Import planning: desensitized plans built from parsed Claude settings.

The planner turns parsed source candidates plus explicit user decisions
into a complete plan against the current directory contents. It performs
the desensitized preview, the exact model mapping (upstream strings are
kept verbatim; no capability guessing), content-based deduplication,
same-name conflict detection and the in-memory source snapshot. Plans
hold the secret only in a repr-excluded field; previews never contain
key material, key prefixes or digests.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

from . import errors
from .claude_source import ParsedClaudeSettings
from .config import Defaults, Provider, SharedConfig
from .credentials import CredentialStore
from .tokens import TOKEN_PREFIX

_PROVIDER_ID_SAFE = re.compile(r"[^a-z0-9-]+")
_TIERS = ("opus", "sonnet", "haiku")


class PlanError(Exception):
    """Planning failure with a stable code; the message is desensitized."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ImportDecisions:
    auth_kind: str | None = None
    primary_source: str | None = None
    aux_same_as_primary: bool = False
    conflict: str = "fail"  # fail | skip | update | new
    new_provider_id: str | None = None


@dataclass(frozen=True)
class PlannedModel:
    model_id: str
    upstream_model: str
    request_keys: tuple[str, ...] = ()


@dataclass
class ImportPlan:
    operation_id: str
    source: Path
    source_digest: str = field(repr=False)
    expected_generation: int
    status: str  # apply | provider_only | unchanged | skipped
    provider_id: str = ""
    base_url: str = ""
    auth: str = ""
    secret: str = field(repr=False, default="")
    reuse_credential_id: str | None = None
    models: tuple[PlannedModel, ...] = ()
    defaults: Defaults | None = None
    incomplete: tuple[str, ...] = ()
    ignored: tuple[str, ...] = ()
    unsupported_auth: tuple[str, ...] = ()
    notes: tuple[str, ...] = ()


def source_digest(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


def verify_source_unchanged(digest: str, raw: bytes) -> None:
    if source_digest(raw) != digest:
        raise PlanError(
            "source_changed",
            "the Claude settings file changed since the preview; preview again "
            "and reconfirm the new content",
        )


def _normalize_url(url: str) -> str:
    parts = urlsplit(url)
    host = (parts.hostname or "").lower()
    try:
        port = parts.port
    except ValueError:
        port = None
    if port is None:
        port = 443 if parts.scheme.lower() == "https" else 80
    return f"{parts.scheme.lower()}://{host}:{port}{parts.path.rstrip('/')}"


def _host_key(host: str) -> str:
    host = host.lower().strip("[]")
    if host == "localhost":
        return "loopback"
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return host
    if ip.is_loopback or ip.is_unspecified:
        return "loopback"
    return str(ip)


def _same_endpoint(url_a: str, url_b: str) -> bool:
    a, b = urlsplit(url_a), urlsplit(url_b)
    try:
        port_a, port_b = a.port, b.port
    except ValueError:
        return False
    if port_a is None:
        port_a = 443 if a.scheme.lower() == "https" else 80
    if port_b is None:
        port_b = 443 if b.scheme.lower() == "https" else 80
    return _host_key(a.hostname or "") == _host_key(b.hostname or "") and port_a == port_b


def _derive_provider_id(base_url: str) -> str:
    host = (urlsplit(base_url).hostname or "").lower()
    candidate = _PROVIDER_ID_SAFE.sub("-", host).strip("-")
    return candidate or "imported"


def _provider_key(provider: Provider, store: CredentialStore) -> tuple:
    url = _normalize_url(provider.base_url)
    if provider.credential_env is not None:
        return ("env", url, provider.auth, provider.credential_env)
    if provider.credential_id is not None:
        try:
            secret = store.read(provider.credential_id)
        except errors.ApiError:
            secret = f"<unreadable:{provider.credential_id}>"
        return ("private", url, provider.auth, secret)
    return ("none", url, provider.auth)


def plan_preview(plan: ImportPlan) -> dict:
    """Desensitized JSON-ready view; never includes key material."""
    preview: dict = {
        "status": plan.status,
        "operation_id": plan.operation_id,
        "source": str(plan.source),
        "expected_generation": plan.expected_generation,
        "provider": None,
        "models": [
            {"id": m.model_id, "upstream_model": m.upstream_model, "request_keys": list(m.request_keys)}
            for m in plan.models
        ],
        "defaults": None,
        "incomplete": list(plan.incomplete),
        "ignored": list(plan.ignored),
        "unsupported_auth": list(plan.unsupported_auth),
        "notes": list(plan.notes),
    }
    if plan.status != "skipped":
        preview["provider"] = {
            "id": plan.provider_id,
            "base_url": plan.base_url,
            "auth": plan.auth,
            "credential": (
                f"reuses private credential {plan.reuse_credential_id}"
                if plan.reuse_credential_id is not None
                else "private file (created on apply)"
            ),
        }
    if plan.defaults is not None:
        preview["defaults"] = {
            "model": plan.defaults.model,
            "aux_model": plan.defaults.aux_model,
            "routes": dict(plan.defaults.routes),
        }
    return preview


def _choose_auth(parsed: ParsedClaudeSettings, decisions: ImportDecisions):
    if parsed.unsupported_auth:
        raise PlanError(
            "auth_unsupported",
            "the source relies on authentication qingniao cannot migrate "
            f"({', '.join(parsed.unsupported_auth)}); provide a supported "
            "API token explicitly instead",
        )
    if not parsed.base_url:
        raise PlanError(
            "base_url_missing",
            "the source has no usable ANTHROPIC_BASE_URL; add the address or "
            "configure the connection manually",
        )
    candidates = list(parsed.auth)
    if not candidates:
        raise PlanError(
            "auth_missing",
            "no ANTHROPIC_AUTH_TOKEN or ANTHROPIC_API_KEY was found in the source; "
            "add a supported credential",
        )
    if len(candidates) > 1:
        if decisions.auth_kind not in ("bearer", "x-api-key"):
            raise PlanError(
                "auth_choice_required",
                "the source contains both ANTHROPIC_AUTH_TOKEN (bearer) and "
                "ANTHROPIC_API_KEY (x-api-key); choose one explicitly",
            )
        for candidate in candidates:
            if candidate.kind == decisions.auth_kind:
                return candidate
        raise PlanError("invalid_decision", "the selected authentication is not present in the source")
    return candidates[0]


def _choose_primary(parsed: ParsedClaudeSettings, decisions: ImportDecisions):
    values = parsed.primary_models
    distinct = {c.value for c in values}
    if decisions.primary_source is not None:
        for candidate in values:
            if candidate.source == decisions.primary_source:
                return candidate.value
        raise PlanError("invalid_decision", "the selected primary model source is not present")
    if len(distinct) > 1:
        raise PlanError(
            "primary_choice_required",
            "settings.model and env.ANTHROPIC_MODEL differ in the source; "
            "choose one explicitly",
        )
    if len(distinct) == 1:
        return next(iter(distinct))
    return None


def _unique_model_id(base: str, taken: set[str]) -> str:
    if base not in taken:
        return base
    n = 2
    while f"{base}--{n}" in taken:
        n += 1
    return f"{base}--{n}"


def build_plan(
    parsed: ParsedClaudeSettings,
    *,
    source_digest: str,
    current: SharedConfig,
    credential_store: CredentialStore,
    decisions: ImportDecisions,
    operation_id: str,
    gateway_endpoint: str | None = None,
) -> ImportPlan:
    auth = _choose_auth(parsed, decisions)
    if auth.value.startswith(TOKEN_PREFIX):
        raise PlanError(
            "self_reference",
            "the source credential carries the qingniao gateway token prefix; a "
            "gateway token cannot be imported as an upstream credential",
        )
    notes: list[str] = []
    if gateway_endpoint is None:
        notes.append(
            "no gateway discovery was available: the self-reference check covered "
            "the token format only, not the endpoint"
        )
    elif _same_endpoint(parsed.base_url, gateway_endpoint):
        raise PlanError(
            "self_reference",
            "the source address points at the qingniao gateway itself; import the "
            "original upstream connection instead",
        )

    primary = _choose_primary(parsed, decisions)
    aux = parsed.aux_model
    incomplete: list[str] = []
    if primary is None:
        incomplete.append("no primary model in the source; defaults are not changed")
    if aux is None and primary is not None:
        if decisions.aux_same_as_primary:
            aux = primary
        else:
            incomplete.append(
                "no CLAUDE_CODE_SUBAGENT_MODEL in the source; confirm using the "
                "primary model as aux explicitly or add one"
            )

    # Exact mapping: catalog entries keep the upstream string verbatim.
    strings: list[tuple[str, tuple[str, ...]]] = []
    if primary is not None:
        strings.append((primary, (primary,)))
    if aux is not None and aux != primary:
        strings.append((aux, (aux,)))
    for tier in _TIERS:
        value = parsed.tier_models.get(tier)
        if value is not None and all(value != s for s, _ in strings):
            strings.append((value, (tier,)))

    imported_key = ("private", _normalize_url(parsed.base_url), auth.kind, auth.value)
    content_match: str | None = None
    for pid, provider in current.providers.items():
        if _provider_key(provider, credential_store) == imported_key:
            content_match = pid
            break

    reuse_credential_id: str | None = None
    if content_match is not None:
        reuse_credential_id = current.providers[content_match].credential_id

    provider_id = content_match or _derive_provider_id(parsed.base_url)
    id_taken = provider_id in current.providers and content_match is None

    # Model collisions with different content force an explicit choice.
    def _model_conflicts(target_provider: str) -> bool:
        return any(
            string in current.models
            and (
                current.models[string].provider != target_provider
                or current.models[string].upstream_model != string
            )
            for string, _ in strings
        )

    conflict = (
        id_taken
        or (content_match is not None and _model_conflicts(content_match))
        or (content_match is None and not id_taken and _model_conflicts(provider_id))
    )
    if conflict and decisions.conflict == "fail":
        raise PlanError(
            "conflict_choice_required",
            "the target already contains a connection or model with the same name "
            "but different content; choose skip, update, or a new provider id",
        )
    if decisions.conflict == "skip":
        return ImportPlan(
            operation_id=operation_id,
            source=parsed.source,
            source_digest=source_digest,
            expected_generation=current.generation,
            status="skipped",
            incomplete=tuple(incomplete),
            ignored=parsed.ignored,
            unsupported_auth=parsed.unsupported_auth,
            notes=tuple(notes),
        )
    if decisions.conflict == "new":
        if not decisions.new_provider_id or not decisions.new_provider_id.strip():
            raise PlanError("invalid_decision", "a new provider id is required for the 'new' choice")
        new_id = decisions.new_provider_id.strip()
        if new_id in current.providers and new_id != provider_id:
            raise PlanError("invalid_decision", "the chosen provider id already exists")
        provider_id = new_id
        reuse_credential_id = reuse_credential_id if content_match is not None else None

    models: list[PlannedModel] = []
    taken = set(current.models)
    for string, keys in strings:
        entry = current.models.get(string)
        if decisions.conflict == "new" and entry is not None and (
            entry.provider != provider_id or entry.upstream_model != string
        ):
            model_id = _unique_model_id(string, taken)
        else:
            model_id = string
        taken.add(model_id)
        models.append(PlannedModel(model_id=model_id, upstream_model=string, request_keys=keys))

    defaults: Defaults | None = None
    if primary is not None and aux is not None:
        routes = dict(current.defaults.routes)
        for model in models:
            for key in model.request_keys:
                routes[key] = model.model_id
        defaults = Defaults(model=primary, aux_model=aux, routes=routes)

    if content_match is not None and defaults is not None and not _model_conflicts(content_match):
        present = (
            current.defaults.model == defaults.model
            and current.defaults.aux_model == defaults.aux_model
            and all(current.defaults.routes.get(k) == v for k, v in defaults.routes.items())
            and all(m.model_id == m.upstream_model for m in models)
        )
        if present:
            return ImportPlan(
                operation_id=operation_id,
                source=parsed.source,
                source_digest=source_digest,
                expected_generation=current.generation,
                status="unchanged",
                provider_id=provider_id,
                base_url=parsed.base_url,
                auth=auth.kind,
                secret=auth.value,
                reuse_credential_id=reuse_credential_id,
                models=tuple(models),
                defaults=defaults,
                incomplete=tuple(incomplete),
                ignored=parsed.ignored,
                unsupported_auth=parsed.unsupported_auth,
                notes=tuple(notes),
            )

    status = "apply" if defaults is not None else "provider_only"
    return ImportPlan(
        operation_id=operation_id,
        source=parsed.source,
        source_digest=source_digest,
        expected_generation=current.generation,
        status=status,
        provider_id=provider_id,
        base_url=parsed.base_url,
        auth=auth.kind,
        secret=auth.value,
        reuse_credential_id=reuse_credential_id,
        models=tuple(models),
        defaults=defaults,
        incomplete=tuple(incomplete),
        ignored=parsed.ignored,
        unsupported_auth=parsed.unsupported_auth,
        notes=tuple(notes),
    )
