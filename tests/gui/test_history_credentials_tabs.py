"""The compact History and Credentials side bars, and the full tabs they open."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel, QMessageBox
from tests.gui.conftest import LOGIN_PASSWORD, TACACS_SECRET, all_widget_text

from tsunami.core.models.jobs import Actor, JobStatus
from tsunami.gui.screens import credentials, inventory_dialogs
from tsunami.gui.screens.history import HistoryTab, HistoryView, relative_time


def _sidebar(window, cls):
    for page in window._views.values():
        found = page.findChild(cls)
        if found is not None:
            return found
    raise AssertionError(f"{cls.__name__} not hosted")


def _make_job(ctx, *, customer="acme", play="plays/push/add-vlan.yml", status=JobStatus.COMPLETED):
    return ctx.history.create_job(
        actor=Actor.GUI,
        play_path=play,
        play_hash="h",
        customer=customer,
        mode="apply",
        status=status,
    )


def _action(menu, text):
    return next(a for a in menu.actions() if a.text() == text)


# -- History ------------------------------------------------------------------------------


def test_history_button_opens_one_tab_with_labelled_filters(main_window):
    view = _sidebar(main_window, HistoryView)
    assert "history" not in main_window._tabs
    view.open_button.click()
    tab = main_window._tabs["history"]
    assert isinstance(tab, HistoryTab)
    assert [label.text() for label in tab.filter_labels] == [
        "Customer",
        "Play",
        "Actor",
        "Status",
    ]
    for label, combo in zip(
        tab.filter_labels,
        (tab.customer_filter, tab.play_filter, tab.actor_filter, tab.status_filter),
        strict=True,
    ):
        assert label.buddy() is combo
    assert [tab.tabs.tabText(i) for i in range(tab.tabs.count())] == ["Jobs", "Compliance"]
    view.open_button.click()  # focuses the same tab
    assert main_window._tabs["history"] is tab


def test_history_sidebar_lists_recent_jobs_and_opens_one(main_window, gui_ctx):
    view = _sidebar(main_window, HistoryView)
    assert view.recent_list.count() == 0 and not view.empty_label.isHidden()
    first = _make_job(gui_ctx)
    bad = _make_job(gui_ctx, customer="globex", play="plays/push/other.yml")
    gui_ctx.history.update_job(bad, status=JobStatus.FAILED)
    gui_ctx.jobs_changed.emit()
    assert view.recent_list.count() == 2
    assert view.empty_label.isHidden()
    texts = [view.recent_list.item(i).text() for i in range(2)]
    assert texts[0].startswith("other\n") and "failed" in texts[0] and "just now" in texts[0]
    assert texts[1].startswith("add-vlan\n") and "acme" in texts[1]
    assert not view.recent_list.item(0).icon().isNull()

    opened = []
    main_window.open_job = opened.append
    view.recent_list.itemDoubleClicked.emit(view.recent_list.item(1))
    assert opened == [first]


def test_history_tab_refreshes_with_f5_path(main_window, gui_ctx, qtbot):
    main_window.run_command("history.open")
    tab = main_window._tabs["history"]
    view = _sidebar(main_window, HistoryView)
    _make_job(gui_ctx)
    with qtbot.waitSignal(gui_ctx.workspace_changed, timeout=10000):
        main_window.refresh_all()
    assert tab.jobs_table.rowCount() == 1
    assert view.recent_list.count() == 1


def test_relative_time():
    from datetime import UTC, datetime, timedelta

    now = datetime(2026, 1, 10, 12, 0, tzinfo=UTC)
    assert relative_time(now - timedelta(seconds=5), now) == "just now"
    assert relative_time(now - timedelta(minutes=5), now) == "5 min ago"
    assert relative_time(now - timedelta(hours=3), now) == "3 h ago"
    assert relative_time(now - timedelta(days=2), now) == "2 d ago"
    assert relative_time(None, now) == ""


# -- Credentials --------------------------------------------------------------------------


@pytest.fixture
def side(main_window):
    return _sidebar(main_window, credentials.CredentialsView)


@pytest.fixture
def cred_tab(main_window):
    main_window.run_command("credentials.open")
    return main_window._tabs["credentials"]


def test_manage_button_opens_one_credentials_tab(main_window, side):
    assert "credentials" not in main_window._tabs
    side.manage_button.click()
    tab = main_window._tabs["credentials"]
    assert isinstance(tab, credentials.CredentialsTab)
    side.manage_button.click()
    assert main_window._tabs["credentials"] is tab
    assert tab.login_list.count() == 1 and tab.customer_combo.findText("acme") >= 0


def test_sidebar_has_no_button_rows(side):
    texts = {b.text() for b in side.findChildren(credentials.QPushButton)}
    assert texts == {"Manage credentials"}


def test_login_menu_actions_by_text(side, gui_ctx, monkeypatch):
    item = side.login_list.item(0)
    menu = side.menu_for(item)
    assert [a.text() for a in menu.actions()] == ["New…", "Overwrite…", "Delete", "Test"]

    seen = []
    monkeypatch.setattr(
        credentials,
        "run_dialog",
        lambda d: (
            seen.append((d.windowTitle(), d.name_edit.text(), d.name_edit.isReadOnly())) or False
        ),
    )
    _action(menu, "New…").trigger()
    _action(menu, "Overwrite…").trigger()
    assert seen == [
        ("New login", "", False),
        ("Overwrite login acme-netadmin", "acme-netadmin", True),
    ]

    notes = []
    monkeypatch.setattr(side.shell, "notify", lambda m, level="info": notes.append((m, level)))
    started = []
    monkeypatch.setattr(side, "start_test", lambda *a: started.append(a) or True)
    monkeypatch.setattr(credentials.QInputDialog, "getItem", lambda *a, **k: ("acme/sa-as01", True))
    _action(menu, "Test").trigger()
    assert started == [("acme-netadmin", "acme", "sa-as01")]

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    _action(menu, "Delete").trigger()
    assert gui_ctx.secrets.list_logins() == ["acme-netadmin"]
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    _action(menu, "Delete").trigger()
    assert gui_ctx.secrets.list_logins() == []
    assert side.login_list.count() == 0


def test_secret_menu_actions_by_text(side, gui_ctx, monkeypatch):
    side.customer_combo.setCurrentText("acme")
    item = side.secret_list.item(0)
    menu = side.menu_for(item)
    assert [a.text() for a in menu.actions()] == ["New…", "Overwrite…", "Delete"]

    def accept(dialog):
        if not dialog.name_edit.isReadOnly():
            dialog.name_edit.setText("snmp")
        dialog.value_edit.setText("Menu-Secret-4471")
        return True

    monkeypatch.setattr(credentials, "run_dialog", accept)
    _action(menu, "New…").trigger()
    assert gui_ctx.secrets.get_secret("acme", "snmp") == "Menu-Secret-4471"
    _action(menu, "Overwrite…").trigger()
    assert gui_ctx.secrets.get_secret("acme", "tacacs") == "Menu-Secret-4471"
    assert "Menu-Secret-4471" not in all_widget_text(side)

    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    _action(menu, "Delete").trigger()
    assert gui_ctx.secrets.list_secrets("acme") == ["snmp"]


def test_empty_space_menu_offers_only_new(side):
    assert [a.text() for a in side.menu_for(None, "login").actions()] == ["New…"]
    assert [a.text() for a in side.menu_for(None, "secret").actions()] == ["New…"]


def test_sidebar_and_tab_stay_in_sync(main_window, side, cred_tab):
    assert side.save_login("acme-lab", "lab", "Lab-Pass-9", None)
    names = [cred_tab.login_list.item(i).text() for i in range(cred_tab.login_list.count())]
    assert names == ["acme-lab", "acme-netadmin"]
    assert side.login_list.count() == 2


def test_tab_buttons_fit_and_work(cred_tab, gui_ctx, monkeypatch):
    cred_tab.resize(1100, 700)
    cred_tab.show()
    labels = [b.text() for b in cred_tab.findChildren(credentials.QPushButton)]
    assert labels == ["New…", "Overwrite…", "Delete", "Test", "New…", "Overwrite…", "Delete"]
    for button in cred_tab.findChildren(credentials.QPushButton):
        assert button.width() >= button.sizeHint().width()
    cred_tab.login_list.setCurrentRow(0)
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    cred_tab.delete_login()
    assert gui_ctx.secrets.list_logins() == []


def _usage_labels(tab):
    tree = tab.used_by_tree
    groups = {}
    for i in range(tree.topLevelItemCount()):
        group = tree.topLevelItem(i)
        groups[group.text(0)] = [group.child(j).text(0) for j in range(group.childCount())]
    return groups


def test_used_by_shows_customer_site_and_device(main_window, gui_ctx, cred_tab):
    gui_ctx.secrets.set_login("acme-lab", credentials.LoginCredential("lab", "Lab-Pass-1"))
    editor = inventory_dialogs.make_editor(gui_ctx)
    editor.update_site("acme", "site-a", credential="acme-lab", set_credential=True)
    editor.update_device("acme", "sa-as02", {"credential": "acme-lab"})
    editor.update_customer("acme", credential="acme-lab")
    gui_ctx.reload_workspace(block=True)
    cred_tab.refresh()

    names = [cred_tab.login_list.item(i).text() for i in range(cred_tab.login_list.count())]
    cred_tab.login_list.setCurrentRow(names.index("acme-lab"))
    assert _usage_labels(cred_tab) == {
        "Customers (1)": ["acme"],
        "Sites (1)": ["acme/site-a"],
        "Devices (1)": ["acme/sa-as02"],
    }
    assert cred_tab.used_by_label.text() == "Used by acme-lab"
    assert cred_tab.login_list.item(names.index("acme-lab")).toolTip() == (
        "Used by 1 customer, 1 site, 1 device"
    )

    cred_tab.login_list.setCurrentRow(names.index("acme-netadmin"))
    assert list(_usage_labels(cred_tab)) == ["Nothing in the workspace uses this login."]


def test_delete_confirmation_mentions_usage(side, monkeypatch):
    asked = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda parent, title, text: asked.append(text) or QMessageBox.StandardButton.No,
    )
    side.delete_login("acme-netadmin")
    assert "still used by 1 customer" in asked[0]


# -- no secrets anywhere ----------------------------------------------------------------------


def test_no_secret_text_in_sidebars_or_tabs(main_window, gui_ctx, side):
    main_window.run_command("credentials.open")
    main_window.run_command("history.open")
    _make_job(gui_ctx)
    gui_ctx.jobs_changed.emit()
    side.save_login("acme-lab", "lab", "Lab-Pass-77", "En-Pass-77")
    side.customer_combo.setCurrentText("acme")
    side.save_secret("acme", "snmp", "Snmp-Secret-77")
    for widget in (
        side,
        _sidebar(main_window, HistoryView),
        main_window._tabs["credentials"],
        main_window._tabs["history"],
    ):
        text = all_widget_text(widget)
        for value in (
            LOGIN_PASSWORD,
            TACACS_SECRET,
            "Lab-Pass-77",
            "En-Pass-77",
            "Snmp-Secret-77",
        ):
            assert value not in text
    tab_text = all_widget_text(main_window._tabs["credentials"])
    assert "acme-lab" in tab_text and "snmp" in tab_text and "tacacs" in tab_text
    assert not any(
        isinstance(w, QLabel) and LOGIN_PASSWORD in w.text()
        for w in main_window.findChildren(QLabel)
    )
