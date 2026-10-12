"""Inventory view: customer > site > device tree, and the device tab with resolved
variables and where each one came from.

See design.md, GUI: Views; Inventory and variables: Provenance.
"""

from __future__ import annotations

import json
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
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
from tsunami.core.resolver import Level, ResolvedVars
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.screens import device_list
from tsunami.gui.screens import inventory_dialogs as dialogs
from tsunami.gui.shell import ActivityItem, Shell

_DEVICE_ROLE = Qt.ItemDataRole.UserRole
_SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1
_KIND_ROLE = Qt.ItemDataRole.UserRole + 2  # ("customer", c) | ("site", c, s) | ("device", c, d)

# Codicon per device type; a custom role gets the generic one.
TYPE_ICONS: dict[str, str] = {
    "core": "server",
    "distribution": "type-hierarchy",
    "access": "plug",
    "wlc": "broadcast",
    "ap": "radio-tower",
    "router": "globe",
}
DEFAULT_TYPE_ICON = "server-process"

# One color per precedence level, DEFAULT to BUILTIN. Mid-tone, so they read on both
# the light and the dark palette.
LEVEL_COLORS: dict[Level, str] = {
    Level.DEFAULT: "#8a8f98",
    Level.GLOBAL: "#4f8fd6",
    Level.ROLE: "#2fa4a0",
    Level.CUSTOMER: "#4aa564",
    Level.CUSTOMER_ROLE: "#8aa62f",
    Level.SITE: "#d49a1f",
    Level.DEVICE: "#e0773a",
    Level.PLAY: "#d1566f",
    Level.PROMPT: "#a566c9",
    Level.BUILTIN: "#7a6fd1",
}


def _format_value(value: Any) -> str:
    return value if isinstance(value, str) else json.dumps(value, default=str)


class DeviceView(QWidget):
    """One device: header and its resolved variables with level and source file."""

    def __init__(self, ctx: AppContext, shell: Shell, customer: str, device: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self.customer = customer
        self.device = device
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.title = QLabel(device)
        self.title.setObjectName("Title")
        top.addWidget(self.title, 1)
        self.vscode_button = QPushButton(icon("edit", theme.current().text), "Open in VS Code")
        self.vscode_button.clicked.connect(self._open_vscode)
        top.addWidget(self.vscode_button)
        layout.addLayout(top)
        self.details = QLabel()
        self.details.setObjectName("Muted")
        self.details.setWordWrap(True)
        layout.addWidget(self.details)

        self.tree = QTreeWidget()
        self.tree.setColumnCount(4)
        self.tree.setHeaderLabels(["Variable", "Value", "Level", "Source"])
        self.tree.setUniformRowHeights(True)
        header = self.tree.header()
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        header.setStretchLastSection(True)
        self.tree.setColumnWidth(0, 220)
        self.tree.setColumnWidth(1, 300)
        self.tree.setColumnWidth(2, 110)
        layout.addWidget(self.tree, 1)

        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def _open_vscode(self) -> None:
        ws = self.ctx.workspace
        if ws is not None:
            self.shell.open_in_vscode(
                ws.root / "inventory" / "customers" / self.customer / "devices.yml"
            )

    def refresh(self) -> None:
        self.tree.clear()
        ws = self.ctx.workspace
        dev = None
        if ws is not None and self.customer in ws.customers:
            dev = ws.customers[self.customer].device(self.device)
        if ws is None or dev is None:
            self.details.setText("This device is not in the loaded workspace.")
            return
        self.details.setText(
            f"{self.customer}  |  host {dev.host}  |  {dev.platform}  |  model {dev.model or '-'}"
            f"  |  site {dev.site}  |  role {dev.role}"
        )
        resolved = ws.resolve_vars(self.customer, device=self.device)
        self._add_values(self.tree.invisibleRootItem(), resolved.values, resolved, ())
        self.tree.expandAll()

    def _add_values(
        self,
        parent: QTreeWidgetItem,
        values: dict[str, Any],
        resolved: ResolvedVars,
        prefix: tuple[str, ...],
    ) -> None:
        for key in sorted(values):
            value = values[key]
            path = (*prefix, key)
            source = resolved.source_of(*path)
            nested = isinstance(value, dict) and bool(value)
            item = QTreeWidgetItem(parent, [key, "" if nested else _format_value(value), "", ""])
            item.setToolTip(1, item.text(1))
            if source is not None:
                item.setText(2, source.level.label)
                item.setForeground(2, QBrush(QColor(LEVEL_COLORS[source.level])))
                item.setText(3, source.file.as_posix() if source.file else "")
                where = source.file.as_posix() if source.file else "set by the engine or a prompt"
                tip = f"{source.level.label}: {where}"
                for column in (0, 2, 3):
                    item.setToolTip(column, tip)
            if nested:
                self._add_values(item, value, resolved, path)


class InventoryView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        layout.setSpacing(4)
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter devices")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._apply_filter)
        layout.addWidget(self.filter_box)
        self.hint = QLabel("No workspace is open.")
        self.hint.setObjectName("Muted")
        self.hint.setMargin(8)
        layout.addWidget(self.hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.tree.itemDoubleClicked.connect(self._open_item)
        self.tree.itemClicked.connect(self._click_item)
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
        layout.addWidget(self.tree, 1)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        colors = theme.current()
        self.tree.clear()
        ws = self.ctx.workspace
        self.hint.setVisible(ws is None)
        self.tree.setVisible(ws is not None)
        self.filter_box.setEnabled(ws is not None)
        if ws is None:
            return
        for slug in sorted(ws.customers):
            customer = ws.customers[slug]
            c_item = QTreeWidgetItem(self.tree, [slug])
            c_item.setIcon(0, icon("organization", colors.muted))
            c_item.setData(0, _KIND_ROLE, ("customer", slug))
            c_item.setToolTip(0, f"{customer.config.name}  login {customer.config.credential}")
            sites: dict[str, QTreeWidgetItem] = {}
            for site in sorted(customer.sites):
                s_item = QTreeWidgetItem(c_item, [site])
                s_item.setIcon(0, icon("folder", colors.muted))
                s_item.setData(0, _KIND_ROLE, ("site", slug, site))
                sites[site] = s_item
            for dev in sorted(customer.devices, key=lambda d: d.name):
                if dev.site not in sites:  # the loader reports it; still show the device
                    s_item = QTreeWidgetItem(c_item, [dev.site])
                    s_item.setIcon(0, icon("folder", colors.muted))
                    s_item.setData(0, _KIND_ROLE, ("site", slug, dev.site))
                    sites[dev.site] = s_item
                kind = dialogs.type_label(dev.role)
                label = f"{dev.name}    {kind} · {dev.platform}"
                d_item = QTreeWidgetItem(sites[dev.site], [label])
                d_item.setIcon(0, icon(TYPE_ICONS.get(dev.role, DEFAULT_TYPE_ICON), colors.accent))
                d_item.setData(0, _DEVICE_ROLE, (slug, dev.name))
                d_item.setData(0, _KIND_ROLE, ("device", slug, dev.name))
                search = f"{dev.name} {dev.host} {kind} {dev.platform} {dev.model or ''}"
                d_item.setData(0, _SEARCH_ROLE, search.lower())
                tip = f"{dev.name}  {dev.host}  site {dev.site}  type {kind}"
                if dev.model:
                    tip += f"  model {dev.model}"
                d_item.setToolTip(0, tip + f"  login {customer.credential_for(dev)}")
        self.tree.expandAll()
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.filter_box.text().strip().lower()

        def visit(item: QTreeWidgetItem) -> bool:
            if not needle:
                item.setHidden(False)
                for i in range(item.childCount()):
                    visit(item.child(i))
                return True
            key = item.data(0, _SEARCH_ROLE)
            if key is not None:
                visible = needle in key
            else:
                visible = any([visit(item.child(i)) for i in range(item.childCount())])
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def _click_item(self, item: QTreeWidgetItem) -> None:
        kind = item.data(0, _KIND_ROLE)
        if kind is not None and kind[0] == "customer":
            self.show_devices(kind[1])
        elif kind is not None and kind[0] == "site":
            self.show_devices(kind[1], kind[2])

    def _open_item(self, item: QTreeWidgetItem) -> None:
        target = item.data(0, _DEVICE_ROLE)
        if target is not None:
            open_device(self.ctx, self.shell, *target)

    # -- context menus ------------------------------------------------------------------

    def _show_menu(self, pos: Any) -> None:
        menu = self.menu_for(self.tree.itemAt(pos))
        menu.exec(self.tree.viewport().mapToGlobal(pos))

    def menu_for(self, item: QTreeWidgetItem | None) -> QMenu:
        """The context menu for a tree item; None is empty space."""
        menu = QMenu(self)
        colors = theme.current()
        editable = self.ctx.workspace is not None
        kind = item.data(0, _KIND_ROLE) if item is not None else None

        def add(text: str, icon_name: str, slot: Any, *args: str) -> None:
            action = menu.addAction(icon(icon_name, colors.text), text)
            action.setEnabled(editable)
            action.triggered.connect(lambda _=False: slot(*args))

        if kind is None:
            add("New customer…", "add", self.new_customer)
        elif kind[0] == "customer":
            add("Show devices", "server-environment", self.show_devices, kind[1])
            add("Add site…", "add", self.new_site, kind[1])
            add("Properties…", "info", self.edit_customer, kind[1])
            menu.addSeparator()
            add("Delete…", "trash", self.delete_customer, kind[1])
        elif kind[0] == "site":
            add("Show devices", "server-environment", self.show_devices, kind[1], kind[2])
            add("New device…", "add", self.new_device, kind[1], kind[2])
            add("Edit…", "edit", self.edit_site, kind[1], kind[2])
            add("View settings", "info", self.view_site, kind[1], kind[2])
            menu.addSeparator()
            add("Delete…", "trash", self.delete_site, kind[1], kind[2])
        else:
            add("Run play…", "play", self.run_play, kind[1], kind[2])
            add("Edit…", "edit", self.edit_device, kind[1], kind[2])
            add("Properties", "info", self.open_properties, kind[1], kind[2])
            menu.addSeparator()
            add("Delete…", "trash", self.delete_device, kind[1], kind[2])
        return menu

    def show_devices(self, customer: str, site: str | None = None) -> None:
        device_list.open_device_list(self.ctx, self.shell, customer, site)

    def run_play(self, customer: str, name: str) -> None:
        device_list.pick_play_and_run(self.ctx, self.shell, customer, [name], self)

    def _editor(self) -> InventoryEditor | None:
        try:
            return dialogs.make_editor(self.ctx)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return None

    def _confirm(self, title: str, text: str) -> bool:
        return QMessageBox.question(self, title, text) == QMessageBox.StandardButton.Yes

    def _inventory(self, customer: str) -> Any:
        ws = self.ctx.workspace
        return ws.customers.get(customer) if ws is not None else None

    def _delete(self, action: Any, message: str) -> bool:
        try:
            action()
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return False
        self.shell.refresh_all()
        self.shell.notify(message)
        return True

    def new_customer(self) -> None:
        dialogs.run_dialog(dialogs.CustomerDialog(self.ctx, self.shell, parent=self))

    def edit_customer(self, customer: str) -> None:
        dialogs.run_dialog(dialogs.CustomerDialog(self.ctx, self.shell, slug=customer, parent=self))

    def delete_customer(self, customer: str) -> None:
        inv = self._inventory(customer)
        editor = self._editor()
        if inv is None or editor is None:
            return
        sites, devices = len(inv.sites), len(inv.devices)
        text = (
            f"This moves the {customer} folder, with its {sites} site{'s' if sites != 1 else ''} "
            f"and {devices} device{'s' if devices != 1 else ''}, to the Recycle Bin. "
            "You can restore it from there."
        )
        if self._confirm(f"Delete customer {customer}", text):
            self._delete(lambda: editor.delete_customer(customer), f"Deleted customer {customer}.")

    def new_site(self, customer: str) -> None:
        dialogs.run_dialog(dialogs.SiteDialog(self.ctx, self.shell, customer, parent=self))

    def edit_site(self, customer: str, site: str) -> None:
        dialogs.run_dialog(
            dialogs.SiteDialog(self.ctx, self.shell, customer, site=site, parent=self)
        )

    def view_site(self, customer: str, site: str) -> None:
        self.shell.open_tab(
            f"site:{customer}/{site}",
            site,
            lambda: dialogs.SiteView(self.ctx, self.shell, customer, site),
            "folder",
        )

    def delete_site(self, customer: str, site: str) -> None:
        inv = self._inventory(customer)
        editor = self._editor()
        if inv is None or editor is None:
            return
        count = sum(1 for d in inv.devices if d.site == site)
        if count:
            noun = "device" if count == 1 else "devices"
            text = (
                f"Site {site} has {count} {noun}. Deleting the site also deletes "
                f"{'that' if count == 1 else 'those'} {count} {noun} from devices.yml, and moves "
                "the site file to the Recycle Bin."
            )
        else:
            text = f"This moves the site file for {site} to the Recycle Bin."
        if self._confirm(f"Delete site {site}", text):
            self._delete(
                lambda: editor.delete_site(customer, site, delete_devices=count > 0),
                f"Deleted site {site}.",
            )

    def new_device(self, customer: str, site: str) -> None:
        dialogs.run_dialog(
            dialogs.DeviceDialog(self.ctx, self.shell, customer, site=site, parent=self)
        )

    def edit_device(self, customer: str, name: str) -> None:
        inv = self._inventory(customer)
        device = inv.device(name) if inv is not None else None
        if device is not None:
            dialogs.run_dialog(
                dialogs.DeviceDialog(self.ctx, self.shell, customer, device=device, parent=self)
            )

    def open_properties(self, customer: str, name: str) -> None:
        open_device(self.ctx, self.shell, customer, name)

    def delete_device(self, customer: str, name: str) -> None:
        editor = self._editor()
        if editor is None:
            return
        if self._confirm(f"Delete device {name}", f"This removes {name} from devices.yml."):
            if self._delete(
                lambda: editor.delete_device(customer, name), f"Deleted device {name}."
            ):
                self.shell.close_tab(f"device:{customer}/{name}")


def open_device(ctx: AppContext, shell: Shell, customer: str, device: str) -> None:
    shell.open_tab(
        f"device:{customer}/{device}",
        device,
        lambda: DeviceView(ctx, shell, customer, device),
        "server-environment",
    )


def factory(ctx: AppContext, shell: Shell) -> QWidget:
    return InventoryView(ctx, shell)


def register(shell: Shell) -> None:
    shell.set_handler(
        "inventory.open_device",
        lambda customer, device: open_device(shell.ctx, shell, customer, device),
    )


VIEW = ActivityItem("inventory", "Inventory", "server-environment", factory, order=20)
