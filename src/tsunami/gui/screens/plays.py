"""Plays view: non-draft plays grouped by type then customer, and the play tab.

See design.md, GUI: Views; Play schema.
"""

from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.issues import Severity
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import ActivityItem, Shell

_PATH_ROLE = Qt.ItemDataRole.UserRole
_GROUPS = (("initial", "Initial"), ("push", "Push"), ("baseline", "Baseline"))


def _status_icon(ctx: AppContext, path: Path) -> QIcon:
    colors = theme.current()
    issues = ctx.workspace.issues_for(path) if ctx.workspace else []
    if any(i.severity == Severity.ERROR for i in issues):
        return icon("error", colors.error)
    if issues:
        return icon("warning", colors.warning)
    return icon("pass", colors.success)


class PlayView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell, path: Path) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        self.path = path
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(8)
        top = QHBoxLayout()
        self.title = QLabel(path.stem)
        self.title.setObjectName("Title")
        top.addWidget(self.title, 1)
        self.vscode_button = QPushButton(icon("edit", theme.current().text), "Open in VS Code")
        self.vscode_button.clicked.connect(self._open_vscode)
        top.addWidget(self.vscode_button)
        self.run_button = QPushButton("Run…")
        self.run_button.setProperty("primary", "true")
        self.run_button.clicked.connect(self._run)
        top.addWidget(self.run_button)
        layout.addLayout(top)
        self.subtitle = QLabel(path.as_posix())
        self.subtitle.setObjectName("Muted")
        layout.addWidget(self.subtitle)

        self.issue_list = QListWidget()
        self.issue_list.setFrameShape(QListWidget.Shape.NoFrame)
        self.issue_list.itemDoubleClicked.connect(self._open_issue)
        layout.addWidget(self.issue_list)
        self.yaml_view = QPlainTextEdit()
        self.yaml_view.setReadOnly(True)
        self.yaml_view.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self.yaml_view.setFont(theme.mono_font())
        layout.addWidget(self.yaml_view, 1)

        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def _absolute(self) -> Path:
        root = self.ctx.workspace.root if self.ctx.workspace else Path()
        return root / self.path

    def refresh(self) -> None:
        ws = self.ctx.workspace
        entry = ws.plays.get(self.path) if ws else None
        if entry is not None and entry.play is not None:
            self.title.setText(entry.play.name)
        try:
            self.yaml_view.setPlainText(self._absolute().read_text(encoding="utf-8"))
        except OSError as exc:
            self.yaml_view.setPlainText(f"Could not read {self.path}: {exc.strerror or exc}")
        self.run_button.setEnabled(entry is not None and entry.play is not None)
        colors = theme.current()
        self.issue_list.clear()
        issues = ws.issues_for(self.path) if ws else []
        for issue in issues:
            is_error = issue.severity == Severity.ERROR
            where = f"line {issue.line}: " if issue.line else ""
            item = QListWidgetItem(
                icon(
                    "error" if is_error else "warning",
                    colors.error if is_error else colors.warning,
                ),
                f"{where}{issue.message}",
            )
            item.setData(_PATH_ROLE, issue.line)
            item.setToolTip(str(issue))
            self.issue_list.addItem(item)
        self.issue_list.setVisible(bool(issues))
        self.issue_list.setMaximumHeight(min(160, 24 * len(issues) + 8))

    def _open_issue(self, item: QListWidgetItem) -> None:
        self.shell.open_in_vscode(self._absolute(), item.data(_PATH_ROLE))

    def _open_vscode(self) -> None:
        self.shell.open_in_vscode(self._absolute())

    def _run(self) -> None:
        try:
            from tsunami.gui.screens.run_wizard import open_run_wizard
        except ImportError:
            self.shell.notify("Run wizard not available", "warning")
            return
        open_run_wizard(self.ctx, self.shell, self.path)


class PlaysView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 4, 0, 0)
        self.hint = QLabel("No workspace is open.")
        self.hint.setObjectName("Muted")
        self.hint.setMargin(8)
        layout.addWidget(self.hint)
        self.tree = QTreeWidget()
        self.tree.setHeaderHidden(True)
        self.tree.setFrameShape(QTreeWidget.Shape.NoFrame)
        self.tree.itemDoubleClicked.connect(self._open_item)
        layout.addWidget(self.tree, 1)
        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def refresh(self) -> None:
        colors = theme.current()
        self.tree.clear()
        ws = self.ctx.workspace
        self.hint.setVisible(ws is None)
        self.tree.setVisible(ws is not None)
        if ws is None:
            return
        for type_id, title in _GROUPS:
            group = QTreeWidgetItem(self.tree, [title])
            group.setIcon(0, icon("folder", colors.muted))
            customers: dict[str, QTreeWidgetItem] = {}
            entries = [
                e
                for e in ws.plays.values()
                if not e.draft
                and (str(e.play.type) if e.play else (e.path.parts[1:2] or ("",))[0]) == type_id
            ]
            for entry in sorted(entries, key=lambda e: (e.play.customer if e.play else "", e.path)):
                customer = entry.play.customer if entry.play else "(invalid)"
                if customer not in customers:
                    customers[customer] = QTreeWidgetItem(group, [customer])
                name = entry.play.name if entry.play else entry.path.stem
                item = QTreeWidgetItem(customers[customer], [name])
                item.setIcon(0, _status_icon(self.ctx, entry.path))
                item.setData(0, _PATH_ROLE, entry.path)
                item.setToolTip(0, entry.path.as_posix())
            group.setHidden(group.childCount() == 0)
        self.tree.expandAll()

    def _open_item(self, item: QTreeWidgetItem) -> None:
        path = item.data(0, _PATH_ROLE)
        if path is not None:
            open_play(self.ctx, self.shell, path)


def open_play(ctx: AppContext, shell: Shell, path: Path | str) -> None:
    path = Path(path)
    ws = ctx.workspace
    if ws is not None and path.is_absolute():
        try:
            path = path.relative_to(ws.root)
        except ValueError:
            pass
    entry = ws.plays.get(path) if ws else None
    title = entry.play.name if entry and entry.play else path.stem
    shell.open_tab(f"play:{path.as_posix()}", title, lambda: PlayView(ctx, shell, path), "play")


def factory(ctx: AppContext, shell: Shell) -> QWidget:
    return PlaysView(ctx, shell)


def register(shell: Shell) -> None:
    shell.set_handler("plays.open_play", lambda path: open_play(shell.ctx, shell, path))


VIEW = ActivityItem("plays", "Plays", "play", factory, order=30)
