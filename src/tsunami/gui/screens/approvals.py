"""Approvals screen: pending MCP requests as cards, Approve or Deny.

See design.md, GUI; MCP interface: Approval flow.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QAbstractItemView,
    QFrame,
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QTabWidget,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from tsunami.core import approvals, inventory_proposals
from tsunami.core.issues import TsunamiError
from tsunami.core.models.jobs import JobStatus
from tsunami.core.models.proposals import ProposalStatus
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.shell import ActivityItem, Shell
from tsunami.gui.widgets.masked_text import MaskedTextView

NOTE = "Claude requested this change. Review the diff for each device before approving."


def _local(value: datetime | None) -> str:
    if value is None:
        return "-"
    return value.astimezone().strftime("%Y-%m-%d %H:%M")


def _play_name(path: str) -> str:
    return Path(path).stem


def _text(value: object) -> str:
    return str(getattr(value, "value", value))


def _confirm(parent: QWidget | None, title: str, text: str) -> bool:
    answer = QMessageBox.question(parent, title, text)
    return answer == QMessageBox.StandardButton.Yes


class DiffText(MaskedTextView):
    """A diff_redacted text: added lines green, removed lines red."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        p = theme.current()
        selections = []
        block = self.document().firstBlock()
        while block.isValid():
            line = block.text()
            color = p.success if line.startswith("+ ") else p.error if line.startswith("- ") else ""
            if color:
                sel = QTextEdit.ExtraSelection()
                sel.cursor = QTextCursor(block)
                sel.cursor.select(QTextCursor.SelectionType.LineUnderCursor)
                fmt = sel.format
                fmt.setForeground(QColor(color))
                sel.format = fmt
                selections.append(sel)
            block = block.next()
        self.setExtraSelections(selections)


class ApprovalCard(QWidget):
    """One request in full: details, per-device render and diff, Approve and Deny."""

    def __init__(self, ctx: AppContext, shell: Shell, job_id: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self.job_id = job_id
        job = ctx.history.get_job(job_id)
        devices = ctx.history.devices(job_id)
        self.job = job

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        title = QLabel(f"Approve: {_play_name(job.play_path)}")
        title.setObjectName("Title")
        root.addWidget(title)
        note = QLabel(NOTE)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        root.addWidget(note)

        details = QFrame()
        details.setObjectName("Card")
        grid = QVBoxLayout(details)
        tags = ", ".join(job.tags) if job.tags else "(none)"
        for label, value in (
            ("Play", job.play_path),
            ("Customer", job.customer),
            ("Requested by", _text(job.actor)),
            ("Requested", _local(job.created_at)),
            ("Tag filter", tags),
            ("Target devices", ", ".join(d.device for d in devices) or "(none)"),
        ):
            grid.addWidget(QLabel(f"{label}: {value}"))
        root.addWidget(details)

        self.prompts = QTableWidget(len(job.prompts), 2)
        self.prompts.setHorizontalHeaderLabels(["Prompt", "Value"])
        self.prompts.verticalHeader().setVisible(False)
        self.prompts.horizontalHeader().setStretchLastSection(True)
        self.prompts.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.prompts.setMaximumHeight(140)
        for row, (key, value) in enumerate(job.prompts.items()):
            self.prompts.setItem(row, 0, QTableWidgetItem(str(key)))
            self.prompts.setItem(row, 1, QTableWidgetItem(str(value)))
        root.addWidget(self.prompts)

        self.device_tabs = QTabWidget()
        for record in devices:
            page = QTabWidget()
            page.addTab(MaskedTextView(record.render_masked or ""), "Render")
            page.addTab(DiffText(record.diff_redacted or ""), "Diff")
            self.device_tabs.addTab(page, record.device)
        root.addWidget(self.device_tabs, 1)

        buttons = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        buttons.addWidget(self.status_label, 1)
        self.deny_button = QPushButton("Deny")
        self.deny_button.setProperty("danger", "true")
        self.approve_button = QPushButton("Approve")
        self.approve_button.setProperty("primary", "true")
        buttons.addWidget(self.deny_button)
        buttons.addWidget(self.approve_button)
        root.addLayout(buttons)
        self.approve_button.clicked.connect(self.approve)
        self.deny_button.clicked.connect(self.deny)

        if job.status != JobStatus.AWAITING_APPROVAL:
            self.status_label.setText(f"This request is {_text(job.status)}.")
            self.approve_button.setEnabled(False)
            self.deny_button.setEnabled(False)

    def _key(self) -> str:
        return f"approval:{self.job_id}"

    def approve(self) -> None:
        job = self.job
        count = len(self.ctx.history.devices(self.job_id))
        if not _confirm(
            self,
            "Approve change",
            f"Apply {_play_name(job.play_path)} to {count} device(s) of customer "
            f"{job.customer}?\n\nThe change will be pushed to the devices now.",
        ):
            return
        try:
            request = approvals.approve(
                self.ctx.history,
                self.ctx.workspace,
                self.job_id,
                approved_by=self.ctx.windows_user(),
            )
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return
        self.shell.start_run(request, f"Run: {_play_name(job.play_path)}")
        self.shell.close_tab(self._key())
        self.ctx.jobs_changed.emit()

    def deny(self) -> None:
        job = self.job
        if not _confirm(
            self,
            "Deny change",
            f"Deny the request to run {_play_name(job.play_path)} for customer {job.customer}?",
        ):
            return
        try:
            approvals.deny(self.ctx.history, self.job_id, denied_by=self.ctx.windows_user())
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return
        self.shell.close_tab(self._key())
        self.ctx.jobs_changed.emit()


PROPOSAL_NOTE = (
    "Claude proposed these inventory changes. Nothing has changed yet: review each device's "
    "before and after, then approve or deny."
)
SOURCES = {"mcp": "Claude, over MCP"}


class ProposalCard(QWidget):
    """One inventory proposal: source, each field before and after, Approve and Deny."""

    def __init__(self, ctx: AppContext, shell: Shell, proposal_id: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self.proposal_id = proposal_id
        proposal = ctx.history.get_inventory_proposal(proposal_id)
        self.proposal = proposal

        root = QVBoxLayout(self)
        root.setContentsMargins(16, 16, 16, 16)
        title = QLabel(f"Approve inventory change: {proposal.customer}")
        title.setObjectName("Title")
        root.addWidget(title)
        note = QLabel(PROPOSAL_NOTE)
        note.setObjectName("Muted")
        note.setWordWrap(True)
        root.addWidget(note)

        details = QFrame()
        details.setObjectName("Card")
        grid = QVBoxLayout(details)
        for label, value in (
            ("Customer", proposal.customer),
            ("Requested by", SOURCES.get(proposal.source, proposal.source)),
            ("Requested", _local(proposal.created_at)),
            ("Reason given (Claude's words, unverified)", proposal.reason or "(none)"),
            ("Devices", ", ".join(c["device"] for c in proposal.changes)),
        ):
            line = QLabel(f"{label}: {value}")
            line.setTextFormat(Qt.TextFormat.PlainText)  # Claude wrote the reason
            line.setWordWrap(True)
            grid.addWidget(line)
        root.addWidget(details)

        rows = [
            (c["device"], field["field"], field["before"], field["after"], c["remove"])
            for c, described in zip(
                proposal.changes, inventory_proposals.describe(proposal.changes), strict=True
            )
            for field in described["fields"]
        ]
        self.changes = QTableWidget(len(rows), 4)
        self.changes.setHorizontalHeaderLabels(["Device", "Field", "Before", "After"])
        self.changes.verticalHeader().setVisible(False)
        self.changes.horizontalHeader().setStretchLastSection(True)
        self.changes.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        p = theme.current()
        for row, (device, field_name, before, after, removed) in enumerate(rows):
            shown_after = "(device removed)" if removed else (after if after is not None else "-")
            cells = (device, field_name, before if before is not None else "-", shown_after)
            for column, text in enumerate(cells):
                item = QTableWidgetItem(str(text))
                if column == 2:
                    item.setForeground(QColor(p.error))
                elif column == 3:
                    item.setForeground(QColor(p.error if removed else p.success))
                self.changes.setItem(row, column, item)
        root.addWidget(self.changes, 1)

        buttons = QHBoxLayout()
        self.status_label = QLabel("")
        self.status_label.setObjectName("Muted")
        self.status_label.setWordWrap(True)
        buttons.addWidget(self.status_label, 1)
        self.deny_button = QPushButton("Deny")
        self.deny_button.setProperty("danger", "true")
        self.approve_button = QPushButton("Approve")
        self.approve_button.setProperty("primary", "true")
        buttons.addWidget(self.deny_button)
        buttons.addWidget(self.approve_button)
        root.addLayout(buttons)
        self.approve_button.clicked.connect(self.approve)
        self.deny_button.clicked.connect(self.deny)

        if proposal.status != ProposalStatus.AWAITING_APPROVAL:
            self._settle(proposal.status)

    def _settle(self, status: ProposalStatus) -> None:
        self.status_label.setText(f"This proposal is {_text(status)}.")
        self.approve_button.setEnabled(False)
        self.deny_button.setEnabled(False)

    def _key(self) -> str:
        return f"approval:{self.proposal_id}"

    def approve(self) -> None:
        count = len(self.proposal.changes)
        if not _confirm(
            self,
            "Approve inventory change",
            f"Change {count} device(s) of customer {self.proposal.customer} in the inventory?",
        ):
            return
        try:
            inventory_proposals.approve_proposal(
                self.ctx.history,
                self.ctx.workspace.root,
                self.proposal_id,
                approved_by=self.ctx.windows_user(),
            )
        except TsunamiError as exc:
            self._settle(self.ctx.history.get_inventory_proposal(self.proposal_id).status)
            self.shell.notify(str(exc), "error")
            self.ctx.jobs_changed.emit()
            return
        self.ctx.reload_workspace()
        self.shell.close_tab(self._key())
        self.ctx.jobs_changed.emit()

    def deny(self) -> None:
        if not _confirm(
            self,
            "Deny inventory change",
            f"Deny the proposed changes to customer {self.proposal.customer}?",
        ):
            return
        try:
            inventory_proposals.deny_proposal(
                self.ctx.history, self.proposal_id, denied_by=self.ctx.windows_user()
            )
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return
        self.shell.close_tab(self._key())
        self.ctx.jobs_changed.emit()


class ApprovalsView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        top = QHBoxLayout()
        self.count_label = QLabel("")
        self.count_label.setObjectName("Muted")
        top.addWidget(self.count_label, 1)
        # No Refresh button of its own: the poller adds new requests, and the side bar's
        # refresh (F5) re-reads them with everything else.
        layout.addLayout(top)
        self.list = QListWidget()
        self.list.setSpacing(2)
        layout.addWidget(self.list, 1)
        self.empty = QLabel("Nothing is waiting for approval.")
        self.empty.setObjectName("Muted")
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)

        self.list.itemClicked.connect(self._open)
        self.list.itemActivated.connect(self._open)
        ctx.approvals_changed.connect(self._on_approvals_changed)
        ctx.jobs_changed.connect(self.refresh)
        self.refresh()

    def _on_approvals_changed(self, _count: int) -> None:
        self.refresh()

    def refresh(self) -> None:
        jobs = self.ctx.history.list_jobs(status=JobStatus.AWAITING_APPROVAL)
        proposals = self.ctx.history.list_inventory_proposals(
            status=ProposalStatus.AWAITING_APPROVAL
        )
        self.list.clear()
        for proposal in proposals:
            count = len(proposal.changes)
            item = QListWidgetItem(
                f"Inventory change\n"
                f"{proposal.customer} - {count} device(s)\n"
                f"Requested {_local(proposal.created_at)}"
            )
            item.setData(Qt.ItemDataRole.UserRole, proposal.id)
            item.setToolTip("Claude proposed changes to the inventory")
            self.list.addItem(item)
        for job in jobs:
            count = len(self.ctx.history.devices(job.id))
            text = (
                f"{_play_name(job.play_path)}\n"
                f"{job.customer} - {count} device(s)\n"
                f"Requested {_local(job.created_at)}"
            )
            item = QListWidgetItem(text)
            item.setData(Qt.ItemDataRole.UserRole, job.id)
            item.setToolTip(job.play_path)
            self.list.addItem(item)
        total = len(jobs) + len(proposals)
        self.count_label.setText(f"{total} pending")
        self.empty.setVisible(not total)
        self.list.setVisible(bool(total))

    def _open(self, item: QListWidgetItem) -> None:
        self.open_request(item.data(Qt.ItemDataRole.UserRole))

    def open_request(self, job_id: str) -> QWidget:
        """Open a run request's card, or an inventory proposal's if the id is one."""
        try:
            job = self.ctx.history.get_job(job_id)
        except TsunamiError:
            proposal = self.ctx.history.get_inventory_proposal(job_id)
            return self.shell.open_tab(
                f"approval:{job_id}",
                f"Approve: inventory, {proposal.customer}",
                lambda: ProposalCard(self.ctx, self.shell, job_id),
                "inbox",
            )
        return self.shell.open_tab(
            f"approval:{job_id}",
            f"Approve: {_play_name(job.play_path)}",
            lambda: ApprovalCard(self.ctx, self.shell, job_id),
            "inbox",
        )


def _factory(ctx: AppContext, shell: Shell) -> QWidget:
    return ApprovalsView(ctx, shell)


VIEW = ActivityItem("approvals", "Approvals", "inbox", _factory, order=50)
