"""Job view: per-device status and live redacted log, canary dialog, Cancel.

One class serves both a running job (events from a RunnerThread, throttled to 100 ms
batches) and a past job (rows loaded from history.db). Everything shown is already
redacted or masked: event text by the runner, history text by the pipeline.

A device that may be half-configured (rollback_failed, interrupted) raises a red banner
that only the Acknowledge button dismisses.

See design.md, GUI: Views (Job view), Behavior rules; Execution engine: Device level.
"""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMessageBox,
    QPushButton,
    QSplitter,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.events import (
    BatchFinished,
    CanaryWaiting,
    DeviceLog,
    DeviceStatusChanged,
    JobFinished,
    JobStarted,
    MaxFailReached,
)
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import (
    ALARM_DEVICE_STATUSES,
    TERMINAL_DEVICE_STATUSES,
    DeviceRecord,
    DeviceStatus,
    JobRecord,
)
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import Shell
from tsunami.gui.widgets.diff_view import DiffView
from tsunami.gui.widgets.masked_text import MaskedTextView

_ICONS = {
    DeviceStatus.PENDING: "circle-outline",
    DeviceStatus.CONNECTING: "sync",
    DeviceStatus.RUNNING: "sync",
    DeviceStatus.SUCCESS: "check",
    DeviceStatus.FAILED: "error",
    DeviceStatus.ROLLED_BACK: "debug-restart",
    DeviceStatus.ROLLBACK_FAILED: "warning",
    DeviceStatus.UNREACHABLE: "debug-disconnect",
    DeviceStatus.HOSTKEY_MISMATCH: "warning",
    DeviceStatus.RENDER_CHANGED: "warning",
    DeviceStatus.INTERRUPTED: "warning",
    DeviceStatus.SKIPPED_PRECHECK: "circle-slash",
    DeviceStatus.SKIPPED_MAXFAIL: "circle-slash",
    DeviceStatus.DEVICE_BUSY: "circle-slash",
    DeviceStatus.CANCELLED: "circle-slash",
}
_RED = {
    DeviceStatus.FAILED,
    DeviceStatus.ROLLBACK_FAILED,
    DeviceStatus.HOSTKEY_MISMATCH,
    DeviceStatus.RENDER_CHANGED,
    DeviceStatus.INTERRUPTED,
}
_ORANGE = {DeviceStatus.ROLLED_BACK, DeviceStatus.UNREACHABLE}
_ACCENT = {DeviceStatus.RUNNING, DeviceStatus.CONNECTING}
_MUTED = {
    DeviceStatus.PENDING,
    DeviceStatus.SKIPPED_PRECHECK,
    DeviceStatus.SKIPPED_MAXFAIL,
    DeviceStatus.DEVICE_BUSY,
    DeviceStatus.CANCELLED,
}


def status_color(status: DeviceStatus) -> str:
    p = theme.current()
    if status == DeviceStatus.SUCCESS:
        return p.success
    if status in _RED:
        return p.error
    if status in _ORANGE:
        return p.warning
    if status in _ACCENT:
        return p.accent
    return p.muted


def make_job_view(
    ctx: AppContext, shell: Shell, *, thread: Any = None, job_id: str | None = None
) -> QWidget:
    """The job view for a running thread, or for a past job loaded from history."""
    return JobView(ctx, shell, thread=thread, job_id=job_id)


def ask_canary(parent: QWidget | None, device: str, status: str, remaining: int) -> bool:
    """Canary finished: True to continue with the remaining devices, False to stop.

    Goes through QMessageBox.question (Yes = continue, No = stop) so one monkeypatch
    covers every confirmation in the app.
    """
    answer = QMessageBox.question(
        parent,
        "Canary finished",
        f"Canary {device} finished with status {status}. "
        f"Continue with the remaining {remaining} devices?\n\n"
        "Yes continues the run. No stops it and cancels the remaining devices.",
        QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
        QMessageBox.StandardButton.No,
    )
    return answer == QMessageBox.StandardButton.Yes


def _fmt_elapsed(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _cell(text: str | None) -> str:
    """One markdown table cell: no line breaks, no raw pipes."""
    return " ".join((text or "").split()).replace("|", "\\|")


class JobView(QWidget):
    def __init__(
        self,
        ctx: AppContext,
        shell: Shell,
        *,
        thread: Any = None,
        job_id: str | None = None,
    ) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self._thread = thread
        self.job_id: str | None = job_id or (thread.job_id if thread is not None else None)
        self._job: JobRecord | None = None
        self._records: dict[str, DeviceRecord] = {}
        self._status: dict[str, DeviceStatus] = {}
        self._detail: dict[str, str] = {}
        self._start: dict[str, datetime] = {}
        self._end: dict[str, datetime] = {}
        self._rows: dict[str, int] = {}
        self._finished = thread is None
        self._job_status = "running" if thread is not None else ""
        self._acknowledged = False
        self._load_error = ""

        self._build()
        if thread is not None:
            thread.events.connect(self._on_events)
            thread.canary_requested.connect(self._on_canary)
            thread.job_started.connect(self._on_job_started)
            thread.job_finished.connect(self._on_job_finished)
            thread.run_failed.connect(self._on_run_failed)
            thread.finished.connect(self._on_thread_finished)
            self._timer = QTimer(self)
            self._timer.setInterval(1000)
            self._timer.timeout.connect(self._tick)
            self._timer.start()
        else:
            self._load_past()
        self._refresh_all()

    # -- construction ---------------------------------------------------------------

    def _build(self) -> None:
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(8)

        head = QHBoxLayout()
        titles = QVBoxLayout()
        self.title_label = QLabel("")
        self.title_label.setObjectName("Title")
        self.meta_label = QLabel("")
        self.meta_label.setObjectName("Muted")
        self.meta_label.setWordWrap(True)
        titles.addWidget(self.title_label)
        titles.addWidget(self.meta_label)
        head.addLayout(titles, 1)
        self.copy_button = QPushButton("Copy summary")
        self.copy_button.clicked.connect(self.copy_summary)
        self.cancel_button = QPushButton("Cancel run")
        self.cancel_button.setProperty("danger", True)
        self.cancel_button.clicked.connect(self.cancel)
        head.addWidget(self.copy_button)
        head.addWidget(self.cancel_button)
        layout.addLayout(head)

        self.alarm_banner, self.alarm_label, self.acknowledge_button = self._banner(
            "error", "Acknowledge"
        )
        self.acknowledge_button.clicked.connect(self.acknowledge)
        layout.addWidget(self.alarm_banner)
        self.warning_banner, self.warning_label, _unused = self._banner("warning", None)
        layout.addWidget(self.warning_banner)

        self.table = QTableWidget(0, 4)
        self.table.setHorizontalHeaderLabels(["Device", "Status", "Elapsed", "Detail"])
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.ResizeMode.ResizeToContents)
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._refresh_detail)

        self.log = MaskedTextView()
        left = QSplitter(Qt.Orientation.Vertical)
        left.addWidget(self.table)
        left.addWidget(self.log)
        left.setStretchFactor(0, 2)
        left.setStretchFactor(1, 3)

        self.detail_tabs = QTabWidget()
        self.render_view = MaskedTextView()
        self.output_view = MaskedTextView()
        self.diff_view = DiffView()
        self.details_view = MaskedTextView()
        for view, name in (
            (self.render_view, "Render"),
            (self.output_view, "Output"),
            (self.diff_view, "Diff"),
            (self.details_view, "Details"),
        ):
            self.detail_tabs.addTab(view, name)

        split = QSplitter(Qt.Orientation.Horizontal)
        split.addWidget(left)
        split.addWidget(self.detail_tabs)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)

    @staticmethod
    def _banner(level: str, button: str | None) -> tuple[QFrame, QLabel, QPushButton]:
        frame = QFrame()
        frame.setObjectName("Banner")
        frame.setProperty("level", level)
        row = QHBoxLayout(frame)
        row.setContentsMargins(0, 0, 8, 0)
        label = QLabel("")
        label.setWordWrap(True)
        color = "#ffffff" if level == "error" else "#1a1917"
        label.setStyleSheet(f"background: transparent; color: {color}; font-weight: 600;")
        row.addWidget(label, 1)
        btn = QPushButton(button or "")
        if button:
            row.addWidget(btn)
        frame.setVisible(False)
        return frame, label, btn

    # -- loading ---------------------------------------------------------------------

    def _load_past(self) -> None:
        if self.job_id is None:
            return
        try:
            self._job = self.ctx.history.get_job(self.job_id)
        except TsunamiError as exc:
            self._load_error = str(exc)
            return
        self._job_status = str(self._job.status)
        self._reload_records(sync=True)
        # A past job's alarm may already have been acknowledged (here or in the status bar).
        if any(s in ALARM_DEVICE_STATUSES for s in self._status.values()):
            self._acknowledged = self.job_id not in self.ctx.unacknowledged_alarms()

    def _reload_records(self, *, sync: bool = False) -> None:
        if self.job_id is None:
            return
        try:
            self._job = self.ctx.history.get_job(self.job_id)
            records = self.ctx.history.devices(self.job_id)
        except TsunamiError:
            return
        self._records = {r.device: r for r in records}
        for name, rec in self._records.items():
            if sync or name not in self._status:
                self._set_status(name, rec.status, self._detail.get(name, ""))

    def _set_status(self, name: str, status: DeviceStatus, detail: str = "") -> None:
        self._ensure_row(name)
        self._status[name] = status
        if detail:
            self._detail[name] = detail
        now = datetime.now(UTC)
        if status not in (DeviceStatus.PENDING,) and name not in self._start:
            self._start[name] = now
        if status in TERMINAL_DEVICE_STATUSES:
            self._end.setdefault(name, now)

    def _ensure_row(self, name: str) -> None:
        if name in self._rows:
            return
        row = self.table.rowCount()
        self.table.insertRow(row)
        self._rows[name] = row
        for col in range(4):
            self.table.setItem(row, col, QTableWidgetItem(""))
        self.table.item(row, 0).setText(name)
        self._status.setdefault(name, DeviceStatus.PENDING)
        if self.table.currentRow() < 0:
            self.table.selectRow(0)

    # -- events ----------------------------------------------------------------------

    def _on_job_started(self, job_id: str) -> None:
        self.job_id = job_id
        self._refresh_all()

    def _on_job_finished(self, job_id: str) -> None:
        self.job_id = job_id
        self._finished = True
        self._reload_records(sync=True)
        self._refresh_all()

    def _on_thread_finished(self) -> None:
        self._finished = True
        self._refresh_all()

    def _on_run_failed(self, message: str) -> None:
        self._finished = True
        self.warning_label.setText(f"Run failed: {message}")
        self.warning_banner.setVisible(True)
        self.log.append_text(f"Run failed: {message}")
        self._refresh_all()

    def _on_events(self, events: list) -> None:
        for event in events:
            self._handle(event)
        self._reload_records()
        self._refresh_all()

    def _handle(self, event: Any) -> None:
        if isinstance(event, JobStarted):
            self.job_id = event.job_id
            for name in event.devices:
                self._ensure_row(name)
        elif isinstance(event, DeviceStatusChanged):
            self._set_status(event.device, event.status, event.detail)
            detail = f" ({event.detail})" if event.detail else ""
            self.log.append_text(f"[{event.device}] -> {event.status}{detail}")
        elif isinstance(event, DeviceLog):
            self.log.append_text(f"[{event.device}] {event.text}")
        elif isinstance(event, CanaryWaiting):
            self.log.append_text(f"Canary {event.device} finished; waiting for your decision")
        elif isinstance(event, BatchFinished):
            self.log.append_text(
                f"Batch {event.batch} finished: {event.failed} of {event.attempted} failed"
            )
        elif isinstance(event, MaxFailReached):
            self.log.append_text(
                f"Stopped: {event.failed} of {event.attempted} devices failed "
                f"(limit {event.max_fail_percent}%)"
            )
        elif isinstance(event, JobFinished):
            self._job_status = str(event.status)
            self._finished = True
            self.log.append_text(f"Job finished: {event.status}")

    def _on_canary(self, device: str) -> None:
        status = self._status.get(device)
        remaining = sum(
            1 for name, s in self._status.items() if s == DeviceStatus.PENDING and name != device
        )
        proceed = ask_canary(self, device, str(status or "unknown"), remaining)
        if self._thread is not None:
            self._thread.answer_canary(proceed)

    # -- actions ---------------------------------------------------------------------

    def cancel(self) -> None:
        if self._thread is None or self._finished:
            return
        answer = QMessageBox.question(
            self,
            "Cancel run",
            "Cancel this run? Devices already working finish their current step, then "
            "stop and roll back if the play says so.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        self._thread.cancel()
        self.log.append_text("Cancel requested")
        self.cancel_button.setEnabled(False)

    def acknowledge(self) -> None:
        if self.job_id is not None:
            self.ctx.acknowledge_alarm(self.job_id)
        self._acknowledged = True
        self.alarm_banner.setVisible(False)

    def copy_summary(self) -> None:
        QApplication.clipboard().setText(self.summary_markdown())
        self.shell.notify("Summary copied to the clipboard.", "info")

    def select_device(self, name: str) -> None:
        row = self._rows.get(name)
        if row is not None:
            self.table.selectRow(row)

    # -- rendering the state ---------------------------------------------------------

    def _play_name(self) -> str:
        path = None
        if self._job is not None:
            path = self._job.play_path
        elif self._thread is not None:
            path = str(self._thread.request.play_path)
        if path is None:
            return "Job"
        ws = self.ctx.workspace
        entry = ws.plays.get(Path(path)) if ws is not None else None
        if entry is not None and entry.play is not None:
            return entry.play.name
        return path

    def _mode(self) -> str:
        if self._job is not None:
            return self._job.mode
        if self._thread is not None:
            return str(self._thread.request.mode)
        return ""

    def _actor(self) -> str:
        if self._job is not None:
            return str(self._job.actor)
        if self._thread is not None:
            return str(self._thread.request.actor)
        return ""

    def _state(self) -> str:
        if self._job is not None and (self._finished or self._thread is None):
            return str(self._job.status)
        return self._job_status or "running"

    def counts(self) -> Counter[str]:
        return Counter(str(s) for s in self._status.values())

    def _counts_text(self) -> str:
        counts = self.counts()
        total = sum(counts.values())
        if not total:
            return "no devices yet"
        parts = [f"{n} {name}" for name, n in sorted(counts.items(), key=lambda kv: -kv[1])]
        return f"{total} device{'s' if total != 1 else ''}: " + ", ".join(parts)

    def _refresh_all(self) -> None:
        if self._load_error:
            self.title_label.setText("Job not found")
            self.meta_label.setText(self._load_error)
            self.cancel_button.setEnabled(False)
            self.copy_button.setEnabled(False)
            return
        self.title_label.setText(self._play_name())
        bits = [self._mode(), self._actor(), self._state(), self._counts_text()]
        self.meta_label.setText(" | ".join(b for b in bits if b))
        self.copy_button.setEnabled(self.job_id is not None)
        self.cancel_button.setEnabled(self._thread is not None and not self._finished)
        self._refresh_table()
        self._refresh_alarm()
        self._refresh_detail()

    def _elapsed(self, name: str) -> str:
        rec = self._records.get(name)
        start = rec.started_at if rec and rec.started_at else self._start.get(name)
        if start is None:
            return ""
        status = self._status.get(name, DeviceStatus.PENDING)
        end = rec.finished_at if rec and rec.finished_at else self._end.get(name)
        if end is None:
            end = datetime.now(UTC) if status not in TERMINAL_DEVICE_STATUSES else start
        return _fmt_elapsed(max(0.0, (end - start).total_seconds()))

    def _refresh_table(self) -> None:
        for name, row in self._rows.items():
            status = self._status.get(name, DeviceStatus.PENDING)
            color = status_color(status)
            status_item = self.table.item(row, 1)
            status_item.setText(str(status))
            status_item.setIcon(icon(_ICONS.get(status, "circle-outline"), color))
            status_item.setForeground(QColor(color))
            self.table.item(row, 2).setText(self._elapsed(name))
            rec = self._records.get(name)
            detail = self._detail.get(name) or (rec.error_redacted if rec else "") or ""
            self.table.item(row, 3).setText(" ".join(detail.split()))

    def _tick(self) -> None:
        if self._finished:
            self._timer.stop()
            return
        for name, row in self._rows.items():
            if self._status.get(name) not in TERMINAL_DEVICE_STATUSES:
                self.table.item(row, 2).setText(self._elapsed(name))

    def alarm_count(self) -> int:
        return sum(1 for s in self._status.values() if s in ALARM_DEVICE_STATUSES)

    def _refresh_alarm(self) -> None:
        count = self.alarm_count()
        if count and not self._acknowledged:
            self.alarm_label.setText(f"{count} device(s) may be half-configured")
            self.alarm_banner.setVisible(True)
        # Never hidden here: only the Acknowledge button dismisses a shown banner.

    def selected_device(self) -> str | None:
        row = self.table.currentRow()
        if row < 0:
            return None
        item = self.table.item(row, 0)
        return item.text() if item else None

    def _refresh_detail(self) -> None:
        name = self.selected_device()
        rec = self._records.get(name) if name else None
        if name is None or rec is None:
            for view in (self.render_view, self.output_view, self.details_view):
                self._set_text(view, "")
            self.diff_view.set_diff("")
            return
        self._set_text(self.render_view, rec.render_masked or "")
        self._set_text(self.output_view, rec.output_redacted or "")
        self.diff_view.set_diff(rec.diff_redacted)
        lines = [f"Status: {rec.status}"]
        if rec.failed_line:
            lines.append(f"Failed line: {rec.failed_line}")
        if rec.error_redacted:
            lines.append(f"Error: {rec.error_redacted}")
        if rec.rollback_status:
            lines.append(f"Rollback: {rec.rollback_status}")
        if rec.backup_path:
            lines.append(f"Backup: {rec.backup_path}")
        if rec.started_at:
            lines.append(f"Started: {rec.started_at:%Y-%m-%d %H:%M:%S} UTC")
        if rec.finished_at:
            lines.append(f"Finished: {rec.finished_at:%Y-%m-%d %H:%M:%S} UTC")
        self._set_text(self.details_view, "\n".join(lines))

    @staticmethod
    def _set_text(view: MaskedTextView, text: str) -> None:
        if view.toPlainText() != text:
            view.setPlainText(text)

    # -- summary ---------------------------------------------------------------------

    def summary_markdown(self) -> str:
        """A ticket-ready summary. Only redacted history text goes in."""
        self._reload_records(sync=self._finished)
        job = self._job
        when = ""
        if job is not None:
            moment = job.started_at or job.created_at
            when = f"{moment:%Y-%m-%d %H:%M} UTC"
        lines = [
            f"## {self._play_name()} ({self._mode()})",
            "",
            f"- Job: `{self.job_id or 'unknown'}`",
            f"- Time: {when or 'unknown'}",
            f"- Status: {self._state()}",
            f"- Run by: {self._actor() or 'unknown'}",
            "",
            "| Status | Devices |",
            "| --- | --- |",
        ]
        for status, n in sorted(self.counts().items(), key=lambda kv: -kv[1]):
            lines.append(f"| {status} | {n} |")
        lines += ["", "| Device | Status | Error |", "| --- | --- | --- |"]
        for name, status in self._status.items():
            rec = self._records.get(name)
            error = ""
            if rec is not None:
                error = rec.error_redacted or ""
                if rec.failed_line:
                    error = f"{error} (line: {rec.failed_line})".strip()
                if rec.rollback_status:
                    error = f"{error} [rollback: {rec.rollback_status}]".strip()
            lines.append(f"| {_cell(name)} | {status} | {_cell(error)} |")
        return "\n".join(lines) + "\n"
