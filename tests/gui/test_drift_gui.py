"""Back up and check for drift: the tab, the inventory menu entries, the backups view."""

from __future__ import annotations

import pytest

from tsunami.core.issues import TsunamiError
from tsunami.gui.screens import backups as backups_screen
from tsunami.gui.screens import drift
from tsunami.gui.screens.drift import COLUMNS, DriftTab, open_drift_tab

from .conftest import LOGIN_PASSWORD, TACACS_SECRET, all_widget_text


@pytest.fixture
def notes(main_window, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(main_window, "notify", lambda m, level="info": seen.append((level, m)))
    return seen


def wait_done(qtbot, tab: DriftTab) -> None:
    qtbot.waitUntil(lambda: not tab.running, timeout=15000)


def column(tab: DriftTab, name: str) -> dict[str, str]:
    col = COLUMNS.index(name)
    return {
        tab.table.item(r, 0).text(): tab.table.item(r, col).text()
        for r in range(tab.table.rowCount())
    }


def select(tab: DriftTab, device: str) -> None:
    row = next(r for r in range(tab.table.rowCount()) if tab.table.item(r, 0).text() == device)
    tab.table.selectRow(row)


def test_first_check_saves_first_backups_then_finds_drift(
    qtbot, gui_ctx, main_window, mock_transport, notes
):
    tab = open_drift_tab(gui_ctx, main_window, "acme", "site-a")
    assert isinstance(tab, DriftTab) and tab.running  # opening it starts the check
    wait_done(qtbot, tab)
    assert column(tab, "Status") == {"sa-as01": "first backup", "sa-as02": "first backup"}
    assert column(tab, "Site") == {"sa-as01": "site-a", "sa-as02": "site-a"}

    running = mock_transport.device("sa-as01").running
    running += ["ntp server 192.0.2.100", f"tacacs-server key {TACACS_SECRET}"]
    tab.start_button.click()
    wait_done(qtbot, tab)
    assert column(tab, "Status") == {"sa-as01": "changed", "sa-as02": "no change"}
    assert column(tab, "Changes")["sa-as01"] == "+2 -0"
    select(tab, "sa-as01")
    diff = tab.diff_view.toPlainText().split("\n")
    assert "+ ntp server 192.0.2.100" in diff
    assert "sa-as01: what changed" in tab.diff_caption.text()
    select(tab, "sa-as02")
    assert tab.diff_view.toPlainText() == ""
    text = all_widget_text(tab) + tab.diff_view.toPlainText()
    assert TACACS_SECRET not in text and LOGIN_PASSWORD not in text
    assert notes[-1] == ("warning", "site-a: 1 changed, 1 unchanged, 0 first backups.")


def test_a_customer_check_covers_every_site(qtbot, gui_ctx, main_window):
    tab = open_drift_tab(gui_ctx, main_window, "acme")
    wait_done(qtbot, tab)
    names = {d.name for d in gui_ctx.workspace.customers["acme"].devices}
    assert set(column(tab, "Status")) == names
    assert "all sites" in all_widget_text(tab)


def test_reopening_the_tab_checks_again(qtbot, gui_ctx, main_window):
    tab = open_drift_tab(gui_ctx, main_window, "acme", "site-a")
    wait_done(qtbot, tab)
    again = open_drift_tab(gui_ctx, main_window, "acme", "site-a")
    assert again is tab
    wait_done(qtbot, tab)
    assert set(column(tab, "Status").values()) == {"no change"}


def test_double_click_opens_the_devices_backups(qtbot, gui_ctx, main_window, monkeypatch):
    tab = open_drift_tab(gui_ctx, main_window, "acme", "site-a")
    wait_done(qtbot, tab)
    opened = []
    monkeypatch.setattr(backups_screen, "open_backups", lambda ctx, shell, c, d: opened.append(d))
    tab.table.itemDoubleClicked.emit(tab.table.item(0, 2))
    assert opened == [tab.table.item(0, 0).text()]


def test_locked_shows_why_and_reads_nothing(qtbot, gui_ctx, main_window, monkeypatch):
    def locked(ctx):
        raise TsunamiError("Tsunami is locked.")

    monkeypatch.setattr(drift.backups, "secret_redactor", locked)
    tab = open_drift_tab(gui_ctx, main_window, "acme", "site-a")
    assert not tab.running
    assert "locked" in tab.error_label.text()
    assert tab.table.rowCount() == 0


def test_backups_view_names_the_drift_check(qtbot, gui_ctx, main_window):
    tab = open_drift_tab(gui_ctx, main_window, "acme", "site-a")
    wait_done(qtbot, tab)
    backups_screen.open_backups(gui_ctx, main_window, "acme", "sa-as01")
    view = main_window._tabs["backups:acme/sa-as01"]
    assert view.table.item(0, 1).text() == "Drift check"
    assert view.table.cellWidget(0, 1) is None  # no job to open


def test_inventory_menu_opens_the_drift_tab(main_window, monkeypatch):
    from tsunami.gui.screens.inventory import InventoryView

    view = main_window._views["inventory"].findChild(InventoryView)
    opened = []
    monkeypatch.setattr(drift, "open_drift_tab", lambda ctx, sh, c, s=None: opened.append((c, s)))
    view.check_drift("acme", "site-a")
    view.check_drift("acme")
    assert opened == [("acme", "site-a"), ("acme", None)]
