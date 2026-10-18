"""Trust host keys for a customer or a site, in bulk (core/hostkey_trust.py).

A tab per customer (or site): Start reads every device's SSH host key without logging
in, through the customer's jump host if it has one. New keys are recorded, known keys
are left alone, and a key that changed is listed with both fingerprints. Only "Accept
new key…", after a confirmation that shows both, replaces a changed key.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.hostkey_trust import (
    DeviceAddress,
    KeyProbe,
    Outcome,
    accept_changed,
    trust_host_keys,
)
from tsunami.core.issues import TsunamiError
from tsunami.core.transports.base import JumpTarget
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.shell import Shell

COLUMNS = ["Device", "Host", "Result", "Fingerprint", "Detail"]
CONNECT_TIMEOUT = 10.0


def open_trust_tab(
    ctx: AppContext, shell: Shell, customer: str, site: str | None = None
) -> QWidget | None:
    if ctx.workspace is None or customer not in ctx.workspace.customers:
        shell.notify(f"Unknown customer {customer!r}.", "error")
        return None
    where = f"{customer}/{site}" if site else customer
    return shell.open_tab(
        f"hostkeys:{where}",
        f"Host keys: {where}",
        lambda: HostKeysTab(ctx, shell, customer, site),
        "key",
    )


class _Signals(QObject):
    result = Signal(object)  # KeyProbe
    finished = Signal()


class _Task(QRunnable):
    def __init__(self, signals: _Signals, run: Any) -> None:
        super().__init__()
        self.signals = signals
        self.run_probe = run

    def run(self) -> None:  # worker thread
        try:
            self.run_probe(self.signals.result.emit)
        finally:
            self.signals.finished.emit()


class HostKeysTab(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell, customer: str, site: str | None) -> None:
        super().__init__()
        self.ctx, self.shell, self.customer, self.site = ctx, shell, customer, site
        self.results: dict[str, KeyProbe] = {}
        self.running = False
        self._cancel = threading.Event()
        self._signals = _Signals()
        self._signals.result.connect(self._on_result)
        self._signals.finished.connect(self._on_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        where = f"{customer} / {site}" if site else customer
        title = QLabel(f"Trust host keys: {where}")
        title.setObjectName("Title")
        layout.addWidget(title)
        explain = QLabel(
            "Reads each device's SSH host key without logging in to it. New keys are "
            "recorded; a key that changed is listed here and replaced only if you accept it."
        )
        explain.setObjectName("Muted")
        explain.setWordWrap(True)
        layout.addWidget(explain)

        row = QHBoxLayout()
        self.progress = QLabel("")
        self.progress.setObjectName("Muted")
        row.addWidget(self.progress, 1)
        self.accept_button = QPushButton("Accept new key…")
        self.accept_button.setEnabled(False)
        self.accept_button.clicked.connect(self.accept_selected)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel)
        self.start_button = QPushButton("Start")
        self.start_button.setProperty("primary", True)
        self.start_button.clicked.connect(self.start)
        for button in (self.accept_button, self.cancel_button, self.start_button):
            row.addWidget(button)
        layout.addLayout(row)

        self.table = QTableWidget(0, len(COLUMNS))
        self.table.setHorizontalHeaderLabels(COLUMNS)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        for col in range(len(COLUMNS)):
            header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(4, QHeaderView.ResizeMode.Stretch)
        self.table.itemSelectionChanged.connect(self._refresh_buttons)
        layout.addWidget(self.table, 1)

        self.devices = self._devices()
        self._rows = {d.name: i for i, d in enumerate(self.devices)}
        self.table.setRowCount(len(self.devices))
        for i, device in enumerate(self.devices):
            for col, text in enumerate([device.name, device.host, "", "", ""]):
                self.table.setItem(i, col, QTableWidgetItem(text))
        self._refresh_buttons()
        self._refresh_progress()

    # -- the run -----------------------------------------------------------------------

    def _devices(self) -> list[DeviceAddress]:
        ws = self.ctx.workspace
        inv = ws.customers.get(self.customer) if ws else None
        if inv is None:
            return []
        return [
            DeviceAddress(d.name, str(d.host), d.port)
            for d in inv.devices
            if self.site is None or d.site == self.site
        ]

    def _jump(self) -> JumpTarget | None:
        ws = self.ctx.workspace
        inv = ws.customers.get(self.customer) if ws else None
        jh = inv.config.jump_host if inv else None
        if jh is None:
            return None
        login = self.ctx.secrets.get_login(jh.credential)  # TsunamiError if locked/missing
        return JumpTarget(str(jh.host), jh.port, login)

    def start(self) -> bool:
        if self.running or not self.devices:
            return False
        try:
            jump = self._jump()
        except TsunamiError as exc:
            self.shell.notify(f"The jump host's login is needed: {exc}", "error")
            return False
        self.results.clear()
        for i in range(self.table.rowCount()):
            for col in (2, 3, 4):
                self.table.item(i, col).setText("")
        self._cancel = threading.Event()
        self.running = True
        legacy = getattr(self.ctx.settings, "ssh_legacy_algorithms", True)
        paths, customer, devices, cancel = (
            self.ctx.paths,
            self.customer,
            list(self.devices),
            self._cancel,
        )

        def run(emit: Any) -> None:
            trust_host_keys(
                paths,
                customer,
                devices,
                jump=jump,
                connect_timeout=CONNECT_TIMEOUT,
                legacy=legacy,
                cancel=cancel,
                on_result=emit,
            )

        QThreadPool.globalInstance().start(_Task(self._signals, run))
        self._refresh_buttons()
        self._refresh_progress()
        return True

    def cancel(self) -> None:
        self._cancel.set()
        self.cancel_button.setEnabled(False)

    def _on_result(self, result: KeyProbe) -> None:
        self.results[result.device] = result
        row = self._rows.get(result.device)
        if row is not None:
            self._show(row, result)
        self._refresh_progress()

    def _on_finished(self) -> None:
        self.running = False
        self._refresh_buttons()
        self._refresh_progress()
        counts = self.counts()
        self.shell.notify(
            f"Host keys for {self.customer}: {counts.get('recorded', 0)} recorded, "
            f"{counts.get('known', 0)} known, {counts.get('changed', 0)} changed, "
            f"{counts.get('unreachable', 0)} unreachable.",
            "info",
        )

    def _show(self, row: int, result: KeyProbe) -> None:
        palette = theme.current()
        color = {
            Outcome.RECORDED: palette.success,
            Outcome.KNOWN: palette.muted,
            Outcome.CHANGED: palette.error,
            Outcome.UNREACHABLE: palette.warning,
            Outcome.CANCELLED: palette.muted,
        }[result.outcome]
        status = self.table.item(row, 2)
        status.setText(str(result.outcome))
        status.setForeground(QColor(color))
        if result.outcome == Outcome.CHANGED:
            fingerprints = f"now {result.fingerprint}  (on file {result.known_fingerprint})"
        else:
            fingerprints = result.fingerprint or ""
        self.table.item(row, 3).setText(fingerprints)
        self.table.item(row, 4).setText(result.detail)

    # -- changed keys ------------------------------------------------------------------

    def selected(self) -> KeyProbe | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        return self.results.get(self.table.item(row, 0).text())

    def accept_selected(self) -> bool:
        result = self.selected()
        if result is None or result.outcome != Outcome.CHANGED:
            return False
        answer = QMessageBox.question(
            self,
            "Accept new host key",
            f"{result.device} ({result.host}) presented a different host key.\n\n"
            f"On file:   {result.known_fingerprint}\n"
            f"Presented: {result.fingerprint}\n\n"
            "Accept it only if you know why it changed (the device was replaced or "
            "reinstalled). A key that changes unexpectedly can mean someone is "
            "intercepting the connection. Replace the key on file?",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return False
        accept_changed(self.ctx.paths, self.customer, result)
        accepted = KeyProbe(
            result.device,
            result.host,
            result.port,
            Outcome.RECORDED,
            fingerprint=result.fingerprint,
            detail="new key accepted",
        )
        self._on_result(accepted)
        self._refresh_buttons()
        return True

    # -- state -------------------------------------------------------------------------

    def counts(self) -> Counter[str]:
        return Counter(str(r.outcome) for r in self.results.values())

    def _refresh_buttons(self) -> None:
        self.start_button.setEnabled(not self.running and bool(self.devices))
        self.cancel_button.setEnabled(self.running and not self._cancel.is_set())
        selected = self.selected()
        self.accept_button.setEnabled(
            not self.running and selected is not None and selected.outcome == Outcome.CHANGED
        )

    def _refresh_progress(self) -> None:
        total = len(self.devices)
        if not total:
            self.progress.setText("No devices here.")
            return
        done = len(self.results)
        counts = self.counts()
        parts = [f"{n} {name}" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        if not self.running and not done:
            self.progress.setText(f"{total} devices. Start reads their host keys.")
        else:
            state = "" if not self.running else "Reading… "
            summary = f": {', '.join(parts)}" if parts else ""
            self.progress.setText(f"{state}{done} of {total}{summary}")

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self._cancel.set()
        super().closeEvent(event)
