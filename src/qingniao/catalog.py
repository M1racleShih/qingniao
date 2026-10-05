"""Entry-level catalog editing for the qingniao CLI.

This module holds the pure catalog logic behind ``qing provider``,
``qing credential`` and ``qing model``: how a config-format dictionary is
viewed as entries, how a single target entry is edited with the smallest
possible diff, how referential integrity is enforced before a delete, and
how named credentials resolve to a runtime credential source. It performs
no I/O; the CLI owns the read-modify-write transaction against the
running gateway (GET config -> edit one entry -> conditional PUT).

The credential catalog has two sources of truth by design:

- ``config["credentials"]`` — a section of format-v1 configurations that
  names env-sourced credentials (``{"source": "env", "env": NAME}``) and
  private credentials intentionally registered through ``qing credential
  add`` (``{"source": "private"}``).
- Providers reference catalog credentials by id through their
  ``credential_id`` field; the gateway resolves the source at request
  time. A ``cred_<hex>`` id that is not in the section still resolves to
  the private store (immutable versions created by imports), and such
  ids are listed as derived entries so the catalog view stays complete.
"""

from __future__ import annotations

from typing import Mapping

from .config import validate_config
from .tokens import CREDENTIAL_ID_RE

AUTH_MODES = ("bearer", "x-api-key")

# --------------------------------------------------------------------- views


def provider_view(config: Mapping, pid: str) -> dict | None:
    """A provider entry in control-API config shape, plus its id.

    Keys mirror the configuration exactly so machine consumers can read
    them back unchanged.
    """
    raw = config.get("providers", {}).get(pid) if isinstance(config.get("providers", {}), dict) else None
    if not isinstance(raw, dict):
        return None
    entry = {"id": pid}
    entry.update(_provider_without_id(raw))
    return entry


def _provider_without_id(raw: Mapping) -> dict:
    entry = {"base_url": raw.get("base_url"), "auth": raw.get("auth")}
    if raw.get("credential_env") is not None:
        entry["credential_env"] = raw["credential_env"]
    elif raw.get("credential_id") is not None:
        entry["credential_id"] = raw["credential_id"]
    return entry


def model_view(config: Mapping, mid: str) -> dict | None:
    raw = config.get("models", {}).get(mid) if isinstance(config.get("models", {}), dict) else None
    if not isinstance(raw, dict):
        return None
    return {
        "id": mid,
        "provider": raw.get("provider"),
        "upstream_model": raw.get("upstream_model"),
    }


def provider_referenced_by_models(config: Mapping, pid: str) -> list[str]:
    """Model ids whose ``provider`` field points at ``pid``, sorted."""
    models = config.get("models", {}) if isinstance(config.get("models", {}), dict) else {}
    return sorted(
        mid for mid, raw in models.items() if isinstance(raw, dict) and raw.get("provider") == pid
    )


def model_referenced_by(config: Mapping, mid: str) -> list[str]:
    """Human/machine readable locations that reference ``mid``.

    Checks ``defaults.model``, ``defaults.aux_model`` and every
    ``defaults.routes`` destination, in that order; each hit is a
    stable string such as ``defaults.model`` or ``defaults.routes.<key>``.
    """
    references: list[str] = []
    defaults = config.get("defaults", {})
    if isinstance(defaults, dict):
        if defaults.get("model") == mid:
            references.append("defaults.model")
        if defaults.get("aux_model") == mid:
            references.append("defaults.aux_model")
        routes = defaults.get("routes", {})
        if isinstance(routes, dict):
            for key, dest in routes.items():
                if dest == mid:
                    references.append(f"defaults.routes.{key}")
    return references


def credential_referenced_by(config: Mapping, cid: str) -> list[str]:
    """Provider ids whose ``credential_id`` references the catalog id."""
    providers = config.get("providers", {}) if isinstance(config.get("providers", {}), dict) else {}
    return sorted(
        pid
        for pid, raw in providers.items()
        if isinstance(raw, dict) and raw.get("credential_id") == cid
    )


def _catalog_entries(config: Mapping) -> dict[str, dict]:
    raw = config.get("credentials", {}) if isinstance(config.get("credentials", {}), dict) else {}
    return {cid: value for cid, value in raw.items() if isinstance(value, dict)}


def _derived_private_ids(config: Mapping) -> list[str]:
    """cred_<hex> ids referenced by providers but absent from the section."""
    _resources = _catalog_entries(config)
    providers = config.get("providers", {}) if isinstance(config.get("providers", {}), dict) else {}
    seen: dict[str, None] = {}
    for raw in providers.values():
        cid = raw.get("credential_id") if isinstance(raw, dict) else None
        if isinstance(cid, str) and cid not in _resources and CREDENTIAL_ID_RE.match(cid):
            seen.setdefault(cid, None)
    return sorted(seen)


def _credential_entry_list(config: Mapping) -> list[dict]:
    """Every catalog entry with metadata only (never secret material)."""
    _resources = _catalog_entries(config)
    entries: list[dict] = []
    for cid in sorted(_resources):
        raw = _resources[cid]
        source = raw.get("source") or "env"
        entry: dict = {"id": cid, "source": source, "referenced_by": credential_referenced_by(config, cid)}
        if source == "env":
            entry["env"] = raw.get("env")
        entries.append(entry)
    for cid in _derived_private_ids(config):
        entries.append(
            {
                "id": cid,
                "source": "private",
                "referenced_by": credential_referenced_by(config, cid),
                "derived": True,
            }
        )
    return entries


def credential_view(config: Mapping, cid: str) -> dict | None:
    for entry in _credential_entry_list(config):
        if entry["id"] == cid:
            return entry
    return None


def list_providers(config: Mapping) -> list[dict]:
    providers = config.get("providers", {}) if isinstance(config.get("providers", {}), dict) else {}
    return [provider_view(config, pid) for pid in sorted(providers)]


def list_models(config: Mapping) -> list[dict]:
    models = config.get("models", {}) if isinstance(config.get("models", {}), dict) else {}
    return [model_view(config, mid) for mid in sorted(models)]


def list_credentials(config: Mapping) -> list[dict]:
    return _credential_entry_list(config)


# -------------------------------------------------------- local validation


def validate_edited_config(config: Mapping) -> list[tuple[str, str]]:
    """Validate a full edited config against the real schema rules.

    Returns ``[(path, message), ...]``; an empty list means the edited
    configuration is valid. This mirrors what the gateway would accept
    on PUT, so catalog commands can fail fast with field-level problems
    before any write is attempted.
    """
    try:
        validate_config(dict(config))
    except Exception as exc:  # ConfigValidationError
        problems = getattr(exc, "problems", None)
        if isinstance(problems, list) and all(isinstance(p, tuple) and len(p) == 2 for p in problems):
            return list(problems)
        return [("", str(exc))]
    return []


def resolve_credential_target(config: Mapping, target: str) -> tuple[str, str] | None:
    """Resolve ``--credential-id TARGET`` to ``(kind, name)``.

    ``kind`` is ``"env"`` (read ``name`` from the gateway process
    environment) or ``"private"`` (the immutable store version ``name``).
    Returns ``None`` when the target is neither a catalog entry nor a
    legacy private store id, so callers can answer ``entry_not_found``.
    """
    _resources = _catalog_entries(config)
    raw = _resources.get(target)
    if raw is not None and isinstance(raw, dict):
        if raw.get("source") == "private":
            return ("private", target)
        return ("env", raw.get("env"))
    if CREDENTIAL_ID_RE.match(target):
        return ("private", target)
    return None


def credential_label(config: Mapping, credential_id: str) -> str:
    """Short human label for a provider's credential_id reference."""
    resolved = resolve_credential_target(config, credential_id)
    if resolved is None:
        return f"credential {credential_id}"
    kind, name = resolved
    if kind == "env":
        return f"env {name}"
    return f"private {credential_id}"
