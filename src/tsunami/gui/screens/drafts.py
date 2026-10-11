"""Drafts screen: diff against live, secret() and destructive highlights, Promote or Delete.

See design.md, GUI.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QTextCursor
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from tsunami.core import drafts as drafts_core
from tsunami.core.drafts import Draft
from tsunami.core.issues import Severity, TsunamiError
from tsunami.gui import icons, theme
from tsunami.gui.context import AppContext
from tsunami.gui.shell import ActivityItem, Shell


def summarize(found: list[tuple[int, str, str]]) -> list[str]:
    """Summary lines such as '2 secret() calls on lines 4, 9'."""
    out = []
    secrets = [n for n, kind, _ in found if kind == "secret"]
    if secrets:
        lines = ", ".join(str(n) for n in secrets)
        plural = "s" if len(secrets) != 1 else ""
        out.append(f"{len(secrets)} secret() call{plural} on lines {lines}")
    for number, kind, detail in found:
        if kind == "destructive":
            out.append(f"destructive command '{detail}' on line {number}")
    return out


def _confirm(parent: QWidget | None, title: str, text: str) -> bool:
    return QMessageBox.question(parent, title, text) == QMessageBox.StandardButton.Yes


def _selection(block, color: str, background: bool = False) -> QTextEdit.ExtraSelection:
    sel = QTextEdit.ExtraSelection()
    sel.cursor = QTextCursor(block)
    sel.cursor.select(QTextCursor.SelectionType.LineUnderCursor)
    fmt = sel.format
    if background:
        c = QColor(color)
        c.setAlpha(70)
        fmt.setBackground(c)
    else:
        fmt.setForeground(QColor(color))
    sel.format = fmt
    return sel


def _code_view(text: str) -> QPlainTextEdit:
    view = QPlainTextEdit()
    view.setReadOnly(True)
    view.setFont(theme.mono_font())
    view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
    view.setPlainText(text)
    return view


class DraftView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell, draft: Draft) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self.draft = draft
        root = ctx.workspace_path
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        title = QLabel(f"Draft: {draft.path.as_posix()}")
        title.setObjectName("Title")
        layout.addWidget(title)
        where = (
            f"Replaces the live file {draft.live_path.as_posix()}"
            if draft.live_exists
            else f"New file; will be created at {draft.live_path.as_posix()}"
        )
        sub = QLabel(where)
        sub.setObjectName("Muted")
        layout.addWidget(sub)

        try:
            text = (root / draft.path).read_text(encoding="utf-8")
            diff = drafts_core.unified_diff(root, draft)
        except (OSError, UnicodeDecodeError, TsunamiError) as exc:
            text, diff = "", [f"cannot read draft: {exc}"]
        self.found = drafts_core.highlights(text)
        lines = summarize(self.found) or ["No secret() calls or destructive commands."]
        self.summary = QLabel("\n".join(lines))
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)

        p = theme.current()
        self.diff_view = _code_view("\n".join(diff))
        sels = []
        block = self.diff_view.document().firstBlock()
        while block.isValid():
            line = block.text()
            if line.startswith(("+++", "---", "@@")):
                sels.append(_selection(block, p.muted))
            elif line.startswith("+"):
                sels.append(_selection(block, p.success))
            elif line.startswith("-"):
                sels.append(_selection(block, p.error))
            block = block.next()
        self.diff_view.setExtraSelections(sels)
        layout.addWidget(self.diff_view, 1)

        self.file_view = _code_view(text)
        sels = []
        for number, kind, _ in self.found:
            block = self.file_view.document().findBlockByNumber(number - 1)
            if block.isValid():
                color = p.warning if kind == "secret" else p.error
                sels.append(_selection(block, color, background=True))
        self.file_view.setExtraSelections(sels)
        layout.addWidget(self.file_view, 1)

        buttons = QHBoxLayout()
        buttons.addStretch(1)
        self.vscode_button = QPushButton("Open in VS Code")
        self.delete_button = QPushButton("Delete")
        self.delete_button.setProperty("danger", "true")
        self.promote_button = QPushButton("Promote")
        self.promote_button.setProperty("primary", "true")
        for b in (self.vscode_button, self.delete_button, self.promote_button):
            buttons.addWidget(b)
        layout.addLayout(buttons)
        self.vscode_button.clicked.connect(lambda: self.shell.open_in_vscode(root / draft.path))
        self.delete_button.clicked.connect(self.delete)
        self.promote_button.clicked.connect(self.promote)

    def _key(self) -> str:
        return f"draft:{self.draft.path.as_posix()}"

    def promote(self) -> None:
        d = self.draft
        live = d.live_path.as_posix()
        if d.live_exists:
            ok = _confirm(
                self,
                "Overwrite live file",
                f"{live} already exists. Promoting this draft will REPLACE it, and the "
                "current live version cannot be recovered from here.\n\nOverwrite it?",
            )
        else:
            ok = _confirm(self, "Promote draft", f"Move this draft to {live}?")
        if not ok:
            return
        try:
            drafts_core.promote(self.ctx.workspace_path, d.path, overwrite=d.live_exists)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return
        self.ctx.reload_workspace(block=False)
        self.shell.notify(f"Promoted to {live}")
        self.shell.close_tab(self._key())

    def delete(self) -> None:
        d = self.draft
        if not _confirm(self, "Delete draft", f"Delete the draft {d.path.as_posix()}?"):
            return
        try:
            drafts_core.delete(self.ctx.workspace_path, d.path)
        except TsunamiError as exc:
            self.shell.notify(str(exc), "error")
            return
        self.ctx.reload_workspace(block=False)
        self.shell.close_tab(self._key())


class DraftsView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 4, 8, 8)
        self.list = QListWidget()
        layout.addWidget(self.list, 1)
        self.empty = QLabel("No drafts. Claude's proposed plays and snippets appear here.")
        self.empty.setObjectName("Muted")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.empty)
        self.list.itemClicked.connect(self._open)
        self.list.itemActivated.connect(self._open)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def _status_icon(self, draft: Draft):
        p = theme.current()
        issues = self.ctx.workspace.issues_for(draft.path) if self.ctx.workspace else []
        if any(i.severity == Severity.ERROR for i in issues):
            return icons.icon("error", p.error), "has errors"
        if issues:
            return icons.icon("warning", p.warning), "has warnings"
        return icons.icon("check", p.success), "valid"

    def refresh(self) -> None:
        self.list.clear()
        root = self.ctx.workspace_path
        found = drafts_core.list_drafts(root) if root else []
        for kind, heading in (("play", "Plays"), ("snippet", "Snippets")):
            group = [d for d in found if d.kind == kind]
            if not group:
                continue
            header = QListWidgetItem(heading.upper())
            header.setFlags(Qt.ItemFlag.NoItemFlags)
            self.list.addItem(header)
            for d in group:
                note = "changes live file" if d.live_exists else "new"
                icon, status = self._status_icon(d)
                item = QListWidgetItem(icon, f"{Path(*d.path.parts[2:]).as_posix()}  ({note})")
                item.setToolTip(f"{d.path.as_posix()} - {status}")
                item.setData(Qt.ItemDataRole.UserRole, d)
                self.list.addItem(item)
        self.empty.setVisible(not found)
        self.list.setVisible(bool(found))

    def _open(self, item: QListWidgetItem) -> None:
        draft = item.data(Qt.ItemDataRole.UserRole)
        if draft is not None:
            self.open_draft(draft)

    def open_draft(self, draft: Draft) -> QWidget:
        return self.shell.open_tab(
            f"draft:{draft.path.as_posix()}",
            f"Draft: {draft.path.name}",
            lambda: DraftView(self.ctx, self.shell, draft),
            "edit",
        )


def _factory(ctx: AppContext, shell: Shell) -> QWidget:
    return DraftsView(ctx, shell)


VIEW = ActivityItem("drafts", "Drafts", "edit", _factory, order=40)
