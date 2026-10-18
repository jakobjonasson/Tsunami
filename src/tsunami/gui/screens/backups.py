"""Backups tab: browse a device's config backups and compare two, secrets always masked.

Backups are saved unredacted (core/backups.py), so everything this tab shows goes through
Tsunami's redactor for the customer first: the exact values of every stored secret and
login, the platform line rules and the user's extra rules. If Tsunami is locked the
stored values cannot be read, so the content is not shown at all (names, times and sizes
still are). There is deliberately no "open the raw file" action; "Show in folder" only
selects the file in Explorer.

See design.md, Backups; Secrets and credentials: Redactor.
"""

from __future__ import annotations

import logging
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QItemSelectionModel, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.backups import (
    BackupEntry,
    BackupError,
    diff_redacted,
    list_backups,
    read_redacted,
)
from tsunami.core.models.common import require_slug
from tsunami.core.redact import Redactor
from tsunami.core.vault import VaultLocked
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import Shell
from tsunami.gui.widgets.diff_view import DiffView
from tsunami.gui.widgets.masked_text import MaskedTextView

log = logging.getLogger(__name__)

LOCKED_TEXT = "Unlock Tsunami to view backups"
NO_BACKUPS_TEXT = (
    "No backups yet for this device.\n"
    "Tsunami saves one before each change it applies, while backups are on for the play."
)
HINT_TEXT = (
    "Select a backup to view it.\nSelect two, or choose one and press Compare, to see what changed."
)
IDENTICAL_TEXT = (
    "These two backups are identical once secret values are masked.\n"
    "A changed password or key does not show as a difference."
)
PREVIOUS = "Previous backup"
NEWEST = "Newest backup"

_ENTRY_ROLE = Qt.ItemDataRole.UserRole
_PAGE_MESSAGE, _PAGE_CONTENT, _PAGE_DIFF = 0, 1, 2


def reveal_in_folder(path: Path) -> None:
    """Show the file selected in Explorer. Tests replace this."""
    if sys.platform == "win32":
        subprocess.Popen(f'explorer.exe /select,"{path}"', close_fds=True)  # noqa: S602
    else:
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path.parent)))


def format_size(size: int) -> str:
    if size < 1024:
        return f"{size} B"
    if size < 1024 * 1024:
        return f"{size / 1024:.1f} KB"
    return f"{size / (1024 * 1024):.1f} MB"


def _local_time(value: datetime) -> str:
    return value.astimezone().strftime("%Y-%m-%d %H:%M:%S")


class BackupsTab(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell, customer: str, device: str) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self.customer = customer
        self.device = device
        self.entries: list[BackupEntry] = []
        self._error: str | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 14, 16, 12)
        layout.setSpacing(8)

        title = QLabel(f"Backups of {device}")
        title.setObjectName("Title")
        layout.addWidget(title)
        self.subtitle = QLabel()
        self.subtitle.setObjectName("Muted")
        layout.addWidget(self.subtitle)

        bar = QHBoxLayout()
        compare_label = QLabel("Compare with")
        self.compare_combo = wheel.ComboBox()
        self.compare_combo.addItems([PREVIOUS, NEWEST])
        compare_label.setBuddy(self.compare_combo)
        self.compare_combo.setToolTip("Used when one backup is selected")
        self.compare_button = QPushButton("Compare")
        self.compare_button.setProperty("primary", True)
        self.compare_button.setIcon(icon("git-compare", theme.current().accent_text))
        self.compare_button.setToolTip("Show what changed between two backups")
        self.compare_button.clicked.connect(lambda _=False: self.compare())
        self.folder_button = QPushButton("Show in folder")
        self.folder_button.setIcon(icon("folder-opened", theme.current().muted))
        self.folder_button.setToolTip("Select the selected backup file in Explorer")
        self.folder_button.clicked.connect(lambda _=False: self.show_in_folder())
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.setIcon(icon("refresh", theme.current().muted))
        self.refresh_button.clicked.connect(lambda _=False: self.refresh())
        bar.addWidget(compare_label)
        bar.addWidget(self.compare_combo)
        bar.addWidget(self.compare_button)
        bar.addStretch(1)
        bar.addWidget(self.folder_button)
        bar.addWidget(self.refresh_button)
        layout.addLayout(bar)

        self.splitter = QSplitter(Qt.Orientation.Vertical)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Taken", "Job", "Size"])
        self.table.setFrameShape(QTableWidget.Shape.NoFrame)
        self.table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self.table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.table.verticalHeader().setVisible(False)
        self.table.setShowGrid(False)
        header = self.table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Fixed)
        header.resizeSection(1, 120)
        header.setStretchLastSection(True)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.splitter.addWidget(self.table)

        lower = QWidget()
        lower_layout = QVBoxLayout(lower)
        lower_layout.setContentsMargins(0, 4, 0, 0)
        lower_layout.setSpacing(4)
        self.caption = QLabel()
        self.caption.setObjectName("Section")
        lower_layout.addWidget(self.caption)
        self.stack = QStackedWidget()
        self.message = QLabel()
        self.message.setObjectName("Muted")
        self.message.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.message.setWordWrap(True)
        self.content_view = MaskedTextView()
        self.diff_view = DiffView()
        self.stack.addWidget(self.message)
        self.stack.addWidget(self.content_view)
        self.stack.addWidget(self.diff_view)
        lower_layout.addWidget(self.stack, 1)
        self.splitter.addWidget(lower)
        self.splitter.setStretchFactor(0, 0)
        self.splitter.setStretchFactor(1, 1)
        self.splitter.setSizes([180, 420])
        layout.addWidget(self.splitter, 1)

        ctx.jobs_changed.connect(self.refresh)
        lock = getattr(ctx, "lock", None)
        changed = getattr(lock, "lock_changed", None)
        if changed is not None:
            changed.connect(lambda _unlocked=False: self._selection_changed())
        self.refresh()

    # -- listing -------------------------------------------------------------------------

    def refresh(self) -> None:
        selected = {e.path for e in self.selected_entries()}
        self._error = None
        try:
            self.entries = list_backups(self.ctx.paths, self.customer, self.device)
        except BackupError as exc:
            self.entries, self._error = [], str(exc)
        self.table.blockSignals(True)
        self.table.setRowCount(len(self.entries))
        for row, entry in enumerate(self.entries):
            self._fill_row(row, entry)
            if entry.path in selected:
                self.table.selectionModel().select(
                    self.table.model().index(row, 0),
                    QItemSelectionModel.SelectionFlag.Select
                    | QItemSelectionModel.SelectionFlag.Rows,
                )
        self.table.blockSignals(False)
        count = len(self.entries)
        self.subtitle.setText(
            f"{self.customer}  |  {count} backup{'s' if count != 1 else ''}"
            "  |  secret values are masked"
        )
        self.table.setVisible(bool(self.entries))
        has = bool(self.entries)
        self.compare_button.setEnabled(has)
        self.compare_combo.setEnabled(has)
        self.folder_button.setEnabled(has)
        self._selection_changed()

    def _fill_row(self, row: int, entry: BackupEntry) -> None:
        taken = QTableWidgetItem(_local_time(entry.taken_at))
        taken.setData(_ENTRY_ROLE, entry)
        taken.setToolTip(entry.taken_at.strftime("%Y-%m-%d %H:%M:%S UTC"))
        self.table.setItem(row, 0, taken)
        link = QPushButton(entry.job_id[:8])
        link.setObjectName("Link")
        link.setCursor(Qt.CursorShape.PointingHandCursor)
        link.setToolTip(f"Open job {entry.job_id}")
        link.clicked.connect(lambda _=False, j=entry.job_id: self.shell.open_job(j))
        self.table.setCellWidget(row, 1, link)
        size = QTableWidgetItem(format_size(entry.size))
        size.setTextAlignment(Qt.AlignmentFlag.AlignLeft | Qt.AlignmentFlag.AlignVCenter)
        self.table.setItem(row, 2, size)
        self.table.setRowHeight(row, 30)

    def selected_entries(self) -> list[BackupEntry]:
        rows = sorted({i.row() for i in self.table.selectedIndexes()})
        out = []
        for row in rows:
            item = self.table.item(row, 0)
            if item is not None:
                out.append(item.data(_ENTRY_ROLE))
        return out

    # -- showing -------------------------------------------------------------------------

    def _redactor(self) -> Redactor | str:
        """The customer's redactor, or the text to show instead when it cannot be built.

        Fails closed: if any stored value cannot be read, nothing is shown."""
        lock = getattr(self.ctx, "lock", None)
        if lock is not None and not lock.unlocked:
            return LOCKED_TEXT
        try:
            values = list(self.ctx.secrets.all_secret_values())
        except VaultLocked:
            return LOCKED_TEXT
        except Exception:  # noqa: BLE001
            log.warning("cannot read the credential store to show a backup", exc_info=True)
            return "Backups cannot be shown: the credential store cannot be read."
        return Redactor(values, extra_rules=self.ctx.settings.extra_rules())

    def _say(self, text: str, caption: str = "") -> None:
        self.content_view.clear()
        self.diff_view.clear()
        self.message.setText(text)
        self.caption.setText(caption)
        self.stack.setCurrentIndex(_PAGE_MESSAGE)

    def _selection_changed(self) -> None:
        chosen = self.selected_entries()
        self.folder_button.setEnabled(len(chosen) == 1)
        self.compare_button.setEnabled(len(chosen) in (1, 2))
        if self._error:
            self._say(self._error)
        elif not self.entries:
            self._say(NO_BACKUPS_TEXT)
        elif len(chosen) == 1:
            self._show_one(chosen[0])
        elif len(chosen) == 2:
            self._show_diff(chosen[1], chosen[0])  # table is newest first
        elif len(chosen) > 2:
            self._say("Select one or two backups.")
        else:
            self._say(HINT_TEXT)

    @staticmethod
    def _label(entry: BackupEntry) -> str:
        return f"{_local_time(entry.taken_at)} (job {entry.job_id[:8]})"

    def _show_one(self, entry: BackupEntry) -> None:
        redactor = self._redactor()
        if isinstance(redactor, str):
            self._say(redactor, self._label(entry))
            return
        try:
            text = read_redacted(entry.path, redactor, root=self.ctx.paths.backups_dir)
        except BackupError as exc:
            self._say(str(exc), self._label(entry))
            return
        self.diff_view.clear()
        self.content_view.setPlainText(text)
        self.caption.setText(self._label(entry))
        self.stack.setCurrentIndex(_PAGE_CONTENT)

    def _show_diff(self, old: BackupEntry, new: BackupEntry) -> None:
        caption = f"Changes from {self._label(old)} to {self._label(new)}"
        redactor = self._redactor()
        if isinstance(redactor, str):
            self._say(redactor, caption)
            return
        try:
            text = diff_redacted(old.path, new.path, redactor, root=self.ctx.paths.backups_dir)
        except BackupError as exc:
            self._say(str(exc), caption)
            return
        if not text:
            self._say(IDENTICAL_TEXT, caption)
            return
        self.content_view.clear()
        self.diff_view.set_diff(text)
        self.caption.setText(caption)
        self.stack.setCurrentIndex(_PAGE_DIFF)

    # -- actions -------------------------------------------------------------------------

    def compare(self) -> None:
        """Two selected: diff them. One selected: diff it with the "Compare with" choice."""
        chosen = self.selected_entries()
        if len(chosen) == 2:
            self._show_diff(chosen[1], chosen[0])
            return
        if len(chosen) != 1:
            self._say("Select a backup to compare.")
            return
        picked = chosen[0]
        index = self.entries.index(picked)
        if self.compare_combo.currentText() == NEWEST:
            other = self.entries[0]
            if other == picked:
                self._say("This is the newest backup; there is nothing newer to compare with.")
                return
            self._show_diff(picked, other)
            return
        if index + 1 >= len(self.entries):
            self._say("This is the oldest backup; there is no earlier one to compare with.")
            return
        self._show_diff(self.entries[index + 1], picked)

    def show_in_folder(self) -> None:
        chosen = self.selected_entries()
        if len(chosen) == 1:
            reveal_in_folder(chosen[0].path)


def open_backups(ctx: AppContext, shell: Shell, customer: str, device: str) -> None:
    """Open (or focus) the Backups tab for one device."""
    try:
        require_slug(customer)
        require_slug(device)
    except Exception:  # noqa: BLE001
        shell.notify(f"Cannot show backups: {device!r} is not a valid device name.", "error")
        return
    shell.open_tab(
        f"backups:{customer}/{device}",
        f"Backups: {device}",
        lambda: BackupsTab(ctx, shell, customer, device),
        "archive",
    )


def register(shell: Shell) -> None:
    shell.set_handler(
        "backups.open", lambda customer, device: open_backups(shell.ctx, shell, customer, device)
    )
