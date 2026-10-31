"""Discover a site's devices over CDP and LLDP, or check it for unlearned neighbors
(core/discovery.py).

A tab per site. Discover: seeds (addresses typed in, and/or the site's own devices), a
scope of networks to follow neighbors into, the type for new devices, the login, and hop
and device limits; Start logs in to each device, adds it to the site and follows its new
neighbors. Check for unlearned neighbors: reads the site's devices and lists neighbors
the inventory lacks, adding nothing. Both list every neighbor that was not learned, with
the reason.

Device output is untrusted; it reaches this tab only as table text.
"""

from __future__ import annotations

import threading
from collections import Counter
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QFormLayout,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.discovery import (
    MAX_DEVICES,
    DeviceVisit,
    Discovery,
    DiscoveryReport,
    DiscoveryRequest,
    Seed,
    Unlearned,
    VisitStatus,
    default_scope,
    parse_scope,
)
from tsunami.core.issues import TsunamiError
from tsunami.core.models.common import Platform
from tsunami.core.models.inventory import DEVICE_TYPES
from tsunami.core.transports.base import JumpTarget
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.shell import Shell

DEVICE_COLUMNS = ["Device", "Host", "Platform", "Model", "Version", "Status", "Detail"]
UNLEARNED_COLUMNS = ["Neighbor", "Address", "Seen from", "Ports", "Reason", "Detail"]
CONNECT_TIMEOUT = 10.0
COMMAND_TIMEOUT = 60.0


def open_discovery_tab(
    ctx: AppContext, shell: Shell, customer: str, site: str, *, check_only: bool = False
) -> QWidget | None:
    if ctx.workspace is None or customer not in ctx.workspace.customers:
        shell.notify(f"Unknown customer {customer!r}.", "error")
        return None
    kind = "neighbors" if check_only else "discover"
    title = f"Neighbors: {site}" if check_only else f"Discover: {site}"
    return shell.open_tab(
        f"{kind}:{customer}/{site}",
        title,
        lambda: DiscoveryTab(ctx, shell, customer, site, check_only=check_only),
        "search" if check_only else "type-hierarchy-sub",
    )


class _Signals(QObject):
    event = Signal(object)  # DeviceVisit | Unlearned
    finished = Signal(object)  # DiscoveryReport | Exception


class _Task(QRunnable):
    def __init__(self, signals: _Signals, discovery: Discovery, transport: Any) -> None:
        super().__init__()
        self.signals, self.discovery, self.transport = signals, discovery, transport

    def run(self) -> None:  # worker thread
        result: Any
        try:
            result = self.discovery.run()
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


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.verticalHeader().setVisible(False)
    table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
    table.setShowGrid(False)
    table.setWordWrap(False)
    header = table.horizontalHeader()
    for col in range(len(columns)):
        header.setSectionResizeMode(col, QHeaderView.ResizeMode.ResizeToContents)
    header.setSectionResizeMode(len(columns) - 1, QHeaderView.ResizeMode.Stretch)
    return table


def _section(text: str) -> QLabel:
    label = QLabel(text)
    label.setProperty("section", True)
    font = label.font()
    font.setBold(True)
    label.setFont(font)
    return label


class DiscoveryTab(QWidget):
    def __init__(
        self, ctx: AppContext, shell: Shell, customer: str, site: str, *, check_only: bool
    ) -> None:
        super().__init__()
        self.ctx, self.shell, self.customer, self.site = ctx, shell, customer, site
        self.check_only = check_only
        self.running = False
        self.discovery: Discovery | None = None
        self.report: DiscoveryReport | None = None
        self.visits: list[DeviceVisit] = []
        self.unlearned: list[Unlearned] = []
        self._signals = _Signals()
        self._signals.event.connect(self._on_event)
        self._signals.finished.connect(self._on_finished)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)
        where = f"{customer} / {site}"
        title = QLabel(f"{'Unlearned neighbors' if check_only else 'Discover devices'}: {where}")
        title.setObjectName("Title")
        layout.addWidget(title)
        explain = QLabel(
            "Logs in to each of this site's devices and lists CDP and LLDP neighbors that are "
            "not in the inventory. Nothing is added."
            if check_only
            else "Logs in to each seed, adds it to this site, and follows its CDP and LLDP "
            "neighbors inside the scope until no new device is left. Neighbors that are not "
            "followed are listed with the reason. New host keys are recorded, as in a run."
        )
        explain.setObjectName("Muted")
        explain.setWordWrap(True)
        layout.addWidget(explain)

        form = QFormLayout()
        form.setHorizontalSpacing(16)
        self.seeds_edit = QLineEdit()
        self.seeds_edit.setPlaceholderText("e.g. 192.0.2.10, 192.0.2.11")
        self.seeds_edit.textChanged.connect(lambda _="": self._seeds_changed())
        self.platform_combo = wheel.ComboBox()
        for platform in Platform:
            self.platform_combo.addItem(platform.value, platform)
        seeds_row = QHBoxLayout()
        seeds_row.addWidget(self.seeds_edit, 1)
        seeds_row.addWidget(QLabel("platform"))
        seeds_row.addWidget(self.platform_combo)
        self.site_devices_check = QCheckBox("Also start from this site's devices")
        self.site_devices_check.toggled.connect(lambda _=False: self._seeds_changed())
        self.scope_edit = QLineEdit()
        self.scope_edit.setPlaceholderText("networks to follow neighbors into, e.g. 192.0.2.0/24")
        self.scope_edit.textEdited.connect(lambda _="": setattr(self, "_scope_edited", True))
        self._scope_edited = False
        self.type_combo = wheel.ComboBox()
        self.login_combo = wheel.ComboBox()
        limits = QHBoxLayout()
        self.hops_spin = wheel.SpinBox()
        self.hops_spin.setRange(1, 20)
        self.hops_spin.setValue(5)
        self.max_spin = wheel.SpinBox()
        self.max_spin.setRange(1, MAX_DEVICES)
        self.max_spin.setValue(MAX_DEVICES)
        limits.addWidget(QLabel("Hops"))
        limits.addWidget(self.hops_spin)
        limits.addSpacing(16)
        limits.addWidget(QLabel("Devices"))
        limits.addWidget(self.max_spin)
        limits.addStretch(1)
        if not check_only:
            form.addRow("Seeds", seeds_row)
            form.addRow("", self.site_devices_check)
            form.addRow("Scope", self.scope_edit)
            form.addRow("Type of new devices", self.type_combo)
        form.addRow("Login", self.login_combo)
        if not check_only:
            form.addRow("Limits", limits)
        layout.addLayout(form)

        row = QHBoxLayout()
        self.progress = QLabel("")
        self.progress.setObjectName("Muted")
        self.progress.setWordWrap(True)
        row.addWidget(self.progress, 1)
        self.cancel_button = QPushButton("Cancel")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(lambda _=False: self.cancel())
        self.start_button = QPushButton("Check" if check_only else "Start")
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

        self.devices_table = _table(DEVICE_COLUMNS)
        self.unlearned_table = _table(UNLEARNED_COLUMNS)
        split = QSplitter(Qt.Orientation.Vertical)
        split.setChildrenCollapsible(False)
        for label, table in (
            ("Devices", self.devices_table),
            ("Unlearned neighbors", self.unlearned_table),
        ):
            box = QWidget()
            box_layout = QVBoxLayout(box)
            box_layout.setContentsMargins(0, 0, 0, 0)
            box_layout.setSpacing(4)
            box_layout.addWidget(_section(label))
            box_layout.addWidget(table, 1)
            split.addWidget(box)
        layout.addWidget(split, 1)

        self._fill_choices()
        self._refresh()

    # -- choices -----------------------------------------------------------------------

    def _inventory(self) -> Any:
        ws = self.ctx.workspace
        return ws.customers.get(self.customer) if ws is not None else None

    def site_devices(self) -> list[Any]:
        inv = self._inventory()
        return [d for d in inv.devices if d.site == self.site] if inv is not None else []

    def default_login(self) -> str:
        inv = self._inventory()
        if inv is None:
            return ""
        return inv.config.site_credentials.get(self.site, inv.config.credential)

    def _fill_choices(self) -> None:
        devices = self.site_devices()
        ws = self.ctx.workspace
        roles = sorted(set(ws.roles) - set(DEVICE_TYPES)) if ws is not None else []
        for slug, label in DEVICE_TYPES.items():
            self.type_combo.addItem(label, slug)
        for role in roles:
            self.type_combo.addItem(role, role)
        self.type_combo.setCurrentIndex(max(0, self.type_combo.findData("access")))
        default = self.default_login()
        names = [default] if default else []
        try:
            names += sorted(n for n in self.ctx.secrets.list_logins() if n != default)
        except TsunamiError:
            pass  # locked: only the default is offered
        for name in names:
            label = f"{name} (site default)" if name == default else name
            self.login_combo.addItem(label, name)
        platforms = Counter(d.platform for d in devices)
        if platforms:
            common = platforms.most_common(1)[0][0]
            self.platform_combo.setCurrentIndex(self.platform_combo.findData(common))
        else:
            fallback = ws.config.defaults.platform if ws is not None and ws.config else None
            index = self.platform_combo.findData(fallback) if fallback else -1
            self.platform_combo.setCurrentIndex(max(0, index))
        self.site_devices_check.setChecked(bool(devices))
        self.site_devices_check.setEnabled(bool(devices))
        if not devices:
            self.site_devices_check.setText("Also start from this site's devices (it has none)")
        self._seeds_changed()

    def _seeds_changed(self) -> None:
        """Keep the scope at the seeds' /24s until the person edits it."""
        if self._scope_edited:
            return
        hosts = [h for h in self.seeds_edit.text().replace(",", " ").split() if h]
        if self.site_devices_check.isChecked():
            hosts += [str(d.host) for d in self.site_devices()]
        self.scope_edit.setText(", ".join(str(n) for n in default_scope(hosts)))

    # -- the run -----------------------------------------------------------------------

    def set_error(self, text: str) -> None:
        self.error_label.setText(text)
        self.error_label.setVisible(bool(text))

    def build_request(self) -> DiscoveryRequest:
        """The request the form describes. Raises TsunamiError naming what is wrong."""
        login = self.login_combo.currentData() or ""
        default = self.default_login()
        site_seeds = [Seed(str(d.host), d.platform, d.name) for d in self.site_devices()]
        if self.check_only:
            if not site_seeds:
                raise TsunamiError(f"Site {self.site} has no devices to check.")
            return DiscoveryRequest(
                self.customer,
                self.site,
                tuple(site_seeds),
                add=False,
                device_credential=None if login == default else login,
            )
        platform = self.platform_combo.currentData()
        typed = [h for h in self.seeds_edit.text().replace(",", " ").split() if h]
        seeds = [Seed(h, platform) for h in typed]
        if self.site_devices_check.isChecked():
            seeds += site_seeds
        if not seeds:
            raise TsunamiError("Give at least one seed address, or start from the site's devices.")
        scope = parse_scope(self.scope_edit.text())
        if not scope:
            raise TsunamiError("Give the scope: the networks discovery may follow neighbors into.")
        return DiscoveryRequest(
            self.customer,
            self.site,
            tuple(seeds),
            scope,
            role=self.type_combo.currentData() or "access",
            max_hops=self.hops_spin.value(),
            max_devices=self.max_spin.value(),
            device_credential=None if login == default else login,
        )

    def _jump(self) -> JumpTarget | None:
        inv = self._inventory()
        jh = inv.config.jump_host if inv is not None else None
        if jh is None:
            return None
        return JumpTarget(str(jh.host), jh.port, self.ctx.secrets.get_login(jh.credential))

    def start(self) -> bool:
        if self.running or self.ctx.workspace_path is None:
            return False
        self.set_error("")
        try:
            request = self.build_request()
            login_name = self.login_combo.currentData() or ""
            login = self.ctx.secrets.get_login(login_name)
            jump = self._jump()
        except TsunamiError as exc:
            self.set_error(str(exc))
            return False
        self.visits.clear()
        self.unlearned.clear()
        self.devices_table.setRowCount(0)
        self.unlearned_table.setRowCount(0)
        transport = self.ctx.make_transport()
        try:
            self.discovery = Discovery(
                self.ctx.workspace_path,
                request,
                login=login,
                transport=transport,
                jump=jump,
                history=self.ctx.history,
                on_event=self._signals.event.emit,
                cancel=threading.Event(),
                connect_timeout=CONNECT_TIMEOUT,
                command_timeout=COMMAND_TIMEOUT,
            )
        except TsunamiError as exc:
            self.set_error(str(exc))
            return False
        self.running = True
        self.report = None
        QThreadPool.globalInstance().start(_Task(self._signals, self.discovery, transport))
        self._refresh()
        return True

    def cancel(self) -> None:
        if self.discovery is not None:
            self.discovery.cancel()
        self.cancel_button.setEnabled(False)

    def _on_event(self, event: DeviceVisit | Unlearned) -> None:
        if isinstance(event, DeviceVisit):
            self.visits.append(event)
            self._add_visit(event)
        else:
            self.unlearned.append(event)
            self._add_unlearned(event)
        self._refresh()

    def _on_finished(self, result: Any) -> None:
        self.running = False
        if isinstance(result, Exception):
            self.set_error(f"Discovery stopped: {result}")
        else:
            self.report = result
            self.shell.notify(f"{self.site}: {result.summary()}")
            if result.added:
                self.ctx.reload_workspace()
        self._refresh()

    # -- tables ------------------------------------------------------------------------

    def _add_visit(self, visit: DeviceVisit) -> None:
        palette = theme.current()
        color = {
            VisitStatus.ADDED: palette.success,
            VisitStatus.KNOWN: palette.muted,
        }.get(visit.status, palette.error)
        values = [
            visit.name or "",
            visit.host,
            visit.platform.value if visit.platform else "",
            visit.model or "",
            visit.version or "",
            str(visit.status),
            visit.detail,
        ]
        self._append(self.devices_table, values, status_col=5, color=color)

    def _add_unlearned(self, item: Unlearned) -> None:
        ports = " → ".join(p for p in (item.local_port, item.remote_port) if p)
        detail = item.detail or item.description
        values = [item.name, item.address or "", item.seen_from, ports, str(item.reason), detail]
        self._append(self.unlearned_table, values, status_col=4, color=theme.current().warning)

    def _append(self, table: QTableWidget, values: list[str], *, status_col: int, color: str):
        row = table.rowCount()
        table.insertRow(row)
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            item.setToolTip(text)
            if col == status_col:
                item.setForeground(QColor(color))
            table.setItem(row, col, item)

    # -- state -------------------------------------------------------------------------

    def _refresh(self) -> None:
        idle = not self.running
        for widget in (
            self.seeds_edit,
            self.platform_combo,
            self.scope_edit,
            self.type_combo,
            self.login_combo,
            self.hops_spin,
            self.max_spin,
        ):
            widget.setEnabled(idle)
        self.site_devices_check.setEnabled(idle and bool(self.site_devices()))
        self.start_button.setEnabled(idle)
        self.cancel_button.setEnabled(
            self.running and self.discovery is not None and not self.discovery.cancel_event.is_set()
        )
        added = sum(1 for v in self.visits if v.status == VisitStatus.ADDED)
        if self.running:
            self.progress.setText(
                f"Reading… {len(self.visits)} devices read, {added} added, "
                f"{len(self.unlearned)} unlearned neighbors"
            )
        elif self.report is not None:
            self.progress.setText(self.report.summary())
        elif self.check_only:
            self.progress.setText(f"{len(self.site_devices())} devices at this site.")
        else:
            self.progress.setText("Start logs in to the seeds and follows their neighbors.")

    def closeEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        if self.discovery is not None:
            self.discovery.cancel()
        super().closeEvent(event)
