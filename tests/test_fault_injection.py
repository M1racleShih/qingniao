"""Fault injection at every transaction persistence boundary.

Each test interrupts the commit sequence at one boundary (I/O failure or
simulated process death via a hand-built partial state) and asserts the
recovery outcome: a complete old or complete new configuration, staged
orphans removed, and follow-up writes never stepping over an unresolved
transaction.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from qingniao import errors, state as state_mod
from qingniao.config import ConfigStore, validate_config
from qingniao.credentials import CredentialStore
from qingniao.gateway import Gateway
from qingniao.transactions import (
    TransactionStore,
    commit_import_offline,
    recover_open_transactions,
)
from tests.test_transactions import LEGACY_CONFIG, OP_ID, SOURCE, plan_for, source_bytes, write_legacy

TOKEN = "sk-ant-api03-SYNTHETIC-TOKEN"


@pytest.fixture
def state_dir(tmp_path) -> Path:
    state_mod.prepare_state_dir(tmp_path)
    return tmp_path


# ------------------------------------------------- simulated process deaths


def test_crash_between_credential_creation_and_record_update(state_dir):
    """The credential file exists but no record mentions it: a staging
    orphan that recovery must remove while the old configuration stays."""
    write_legacy(state_dir)
    legacy = (state_dir / "config.json").read_bytes()
    txs = TransactionStore(state_dir)
    record = {
        "operation_id": OP_ID,
        "expected_generation": 0,
        "provider_id": "api-anthropic-test",
        "created_credential_id": None,
        "backup_path": None,
        "plan_digest": "0" * 64,
        "conclusion": None,
    }
    txs.open(record)
    orphan = CredentialStore(state_dir).create(TOKEN)  # crash point: record not yet updated

    outcomes = recover_open_transactions(state_dir)
    assert outcomes == [{"operation_id": OP_ID, "status": "aborted"}]
    assert (state_dir / "config.json").read_bytes() == legacy
    assert not (state_dir / "credentials" / orphan).exists()
    assert list((state_dir / "credentials").iterdir()) == []


def test_orphan_cleanup_keeps_rotated_versions_and_config_references(state_dir):
    """Rotated old versions created by committed records stay; only files
    no committed record or configuration ever owned are removed."""
    # first commit: provider with credential A
    first = commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    assert first["status"] == "committed"
    cred_a = first["credential_id"]

    # rotate: same provider updated to a new secret -> credential B
    rotation_source = json.loads(json.dumps(SOURCE))
    rotation_source["env"]["ANTHROPIC_AUTH_TOKEN"] = "sk-ant-api03-SYNTHETIC-ROTATED"
    raw = json.dumps(rotation_source).encode()
    from qingniao.claude_source import parse_claude_settings
    from qingniao.importing import ImportDecisions, build_plan, source_digest

    plan = build_plan(
        parse_claude_settings(raw, source=Path("/tmp/settings.json")),
        source_digest=source_digest(raw),
        current=ConfigStore(state_dir / "config.json").load(),
        credential_store=CredentialStore(state_dir),
        decisions=ImportDecisions(conflict="update"),
        operation_id="op_" + "6" * 32,
    )
    second = commit_import_offline(state_dir=state_dir, plan=plan, raw_source=raw)
    assert second["status"] == "committed"
    cred_b = second["credential_id"]

    orphan = CredentialStore(state_dir).create("sk-ant-api03-SYNTHETIC-ORPHAN")
    stranger = state_dir / "credentials" / "user-notes.txt"
    stranger.write_text("left alone")

    recover_open_transactions(state_dir)
    remaining = {p.name for p in (state_dir / "credentials").iterdir()}
    assert cred_a in remaining  # rotated version kept
    assert cred_b in remaining  # active version kept
    assert orphan not in remaining
    assert "user-notes.txt" in remaining


# ------------------------------------------------- injected I/O failures


def test_transaction_record_write_failure_writes_nothing(state_dir, monkeypatch):
    write_legacy(state_dir)
    legacy = (state_dir / "config.json").read_bytes()
    from qingniao import transactions as tx_mod

    def broken_write(path, payload, mode=0o600):
        raise OSError("injected record write failure")

    monkeypatch.setattr(tx_mod, "atomic_write_json", broken_write)
    with pytest.raises(OSError):
        commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    monkeypatch.undo()
    assert (state_dir / "config.json").read_bytes() == legacy
    assert not (state_dir / "credentials").exists()
    assert recover_open_transactions(state_dir) == []


def test_backup_failure_aborts_with_old_config_intact(state_dir, monkeypatch):
    write_legacy(state_dir)
    legacy = (state_dir / "config.json").read_bytes()

    def broken_backup(self):
        raise OSError("injected backup failure")

    monkeypatch.setattr(ConfigStore, "backup_legacy_now", broken_backup)
    with pytest.raises(errors.ApiError):
        commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    monkeypatch.undo()
    assert (state_dir / "config.json").read_bytes() == legacy
    assert not (state_dir / "config.backup.pre-v1.json").exists()
    assert list((state_dir / "credentials").iterdir()) == []
    record = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert record["conclusion"]["status"] == "aborted"


def test_receipt_write_failure_after_commit_is_repaired_by_next_write(state_dir, monkeypatch):
    write_legacy(state_dir)
    from qingniao import transactions as tx_mod

    real_conclude = TransactionStore.conclude
    injected = {"first": True}

    def flaky_conclude(self, operation_id, conclusion):
        if injected["first"]:
            injected["first"] = False
            raise OSError("injected receipt failure")
        return real_conclude(self, operation_id, conclusion)

    monkeypatch.setattr(TransactionStore, "conclude", flaky_conclude)
    with pytest.raises(OSError):
        commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    monkeypatch.undo()

    # the commit point was passed: the new configuration is complete and the
    # open record is resolved by the next write entry, which then proceeds.
    config = json.loads((state_dir / "config.json").read_text())
    assert config["last_operation"] == OP_ID
    assert config["providers"]["api-anthropic-test"]["credential_id"]

    store = ConfigStore(state_dir / "config.json")
    gateway = Gateway(store.load(), store, credential_store=CredentialStore(state_dir))
    gateway.apply_config(config)  # runs recovery first
    record = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert record["conclusion"]["status"] == "committed"
    assert (state_dir / "credentials" / config["providers"]["api-anthropic-test"]["credential_id"]).exists()


# ------------------------------------------------- writes never step over damage


def test_write_entry_refuses_when_record_is_unreadable(state_dir, tmp_path):
    write_legacy(state_dir)
    tx_dir = state_dir / "transactions"
    tx_dir.mkdir(parents=True)
    (tx_dir / f"{OP_ID}.json").write_text("{corrupt")

    store = ConfigStore(state_dir / "config.json")
    gateway = Gateway(store.load(), store, credential_store=CredentialStore(state_dir))
    before = (state_dir / "config.json").read_bytes()
    with pytest.raises(errors.ApiError) as excinfo:
        gateway.apply_config(json.loads(before))
    assert excinfo.value.code == "transaction_damaged"
    assert (state_dir / "config.json").read_bytes() == before


def test_gateway_startup_refuses_damaged_committed_transaction(state_dir):
    write_legacy(state_dir)
    record = {
        "operation_id": OP_ID,
        "expected_generation": 0,
        "provider_id": "api-anthropic-test",
        "created_credential_id": CredentialStore(state_dir).create(TOKEN),
        "backup_path": None,
        "plan_digest": "0" * 64,
        "conclusion": None,
    }
    TransactionStore(state_dir).open(record)
    data = validate_config(
        {
            "schema_version": 1,
            "generation": 1,
            "last_operation": OP_ID,
            "providers": {
                "p": {"base_url": "https://x.test", "credential_id": record["created_credential_id"], "auth": "bearer"}
            },
            "models": {},
            "defaults": {"model": None, "aux_model": None, "routes": {}},
        }
    )
    ConfigStore(state_dir / "config.json").apply(data.to_json())
    (state_dir / "credentials" / record["created_credential_id"]).unlink()

    with pytest.raises(errors.ApiError) as excinfo:
        recover_open_transactions(state_dir)
    assert excinfo.value.code == "transaction_damaged"


def test_subsequent_offline_commit_recovers_before_proceeding(state_dir):
    write_legacy(state_dir)
    # stage an interrupted, never-committed transaction
    record = {
        "operation_id": OP_ID,
        "expected_generation": 0,
        "provider_id": "api-anthropic-test",
        "created_credential_id": CredentialStore(state_dir).create(TOKEN),
        "backup_path": "config.backup.pre-v1.json",
        "plan_digest": "0" * 64,
        "conclusion": None,
    }
    TransactionStore(state_dir).open(record)
    (state_dir / "config.backup.pre-v1.json").write_text("stale-backup")

    result = commit_import_offline(
        state_dir=state_dir,
        plan=plan_for(state_dir, operation_id="op_" + "8" * 32),
        raw_source=source_bytes(),
    )
    assert result["status"] == "committed"
    closed = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert closed["conclusion"]["status"] == "aborted"
    # the stale staging backup was cleaned; the committing transaction made
    # its own backup of the legacy bytes and keeps it for manual rollback
    backup = state_dir / "config.backup.pre-v1.json"
    assert backup.exists()
    assert json.loads(backup.read_text()) == LEGACY_CONFIG
    assert list((state_dir / "credentials").iterdir()) == [
        state_dir / "credentials" / result["credential_id"]
    ]
