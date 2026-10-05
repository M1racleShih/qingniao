"""One logical commit for configuration imports.

An import is a single transaction with one release point: take the state
directory's exclusive lock, recover any open transaction, verify the
source snapshot and the expected generation, stage the private
credential, snapshot a legacy configuration before the first versioned
write, atomically replace the configuration (the commit point), then
persist the sanitized receipt. The private transaction record carries
ids and digests only — never secret material. Recovery judges every
open or unconfirmed record by comparing the active configuration's
``last_operation`` with the record: committed transactions keep all
their products, uncommitted ones have their staged credential and
upgrade backup removed. Uncertain outcomes are never reported as
rolled back or succeeded.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import errors, state as state_mod
from .config import ConfigStore, atomic_write_json
from .credentials import CREDENTIALS_DIR_NAME, CredentialStore
from .importing import ImportPlan, plan_preview, verify_source_unchanged
from .tokens import CREDENTIAL_ID_RE, OPERATION_ID_RE

TRANSACTIONS_DIR_NAME = "transactions"
_OPERATION_FILE_RE = re.compile(r"\A(op_[0-9a-f]{32})\.json\Z")


def plan_digest(plan: ImportPlan) -> str:
    return hashlib.sha256(
        json.dumps(plan_preview(plan), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


class TransactionStore:
    """Sanitized transaction records, one JSON file per operation id."""

    def __init__(self, state_dir: Path):
        self.dir = Path(state_dir) / TRANSACTIONS_DIR_NAME

    def _record_path(self, operation_id: str) -> Path:
        if not isinstance(operation_id, str) or not OPERATION_ID_RE.match(operation_id):
            raise errors.ApiError(400, errors.INVALID_REQUEST, "operation id must be of the form op_<hex>")
        return self.dir / f"{operation_id}.json"

    def open(self, record: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        self.dir.chmod(0o700)
        atomic_write_json(self._record_path(record["operation_id"]), record)

    def get(self, operation_id: str) -> dict | None:
        path = self._record_path(operation_id)
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    def conclude(self, operation_id: str, conclusion: dict) -> dict:
        record = self.get(operation_id)
        if record is None:
            raise errors.ApiError(
                500,
                errors.TRANSACTION_DAMAGED,
                f"transaction record for {operation_id} is missing or unreadable",
            )
        record["conclusion"] = conclusion
        self.open(record)
        return record

    def records(self) -> list[tuple[str, dict | None]]:
        if not self.dir.is_dir():
            return []
        out: list[tuple[str, dict | None]] = []
        for entry in sorted(self.dir.iterdir()):
            match = _OPERATION_FILE_RE.match(entry.name)
            if match is None:
                continue
            try:
                data = json.loads(entry.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                data = None
            out.append((match.group(1), data if isinstance(data, dict) else None))
        return out

    def open_records(self) -> list[tuple[str, dict | None]]:
        return [
            (op_id, record)
            for op_id, record in self.records()
            if record is None
            or record.get("conclusion") is None
            or (isinstance(record.get("conclusion"), dict)
                and record["conclusion"].get("status") == "unconfirmed")
        ]


def recover_open_transactions(state_dir: Path) -> list[dict]:
    """Resolve open or unconfirmed transactions to committed/aborted.

    Judgement is by the active configuration's ``last_operation``: equal
    ids mean the commit point was reached and all products are kept
    (missing credentials refuse to proceed instead of guessing); any
    other state means the commit never happened and this operation's
    staged credential and upgrade backup are removed.

    Credential files referenced by the active configuration or created
    by committed records are durable versions and kept (rotation never
    deletes old versions); files no record ever committed are staging
    orphans of interrupted transactions and are removed.
    """
    state_dir = Path(state_dir)
    store = ConfigStore(state_dir / state_mod.CONFIG_NAME)
    creds = CredentialStore(state_dir)
    txs = TransactionStore(state_dir)
    outcomes: list[dict] = []
    for op_id, record in txs.open_records():
        if record is None:
            raise errors.ApiError(
                500,
                errors.TRANSACTION_DAMAGED,
                f"transaction record {op_id} is unreadable; resolve it manually in the "
                "transactions directory before starting the gateway",
            )
        try:
            current = store.load()
        except errors.ApiError:
            current = None
        committed = current is not None and current.last_operation == op_id
        if committed:
            created = record.get("created_credential_id")
            if created:
                try:
                    creds.read(created)
                except errors.ApiError as exc:
                    raise errors.ApiError(
                        500,
                        errors.TRANSACTION_DAMAGED,
                        f"transaction {op_id} committed but its credential cannot be "
                        f"read ({exc.code}); restore the credential or roll back manually",
                    ) from exc
            conclusion = {
                "status": "committed",
                "generation": current.generation,
                "provider_id": record.get("provider_id"),
                "credential_id": created,
            }
        else:
            created = record.get("created_credential_id")
            if isinstance(created, str):
                creds.delete(created)
            backup_name = record.get("backup_path")
            if isinstance(backup_name, str) and backup_name == ConfigStore.BACKUP_NAME:
                try:
                    (state_dir / backup_name).unlink()
                except OSError:
                    pass
            conclusion = {
                "status": "aborted",
                "generation": current.generation if current is not None else 0,
            }
        txs.conclude(op_id, conclusion)
        outcomes.append({"operation_id": op_id, "status": conclusion["status"]})
    _remove_orphan_credentials(state_dir, store, txs)
    return outcomes


def _remove_orphan_credentials(state_dir: Path, store: ConfigStore, txs: TransactionStore) -> None:
    credentials_dir = state_dir / CREDENTIALS_DIR_NAME
    if not credentials_dir.is_dir():
        return
    try:
        current = store.load()
        keep = {
            provider.credential_id
            for provider in current.providers.values()
            if provider.credential_id is not None
        }
    except errors.ApiError:
        keep = set()
    for _op_id, record in txs.records():
        if isinstance(record, dict):
            conclusion = record.get("conclusion")
            if isinstance(conclusion, dict) and conclusion.get("status") == "committed":
                created = record.get("created_credential_id")
                if isinstance(created, str):
                    keep.add(created)
    creds = CredentialStore(state_dir)
    for entry in credentials_dir.iterdir():
        name = entry.name
        if not CREDENTIAL_ID_RE.match(name):
            continue  # never touch files qingniao did not create
        if name not in keep:
            creds.delete(name)


def query_operation(state_dir: Path, operation_id: object) -> dict | None:
    """Sanitized operation result: id, commit status and generation only."""
    if not isinstance(operation_id, str) or not OPERATION_ID_RE.match(operation_id):
        return None
    record = TransactionStore(Path(state_dir)).get(operation_id)
    if record is None:
        return None
    conclusion = record.get("conclusion")
    if not isinstance(conclusion, dict):
        return {"operation_id": operation_id, "status": "in_progress", "generation": None}
    return {
        "operation_id": operation_id,
        "status": conclusion.get("status", "unknown"),
        "generation": conclusion.get("generation"),
    }


def commit_import_offline(*, state_dir: Path, plan: ImportPlan, raw_source: bytes) -> dict:
    """Commit an import while holding the state directory's exclusive lock."""
    state_dir = Path(state_dir)
    state_mod.prepare_state_dir(state_dir)
    lock = state_mod.GatewayLock(state_dir)
    lock.acquire()  # raises gateway_locked when a gateway owns the directory
    try:
        return _commit_import(state_dir=state_dir, plan=plan, raw_source=raw_source)
    finally:
        lock.release()


def commit_import_in_gateway(*, state_dir: Path, plan: ImportPlan) -> dict:
    """Commit an import from the running gateway, which already holds the
    state directory lock; every write entry shares this serialized path."""
    return _commit_import(state_dir=Path(state_dir), plan=plan, raw_source=None)


def _commit_import(*, state_dir: Path, plan: ImportPlan, raw_source: bytes | None) -> dict:
    store = ConfigStore(state_dir / state_mod.CONFIG_NAME)
    creds = CredentialStore(state_dir)
    txs = TransactionStore(state_dir)
    recover_open_transactions(state_dir)
    if raw_source is not None:
        verify_source_unchanged(plan.source_digest, raw_source)
    current = store.load()
    if plan.expected_generation != current.generation:
        raise errors.ApiError(
            409,
            errors.GENERATION_CONFLICT,
            "the configuration changed since the preview; preview again",
            current_generation=current.generation,
        )
    if plan.status == "skipped":
        return {"status": "skipped", "operation_id": plan.operation_id}
    if plan.status == "unchanged":
        return {
            "status": "unchanged",
            "operation_id": plan.operation_id,
            "generation": current.generation,
            "provider_id": plan.provider_id,
            "credential_id": plan.reuse_credential_id,
        }

    record = {
        "operation_id": plan.operation_id,
        "expected_generation": plan.expected_generation,
        "created_credential_id": None,
        "backup_path": None,
        "plan_digest": plan_digest(plan),
        "conclusion": None,
    }
    txs.open(record)
    committed = False
    resolved = False
    credential_id = plan.reuse_credential_id
    try:
        if credential_id is None and plan.secret:
            credential_id = creds.create(plan.secret)
            record["created_credential_id"] = credential_id
            txs.open(record)
        try:
            upgraded = store.backup_legacy_now()
        except OSError as exc:
            raise errors.ApiError(
                500,
                errors.CONFIG_PERSIST_FAILED,
                "cannot back up the legacy configuration before the first versioned write",
            ) from exc
        if upgraded is not None and upgraded[1]:
            record["backup_path"] = upgraded[0]
            txs.open(record)

        data = current.to_json()
        # Upgrade the payload's generation for validation; apply()
        # re-verifies against the on-disk file under the same lock.
        data["generation"] = plan.expected_generation + 1
        if plan.provider_id:
            # Credential-only plans (``qing credential add --from-stdin``)
            # leave the provider catalog untouched here; the private version
            # is staged and committed below and referenced later by id.
            provider: dict = {"base_url": plan.base_url, "auth": plan.auth}
            if credential_id is not None:
                provider["credential_id"] = credential_id
            data["providers"][plan.provider_id] = provider
        elif credential_id is not None:
            # A credential-only commit registers the new immutable version
            # in the credential catalog in the same single transaction.
            data["credentials"][credential_id] = {"source": "private"}
        for model in plan.models:
            data["models"][model.model_id] = {
                "provider": plan.provider_id,
                "upstream_model": model.upstream_model,
            }
        if plan.defaults is not None:
            data["defaults"] = {
                "model": plan.defaults.model,
                "aux_model": plan.defaults.aux_model,
                "routes": dict(plan.defaults.routes),
            }
        try:
            config = store.apply(
                data,
                expected_generation=plan.expected_generation,
                operation_id=plan.operation_id,
            )
        except errors.ApiError as exc:
            if exc.code == errors.CONFIG_PERSIST_FAILED:
                txs.conclude(
                    plan.operation_id,
                    {"status": "unconfirmed", "generation": plan.expected_generation},
                )
                resolved = True
                return {
                    "status": "unconfirmed",
                    "operation_id": plan.operation_id,
                    "message": "the commit result is uncertain; it will be resolved "
                    "by recovery on the next write or gateway start",
                }
            raise
        committed = True
        txs.conclude(
            plan.operation_id,
            {
                "status": "committed",
                "generation": config.generation,
                "provider_id": plan.provider_id,
                "credential_id": credential_id,
            },
        )
        return {
            "status": "committed",
            "operation_id": plan.operation_id,
            "generation": config.generation,
            "provider_id": plan.provider_id,
            "credential_id": credential_id,
            "upgraded_from_legacy": store.last_upgrade is not None,
        }
    finally:
        if not committed and not resolved:
            # Pre-commit failure: this operation's staged products are
            # removed and the record closed as aborted.
            try:
                if record.get("created_credential_id"):
                    creds.delete(record["created_credential_id"])
                if record.get("backup_path"):
                    try:
                        (state_dir / record["backup_path"]).unlink()
                    except OSError:
                        pass
                txs.conclude(
                    plan.operation_id,
                    {"status": "aborted", "generation": plan.expected_generation},
                )
            except Exception:
                pass
