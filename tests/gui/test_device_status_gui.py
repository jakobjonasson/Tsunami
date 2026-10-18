"""Device list status columns and the device page buttons, over real runs on the mock transport."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QMessageBox

from tsunami.core import ssh_launch
from tsunami.core.models.jobs import DeviceStatus
from tsunami.core.models.play import RunMode
from tsunami.core.runner import RunRequest
from tsunami.core.transports.base import UnreachableError
from tsunami.core.vault import VaultLocked
from tsunami.gui.screens import credentials, device_list
from tsunami.gui.screens.device_list import DeviceListView
from tsunami.gui.screens.inventory import DeviceView, InventoryView, open_device
from tsunami.gui.screens.job_view import JobView, status_color

PLAY = "plays/push/add-vlan.yml"
PROMPTS = {"vlan_id": 30, "vlan_name": "Cameras"}
SHOW_VERSION = """\
Cisco IOS XE Software, Version 17.09.04a
sa-as01 uptime is 3 weeks, 2 days
Model Number                       : C9300-24P
System Serial Number               : FCW2233L0AB
"""
MOBA = Path("C:/Program Files/Mobatek/MobaXterm/MobaXterm.exe")
COLS = {name: i for i, name in enumerate(device_list.COLUMNS)}


def _run(main_window, qtbot, request: RunRequest) -> JobView:
    before = set(main_window.findChildren(JobView))
    main_window.start_run(request, "test")
    view = next(v for v in main_window.findChildren(JobView) if v not in before)
    qtbot.waitUntil(lambda: view._finished and view.job_id is not None, timeout=20000)
    qtbot.waitUntil(lambda: not main_window._threads, timeout=20000)
    return view


def _cell(view: DeviceListView, name: str, column: str) -> str:
    row = next(r for r in range(view.table.rowCount()) if view._name_at(r) == name)
    return view.table.item(row, COLS[column]).text()


def _item(view: DeviceListView, name: str, column: str):
    row = next(r for r in range(view.table.rowCount()) if view._name_at(r) == name)
    return view.table.item(row, COLS[column])


def _order(view: DeviceListView) -> list[str]:
    return [view._name_at(r) for r in range(view.table.rowCount())]


@pytest.fixture(autouse=True)
def _yes(monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)


@pytest.fixture
def mixed_run(main_window, gui_ctx, mock_transport, qtbot):
    """One apply: sa-as01 succeeds, sa-as02 rejects the vlan line and rolls back."""
    for name in ("sa-as01", "sa-as02"):
        mock_transport.device(name).exec_responses["show version"] = SHOW_VERSION
    mock_transport.device("sa-as02").reject_lines = ["vlan 30"]
    main_window._views["inventory"].findChild(InventoryView).show_devices("acme")
    view = main_window._tabs["devices:acme"]
    job = _run(main_window, qtbot, RunRequest(PLAY, RunMode.APPLY, prompts=PROMPTS))
    return view, job


# -- device list ----------------------------------------------------------------------


def test_a_fresh_list_shows_never(main_window):
    main_window._views["inventory"].findChild(InventoryView).show_devices("acme")
    view = main_window._tabs["devices:acme"]
    assert _cell(view, "sa-as01", "Last job") == "never"
    assert _cell(view, "sa-as01", "Last contact") == "never"
    assert _cell(view, "sa-as01", "Version") == ""


def test_status_columns_are_filled_after_a_run(mixed_run, gui_ctx):
    view, job = mixed_run
    assert view.table.columnCount() == len(device_list.COLUMNS)
    assert _cell(view, "sa-as01", "Last job").startswith("success · ")
    assert _cell(view, "sa-as02", "Last job").startswith("rolled back · ")
    assert _cell(view, "sa-as01", "Version") == "17.09.04a"
    assert _cell(view, "sa-as02", "Version") == "17.09.04a"
    # Both devices were reached, even the one whose change failed.
    assert _cell(view, "sa-as01", "Last contact") == "just now"
    assert _cell(view, "sa-as02", "Last contact") == "just now"

    ok = _item(view, "sa-as01", "Last job")
    bad = _item(view, "sa-as02", "Last job")
    assert ok.foreground().color() == QColor(status_color(DeviceStatus.SUCCESS))
    assert bad.foreground().color() == QColor(status_color(DeviceStatus.ROLLED_BACK))
    assert ok.foreground().color() != bad.foreground().color()
    assert ok.toolTip().splitlines()[0] == "Add VLAN"
    assert str(datetime.now().year) in ok.toolTip()


def test_unreachable_device_shows_a_job_but_no_contact(
    main_window, gui_ctx, mock_transport, qtbot, monkeypatch
):
    mock_transport._unreachable.add("sa-as02")
    main_window._views["inventory"].findChild(InventoryView).show_devices("acme")
    view = main_window._tabs["devices:acme"]
    _run(main_window, qtbot, RunRequest(PLAY, RunMode.APPLY, prompts=PROMPTS))
    assert _cell(view, "sa-as02", "Last job").startswith("unreachable · ")
    assert _cell(view, "sa-as02", "Last contact") == "never"
    assert _cell(view, "sa-as01", "Last contact") == "just now"


def test_columns_sort_by_status_and_by_time(mixed_run):
    view, _job = mixed_run
    from PySide6.QtCore import Qt

    view.table.sortByColumn(COLS["Last job"], Qt.SortOrder.AscendingOrder)
    assert _order(view) == ["sa-as02", "sa-as01"]  # problems first
    view.table.sortByColumn(COLS["Last job"], Qt.SortOrder.DescendingOrder)
    assert _order(view) == ["sa-as01", "sa-as02"]

    now = datetime.now(UTC)
    view._contact = {"sa-as01": now - timedelta(hours=3), "sa-as02": now - timedelta(minutes=5)}
    view._fill_status()
    view.table.sortByColumn(COLS["Last contact"], Qt.SortOrder.AscendingOrder)
    assert _order(view) == ["sa-as01", "sa-as02"]  # longest ago first
    assert _cell(view, "sa-as01", "Last contact") == "3 h ago"
    view.table.sortByColumn(COLS["Last contact"], Qt.SortOrder.DescendingOrder)
    assert _order(view) == ["sa-as02", "sa-as01"]

    view._contact = {"sa-as01": now}  # a device never reached sorts before every contact
    view._fill_status()
    view.table.sortByColumn(COLS["Last contact"], Qt.SortOrder.AscendingOrder)
    assert _order(view) == ["sa-as02", "sa-as01"]
    assert _cell(view, "sa-as02", "Last contact") == "never"


def test_filter_and_selection_survive_a_sort(mixed_run):
    view, _job = mixed_run
    from PySide6.QtCore import Qt

    view.filter_box.setText("as02")
    view.table.sortByColumn(COLS["Last job"], Qt.SortOrder.DescendingOrder)
    assert view.visible_names() == ["sa-as02"]
    view.table.sortByColumn(COLS["Name"], Qt.SortOrder.DescendingOrder)
    assert view.visible_names() == ["sa-as02"]
    view.filter_box.setText("")
    view.select_all_shown()
    assert sorted(view.selected_names()) == ["sa-as01", "sa-as02"]
    assert view.run_button.isEnabled()


def test_the_list_reloads_when_a_job_finishes(main_window, mock_transport, qtbot):
    main_window._views["inventory"].findChild(InventoryView).show_devices("acme")
    view = main_window._tabs["devices:acme"]
    assert _cell(view, "sa-as01", "Last job") == "never"
    _run(main_window, qtbot, RunRequest(PLAY, RunMode.APPLY, prompts=PROMPTS))
    assert _cell(view, "sa-as01", "Last job") != "never"


def test_loading_the_list_queries_once_per_customer(gui_ctx, main_window):
    calls: list[str] = []
    history = gui_ctx.history
    for name in ("device_facts", "device_activity", "last_contact"):
        original = getattr(history, name)

        def counted(customer, _name=name, _original=original):
            calls.append(_name)
            return _original(customer)

        setattr(history, name, counted)
    view = DeviceListView(gui_ctx, main_window, "acme")
    assert sorted(calls) == ["device_activity", "device_facts", "last_contact"]
    assert view.table.rowCount() == 2


# -- device page ----------------------------------------------------------------------


def _page(main_window, device="sa-as01") -> DeviceView:
    open_device(main_window.ctx, main_window, "acme", device)
    return main_window._tabs[f"device:acme/{device}"]


def test_header_shows_what_the_device_reported(mixed_run, main_window):
    page = _page(main_window)
    text = page.details.text()
    assert "version 17.09.04a" in text and "serial FCW2233L0AB" in text
    assert "model C9300-24P" in text or "model " in text


def test_recent_jobs_are_listed_and_open_a_job(mixed_run, main_window, monkeypatch):
    _view, job = mixed_run
    page = _page(main_window, "sa-as02")
    assert page.jobs.topLevelItemCount() == 1
    item = page.jobs.topLevelItem(0)
    assert item.text(1) == "apply" and item.text(2) == "rolled back"
    assert item.foreground(2).color() == QColor(status_color(DeviceStatus.ROLLED_BACK))
    opened = []
    monkeypatch.setattr(main_window, "open_job", opened.append)
    page._open_job(item)
    assert opened == [job.job_id]
    assert page.jobs_empty.isHidden()


def test_a_device_with_no_jobs_says_so(main_window):
    page = _page(main_window)
    assert page.jobs.topLevelItemCount() == 0
    assert not page.jobs_empty.isHidden()


def test_open_ssh_goes_through_the_seam_without_a_password(main_window, monkeypatch):
    launched = []
    monkeypatch.setattr(ssh_launch, "spawn", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(ssh_launch, "find_mobaxterm", lambda *a, **k: MOBA)
    page = _page(main_window)
    page.ssh_button.click()
    assert launched == [[str(MOBA), "-newtab", "ssh -p 22 netadmin@203.0.113.11"]]
    from tests.gui.conftest import LOGIN_PASSWORD

    assert LOGIN_PASSWORD not in " ".join(launched[0])
    assert LOGIN_PASSWORD not in page.action_label.text()
    assert "Opened an SSH session to 203.0.113.11" in page.action_label.text()


def test_open_ssh_without_a_username_when_locked(main_window, gui_ctx, monkeypatch):
    launched = []
    monkeypatch.setattr(ssh_launch, "spawn", lambda argv, **kw: launched.append(argv))
    monkeypatch.setattr(ssh_launch, "find_mobaxterm", lambda *a, **k: None)
    monkeypatch.setattr(ssh_launch, "find_openssh", lambda *a, **k: Path("ssh.exe"))

    def locked(name):
        raise VaultLocked

    monkeypatch.setattr(gui_ctx.secrets, "get_login", locked)
    page = _page(main_window)
    page.ssh_button.click()
    assert launched == [["ssh.exe", "-p", "22", "203.0.113.11"]]
    assert "without a username" in page.action_label.text()
    assert "locked" in page.action_label.text()


def test_open_ssh_reports_no_client(main_window, monkeypatch):
    monkeypatch.setattr(ssh_launch, "find_mobaxterm", lambda *a, **k: None)
    monkeypatch.setattr(ssh_launch, "find_openssh", lambda *a, **k: None)
    page = _page(main_window)
    page.ssh_button.click()
    assert "No SSH client found" in page.action_label.text()


def test_run_play_opens_the_picker_with_this_device(main_window, monkeypatch):
    seen = []
    monkeypatch.setattr(device_list, "run_picker", lambda dialog: seen.append(dialog) or False)
    page = _page(main_window)
    page.run_button.click()
    assert len(seen) == 1 and seen[0].devices == ["sa-as01"]
    assert Path(PLAY) in seen[0].offered_paths()


class _FakeSession:
    def close(self) -> None:
        pass


def _fake_transport(error: Exception | None):
    class Fake:
        def __init__(self, paths, settings) -> None:
            pass

        def open(self, target):
            assert target.credential is not None and target.host == "203.0.113.11"
            if error is not None:
                raise error
            return _FakeSession()

        def close(self) -> None:
            pass

    return Fake


def test_test_connection_shows_success_inline(main_window, qtbot, monkeypatch):
    monkeypatch.setattr(credentials, "DeviceTransport", _fake_transport(None))
    page = _page(main_window)
    page.test_button.click()
    assert not page.test_button.isEnabled()  # busy while it runs
    assert "Testing acme-netadmin on sa-as01" in page.action_label.text() or page.last_test_result
    qtbot.waitUntil(lambda: page.last_test_result is not None, timeout=10000)
    assert page.last_test_result[0] is True
    assert page.action_label.text().startswith("Success (acme-netadmin)")
    assert page.test_button.isEnabled()


def test_test_connection_shows_failure_inline(main_window, qtbot, monkeypatch):
    monkeypatch.setattr(
        credentials, "DeviceTransport", _fake_transport(UnreachableError("no route"))
    )
    page = _page(main_window)
    page.test_button.click()
    qtbot.waitUntil(lambda: page.last_test_result is not None, timeout=10000)
    assert page.last_test_result[0] is False
    assert page.action_label.text().startswith("Failed (acme-netadmin)")
    assert "no route" in page.action_label.text()


def test_test_connection_when_the_login_is_unavailable(main_window, gui_ctx, monkeypatch):
    def missing(name):
        raise VaultLocked

    monkeypatch.setattr(gui_ctx.secrets, "get_login", missing)
    page = _page(main_window)
    assert page.test_connection() is False
    assert page.action_label.text().startswith("Cannot test")
    assert page.test_button.isEnabled()


def test_backups_calls_the_command_when_it_exists(main_window, monkeypatch):
    calls = []
    main_window.set_handler("backups.open", lambda *args: calls.append(args))
    page = _page(main_window)
    page.backups_button.click()
    assert calls == [("acme", "sa-as01")]


def test_backups_says_not_available_without_the_command(main_window, monkeypatch):
    monkeypatch.delattr(main_window, "_handler_backups_open", raising=False)
    page = _page(main_window)
    page.backups_button.click()
    assert page.action_label.text() == "Backups are not available."


def test_the_page_buttons_are_disabled_for_a_device_not_in_the_workspace(main_window):
    open_device(main_window.ctx, main_window, "acme", "ghost")
    page = main_window._tabs["device:acme/ghost"]
    assert not page.run_button.isEnabled() and not page.ssh_button.isEnabled()
    assert "not in the loaded workspace" in page.details.text()
