"""Credentials screen: names only; create, overwrite, delete, test. Values never shown.

Values are write-only: a dialog collects one, the view hands it straight to the store's
set_login or set_secret, and the dialog fields are cleared. Nothing here displays,
pre-fills or logs a value.

See design.md, GUI; Secrets and credentials.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtWidgets import (
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QListWidget,
    QMessageBox,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.issues import TsunamiError
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.secret_store import LoginCredential
from tsunami.core.transports import DeviceTransport
from tsunami.core.transports.base import ConnectTarget
from tsunami.gui.context import AppContext
from tsunami.gui.shell import ActivityItem, Shell


def _password_edit() -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    return edit


class _ValueDialog(QDialog):
    """Collects a name and write-only value fields. Never pre-filled."""

    def __init__(self, title: str, *, login: bool, name: str | None, parent: QWidget | None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.login = login
        form = QFormLayout(self)
        self.name_edit = QLineEdit()
        if name is not None:
            self.name_edit.setText(name)
            self.name_edit.setReadOnly(True)
        else:
            self.name_edit.setPlaceholderText("lowercase letters, digits, hyphens")
        form.addRow("Name", self.name_edit)
        self.username_edit = QLineEdit()
        self.value_edit = _password_edit()
        self.enable_edit = _password_edit()
        if login:
            form.addRow("Username", self.username_edit)
            form.addRow("Password", self.value_edit)
            self.enable_edit.setPlaceholderText("optional")
            form.addRow("Enable", self.enable_edit)
        else:
            form.addRow("Value", self.value_edit)
        note = QLabel("Values are write-only and are never shown again.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def clear_values(self) -> None:
        for edit in (self.username_edit, self.value_edit, self.enable_edit):
            edit.clear()


class _TestSignals(QObject):
    done = Signal(str, bool, str)  # credential name, ok, message


class _TestTask(QRunnable):
    def __init__(
        self, signals: _TestSignals, ctx: AppContext, name: str, target: ConnectTarget
    ) -> None:
        super().__init__()
        self.signals, self.ctx, self.name, self.target = signals, ctx, name, target

    def run(self) -> None:
        transport = None
        try:
            transport = DeviceTransport(self.ctx.paths, self.ctx.settings)
            session = transport.open(self.target)
            session.close()
            result = (True, f"Logged in to {self.target.device} and closed the session.")
        except Exception as exc:  # noqa: BLE001 - reported to the person; messages are redacted
            result = (False, f"{type(exc).__name__}: {exc}")
        finally:
            try:
                if transport is not None:
                    transport.close()
            except Exception:  # noqa: BLE001, S110
                pass
        self.signals.done.emit(self.name, *result)


class CredentialsView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx, self.shell = ctx, shell
        self._test_signals = _TestSignals()
        self._test_signals.done.connect(self._test_done)
        self.last_test_result: tuple[str, bool, str] | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)

        logins = QGroupBox("Device logins")
        box = QVBoxLayout(logins)
        self.login_list = QListWidget()
        box.addWidget(self.login_list)
        row = QHBoxLayout()
        for text, slot in (
            ("New…", self.new_login),
            ("Overwrite…", self.overwrite_login),
            ("Delete", self.delete_login),
            ("Test", self.test_login),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(button)
        box.addLayout(row)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)
        box.addWidget(self.test_label)
        layout.addWidget(logins, 1)

        secrets = QGroupBox("Template secrets")
        box = QVBoxLayout(secrets)
        self.customer_combo = QComboBox()
        self.customer_combo.currentIndexChanged.connect(lambda _=0: self.refresh_secrets())
        box.addWidget(self.customer_combo)
        self.secret_list = QListWidget()
        box.addWidget(self.secret_list)
        row = QHBoxLayout()
        for text, slot in (
            ("New…", self.new_secret),
            ("Overwrite…", self.overwrite_secret),
            ("Delete", self.delete_secret),
        ):
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(button)
        box.addLayout(row)
        layout.addWidget(secrets, 1)

        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    # -- refresh -----------------------------------------------------------------

    def refresh(self) -> None:
        self.refresh_logins()
        current = self.customer_combo.currentText()
        self.customer_combo.blockSignals(True)
        self.customer_combo.clear()
        if self.ctx.workspace is not None:
            self.customer_combo.addItems(sorted(self.ctx.workspace.customers))
        index = self.customer_combo.findText(current)
        self.customer_combo.setCurrentIndex(max(index, 0) if self.customer_combo.count() else -1)
        self.customer_combo.blockSignals(False)
        self.refresh_secrets()

    def refresh_logins(self) -> None:
        self.login_list.clear()
        try:
            self.login_list.addItems(self.ctx.secrets.list_logins())
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")

    def refresh_secrets(self) -> None:
        self.secret_list.clear()
        customer = self.customer_combo.currentText()
        if not customer:
            return
        try:
            self.secret_list.addItems(self.ctx.secrets.list_secrets(customer))
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")

    # -- store operations (values go only to set_login / set_secret) ----------------

    def save_login(self, name: str, username: str, password: str, enable: str | None) -> bool:
        if not username or not password:
            self.shell.notify("A login needs a username and a password.", "error")
            return False
        try:
            self.ctx.secrets.set_login(name, LoginCredential(username, password, enable or None))
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        self.refresh_logins()
        self.shell.notify(f"Saved login {name}.")
        return True

    def save_secret(self, customer: str, name: str, value: str) -> bool:
        if not customer:
            self.shell.notify("Choose a customer first.", "error")
            return False
        if not value:
            self.shell.notify("A secret needs a value.", "error")
            return False
        try:
            self.ctx.secrets.set_secret(customer, name, value)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        self.refresh_secrets()
        self.shell.notify(f"Saved secret {customer}/{name}.")
        return True

    def remove_login(self, name: str) -> bool:
        try:
            self.ctx.secrets.delete_login(name)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        self.refresh_logins()
        return True

    def remove_secret(self, customer: str, name: str) -> bool:
        try:
            self.ctx.secrets.delete_secret(customer, name)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        self.refresh_secrets()
        return True

    # -- button handlers --------------------------------------------------------------

    def _login_dialog(self, name: str | None) -> None:
        title = "New login" if name is None else f"Overwrite login {name}"
        dialog = _ValueDialog(title, login=True, name=name, parent=self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            self.save_login(
                name or dialog.name_edit.text().strip(),
                dialog.username_edit.text(),
                dialog.value_edit.text(),
                dialog.enable_edit.text() or None,
            )
        finally:
            dialog.clear_values()

    def _secret_dialog(self, name: str | None) -> None:
        customer = self.customer_combo.currentText()
        title = "New secret" if name is None else f"Overwrite secret {name}"
        dialog = _ValueDialog(title, login=False, name=name, parent=self)
        try:
            if dialog.exec() != QDialog.DialogCode.Accepted:
                return
            secret_name = name or dialog.name_edit.text().strip()
            self.save_secret(customer, secret_name, dialog.value_edit.text())
        finally:
            dialog.clear_values()

    def new_login(self) -> None:
        self._login_dialog(None)

    def overwrite_login(self) -> None:
        item = self.login_list.currentItem()
        if item is not None:
            self._login_dialog(item.text())

    def delete_login(self) -> None:
        item = self.login_list.currentItem()
        if item is None:
            return
        answer = QMessageBox.question(self, "Delete login", f"Delete login {item.text()}?")
        if answer == QMessageBox.StandardButton.Yes:
            self.remove_login(item.text())

    def new_secret(self) -> None:
        self._secret_dialog(None)

    def overwrite_secret(self) -> None:
        item = self.secret_list.currentItem()
        if item is not None:
            self._secret_dialog(item.text())

    def delete_secret(self) -> None:
        item = self.secret_list.currentItem()
        if item is None:
            return
        customer = self.customer_combo.currentText()
        text = f"Delete secret {customer}/{item.text()}?"
        if QMessageBox.question(self, "Delete secret", text) == QMessageBox.StandardButton.Yes:
            self.remove_secret(customer, item.text())

    # -- test -------------------------------------------------------------------------

    def devices_using(self, credential: str) -> list[tuple[str, str]]:
        workspace = self.ctx.workspace
        if workspace is None:
            return []
        return [
            (slug, device.name)
            for slug, inv in sorted(workspace.customers.items())
            for device in inv.devices
            if inv.credential_for(device) == credential  # own, site default, or customer's
        ]

    def test_login(self) -> None:
        item = self.login_list.currentItem()
        if item is None:
            return
        choices = self.devices_using(item.text())
        if not choices:
            self.shell.notify(f"No device in the workspace uses {item.text()}.", "warning")
            return
        labels = [f"{c}/{d}" for c, d in choices]
        label, ok = QInputDialog.getItem(self, "Test login", "Device", labels, 0, False)
        if ok:
            customer, device = choices[labels.index(label)]
            self.start_test(item.text(), customer, device)

    def start_test(self, name: str, customer: str, device_name: str) -> bool:
        workspace = self.ctx.workspace
        inv = workspace.customers.get(customer) if workspace else None
        device = inv.device(device_name) if inv else None
        if device is None:
            self.shell.notify(f"Unknown device {customer}/{device_name}.", "error")
            return False
        try:
            credential = self.ctx.secrets.get_login(name)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        target = ConnectTarget(
            customer=customer,
            device=device.name,
            host=str(device.host),
            port=device.port,
            platform=device.platform,
            method=ConnectionMethod.SSH,
            credential=credential,
        )
        self.test_label.setText(f"Testing {name} on {customer}/{device_name}…")
        QThreadPool.globalInstance().start(_TestTask(self._test_signals, self.ctx, name, target))
        return True

    def _test_done(self, name: str, ok: bool, message: str) -> None:
        self.last_test_result = (name, ok, message)
        self.test_label.setText(f"{'Success' if ok else 'Failed'} ({name}): {message}")
        if not ok:
            self.shell.notify(f"Login test failed: {message}", "warning")


def _factory(ctx: AppContext, shell: Any) -> QWidget:
    return CredentialsView(ctx, shell)


VIEW = ActivityItem("credentials", "Credentials", "key", _factory, order=70)
