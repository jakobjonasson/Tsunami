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
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.resolver import Level, ResolvedVars
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import ActivityItem, Shell

_DEVICE_ROLE = Qt.ItemDataRole.UserRole
_SEARCH_ROLE = Qt.ItemDataRole.UserRole + 1

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
            sites: dict[str, QTreeWidgetItem] = {}
            for dev in sorted(customer.devices, key=lambda d: d.name):
                if dev.site not in sites:
                    s_item = QTreeWidgetItem(c_item, [dev.site])
                    s_item.setIcon(0, icon("folder", colors.muted))
                    sites[dev.site] = s_item
                label = f"{dev.name}    {dev.platform}" + (f"  {dev.model}" if dev.model else "")
                d_item = QTreeWidgetItem(sites[dev.site], [label])
                d_item.setIcon(0, icon("server-environment", colors.accent))
                d_item.setData(0, _DEVICE_ROLE, (slug, dev.name))
                search = f"{dev.name} {dev.host} {dev.platform} {dev.model or ''}"
                d_item.setData(0, _SEARCH_ROLE, search.lower())
                d_item.setToolTip(0, f"{dev.name}  {dev.host}  site {dev.site}  role {dev.role}")
        self.tree.expandAll()
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.filter_box.text().strip().lower()

        def visit(item: QTreeWidgetItem) -> bool:
            key = item.data(0, _SEARCH_ROLE)
            if key is not None:
                visible = needle in key
            else:
                visible = any([visit(item.child(i)) for i in range(item.childCount())])
            item.setHidden(not visible)
            return visible

        for i in range(self.tree.topLevelItemCount()):
            visit(self.tree.topLevelItem(i))

    def _open_item(self, item: QTreeWidgetItem) -> None:
        target = item.data(0, _DEVICE_ROLE)
        if target is not None:
            open_device(self.ctx, self.shell, *target)


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
