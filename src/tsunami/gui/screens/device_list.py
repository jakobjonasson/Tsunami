"""Device list tab for a customer or a site, and the play picker for the selected devices.

See design.md, GUI: Views.
"""

from __future__ import annotations

import ipaddress
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QItemSelection, QItemSelectionModel, Qt, QTimer
from PySide6.QtGui import QBrush, QColor
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
from tsunami.core.models.jobs import DeviceStatus
from tsunami.core.models.play import GLOBAL_CUSTOMER, PlayType
from tsunami.core.targets import resolve_targets
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.screens.history import relative_time
from tsunami.gui.screens.job_view import status_color
from tsunami.gui.shell import Shell

COLUMNS = (
    "Name",
    "Type",
    "Platform",
    "Model",
    "Version",
    "Last job",
    "Last contact",
    "Host",
    "Site",
    "Login",
)
_STRETCH_COLUMNS = ("Model", "Login")
_STATUS_COLUMNS = ("Last job", "Last contact")
RUN_DISABLED_TIP = "Select one or more devices to run a play"
NEVER = "never"
_NAME_ROLE = Qt.ItemDataRole.UserRole
_ROLE_ROLE = Qt.ItemDataRole.UserRole + 1
_SORT_ROLE = Qt.ItemDataRole.UserRole + 2
_REFRESH_TIMES_MS = 60_000

# Last job column sort order, problems first: sorting ascending groups the failures at
# the top, then what ran, then what never did. Newer jobs come later within a group.
_STATUS_ORDER = [
    DeviceStatus.ROLLBACK_FAILED,
    DeviceStatus.FAILED,
    DeviceStatus.HOSTKEY_MISMATCH,
    DeviceStatus.RENDER_CHANGED,
    DeviceStatus.INTERRUPTED,
    DeviceStatus.UNREACHABLE,
    DeviceStatus.ROLLED_BACK,
    DeviceStatus.SKIPPED_PRECHECK,
    DeviceStatus.DEVICE_BUSY,
    DeviceStatus.CANCELLED,
    DeviceStatus.SKIPPED_MAXFAIL,
    DeviceStatus.CONNECTING,
    DeviceStatus.RUNNING,
    DeviceStatus.PENDING,
    DeviceStatus.READY,
    DeviceStatus.NO_CHANGES,
    DeviceStatus.SUCCESS,
]
_STATUS_RANK = {s: i for i, s in enumerate(_STATUS_ORDER)}


def status_label(status: DeviceStatus) -> str:
    return str(status).replace("_", " ")


def _ts(value: datetime | None) -> float:
    return value.timestamp() if value else 0.0


def local_time(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S") if value else ""


def play_name(ctx: AppContext, play_path: str) -> str:
    """The play's own name for a job's recorded path, or the file name if the play is gone."""
    path = Path(play_path.replace("\\", "/"))
    ws = ctx.workspace
    entry = ws.plays.get(path) if ws is not None else None
    return entry.play.name if entry is not None and entry.play else path.stem


def _host_key(host: str) -> tuple:
    """Addresses sort numerically, names alphabetically after them."""
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return (1, 0, host.lower())
    return (0, addr.version * 2**128 + int(addr), "")


class _SortItem(QTableWidgetItem):
    """A cell that sorts by its sort role (a number or tuple) rather than by its text."""

    def __lt__(self, other: QTableWidgetItem) -> bool:
        mine, theirs = self.data(_SORT_ROLE), other.data(_SORT_ROLE)
        if mine is None or theirs is None:
            return self.text().lower() < other.text().lower()
        return mine < theirs


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
        header.setMinimumSectionSize(48)
        # Columns size to their contents; Model and Login share what is left (see
        # _fit_columns), so the table fits without a horizontal scroll bar until the
        # contents alone outgrow it. With ten columns a plain Stretch would squeeze those two
        # to nothing at narrow widths: they never get narrower than their text.
        header.setStretchLastSection(False)
        for col, name in enumerate(COLUMNS):
            mode = (
                QHeaderView.ResizeMode.Interactive
                if name in _STRETCH_COLUMNS
                else QHeaderView.ResizeMode.ResizeToContents
            )
            header.setSectionResizeMode(col, mode)
        self._wanted: dict[int, int] = {}
        self.table.viewport().installEventFilter(self)
        self.table.itemSelectionChanged.connect(self._update_buttons)
        self.table.cellDoubleClicked.connect(self._open_row)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.SortOrder.AscendingOrder)
        # Hidden rows belong to a row number, not to a device: hide them again after a sort.
        self.table.model().layoutChanged.connect(self._apply_filter)
        layout.addWidget(self.table, 1)

        self._facts: dict[str, Any] = {}
        self._activity: dict[str, Any] = {}
        self._contact: dict[str, datetime] = {}
        self._play_names: dict[str, str] = {}
        ctx.workspace_changed.connect(self.refresh)
        ctx.jobs_changed.connect(self.reload_status)
        # The ages ("5 min ago") move on without any job finishing.
        self._age_timer = QTimer(self)
        self._age_timer.setInterval(_REFRESH_TIMES_MS)
        self._age_timer.timeout.connect(self._fill_status)
        self._age_timer.start()
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

    def _load_status(self) -> None:
        """One query each, for the whole customer: not one per device."""
        history = self.ctx.history
        self._facts = history.device_facts(self.customer)
        self._activity = history.device_activity(self.customer)
        self._contact = history.last_contact(self.customer)
        self._play_names = {}

    def refresh(self) -> None:
        keep = set(self.selected_names())
        self._load_status()
        ws = self.ctx.workspace
        cust = ws.customers.get(self.customer) if ws else None
        devices = self._devices()
        self.subtitle.setText(
            f"{len(devices)} device{'s' if len(devices) != 1 else ''}"
            if cust is not None
            else "This customer is not in the loaded workspace."
        )
        self._build_type_buttons(devices)
        # Rows must not move while they are being filled in.
        sort_column = self.table.horizontalHeader().sortIndicatorSection()
        sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        self.table.setSortingEnabled(False)
        self.table.setRowCount(len(devices))
        for row, dev in enumerate(devices):
            login = cust.credential_for(dev) if cust is not None else ""
            cells = (
                dev.name,
                type_label(dev.role),
                dev.platform,
                dev.model or "",
                "",
                "",
                "",
                dev.host,
                dev.site,
                login,
            )
            for col, text in enumerate(cells):
                item = _SortItem(text)
                item.setToolTip(text)
                is_host = COLUMNS[col] == "Host"
                item.setData(_SORT_ROLE, _host_key(text) if is_host else text.lower())
                if col == 0:
                    item.setData(_NAME_ROLE, dev.name)
                    item.setData(_ROLE_ROLE, dev.role)
                self.table.setItem(row, col, item)
        self._fill_status()
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(sort_column, sort_order)
        self._measure_columns()
        self._apply_filter()
        self._select_rows(
            [
                r
                for r in range(self.table.rowCount())
                if self._name_at(r) in keep and not self.table.isRowHidden(r)
            ]
        )
        self._update_buttons()

    def reload_status(self) -> None:
        """A job finished: re-read what the devices reported and did, and redraw those cells."""
        self._load_status()
        self._fill_status()
        self._measure_columns()

    def _play_name(self, play_path: str) -> str:
        cached = self._play_names.get(play_path)
        if cached is None:
            cached = self._play_names[play_path] = play_name(self.ctx, play_path)
        return cached

    def _fill_status(self) -> None:
        """Model (when the inventory has none), Version and the two status columns, from the
        data loaded by _load_status."""
        job_col, contact_col = (COLUMNS.index(c) for c in _STATUS_COLUMNS)
        version_col, model_col = COLUMNS.index("Version"), COLUMNS.index("Model")
        ws = self.ctx.workspace
        cust = ws.customers.get(self.customer) if ws else None
        colors = theme.current()
        was_sorting = self.table.isSortingEnabled()
        self.table.setSortingEnabled(False)  # setText on a sorted column would move rows
        now = datetime.now(UTC)
        for row in range(self.table.rowCount()):
            name = self._name_at(row)
            facts = self._facts.get(name)
            version = self.table.item(row, version_col)
            version.setText(facts.version if facts and facts.version else "")
            version.setData(_SORT_ROLE, version.text().lower())
            version.setToolTip(
                f"{version.text()}\nreported by the device {local_time(facts.updated_at)}"
                if facts and facts.version
                else ""
            )
            dev = cust.device(name) if cust is not None else None
            if dev is not None and not dev.model:
                model = self.table.item(row, model_col)
                model.setText(facts.model if facts and facts.model else "")
                model.setData(_SORT_ROLE, model.text().lower())
                model.setToolTip(model.text())

            item = self.table.item(row, job_col)
            activity = self._activity.get(name)
            if activity is None:
                item.setText(NEVER)
                item.setForeground(QBrush(QColor(colors.muted)))
                item.setToolTip("No job has touched this device yet")
                item.setData(_SORT_ROLE, (len(_STATUS_RANK) + 1, 0.0))
            else:
                item.setText(f"{status_label(activity.status)} · {relative_time(activity.at, now)}")
                item.setForeground(QBrush(QColor(status_color(activity.status))))
                item.setToolTip(f"{self._play_name(activity.play_path)}\n{local_time(activity.at)}")
                rank = _STATUS_RANK.get(activity.status, len(_STATUS_RANK))
                item.setData(_SORT_ROLE, (rank, _ts(activity.at)))

            item = self.table.item(row, contact_col)
            contact = self._contact.get(name)
            item.setText(relative_time(contact, now) if contact else NEVER)
            item.setToolTip(
                local_time(contact) if contact else "Tsunami has not reached this device yet"
            )
            item.setForeground(QBrush(QColor(colors.text if contact else colors.muted)))
            item.setData(_SORT_ROLE, _ts(contact))
        self.table.setSortingEnabled(was_sorting)

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

    def eventFilter(self, watched: Any, event: Any) -> bool:
        if watched is self.table.viewport() and event.type() == QEvent.Type.Resize:
            self._fit_columns()
        return super().eventFilter(watched, event)

    def _measure_columns(self) -> None:
        """Remember how wide Model and Login need to be for their text, then fit."""
        header = self.table.horizontalHeader()
        for name in _STRETCH_COLUMNS:
            col = COLUMNS.index(name)
            self._wanted[col] = max(self.table.sizeHintForColumn(col), header.sectionSizeHint(col))
        self._fit_columns()

    def _fit_columns(self) -> None:
        """Give Model and Login the width the other columns leave, but never less than their
        text needs."""
        header = self.table.horizontalHeader()
        flexible = [COLUMNS.index(c) for c in _STRETCH_COLUMNS]
        used = sum(header.sectionSize(c) for c in range(len(COLUMNS)) if c not in flexible)
        share = (self.table.viewport().width() - used) // len(flexible)
        for col in flexible:
            width = max(self._wanted.get(col, 0), share, header.minimumSectionSize())
            if header.sectionSize(col) != width:
                header.resizeSection(col, width)

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

    def _select_rows(self, rows: list[int]) -> None:
        """Select exactly these rows. selectRow() per row would leave only the last one
        selected in extended-selection mode."""
        selection = QItemSelection()
        last = self.table.columnCount() - 1
        model = self.table.model()
        for row in rows:
            selection.select(model.index(row, 0), model.index(row, last))
        self.table.selectionModel().select(
            selection,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )

    def select_all_shown(self) -> None:
        self._select_rows(
            [r for r in range(self.table.rowCount()) if not self.table.isRowHidden(r)]
        )

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
