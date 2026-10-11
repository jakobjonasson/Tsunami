"""Dialogs and the site tab for editing inventory from the Inventory view.

Each dialog has save() -> bool, which validates and writes through InventoryEditor, shows
an editor refusal inline and keeps the dialog open, and on success refreshes the
workspace and notifies. OK just calls save(). Menus open dialogs through run_dialog(), the
one place a modal event loop starts, so tests can replace it.

Login names come from Credential Manager; values are never shown (new logins go through
prompt_new_login, a write-only dialog).

See design.md, Inventory and variables; GUI: Views.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import ValidationError
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QSpinBox,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core import inventory_edit
from tsunami.core.catalog import load_catalog
from tsunami.core.inventory_edit import InventoryEditor
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform
from tsunami.core.models.inventory import DEVICE_TYPES, Device, JumpHost
from tsunami.core.yamlio import load_yaml_text
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.screens.credentials import prompt_new_login
from tsunami.gui.shell import Shell

_NONE = "(none)"


def type_label(role: str) -> str:
    """The label for a device type; a custom role shows its slug."""
    return DEVICE_TYPES.get(role, role)


def run_dialog(dialog: QDialog) -> bool:
    """Show a dialog modally. True if it was accepted. Tests replace this."""
    return dialog.exec() == QDialog.DialogCode.Accepted


def _recycle(path: Path) -> None:
    # Looked up at call time, so tests can replace the Recycle Bin call.
    inventory_edit.send_to_recycle_bin(path)


def make_editor(ctx: AppContext) -> InventoryEditor:
    if ctx.workspace_path is None:
        raise inventory_edit.InventoryEditError("no workspace is open")
    return InventoryEditor(ctx.workspace_path, recycle=_recycle)


def site_file(ctx: AppContext, customer: str, site: str) -> Path | None:
    if ctx.workspace_path is None:
        return None
    return ctx.workspace_path / "inventory" / "customers" / customer / "sites" / f"{site}.yml"


def read_site_text(ctx: AppContext, customer: str, site: str) -> str:
    path = site_file(ctx, customer, site)
    try:
        return path.read_text(encoding="utf-8") if path is not None else ""
    except OSError:
        return ""


def parse_vars_text(text: str) -> dict[str, Any]:
    """YAML text to a mapping; blank is an empty mapping. Raises ValueError with a
    message fit to show."""
    if not text.strip():
        return {}
    try:
        data = load_yaml_text(text, Path("variables")).data
    except TsunamiError as exc:
        raise ValueError(f"Variables are not valid YAML: {exc}") from None
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError("Variables must be a mapping of names to values.")
    return data


def vars_to_text(values: dict[str, Any]) -> str:
    if not values:
        return ""
    import io

    from ruamel.yaml import YAML

    out = io.StringIO()
    yaml = YAML(typ="safe")
    yaml.default_flow_style = False
    yaml.dump(values, out)
    return out.getvalue()


def _plain_edit(text: str = "", height: int = 110) -> QPlainTextEdit:
    edit = QPlainTextEdit()
    edit.setProperty("editable", True)
    edit.setFont(theme.mono_font())
    edit.setPlainText(text)
    edit.setMinimumHeight(height)
    edit.setTabChangesFocus(True)
    return edit


class LoginPicker(QWidget):
    """A combo of Credential Manager login names and a New login button.

    With default_label, the first item means "no login set here" and login() is None.
    """

    def __init__(
        self,
        ctx: AppContext,
        *,
        default_label: str | None = None,
        current: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.default_label = default_label
        self.load_error: str | None = None
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(
            QComboBox.SizeAdjustPolicy.AdjustToMinimumContentsLengthWithIcon
        )
        self.combo.setMinimumContentsLength(18)
        self.new_button = QPushButton("New login…")
        self.new_button.clicked.connect(lambda _=False: self.new_login())
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(8)
        row.addWidget(self.combo, 1)
        row.addWidget(self.new_button)
        self.reload(current)

    def reload(self, select: str | None) -> None:
        try:
            names = list(self.ctx.secrets.list_logins())
            self.load_error = None
        except TsunamiError as exc:
            names, self.load_error = [], str(exc)
        if select and select not in names:
            names.append(select)  # keep a name the file uses even if the store lacks it
        self.combo.clear()
        if self.default_label is not None:
            self.combo.addItem(self.default_label, None)
        for name in names:
            self.combo.addItem(name, name)
        self.combo.setCurrentIndex(max(self.combo.findData(select), 0) if select else 0)

    def set_default_label(self, text: str) -> None:
        if self.default_label is not None:
            self.default_label = text
            self.combo.setItemText(0, text)

    def login(self) -> str | None:
        data = self.combo.currentData()
        return data if isinstance(data, str) and data else None

    def new_login(self) -> str | None:
        name = prompt_new_login(self.ctx, self)
        if name:
            self.reload(name)
        return name


class _EditDialog(QDialog):
    """A form with an inline error line and OK / Cancel."""

    def __init__(self, title: str, ctx: AppContext, shell: Shell, parent: QWidget | None) -> None:
        super().__init__(parent)
        self.ctx, self.shell = ctx, shell
        self.setWindowTitle(title)
        self.setMinimumWidth(480)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(20, 18, 20, 16)
        outer.setSpacing(14)
        self.form = QFormLayout()
        self.form.setHorizontalSpacing(14)
        self.form.setVerticalSpacing(10)
        self.form.setFieldGrowthPolicy(QFormLayout.FieldGrowthPolicy.AllNonFixedFieldsGrow)
        outer.addLayout(self.form)
        self.error_label = QLabel()
        self.error_label.setObjectName("FormError")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme.current().error};")
        self.error_label.setVisible(False)
        outer.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setProperty("primary", True)
        ok.setDefault(True)
        self.buttons.accepted.connect(self._ok)
        self.buttons.rejected.connect(self.reject)
        outer.addWidget(self.buttons)

    def set_error(self, message: str) -> None:
        self.error_label.setText(message)
        self.error_label.setVisible(bool(message))

    def _ok(self) -> None:
        if self.save():
            self.accept()

    def _run(self, action: Any, message: str) -> bool:
        """Run an editor call. On refusal show it inline and return False; else refresh."""
        try:
            action()
        except (TsunamiError, OSError) as exc:
            self.set_error(str(exc))
            return False
        self.shell.refresh_all()
        self.shell.notify(message)
        return True

    def save(self) -> bool:  # pragma: no cover - overridden
        raise NotImplementedError


class DeviceDialog(_EditDialog):
    """Add a device (device is None) or edit one."""

    def __init__(
        self,
        ctx: AppContext,
        shell: Shell,
        customer: str,
        *,
        device: Device | None = None,
        site: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        title = f"New device in {customer}" if device is None else f"Edit {device.name}"
        super().__init__(title, ctx, shell, parent)
        self.customer = customer
        self.device = device
        ws = ctx.workspace
        inv = ws.customers[customer] if ws is not None and customer in ws.customers else None
        self.inventory = inv
        self.catalog = load_catalog()

        self.name_edit = QLineEdit(device.name if device else "")
        self.name_edit.setPlaceholderText("e.g. sa-as01")
        self.host_edit = QLineEdit(str(device.host) if device else "")
        self.host_edit.setPlaceholderText("IP address or DNS name")
        self.port_spin = QSpinBox()
        self.port_spin.setRange(1, 65535)
        self.port_spin.setFixedWidth(96)
        self.port_spin.setValue(device.port if device else 22)
        self.site_combo = QComboBox()
        for name in sorted(inv.sites) if inv else []:
            self.site_combo.addItem(name, name)
        wanted_site = device.site if device else site
        if wanted_site and self.site_combo.findData(wanted_site) >= 0:
            self.site_combo.setCurrentIndex(self.site_combo.findData(wanted_site))
        self.type_combo = QComboBox()
        for slug, label in DEVICE_TYPES.items():
            self.type_combo.addItem(label, slug)
        for slug in sorted(ws.roles if ws is not None else ()):
            if slug not in DEVICE_TYPES:
                self.type_combo.addItem(slug, slug)
        wanted_type = device.role if device else "access"
        if self.type_combo.findData(wanted_type) < 0:
            self.type_combo.addItem(wanted_type, wanted_type)
        self.type_combo.setCurrentIndex(self.type_combo.findData(wanted_type))
        self.platform_combo = QComboBox()
        for platform in Platform:
            self.platform_combo.addItem(str(platform), str(platform))
        if device:
            self.platform_combo.setCurrentIndex(self.platform_combo.findData(str(device.platform)))
        self.model_combo = QComboBox()
        self._fill_models(device.model if device else None)
        self.platform_combo.currentIndexChanged.connect(lambda _=0: self._fill_models(None))
        self.login = LoginPicker(
            ctx, default_label=self._default_text(), current=device.credential if device else None
        )
        self.site_combo.currentIndexChanged.connect(lambda _=0: self._update_default())
        self.original_vars = dict(device.vars) if device else {}
        self.vars_edit = _plain_edit(vars_to_text(self.original_vars))
        self.vars_edit.setPlaceholderText("Optional YAML, e.g.\nmgmt_ip: 192.0.2.10/24")

        self.form.addRow("Name", self.name_edit)
        self.form.addRow("Host", self.host_edit)
        self.form.addRow("Port", self.port_spin)
        self.form.addRow("Site", self.site_combo)
        self.form.addRow("Type", self.type_combo)
        self.form.addRow("Platform", self.platform_combo)
        self.form.addRow("Model", self.model_combo)
        self.form.addRow("Login", self.login)
        self.form.addRow("Variables", self.vars_edit)
        if self.login.load_error:
            self.set_error(self.login.load_error)

    def _default_text(self) -> str:
        if self.inventory is None:
            return "Default"
        site = self.site_combo.currentData()
        name = self.inventory.config.site_credentials.get(site) or self.inventory.config.credential
        return f"Default ({name})"

    def _update_default(self) -> None:
        self.login.set_default_label(self._default_text())

    def _fill_models(self, select: str | None) -> None:
        platform = self.platform_combo.currentData()
        keep = select if select is not None else self.model_combo.currentData()
        self.model_combo.clear()
        self.model_combo.addItem(_NONE, None)
        entries = sorted(
            (self.catalog.get(i) for i in self.catalog.ids()), key=lambda e: e.label.lower()
        )
        for entry in entries:
            if str(entry.platform) == platform:
                self.model_combo.addItem(entry.label, entry.id)
        if select and self.model_combo.findData(select) < 0:
            self.model_combo.addItem(select, select)  # not in the catalog; the editor says so
        index = self.model_combo.findData(keep) if keep else 0
        self.model_combo.setCurrentIndex(max(index, 0))

    def fields(self) -> dict[str, Any]:
        """The device as the form describes it. Raises ValueError for bad variables."""
        values = parse_vars_text(self.vars_edit.toPlainText())
        return {
            "name": self.name_edit.text().strip(),
            "host": self.host_edit.text().strip(),
            "port": self.port_spin.value(),
            "site": self.site_combo.currentData() or "",
            "role": self.type_combo.currentData(),
            "platform": self.platform_combo.currentData(),
            "model": self.model_combo.currentData(),
            "credential": self.login.login(),
            "vars": values or None,
        }

    def save(self) -> bool:
        self.set_error("")
        try:
            fields = self.fields()
        except ValueError as exc:
            self.set_error(str(exc))
            return False
        if not fields["site"]:
            self.set_error("This customer has no sites yet. Add a site first.")
            return False
        try:
            editor = make_editor(self.ctx)
        except TsunamiError as exc:
            self.set_error(str(exc))
            return False
        if self.device is None:
            return self._run(
                lambda: editor.add_device(self.customer, fields),
                f"Added device {fields['name']}.",
            )
        old = self.device
        before = {
            "name": old.name,
            "host": str(old.host),
            "port": old.port,
            "site": old.site,
            "role": old.role,
            "platform": str(old.platform),
            "model": old.model,
            "credential": old.credential,
            "vars": dict(old.vars) or None,
        }
        changed = {k: v for k, v in fields.items() if before[k] != v}
        if not changed:
            return True
        return self._run(
            lambda: editor.update_device(self.customer, old.name, changed),
            f"Saved device {fields['name']}.",
        )


class SiteDialog(_EditDialog):
    """Add a site (site is None) or edit one: name, default login, variables."""

    def __init__(
        self,
        ctx: AppContext,
        shell: Shell,
        customer: str,
        *,
        site: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        title = f"New site in {customer}" if site is None else f"Edit site {site}"
        super().__init__(title, ctx, shell, parent)
        self.customer, self.site = customer, site
        ws = ctx.workspace
        inv = ws.customers[customer] if ws is not None and customer in ws.customers else None
        customer_login = inv.config.credential if inv else ""
        self.original_login = inv.config.site_credentials.get(site) if inv and site else None
        self.name_edit = QLineEdit(site or "")
        self.name_edit.setPlaceholderText("lowercase letters, digits, dashes")
        self.login = LoginPicker(
            ctx,
            default_label=f"Default (customer: {customer_login})",
            current=self.original_login,
        )
        self.original_text = read_site_text(ctx, customer, site) if site else ""
        self.vars_edit = _plain_edit(self.original_text, 150)
        self.vars_edit.setPlaceholderText("Optional YAML, e.g.\nsite_code: SA")
        self.form.addRow("Name", self.name_edit)
        self.form.addRow("Default login", self.login)
        self.form.addRow("Variables", self.vars_edit)
        if self.login.load_error:
            self.set_error(self.login.load_error)

    def save(self) -> bool:
        self.set_error("")
        name = self.name_edit.text().strip()
        credential = self.login.login()
        text = self.vars_edit.toPlainText()
        try:
            parse_vars_text(text)
            editor = make_editor(self.ctx)
        except (ValueError, TsunamiError) as exc:
            self.set_error(str(exc))
            return False
        if self.site is None:
            return self._run(
                lambda: editor.create_site(
                    self.customer, name, credential=credential, vars_text=text
                ),
                f"Added site {name}.",
            )
        site = self.site
        return self._run(
            lambda: editor.update_site(
                self.customer,
                site,
                new_name=name if name != site else None,
                credential=credential,
                set_credential=credential != self.original_login,
                vars_text=text if text != self.original_text else None,
            ),
            f"Saved site {name}.",
        )


class CustomerDialog(_EditDialog):
    """New customer (slug is None) or edit one: name, login, lab flag, jump host."""

    def __init__(
        self,
        ctx: AppContext,
        shell: Shell,
        *,
        slug: str | None = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__("New customer" if slug is None else f"Customer {slug}", ctx, shell, parent)
        self.slug = slug
        ws = ctx.workspace
        config = ws.customers[slug].config if ws is not None and slug in ws.customers else None
        self.config = config
        self.slug_edit = QLineEdit(slug or "")
        self.slug_edit.setPlaceholderText("e.g. acme: lowercase letters, digits, dashes")
        self.slug_edit.setReadOnly(slug is not None)
        self.name_edit = QLineEdit(config.name if config else "")
        self.name_edit.setPlaceholderText("Display name")
        self.login = LoginPicker(ctx, current=config.credential if config else None)
        self.lab_check = QCheckBox("Lab customer")
        self.lab_check.setChecked(bool(config and config.lab))

        jump = config.jump_host if config else None
        self.jump_group = QGroupBox("Jump host")
        self.jump_group.setCheckable(True)
        self.jump_group.setChecked(jump is not None)
        jump_form = QFormLayout(self.jump_group)
        jump_form.setContentsMargins(14, 12, 14, 12)
        jump_form.setHorizontalSpacing(14)
        jump_form.setVerticalSpacing(10)
        self.jump_host_edit = QLineEdit(str(jump.host) if jump else "")
        self.jump_host_edit.setPlaceholderText("IP address or DNS name")
        self.jump_port = QSpinBox()
        self.jump_port.setRange(1, 65535)
        self.jump_port.setFixedWidth(96)
        self.jump_port.setValue(jump.port if jump else 22)
        self.jump_login = LoginPicker(ctx, current=jump.credential if jump else None)
        jump_form.addRow("Host", self.jump_host_edit)
        jump_form.addRow("Port", self.jump_port)
        jump_form.addRow("Login", self.jump_login)

        self.form.addRow("ID", self.slug_edit)  # the folder name; plays name the customer by it
        self.form.addRow("Name", self.name_edit)
        self.form.addRow("Login", self.login)
        self.form.addRow("", self.lab_check)
        self.form.addRow(self.jump_group)
        if self.login.load_error:
            self.set_error(self.login.load_error)

    def _jump_host(self) -> dict[str, Any] | None:
        """The jump host as a dict, None if off. Raises ValueError if it is invalid."""
        if not self.jump_group.isChecked():
            return None
        credential = self.jump_login.login()
        if not credential:
            raise ValueError("Choose a login for the jump host.")
        data = {
            "host": self.jump_host_edit.text().strip(),
            "port": self.jump_port.value(),
            "credential": credential,
        }
        try:
            JumpHost.model_validate(data)
        except ValidationError:
            raise ValueError("The jump host needs an IP address or DNS name.") from None
        return data

    def save(self) -> bool:
        self.set_error("")
        name = self.name_edit.text().strip()
        credential = self.login.login()
        try:
            jump = self._jump_host()
            if not name:
                raise ValueError("Enter a name for the customer.")
            if not credential:
                raise ValueError("Choose a login for the customer.")
            editor = make_editor(self.ctx)
        except (ValueError, TsunamiError) as exc:
            self.set_error(str(exc))
            return False
        lab = self.lab_check.isChecked()
        if self.slug is None:
            slug = self.slug_edit.text().strip()

            def create() -> None:
                editor.create_customer(slug, name=name, credential=credential, lab=lab)
                if jump is not None:
                    editor.update_customer(slug, jump_host=jump)

            return self._run(create, f"Added customer {slug}.")
        slug = self.slug
        old = self.config
        before_jump = (
            None
            if old is None or old.jump_host is None
            else {
                "host": str(old.jump_host.host),
                "port": old.jump_host.port,
                "credential": old.jump_host.credential,
            }
        )
        after = {"name": name, "credential": credential, "lab": lab, "jump_host": jump}
        before = {
            "name": old.name if old else None,
            "credential": old.credential if old else None,
            "lab": old.lab if old else False,
            "jump_host": before_jump,
        }
        changed = {k: v for k, v in after.items() if before[k] != v}
        if not changed:
            return True
        return self._run(lambda: editor.update_customer(slug, **changed), f"Saved customer {slug}.")


class SiteView(QWidget):
    """Read-only settings for one site, in an editor tab, with an Edit button."""

    def __init__(self, ctx: AppContext, shell: Shell, customer: str, site: str) -> None:
        super().__init__()
        self.ctx, self.shell = ctx, shell
        self.customer, self.site = customer, site
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        top = QHBoxLayout()
        self.title = QLabel(site)
        self.title.setObjectName("Title")
        top.addWidget(self.title, 1)
        self.edit_button = QPushButton(icon("edit", theme.current().text), "Edit…")
        self.edit_button.clicked.connect(lambda _=False: self.edit())
        top.addWidget(self.edit_button)
        layout.addLayout(top)
        self.details = QLabel()
        self.details.setWordWrap(True)
        self.details.setObjectName("Muted")
        layout.addWidget(self.details)
        layout.addWidget(self._section("Devices"))
        self.device_list = QTreeWidget()
        self.device_list.setColumnCount(4)
        self.device_list.setHeaderLabels(["Device", "Type", "Platform", "Login"])
        self.device_list.setRootIsDecorated(False)
        self.device_list.setUniformRowHeights(True)
        self.device_list.setColumnWidth(0, 160)
        self.device_list.setColumnWidth(1, 190)
        self.device_list.setColumnWidth(2, 90)
        self.device_list.setMinimumHeight(120)
        layout.addWidget(self.device_list, 1)
        layout.addWidget(self._section("Variables"))
        self.vars_view = QPlainTextEdit()
        self.vars_view.setReadOnly(True)
        self.vars_view.setFont(theme.mono_font())
        layout.addWidget(self.vars_view, 1)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    @staticmethod
    def _section(text: str) -> QLabel:
        label = QLabel(text)
        label.setObjectName("Section")
        return label

    def edit(self) -> bool:
        if self.ctx.workspace is None:
            return False
        return run_dialog(
            SiteDialog(self.ctx, self.shell, self.customer, site=self.site, parent=self)
        )

    def refresh(self) -> None:
        self.device_list.clear()
        self.vars_view.clear()
        ws = self.ctx.workspace
        inv = ws.customers.get(self.customer) if ws is not None else None
        if inv is None or self.site not in inv.sites:
            self.details.setText("This site is not in the loaded workspace.")
            self.edit_button.setEnabled(False)
            return
        self.edit_button.setEnabled(True)
        site_default = inv.config.site_credentials.get(self.site)
        effective = site_default or inv.config.credential
        self.details.setText(
            f"Customer {self.customer}\n"
            f"Default login: {site_default or 'none set (uses the customer login)'}\n"
            f"Login for devices without their own: {effective}"
        )
        for dev in sorted(inv.devices, key=lambda d: d.name):
            if dev.site == self.site:
                login = inv.credential_for(dev)
                QTreeWidgetItem(
                    self.device_list, [dev.name, type_label(dev.role), str(dev.platform), login]
                )
        text = read_site_text(self.ctx, self.customer, self.site)
        self.vars_view.setPlainText(text if text.strip() else "(no variables)")
