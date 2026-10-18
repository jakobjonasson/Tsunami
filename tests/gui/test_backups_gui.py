"""The Backups tab: lists the backups a real mock run wrote, shows them redacted, compares
two, and refuses to show content while Tsunami is locked."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtWidgets import QMessageBox
from tests.gui.conftest import LOGIN_PASSWORD, TACACS_SECRET, all_widget_text

from tsunami.core.models.play import RunMode
from tsunami.core.runner import RunRequest
from tsunami.core.vault import LockedCipher
from tsunami.gui.screens import backups as backups_screen
from tsunami.gui.screens.backups import (
    HINT_TEXT,
    LOCKED_TEXT,
    NEWEST,
    NO_BACKUPS_TEXT,
    PREVIOUS,
    BackupsTab,
)
from tsunami.gui.screens.job_view import JobView

PLAY = "plays/push/add-vlan.yml"
ENABLE_SECRET = "Enable-Secret-4321"
SNMP_COMMUNITY = "Snmp-Comm-9988"
CLEAR = (LOGIN_PASSWORD, TACACS_SECRET, ENABLE_SECRET, SNMP_COMMUNITY)
SECRET_LINES = [
    f"enable secret {ENABLE_SECRET}",
    f"username netadmin secret {LOGIN_PASSWORD}",
    f"snmp-server community {SNMP_COMMUNITY} RO",
    "tacacs server T1",
    " address ipv4 192.0.2.10",
    f" key {TACACS_SECRET}",
    f"! the shared key is {TACACS_SECRET}",
]
KEY = "acme/sa-as01"


def _run(main_window, qtbot, vlan: int) -> JobView:
    before = set(main_window.findChildren(JobView))
    request = RunRequest(PLAY, RunMode.APPLY, prompts={"vlan_id": vlan, "vlan_name": f"Vlan{vlan}"})
    main_window.start_run(request, "test")
    view = next(v for v in main_window.findChildren(JobView) if v not in before)
    qtbot.waitUntil(lambda: view._finished and view.job_id is not None, timeout=20000)
    qtbot.waitUntil(lambda: not main_window._threads, timeout=20000)
    return view


@pytest.fixture
def two_backups(main_window, gui_ctx, mock_transport, qtbot, monkeypatch):
    """Two real APPLY runs; each saves a backup first. Returns their job ids, oldest first."""
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    state = mock_transport.device("sa-as01")
    state.running += SECRET_LINES
    first = _run(main_window, qtbot, 30)
    state.running += ["ntp server 192.0.2.150"]
    # Backups are named to the second; make sure the two files cannot collide.
    qtbot.wait(1100)
    second = _run(main_window, qtbot, 31)
    return first.job_id, second.job_id


@pytest.fixture
def open_tab(main_window):
    backups_screen.register(main_window)

    def open_(customer="acme", device="sa-as01") -> BackupsTab:
        main_window.run_command_with("backups.open", customer, device)
        return main_window._tabs[f"backups:{customer}/{device}"]

    return open_


def _select(tab: BackupsTab, *rows: int) -> None:
    tab.table.clearSelection()
    for row in rows:
        tab.table.selectionModel().select(
            tab.table.model().index(row, 0),
            tab.table.selectionModel().SelectionFlag.Select
            | tab.table.selectionModel().SelectionFlag.Rows,
        )


def _raw_backups(gui_ctx):
    return sorted((gui_ctx.paths.backups_dir / "acme" / "sa-as01").glob("*.cfg"))


def test_the_tab_lists_the_backups_a_run_made_newest_first(
    main_window, gui_ctx, two_backups, open_tab, monkeypatch
):
    first, second = two_backups
    tab = open_tab()

    assert main_window.editor.currentWidget() is tab
    assert [e.job_id for e in tab.entries] == [second, first]
    assert tab.table.rowCount() == 2
    assert tab.table.item(0, 0).text() > tab.table.item(1, 0).text()  # local time, newest first
    assert tab.table.item(0, 2).text().endswith(("B", "KB"))
    assert tab.table.cellWidget(0, 1).text() == second[:8]
    assert tab.stack.currentWidget() is tab.message and tab.message.text() == HINT_TEXT

    opened = []
    monkeypatch.setattr(main_window, "open_job", opened.append)
    tab.table.cellWidget(1, 1).click()
    assert opened == [first]

    open_tab()  # a second open focuses the same tab
    assert sum(1 for k in main_window._tabs if k == f"backups:{KEY}") == 1


def test_one_backup_is_shown_redacted(main_window, gui_ctx, two_backups, open_tab):
    raw = "\n".join(p.read_text(encoding="utf-8") for p in _raw_backups(gui_ctx))
    for clear in CLEAR:
        assert clear in raw  # the files on disk really are unredacted

    tab = open_tab()
    _select(tab, 0)

    shown = tab.content_view.toPlainText()
    assert tab.stack.currentWidget() is tab.content_view
    assert "hostname sa-as01" in shown and "ntp server 192.0.2.150" in shown
    assert "<redacted>" in shown
    assert tab.content_view.isReadOnly()
    everything = all_widget_text(main_window)
    for clear in CLEAR:
        assert clear not in shown
        assert clear not in everything


def test_two_selected_backups_show_a_redacted_diff(main_window, gui_ctx, two_backups, open_tab):
    tab = open_tab()
    _select(tab, 0, 1)

    assert tab.stack.currentWidget() is tab.diff_view
    diff = tab.diff_view.toPlainText()
    assert "+ ntp server 192.0.2.150" in diff.split("\n")  # added since the older backup
    assert "Changes from" in tab.caption.text()
    everything = all_widget_text(main_window)
    for clear in CLEAR:
        assert clear not in diff
        assert clear not in everything


def test_compare_one_backup_with_previous_or_newest(main_window, gui_ctx, two_backups, open_tab):
    tab = open_tab()
    assert [tab.compare_combo.itemText(i) for i in range(2)] == [PREVIOUS, NEWEST]

    _select(tab, 0)  # the newest, compared with the one before it
    tab.compare_combo.setCurrentText(PREVIOUS)
    tab.compare()
    assert tab.stack.currentWidget() is tab.diff_view
    assert "+ ntp server 192.0.2.150" in tab.diff_view.toPlainText().split("\n")

    _select(tab, 1)  # the oldest, compared with the newest: same diff
    tab.compare_combo.setCurrentText(NEWEST)
    tab.compare()
    assert tab.stack.currentWidget() is tab.diff_view
    assert "+ ntp server 192.0.2.150" in tab.diff_view.toPlainText().split("\n")

    tab.compare_combo.setCurrentText(PREVIOUS)  # nothing earlier than the oldest
    tab.compare()
    assert tab.stack.currentWidget() is tab.message and "oldest" in tab.message.text()

    _select(tab, 0)
    tab.compare_combo.setCurrentText(NEWEST)  # the newest against itself
    tab.compare()
    assert tab.stack.currentWidget() is tab.message and "newest" in tab.message.text()

    for clear in CLEAR:
        assert clear not in all_widget_text(main_window)


def test_locked_tsunami_hides_content_but_lists_backups(
    main_window, gui_ctx, two_backups, open_tab
):
    class FakeLock(QObject):
        lock_changed = Signal(bool)
        unlocked = True

    lock = FakeLock()
    gui_ctx.lock = lock
    tab = open_tab()
    _select(tab, 0)
    assert tab.stack.currentWidget() is tab.content_view  # unlocked: shown

    lock.unlocked = False  # auto-lock fires while the content is on screen
    lock.lock_changed.emit(False)
    assert tab.stack.currentWidget() is tab.message and tab.message.text() == LOCKED_TEXT
    assert tab.content_view.toPlainText() == "" and tab.diff_view.toPlainText() == ""
    assert tab.table.rowCount() == 2  # names, times and sizes still listed
    _select(tab, 0, 1)
    assert tab.message.text() == LOCKED_TEXT and tab.diff_view.toPlainText() == ""
    tab.compare()
    assert tab.message.text() == LOCKED_TEXT
    assert "hostname" not in all_widget_text(tab)

    lock.unlocked = True
    lock.lock_changed.emit(True)
    assert tab.stack.currentWidget() is tab.diff_view  # unlocking shows it again


def test_a_locked_credential_store_hides_content_too(main_window, gui_ctx, two_backups, open_tab):
    gui_ctx.secrets.cipher = LockedCipher()  # no Tsunami window lock manager in this context
    tab = open_tab()
    _select(tab, 0)
    assert tab.message.text() == LOCKED_TEXT
    assert "hostname" not in all_widget_text(tab)
    assert tab.table.rowCount() == 2


def test_an_unreadable_credential_store_fails_closed(main_window, gui_ctx, two_backups, open_tab):
    def broken():
        raise RuntimeError("backend down")

    gui_ctx.secrets.all_secret_values = broken
    tab = open_tab()
    _select(tab, 0)
    assert tab.stack.currentWidget() is tab.message
    assert "cannot be read" in tab.message.text()
    assert "hostname" not in all_widget_text(tab)


def test_empty_state_for_a_device_with_no_backups(main_window, open_tab):
    tab = open_tab("acme", "sa-as02")
    assert tab.table.rowCount() == 0 and not tab.table.isVisibleTo(tab)
    assert tab.stack.currentWidget() is tab.message and tab.message.text() == NO_BACKUPS_TEXT
    assert not tab.compare_button.isEnabled() and not tab.folder_button.isEnabled()
    assert "0 backups" in tab.subtitle.text()


def test_refresh_picks_up_a_new_backup(main_window, gui_ctx, open_tab):
    tab = open_tab()
    assert tab.table.rowCount() == 0
    folder = gui_ctx.paths.backups_dir / "acme" / "sa-as01"
    folder.mkdir(parents=True)
    (folder / "20260101T010101Z_job-1234abcd.cfg").write_text("hostname x\n", encoding="utf-8")
    (folder / "readme.txt").write_text("not a backup")
    tab.refresh_button.click()
    assert tab.table.rowCount() == 1
    assert tab.table.cellWidget(0, 1).text() == "job-1234"
    assert "1 backup" in tab.subtitle.text()


def test_show_in_folder_selects_the_file_and_nothing_opens_the_raw_text(
    main_window, gui_ctx, two_backups, open_tab, monkeypatch
):
    shown = []
    monkeypatch.setattr(backups_screen, "reveal_in_folder", shown.append)
    tab = open_tab()
    tab.folder_button.click()  # nothing selected yet
    assert shown == []
    _select(tab, 1)
    tab.folder_button.click()
    assert shown == [tab.entries[1].path]

    from PySide6.QtWidgets import QPushButton

    labels = {b.text().lower() for b in tab.findChildren(QPushButton)}
    assert not any("vs code" in t or "raw" in t or "open file" in t for t in labels)


def test_bad_names_are_refused_by_the_command(main_window, open_tab, monkeypatch):
    notes = []
    monkeypatch.setattr(main_window, "notify", lambda message, level="info": notes.append(message))
    open_tab()
    main_window.run_command_with("backups.open", "acme", "../etc")
    assert notes and "not a valid device name" in notes[0]
    assert "backups:acme/../etc" not in main_window._tabs
