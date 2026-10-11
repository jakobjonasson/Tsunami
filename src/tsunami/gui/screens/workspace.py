"""Workspace view: open or init, summary, lint summary, reload.

See design.md, GUI: Views.
"""

from __future__ import annotations

from collections import defaultdict
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QListWidget,
    QListWidgetItem,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.issues import Issue, Severity
from tsunami.gui import theme
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import ActivityItem, Shell

_PATH_ROLE = Qt.ItemDataRole.UserRole
_LINE_ROLE = Qt.ItemDataRole.UserRole + 1
_NO_WORKSPACE = "No workspace is open. Open an existing one or create a new one."


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" + ("" if count == 1 else "s")


class WorkspaceView(QWidget):
    def __init__(self, ctx: AppContext, shell: Shell) -> None:
        super().__init__()
        self.ctx = ctx
        self.shell = shell
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        layout.setSpacing(8)

        self.name_label = QLabel()
        self.name_label.setObjectName("Title")
        self.name_label.setWordWrap(True)
        self.path_label = QLabel()
        self.path_label.setObjectName("Muted")
        self.path_label.setWordWrap(True)
        self.schema_label = QLabel()
        self.schema_label.setObjectName("Muted")
        self.counts_label = QLabel()
        self.counts_label.setWordWrap(True)
        self.hint_label = QLabel(_NO_WORKSPACE)
        self.hint_label.setObjectName("Muted")
        self.hint_label.setWordWrap(True)
        for w in (self.name_label, self.path_label, self.schema_label, self.counts_label):
            layout.addWidget(w)
        layout.addWidget(self.hint_label)

        text = theme.current().text
        self.open_button = self._button("Open…", "folder-opened", "workspace.open")
        self.init_button = self._button("Init…", "new-folder", "workspace.init")
        self.reload_button = self._button("Reload", "refresh", "workspace.reload")
        self.vscode_button = QPushButton(icon("edit", text), "Open folder in VS Code")
        self.vscode_button.clicked.connect(self._open_folder)
        row = QHBoxLayout()
        row.addWidget(self.open_button)
        row.addWidget(self.init_button)
        row.addWidget(self.reload_button)
        layout.addLayout(row)
        layout.addWidget(self.vscode_button)

        self.lint_title = QLabel("Lint")
        self.lint_title.setObjectName("Muted")
        layout.addWidget(self.lint_title)
        self.lint_list = QListWidget()
        self.lint_list.setFrameShape(QListWidget.Shape.NoFrame)
        self.lint_list.itemClicked.connect(self._open_issue_file)
        layout.addWidget(self.lint_list, 1)

        ctx.workspace_changed.connect(self.refresh)
        self.refresh()

    def _button(self, text: str, icon_name: str, command: str) -> QPushButton:
        button = QPushButton(icon(icon_name, theme.current().text), text)
        button.clicked.connect(lambda: self.shell.run_command(command))
        return button

    def _open_folder(self) -> None:
        if self.ctx.workspace_path is not None:
            self.shell.open_in_vscode(self.ctx.workspace_path)

    def _open_issue_file(self, item: QListWidgetItem) -> None:
        path, line = item.data(_PATH_ROLE), item.data(_LINE_ROLE)
        if path is not None:
            self.shell.open_in_vscode(Path(path), line)

    def refresh(self) -> None:
        ws = self.ctx.workspace
        for w in (
            self.name_label,
            self.path_label,
            self.schema_label,
            self.counts_label,
            self.reload_button,
            self.vscode_button,
            self.lint_title,
            self.lint_list,
        ):
            w.setVisible(ws is not None)
        self.hint_label.setVisible(ws is None)
        self.hint_label.setText(
            f"Could not load the workspace: {self.ctx.last_error}"
            if ws is None and self.ctx.last_error
            else _NO_WORKSPACE
        )
        self.lint_list.clear()
        if ws is None:
            return

        config = ws.config
        self.name_label.setText(config.name if config else (ws.root.name or "Workspace"))
        self.path_label.setText(str(self.ctx.workspace_path or ws.root))
        self.schema_label.setText(
            f"Schema version {config.schema_version}" if config else "Schema version unknown"
        )
        devices = sum(len(c.devices) for c in ws.customers.values())
        drafts = sum(1 for e in ws.plays.values() if e.draft)
        self.counts_label.setText(
            f"{_plural(len(ws.customers), 'customer')}, {_plural(devices, 'device')}, "
            f"{_plural(len(ws.plays) - drafts, 'play')}, {_plural(drafts, 'draft play')}"
        )

        by_file: dict[Path | None, list[Issue]] = defaultdict(list)
        for issue in ws.issues:
            by_file[issue.file].append(issue)
        colors = theme.current()
        if not by_file:
            self.lint_title.setText("Lint: no problems")
            return
        self.lint_title.setText(f"Lint: {_plural(len(ws.issues), 'problem')}")
        for file, issues in sorted(by_file.items(), key=lambda kv: str(kv[0] or "")):
            errors = sum(1 for i in issues if i.severity == Severity.ERROR)
            warnings = len(issues) - errors
            parts = []
            if errors:
                parts.append(_plural(errors, "error"))
            if warnings:
                parts.append(_plural(warnings, "warning"))
            label = file.as_posix() if file else "(workspace)"
            item = QListWidgetItem(
                icon("error" if errors else "warning", colors.error if errors else colors.warning),
                f"{label}  ({', '.join(parts)})",
            )
            lines = [i.line for i in issues if i.line]
            item.setData(_PATH_ROLE, str(ws.root / (file or "workspace.yml")))
            item.setData(_LINE_ROLE, min(lines) if lines else None)
            item.setToolTip("\n".join(str(i) for i in issues))
            self.lint_list.addItem(item)


def factory(ctx: AppContext, shell: Shell) -> QWidget:
    return WorkspaceView(ctx, shell)


VIEW = ActivityItem("workspace", "Workspace", "files", factory, order=10)
