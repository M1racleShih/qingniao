from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from qingniao import errors
from qingniao.credentials import CredentialStore

SECRET = "sk-ant-api03-SYNTHETIC-SECRET-0123456789"


def test_create_read_roundtrip_with_restrictive_modes(tmp_path):
    store = CredentialStore(tmp_path)
    cred_id = store.create(SECRET)
    assert cred_id.startswith("cred_") and len(cred_id) == len("cred_") + 32
    cred_path = tmp_path / "credentials" / cred_id
    assert cred_path.exists()
    assert stat.S_IMODE(cred_path.stat().st_mode) == 0o600
    assert stat.S_IMODE((tmp_path / "credentials").stat().st_mode) == 0o700
    assert store.read(cred_id) == SECRET


def test_create_rejects_empty_or_non_string_secret(tmp_path):
    store = CredentialStore(tmp_path)
    for bad in ("", "   \n", 5, None):
        with pytest.raises(errors.ApiError) as excinfo:
            store.create(bad)
        assert excinfo.value.code == "credential_invalid"
        assert SECRET not in excinfo.value.message


def test_ids_are_random_and_versions_immutable(tmp_path):
    store = CredentialStore(tmp_path)
    first = store.create(SECRET)
    second = store.create(SECRET)
    assert first != second
    assert not hasattr(store, "update")


def test_read_rejects_malformed_ids_without_touching_disk(tmp_path):
    store = CredentialStore(tmp_path)
    store.create(SECRET)
    outside = tmp_path / "outside.txt"
    outside.write_text("sentinel")
    for bad in ("../outside.txt", "nope", "CRED_" + "0" * 32, "cred_" + "0" * 31, "", "cred_../x"):
        with pytest.raises(errors.ApiError) as excinfo:
            store.read(bad)
        assert excinfo.value.code == "credential_invalid"
    assert outside.read_text() == "sentinel"


def test_read_missing_credential_is_sanitized(tmp_path):
    store = CredentialStore(tmp_path)
    missing = "cred_" + "f" * 32
    with pytest.raises(errors.ApiError) as excinfo:
        store.read(missing)
    assert excinfo.value.code == "credential_missing"
    assert SECRET not in excinfo.value.message


def test_read_rejects_symlinked_secret_file(tmp_path):
    store = CredentialStore(tmp_path)
    cred_id = store.create(SECRET)
    cred_path = tmp_path / "credentials" / cred_id
    target = tmp_path / "leak.txt"
    target.write_text("other-secret")
    cred_path.unlink()
    cred_path.symlink_to(target)
    with pytest.raises(errors.ApiError) as excinfo:
        store.read(cred_id)
    assert excinfo.value.code == "credential_unreadable"
    assert "symlink" in excinfo.value.message or "link" in excinfo.value.message


def test_read_rejects_non_regular_file(tmp_path):
    store = CredentialStore(tmp_path)
    (tmp_path / "credentials").mkdir(mode=0o700)
    cred_id = "cred_" + "a" * 32
    os.mkfifo(tmp_path / "credentials" / cred_id)
    with pytest.raises(errors.ApiError) as excinfo:
        store.read(cred_id)
    assert excinfo.value.code == "credential_unreadable"


def test_read_rejects_secret_file_with_group_or_other_bits(tmp_path):
    store = CredentialStore(tmp_path)
    cred_id = store.create(SECRET)
    cred_path = tmp_path / "credentials" / cred_id
    os.chmod(cred_path, 0o644)
    with pytest.raises(errors.ApiError) as excinfo:
        store.read(cred_id)
    assert excinfo.value.code == "credential_unreadable"
    assert SECRET not in excinfo.value.message


def test_read_rejects_private_directory_with_group_or_other_bits(tmp_path):
    store = CredentialStore(tmp_path)
    cred_id = store.create(SECRET)
    os.chmod(tmp_path / "credentials", 0o755)
    with pytest.raises(errors.ApiError) as excinfo:
        store.read(cred_id)
    assert excinfo.value.code == "credential_unreadable"
    assert SECRET not in excinfo.value.message


def test_read_rejects_symlinked_private_directory(tmp_path):
    store = CredentialStore(tmp_path)
    cred_id = store.create(SECRET)
    real = tmp_path / "credentials"
    moved = tmp_path / "credentials-real"
    real.rename(moved)
    real.symlink_to(moved)
    with pytest.raises(errors.ApiError) as excinfo:
        store.read(cred_id)
    assert excinfo.value.code == "credential_unreadable"


def test_read_rejects_empty_secret_file(tmp_path):
    store = CredentialStore(tmp_path)
    cred_id = store.create(SECRET)
    (tmp_path / "credentials" / cred_id).write_text("")
    with pytest.raises(errors.ApiError) as excinfo:
        store.read(cred_id)
    assert excinfo.value.code == "credential_unreadable"


def test_exists_and_delete(tmp_path):
    store = CredentialStore(tmp_path)
    cred_id = store.create(SECRET)
    assert store.exists(cred_id)
    store.delete(cred_id)
    assert not store.exists(cred_id)
    with pytest.raises(errors.ApiError):
        store.read(cred_id)
    store.delete(cred_id)  # idempotent cleanup
    store.delete("not-an-id")  # malformed ids never touch the disk
    assert list((tmp_path / "credentials").iterdir()) == []


def test_create_rejects_symlinked_private_directory(tmp_path):
    store = CredentialStore(tmp_path)
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / "credentials"
    link.symlink_to(real)
    with pytest.raises(errors.ApiError) as excinfo:
        store.create(SECRET)
    assert excinfo.value.code == "credential_unreadable"
    assert list(real.iterdir()) == []


def test_store_reports_directory_location_without_secrets(tmp_path):
    store = CredentialStore(Path(tmp_path))
    store.create(SECRET)
    assert "credentials" in str(store.path)
