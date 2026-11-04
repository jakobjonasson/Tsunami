"""Backups of a customer or site: the overview tab, the Backup column, CSV export, and a
drift check recorded as a job (History, Last job and Last contact)."""

from __future__ import annotations

import csv

import pytest

from tsunami.core.drift import DRIFT_JOB, DRIFT_MODE
from tsunami.core.models.jobs import JobStatus
from tsunami.gui.screens import backups as backups_screen
from tsunami.gui.screens import device_list
from tsunami.gui.screens.backups import (
    NO_BACKUP,
    OVERVIEW_COLUMNS,
    BackupOverviewTab,
    BackupsTab,
    open_backup_overview,
)
from tsunami.gui.screens.drift import DriftTab, open_drift_tab
from tsunami.gui.screens.inventory_dialogs import SITE_DEVICE_COLUMNS, SiteView
from tsunami.gui.screens.job_view import JobView

from .conftest import LOGIN_PASSWORD, TACACS_SECRET, all_widget_text


@pytest.fixture
def notes(main_window, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(main_window, "notify", lambda m, level="info": seen.append((level, m)))
    return seen


def run_drift(qtbot, gui_ctx, main_window, site="site-a", device=None) -> DriftTab:
    tab = open_drift_tab(gui_ctx, main_window, "acme", site, device=device)
    qtbot.waitUntil(lambda: not tab.running, timeout=15000)
    return tab


def overview_column(tab: BackupOverviewTab, name: str) -> dict[str, str]:
    col = OVERVIEW_COLUMNS.index(name)
    return {
        tab.table.item(r, 0).text(): tab.table.item(r, col).text()
        for r in range(tab.table.rowCount())
    }


def list_column(view, name: str) -> dict[str, str]:
    col = device_list.COLUMNS.index(name)
    return {
        view.table.item(r, 0).text(): view.table.item(r, col).text()
        for r in range(view.table.rowCount())
    }


def test_overview_lists_each_device_and_whether_it_has_a_backup(qtbot, gui_ctx, main_window):
    tab = open_backup_overview(gui_ctx, main_window, "acme", "site-a")
    assert isinstance(tab, BackupOverviewTab)
    assert overview_column(tab, "Backups") == {"sa-as01": "0", "sa-as02": "0"}
    assert set(overview_column(tab, "Newest backup").values()) == {NO_BACKUP}
    assert "2 without a backup" in tab.subtitle.text()

    run_drift(qtbot, gui_ctx, main_window, device="sa-as01")  # jobs_changed refreshes it
    assert overview_column(tab, "Backups") == {"sa-as01": "1", "sa-as02": "0"}
    assert overview_column(tab, "Taken by")["sa-as01"] == "Drift check"
    assert "just now" in overview_column(tab, "Newest backup")["sa-as01"]
    assert "1 without a backup" in tab.subtitle.text()

    tab.table.selectRow(0)
    assert tab.open_button.isEnabled()
    tab.open_selected()
    assert isinstance(main_window._tabs["backups:acme/sa-as01"], BackupsTab)
    text = all_widget_text(tab)
    assert LOGIN_PASSWORD not in text and TACACS_SECRET not in text


def test_overview_checks_for_drift_and_is_one_tab_per_scope(qtbot, gui_ctx, main_window):
    customer = open_backup_overview(gui_ctx, main_window, "acme")
    assert set(overview_column(customer, "Device")) == {
        d.name for d in gui_ctx.workspace.customers["acme"].devices
    }
    assert open_backup_overview(gui_ctx, main_window, "acme") is customer
    site = open_backup_overview(gui_ctx, main_window, "acme", "site-a")
    assert site is not customer
    site.drift_button.click()
    drift_tab = main_window._tabs["drift:acme/site-a"]
    qtbot.waitUntil(lambda: not drift_tab.running, timeout=15000)
    assert set(overview_column(site, "Backups").values()) == {"1"}
    assert "all backed up" in site.subtitle.text()


def test_unknown_customer_has_no_overview(gui_ctx, main_window, notes):
    assert open_backup_overview(gui_ctx, main_window, "nobody") is None
    assert notes[-1][0] == "error"


def test_export_writes_the_csv_where_chosen(
    qtbot, gui_ctx, main_window, notes, tmp_path, monkeypatch
):
    run_drift(qtbot, gui_ctx, main_window, device="sa-as01")
    target = tmp_path / "out" / "acme-report"
    target.parent.mkdir()
    asked: list = []
    monkeypatch.setattr(
        backups_screen,
        "pick_report_path",
        lambda parent, suggested: (asked.append(suggested), target)[1],
    )
    tab = open_backup_overview(gui_ctx, main_window, "acme", "site-a")
    written = tab.export()
    assert written == target.with_suffix(".csv")  # the extension is added
    assert "tsunami-acme-site-a-" in asked[0]
    rows = list(csv.DictReader(written.read_text(encoding="utf-8-sig").splitlines()))
    by_device = {r["device"]: r for r in rows}
    assert set(by_device) == {"sa-as01", "sa-as02"}
    assert by_device["sa-as01"]["backups"] == "1"
    assert by_device["sa-as01"]["last_job"] == "Drift check"
    assert by_device["sa-as02"]["backups"] == "0"
    assert notes[-1] == ("info", f"Saved 2 devices to {written}.")
    raw = written.read_text(encoding="utf-8-sig")
    assert LOGIN_PASSWORD not in raw and TACACS_SECRET not in raw


def test_export_cancelled_or_failing(gui_ctx, main_window, notes, tmp_path, monkeypatch):
    monkeypatch.setattr(backups_screen, "pick_report_path", lambda parent, suggested: None)
    assert backups_screen.export_csv(gui_ctx, main_window, "acme") is None
    assert notes == []
    folder = tmp_path / "a-folder.csv"
    folder.mkdir()  # writing to a folder fails
    monkeypatch.setattr(backups_screen, "pick_report_path", lambda parent, suggested: folder)
    assert backups_screen.export_csv(gui_ctx, main_window, "acme") is None
    assert notes[-1][0] == "error" and "Could not save" in notes[-1][1]


def test_device_list_shows_drift_check_as_last_job_contact_and_backup(
    qtbot, gui_ctx, main_window, monkeypatch
):
    device_list.open_device_list(gui_ctx, main_window, "acme", "site-a")
    view = main_window._tabs["devices:acme/site-a"]
    assert list_column(view, "Backup") == {"sa-as01": NO_BACKUP, "sa-as02": NO_BACKUP}
    assert set(list_column(view, "Last job").values()) == {device_list.NEVER}

    run_drift(qtbot, gui_ctx, main_window)
    assert {v.split(" · ")[0] for v in list_column(view, "Last job").values()} == {"success"}
    assert device_list.NEVER not in list_column(view, "Last contact").values()
    assert NO_BACKUP not in list_column(view, "Backup").values()
    col = device_list.COLUMNS.index("Last job")
    assert DRIFT_JOB in view.table.item(0, col).toolTip()

    opened: list = []
    monkeypatch.setattr(
        backups_screen, "export_csv", lambda *a, **k: opened.append(("export", a[2], a[3]))
    )
    view.backups_button.click()
    assert isinstance(main_window._tabs["backups-overview:acme/site-a"], BackupOverviewTab)
    view.export_button.click()
    assert opened == [("export", "acme", "site-a")]


def test_site_view_has_a_backup_column_and_buttons(qtbot, gui_ctx, main_window, monkeypatch):
    view = SiteView(gui_ctx, main_window, "acme", "site-a")
    qtbot.addWidget(view)
    backup_col = SITE_DEVICE_COLUMNS.index("Backup")

    def backups() -> dict[str, str]:
        tree = view.device_list
        return {
            tree.topLevelItem(i).text(0): tree.topLevelItem(i).text(backup_col)
            for i in range(tree.topLevelItemCount())
        }

    assert backups() == {"sa-as01": NO_BACKUP, "sa-as02": NO_BACKUP}
    run_drift(qtbot, gui_ctx, main_window, device="sa-as02")
    view.refresh()
    assert backups()["sa-as01"] == NO_BACKUP and backups()["sa-as02"] != NO_BACKUP

    opened: list = []
    monkeypatch.setattr(backups_screen, "export_csv", lambda *a, **k: opened.append((a[2], a[3])))
    view.export_button.click()
    assert opened == [("acme", "site-a")]
    view.backups_button.click()
    assert isinstance(main_window._tabs["backups-overview:acme/site-a"], BackupOverviewTab)


def test_a_drift_check_is_a_job_in_history(qtbot, gui_ctx, main_window):
    mock = gui_ctx.make_transport()
    tab = run_drift(qtbot, gui_ctx, main_window)
    mock.device("sa-as01").running.append(f"tacacs-server key {TACACS_SECRET}")
    mock.device("sa-as01").running.append("ntp server 192.0.2.100")
    tab.start_button.click()
    qtbot.waitUntil(lambda: not tab.running, timeout=15000)

    newest, first = gui_ctx.history.list_jobs(customer="acme")[:2]
    assert (newest.play_path, newest.mode) == (DRIFT_JOB, DRIFT_MODE)
    assert newest.status == JobStatus.COMPLETED and first.status == JobStatus.COMPLETED
    assert tab.job_button.isEnabled()
    tab.job_button.click()
    view = main_window._tabs[f"job:{newest.id}"]
    assert isinstance(view, JobView)
    assert view.title_label.text() == DRIFT_JOB
    details = {view.table.item(r, 0).text(): view.table.item(r, 3).text() for r in range(2)}
    assert details["sa-as01"] == "changed (+2 -0): saved a new backup"
    assert details["sa-as02"] == "no change"
    # Nothing to run again: there is no play behind it.
    for button in (view.again_button, view.retry_button, view.apply_button):
        assert button.isHidden()
    view.select_device("sa-as01")
    assert "+ ntp server 192.0.2.100" in view.diff_view.toPlainText().split("\n")
    text = all_widget_text(view) + view.diff_view.toPlainText() + view.output_view.toPlainText()
    assert TACACS_SECRET not in text and LOGIN_PASSWORD not in text
