"""Workspace, Inventory, Plays and History views (work package 4.2A)."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QTreeWidgetItem

from tsunami.core.models.jobs import Actor, DeviceStatus, JobStatus
from tsunami.gui.screens.history import HistoryTab
from tsunami.gui.screens.inventory import DeviceView, InventoryView
from tsunami.gui.screens.plays import PlaysView, PlayView
from tsunami.gui.screens.workspace import WorkspaceView

VIEW_IDS = ["workspace", "inventory", "plays", "history"]


def _view(window, cls):
    for page in window._views.values():
        found = page.findChild(cls)
        if found is not None:
            return found
    raise AssertionError(f"{cls.__name__} not hosted")


def _children(item: QTreeWidgetItem) -> list[QTreeWidgetItem]:
    return [item.child(i) for i in range(item.childCount())]


def _find(tree, *names: str) -> QTreeWidgetItem:
    items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    item = None
    for name in names:
        item = next(i for i in items if i.text(0).split()[0] == name)
        items = _children(item)
    assert item is not None
    return item


def _var_rows(view: DeviceView) -> dict[str, tuple[str, str]]:
    rows = {}
    for i in range(view.tree.topLevelItemCount()):
        item = view.tree.topLevelItem(i)
        rows[item.text(0)] = (item.text(2), item.text(3))
    return rows


def _make_job(ctx, *, customer="acme", play="plays/push/add-vlan.yml", actor=Actor.GUI):
    return ctx.history.create_job(
        actor=actor,
        play_path=play,
        play_hash="h",
        customer=customer,
        mode="apply",
        status=JobStatus.COMPLETED,
    )


def test_views_exist(main_window):
    for view_id in VIEW_IDS:
        assert view_id in main_window._views
        main_window.show_view(view_id)
        assert main_window._views[view_id].isVisibleTo(main_window)


def test_workspace_summary(main_window, gui_ctx):
    view = _view(main_window, WorkspaceView)
    assert view.name_label.text() == "Example network workspace"
    assert "Schema version 1" in view.schema_label.text()
    assert "1 customer, 2 devices, 3 plays, 0 draft plays" in view.counts_label.text()


def test_workspace_buttons_and_no_workspace(main_window, gui_ctx, qtbot):
    view = _view(main_window, WorkspaceView)
    calls = []
    main_window.run_command = calls.append
    view.reload_button.click()
    view.open_button.click()
    assert calls == ["workspace.reload", "workspace.open"]
    gui_ctx.workspace = None
    gui_ctx.workspace_changed.emit()
    assert view.hint_label.isVisibleTo(view)
    assert not view.reload_button.isVisibleTo(view)
    assert view.open_button.isVisibleTo(view)
    assert view.init_button.isVisibleTo(view)


def test_inventory_tree_and_filter(main_window):
    view = _view(main_window, InventoryView)
    assert _find(view.tree, "acme", "site-a", "sa-as01")
    site = _find(view.tree, "acme", "site-a")
    assert [c.text(0).split()[0] for c in _children(site)] == ["sa-as01", "sa-as02"]
    assert "ios-xe" in _children(site)[0].text(0)
    view.filter_box.setText("as02")
    assert _find(view.tree, "acme", "site-a", "sa-as01").isHidden()
    assert not _find(view.tree, "acme", "site-a", "sa-as02").isHidden()
    view.filter_box.setText("zzz")
    assert _find(view.tree, "acme").isHidden()
    view.filter_box.setText("")
    assert not _find(view.tree, "acme", "site-a", "sa-as01").isHidden()


def test_device_tab_variables(main_window):
    main_window.run_command_with("inventory.open_device", "acme", "sa-as01")
    tab = main_window._tabs["device:acme/sa-as01"]
    assert isinstance(tab, DeviceView)
    rows = _var_rows(tab)
    assert rows["ntp_servers"] == ("customer", "inventory/customers/acme/customer.yml")
    assert rows["mgmt_vlan"][0] == "customer role"
    assert rows["hostname"][0] == "default"
    assert tab.title.text() == "sa-as01"
    assert "203.0.113.11" in tab.details.text()


def test_open_device_twice_reuses_tab(main_window):
    main_window.run_command_with("inventory.open_device", "acme", "sa-as01")
    count = main_window.editor.count()
    main_window.run_command_with("inventory.open_device", "acme", "sa-as01")
    assert main_window.editor.count() == count


def test_device_double_click_opens_tab(main_window):
    view = _view(main_window, InventoryView)
    view.tree.itemDoubleClicked.emit(_find(view.tree, "acme", "site-a", "sa-as02"), 0)
    assert "device:acme/sa-as02" in main_window._tabs


def test_plays_tree_and_tab(main_window):
    view = _view(main_window, PlaysView)
    initial = _find(view.tree, "Initial", "acme")
    push = _find(view.tree, "Push", "acme")
    baseline = _find(view.tree, "Baseline", "acme")
    assert all(node.childCount() == 1 for node in (initial, push, baseline))
    path = push.child(0).data(0, Qt.ItemDataRole.UserRole)
    assert str(path).endswith("add-vlan.yml")

    main_window.run_command_with("plays.open_play", path)
    tab = main_window._tabs[f"play:{path.as_posix()}"]
    assert isinstance(tab, PlayView)
    assert "name:" in tab.yaml_view.toPlainText()
    assert tab.run_button.text() == "Run…"
    assert tab.run_button.property("primary") == "true"


def _history_tab(window) -> HistoryTab:
    window.run_command("history.open")
    return window._tabs["history"]


def test_history_updates_and_filters(main_window, gui_ctx):
    view = _history_tab(main_window)
    assert view.jobs_table.rowCount() == 0
    first = _make_job(gui_ctx)
    other = _make_job(gui_ctx, customer="globex", play="plays/push/other.yml", actor=Actor.MCP)
    gui_ctx.history.update_job(other, status=JobStatus.FAILED)
    gui_ctx.jobs_changed.emit()
    assert view.jobs_table.rowCount() == 2
    # newest first
    assert view.jobs_table.item(0, 3).text() == "globex"

    view.customer_filter.setCurrentText("acme")
    assert view.jobs_table.rowCount() == 1
    assert view.jobs_table.item(0, 0).data(0x100) == first
    view.customer_filter.setCurrentText("All")
    view.actor_filter.setCurrentText("mcp")
    assert view.jobs_table.rowCount() == 1
    view.actor_filter.setCurrentText("All")
    view.status_filter.setCurrentText("failed")
    assert view.jobs_table.rowCount() == 1
    assert view.jobs_table.item(0, 3).text() == "globex"


def test_history_alarm_and_open(main_window, gui_ctx):
    view = _history_tab(main_window)
    ok = _make_job(gui_ctx)
    bad = _make_job(gui_ctx)
    gui_ctx.history.add_devices(bad, ["sa-as01"])
    gui_ctx.history.update_device(bad, "sa-as01", status=DeviceStatus.ROLLBACK_FAILED)
    gui_ctx.jobs_changed.emit()
    alarm_by_job = {
        view.jobs_table.item(r, 0).data(0x100): view.jobs_table.item(r, 0).data(0x101)
        for r in range(view.jobs_table.rowCount())
    }
    assert alarm_by_job == {ok: False, bad: True}

    opened = []
    main_window.open_job = opened.append
    view.jobs_table.itemDoubleClicked.emit(view.jobs_table.item(0, 0))
    assert opened == [view.jobs_table.item(0, 0).data(0x100)]
