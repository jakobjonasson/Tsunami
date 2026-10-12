"""Shared GUI fixtures: a real main window over a temp workspace, mock devices and a
temp history.db. Qt runs offscreen; the keyring is the in-memory test backend."""

from __future__ import annotations

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QSettings

from tsunami.core.history import History
from tsunami.core.models.settings import AppSettings
from tsunami.core.secret_store import KeyringSecretStore, LoginCredential
from tsunami.core.transports.mock import MockTransport

FIXTURE = Path(__file__).parents[1] / "fixtures" / "workspace"
LOGIN_PASSWORD = "Login-Pass-1234"
TACACS_SECRET = "Real-Tacacs-Key-77"


@pytest.fixture(autouse=True)
def wheel_guard(qapp):
    """The app-wide wheel filter, as app.py installs it."""
    from tsunami.gui import wheel

    return wheel.install(qapp)


@pytest.fixture
def workspace_root(tmp_path) -> Path:
    root = tmp_path / "ws"
    shutil.copytree(FIXTURE, root)
    return root


@pytest.fixture
def mock_transport() -> MockTransport:
    mock = MockTransport()
    for name in ("sa-as01", "sa-as02"):
        state = mock.add_device(name, running=[f"hostname {name}", "ntp server 192.0.2.99"])
        state.exec_responses["re:^show vlan id"] = "30   Cameras   active"
    return mock


@pytest.fixture
def gui_ctx(qapp, tmp_path, app_paths, workspace_root, mock_transport):
    from tsunami.gui import theme
    from tsunami.gui.context import AppContext

    theme.apply(qapp, theme.LIGHT)
    history = History(tmp_path / "history.db")
    secrets = KeyringSecretStore(index_path=tmp_path / "credential-index.json")
    secrets.set_login("acme-netadmin", LoginCredential("netadmin", LOGIN_PASSWORD))
    secrets.set_secret("acme", "tacacs", TACACS_SECRET)
    ctx = AppContext(
        app_paths,
        AppSettings(),
        history,
        secrets,
        transport_factory=lambda: mock_transport,
        qsettings=QSettings(str(tmp_path / "gui.ini"), QSettings.Format.IniFormat),
    )
    ctx.open_workspace(workspace_root, block=True)
    yield ctx
    history.close()


@pytest.fixture
def main_window(qtbot, gui_ctx):
    """A real MainWindow with every registered view, as the app builds it."""
    from tsunami.gui.main_window import MainWindow
    from tsunami.gui.screens import load_views, register_all

    window = MainWindow(gui_ctx, load_views())
    register_all(window)
    qtbot.addWidget(window)
    yield window
    window.poller.stop()


def all_widget_text(widget) -> str:
    """Every piece of text visible in a widget tree, to prove secrets never appear."""
    from PySide6.QtWidgets import (
        QAbstractButton,
        QComboBox,
        QLabel,
        QLineEdit,
        QListWidget,
        QPlainTextEdit,
        QTableWidget,
        QTextEdit,
        QTreeWidget,
        QWidget,
    )

    parts = []
    for child in [widget, *widget.findChildren(QWidget)]:
        if isinstance(child, (QLabel, QAbstractButton)):
            parts.append(child.text())
        elif isinstance(child, QLineEdit):
            parts.append(child.text())
        elif isinstance(child, (QPlainTextEdit, QTextEdit)):
            parts.append(child.toPlainText())
        elif isinstance(child, QComboBox):
            parts.extend(child.itemText(i) for i in range(child.count()))
        elif isinstance(child, QListWidget):
            parts.extend(child.item(i).text() for i in range(child.count()))
        elif isinstance(child, QTableWidget):
            for r in range(child.rowCount()):
                for c in range(child.columnCount()):
                    item = child.item(r, c)
                    if item is not None:
                        parts.append(item.text())
        elif isinstance(child, QTreeWidget):
            iterator = [child.topLevelItem(i) for i in range(child.topLevelItemCount())]
            while iterator:
                item = iterator.pop()
                parts.extend(item.text(c) for c in range(child.columnCount()))
                iterator.extend(item.child(i) for i in range(item.childCount()))
        parts.append(child.toolTip())
    return "\n".join(p for p in parts if p)
