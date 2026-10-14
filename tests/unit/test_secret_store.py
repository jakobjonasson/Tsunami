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
    SERVICE,
    TEST_SERVICE,
    KeyringSecretStore,
    LoginCredential,
    parse_enumerated_usernames,
    parse_legacy_usernames,
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
    with pytest.raises(TsunamiError, match="^credential lab is not in Credential Manager"):
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
    with pytest.raises(TsunamiError, match="is not in Credential Manager"):
        store.delete_login("ghost")


def test_secret_values_only_for_customer(store):
    store.set_secret("acme", "a", "va")
    store.set_secret("acme", "b", "vb")
    store.set_secret("other", "c", "vc")
    store.set_login("lab", LoginCredential("u", "pw"))
    assert sorted(store.secret_values("acme")) == ["va", "vb"]


def test_all_secret_values_spans_customers_and_logins_but_not_usernames(store):
    store.set_secret("acme", "a", "va")
    store.set_secret("other", "c", "vc")
    store.set_login("lab", LoginCredential("netadmin", "pw", "en"))
    assert sorted(store.all_secret_values()) == ["en", "pw", "va", "vc"]


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
        {"TargetName": "LegacyGeneric:target=tsunami/cred/lab", "UserName": "cred/lab"},
        {"TargetName": "tsunami/secret/acme/a", "UserName": "secret/acme/a"},
        {"TargetName": "tsunami/cred/lab", "UserName": "cred/lab"},  # duplicate
        {"TargetName": "tsunami-test/cred/other", "UserName": "cred/other"},
        {"TargetName": "tsunami/cred/evil", "UserName": "cred/different"},  # mismatch
        {"TargetName": "tsunami", "UserName": "cred/old"},  # the old layout: not listed
        {"TargetName": "cred/old2@tsunami", "UserName": "cred/old2"},
        {"TargetName": "tsunami/cred/x"},  # no UserName
    ]
    assert parse_enumerated_usernames("tsunami", creds) == ["cred/lab", "secret/acme/a"]


def test_parse_legacy_usernames():
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
    assert parse_legacy_usernames("tsunami", creds) == [
        "cred/lab",
        "secret/acme/a",
        "secret/acme/z",
    ]


def test_listing_uses_enumerator():
    creds = [
        {"TargetName": "tsunami/cred/b", "UserName": "cred/b"},
        {"TargetName": "tsunami/cred/a", "UserName": "cred/a"},
        {"TargetName": "tsunami/secret/acme/k", "UserName": "secret/acme/k"},
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
    backend = WinVaultKeyring()
    backend.persist = "local machine"  # as the app sets it; never enterprise (see conftest)
    keyring.set_keyring(backend)
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


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_tests_can_never_change_the_real_tsunami_entries():
    """conftest guards the real store for the whole run (a lost login was traced here)."""
    from keyring.backends.Windows import WinVaultKeyring

    backend = WinVaultKeyring()
    with pytest.raises(RuntimeError, match="real Credential Manager"):
        backend.set_password(SERVICE, "cred/guard-probe", "x")
    with pytest.raises(RuntimeError, match="real Credential Manager"):
        backend._delete_password(f"cred/guard-probe@{SERVICE}")
    with pytest.raises(RuntimeError, match="real Credential Manager"):
        backend.set_password(f"{SERVICE}/cred/guard-probe", "cred/guard-probe", "x")
    # keyring's default is enterprise persistence, which tests may never write anywhere
    with pytest.raises(RuntimeError, match="enterprise-persistence"):
        backend.set_password(f"{TEST_SERVICE}/cred/guard-probe", "cred/guard-probe", "x")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_writes_force_local_machine_persistence(monkeypatch):
    """Even on a backend left at keyring's enterprise default, the store writes with
    local-machine persistence: Windows lets a stale enterprise entry resurface over a
    newer local one with the same target, so Tsunami never writes enterprise."""
    from keyring.backends.Windows import WinVaultKeyring
    from win32ctypes.pywin32 import win32cred

    seen: list[int] = []
    backend = WinVaultKeyring()  # enterprise by default
    monkeypatch.setattr(
        WinVaultKeyring, "_set_password", lambda self, *a: seen.append(self.persist)
    )
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    try:
        KeyringSecretStore(TEST_SERVICE).set_secret("acme", "probe", "v")
    finally:
        keyring.set_keyring(previous)
    assert seen == [win32cred.CRED_PERSIST_LOCAL_MACHINE]


def test_writes_and_deletes_are_logged_by_name_never_value(tmp_path, caplog):
    store = KeyringSecretStore(index_path=tmp_path / "idx.json")
    with caplog.at_level("INFO", logger="tsunami.core.secret_store"):
        store.set_login("lab", LoginCredential("u", "Pw-Secret-99"))
        store.delete_login("lab")
    text = caplog.text
    assert "wrote cred/lab" in text and "deleting cred/lab" in text
    assert "test_secret_store.test_writes_and_deletes_are_logged_by_name_never_value" in text
    assert "Pw-Secret-99" not in text


# -- one entry per value, and moving the old shared layout ------------------------------


class FakeWinVault(KeyringBackend):
    """keyring's WinVaultKeyring logic over a dict of targets: the last username written
    under a service takes the bare target, the previous one moves to <user>@<service>."""

    priority = 1  # type: ignore[assignment]

    def __init__(self) -> None:
        super().__init__()
        self.targets: dict[str, tuple[str, str]] = {}  # target -> (UserName, value)
        self.corrupt_new_writes = False

    def get_password(self, service, username):
        hit = self.targets.get(service)
        if hit is None or hit[0] != username:
            hit = self.targets.get(f"{username}@{service}")
        return hit[1] if hit else None

    def set_password(self, service, username, password):
        if service in self.targets:
            user, value = self.targets[service]
            self.targets[f"{user}@{service}"] = (user, value)
        if self.corrupt_new_writes and "/" in service:
            password = password + "-garbled"
        self.targets[service] = (username, password)

    def delete_password(self, service, username):
        deleted = False
        for target in (service, f"{username}@{service}"):
            if self.targets.get(target, ("",))[0] == username:
                del self.targets[target]
                deleted = True
        if not deleted:
            raise keyring.errors.PasswordDeleteError(service)

    def _set_password(self, target, username, password):  # WinVault's direct CredWrite
        if self.corrupt_new_writes and "/" in target:
            password = password + "-garbled"
        self.targets[target] = (username, password)

    def enumerate(self) -> list[dict[str, str]]:
        return [{"TargetName": t, "UserName": u} for t, (u, _v) in self.targets.items()]


@pytest.fixture
def vault():
    backend = FakeWinVault()
    previous = keyring.get_keyring()
    keyring.set_keyring(backend)
    yield backend
    keyring.set_keyring(previous)


def test_each_value_has_its_own_target_and_writes_never_move_another(vault):
    s = KeyringSecretStore(enumerator=vault.enumerate)
    s.set_login("jj-router", LoginCredential("u1", "p1"))
    s.set_login("cml-lab", LoginCredential("u2", "p2"))
    s.set_login("jj-router", LoginCredential("u1", "p1b"))  # overwrite
    assert sorted(vault.targets) == ["tsunami/cred/cml-lab", "tsunami/cred/jj-router"]
    s.delete_login("jj-router")
    assert s.get_login("cml-lab") == LoginCredential("u2", "p2")
    assert s.list_logins() == ["cml-lab"]


def test_migrate_moves_the_old_shared_layout_including_a_duplicate(vault):
    # The state found on the bench: jj-router on the bare target AND at jj-router@,
    # another login at its compound target, and a secret.
    vault.targets = {
        "tsunami": ("cred/jj-router", "J-new"),
        "cred/jj-router@tsunami": ("cred/jj-router", "J-old"),
        "cred/cml-lab@tsunami": ("cred/cml-lab", "C"),
        "secret/acme/tacacs@tsunami": ("secret/acme/tacacs", "T"),
        "tsunami-test": ("cred/other", "O"),  # another service: left alone
    }
    s = KeyringSecretStore(enumerator=vault.enumerate)
    assert s.migrate_legacy() == 3
    assert vault.targets == {
        "tsunami/cred/jj-router": ("cred/jj-router", "J-new"),  # the bare one wins
        "tsunami/cred/cml-lab": ("cred/cml-lab", "C"),
        "tsunami/secret/acme/tacacs": ("secret/acme/tacacs", "T"),
        "tsunami-test": ("cred/other", "O"),
    }
    assert s.migrate_legacy() == 0  # nothing left to move


def test_migrate_keeps_an_existing_new_entry_and_drops_the_old_one(vault):
    vault.targets = {
        "tsunami/cred/lab": ("cred/lab", "NEW"),
        "tsunami": ("cred/lab", "OLD"),
    }
    s = KeyringSecretStore(enumerator=vault.enumerate)
    assert s.migrate_legacy() == 0
    assert vault.targets == {"tsunami/cred/lab": ("cred/lab", "NEW")}


def test_migrate_keeps_the_old_entry_when_the_copy_does_not_read_back(vault, caplog):
    vault.targets = {"tsunami": ("cred/lab", "OLD")}
    vault.corrupt_new_writes = True
    s = KeyringSecretStore(enumerator=vault.enumerate)
    with caplog.at_level("ERROR", logger="tsunami.core.secret_store"):
        assert s.migrate_legacy() == 0
    assert vault.targets["tsunami"] == ("cred/lab", "OLD")  # never deleted
    assert "did not read back" in caplog.text and "OLD" not in caplog.text


def test_migrate_without_enumeration_uses_the_index(tmp_path, memory_keyring):
    keyring.set_password("tsunami", "cred/lab", '{"username": "u", "password": "p"}')
    index = tmp_path / "idx.json"
    index.write_text('["cred/lab"]', encoding="utf-8")
    s = KeyringSecretStore(index_path=index)
    assert s.migrate_legacy() == 1
    assert keyring.get_password("tsunami", "cred/lab") is None
    assert s.get_login("lab") == LoginCredential("u", "p")
    assert s.list_logins() == ["lab"]


def test_check_missing_reports_only_what_this_process_did_not_delete(vault):
    s = KeyringSecretStore(enumerator=vault.enumerate)
    s.set_login("a", LoginCredential("u", "p"))
    s.set_login("b", LoginCredential("u", "p"))
    assert s.check_missing() == []  # first call records
    s.delete_login("a")  # Tsunami's own delete: not reported
    other = KeyringSecretStore(enumerator=vault.enumerate)
    other.delete_login("b")  # something else removes it
    assert s.check_missing() == ["cred/b"]
    assert s.check_missing() == []  # reported once
    s.set_login("c", LoginCredential("u", "p"))
    assert s.check_missing() == []
