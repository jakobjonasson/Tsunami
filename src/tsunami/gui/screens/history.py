"""History view: a compact side bar list of recent jobs, and a full History tab with
labelled filters, the jobs table and compliance history.

See design.md, GUI: Views; Local data and audit.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import PurePosixPath

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import (
    QComboBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.models.jobs import ALARM_DEVICE_STATUSES, JobRecord, JobStatus
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import ActivityItem, Shell

ALL = "All"
HISTORY_TAB_KEY = "history"
RECENT_LIMIT = 30
ALARM_TOOLTIP = "A device may be half-configured (rollback failed or interrupted)"
_JOB_ID_ROLE = Qt.ItemDataRole.UserRole
_ALARM_ROLE = Qt.ItemDataRole.UserRole + 1
JOB_COLUMNS = ["Time", "Actor", "Play", "Customer", "Mode", "Status"]

_STATUS_ICONS = {
    JobStatus.COMPLETED: "check",
    JobStatus.NO_CHANGES: "pass",
    JobStatus.PARTIAL: "warning",
    JobStatus.FAILED: "error",
    JobStatus.INTERRUPTED: "warning",
    JobStatus.RUNNING: "sync",
    JobStatus.PAUSED: "debug-pause",
    JobStatus.AWAITING_APPROVAL: "clock",
    JobStatus.APPROVED: "pass",
    JobStatus.DENIED: "circle-slash",
    JobStatus.EXPIRED: "circle-slash",
    JobStatus.CANCELLED: "circle-slash",
    JobStatus.PENDING: "circle-outline",
}
_RED = {JobStatus.FAILED, JobStatus.INTERRUPTED}
_ORANGE = {JobStatus.PARTIAL}
_GREEN = {JobStatus.COMPLETED}
_ACCENT = {JobStatus.RUNNING, JobStatus.AWAITING_APPROVAL}


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


def relative_time(value: datetime | None, now: datetime | None = None) -> str:
    """A short age such as "just now", "5 min ago", "3 h ago" or "2 d ago"."""
    if value is None:
        return ""
    seconds = int(((now or datetime.now(UTC)) - value).total_seconds())
    if seconds < 60:
        return "just now"
    if seconds < 3600:
        return f"{seconds // 60} min ago"
    if seconds < 86400:
        return f"{seconds // 3600} h ago"
    if seconds < 30 * 86400:
        return f"{seconds // 86400} d ago"
    return value.astimezone().strftime("%Y-%m-%d")


def _status_color(status: JobStatus) -> str:
    colors = theme.current()
    if status in _RED:
        return colors.error
    if status in _ORANGE:
        return colors.warning
    if status in _GREEN:
        return colors.success
    if status in _ACCENT:
        return colors.accent
    return colors.muted


def _alarm_job_ids(ctx: AppContext, jobs: list[JobRecord]) -> set[str]:
    return {
        job.id
        for job in jobs
        if any(d.status in ALARM_DEVICE_STATUSES for d in ctx.history.devices(job.id))
    }


class HistoryView(QWidget):
    """The side bar: the most recent jobs and a button for the full History tab."""

    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell

        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        self.open_button = QPushButton("Open full history")
        self.open_button.setProperty("primary", True)
        self.open_button.setToolTip("Filters, every column and compliance history, in a tab")
        self.open_button.clicked.connect(lambda _=False: open_history(self.ctx, self.shell))
        layout.addWidget(self.open_button)
        self.recent_list = QListWidget()
        self.recent_list.setFrameShape(QListWidget.Shape.NoFrame)
        self.recent_list.setIconSize(QSize(16, 16))
        self.recent_list.setTextElideMode(Qt.TextElideMode.ElideRight)
        self.recent_list.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.recent_list.itemDoubleClicked.connect(self._open_job)
        self.recent_list.itemActivated.connect(self._open_job)
        layout.addWidget(self.recent_list, 1)
        self.empty_label = QLabel("No jobs yet.")
        self.empty_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty_label)

        ctx.jobs_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        colors = theme.current()
        jobs = self.ctx.history.list_jobs(limit=RECENT_LIMIT)
        alarms = _alarm_job_ids(self.ctx, jobs)
        self.recent_list.clear()
        for job in jobs:
            alarm = job.id in alarms
            play = PurePosixPath(job.play_path.replace("\\", "/")).stem or job.play_path
            when = relative_time(job.created_at)
            status = str(job.status).replace("_", " ")
            item = QListWidgetItem(f"{play}\n{job.customer} · {status} · {when}")
            item.setData(_JOB_ID_ROLE, job.id)
            item.setData(_ALARM_ROLE, alarm)
            color = colors.error if alarm else _status_color(job.status)
            icon_name = "error" if alarm else _STATUS_ICONS.get(job.status, "circle-outline")
            item.setIcon(icon(icon_name, color))
            tip = (
                f"{job.play_path}\n{job.customer}, {job.mode}, {job.status}, {job.actor}\n"
                f"{_local_time(job.created_at)}"
            )
            item.setToolTip(f"{tip}\n{ALARM_TOOLTIP}" if alarm else tip)
            if alarm:
                item.setForeground(QBrush(QColor(colors.error)))
            self.recent_list.addItem(item)
        self.empty_label.setVisible(not jobs)
        self.recent_list.setVisible(bool(jobs))

    def _open_job(self, item: QListWidgetItem) -> None:
        self.shell.open_job(item.data(_JOB_ID_ROLE))


class HistoryTab(QWidget):
    """The full History tab: labelled filters, every job column, and compliance history."""

    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self._jobs: list[JobRecord] = []
        self._alarms: set[str] = set()

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.tabs = QTabWidget()
        self.tabs.setDocumentMode(True)
        layout.addWidget(self.tabs)

        jobs_page = QWidget()
        jobs_layout = QVBoxLayout(jobs_page)
        jobs_layout.setContentsMargins(8, 8, 8, 8)
        filters = QHBoxLayout()
        self.customer_filter = self._combo("Customer")
        self.play_filter = self._combo("Play")
        self.actor_filter = self._combo("Actor")
        self.status_filter = self._combo("Status")
        self.filter_labels: list[QLabel] = []
        for text, combo in (
            ("Customer", self.customer_filter),
            ("Play", self.play_filter),
            ("Actor", self.actor_filter),
            ("Status", self.status_filter),
        ):
            label = QLabel(text)
            label.setBuddy(combo)
            self.filter_labels.append(label)
            filters.addWidget(label)
            filters.addWidget(combo, 1)
            combo.currentIndexChanged.connect(self._render_jobs)
        self.refresh_button = QPushButton(icon("refresh", theme.current().text), "Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        filters.addWidget(self.refresh_button)
        jobs_layout.addLayout(filters)
        self.jobs_table = _table(JOB_COLUMNS)
        self.jobs_table.itemDoubleClicked.connect(self._open_job)
        jobs_layout.addWidget(self.jobs_table, 1)
        self.tabs.addTab(jobs_page, "Jobs")

        comp_page = QWidget()
        comp_layout = QVBoxLayout(comp_page)
        comp_layout.setContentsMargins(8, 8, 8, 8)
        pick = QHBoxLayout()
        pick_label = QLabel("Customer")
        self.compliance_customer = wheel.ComboBox()
        pick_label.setBuddy(self.compliance_customer)
        self.compliance_customer.currentIndexChanged.connect(self._render_compliance)
        pick.addWidget(pick_label)
        pick.addWidget(self.compliance_customer, 1)
        pick.addStretch(1)
        comp_layout.addLayout(pick)
        self.compliance_table = _table(["Time", "Device", "Missing", "Extra"])
        comp_layout.addWidget(self.compliance_table, 1)
        self.tabs.addTab(comp_page, "Compliance")

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
        self._alarms = _alarm_job_ids(self.ctx, self._jobs)
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
                        item.setToolTip(ALARM_TOOLTIP)
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


def open_history(ctx: AppContext, shell: Shell) -> None:
    """Open (or focus) the one History tab."""
    shell.open_tab(HISTORY_TAB_KEY, "History", lambda: HistoryTab(ctx, shell), "history")


def factory(ctx: AppContext, shell: Shell) -> QWidget:
    return HistoryView(ctx, shell)


def register(shell: Shell) -> None:
    shell.register_command(
        "history.open", "History: Open full history", lambda: open_history(shell.ctx, shell)
    )


VIEW = ActivityItem("history", "History", "history", factory, order=60)
