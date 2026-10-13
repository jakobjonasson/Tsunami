"""Device list tab for a customer or a site, and the play picker for the selected devices.

See design.md, GUI: Views.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QDialog,
    QDialogButtonBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.models.inventory import DEVICE_TYPES
from tsunami.core.models.play import GLOBAL_CUSTOMER, PlayType
from tsunami.core.targets import resolve_targets
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.shell import Shell

COLUMNS = ("Name", "Type", "Platform", "Model", "Host", "Site", "Login")
_STRETCH_COLUMNS = ("Model", "Login")
RUN_DISABLED_TIP = "Select one or more devices to run a play"
_NAME_ROLE = Qt.ItemDataRole.UserRole
_ROLE_ROLE = Qt.ItemDataRole.UserRole + 1


def type_label(role: str) -> str:
    """The label for a device type; a custom role shows its slug."""
    return DEVICE_TYPES.get(role, role)


def _toggle_style() -> str:
    accent = theme.current().accent
    return (
        f"QPushButton:checked {{ border: 1px solid {accent}; color: {accent}; padding: 4px 14px; }}"
    )


def run_picker(dialog: QDialog) -> bool:
    """Show the picker modally. True if accepted. Tests replace this."""
    return dialog.exec() == QDialog.DialogCode.Accepted


class PlayPickerDialog(QDialog):
    """Lists the customer's plays, and global plays, that target every selected device."""

    def __init__(
        self, ctx: AppContext, customer: str, devices: list[str], parent: QWidget | None = None
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Run play")
        self.setMinimumWidth(520)
        self.devices = devices
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        head = QLabel(f"Devices ({len(devices)}): {', '.join(devices)}")
        head.setWordWrap(True)
        layout.addWidget(head)
        self.tree = QTreeWidget()
        self.tree.setColumnCount(3)
        self.tree.setHeaderLabels(["Play", "Type", "Path"])
        self.tree.setRootIsDecorated(False)
        self.tree.header().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        self.tree.header().setStretchLastSection(True)
        self.tree.itemDoubleClicked.connect(lambda *_: self.accept())
        layout.addWidget(self.tree, 1)
        self.explanation = QLabel(
            "No play of this customer targets all of the selected devices. Select fewer "
            "devices, or add the devices to a play's targets."
        )
        self.explanation.setObjectName("Muted")
        self.explanation.setWordWrap(True)
        layout.addWidget(self.explanation)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Run…")
        ok.setProperty("primary", True)
        layout.addWidget(self.buttons)
        self.tree.currentItemChanged.connect(lambda *_: self._update_ok())

        for path, name, kind in self._offered(ctx, customer):
            item = QTreeWidgetItem([name, kind, path.as_posix()])
            item.setData(0, _NAME_ROLE, path)
            self.tree.addTopLevelItem(item)
        found = self.tree.topLevelItemCount() > 0
        if found:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        self.tree.setVisible(found)
        self.explanation.setVisible(not found)
        self._update_ok()

    def _offered(self, ctx: AppContext, customer: str) -> list[tuple[Path, str, str]]:
        ws = ctx.workspace
        if ws is None or customer not in ws.customers:
            return []
        cust = ws.customers[customer]
        out = []
        for entry in ws.plays.values():
            play = entry.play
            if entry.draft or play is None:
                continue
            is_global = play.customer == GLOBAL_CUSTOMER
            if play.customer != customer and not is_global:
                continue
            if play.type == PlayType.INITIAL:
                continue
            result = resolve_targets(
                play.targets,
                cust.devices,
                sites=cust.sites.keys(),
                roles=ws.roles.keys(),
                limit=self.devices,
            )
            if result.ok:
                name = f"{play.name} (global)" if is_global else play.name
                out.append((entry.path, name, str(play.type)))
        return sorted(out, key=lambda r: (r[1], r[0]))

    def offered_paths(self) -> list[Path]:
        return [
            self.tree.topLevelItem(i).data(0, _NAME_ROLE)
            for i in range(self.tree.topLevelItemCount())
        ]

    def _update_ok(self) -> None:
        self.buttons.button(QDialogButtonBox.StandardButton.Ok).setEnabled(
            self.selected_play() is not None
        )

    def selected_play(self) -> Path | None:
        item = self.tree.currentItem()
        return item.data(0, _NAME_ROLE) if item is not None else None


def pick_play_and_run(
    ctx: AppContext,
    shell: Shell,
    customer: str,
    devices: list[str],
    parent: QWidget | None = None,
) -> bool:
    """Show the play picker; on OK open the run wizard with those devices preselected."""
    if not devices:
        return False
    dialog = PlayPickerDialog(ctx, customer, devices, parent)
    if not run_picker(dialog):
        return False
    path = dialog.selected_play()
    if path is None:
        return False
    from tsunami.gui.screens.run_wizard import open_run_wizard

    open_run_wizard(ctx, shell, path, devices=devices, customer=customer)
    return True


class DeviceListView(QWidget):
    """Devices of one customer, or of one site, with type and text filters."""

    def __init__(
        self, ctx: AppContext, shell: Shell, customer: str, site: str | None = None
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self.customer = customer
        self.site = site
        self._type_buttons: dict[str, QPushButton] = {}
        self._hidden_types: set[str] = set()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        self.title = QLabel(f"{customer} / {site}" if site else customer)
        self.title.setObjectName("Title")
        layout.addWidget(self.title)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("Muted")
        layout.addWidget(self.subtitle)

        self.type_row = QHBoxLayout()
        self.type_row.setSpacing(6)
        layout.addLayout(self.type_row)

        bar = QHBoxLayout()
        bar.setSpacing(8)
        self.filter_box = QLineEdit()
        self.filter_box.setPlaceholderText("Filter devices")
        self.filter_box.setClearButtonEnabled(True)
        self.filter_box.textChanged.connect(self._apply_filter)
        bar.addWidget(self.filter_box, 1)
        self.select_all_button = QPushButton("Select all shown")
        self.select_all_button.clicked.connect(self.select_all_shown)
        bar.addWidget(self.select_all_button)
        self.run_button = QPushButton("Run play…")
        self.run_button.setProperty("primary", True)
        self.run_button.clicked.connect(self.run_play)
        bar.addWidget(self.run_button)
        layout.addLayout(bar)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(list(COLUMNS))
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        self.table.setAlternatingRowColors(True)
        header = self.table.horizontalHeader()
        header.setDefaultAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        header.setMinimumSectionSize(60)
        # Short columns size to their contents; Model and Login share what is left, so the
        # table fits without a horizontal scroll bar until the contents alone outgrow it.
        header.setStretchLastSection(False)
        for col, name in enumerate(COLUMNS):
            mode = (
                QHeaderView.ResizeMode.Stretch
                if name in _STRETCH_COLUMNS
                else QHeaderView.ResizeMode.ResizeToContents
            )
            header.setSectionResizeMode(col, mode)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(self._open_row)
        layout.addWidget(self.table, 1)

        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    # -- data -------------------------------------------------------------------------

    def _devices(self) -> list[Any]:
        ws = self.ctx.workspace
        if ws is None or self.customer not in ws.customers:
            return []
        devices = ws.customers[self.customer].devices
        if self.site is not None:
            devices = [d for d in devices if d.site == self.site]
        return sorted(devices, key=lambda d: d.name)

    def refresh(self) -> None:
        keep = set(self.selected_names())
        ws = self.ctx.workspace
        cust = ws.customers.get(self.customer) if ws else None
        devices = self._devices()
        self.subtitle.setText(
            f"{len(devices)} device{'s' if len(devices) != 1 else ''}"
            if cust is not None
            else "This customer is not in the loaded workspace."
        )
        self._build_type_buttons(devices)
        self.table.setRowCount(len(devices))
        for row, dev in enumerate(devices):
            login = cust.credential_for(dev) if cust is not None else ""
            cells = (
                dev.name,
                type_label(dev.role),
                dev.platform,
                dev.model or "",
                dev.host,
                dev.site,
                login,
            )
            for col, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setToolTip(text)
                if col == 0:
                    item.setData(_NAME_ROLE, dev.name)
                    item.setData(_ROLE_ROLE, dev.role)
                self.table.setItem(row, col, item)
        self._apply_filter()
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            if self._name_at(row) in keep and not self.table.isRowHidden(row):
                self.table.selectRow(row)
        self._update_buttons()

    def _build_type_buttons(self, devices: list[Any]) -> None:
        present = {d.role for d in devices}
        order = [r for r in DEVICE_TYPES if r in present] + sorted(present - DEVICE_TYPES.keys())
        if order == list(self._type_buttons):
            return
        while self.type_row.count():
            child = self.type_row.takeAt(0)
            widget = child.widget()
            if widget is not None:
                widget.setParent(None)
                widget.deleteLater()
        self._type_buttons = {}
        for role in order:
            button = QPushButton(type_label(role))
            button.setCheckable(True)
            button.setStyleSheet(_toggle_style())
            button.setChecked(role not in self._hidden_types)
            button.toggled.connect(lambda on, r=role: self._type_toggled(r, on))
            self.type_row.addWidget(button)
            self._type_buttons[role] = button
        self.type_row.addStretch(1)

    def _type_toggled(self, role: str, on: bool) -> None:
        if on:
            self._hidden_types.discard(role)
        else:
            self._hidden_types.add(role)
        self._apply_filter()

    def _apply_filter(self) -> None:
        needle = self.filter_box.text().strip().lower()
        for row in range(self.table.rowCount()):
            role = self.table.item(row, 0).data(_ROLE_ROLE)
            text = " ".join(self.table.item(row, c).text() for c in range(len(COLUMNS))).lower()
            hide = role in self._hidden_types or (bool(needle) and needle not in text)
            self.table.setRowHidden(row, hide)
        self._update_buttons()

    # -- selection and actions -------------------------------------------------------

    def _name_at(self, row: int) -> str:
        return self.table.item(row, 0).data(_NAME_ROLE)

    def visible_names(self) -> list[str]:
        return [
            self._name_at(r) for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)
        ]

    def selected_names(self) -> list[str]:
        rows = sorted({i.row() for i in self.table.selectedItems()})
        return [self._name_at(r) for r in rows]

    def select_all_shown(self) -> None:
        self.table.clearSelection()
        for row in range(self.table.rowCount()):
            if not self.table.isRowHidden(row):
                self.table.selectRow(row)

    def _update_buttons(self) -> None:
        has_selection = bool(self.selected_names())
        self.run_button.setEnabled(has_selection)
        self.run_button.setToolTip("" if has_selection else RUN_DISABLED_TIP)
        self.select_all_button.setEnabled(bool(self.visible_names()))

    def _open_row(self, row: int, _col: int) -> None:
        from tsunami.gui.screens.inventory import open_device

        open_device(self.ctx, self.shell, self.customer, self._name_at(row))

    def run_play(self) -> None:
        pick_play_and_run(self.ctx, self.shell, self.customer, self.selected_names(), self)


def open_device_list(ctx: AppContext, shell: Shell, customer: str, site: str | None = None) -> None:
    key = f"devices:{customer}/{site}" if site else f"devices:{customer}"
    title = f"{site} devices" if site else f"{customer} devices"
    shell.open_tab(
        key, title, lambda: DeviceListView(ctx, shell, customer, site), "server-environment"
    )
