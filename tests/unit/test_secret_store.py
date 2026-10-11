from __future__ import annotations

import sys
from pathlib import Path

import keyring
import keyring.errors
import pytest
from keyring.backend import KeyringBackend

from tsunami.core.issues import TsunamiError
from tsunami.core.secret_store import (
    MAX_VALUE_BYTES,
    TEST_SERVICE,
    KeyringSecretStore,
    LoginCredential,
    parse_enumerated_usernames,
)


@pytest.fixture
def store(tmp_path: Path) -> KeyringSecretStore:
    return KeyringSecretStore(index_path=tmp_path / "credential-index.json")


def test_login_round_trip_with_and_without_enable(store):
    store.set_login("lab", LoginCredential("admin", "pw1", "en1"))
    store.set_login("plain", LoginCredential("ops", "pw2"))
    assert store.get_login("lab") == LoginCredential("admin", "pw1", "en1")
    assert store.get_login("plain").enable is None
    store.delete_login("lab")
    with pytest.raises(TsunamiError, match="no credential named credential lab"):
        store.get_login("lab")


def test_secret_round_trip_and_overwrite(store):
    store.set_secret("acme", "snmp", "v1")
    store.set_secret("acme", "snmp", "v2")
    assert store.get_secret("acme", "snmp") == "v2"
    store.delete_secret("acme", "snmp")
    with pytest.raises(TsunamiError):
        store.get_secret("acme", "snmp")


def test_listing_after_sets_and_deletes(store):
    store.set_login("b", LoginCredential("u", "p"))
    store.set_login("a", LoginCredential("u", "p"))
    store.set_secret("acme", "zed", "x")
    store.set_secret("acme", "alpha", "y")
    store.set_secret("other", "k", "z")
    assert store.list_logins() == ["a", "b"]
    assert store.list_secrets("acme") == ["alpha", "zed"]
    store.delete_login("a")
    store.delete_secret("acme", "zed")
    assert store.list_logins() == ["b"]
    assert store.list_secrets("acme") == ["alpha"]


def test_index_holds_names_only(store, tmp_path):
    store.set_secret("acme", "snmp", "hunter2-value")
    text = (tmp_path / "credential-index.json").read_text(encoding="utf-8")
    assert "secret/acme/snmp" in text
    assert "hunter2-value" not in text


def test_delete_missing_raises(store):
    with pytest.raises(TsunamiError, match="no credential named"):
        store.delete_login("ghost")


def test_secret_values_only_for_customer(store):
    store.set_secret("acme", "a", "va")
    store.set_secret("acme", "b", "vb")
    store.set_secret("other", "c", "vc")
    store.set_login("lab", LoginCredential("u", "pw"))
    assert sorted(store.secret_values("acme")) == ["va", "vb"]


@pytest.mark.parametrize("bad", ["../x", "Acme", "a/b", "", "a b"])
def test_slug_validation(store, bad):
    with pytest.raises(TsunamiError):
        store.set_login(bad, LoginCredential("u", "p"))
    with pytest.raises(TsunamiError):
        store.set_secret(bad, "n", "v")
    with pytest.raises(TsunamiError):
        store.set_secret("acme", bad, "v")
    with pytest.raises(TsunamiError):
        store.list_secrets(bad)


def test_size_limit_boundary_is_utf16(store):
    limit_chars = MAX_VALUE_BYTES // 2
    store.set_secret("acme", "ok", "x" * limit_chars)
    with pytest.raises(TsunamiError, match="limit"):
        store.set_secret("acme", "big", "x" * (limit_chars + 1))
    assert store.list_secrets("acme") == ["ok"]


def test_login_size_counts_json(store):
    with pytest.raises(TsunamiError, match="limit"):
        store.set_login("big", LoginCredential("u", "p" * 1300))


def test_missing_does_not_leak(store):
    store.set_secret("acme", "real", "topsecretvalue")
    with pytest.raises(TsunamiError) as exc:
        store.get_secret("acme", "nope")
    assert "topsecretvalue" not in str(exc.value)
    assert "acme/nope" in str(exc.value)


def test_repr_has_no_password(store):
    cred = LoginCredential("admin", "pw-secret", "en-secret")
    assert "pw-secret" not in repr(cred)
    assert "en-secret" not in repr(cred)
    store.set_login("lab", cred)
    assert "pw-secret" not in repr(store) + str(store)


def test_malformed_login_does_not_leak(store):
    keyring.set_password("tsunami", "cred/bad", "not-json-SECRETBITS")
    with pytest.raises(TsunamiError) as exc:
        store.get_login("bad")
    assert "SECRETBITS" not in str(exc.value)


class FailingKeyring(KeyringBackend):
    priority = 1  # type: ignore[assignment]

    def get_password(self, service, username):
        raise RuntimeError("boom: topsecretvalue")

    def set_password(self, service, username, password):
        raise RuntimeError(f"cannot store {password}")

    def delete_password(self, service, username):
        raise RuntimeError("boom: topsecretvalue")


def test_backend_exception_text_not_propagated(tmp_path):
    previous = keyring.get_keyring()
    keyring.set_keyring(FailingKeyring())
    try:
        s = KeyringSecretStore(index_path=tmp_path / "i.json")
        for call in (
            lambda: s.set_secret("acme", "k", "topsecretvalue"),
            lambda: s.get_secret("acme", "k"),
            lambda: s.delete_secret("acme", "k"),
        ):
            with pytest.raises(TsunamiError) as exc:
                call()
            assert "topsecretvalue" not in str(exc.value)
            assert exc.value.__cause__ is None
            assert exc.value.__suppress_context__
    finally:
        keyring.set_keyring(previous)


def test_parse_enumerated_usernames():
    creds = [
        {"TargetName": "LegacyGeneric:target=tsunami", "UserName": "secret/acme/z"},
        {"TargetName": "LegacyGeneric:target=cred/lab@tsunami", "UserName": "cred/lab"},
        {"TargetName": "secret/acme/a@tsunami", "UserName": "secret/acme/a"},
        {"TargetName": "cred/lab@tsunami", "UserName": "cred/lab"},  # duplicate
        {"TargetName": "tsunami-test", "UserName": "cred/other"},
        {"TargetName": "x@tsunami-test", "UserName": "x"},
        {"TargetName": "unrelated", "UserName": "cred/nope"},
        {"TargetName": "evil@tsunami", "UserName": "cred/different"},  # target/user mismatch
        {"TargetName": "tsunami"},  # no UserName
    ]
    assert parse_enumerated_usernames("tsunami", creds) == [
        "cred/lab",
        "secret/acme/a",
        "secret/acme/z",
    ]


def test_listing_uses_enumerator():
    creds = [
        {"TargetName": "tsunami", "UserName": "cred/b"},
        {"TargetName": "cred/a@tsunami", "UserName": "cred/a"},
        {"TargetName": "secret/acme/k@tsunami", "UserName": "secret/acme/k"},
    ]
    s = KeyringSecretStore(enumerator=lambda: creds)
    assert s.list_logins() == ["a", "b"]
    assert s.list_secrets("acme") == ["k"]


def test_listing_without_index_or_enumeration_raises():
    with pytest.raises(TsunamiError, match="cannot list"):
        KeyringSecretStore().list_logins()


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_real_vault_round_trip_and_listing():
    from keyring.backends.Windows import WinVaultKeyring

    previous = keyring.get_keyring()
    keyring.set_keyring(WinVaultKeyring())
    s = KeyringSecretStore(TEST_SERVICE)
    try:
        s.set_login("t1", LoginCredential("u", "p", "e"))
        s.set_secret("acme", "k", "vvvv")
        s.set_secret("acme", "k2", "wwww")
        assert s.get_login("t1") == LoginCredential("u", "p", "e")
        assert s.list_logins() == ["t1"]
        assert s.list_secrets("acme") == ["k", "k2"]
        s.delete_secret("acme", "k")
        assert s.list_secrets("acme") == ["k2"]
    finally:
        for fn in (
            lambda: s.delete_login("t1"),
            lambda: s.delete_secret("acme", "k"),
            lambda: s.delete_secret("acme", "k2"),
        ):
            try:
                fn()
            except Exception:  # noqa: BLE001, S110
                pass
        keyring.set_keyring(previous)
