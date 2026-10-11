"""Side-bar views and editor-tab screens.

A side-bar view module exposes VIEW (an ActivityItem, gui/shell.py) and may define
register(shell) to add commands. Tab-only modules (run_wizard, job_view) expose
factories that side-bar views and the shell call.

See design.md, GUI: Views.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any

from tsunami.gui.shell import ActivityItem

log = logging.getLogger(__name__)

VIEW_MODULES = (
    "workspace",
    "inventory",
    "plays",
    "drafts",
    "history",
    "approvals",
    "credentials",
    "settings",
)


def load_views() -> list[ActivityItem]:
    views = []
    for name in VIEW_MODULES:
        module = importlib.import_module(f"tsunami.gui.screens.{name}")
        view = getattr(module, "VIEW", None)
        if isinstance(view, ActivityItem):
            views.append(view)
    return views


def register_all(shell: Any) -> None:
    for name in (*VIEW_MODULES, "run_wizard", "job_view"):
        module = importlib.import_module(f"tsunami.gui.screens.{name}")
        register = getattr(module, "register", None)
        if callable(register):
            register(shell)
