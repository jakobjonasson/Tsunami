"""The Tsunami password in the GUI: first-launch setup, unlock, auto-lock, change, reset.

While locked, stored logins and secrets cannot be read or written (the secret store's
cipher refuses), and the MCP bridge answers every call with 403. Jobs already running
keep the logins they loaded. A forgotten password cannot be recovered: the reset deletes
every stored login and secret, then asks for a new password.

See design.md, Secrets and credentials: Tsunami password.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from PySide6.QtCore import QEvent, QObject, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.issues import TsunamiError
from tsunami.core.vault import LockedCipher, Vault, VaultError, check_password

log = logging.getLogger(__name__)
CHECK_INTERVAL_MS = 30_000
_INPUT_EVENTS = frozenset(
    {
        QEvent.Type.KeyPress,
        QEvent.Type.MouseButtonPress,
        QEvent.Type.MouseMove,
        QEvent.Type.Wheel,
    }
)


def run_dialog(dialog: QDialog) -> bool:
    """Show a dialog modally. Tests replace this to fill and accept dialogs."""
    return dialog.exec() == QDialog.DialogCode.Accepted


def _primary_button(text: str) -> QPushButton:
    """The accent button; the property is set before the style sheet first applies."""
    button = QPushButton(text)
    button.setProperty("primary", True)
    return button


def _password_field(placeholder: str = "") -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    edit.setPlaceholderText(placeholder)
    edit.setMinimumWidth(280)
    return edit


class _Dialog(QDialog):
    def __init__(self, title: str, intro: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setModal(True)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(20, 18, 20, 16)
        layout.setSpacing(12)
        heading = QLabel(intro)
        heading.setWordWrap(True)
        layout.addWidget(heading)
        self.form = QFormLayout()
        self.form.setSpacing(8)
        layout.addLayout(self.form)
        self.error = QLabel()
        self.error.setObjectName("FormError")
        self.error.setWordWrap(True)
        self.error.hide()
        layout.addWidget(self.error)
        self.buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Cancel)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def fail(self, message: str) -> None:
        self.error.setText(message[:1].upper() + message[1:])
        self.error.show()


class SetPasswordDialog(_Dialog):
    """First launch, change, or after a reset. With current=True, asks for the old one."""

    def __init__(self, parent: QWidget | None = None, *, current: bool = False) -> None:
        intro = (
            "Choose a new Tsunami password."
            if current
            else "Set a Tsunami password. It encrypts your stored logins and secrets, and "
            "Tsunami asks for it each time it starts."
        )
        super().__init__("Tsunami password", intro, parent)
        self.current_edit = _password_field()
        if current:
            self.form.addRow("Current password", self.current_edit)
        self.password_edit = _password_field("at least 8 characters")
        self.confirm_edit = _password_field()
        self.form.addRow("New password", self.password_edit)
        self.form.addRow("Confirm", self.confirm_edit)
        note = QLabel(
            "There is no recovery. If you forget it, resetting deletes your stored logins "
            "and secrets, and you enter them again."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        self.layout().insertWidget(2, note)
        ok = _primary_button("Set password")
        self.buttons.addButton(ok, QDialogButtonBox.ButtonRole.AcceptRole)
        ok.clicked.connect(self.save)

    def save(self) -> bool:
        password = self.password_edit.text()
        if password != self.confirm_edit.text():
            self.fail("The passwords do not match.")
            return False
        try:
            check_password(password)
        except VaultError as exc:
            self.fail(str(exc))
            return False
        self.accept()
        return True


class UnlockDialog(_Dialog):
    RESET = 2  # result code: the person chose to reset a forgotten password

    def __init__(self, vault: Vault, parent: QWidget | None = None) -> None:
        super().__init__("Unlock Tsunami", "Enter the Tsunami password.", parent)
        self.vault = vault
        self.cipher: Any = None
        self.password_edit = _password_field()
        self.form.addRow("Password", self.password_edit)
        forgot = QPushButton("Forgot password…", objectName="Link")
        forgot.clicked.connect(lambda: self.done(self.RESET))
        self.buttons.addButton(forgot, QDialogButtonBox.ButtonRole.ResetRole)
        ok = _primary_button("Unlock")
        self.buttons.addButton(ok, QDialogButtonBox.ButtonRole.AcceptRole)
        ok.setDefault(True)
        ok.clicked.connect(self.save)

    def save(self) -> bool:
        try:
            self.cipher = self.vault.unlock(self.password_edit.text())
        except VaultError as exc:
            self.password_edit.clear()
            self.fail(str(exc))
            return False
        finally:
            self.password_edit.clear()
        self.accept()
        return True


class LockManager(QObject):
    """Owns the lock state: the secret store's cipher, and the idle timer."""

    lock_changed = Signal(bool)  # True when unlocked

    def __init__(self, paths: Any, secrets: Any, settings: Any) -> None:
        super().__init__()
        self.vault = Vault(paths.vault_file)
        self.secrets = secrets
        self.settings = settings
        self._unlocked = False
        self._last_input = time.monotonic()
        self._timer = QTimer(self)
        self._timer.setInterval(CHECK_INTERVAL_MS)
        self._timer.timeout.connect(self.check_idle)
        self._prompting = False

    @property
    def unlocked(self) -> bool:
        return self._unlocked

    # -- startup ------------------------------------------------------------------------

    def startup(self, parent: QWidget | None = None) -> bool:
        """Set the password on first launch, else unlock. False if the person cancels."""
        app = QApplication.instance()
        if app is not None:
            app.installEventFilter(self)
        self._timer.start()
        if not self.vault.exists():
            return self._set_first_password(parent)
        return self.unlock_interactive(parent)

    def _set_first_password(self, parent: QWidget | None) -> bool:
        dialog = SetPasswordDialog(parent)
        if not run_dialog(dialog):
            return False
        cipher, save = self.vault.prepare(dialog.password_edit.text())
        _clear(dialog)
        count = self.secrets.rekey(cipher)  # encrypts logins saved before the password
        save()
        log.info("Tsunami password set; %d stored entries encrypted", count)
        self._set_unlocked(True)
        return True

    # -- lock and unlock ------------------------------------------------------------------

    def _encrypt_plain(self) -> None:
        encrypt = getattr(self.secrets, "encrypt_plain", None)
        if encrypt is None:
            return
        try:
            count = encrypt()
        except TsunamiError as exc:
            log.warning("could not encrypt plain stored values: %s", exc)
            return
        if count:
            log.info("encrypted %d stored values that were in plain text", count)

    def lock(self) -> None:
        if not self._unlocked:
            return
        self.secrets.cipher = LockedCipher()
        self._set_unlocked(False)
        log.info("Tsunami locked")

    def unlock_interactive(self, parent: QWidget | None = None) -> bool:
        if self._prompting:
            return False
        self._prompting = True
        try:
            while True:
                dialog = UnlockDialog(self.vault, parent)
                ok = run_dialog(dialog)
                if dialog.result() == UnlockDialog.RESET:
                    if self.reset(parent):
                        return True
                    continue
                if not ok or dialog.cipher is None:
                    return False
                self.secrets.cipher = dialog.cipher
                self._encrypt_plain()
                self._set_unlocked(True)
                return True
        finally:
            self._prompting = False

    def lock_now(self, parent: QWidget | None = None) -> None:
        """The lock command: lock, then ask for the password again."""
        self.lock()
        self.unlock_interactive(parent)

    # -- change and reset -----------------------------------------------------------------

    def change_password(self, parent: QWidget | None = None) -> bool:
        dialog = SetPasswordDialog(parent, current=True)
        while run_dialog(dialog):
            try:
                self.vault.unlock(dialog.current_edit.text())
            except VaultError:
                dialog.fail("The current password is wrong.")
                continue
            cipher, save = self.vault.prepare(dialog.password_edit.text())
            _clear(dialog)
            try:
                self.secrets.rekey(cipher)
            except TsunamiError as exc:
                QMessageBox.warning(parent, "Tsunami password", f"Nothing changed: {exc}")
                return False
            save()
            log.info("Tsunami password changed")
            return True
        return False

    def reset(self, parent: QWidget | None = None) -> bool:
        answer = QMessageBox.question(
            parent,
            "Reset the Tsunami password",
            "Resetting deletes every login and secret Tsunami has stored. You enter them "
            "again in Credentials. Workspaces, plays and job history are kept.\n\n"
            "Reset and choose a new password?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        self.secrets.cipher = None
        count = self.secrets.delete_everything()
        self.vault.remove()
        log.warning("Tsunami password reset; %d stored entries deleted", count)
        return self._set_first_password(parent)

    # -- idle -----------------------------------------------------------------------------

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 - Qt API
        if event.type() in _INPUT_EVENTS:
            self._last_input = time.monotonic()
        return False

    def idle_seconds(self) -> float:
        return time.monotonic() - self._last_input

    def check_idle(self) -> None:
        minutes = getattr(self.settings, "auto_lock_minutes", 15)
        if self._unlocked and minutes and self.idle_seconds() >= minutes * 60:
            log.info("auto-lock after %d idle minutes", minutes)
            parent = QApplication.activeWindow()
            self.lock()
            self.unlock_interactive(parent)

    def _set_unlocked(self, unlocked: bool) -> None:
        self._unlocked = unlocked
        self._last_input = time.monotonic()
        self.lock_changed.emit(unlocked)


def _clear(dialog: SetPasswordDialog) -> None:
    for edit in (dialog.current_edit, dialog.password_edit, dialog.confirm_edit):
        edit.clear()
