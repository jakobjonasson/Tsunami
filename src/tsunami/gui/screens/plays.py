"""Plays view: non-draft plays grouped by type then customer, and the play tab.

See design.md, GUI: Views; Play schema.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMenu,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami.core.issues import Severity, TsunamiError
from tsunami.core.models.play import GLOBAL_CUSTOMER
from tsunami.core.play_edit import copy_play
from tsunami.gui import theme, wheel
from tsunami.gui.context import AppContext
from tsunami.gui.icons import icon
from tsunami.gui.shell import ActivityItem, Shell

_PATH_ROLE = Qt.ItemDataRole.UserRole
_GROUPS = (("initial", "Initial"), ("push", "Push"), ("baseline", "Baseline"))


def run_dialog(dialog: QDialog) -> bool:
    """Show a dialog modally. True if accepted. Tests replace this."""
    return dialog.exec() == QDialog.DialogCode.Accepted


class CopyPlayDialog(QDialog):
    """Copy a live play for another customer, or as a global play that runs for any
    customer (listed first). Snippets are not copied."""

    def __init__(self, ctx: AppContext, path: Path, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.ctx = ctx
        self.path = path
        self.new_path: Path | None = None
        self.setWindowTitle(f"Copy {path.stem}")
        self.setMinimumWidth(440)
        ws = ctx.workspace
        entry = ws.plays.get(path) if ws else None
        own = entry.play.customer if entry and entry.play else None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 16, 16, 16)
        layout.setSpacing(10)
        layout.addWidget(
            QLabel(f"Copy {path.as_posix()} for another customer, or as a global play.")
        )
        form = QFormLayout()
        self.customer_combo = wheel.ComboBox()
        self._own = own
        if own != GLOBAL_CUSTOMER:
            self.customer_combo.addItem("Global (any customer)", GLOBAL_CUSTOMER)
        for name in sorted(ws.customers if ws else ()):
            if name != own:
                self.customer_combo.addItem(name, name)
        form.addRow("Customer", self.customer_combo)
        self.name_edit = QLineEdit()
        form.addRow("File name", self.name_edit)
        layout.addLayout(form)
        note = QLabel(
            "Snippets are not copied: customer-level snippets the play uses must already "
            "exist for the new customer."
        )
        note.setObjectName("Muted")
        note.setWordWrap(True)
        layout.addWidget(note)
        self.error_label = QLabel("")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet(f"color: {theme.current().error};")
        self.error_label.setVisible(False)
        layout.addWidget(self.error_label)
        self.buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        ok = self.buttons.button(QDialogButtonBox.StandardButton.Ok)
        ok.setText("Copy")
        ok.setProperty("primary", True)
        ok.setEnabled(self.customer_combo.count() > 0)
        self.buttons.accepted.connect(self.accept)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)
        self.customer_combo.currentIndexChanged.connect(self._customer_changed)
        self._customer_changed()

    def _customer_changed(self, _index: int = 0) -> None:
        customer = self.customer_combo.currentData()
        if customer == GLOBAL_CUSTOMER:
            self.name_edit.setText(self._global_name())
        elif customer:
            self.name_edit.setText(f"{self.path.stem}-{customer}")

    def _global_name(self) -> str:
        """The file name without the customer in it ("acme-baseline" -> "baseline"), or
        with "-global" when that name is taken."""
        stem = self.path.stem
        if self._own and stem.startswith(f"{self._own}-"):
            stem = stem.removeprefix(f"{self._own}-")
        elif self._own and stem.endswith(f"-{self._own}"):
            stem = stem.removesuffix(f"-{self._own}")
        if (self.path.parent / f"{stem}.yml") == self.path or self._taken(stem):
            stem = f"{stem}-global"
        return stem

    def _taken(self, stem: str) -> bool:
        ws = self.ctx.workspace
        return ws is not None and (ws.root / self.path.parent / f"{stem}.yml").exists()

    def accept(self) -> None:
        root = self.ctx.workspace.root if self.ctx.workspace else Path()
        try:
            self.new_path = copy_play(
                root,
                self.path,
                self.customer_combo.currentData(),
                self.name_edit.text().strip() or None,
            )
        except TsunamiError as exc:
            self.error_label.setText(str(exc))
            self.error_label.setVisible(True)
            return
        super().accept()


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
        self.tree.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.tree.customContextMenuRequested.connect(self._show_menu)
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
            for entry in sorted(
                entries,
                key=lambda e: (
                    e.play.customer != GLOBAL_CUSTOMER if e.play else True,
                    e.play.customer if e.play else "",
                    e.path,
                ),
            ):
                customer = entry.play.customer if entry.play else "(invalid)"
                if customer not in customers:
                    label = "Global" if customer == GLOBAL_CUSTOMER else customer
                    customers[customer] = QTreeWidgetItem(group, [label])
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

    # -- context menu -------------------------------------------------------------------

    def _show_menu(self, pos: Any) -> None:
        item = self.tree.itemAt(pos)
        if item is None or item.data(0, _PATH_ROLE) is None:
            return
        self.menu_for(item).exec(self.tree.viewport().mapToGlobal(pos))

    def menu_for(self, item: QTreeWidgetItem | None) -> QMenu:
        """The context menu for a play item; empty for groups and empty space."""
        menu = QMenu(self)
        path = item.data(0, _PATH_ROLE) if item is not None else None
        if path is None:
            return menu
        colors = theme.current()
        ws = self.ctx.workspace
        entry = ws.plays.get(path) if ws else None
        has_errors = ws is not None and any(
            i.severity == Severity.ERROR for i in ws.issues_for(path)
        )
        runnable = entry is not None and entry.play is not None and not has_errors

        def add(text: str, icon_name: str, slot: Any, enabled: bool = True) -> None:
            action = menu.addAction(icon(icon_name, colors.text), text)
            action.setEnabled(enabled)
            action.triggered.connect(lambda _=False: slot(path))

        add("Open", "play", lambda p: open_play(self.ctx, self.shell, p))
        add("Edit in VS Code", "edit", self.edit_in_vscode)
        add("Run…", "play", self.run_play, runnable)
        add("Copy to customer…", "copy", self.copy_to_customer, entry is not None)
        menu.addSeparator()
        add("Delete…", "trash", self.delete_play)
        return menu

    def _absolute(self, path: Path) -> Path:
        root = self.ctx.workspace.root if self.ctx.workspace else Path()
        return root / path

    def edit_in_vscode(self, path: Path) -> None:
        self.shell.open_in_vscode(self._absolute(path))

    def run_play(self, path: Path) -> None:
        from tsunami.gui.screens.run_wizard import open_run_wizard

        open_run_wizard(self.ctx, self.shell, path)

    def copy_to_customer(self, path: Path) -> None:
        dialog = CopyPlayDialog(self.ctx, path, self)
        if dialog.customer_combo.count() == 0:
            self.shell.notify("There is no other customer to copy the play to.", "warning")
            return
        if not run_dialog(dialog) or dialog.new_path is None:
            return
        self.shell.refresh_all()
        open_play(self.ctx, self.shell, dialog.new_path)
        self.shell.notify(f"Copied to {dialog.new_path.as_posix()}.")

    def delete_play(self, path: Path) -> None:
        from tsunami.core import inventory_edit

        answer = QMessageBox.question(
            self,
            f"Delete play {path.stem}",
            f"This moves {path.as_posix()} to the Recycle Bin. You can restore it from there.",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            inventory_edit.send_to_recycle_bin(self._absolute(path))  # looked up at call time
        except Exception as exc:  # the shell call can fail in many OS-specific ways
            self.shell.notify(f"Could not delete {path.name}: {exc}", "error")
            return
        self.shell.close_tab(f"play:{path.as_posix()}")
        self.shell.close_tab(f"run-wizard:{path.as_posix()}")
        self.shell.refresh_all()
        self.shell.notify(f"Deleted play {path.stem}.")


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
