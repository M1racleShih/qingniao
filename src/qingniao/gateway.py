"""Gateway core: instances, immutable route snapshots, leases and CAS updates.

All state lives in memory. Instances never survive a gateway restart; the
shared configuration is the only persisted state. Time is injected so lease
behaviour is testable without sleeping.
"""

from __future__ import annotations

import os
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Callable, Mapping

from . import errors
from .config import SharedConfig, ConfigStore
from .credentials import CredentialStore
from .sse import USAGE_KEYS
from .tokens import new_bearer_token, new_instance_id, token_hash

DEFAULT_LEASE_SECONDS = 30.0
MAX_RECORDS = 1000


@dataclass(frozen=True)
class CredentialRef:
    """Typed credential reference: an environment variable or a private id."""

    kind: str  # "env" | "private"
    name: str

    @property
    def is_private(self) -> bool:
        return self.kind == "private"


@dataclass(frozen=True)
class RouteSnapshot:
    """Immutable resolved destination captured for one request model."""

    request_model: str
    catalog_model: str
    provider_id: str
    base_url: str
    credential: CredentialRef
    auth: str
    upstream_model: str
    route_revision: int


@dataclass
class Instance:
    id: str
    label: str | None
    token_hash: str
    model: str
    aux_model: str
    routes: dict[str, RouteSnapshot]
    revision: int
    created_wall: float
    created_mono: float
    lease_expires_mono: float
    ended: bool = False

    @property
    def state(self) -> str:
        if self.ended:
            return "ended"
        return "active"


@dataclass
class RequestRecord:
    """Bounded, sanitized request metadata. No bodies, tool arguments,
    credentials, raw exceptions or upstream error payloads are ever stored."""

    id: str
    instance_id: str
    request_model: str
    provider_id: str | None
    upstream_model: str | None
    route_revision: int | None
    outcome: str  # in_progress | success | failed | cancelled | incomplete
    started_at: float
    finished_at: float | None = None
    status_code: int | None = None
    error_code: str | None = None
    usage: dict | None = None

    def to_json(self) -> dict:
        return {
            "id": self.id,
            "instance_id": self.instance_id,
            "request_model": self.request_model,
            "provider_id": self.provider_id,
            "upstream_model": self.upstream_model,
            "route_revision": self.route_revision,
            "outcome": self.outcome,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "status_code": self.status_code,
            "error_code": self.error_code,
            "usage": self.usage,
        }


def sanitize_usage(usage: object) -> dict | None:
    """Keep only known usage keys with integer values; anything else becomes
    unknown (None) and arbitrary objects are never stored."""
    if not isinstance(usage, dict):
        return None
    out: dict[str, int | None] = {}
    for key in USAGE_KEYS:
        if key not in usage:
            continue
        value = usage[key]
        if isinstance(value, bool) or not isinstance(value, int):
            out[key] = None
        else:
            out[key] = value
    return out or None


class Gateway:
    def __init__(
        self,
        config: SharedConfig,
        config_store: ConfigStore | None = None,
        *,
        credential_store: CredentialStore | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
        lease_seconds: float = DEFAULT_LEASE_SECONDS,
    ):
        self.config = config
        self.config_store = config_store
        self.credential_store = credential_store
        self.config_revision = 1
        self.clock = clock
        self.wall_clock = wall_clock
        self.lease_seconds = lease_seconds
        self._instances: dict[str, Instance] = {}
        self._by_token: dict[str, str] = {}
        self.records: deque[RequestRecord] = deque(maxlen=MAX_RECORDS)
        self._records_by_id: dict[str, RequestRecord] = {}

    # ------------------------------------------------------------- config

    def apply_config(self, data: object) -> SharedConfig:
        if self.config_store is None:
            raise errors.ApiError(500, errors.INVALID_CONFIG, "no configuration store attached")
        config = self.config_store.apply(data)
        self.config = config
        self.config_revision += 1
        return config

    def resolve_snapshot(self, request_model: str, catalog_model: str, route_revision: int) -> RouteSnapshot:
        entry = self.config.models.get(catalog_model)
        if entry is None:
            raise errors.ApiError(
                400,
                errors.INVALID_SELECTION,
                f"unknown destination {catalog_model!r}",
            )
        provider = self.config.providers[entry.provider]
        if provider.credential_id is not None:
            credential = CredentialRef(kind="private", name=provider.credential_id)
        else:
            credential = CredentialRef(kind="env", name=provider.credential_env or "")
        return RouteSnapshot(
            request_model=request_model,
            catalog_model=catalog_model,
            provider_id=entry.provider,
            base_url=provider.base_url,
            credential=credential,
            auth=provider.auth,
            upstream_model=entry.upstream_model,
            route_revision=route_revision,
        )

    # ---------------------------------------------------------- instances

    def create_instance(
        self,
        *,
        label: str | None = None,
        model: str | None = None,
        aux_model: str | None = None,
        route_overrides: Mapping[str, str] | None = None,
    ) -> tuple[Instance, str]:
        if label is not None and (not isinstance(label, str) or not label.strip()):
            raise errors.ApiError(400, errors.INVALID_SELECTION, "label must be a non-empty string")
        for role, value in (("model", model), ("aux_model", aux_model)):
            if value is not None and (not isinstance(value, str) or not value):
                raise errors.ApiError(400, errors.INVALID_SELECTION, f"{role} must be a non-empty string when provided")
        route_sources: dict[str, str] = dict(self.config.defaults.routes)
        if route_overrides:
            for request_model, dest in route_overrides.items():
                if not isinstance(request_model, str) or not request_model:
                    raise errors.ApiError(
                        400, errors.INVALID_SELECTION, "route override keys must be non-empty strings"
                    )
                if not isinstance(dest, str) or dest not in self.config.models:
                    raise errors.ApiError(
                        400,
                        errors.INVALID_SELECTION,
                        f"route override for {request_model!r} references unknown destination {dest!r}",
                    )
                route_sources[request_model] = dest
        chosen_model = model if model is not None else self.config.defaults.model
        chosen_aux = aux_model if aux_model is not None else self.config.defaults.aux_model
        if chosen_model is None:
            raise errors.ApiError(
                400,
                errors.NO_DEFAULT_MODEL,
                "no default model configured; select one explicitly",
            )
        if chosen_aux is None:
            raise errors.ApiError(
                400,
                errors.NO_DEFAULT_MODEL,
                "no default aux model configured; select one explicitly",
            )
        for role, chosen in (("model", chosen_model), ("aux_model", chosen_aux)):
            if chosen not in route_sources:
                raise errors.ApiError(
                    400,
                    errors.INVALID_SELECTION,
                    f"{role} {chosen!r} has no route; add it to routes",
                )
        routes = {
            request_model: self.resolve_snapshot(request_model, dest, 0)
            for request_model, dest in route_sources.items()
        }
        token = new_bearer_token()
        instance = Instance(
            id=new_instance_id(),
            label=label,
            token_hash=token_hash(token),
            model=chosen_model,
            aux_model=chosen_aux,
            routes=routes,
            revision=0,
            created_wall=self.wall_clock(),
            created_mono=self.clock(),
            lease_expires_mono=self.clock() + self.lease_seconds,
        )
        self._instances[instance.id] = instance
        self._by_token[instance.token_hash] = instance.id
        return instance, token

    def is_expired(self, instance: Instance) -> bool:
        return not instance.ended and self.clock() >= instance.lease_expires_mono

    def effective_state(self, instance: Instance) -> str:
        if instance.ended:
            return "ended"
        if self.is_expired(instance):
            return "expired"
        return "active"

    def instance_status(self, instance: Instance) -> dict:
        return {
            "id": instance.id,
            "label": instance.label,
            "state": self.effective_state(instance),
            "model": instance.model,
            "aux_model": instance.aux_model,
            "revision": instance.revision,
            "created_at": instance.created_wall,
            "lease_expires_in": round(instance.lease_expires_mono - self.clock(), 3),
            "routes": {
                request_model: self._route_status(request_model, snap)
                for request_model, snap in instance.routes.items()
            },
        }

    def _route_status(self, request_model: str, snap: RouteSnapshot) -> dict:
        status: dict = {
            "request_model": request_model,
            "catalog_model": snap.catalog_model,
            "catalog_present": (
                snap.catalog_model in self.config.models
                and self._snapshot_matches_catalog(snap)
            ),
            "provider": snap.provider_id,
            "base_url": snap.base_url,
            "upstream_model": snap.upstream_model,
            "auth": snap.auth,
            "route_revision": snap.route_revision,
        }
        # Environment variable names are not secret and keep their existing
        # key; private references show the kind and id only.
        if snap.credential.is_private:
            status["credential_id"] = snap.credential.name
        else:
            status["credential_env"] = snap.credential.name
        return status

    def _snapshot_matches_catalog(self, snap: RouteSnapshot) -> bool:
        entry = self.config.models.get(snap.catalog_model)
        if entry is None:
            return False
        provider = self.config.providers.get(entry.provider)
        if provider is None:
            return False
        if provider.credential_id is not None:
            credential = CredentialRef(kind="private", name=provider.credential_id)
        else:
            credential = CredentialRef(kind="env", name=provider.credential_env or "")
        return (
            snap.provider_id == entry.provider
            and snap.base_url == provider.base_url
            and snap.credential == credential
            and snap.auth == provider.auth
            and snap.upstream_model == entry.upstream_model
        )

    def list_instances(self, offset: int = 0, limit: int = 50) -> tuple[list[dict], int, int | None]:
        instances = sorted(self._instances.values(), key=lambda i: i.created_mono)
        total = len(instances)
        window = instances[offset : offset + limit]
        next_offset = offset + limit if offset + limit < total else None
        return [self.instance_status(i) for i in window], total, next_offset

    def get_instance(self, instance_id: str) -> Instance | None:
        return self._instances.get(instance_id)

    def _require_instance(self, instance_id: str) -> Instance:
        instance = self._instances.get(instance_id)
        if instance is None:
            raise errors.ApiError(404, errors.INSTANCE_NOT_FOUND, f"no instance {instance_id!r}")
        return instance

    def _require_active(self, instance: Instance, *, allow_expired_end: bool = False) -> None:
        if instance.ended:
            raise errors.ApiError(403, errors.INSTANCE_ENDED, f"instance {instance.id} has ended")
        if self.is_expired(instance) and not allow_expired_end:
            raise errors.ApiError(403, errors.INSTANCE_EXPIRED, f"lease for instance {instance.id} expired")

    def resolve_target(self, target: str) -> Instance:
        """Resolve a CLI target: exact instance ID first, else a unique label."""
        instance = self._instances.get(target)
        if instance is not None:
            return instance
        matches = [i for i in self._instances.values() if i.label == target]
        if len(matches) > 1:
            raise errors.ApiError(
                409,
                errors.AMBIGUOUS_LABEL,
                f"label {target!r} matches multiple instances; use the full instance ID",
                candidates=[i.id for i in matches],
            )
        if len(matches) == 1:
            return matches[0]
        raise errors.ApiError(404, errors.INSTANCE_NOT_FOUND, f"no instance or label matches {target!r}")

    def renew_instance(self, instance_id: str) -> Instance:
        instance = self._require_instance(instance_id)
        self._require_active(instance)
        instance.lease_expires_mono = self.clock() + self.lease_seconds
        return instance

    def end_instance(self, instance_id: str) -> Instance:
        instance = self._require_instance(instance_id)
        instance.ended = True
        return instance

    def set_route(
        self, instance_id: str, request_model: str, catalog_model: str, expected_revision: int
    ) -> tuple[Instance, RouteSnapshot]:
        instance = self._require_instance(instance_id)
        self._require_active(instance)
        if expected_revision != instance.revision:
            raise errors.ApiError(
                409,
                errors.REVISION_CONFLICT,
                "instance was modified concurrently; re-read its status and retry",
                current_revision=instance.revision,
            )
        if not isinstance(request_model, str) or not request_model:
            raise errors.ApiError(400, errors.INVALID_REQUEST, "request_model must be a non-empty string")
        new_revision = instance.revision + 1
        snapshot = self.resolve_snapshot(request_model, catalog_model, new_revision)
        instance.routes[request_model] = snapshot
        instance.revision = new_revision
        return instance, snapshot

    # ------------------------------------------------------ request path

    def is_instance_token(self, token: str) -> bool:
        return token_hash(token) in self._by_token

    def authenticate(self, bearer: str | None) -> Instance:
        if not bearer:
            raise errors.ApiError(401, errors.INVALID_INSTANCE_TOKEN, "missing instance token")
        instance_id = self._by_token.get(token_hash(bearer))
        if instance_id is None:
            raise errors.ApiError(
                401,
                errors.INVALID_INSTANCE_TOKEN,
                "invalid instance token; if the gateway restarted, reconnect with a new instance",
            )
        instance = self._instances[instance_id]
        self._require_active(instance)
        return instance

    def route_for(self, instance: Instance, request_model: str) -> RouteSnapshot:
        snapshot = instance.routes.get(request_model)
        if snapshot is None:
            raise errors.ApiError(
                400,
                errors.UNKNOWN_MODEL,
                f"request model {request_model!r} is not routed for instance {instance.id}",
                request_model=request_model,
            )
        return snapshot

    def credential(self, snapshot: RouteSnapshot) -> str:
        """Resolve the snapshot's credential just before the request is sent.

        Environment references read the gateway process environment; private
        references read the immutable credential file through the checked
        store. Failures are sanitized and never fall back to another source.
        """
        if snapshot.credential.is_private:
            if self.credential_store is None:
                raise errors.ApiError(
                    500,
                    errors.CREDENTIAL_UNREADABLE,
                    "private credential storage is not attached to this gateway process",
                    credential_id=snapshot.credential.name,
                )
            return self.credential_store.read(snapshot.credential.name)
        value = os.environ.get(snapshot.credential.name, "")
        if not value:
            raise errors.ApiError(
                500,
                errors.CREDENTIAL_MISSING,
                f"environment variable {snapshot.credential.name} is not set in the gateway process",
                credential_env=snapshot.credential.name,
            )
        return value

    def begin_request(
        self,
        *,
        instance_id: str,
        request_model: str,
        provider_id: str | None = None,
        upstream_model: str | None = None,
        route_revision: int | None = None,
    ) -> str:
        record = RequestRecord(
            id=f"req_{uuid.uuid4().hex[:12]}",
            instance_id=instance_id,
            request_model=request_model,
            provider_id=provider_id,
            upstream_model=upstream_model,
            route_revision=route_revision,
            outcome="in_progress",
            started_at=self.wall_clock(),
        )
        # Single retention set: the deque is the only owner of retained
        # records. When it is full, appending evicts the oldest record and
        # the id index drops the same entry, so at most MAX_RECORDS distinct
        # records are ever retained. An evicted active record can no longer
        # be finished (late finish is a no-op).
        if len(self.records) == self.records.maxlen:
            evicted = self.records[0]
            if self._records_by_id.get(evicted.id) is evicted:
                del self._records_by_id[evicted.id]
        self.records.append(record)
        self._records_by_id[record.id] = record
        return record.id

    def finish_request(
        self,
        record_id: str,
        *,
        outcome: str,
        status_code: int | None = None,
        usage: dict | None = None,
        error_code: str | None = None,
    ) -> None:
        record = self._records_by_id.pop(record_id, None)
        if record is None:
            return
        record.outcome = outcome
        record.status_code = status_code
        record.usage = usage
        record.error_code = error_code
        record.finished_at = self.wall_clock()

    def note_request_usage(self, record_id: str, usage: dict | None) -> None:
        """Publish currently known usage to an in-progress record."""
        record = self._records_by_id.get(record_id)
        if record is not None and record.outcome == "in_progress":
            record.usage = usage

    def list_requests(
        self, offset: int = 0, limit: int = 50, instance_id: str | None = None
    ) -> tuple[list[dict], int, int | None]:
        selected = [r for r in self.records if instance_id is None or r.instance_id == instance_id]
        total = len(selected)
        window = selected[offset : offset + limit]
        next_offset = offset + limit if offset + limit < total else None
        return [r.to_json() for r in window], total, next_offset

    @property
    def instance_count(self) -> int:
        return len(self._instances)
