from __future__ import annotations

import json
from pathlib import Path

import pytest

from qingniao import errors, state as state_mod
from qingniao.claude_source import parse_claude_settings
from qingniao.config import ConfigStore, validate_config
from qingniao.credentials import CredentialStore
from qingniao.importing import ImportDecisions, build_plan, source_digest
from qingniao.transactions import (
    TransactionStore,
    commit_import_offline,
    recover_open_transactions,
)

TOKEN = "sk-ant-api03-SYNTHETIC-TOKEN"
OP_ID = "op_" + "3" * 32

LEGACY_CONFIG = {
    "providers": {
        "prov-p": {"base_url": "http://127.0.0.1:9101", "credential_env": "QING_TEST_P", "auth": "bearer"},
    },
    "models": {"model-p": {"provider": "prov-p", "upstream_model": "vendor/p"}},
    "defaults": {"model": "req-main", "aux_model": "req-aux", "routes": {"req-main": "model-p", "req-aux": "model-p"}},
}

SOURCE = {
    "env": {
        "ANTHROPIC_BASE_URL": "https://api.anthropic.test",
        "ANTHROPIC_AUTH_TOKEN": TOKEN,
        "ANTHROPIC_MODEL": "claude-main",
        "CLAUDE_CODE_SUBAGENT_MODEL": "claude-aux-upstream",
    }
}


def source_bytes() -> bytes:
    return json.dumps(SOURCE).encode()


@pytest.fixture
def state_dir(tmp_path) -> Path:
    state_mod.prepare_state_dir(tmp_path)
    return tmp_path


def write_legacy(state_dir: Path) -> bytes:
    raw = json.dumps(LEGACY_CONFIG, indent=2).encode()
    (state_dir / "config.json").write_bytes(raw)
    return raw


def plan_for(state_dir: Path, *, operation_id: str = OP_ID, decisions=None) -> object:
    store = ConfigStore(state_dir / "config.json")
    parsed = parse_claude_settings(source_bytes(), source=Path("/tmp/settings.json"))
    return build_plan(
        parsed,
        source_digest=source_digest(source_bytes()),
        current=store.load(),
        credential_store=CredentialStore(state_dir),
        decisions=decisions or ImportDecisions(),
        operation_id=operation_id,
    )


# ---------------------------------------------------------------- success path


def test_commit_success_upgrades_legacy_with_backup_and_receipt(state_dir):
    legacy = write_legacy(state_dir)
    result = commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    assert result["status"] == "committed"
    assert result["generation"] == 1
    assert result["operation_id"] == OP_ID

    config = json.loads((state_dir / "config.json").read_text())
    assert config["schema_version"] == 1
    assert config["generation"] == 1
    assert config["last_operation"] == OP_ID
    provider = config["providers"]["api-anthropic-test"]
    assert provider["credential_id"] == result["credential_id"]
    assert "credential_env" not in provider
    assert provider["auth"] == "bearer"
    assert config["models"]["claude-main"] == {"provider": "api-anthropic-test", "upstream_model": "claude-main"}
    assert config["defaults"]["model"] == "claude-main"
    # legacy content is preserved alongside
    assert config["providers"]["prov-p"]["credential_env"] == "QING_TEST_P"

    backup = state_dir / "config.backup.pre-v1.json"
    assert backup.read_bytes() == legacy

    secret_file = state_dir / "credentials" / result["credential_id"]
    assert secret_file.read_text() == TOKEN

    record = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert record["conclusion"]["status"] == "committed"
    assert record["conclusion"]["generation"] == 1
    assert record["created_credential_id"] == result["credential_id"]
    assert record["backup_path"] == "config.backup.pre-v1.json"


def test_commit_onto_v1_skips_backup_and_keeps_generation_sequence(state_dir):
    write_legacy(state_dir)
    store = ConfigStore(state_dir / "config.json")
    store.apply(LEGACY_CONFIG)  # upgrades the legacy file: v1 generation 1 plus a backup
    backup = state_dir / "config.backup.pre-v1.json"
    assert backup.exists()
    first_backup = backup.read_bytes()

    result = commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    assert result["status"] == "committed"
    assert result["generation"] == 2
    # no second backup; the first pre-v1 snapshot stays
    assert backup.read_bytes() == first_backup
    record = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert record["backup_path"] is None


def test_repeat_import_is_unchanged_without_new_files(state_dir):
    first = commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    cred = first["credential_id"]
    tx_files = set((state_dir / "transactions").iterdir())
    second = commit_import_offline(
        state_dir=state_dir,
        plan=plan_for(state_dir, operation_id="op_" + "4" * 32),
        raw_source=source_bytes(),
    )
    assert second["status"] == "unchanged"
    assert second["credential_id"] == cred
    assert set((state_dir / "transactions").iterdir()) == tx_files


# ---------------------------------------------------------------- failure paths


def test_credential_stage_failure_aborts_and_preserves_old_config(state_dir, monkeypatch):
    legacy = write_legacy(state_dir)

    def broken_create(self, secret):
        raise errors.ApiError(500, errors.CREDENTIAL_UNREADABLE, "injected stage failure")

    monkeypatch.setattr(CredentialStore, "create", broken_create)
    with pytest.raises(errors.ApiError):
        commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    assert (state_dir / "config.json").read_bytes() == legacy
    assert not (state_dir / "credentials").exists() or list((state_dir / "credentials").iterdir()) == []
    record = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert record["conclusion"]["status"] == "aborted"
    assert not (state_dir / "config.backup.pre-v1.json").exists()


def test_persist_failure_at_commit_point_is_unconfirmed_then_recovered(state_dir, monkeypatch):
    legacy = write_legacy(state_dir)
    real_apply = ConfigStore.apply

    def failing_apply(self, data, **kwargs):
        raise errors.ApiError(500, errors.CONFIG_PERSIST_FAILED, "injected persist failure")

    monkeypatch.setattr(ConfigStore, "apply", failing_apply)
    result = commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
    assert result["status"] == "unconfirmed"
    record = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert record["conclusion"]["status"] == "unconfirmed"
    monkeypatch.setattr(ConfigStore, "apply", real_apply)

    # the replace never happened; a later recovery resolves to aborted and cleans up
    outcomes = recover_open_transactions(state_dir)
    assert outcomes and outcomes[0]["status"] == "aborted"
    assert (state_dir / "config.json").read_bytes() == legacy
    staged = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert staged["conclusion"]["status"] == "aborted"
    assert not (state_dir / "config.backup.pre-v1.json").exists()
    assert list((state_dir / "credentials").iterdir()) == []


def test_generation_conflict_rejected_before_any_write(state_dir):
    write_legacy(state_dir)
    plan = plan_for(state_dir)  # built against the legacy file (generation 0)
    store = ConfigStore(state_dir / "config.json")
    store.apply(LEGACY_CONFIG)  # another writer advanced the disk to generation 1
    before = (state_dir / "config.json").read_bytes()
    with pytest.raises(errors.ApiError) as excinfo:
        commit_import_offline(state_dir=state_dir, plan=plan, raw_source=source_bytes())
    assert excinfo.value.code == "generation_conflict"
    assert (state_dir / "config.json").read_bytes() == before
    assert not (state_dir / "transactions").exists()


def test_changed_source_rejected_before_writes(state_dir):
    write_legacy(state_dir)
    plan = plan_for(state_dir)
    with pytest.raises(Exception) as excinfo:
        commit_import_offline(
            state_dir=state_dir,
            plan=plan,
            raw_source=json.dumps({**SOURCE, "env": {**SOURCE["env"], "ANTHROPIC_MODEL": "other"}}).encode(),
        )
    assert excinfo.value.code == "source_changed"
    assert not (state_dir / "transactions").exists()


def test_offline_commit_requires_gateway_lock(state_dir):
    write_legacy(state_dir)
    lock = state_mod.GatewayLock(state_dir)
    lock.acquire()
    try:
        with pytest.raises(errors.ApiError) as excinfo:
            commit_import_offline(state_dir=state_dir, plan=plan_for(state_dir), raw_source=source_bytes())
        assert excinfo.value.code == "gateway_locked"
        assert not (state_dir / "transactions").exists()
    finally:
        lock.release()


# ---------------------------------------------------------------- recovery


def _stage_open_transaction(state_dir: Path, *, committed: bool) -> dict:
    creds = CredentialStore(state_dir)
    cred_id = creds.create(TOKEN)
    backup = state_dir / "config.backup.pre-v1.json"
    backup.write_bytes(b"legacy-bytes")
    record = {
        "operation_id": OP_ID,
        "expected_generation": 0,
        "created_credential_id": cred_id,
        "backup_path": "config.backup.pre-v1.json",
        "plan_digest": "0" * 64,
        "conclusion": None,
    }
    TransactionStore(state_dir).open(record)
    if committed:
        data = validate_config(
            {
                "schema_version": 1,
                "generation": 1,
                "last_operation": OP_ID,
                "providers": {"p": {"base_url": "https://x.test", "credential_id": cred_id, "auth": "bearer"}},
                "models": {},
                "defaults": {"model": None, "aux_model": None, "routes": {}},
            }
        ).to_json()
        ConfigStore(state_dir / "config.json").apply(data)
    return record


def test_recovery_after_commit_point_crash_completes_receipt(state_dir):
    record = _stage_open_transaction(state_dir, committed=True)
    config_before = (state_dir / "config.json").read_bytes()
    outcomes = recover_open_transactions(state_dir)
    assert outcomes == [{"operation_id": OP_ID, "status": "committed"}]
    assert (state_dir / "config.json").read_bytes() == config_before
    assert (state_dir / "credentials" / record["created_credential_id"]).exists()
    assert (state_dir / "config.backup.pre-v1.json").exists()  # committed: backup is kept
    closed = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert closed["conclusion"]["status"] == "committed"


def test_recovery_before_commit_cleans_staging(state_dir):
    legacy = write_legacy(state_dir)
    record = _stage_open_transaction(state_dir, committed=False)
    outcomes = recover_open_transactions(state_dir)
    assert outcomes == [{"operation_id": OP_ID, "status": "aborted"}]
    assert (state_dir / "config.json").read_bytes() == legacy
    assert not (state_dir / "credentials" / record["created_credential_id"]).exists()
    assert not (state_dir / "config.backup.pre-v1.json").exists()
    closed = json.loads((state_dir / "transactions" / f"{OP_ID}.json").read_text())
    assert closed["conclusion"]["status"] == "aborted"


def test_recovery_of_damaged_committed_transaction_refuses(state_dir):
    record = _stage_open_transaction(state_dir, committed=True)
    (state_dir / "credentials" / record["created_credential_id"]).unlink()
    with pytest.raises(errors.ApiError) as excinfo:
        recover_open_transactions(state_dir)
    assert excinfo.value.code == "transaction_damaged"


def test_recovery_is_noop_without_open_transactions(state_dir):
    write_legacy(state_dir)
    assert recover_open_transactions(state_dir) == []
