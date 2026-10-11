"""Main window: activity bar, side bar, tabbed editor area, bottom panel, status bar.

Laid out like VS Code. Views plug in through ActivityItem (gui/shell.py); editor tabs are
opened by key so a device or job opens once. The bottom panel holds Problems (every
validation issue, double-click opens VS Code at the line) and Output (the redacted app
log). The status bar shows the workspace, issue counts, MCP state, pending approvals,
running jobs, alarms and the Windows user.

See design.md, GUI: Layout, Views, Behavior rules.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from PySide6.QtCore import QByteArray, Qt, QTimer, QUrl
from PySide6.QtGui import QDesktopServices, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QButtonGroup,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QMessageBox,
    QPushButton,
    QSplitter,
    QStackedWidget,
    QSystemTrayIcon,
    QTabWidget,
    QToolButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from tsunami import __version__
from tsunami.core.issues import Severity, TsunamiError
from tsunami.core.runner import RunRequest
from tsunami.gui import icons, theme
from tsunami.gui.approvals_poller import ApprovalsPoller
from tsunami.gui.context import AppContext
from tsunami.gui.palette import PaletteItem, PalettePopup
from tsunami.gui.runner_thread import RunnerThread
from tsunami.gui.shell import ActivityItem
from tsunami.gui.widgets.masked_text import MaskedTextView


class _ClickLabel(QLabel):
    def __init__(self, text: str = "", on_click: Callable[[], None] | None = None) -> None:
        super().__init__(text)
        self._on_click = on_click
        if on_click:
            self.setCursor(Qt.CursorShape.PointingHandCursor)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802 - Qt API
        if self._on_click:
            self._on_click()
        super().mouseReleaseEvent(event)

    def set_flag(self, name: str, on: bool) -> None:
        self.setProperty(name, on)
        self.style().unpolish(self)
        self.style().polish(self)


class ProblemsPanel(QTreeWidget):
    def __init__(self, shell: MainWindow) -> None:
        super().__init__()
        self.shell = shell
        self.setHeaderLabels(["", "Problem", "File"])
        self.setRootIsDecorated(False)
        self.setColumnWidth(0, 28)
        self.setColumnWidth(1, 560)
        self.itemActivated.connect(self._open)

    def refresh(self) -> None:
        self.clear()
        workspace = self.shell.ctx.workspace
        if workspace is None:
            return
        p = theme.current()
        for issue in workspace.issues:
            error = issue.severity == Severity.ERROR
            item = QTreeWidgetItem(["", issue.message, _where(issue.file, issue.line)])
            item.setIcon(
                0, icons.icon("error" if error else "warning", p.error if error else p.warning)
            )
            item.setData(0, Qt.ItemDataRole.UserRole, (issue.file, issue.line))
            self.addTopLevelItem(item)

    def _open(self, item: QTreeWidgetItem) -> None:
        file, line = item.data(0, Qt.ItemDataRole.UserRole)
        if file is not None:
            self.shell.open_in_vscode(Path(file), line)


class OutputPanel(MaskedTextView):
    """Tails logs/app.log, which is already redacted by the logging formatter."""

    def __init__(self, log_file: Path) -> None:
        super().__init__()
        self.log_file = log_file
        self._offset = log_file.stat().st_size if log_file.is_file() else 0
        self._timer = QTimer(self)
        self._timer.setInterval(1000)
        self._timer.timeout.connect(self._tail)
        self._timer.start()

    def _tail(self) -> None:
        if not self.log_file.is_file():
            return
        size = self.log_file.stat().st_size
        if size < self._offset:  # rotated
            self._offset = 0
        if size == self._offset:
            return
        with self.log_file.open("r", encoding="utf-8", errors="replace") as handle:
            handle.seek(self._offset)
            chunk = handle.read()
            self._offset = handle.tell()
        self.append_text(chunk)


class WelcomePage(QWidget):
    def __init__(self, shell: MainWindow) -> None:
        super().__init__()
        layout = QVBoxLayout(self)
        layout.setContentsMargins(48, 48, 48, 48)
        title = QLabel("Tsunami")
        title.setObjectName("Title")
        subtitle = QLabel(
            f"Version {__version__}. Push templated config to Cisco and Aruba switches."
        )
        subtitle.setObjectName("Muted")
        layout.addWidget(title)
        layout.addWidget(subtitle)
        layout.addSpacing(16)
        for text, command in (
            ("Open workspace…", "workspace.open"),
            ("Init workspace…", "workspace.init"),
            ("Command palette    Ctrl+Shift+P", "palette.show"),
            ("Quick open    Ctrl+P", "quickopen.show"),
        ):
            button = QPushButton(text)
            button.setFlat(True)
            button.setStyleSheet("text-align: left; padding: 4px 0;")
            button.clicked.connect(lambda _=False, c=command: shell.run_command(c))
            layout.addWidget(button, 0, Qt.AlignmentFlag.AlignLeft)
        layout.addStretch(1)


class MainWindow(QMainWindow):
    def __init__(self, ctx: AppContext, views: Sequence[ActivityItem] = ()) -> None:
        super().__init__()
        self.ctx = ctx
        self.setWindowTitle("Tsunami")
        self.resize(1360, 860)
        self._commands: dict[str, PaletteItem] = {}
        self._tabs: dict[str, QWidget] = {}
        self._threads: list[RunnerThread] = []
        self._views: dict[str, QWidget] = {}
        self._view_items = sorted(views, key=lambda v: (v.bottom, v.order))
        self._build()
        self._register_builtin_commands()
        self._restore_layout()
        ctx.workspace_changed.connect(self._on_workspace_changed)
        ctx.workspace_loading.connect(self._on_loading)
        ctx.alarms_changed.connect(self._refresh_alarms)
        ctx.jobs_changed.connect(self._refresh_alarms)
        self.poller = ApprovalsPoller(ctx)
        self.poller.pending_changed.connect(self._on_pending)
        self.poller.new_request.connect(self._on_new_request)
        self.tray = QSystemTrayIcon(icons.icon("pulse", theme.current().accent), self)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()
        if ctx.workspace is not None or ctx.last_error:
            self._on_workspace_changed()  # the context may have loaded before the window
        self._refresh_alarms()

    # -- layout --------------------------------------------------------------------

    def _build(self) -> None:
        p = theme.current()
        central = QWidget()
        root = QHBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        self.activity = QFrame(objectName="ActivityBar")
        bar = QVBoxLayout(self.activity)
        bar.setContentsMargins(0, 4, 0, 4)
        bar.setSpacing(0)
        self._activity_group = QButtonGroup(self)
        self._activity_group.setExclusive(True)
        self.side_stack = QStackedWidget()
        top_added = False
        for item in self._view_items:
            if item.bottom and not top_added:
                bar.addStretch(1)
                top_added = True
            button = QToolButton()
            button.setCheckable(True)
            button.setIcon(icons.icon(item.icon, p.muted))
            button.setIconSize(button.iconSize() * 1.6)
            button.setToolTip(item.title)
            button.clicked.connect(lambda _=False, v=item.view_id: self.show_view(v))
            self._activity_group.addButton(button)
            bar.addWidget(button)
            item_button = button
            page = QWidget(objectName="SideBar")
            page_layout = QVBoxLayout(page)
            page_layout.setContentsMargins(0, 0, 0, 0)
            page_layout.setSpacing(0)
            header = QLabel(item.title.upper(), objectName="SideBarHeader")
            page_layout.addWidget(header)
            view = item.factory(self.ctx, self)
            page_layout.addWidget(view, 1)
            self.side_stack.addWidget(page)
            self._views[item.view_id] = page
            page.setProperty("activity_button", item_button)
        if not top_added:
            bar.addStretch(1)

        self.editor = QTabWidget(objectName="Editor")
        self.editor.setTabsClosable(False)  # each tab gets a Codicon close button
        self.editor.setMovable(True)
        self.editor.setDocumentMode(True)
        self.editor.tabCloseRequested.connect(self._close_tab_index)
        self.welcome = WelcomePage(self)

        self.panel = QTabWidget(objectName="Panel")
        self.panel.setDocumentMode(True)
        self.problems = ProblemsPanel(self)
        self.output = OutputPanel(self.ctx.paths.logs_dir / "app.log")
        self.panel.addTab(self.problems, "Problems")
        self.panel.addTab(self.output, "Output")

        self.editor_stack = QStackedWidget()
        self.editor_stack.addWidget(self.welcome)
        self.editor_stack.addWidget(self.editor)
        self.vertical = QSplitter(Qt.Orientation.Vertical)
        self.vertical.addWidget(self.editor_stack)
        self.vertical.addWidget(self.panel)
        self.vertical.setStretchFactor(0, 4)
        self.vertical.setStretchFactor(1, 1)
        self.horizontal = QSplitter(Qt.Orientation.Horizontal)
        self.horizontal.addWidget(self.side_stack)
        self.horizontal.addWidget(self.vertical)
        self.horizontal.setStretchFactor(1, 1)
        self.horizontal.setSizes([300, 1000])

        root.addWidget(self.activity)
        root.addWidget(self.horizontal, 1)
        self.setCentralWidget(central)

        status = self.statusBar()
        status.setSizeGripEnabled(False)
        self.status_workspace = _ClickLabel("No workspace", lambda: self.show_view("workspace"))
        self.status_issues = _ClickLabel("", self.show_problems)
        self.status_mcp = _ClickLabel("MCP: off", lambda: self.run_command("settings.open"))
        self.status_approvals = _ClickLabel("", lambda: self.show_view("approvals"))
        self.status_jobs = _ClickLabel("")
        self.status_alarm = _ClickLabel("", lambda: self.show_view("history"))
        self.status_user = QLabel(self.ctx.windows_user())
        for label in (self.status_workspace, self.status_issues):
            status.addWidget(label)
        for label in (
            self.status_alarm,
            self.status_jobs,
            self.status_approvals,
            self.status_mcp,
            self.status_user,
        ):
            status.addPermanentWidget(label)
        self._update_editor_stack()

    # -- Shell protocol ------------------------------------------------------------------

    def open_tab(
        self, key: str, title: str, factory: Callable[[], QWidget], icon: str | None = None
    ) -> QWidget:
        if key in self._tabs:
            widget = self._tabs[key]
            self.editor.setCurrentWidget(widget)
            return widget
        widget = factory()
        widget.setProperty("tab_key", key)
        index = self.editor.addTab(widget, title)
        if icon:
            self.editor.setTabIcon(index, icons.icon(icon, theme.current().muted))
        close = QToolButton()
        close.setAutoRaise(True)
        close.setIcon(icons.icon("close", theme.current().muted))
        close.setToolTip("Close (Ctrl+W)")
        close.clicked.connect(lambda _=False, k=key: self.close_tab(k))
        self.editor.tabBar().setTabButton(
            index, self.editor.tabBar().ButtonPosition.RightSide, close
        )
        self.editor.setCurrentIndex(index)
        self._tabs[key] = widget
        self._update_editor_stack()
        return widget

    def close_tab(self, key: str) -> None:
        widget = self._tabs.pop(key, None)
        if widget is not None:
            self.editor.removeTab(self.editor.indexOf(widget))
            widget.deleteLater()
        self._update_editor_stack()

    def show_view(self, view_id: str) -> None:
        page = self._views.get(view_id)
        if page is None:
            return
        self.side_stack.setCurrentWidget(page)
        page.property("activity_button").setChecked(True)
        self.side_stack.show()

    def register_command(
        self, command_id: str, title: str, callback: Callable[[], None], shortcut: str | None = None
    ) -> None:
        self._commands[command_id] = PaletteItem(title, callback, shortcut or "")
        if shortcut:
            QShortcut(QKeySequence(shortcut), self, activated=callback)

    def run_command(self, command_id: str) -> None:
        command = self._commands.get(command_id)
        if command is not None:
            command.callback()

    def notify(self, message: str, level: str = "info") -> None:
        self.statusBar().showMessage(message, 8000)
        if level == "error":
            QMessageBox.warning(self, "Tsunami", message)

    def start_run(self, request: RunRequest, title: str) -> RunnerThread:
        thread = RunnerThread(self.ctx, request, self)
        self._threads.append(thread)
        thread.finished.connect(
            lambda: self._threads.remove(thread) if thread in self._threads else None
        )
        thread.finished.connect(self._refresh_jobs_label)
        key = f"run:{id(thread)}"
        self.open_tab(key, title, lambda: _job_view(self.ctx, self, thread=thread), "play")
        thread.start()
        self._refresh_jobs_label()
        return thread

    def open_job(self, job_id: str) -> None:
        running = next((t for t in self._threads if t.job_id == job_id), None)
        if running is not None:
            for key, widget in self._tabs.items():
                if key == f"run:{id(running)}":
                    self.editor.setCurrentWidget(widget)
                    return
        self.open_tab(
            f"job:{job_id}",
            f"Job {job_id[:8]}",
            lambda: _job_view(self.ctx, self, job_id=job_id),
            "history",
        )

    def open_in_vscode(self, path: Path, line: int | None = None) -> None:
        if not path.is_absolute() and self.ctx.workspace_path is not None:
            path = self.ctx.workspace_path / path
        code = shutil.which("code")
        if code:
            target = f"{path}:{line}" if line else str(path)
            subprocess.Popen([code, "-g", target], close_fds=True)  # noqa: S603
        else:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def show_problems(self) -> None:
        self.panel.show()
        self.panel.setCurrentWidget(self.problems)

    # -- commands ---------------------------------------------------------------------

    def _register_builtin_commands(self) -> None:
        self.register_command(
            "palette.show", "Show all commands", self._show_palette, "Ctrl+Shift+P"
        )
        self.register_command(
            "quickopen.show", "Go to device, play or job", self._show_quick_open, "Ctrl+P"
        )
        self.register_command("workspace.open", "Workspace: Open…", self._open_workspace_dialog)
        self.register_command("workspace.init", "Workspace: Init…", self._init_workspace_dialog)
        self.register_command(
            "workspace.reload", "Workspace: Reload", self.ctx.reload_workspace, "F5"
        )
        self.register_command("view.panel", "View: Toggle panel", self._toggle_panel, "Ctrl+J")
        self.register_command(
            "view.sidebar", "View: Toggle side bar", self._toggle_sidebar, "Ctrl+B"
        )
        self.register_command("view.problems", "View: Problems", self.show_problems, "Ctrl+Shift+M")
        self.register_command("tab.close", "Close tab", self._close_current_tab, "Ctrl+W")
        for item in self._view_items:
            self.register_command(
                f"view.{item.view_id}",
                f"View: {item.title}",
                lambda v=item.view_id: self.show_view(v),
            )

    def _show_palette(self) -> None:
        PalettePopup(
            sorted(self._commands.values(), key=lambda c: c.title), "Type a command", self
        ).exec()

    def _show_quick_open(self) -> None:
        items: list[PaletteItem] = []
        workspace = self.ctx.workspace
        if workspace is not None:
            for slug, cust in workspace.customers.items():
                for device in cust.devices:
                    items.append(
                        PaletteItem(
                            f"{device.name}  ({slug})",
                            lambda c=slug, d=device.name: self._open_device(c, d),
                            "device",
                        )
                    )
            for path in workspace.plays:
                items.append(
                    PaletteItem(path.as_posix(), lambda p=path: self._open_play(p), "play")
                )
        for job in self.ctx.history.list_jobs(limit=30):
            items.append(
                PaletteItem(
                    f"Job {job.id[:8]} {job.play_path} ({job.status})",
                    lambda j=job.id: self.open_job(j),
                    "job",
                )
            )
        PalettePopup(items, "Go to device, play or job", self).exec()

    def _open_device(self, customer: str, device: str) -> None:
        self.run_command_with("inventory.open_device", customer, device)

    def _open_play(self, path: Path) -> None:
        self.run_command_with("plays.open_play", path)

    def run_command_with(self, command_id: str, *args: Any) -> None:
        """Commands that take arguments are registered as attributes by their views."""
        handler = getattr(self, f"_handler_{command_id.replace('.', '_')}", None)
        if handler is not None:
            handler(*args)

    def set_handler(self, command_id: str, handler: Callable[..., None]) -> None:
        setattr(self, f"_handler_{command_id.replace('.', '_')}", handler)

    def _open_workspace_dialog(self) -> None:
        folder = QFileDialog.getExistingDirectory(self, "Open workspace")
        if folder:
            self.ctx.open_workspace(folder)

    def _init_workspace_dialog(self) -> None:
        from tsunami.core.scaffold import init_workspace

        folder = QFileDialog.getExistingDirectory(self, "Init workspace in folder")
        if not folder:
            return
        try:
            init_workspace(Path(folder), name=Path(folder).name)
        except TsunamiError as exc:
            self.notify(str(exc), "error")
            return
        self.ctx.open_workspace(folder)

    def _toggle_panel(self) -> None:
        self.panel.setVisible(not self.panel.isVisible())

    def _toggle_sidebar(self) -> None:
        self.side_stack.setVisible(not self.side_stack.isVisible())

    def _close_current_tab(self) -> None:
        widget = self.editor.currentWidget()
        if widget is not None:
            self.close_tab(widget.property("tab_key"))

    def _close_tab_index(self, index: int) -> None:
        widget = self.editor.widget(index)
        if widget is not None:
            self.close_tab(widget.property("tab_key"))

    # -- state updates ------------------------------------------------------------------

    def _update_editor_stack(self) -> None:
        self.editor_stack.setCurrentWidget(self.editor if self.editor.count() else self.welcome)

    def _on_loading(self, loading: bool) -> None:
        if loading:
            self.status_workspace.setText("Loading workspace…")

    def _on_workspace_changed(self) -> None:
        ctx = self.ctx
        if ctx.last_error:
            self.notify(f"Workspace failed to load: {ctx.last_error}", "error")
        workspace = ctx.workspace
        if workspace is None:
            self.status_workspace.setText("No workspace")
            self.status_issues.setText("")
            return
        name = workspace.config.name if workspace.config else ctx.workspace_path.name
        self.status_workspace.setText(name)
        self.setWindowTitle(f"{name} — Tsunami")
        self.status_issues.setText(
            f"{len(workspace.errors)} errors  {len(workspace.warnings)} warnings"
        )
        self.status_issues.set_flag("attention", bool(workspace.errors))
        self.problems.refresh()
        self._refresh_mcp()

    def _refresh_mcp(self) -> None:
        text = "MCP: off"
        policy = self.ctx.paths.policy_file
        if policy.is_file():
            try:
                from ruamel.yaml import YAML

                data = YAML(typ="safe").load(policy.read_text(encoding="utf-8")) or {}
                if data.get("enabled"):
                    text = "MCP: lab only" if data.get("apply") == "lab_only" else "MCP: no apply"
            except Exception:  # noqa: BLE001 - a broken policy file shows as off
                text = "MCP: policy error"
        self.status_mcp.setText(text)

    def _on_pending(self, count: int) -> None:
        self.status_approvals.setText(f"{count} awaiting approval" if count else "")
        self.status_approvals.set_flag("attention", count > 0)

    def _on_new_request(self, job_id: str) -> None:
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.showMessage(
                "Tsunami", "Claude is requesting a change. Review it in Approvals."
            )

    def _refresh_jobs_label(self) -> None:
        running = sum(1 for t in self._threads if t.isRunning())
        self.status_jobs.setText(f"{running} running" if running else "")

    def _refresh_alarms(self) -> None:
        alarms = self.ctx.unacknowledged_alarms()
        self.status_alarm.setText(f"{len(alarms)} job(s) need attention" if alarms else "")
        self.status_alarm.set_flag("alarm", bool(alarms))

    # -- persistence ---------------------------------------------------------------------

    def _restore_layout(self) -> None:
        settings = self.ctx.qsettings
        geometry = settings.value("window/geometry")
        if isinstance(geometry, QByteArray):
            self.restoreGeometry(geometry)
        for name, splitter in (("horizontal", self.horizontal), ("vertical", self.vertical)):
            state = settings.value(f"window/{name}")
            if isinstance(state, QByteArray):
                splitter.restoreState(state)
        last = settings.value("window/view", "workspace")
        self.show_view(last if last in self._views else next(iter(self._views), ""))

    def closeEvent(self, event) -> None:  # noqa: N802 - Qt API
        running = [t for t in self._threads if t.isRunning()]
        if running:
            answer = QMessageBox.question(
                self, "Tsunami", f"{len(running)} run(s) still in progress. Cancel them and quit?"
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            for thread in running:
                thread.cancel()
            for thread in running:
                thread.wait(30000)
        settings = self.ctx.qsettings
        settings.setValue("window/geometry", self.saveGeometry())
        settings.setValue("window/horizontal", self.horizontal.saveState())
        settings.setValue("window/vertical", self.vertical.saveState())
        current = self.side_stack.currentWidget()
        for view_id, page in self._views.items():
            if page is current:
                settings.setValue("window/view", view_id)
        self.poller.stop()
        super().closeEvent(event)


def _ask_canary(device: str) -> bool:
    answer = QMessageBox.question(
        None, "Canary finished", f"{device} is done. Continue with the remaining devices?"
    )
    return answer == QMessageBox.StandardButton.Yes


def _where(file: Path | None, line: int | None) -> str:
    if file is None:
        return ""
    return f"{Path(file).as_posix()}:{line}" if line else Path(file).as_posix()


def _job_view(ctx: AppContext, shell: MainWindow, **kwargs: Any) -> QWidget:
    """The job view from screens.job_view, or a minimal log until it exists."""
    try:
        from tsunami.gui.screens.job_view import make_job_view
    except ImportError:
        return _MinimalJobView(ctx, **kwargs)
    return make_job_view(ctx, shell, **kwargs)


class _MinimalJobView(MaskedTextView):
    def __init__(
        self, ctx: AppContext, thread: RunnerThread | None = None, job_id: str | None = None
    ):
        super().__init__()
        if thread is not None:
            thread.events.connect(lambda events: [self.append_text(repr(e)) for e in events])
            thread.canary_requested.connect(
                lambda device: thread.answer_canary(_ask_canary(device))
            )
        if job_id is not None:
            for device in ctx.history.devices(job_id):
                self.append_text(f"{device.device}: {device.status}")
