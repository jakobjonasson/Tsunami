"""Back up and check for drift: a tab per site, or per customer for all its sites
(core/drift.py).

Opening the tab starts the check. Each device is read with its own login; a device
without a backup gets its first, and one whose config differs from its newest backup gets
a new backup and a row that shows the redacted diff when selected. Double-click a row to
open the device's backups. Nothing is sent to a device but `show running-config`.

Device output is untrusted; it reaches this tab only as redacted text.
"""

from __future__ import annotations

import threading
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.drift import (
    READ,
    DriftCheck,
    DriftDevice,
    DriftReport,
    DriftResult,
    DriftStatus,
)
from tsunami.core.issues import TsunamiError
from tsunami.core.secret_store import LoginCredential
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.screens import backups
from tsunami.gui.screens.device_list import local_time
from tsunami.gui.shell import Shell
from tsunami.gui.widgets.diff_view import DiffView

COLUMNS = ["Device", "Site", "Host", "Status", "Changes", "Compared with", "Detail"]
CONNECT_TIMEOUT = 10.0
COMMAND_TIMEOUT = 120.0  # a large running config takes a while
SELECT_TEXT = "Select a changed device to see what changed since its previous backup."

_RESULT_ROLE = Qt.ItemDataRole.UserRole


def open_drift_tab(
    ctx: AppContext, shell: Shell, customer: str, site: str | None = None
) -> QWidget | None:
    """Open (or focus) the drift tab for a site, or for every site of a customer, and
    start a check unless one is running."""
    if ctx.workspace is None or customer not in ctx.workspace.customers:
        shell.notify(f"Unknown customer {customer!r}.", "error")
        return None
    where = f"{customer}/{site}" if site else customer
    tab = shell.open_tab(
        f"drift:{where}",
        f"Drift: {site or customer}",
        lambda: DriftTab(ctx, shell, customer, site),
        "git-compare",
    )
    if isinstance(tab, DriftTab) and not tab.running:
        tab.start()
    return tab


class _Signals(QObject):
    result = Signal(object)  # DriftResult
    finished = Signal(object)  # DriftReport | Exception


class _Task(QRunnable):
    def __init__(self, signals: _Signals, check: DriftCheck, transport: Any) -> None:
        super().__init__()
        self.signals, self.check, self.transport = signals, check, transport

    def run(self) -> None:  # worker thread
        result: Any
        try:
            result = self.check.run()
        except Exception as exc:  # noqa: BLE001 - reported to the person
            result = exc
        finally:
            close = getattr(self.transport, "close", None)
            if callable(close):
                try:
                    close()
                except Exception:  # noqa: BLE001, S110 - closing is best effort
                    pass
        self.signals.finished.emit(result)


class DriftTab(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell, customer: str, site: str | None) -> None:
        super().__init__()
        self.ctx, self.shell, self.customer, self.site = ctx, shell, customer, site
        self.running = False
        self.check: DriftCheck | None = None
        self.report: DriftReport | None = None
        self.results: list[DriftResult] = []
        self._signals = _Signals()
        self._signals.result.connect(self._on_result)
        self._signals.finished.connect(self._on_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        where = f"{customer} / {site}" if site else f"{customer}, all sites"
        title = QLabel(f"Back up and check for drift: {where}")
        title.setObjectName("Title")
        layout.addWidget(title)
        explain = QLabel(
            "Reads each device's running config with its own login. A device without a "
            "backup gets its first. Otherwise the config is compared with its newest backup, "
            "and a new backup is saved when it changed. Comments and lines the device "
            "rewrites on its own are ignored, and secret values are masked, so a changed "
            "password shows as “secrets changed” without the values."
        )
        explain.setObjectName("Muted")
        explain.setWordWrap(True)
        layout.addWidget(explain)

        row = QHBoxLayout()
        self.progress = QLabel("")
        self.progress.setObjectName("Muted")
        self.progress.setWordWrap(True)
        row.addWidget(self.progress, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.clicked.connect(lambda _=False: self.cancel())
        self.start_button = QPushButton("Check again")
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(lambda _=False: self.start())
        row.addWidget(self.cancel_button)
        row.addWidget(self.start_button)
        layout.addLayout(row)

        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme.current().error};")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        self.table.setWordWrap(False)
        self.table.setSortingEnabled(False)
        header = self.table.horizontalHeader()
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(len(COLUMNS) - 1, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._show_selected)
        self.table.itemDoubleClicked.connect(self._open_backups)

        self.diff_caption = QLabel(SELECT_TEXT)
        self.diff_caption.setObjectName("Muted")
        self.diff_view = DiffView()
        diff_box = QWidget()
        diff_layout = QVBoxLayout(diff_box)
        diff_layout.setContentsMargins(0, 0, 0, 0)
        diff_layout.setSpacing(4)
        diff_layout.addWidget(self.diff_caption)
        diff_layout.addWidget(self.diff_view, 1)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        split.addWidget(self.table)
        split.addWidget(diff_box)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        self._refresh()

    # -- the devices -------------------------------------------------------------------

    def _inventory(self) -> Any:
        ws = self.ctx.workspace
        return ws.customers.get(self.customer) if ws is not None else None

    def inventory_devices(self) -> list[Any]:
        inv = self._inventory()
        if inv is None:
            return []
        devices = [d for d in inv.devices if self.site is None or d.site == self.site]
        return sorted(devices, key=lambda d: (d.site, d.name))

    def _plan(self) -> tuple[list[DriftDevice], dict[str, LoginCredential]]:
        inv = self._inventory()
        devices = [
            DriftDevice(d.name, str(d.host), d.platform, inv.credential_for(d), d.port)
            for d in self.inventory_devices()
        ]
        logins: dict[str, LoginCredential] = {}
        for name in sorted({d.login for d in devices}):
            try:
                logins[name] = self.ctx.secrets.get_login(name)
            except TsunamiError as exc:
                if "locked" in str(exc).lower():
                    raise
                # Missing: the devices using it are reported as having no login.
        return devices, logins

    # -- the run -----------------------------------------------------------------------

    def set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def start(self) -> bool:
        if self.running:
            return False
        self.set_error("")
        if self.ctx.workspace is None:
            self.set_error("No workspace is open.")
            return False
        try:
            devices, logins = self._plan()
            if not devices:
                raise TsunamiError(
                    f"{'Site ' + self.site if self.site else self.customer} has no devices."
                )
            redactor = backups.secret_redactor(self.ctx)
            jump = self.ctx.jump_target(self.customer)
        except TsunamiError as exc:
            self.set_error(str(exc))
            self._refresh()
            return False
        self.results.clear()
        self.table.setRowCount(0)
        self.diff_view.set_diff("")
        self.diff_caption.setText(SELECT_TEXT)
        transport = self.ctx.make_transport()
        try:
            self.check = DriftCheck(
                self.ctx.paths,
                self.customer,
                devices,
                logins=logins,
                transport=transport,
                redactor=redactor,
                jump=jump,
                keep=self.ctx.settings.backup_retention,
                on_result=self._signals.result.emit,
                cancel=threading.Event(),
                connect_timeout=CONNECT_TIMEOUT,
                command_timeout=COMMAND_TIMEOUT,
            )
        except TsunamiError as exc:
            self.set_error(str(exc))
            return False
        self.running = True
        self.report = None
        QThreadPool.globalInstance().start(_Task(self._signals, self.check, transport))
        self._refresh()
        return True

    def cancel(self) -> None:
        if self.check is not None:
            self.check.cancel()
        self.cancel_button.setEnabled(False)

    def _on_result(self, result: DriftResult) -> None:
        self.results.append(result)
        self._add_row(result)
        self._refresh()

    def _on_finished(self, result: Any) -> None:
        self.running = False
        if isinstance(result, Exception):
            self.set_error(f"The drift check stopped: {result}")
        else:
            self.report = result
            level = "warning" if result.count(DriftStatus.CHANGED) else "info"
            self.shell.notify(f"{self.site or self.customer}: {result.summary()}", level)
        self._refresh()

    # -- table -------------------------------------------------------------------------

    def _add_row(self, result: DriftResult) -> None:
        palette = theme.current()
        color = {
            DriftStatus.CHANGED: palette.warning,
            DriftStatus.SECRETS: palette.warning,
            DriftStatus.FIRST: palette.success,
            DriftStatus.UNCHANGED: palette.muted,
        }.get(result.status, palette.error)
        inv = self._inventory()
        device = inv.device(result.device) if inv is not None else None
        changes = f"+{result.added} -{result.removed}" if result.diff else ""
        values = [
            result.device,
            device.site if device is not None else "",
            result.host,
            str(result.status),
            changes,
            local_time(result.previous_at) if result.previous_at else "",
            result.detail,
        ]
        row = self.table.rowCount()
        self.table.insertRow(row)
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if col == 0:
                item.setData(_RESULT_ROLE, result)
            if col == COLUMNS.index("Status"):
                item.setForeground(QColor(color))
            self.table.setItem(row, col, item)

    def selected_result(self) -> DriftResult | None:
        rows = {i.row() for i in self.table.selectedIndexes()}
        if len(rows) != 1:
            return None
        item = self.table.item(rows.pop(), 0)
        return item.data(_RESULT_ROLE) if item is not None else None

    def _show_selected(self) -> None:
        result = self.selected_result()
        if result is None:
            self.diff_caption.setText(SELECT_TEXT)
            self.diff_view.set_diff("")
        elif result.diff:
            self.diff_caption.setText(
                f"{result.device}: what changed since the backup of "
                f"{local_time(result.previous_at) if result.previous_at else 'before'} "
                "(+ added, - removed; secrets masked)"
            )
            self.diff_view.set_diff(result.diff)
        else:
            self.diff_caption.setText(f"{result.device}: {result.status}. {result.detail}".strip())
            self.diff_view.set_diff("")

    def _open_backups(self, item: QTableWidgetItem) -> None:
        first = self.table.item(item.row(), 0)
        result = first.data(_RESULT_ROLE) if first is not None else None
        if result is not None:
            backups.open_backups(self.ctx, self.shell, self.customer, result.device)

    # -- state -------------------------------------------------------------------------

    def _refresh(self) -> None:
        self.start_button.setEnabled(not self.running)
        self.cancel_button.setEnabled(
            self.running and self.check is not None and not self.check.cancel_event.is_set()
        )
        total = len(self.inventory_devices())
        if self.running:
            read = sum(1 for r in self.results if r.status in READ)
            self.progress.setText(f"Reading… {len(self.results)} of {total} done, {read} read")
        elif self.report is not None:
            self.progress.setText(self.report.summary())
        else:
            self.progress.setText(f"{total} devices.")

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self.check is not None:
            self.check.cancel()
        super().closeEvent(event)
