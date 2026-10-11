"""The contract views use to talk to the main window, and how views are registered.

Each side-bar view module exposes VIEW, an ActivityItem. A module may also define
register(shell) to add command-palette commands. Views never import main_window.

See design.md, GUI: Layout, Views.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtWidgets import QWidget

from tsunami.core.runner import RunRequest
from tsunami.gui.context import AppContext


@dataclass(frozen=True)
class ActivityItem:
    view_id: str  # e.g. "inventory"
    title: str  # side bar header, e.g. "Inventory"
    icon: str  # codicon name, e.g. "server-environment"
    factory: Callable[[AppContext, Shell], QWidget]
    order: int = 100
    bottom: bool = False  # pinned to the bottom of the activity bar (Settings)


class Shell(Protocol):
    ctx: AppContext

    def open_tab(
        self, key: str, title: str, factory: Callable[[], QWidget], icon: str | None = None
    ) -> QWidget:
        """Open (or focus) the editor tab with this key; factory builds it the first time."""
        ...

    def close_tab(self, key: str) -> None: ...

    def show_view(self, view_id: str) -> None:
        """Switch the side bar to a view."""
        ...

    def register_command(
        self, command_id: str, title: str, callback: Callable[[], None], shortcut: str | None = None
    ) -> None:
        """Add a command to the palette (Ctrl+Shift+P), optionally with a shortcut."""
        ...

    def notify(self, message: str, level: str = "info") -> None:
        """A short status message; level is info, warning or error."""
        ...

    def start_run(self, request: RunRequest, title: str) -> Any:
        """Start a run in a RunnerThread and open its job view tab. Returns the thread."""
        ...

    def open_job(self, job_id: str) -> None:
        """Open the job view tab for a past or running job."""
        ...

    def open_in_vscode(self, path: Path, line: int | None = None) -> None: ...
