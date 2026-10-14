"""The Tsunami password in the GUI (gui/lock.py): first launch, unlock, lock, change, reset."""

from __future__ import annotations

import keyring
import pytest
from PySide6.QtWidgets import QMessageBox

from tsunami.core import vault as vault_module
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.vault import VaultLocked
from tsunami.gui import lock as lock_module
from tsunami.gui.lock import LockManager, SetPasswordDialog, UnlockDialog

PASSWORD = "first-password-1"


class Settings:
    auto_lock_minutes = 15


@pytest.fixture(autouse=True)
def fast_scrypt(monkeypatch):
    monkeypatch.setattr(vault_module, "_SCRYPT_N", 2**10)


@pytest.fixture
def store(tmp_path) -> KeyringSecretStore:
    return KeyringSecretStore(index_path=tmp_path / "idx.json")


@pytest.fixture
def manager(qapp, app_paths, store) -> LockManager:
    return LockManager(app_paths, store, Settings())


class Answers:
    """Plays the person at the dialogs: a list of (dialog class, action) in order."""

    def __init__(self, monkeypatch, *steps):
        self.steps = list(steps)
        monkeypatch.setattr(lock_module, "run_dialog", self.run)

    def run(self, dialog) -> bool:
        expected, action = self.steps.pop(0)
        assert isinstance(dialog, expected), f"expected {expected.__name__}, got {dialog}"
        return action(dialog)


def set_password(password, confirm=None, current=None):
    def act(dialog: SetPasswordDialog) -> bool:
        if current is not None:
            dialog.current_edit.setText(current)
        dialog.password_edit.setText(password)
        dialog.confirm_edit.setText(password if confirm is None else confirm)
        return dialog.save()

    return act


def unlock_with(password):
    def act(dialog: UnlockDialog) -> bool:
        dialog.password_edit.setText(password)
        return dialog.save()

    return act


def cancel(dialog) -> bool:
    return False


def test_first_launch_sets_the_password_and_encrypts_saved_logins(manager, store, monkeypatch):
    store.set_login("acme-admin", LoginCredential("netadmin", "Old-Plain-1"))
    Answers(monkeypatch, (SetPasswordDialog, set_password(PASSWORD)))
    assert manager.startup()
    assert manager.unlocked and manager.vault.exists()
    assert keyring.get_password("tsunami/cred/acme-admin", "cred/acme-admin").startswith("tv1:")
    assert store.get_login("acme-admin").password == "Old-Plain-1"


def test_mismatched_confirmation_keeps_the_dialog_open(manager, monkeypatch):
    dialog = SetPasswordDialog()
    dialog.password_edit.setText(PASSWORD)
    dialog.confirm_edit.setText("something-else")
    assert not dialog.save() and "do not match" in dialog.error.text()
    dialog.confirm_edit.setText(PASSWORD)
    dialog.password_edit.setText("short")
    assert not dialog.save()


def test_cancel_at_first_launch_exits(manager, monkeypatch):
    Answers(monkeypatch, (SetPasswordDialog, cancel))
    assert not manager.startup()
    assert not manager.vault.exists()


def test_unlock_after_a_wrong_password(manager, store, monkeypatch):
    manager.vault.create(PASSWORD)
    wrong = UnlockDialog(manager.vault)
    wrong.password_edit.setText("not it at all")
    assert not wrong.save() and wrong.error.text() == "Wrong Tsunami password"
    assert wrong.password_edit.text() == ""  # never left in the field
    Answers(monkeypatch, (UnlockDialog, unlock_with(PASSWORD)))
    assert manager.startup() and manager.unlocked


def test_lock_refuses_stored_values_until_unlocked(manager, store, monkeypatch):
    Answers(monkeypatch, (SetPasswordDialog, set_password(PASSWORD)))
    manager.startup()
    store.set_secret("acme", "tacacs", "Tac-Key-99")
    seen = []
    manager.lock_changed.connect(seen.append)
    manager.lock()
    assert not manager.unlocked and seen == [False]
    with pytest.raises(VaultLocked):
        store.get_secret("acme", "tacacs")
    Answers(monkeypatch, (UnlockDialog, unlock_with(PASSWORD)))
    assert manager.unlock_interactive()
    assert store.get_secret("acme", "tacacs") == "Tac-Key-99" and seen == [False, True]


def test_auto_lock_after_idle_minutes(manager, monkeypatch):
    Answers(monkeypatch, (SetPasswordDialog, set_password(PASSWORD)), (UnlockDialog, cancel))
    manager.startup()
    manager.check_idle()
    assert manager.unlocked  # not idle yet
    manager._last_input -= 16 * 60
    manager.check_idle()
    assert not manager.unlocked  # locked, and the unlock prompt was dismissed


def test_auto_lock_zero_means_never(manager, monkeypatch):
    Answers(monkeypatch, (SetPasswordDialog, set_password(PASSWORD)))
    manager.startup()
    manager.settings.auto_lock_minutes = 0
    manager._last_input -= 10 * 3600
    manager.check_idle()
    assert manager.unlocked
    manager.settings.auto_lock_minutes = 15


def test_change_password(manager, store, monkeypatch):
    Answers(monkeypatch, (SetPasswordDialog, set_password(PASSWORD)))
    manager.startup()
    store.set_secret("acme", "tacacs", "Tac-Key-99")
    Answers(
        monkeypatch,
        (SetPasswordDialog, set_password("new-password-2", current="wrong-current")),
        (SetPasswordDialog, set_password("new-password-2", current=PASSWORD)),
    )
    assert manager.change_password()
    assert store.get_secret("acme", "tacacs") == "Tac-Key-99"
    assert manager.vault.unlock("new-password-2")
    with pytest.raises(vault_module.VaultError):
        manager.vault.unlock(PASSWORD)


def test_forgotten_password_reset_deletes_stored_values(manager, store, monkeypatch):
    manager.vault.create(PASSWORD)
    store.cipher = manager.vault.unlock(PASSWORD)
    store.set_secret("acme", "tacacs", "Tac-Key-99")
    store.cipher = None
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    Answers(
        monkeypatch,
        (UnlockDialog, lambda d: d.done(UnlockDialog.RESET) or False),
        (SetPasswordDialog, set_password("fresh-password-3")),
    )
    assert manager.startup() and manager.unlocked
    assert store.list_secrets("acme") == []
    assert manager.vault.unlock("fresh-password-3")


def test_bridge_answers_403_while_locked(manager, app_paths, monkeypatch):
    from tsunami.mcp_server.bridge import BridgeClient, BridgeServer

    Answers(monkeypatch, (SetPasswordDialog, set_password(PASSWORD)))
    manager.startup()
    server = BridgeServer(app_paths, lambda tool, args: {"ok": tool}, lambda: manager.unlocked)
    server.start()
    try:
        client = BridgeClient(app_paths)
        assert client.call("list_customers", {}) == {"ok": "list_customers"}
        manager.lock()
        with pytest.raises(Exception, match="403 Tsunami is locked"):
            client.call("list_customers", {})
    finally:
        server.stop()
