"""Credentials: a compact side bar list of names (actions in right-click menus) and a full tab.

Values are write-only: a dialog collects one, the view hands it straight to the store's
set_login or set_secret, and the dialog fields are cleared. Nothing here displays,
pre-fills or logs a value.

See design.md, GUI; Secrets and credentials.
"""

from __future__ import annotations

from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QCheckBox,
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
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.inventory_edit import InventoryEditor
from tsunami.core.issues import TsunamiError
from tsunami.core.logins import customer_logins
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.operations import record_test
from tsunami.core.redact import Redactor, redact_exception
from tsunami.core.secret_store import SINGLE_FIELD, LoginCredential, SecretFields
from tsunami.core.transports import DeviceTransport
from tsunami.core.transports.base import ConnectTarget
from tsunami.core.workspace import Workspace
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import ActivityItem, Shell


def _password_edit() -> QLineEdit:
    edit = QLineEdit()
    edit.setEchoMode(QLineEdit.EchoMode.Password)
    return edit


def run_dialog(dialog: QDialog) -> bool:
    """Show a dialog modally. True if accepted. Tests replace this."""
    return dialog.exec() == QDialog.DialogCode.Accepted


CREDENTIALS_TAB_KEY = "credentials"
_KIND_ROLE = Qt.ItemDataRole.UserRole
LOGIN, SECRET = "login", "secret"


class _StoreChanges(QObject):
    """Tells every open Credentials view (side bar and tab) that the store changed."""

    changed = Signal()


_CHANGES = _StoreChanges()


PUSHED = "Pushed by plays"  # a customer's push_logins: plays create it, nothing logs in


def credential_usage(workspace: Workspace | None, name: str) -> list[tuple[str, str]]:
    """Where a login is referenced: (kind, label) with kind Customer, Site, Device, Jump host,
    or Pushed by plays (a customer's push_logins).

    Only direct references (`customer.credential`, `site_credentials`, `device.credential`,
    the jump host's login), not devices that merely inherit it."""
    if workspace is None:
        return []
    found: list[tuple[str, str]] = []
    for slug, inv in sorted(workspace.customers.items()):
        if inv.config.credential == name:
            found.append(("Customer", slug))
        if inv.config.jump_host is not None and inv.config.jump_host.credential == name:
            found.append(("Jump host", slug))
        found.extend(
            ("Jump host", f"{slug}/{site}")
            for site, jh in sorted(inv.site_jumps.items())
            if jh is not None and jh.credential == name
        )
        found.extend(
            ("Jump host", f"{slug}/{d.name}")
            for d in inv.devices
            if d.jump_host is not None and d.jump_host.credential == name
        )
        found.extend(
            ("Site", f"{slug}/{site}")
            for site, cred in sorted(inv.config.site_credentials.items())
            if cred == name
        )
        found.extend(("Device", f"{slug}/{d.name}") for d in inv.devices if d.credential == name)
        if name in inv.config.push_logins:
            found.append((PUSHED, slug))
    return found


def usage_summary(usage: list[tuple[str, str]]) -> str:
    """e.g. "1 customer, 2 devices"; "" when unused."""
    parts = []
    for kind, singular, plural in (
        ("Customer", "customer", "customers"),
        ("Jump host", "jump host", "jump hosts"),
        ("Site", "site", "sites"),
        ("Device", "device", "devices"),
        (PUSHED, "customer's plays", "customers' plays"),
    ):
        count = sum(1 for k, _ in usage if k == kind)
        if count:
            parts.append(f"{count} {singular if count == 1 else plural}")
    return ", ".join(parts)


class PushCustomers(QGroupBox):
    """A checklist of customers whose plays may push a login (their push_logins).

    A customer that logs in with the login may always push it: it is shown checked and
    cannot be changed here. choices() covers only the customers that can be changed."""

    def __init__(self, workspace: Workspace, name: str | None) -> None:
        super().__init__("Customers whose plays may push it")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        hint = QLabel(
            "Plays for these customers may create this account on devices with login(), "
            "and offer it where a play asks for a login."
        )
        hint.setObjectName("Muted")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.list = QListWidget()
        for slug, inv in sorted(workspace.customers.items()):
            item = QListWidgetItem(slug)
            item.setData(_KIND_ROLE, slug)
            item.setFlags(item.flags() | Qt.ItemFlag.ItemIsUserCheckable)
            if name is not None and name in customer_logins(inv):
                item.setText(f"{slug} (logs in with it)")
                item.setFlags(item.flags() & ~Qt.ItemFlag.ItemIsEnabled)
                item.setCheckState(Qt.CheckState.Checked)
            else:
                pushed = name is not None and name in inv.config.push_logins
                item.setCheckState(Qt.CheckState.Checked if pushed else Qt.CheckState.Unchecked)
            self.list.addItem(item)
        row = self.list.sizeHintForRow(0) if self.list.count() else 24
        rows = min(6, max(2, self.list.count()))
        self.list.setFixedHeight(rows * max(row, 24) + 2 * self.list.frameWidth() + 4)
        layout.addWidget(self.list)

    def items(self) -> dict[str, QListWidgetItem]:
        return {
            self.list.item(i).data(_KIND_ROLE): self.list.item(i) for i in range(self.list.count())
        }

    def choices(self) -> dict[str, bool]:
        """customer slug -> whether its plays may push the login, for changeable rows."""
        return {
            slug: item.checkState() == Qt.CheckState.Checked
            for slug, item in self.items().items()
            if item.flags() & Qt.ItemFlag.ItemIsEnabled
        }


class _CustomersDialog(QDialog):
    """Change which customers' plays may push an existing login."""

    def __init__(self, workspace: Workspace, name: str, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.setWindowTitle(f"Customers for {name}")
        self.setMinimumWidth(420)
        layout = QVBoxLayout(self)
        self.customers = PushCustomers(workspace, name)
        layout.addWidget(self.customers)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)


class _ValueDialog(QDialog):
    """Collects a name and write-only value fields. Never pre-filled. A new login can also
    say which customers' plays may push it (customers). A login can have no password, for
    AOS-S switches that never had one set (no_password_check)."""

    def __init__(
        self,
        title: str,
        *,
        login: bool,
        name: str | None,
        parent: QWidget | None,
        customers: PushCustomers | None = None,
    ):
        super().__init__(parent)
        self.customers = customers
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
        self.no_password_check = QCheckBox("No password (AOS-S switch that never had one set)")
        self.no_password_check.setToolTip(
            "Logs in over SSH with no authentication. Only AOS-S (ProCurve) switches with "
            "no manager or operator password accept this. The username can be anything."
        )
        self.no_password_check.toggled.connect(self._no_password_toggled)
        if login:
            form.addRow("Username", self.username_edit)
            form.addRow("Password", self.value_edit)
            form.addRow(self.no_password_check)
            self.enable_edit.setPlaceholderText("optional")
            form.addRow("Enable", self.enable_edit)
        else:
            form.addRow("Value", self.value_edit)
        if customers is not None:
            form.addRow(customers)
        note = QLabel("Values are write-only and are never shown again.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)

    def _no_password_toggled(self, checked: bool) -> None:
        if checked:
            self.value_edit.clear()
        self.value_edit.setEnabled(not checked)

    def clear_values(self) -> None:
        for edit in (self.username_edit, self.value_edit, self.enable_edit):
            edit.clear()


# Kinds offered by New secret. A kind only fills in field names; every field can be edited.
SINGLE, SNMPV3, FIELDS = "Single value", "SNMPv3 user", "Fields"
SNMPV3_FIELDS = (
    ("username", True),
    ("auth_protocol", True),
    ("auth_key", False),
    ("priv_protocol", True),
    ("priv_key", False),
)


class _FieldRow(QWidget):
    """One field of a secret: name, write-only value, and whether it is not secret."""

    def __init__(self, name: str = "", plain: bool = False) -> None:
        super().__init__()
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        self.name_edit = QLineEdit(name)
        self.name_edit.setPlaceholderText("field_name")
        self.value_edit = _password_edit()
        self.value_edit.setPlaceholderText("value")
        self.plain_box = QCheckBox("Not secret")
        self.plain_box.setToolTip(
            "Shown in previews and diffs and usable on any line, like a username or a "
            "protocol name. Leave unticked for keys and passwords."
        )
        self.plain_box.toggled.connect(self._plain_changed)
        self.plain_box.setChecked(plain)
        self.remove_button = QPushButton("Remove")
        for widget in (self.name_edit, self.value_edit):
            row.addWidget(widget, 1)
        row.addWidget(self.plain_box)
        row.addWidget(self.remove_button)

    def _plain_changed(self, plain: bool) -> None:
        mode = QLineEdit.EchoMode.Normal if plain else QLineEdit.EchoMode.Password
        self.value_edit.setEchoMode(mode)


class _SecretDialog(QDialog):
    """New or overwrite a customer's secret: a single value, or named fields (an SNMPv3
    user, a key with its id). Values are write-only and never pre-filled; overwriting a
    secret with fields lists its field names again so only the values need typing."""

    def __init__(
        self,
        title: str,
        *,
        customer: str,
        name: str | None,
        parent: QWidget | None,
        existing: SecretFields | None = None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle(title)
        self.setMinimumWidth(560)
        form = QFormLayout(self)
        customer_label = QLabel(customer)
        customer_label.setTextFormat(Qt.TextFormat.PlainText)
        form.addRow("Customer", customer_label)
        self.name_edit = QLineEdit()
        if name is not None:
            self.name_edit.setText(name)
            self.name_edit.setReadOnly(True)
        else:
            self.name_edit.setPlaceholderText("lowercase letters, digits, hyphens")
        form.addRow("Name", self.name_edit)
        self.kind_combo = QComboBox()
        self.kind_combo.addItems([SINGLE, SNMPV3, FIELDS])
        form.addRow("Kind", self.kind_combo)
        self.value_edit = _password_edit()
        self.value_label = QLabel("Value")
        form.addRow(self.value_label, self.value_edit)
        self.fields_box = QWidget()
        box = QVBoxLayout(self.fields_box)
        box.setContentsMargins(0, 0, 0, 0)
        self.rows_layout = QVBoxLayout()
        box.addLayout(self.rows_layout)
        self.add_button = QPushButton("Add field")
        self.add_button.clicked.connect(lambda _=False: self.add_row())
        box.addWidget(self.add_button, 0, Qt.AlignmentFlag.AlignLeft)
        form.addRow(self.fields_box)
        self.rows: list[_FieldRow] = []
        self.usage = QLabel()
        self.usage.setTextFormat(Qt.TextFormat.PlainText)
        self.usage.setWordWrap(True)
        self.usage.setObjectName("Muted")
        form.addRow(self.usage)
        note = QLabel("Values are write-only and are never shown again.")
        note.setWordWrap(True)
        form.addRow(note)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)
        if existing is not None and not existing.single:
            self.kind_combo.setCurrentText(FIELDS)
            for field_name in existing.values:
                self.add_row(field_name, field_name in existing.plain)
        self._kind_changed()
        self.kind_combo.currentTextChanged.connect(lambda _t: self._kind_changed(preset=True))
        self.name_edit.textChanged.connect(lambda _t: self._update_usage())

    def add_row(self, name: str = "", plain: bool = False) -> _FieldRow:
        row = _FieldRow(name, plain)
        row.remove_button.clicked.connect(lambda _=False, r=row: self.remove_row(r))
        row.name_edit.textChanged.connect(lambda _t: self._update_usage())
        self.rows.append(row)
        self.rows_layout.addWidget(row)
        return row

    def remove_row(self, row: _FieldRow) -> None:
        if row in self.rows:
            self.rows.remove(row)
            row.value_edit.clear()
            row.setParent(None)
            row.deleteLater()
        self._update_usage()

    def _kind_changed(self, preset: bool = False) -> None:
        kind = self.kind_combo.currentText()
        single = kind == SINGLE
        self.value_label.setVisible(single)
        self.value_edit.setVisible(single)
        self.fields_box.setVisible(not single)
        if preset and kind == SNMPV3:
            for row in list(self.rows):
                self.remove_row(row)
            for field_name, plain in SNMPV3_FIELDS:
                self.add_row(field_name, plain)
        elif preset and kind == FIELDS and not self.rows:
            self.add_row()
        self._update_usage()

    def _update_usage(self) -> None:
        name = self.name_edit.text().strip() or "name"
        if self.kind_combo.currentText() == SINGLE:
            self.usage.setText(
                f"Plays use it as {{{{ secret('{name}') }}}}, or pick it in a prompt of "
                "type secret. It may only go on lines where the redactor masks it."
            )
            return
        first = next((r.name_edit.text().strip() for r in self.rows if r.name_edit.text()), "")
        field = first or "field"
        self.usage.setText(
            f"Plays use a field as {{{{ secret('{name}', '{field}') }}}}, or pick this secret "
            f"in a prompt of type secret and write {{{{ prompt_name.{field} }}}}. Secret fields "
            "may only go on lines where the redactor masks them; fields marked Not secret may "
            "go anywhere."
        )

    def fields(self) -> SecretFields:
        if self.kind_combo.currentText() == SINGLE:
            return SecretFields({SINGLE_FIELD: self.value_edit.text()})
        values: dict[str, str] = {}
        plain: set[str] = set()
        for row in self.rows:
            field_name = row.name_edit.text().strip()
            if not field_name and not row.value_edit.text():
                continue  # an empty row
            if field_name in values:
                raise TsunamiError(f"field {field_name!r} is listed twice")
            values[field_name] = row.value_edit.text()
            if row.plain_box.isChecked():
                plain.add(field_name)
        return SecretFields(values, frozenset(plain))

    def clear_values(self) -> None:
        self.value_edit.clear()
        for row in self.rows:
            row.value_edit.clear()


def prompt_new_login(ctx: AppContext, parent: QWidget | None) -> str | None:
    """Ask for a new login (name, username, password, optional enable), save it to the
    store and return its name; None if cancelled or it could not be saved. Values are
    write-only: they go straight to set_login and the dialog fields are cleared."""
    dialog = _ValueDialog("New login", login=True, name=None, parent=parent)
    try:
        if not run_dialog(dialog):
            return None
        name = dialog.name_edit.text().strip()
        username, password = dialog.username_edit.text(), dialog.value_edit.text()
        no_password = dialog.no_password_check.isChecked()
        if not name or not username or not (password or no_password):
            QMessageBox.warning(parent, "New login", "A login needs a name, username and password.")
            return None
        if no_password:
            password = ""
        try:
            ctx.secrets.set_login(
                name, LoginCredential(username, password, dialog.enable_edit.text() or None)
            )
        except TsunamiError as exc:
            QMessageBox.warning(parent, "New login", str(exc))
            return None
        _CHANGES.changed.emit()
        return name
    finally:
        dialog.clear_values()


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
        failure: Exception | None = None
        cred = self.target.credential
        redactor = Redactor([v for v in (cred.password, cred.enable) if v] if cred else [])
        try:
            transport = DeviceTransport(self.ctx.paths, self.ctx.settings)
            session = transport.open(self.target)
            session.close()
            result = (True, f"Logged in to {self.target.device} and closed the session.")
        except Exception as exc:  # noqa: BLE001 - reported to the person, redacted
            failure = exc
            result = (False, redact_exception(exc, redactor))
        finally:
            try:
                if transport is not None:
                    transport.close()
            except Exception:  # noqa: BLE001, S110
                pass
        # Shown in History as an operation (Test connection).
        record_test(
            self.ctx.history,
            self.target.customer,
            self.target.device,
            self.name,
            result[0],
            result[1],
            failure,
        )
        self.signals.done.emit(self.name, *result)


def _section_label(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("section", True)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


class _CredentialsBase(QWidget):
    """Lists, store operations, dialogs and context menus shared by the side bar view and
    the full Credentials tab. Subclasses lay the widgets out."""

    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx, self.shell = ctx, shell
        self._test_signals = _TestSignals()
        self._test_signals.done.connect(self._test_done)
        self.last_test_result: tuple[str, bool, str] | None = None

        self.login_list = self._make_list(LOGIN)
        self.customer_combo = wheel.ComboBox()
        self.customer_combo.currentIndexChanged.connect(lambda _=0: self.refresh_secrets())
        self.secret_list = self._make_list(SECRET)
        self.test_label = QLabel("")
        self.test_label.setWordWrap(True)

        ctx.workspace_changed.connect(self.refresh)
        _CHANGES.changed.connect(self._store_changed)

    def _make_list(self, kind: str) -> QListWidget:
        widget = QListWidget()
        widget.setProperty("kind", kind)
        widget.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        widget.customContextMenuRequested.connect(
            lambda pos, w=widget, k=kind: self._show_menu(w, k, pos)
        )
        return widget

    # -- context menus -----------------------------------------------------------------

    def _show_menu(self, widget: QListWidget, kind: str, pos: Any) -> None:
        item = widget.itemAt(pos)
        if item is not None:
            widget.setCurrentItem(item)
        self.menu_for(item, kind).exec(widget.viewport().mapToGlobal(pos))

    def menu_for(self, item: QListWidgetItem | None, kind: str = LOGIN) -> QMenu:
        """The context menu for a login or secret item, with the same actions as the
        buttons in the tab. On empty space (item None) only New… is offered; kind says
        which list it is."""
        menu = QMenu(self)
        colors = theme.current()
        if item is not None:
            kind = item.data(_KIND_ROLE) or kind
        name = item.text() if item is not None else None
        new, overwrite, delete = (
            (self.new_login, self.overwrite_login, self.delete_login)
            if kind == LOGIN
            else (self.new_secret, self.overwrite_secret, self.delete_secret)
        )

        def add(text: str, icon_name: str, slot: Any) -> None:
            action = menu.addAction(icon(icon_name, colors.text), text)
            action.triggered.connect(lambda _=False: slot())

        add("New…", "add", new)
        if name is not None:
            add("Overwrite…", "edit", lambda: overwrite(name))
            if kind == LOGIN:
                add("Customers…", "organization", lambda: self.edit_customers(name))
            add("Delete", "trash", lambda: delete(name))
            if kind == LOGIN:
                add("Test", "debug-start", lambda: self.test_login(name))
        return menu

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

    def _store_changed(self) -> None:
        self.refresh_logins()
        self.refresh_secrets()

    def _fill(self, widget: QListWidget, kind: str, names: list[str]) -> None:
        current = widget.currentItem().text() if widget.currentItem() else None
        widget.clear()
        for name in names:
            item = QListWidgetItem(name)
            item.setData(_KIND_ROLE, kind)
            self._decorate(item, kind)
            widget.addItem(item)
            if name == current:
                widget.setCurrentItem(item)

    def _decorate(self, item: QListWidgetItem, kind: str) -> None:
        """Hook for subclasses to add an icon or tooltip."""

    def refresh_logins(self) -> None:
        try:
            names = self.ctx.secrets.list_logins()
        except TsunamiError as exc:
            self.login_list.clear()
            self.shell.notify(str(exc), "error")
            return
        self._fill(self.login_list, LOGIN, names)

    def refresh_secrets(self) -> None:
        customer = self.customer_combo.currentText()
        if not customer:
            self.secret_list.clear()
            return
        try:
            names = self.ctx.secrets.list_secrets(customer)
        except TsunamiError as exc:
            self.secret_list.clear()
            self.shell.notify(str(exc), "error")
            return
        self._fill(self.secret_list, SECRET, names)

    # -- store operations (values go only to set_login / set_secret) ----------------

    def save_login(
        self,
        name: str,
        username: str,
        password: str,
        enable: str | None,
        *,
        no_password: bool = False,
    ) -> bool:
        """An empty password is saved only with no_password, so a forgotten one is refused."""
        if not username or not (password or no_password):
            self.shell.notify("A login needs a username and a password.", "error")
            return False
        if no_password:
            password = ""
        try:
            self.ctx.secrets.set_login(name, LoginCredential(username, password, enable or None))
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        _CHANGES.changed.emit()
        self.shell.notify(f"Saved login {name}.")
        return True

    def save_secret(self, customer: str, name: str, value: str | SecretFields) -> bool:
        """Save a single value, or a secret with fields (SecretFields)."""
        if not customer:
            self.shell.notify("Choose a customer first.", "error")
            return False
        fields = value if isinstance(value, SecretFields) else SecretFields({SINGLE_FIELD: value})
        if not any(fields.values.values()):
            self.shell.notify("A secret needs a value.", "error")
            return False
        try:
            self.ctx.secrets.set_secret_fields(customer, name, fields)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        _CHANGES.changed.emit()
        self.shell.notify(f"Saved secret {customer}/{name}.")
        return True

    def save_customers(self, name: str, allow: dict[str, bool]) -> bool:
        """Write which customers' plays may push login name (their push_logins)."""
        workspace = self.ctx.workspace
        if workspace is not None:
            # A customer that logs in with it may always push it: no push_logins entry.
            allow = {
                slug: wanted
                for slug, wanted in allow.items()
                if slug in workspace.customers
                and name not in customer_logins(workspace.customers[slug])
            }
        if not allow or self.ctx.workspace_path is None:
            return True
        try:
            changed = InventoryEditor(self.ctx.workspace_path).set_login_customers(name, allow)
        except (TsunamiError, OSError) as exc:
            self.shell.notify(f"Could not save the customers for {name}: {exc}", "error")
            return False
        if changed:
            self.shell.refresh_all()
            self.shell.notify(f"Saved which customers may push {name}: {', '.join(changed)}.")
        return True

    def remove_login(self, name: str) -> bool:
        try:
            self.ctx.secrets.delete_login(name)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        _CHANGES.changed.emit()
        return True

    def remove_secret(self, customer: str, name: str) -> bool:
        try:
            self.ctx.secrets.delete_secret(customer, name)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        _CHANGES.changed.emit()
        return True

    # -- action handlers (name defaults to the selected item) ---------------------------

    def _login_dialog(self, name: str | None) -> None:
        title = "New login" if name is None else f"Overwrite login {name}"
        workspace = self.ctx.workspace
        customers = (
            PushCustomers(workspace, None)
            if name is None and workspace is not None and workspace.customers
            else None
        )
        dialog = _ValueDialog(title, login=True, name=name, parent=self, customers=customers)
        try:
            if not run_dialog(dialog):
                return
            login_name = name or dialog.name_edit.text().strip()
            saved = self.save_login(
                login_name,
                dialog.username_edit.text(),
                dialog.value_edit.text(),
                dialog.enable_edit.text() or None,
                no_password=dialog.no_password_check.isChecked(),
            )
            if saved and customers is not None:
                self.save_customers(login_name, customers.choices())
        finally:
            dialog.clear_values()

    def _secret_dialog(self, name: str | None) -> None:
        customer = self.customer_combo.currentText()
        if not customer:
            self.shell.notify("Choose a customer first: secrets are saved per customer.", "error")
            return
        existing = None
        if name is not None:
            try:  # field names and which are plain, to type only the values again
                existing = self.ctx.secrets.get_secret_fields(customer, name)
            except TsunamiError:
                existing = None
        title = f"New secret for {customer}" if name is None else f"Overwrite secret {name}"
        dialog = _SecretDialog(title, customer=customer, name=name, parent=self, existing=existing)
        existing = None
        try:
            if not run_dialog(dialog):
                return
            secret_name = name or dialog.name_edit.text().strip()
            try:
                fields = dialog.fields()
            except TsunamiError as exc:
                self.shell.notify(str(exc), "error")
                return
            self.save_secret(customer, secret_name, fields)
        finally:
            dialog.clear_values()

    @staticmethod
    def _selected(widget: QListWidget, name: str | None) -> str | None:
        if name is not None:
            return name
        item = widget.currentItem()
        return item.text() if item is not None else None

    def new_login(self) -> None:
        self._login_dialog(None)

    def overwrite_login(self, name: str | None = None) -> None:
        name = self._selected(self.login_list, name)
        if name is not None:
            self._login_dialog(name)

    def edit_customers(self, name: str | None = None) -> None:
        name = self._selected(self.login_list, name)
        workspace = self.ctx.workspace
        if name is None:
            return
        if workspace is None or not workspace.customers:
            self.shell.notify("Open a workspace with customers first.", "warning")
            return
        dialog = _CustomersDialog(workspace, name, self)
        if run_dialog(dialog):
            self.save_customers(name, dialog.customers.choices())

    def delete_login(self, name: str | None = None) -> None:
        name = self._selected(self.login_list, name)
        if name is None:
            return
        text = f"Delete login {name}?"
        usage = self.logins_used_by(name)
        used = usage_summary([u for u in usage if u[0] != PUSHED])
        if used:
            text += f"\n\nIt is still used by {used}; they will not be able to log in."
        pushers = [label for kind, label in usage if kind == PUSHED]
        if pushers:
            text += f"\n\nPlays for {', '.join(pushers)} push it; they will fail until it exists."
        if QMessageBox.question(self, "Delete login", text) == QMessageBox.StandardButton.Yes:
            self.remove_login(name)

    def new_secret(self) -> None:
        self._secret_dialog(None)

    def overwrite_secret(self, name: str | None = None) -> None:
        name = self._selected(self.secret_list, name)
        if name is not None:
            self._secret_dialog(name)

    def delete_secret(self, name: str | None = None) -> None:
        name = self._selected(self.secret_list, name)
        if name is None:
            return
        customer = self.customer_combo.currentText()
        text = f"Delete secret {customer}/{name}?"
        if QMessageBox.question(self, "Delete secret", text) == QMessageBox.StandardButton.Yes:
            self.remove_secret(customer, name)

    # -- usage and test -------------------------------------------------------------------

    def logins_used_by(self, name: str) -> list[tuple[str, str]]:
        return credential_usage(self.ctx.workspace, name)

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

    def test_login(self, name: str | None = None) -> None:
        name = self._selected(self.login_list, name)
        if name is None:
            return
        choices = self.devices_using(name)
        if not choices:
            self.shell.notify(f"No device in the workspace uses {name}.", "warning")
            return
        labels = [f"{c}/{d}" for c, d in choices]
        label, ok = QInputDialog.getItem(self, "Test login", "Device", labels, 0, False)
        if ok:
            customer, device = choices[labels.index(label)]
            self.start_test(name, customer, device)

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
        try:
            # The device is reached as a run reaches it. Going direct to a private address
            # could reach another machine and record its key as the device's.
            jump = self.ctx.jump_target(customer, device.name)
        except TsunamiError as exc:
            self.shell.notify(f"The jump host's login is needed: {exc}", "error")
            return False
        target = ConnectTarget(
            customer=customer,
            device=device.name,
            host=str(device.host),
            port=device.port,
            platform=device.platform,
            method=ConnectionMethod.SSH,
            credential=credential,
            jump=jump,
        )
        self.test_label.setText(f"Testing {name} on {customer}/{device_name}…")
        QThreadPool.globalInstance().start(_TestTask(self._test_signals, self.ctx, name, target))
        return True

    def _test_done(self, name: str, ok: bool, message: str) -> None:
        self.last_test_result = (name, ok, message)
        self.test_label.setText(f"{'Success' if ok else 'Failed'} ({name}): {message}")
        if not ok:
            self.shell.notify(f"Login test failed: {message}", "warning")


class CredentialsView(_CredentialsBase):
    """The side bar: compact lists of login and secret names. Actions are in the
    right-click menus; Manage credentials opens the full tab."""

    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__(ctx, shell)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.manage_button = QPushButton("Manage credentials")
        self.manage_button.setProperty("primary", True)
        self.manage_button.setToolTip("Buttons, and where each login is used, in a tab")
        self.manage_button.clicked.connect(lambda _=False: open_credentials(self.ctx, self.shell))
        layout.addWidget(self.manage_button)

        layout.addWidget(_section_label("Device logins"))
        layout.addWidget(self.login_list, 1)
        layout.addWidget(_section_label("Secrets (per customer)"))
        layout.addWidget(self.customer_combo)
        layout.addWidget(self.secret_list, 1)
        hint = QLabel("Right-click a name for New, Overwrite, Delete and Test.")
        hint.setWordWrap(True)
        hint.setProperty("hint", True)
        layout.addWidget(hint)
        layout.addWidget(self.test_label)
        for widget in (self.login_list, self.secret_list):
            widget.setFrameShape(QListWidget.Shape.NoFrame)
        self.refresh()

    def _decorate(self, item: QListWidgetItem, kind: str) -> None:
        item.setIcon(icon("key" if kind == LOGIN else "lock", theme.current().muted))


class CredentialsTab(_CredentialsBase):
    """The full Credentials tab: button rows that fit, and where each login is used."""

    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__(ctx, shell)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(12)

        logins = QGroupBox("Device logins")
        box = QVBoxLayout(logins)
        box.addWidget(self.login_list, 2)
        box.addLayout(self._buttons(LOGIN))
        self.used_by_label = _section_label("Used by")
        box.addWidget(self.used_by_label)
        self.used_by_tree = QTreeWidget()
        self.used_by_tree.setHeaderHidden(True)
        self.used_by_tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.used_by_tree.setSelectionMode(QTreeWidget.SelectionMode.NoSelection)
        box.addWidget(self.used_by_tree, 2)
        box.addWidget(self.test_label)
        layout.addWidget(logins, 1)

        secrets = QGroupBox("Secrets (per customer)")
        box = QVBoxLayout(secrets)
        hint = QLabel(
            "Values plays push that are not logins: SNMP communities and SNMPv3 users, "
            "TACACS+ and RADIUS keys, pre-shared keys, NTP keys. A secret is one value, or "
            "named fields. Plays read them with secret('name') or a prompt of type secret."
        )
        hint.setWordWrap(True)
        hint.setObjectName("Muted")
        box.addWidget(hint)
        box.addWidget(QLabel("Customer"))
        box.addWidget(self.customer_combo)
        box.addWidget(self.secret_list, 1)
        box.addLayout(self._buttons(SECRET))
        layout.addWidget(secrets, 1)

        self.login_list.currentItemChanged.connect(lambda *_: self._show_usage())
        self.refresh()

    def _buttons(self, kind: str) -> QHBoxLayout:
        specs = (
            (("New…", self.new_login), ("Overwrite…", self.overwrite_login))
            + (("Customers…", self.edit_customers),)
            + (("Delete", self.delete_login), ("Test", self.test_login))
            if kind == LOGIN
            else (
                ("New…", self.new_secret),
                ("Overwrite…", self.overwrite_secret),
                ("Delete", self.delete_secret),
            )
        )
        row = QHBoxLayout()
        for text, slot in specs:
            button = QPushButton(text)
            button.clicked.connect(lambda _=False, s=slot: s())
            row.addWidget(button)
        return row

    def _decorate(self, item: QListWidgetItem, kind: str) -> None:
        item.setIcon(icon("key" if kind == LOGIN else "lock", theme.current().muted))
        if kind == LOGIN:
            summary = usage_summary(self.logins_used_by(item.text()))
            item.setToolTip(f"Used by {summary}" if summary else "Not used by anything")

    def refresh_logins(self) -> None:
        super().refresh_logins()
        self._show_usage()

    def _show_usage(self) -> None:
        """List the customers, jump hosts, sites and devices that reference the selected
        login, and the customers whose plays push it, so a delete's effect is visible first."""
        tree = self.used_by_tree
        tree.clear()
        item = self.login_list.currentItem()
        if item is None:
            self.used_by_label.setText("Used by")
            return
        name = item.text()
        usage = self.logins_used_by(name)
        self.used_by_label.setText(f"Used by {name}")
        if not usage:
            tree.addTopLevelItem(QTreeWidgetItem(["Nothing in the workspace uses this login."]))
            return
        for kind, plural in (
            ("Customer", "Customers"),
            ("Jump host", "Jump hosts"),
            ("Site", "Sites"),
            ("Device", "Devices"),
            (PUSHED, PUSHED),
        ):
            labels = [label for k, label in usage if k == kind]
            if not labels:
                continue
            group = QTreeWidgetItem([f"{plural} ({len(labels)})"])
            for label in labels:
                group.addChild(QTreeWidgetItem([label]))
            tree.addTopLevelItem(group)
            group.setExpanded(True)


def open_credentials(ctx: AppContext, shell: Shell) -> None:
    """Open (or focus) the one Credentials tab."""
    shell.open_tab(CREDENTIALS_TAB_KEY, "Credentials", lambda: CredentialsTab(ctx, shell), "key")


def register(shell: Shell) -> None:
    shell.register_command(
        "credentials.open",
        "Credentials: Manage credentials",
        lambda: open_credentials(shell.ctx, shell),
    )


def _factory(ctx: AppContext, shell: Any) -> QWidget:
    return CredentialsView(ctx, shell)


VIEW = ActivityItem("credentials", "Credentials", "key", _factory, order=70)
