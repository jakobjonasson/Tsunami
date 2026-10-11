"""The Tsunami password vault (core/vault.py) and the encrypted secret store."""

from __future__ import annotations

import json

import keyring
import pytest

from tsunami.core import vault as vault_module
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.vault import Cipher, LockedCipher, Vault, VaultError, VaultLocked

PASSWORD = "correct horse battery"


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(vault_module, "_SCRYPT_N", 2**10)  # the real cost is 2**17


@pytest.fixture
def vault(tmp_path) -> Vault:
    return Vault(tmp_path / "vault.json")


@pytest.fixture
def store(tmp_path) -> KeyringSecretStore:
    return KeyringSecretStore(index_path=tmp_path / "idx.json")


def raw(name: str) -> str | None:
    return keyring.get_password("tsunami", name)


def test_create_and_unlock(vault):
    assert not vault.exists()
    created = vault.create(PASSWORD)
    assert vault.exists()
    data = json.loads(vault.path.read_text())
    assert PASSWORD not in vault.path.read_text() and data["kdf"] == "scrypt"
    assert vault.unlock(PASSWORD).key == created.key


@pytest.mark.parametrize("attempt", ["wrong password", "", PASSWORD.upper()])
def test_wrong_password(vault, attempt):
    vault.create(PASSWORD)
    with pytest.raises(VaultError, match="wrong Tsunami password"):
        vault.unlock(attempt)


def test_short_password_refused(vault):
    with pytest.raises(VaultError, match="at least 8"):
        vault.create("short")
    assert not vault.exists()


def test_damaged_vault_file(vault):
    vault.create(PASSWORD)
    vault.path.write_text("{not json")
    with pytest.raises(VaultError, match="damaged"):
        vault.unlock(PASSWORD)


def test_values_are_bound_to_their_entry():
    cipher = Cipher(b"k" * 32)
    token = cipher.encrypt("cred/a", "secret-value")
    assert token.startswith("tv1:") and "secret-value" not in token
    assert cipher.decrypt("cred/a", token) == "secret-value"
    with pytest.raises(VaultError):
        cipher.decrypt("cred/b", token)  # copied to another entry
    with pytest.raises(VaultError):
        Cipher(b"x" * 32).decrypt("cred/a", token)  # another password


def test_store_writes_ciphertext_and_reads_plaintext(store, vault):
    store.cipher = vault.create(PASSWORD)
    store.set_login("acme-admin", LoginCredential("netadmin", "Pw-12345", "En-67890"))
    store.set_secret("acme", "tacacs", "Tac-Key-55")
    for name in ("cred/acme-admin", "secret/acme/tacacs"):
        assert raw(name).startswith("tv1:")
    assert "Pw-12345" not in raw("cred/acme-admin") and "netadmin" not in raw("cred/acme-admin")
    assert store.get_login("acme-admin") == LoginCredential("netadmin", "Pw-12345", "En-67890")
    assert store.get_secret("acme", "tacacs") == "Tac-Key-55"


def test_locked_store_refuses_values_but_lists_names(store, vault):
    store.cipher = vault.create(PASSWORD)
    store.set_login("acme-admin", LoginCredential("netadmin", "Pw-12345"))
    store.cipher = LockedCipher()
    assert store.list_logins() == ["acme-admin"]
    with pytest.raises(VaultLocked):
        store.get_login("acme-admin")
    with pytest.raises(VaultLocked):
        store.set_login("other", LoginCredential("u", "p"))
    with pytest.raises(VaultLocked):
        store.all_secret_values()  # never a silently empty redaction list


def test_a_process_without_the_key_cannot_read(store, vault):
    store.cipher = vault.create(PASSWORD)
    store.set_secret("acme", "tacacs", "Tac-Key-55")
    plain_store = KeyringSecretStore(index_path=store._index_path)  # no cipher at all
    with pytest.raises(VaultLocked):
        plain_store.get_secret("acme", "tacacs")


def test_first_password_encrypts_existing_entries(store, vault):
    store.set_login("acme-admin", LoginCredential("netadmin", "Pw-12345"))  # before the vault
    store.set_secret("acme", "tacacs", "Tac-Key-55")
    assert raw("secret/acme/tacacs") == "Tac-Key-55"
    assert store.rekey(vault.create(PASSWORD)) == 2
    assert raw("secret/acme/tacacs").startswith("tv1:")
    assert store.get_secret("acme", "tacacs") == "Tac-Key-55"


def test_password_change_reencrypts(store, vault):
    old = vault.create(PASSWORD)
    store.cipher = old
    store.set_secret("acme", "tacacs", "Tac-Key-55")
    new = vault.create("a whole new password")
    store.rekey(new)
    assert store.get_secret("acme", "tacacs") == "Tac-Key-55"
    with pytest.raises(VaultError):
        old.decrypt("secret/acme/tacacs", raw("secret/acme/tacacs"))
    assert vault.unlock("a whole new password").key == new.key


def test_reset_deletes_everything(store, vault):
    store.cipher = vault.create(PASSWORD)
    store.set_login("acme-admin", LoginCredential("netadmin", "Pw-12345"))
    store.set_secret("acme", "tacacs", "Tac-Key-55")
    assert store.delete_everything() == 2
    assert store.list_logins() == [] and store.list_secrets("acme") == []


def test_failed_rekey_restores_every_entry(store, vault, monkeypatch):
    old = vault.create(PASSWORD)
    store.cipher = old
    store.set_secret("acme", "a", "Value-A-111")
    store.set_secret("acme", "b", "Value-B-222")
    before = {n: raw(n) for n in ("secret/acme/a", "secret/acme/b")}
    real_set, calls = keyring.set_password, []

    def flaky(service, name, value):
        calls.append(name)
        if len(calls) == 2:
            raise OSError("disk full")
        real_set(service, name, value)

    monkeypatch.setattr(keyring, "set_password", flaky)
    with pytest.raises(Exception):  # noqa: B017
        store.rekey(Cipher(b"n" * 32))
    monkeypatch.setattr(keyring, "set_password", real_set)
    assert {n: raw(n) for n in before} == before
    assert store.cipher is old and store.get_secret("acme", "a") == "Value-A-111"
