"""History view: past jobs with filters and alarm marking, plus compliance history.

See design.md, GUI: Views; Local data and audit.
"""

from __future__ import annotations

from datetime import datetime

from PySide6.QtCore import Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.models.jobs import ALARM_DEVICE_STATUSES, JobRecord
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import ActivityItem, Shell

ALL = "All"
_JOB_ID_ROLE = Qt.ItemDataRole.UserRole
_ALARM_ROLE = Qt.ItemDataRole.UserRole + 1
JOB_COLUMNS = ["Time", "Actor", "Play", "Customer", "Mode", "Status"]


def _table(columns: list[str]) -> QTableWidget:
    table = QTableWidget(0, len(columns))
    table.setHorizontalHeaderLabels(columns)
    table.setFrameShape(QTableWidget.Shape.NoFrame)
    table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
    table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
    table.setSelectionMode(QTableWidget.SelectionMode.SingleSelection)
    table.verticalHeader().setVisible(False)
    table.setShowGrid(False)
    table.horizontalHeader().setStretchLastSection(True)
    table.horizontalHeader().setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
    return table


def _local_time(value: datetime | None) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S") if value else ""


class HistoryView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self._jobs: list[JobRecord] = []
        self._alarms: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        tabs = QTabWidget()
        tabs.setDocumentMode(True)
        layout.addWidget(tabs)

        jobs_page = QWidget()
        jobs_layout = QVBoxLayout(jobs_page)
        jobs_layout.setContentsMargins(8, 8, 8, 8)
        filters = QHBoxLayout()
        self.customer_filter = self._combo("Customer")
        self.play_filter = self._combo("Play")
        self.actor_filter = self._combo("Actor")
        self.status_filter = self._combo("Status")
        for combo in (
            self.customer_filter,
            self.play_filter,
            self.actor_filter,
            self.status_filter,
        ):
            filters.addWidget(combo, 1)
            combo.currentIndexChanged.connect(self._render_jobs)
        self.refresh_button = QPushButton(icon("refresh", theme.current().text), "Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        filters.addWidget(self.refresh_button)
        jobs_layout.addLayout(filters)
        self.jobs_table = _table(JOB_COLUMNS)
        self.jobs_table.itemDoubleClicked.connect(self._open_job)
        jobs_layout.addWidget(self.jobs_table, 1)
        tabs.addTab(jobs_page, "Jobs")

        comp_page = QWidget()
        comp_layout = QVBoxLayout(comp_page)
        comp_layout.setContentsMargins(8, 8, 8, 8)
        self.compliance_customer = wheel.ComboBox()
        self.compliance_customer.currentIndexChanged.connect(self._render_compliance)
        comp_layout.addWidget(self.compliance_customer)
        self.compliance_table = _table(["Time", "Device", "Missing", "Extra"])
        comp_layout.addWidget(self.compliance_table, 1)
        tabs.addTab(comp_page, "Compliance")

        ctx.jobs_changed.connect(self.refresh)
        ctx.workspace_changed.connect(self._reload_customers)
        self._reload_customers()
        self.refresh()

    @staticmethod
    def _combo(tooltip: str) -> QComboBox:
        combo = wheel.ComboBox()
        combo.addItem(ALL)
        combo.setToolTip(tooltip)
        return combo

    # -- jobs -------------------------------------------------------------------

    def refresh(self) -> None:
        self._jobs = self.ctx.history.list_jobs(limit=500)
        self._alarms = {
            job.id
            for job in self._jobs
            if any(d.status in ALARM_DEVICE_STATUSES for d in self.ctx.history.devices(job.id))
        }
        for combo, values in (
            (self.customer_filter, {j.customer for j in self._jobs}),
            (self.play_filter, {j.play_path for j in self._jobs}),
            (self.actor_filter, {str(j.actor) for j in self._jobs}),
            (self.status_filter, {str(j.status) for j in self._jobs}),
        ):
            self._fill_combo(combo, [ALL, *sorted(values)])
        self._render_jobs()
        self._render_compliance()

    @staticmethod
    def _fill_combo(combo: QComboBox, values: list[str]) -> None:
        current = combo.currentText()
        combo.blockSignals(True)
        combo.clear()
        combo.addItems(values)
        combo.setCurrentIndex(max(combo.findText(current), 0))
        combo.blockSignals(False)

    def _render_jobs(self) -> None:
        colors = theme.current()
        wanted = [
            (self.customer_filter.currentText(), lambda j: j.customer),
            (self.play_filter.currentText(), lambda j: j.play_path),
            (self.actor_filter.currentText(), lambda j: str(j.actor)),
            (self.status_filter.currentText(), lambda j: str(j.status)),
        ]
        rows = [j for j in self._jobs if all(w == ALL or get(j) == w for w, get in wanted)]
        self.jobs_table.setRowCount(len(rows))
        for r, job in enumerate(rows):
            alarm = job.id in self._alarms
            cells = [
                _local_time(job.created_at),
                str(job.actor),
                job.play_path,
                job.customer,
                job.mode,
                str(job.status),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(_JOB_ID_ROLE, job.id)
                item.setData(_ALARM_ROLE, alarm)
                if alarm:
                    item.setForeground(QBrush(QColor(colors.error)))
                    if c == 0:
                        item.setIcon(icon("error", colors.error))
                        item.setToolTip(
                            "A device may be half-configured (rollback failed or interrupted)"
                        )
                self.jobs_table.setItem(r, c, item)

    def _open_job(self, item: QTableWidgetItem) -> None:
        self.shell.open_job(item.data(_JOB_ID_ROLE))

    # -- compliance -------------------------------------------------------------

    def _reload_customers(self) -> None:
        ws = self.ctx.workspace
        self._fill_combo(self.compliance_customer, sorted(ws.customers) if ws else [])
        self._render_compliance()

    def _render_compliance(self) -> None:
        customer = self.compliance_customer.currentText()
        results = self.ctx.history.compliance_history(customer) if customer else []
        self.compliance_table.setRowCount(len(results))
        for r, row in enumerate(results):
            cells = [
                _local_time(row["created_at"]),
                row["device"],
                str(row["missing_count"]),
                str(row["extra_count"]),
            ]
            for c, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(_JOB_ID_ROLE, row["job_id"])
                self.compliance_table.setItem(r, c, item)


def factory(ctx: AppContext, shell: Shell) -> QWidget:
    return HistoryView(ctx, shell)


VIEW = ActivityItem("history", "History", "history", factory, order=60)
