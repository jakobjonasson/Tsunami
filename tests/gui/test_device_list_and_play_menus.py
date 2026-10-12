"""Device list tab, play picker, preselected run wizard and the Plays context menu."""

from __future__ import annotations

from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox, QTreeWidgetItem

from tsunami.gui.screens import device_list
from tsunami.gui.screens.device_list import DeviceListView, PlayPickerDialog
from tsunami.gui.screens.inventory import InventoryView
from tsunami.gui.screens.plays import PlaysView
from tsunami.gui.screens.run_wizard import RunWizard

YES = QMessageBox.StandardButton.Yes
ADD_VLAN = Path("plays/push/add-vlan.yml")
DAY0 = Path("plays/initial/acme-access-day0.yml")


def _node(tree, *names: str) -> QTreeWidgetItem:
    items = [tree.topLevelItem(i) for i in range(tree.topLevelItemCount())]
    item = None
    for name in names:
        item = next(i for i in items if i.text(0).split()[0] == name)
        items = [item.child(i) for i in range(item.childCount())]
    assert item is not None
    return item


def _play_item(view, path: Path) -> QTreeWidgetItem:
    stack = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        if item.data(0, device_list._NAME_ROLE) == path:
            return item
        stack.extend(item.child(i) for i in range(item.childCount()))
    raise AssertionError(f"{path} not in the Plays tree")


def _actions(menu) -> dict:
    return {a.text(): a for a in menu.actions() if not a.isSeparator()}


@pytest.fixture
def picker_choice(monkeypatch):
    """Replaces the picker exec: records the dialog and accepts it."""
    seen: list[PlayPickerDialog] = []

    def run(dialog):
        seen.append(dialog)
        for i in range(dialog.tree.topLevelItemCount()):
            item = dialog.tree.topLevelItem(i)
            if item.data(0, device_list._NAME_ROLE) == ADD_VLAN:
                dialog.tree.setCurrentItem(item)
        return True

    monkeypatch.setattr(device_list, "run_picker", run)
    return seen


@pytest.fixture
def asked(monkeypatch):
    box = type("Asked", (), {"answer": YES, "texts": []})()

    def question(parent, title, text, *a, **k):
        box.texts.append(text)
        return box.answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return box


def _inventory(main_window):
    return main_window._views["inventory"].findChild(InventoryView)


def _plays(main_window):
    return main_window._views["plays"].findChild(PlaysView)


def test_click_customer_and_site_open_device_tabs(main_window):
    inv = _inventory(main_window)
    inv._click_item(_node(inv.tree, "acme"))
    inv._click_item(_node(inv.tree, "acme", "site-a"))
    customer_tab = main_window._tabs["devices:acme"]
    site_tab = main_window._tabs["devices:acme/site-a"]
    assert isinstance(customer_tab, DeviceListView) and isinstance(site_tab, DeviceListView)
    assert customer_tab.visible_names() == ["sa-as01", "sa-as02"]
    row = [customer_tab.table.item(0, c).text() for c in range(7)]
    assert row[:3] == ["sa-as01", "Access switch", "ios-xe"]
    assert row[4:6] == ["203.0.113.11", "site-a"]
    assert row[6] == "acme-netadmin"
    inv._click_item(_node(inv.tree, "acme"))  # reused, not duplicated
    assert main_window._tabs["devices:acme"] is customer_tab


def test_type_filter_and_text_filter_hide_rows(main_window):
    _inventory(main_window).show_devices("acme")
    view = main_window._tabs["devices:acme"]
    assert list(view._type_buttons) == ["access"]
    view._type_buttons["access"].setChecked(False)
    assert view.visible_names() == []
    view._type_buttons["access"].setChecked(True)
    view.filter_box.setText("as02")
    assert view.visible_names() == ["sa-as02"]


def test_select_all_shown_enables_run_and_double_click_opens_device(main_window):
    _inventory(main_window).show_devices("acme", "site-a")
    view = main_window._tabs["devices:acme/site-a"]
    assert not view.run_button.isEnabled()
    view.filter_box.setText("as01")
    view.select_all_shown()
    assert view.selected_names() == ["sa-as01"]
    assert view.run_button.isEnabled()
    view._open_row(0, 0)
    assert "device:acme/sa-as01" in main_window._tabs


def test_picker_offers_add_vlan_for_both_devices(main_window, gui_ctx):
    dialog = PlayPickerDialog(gui_ctx, "acme", ["sa-as01", "sa-as02"])
    paths = dialog.offered_paths()
    assert ADD_VLAN in paths
    assert DAY0 not in paths
    assert dialog.selected_play() is not None


def test_picker_skips_play_that_excludes_a_selected_device(main_window, gui_ctx, workspace_root):
    text = (workspace_root / ADD_VLAN).read_text(encoding="utf-8")
    text = text.replace("name: Add VLAN", "name: Only one")
    text = text.replace("  roles: [access]", "  devices: [sa-as01]")
    only = Path("plays/push/only-one.yml")
    (workspace_root / only).write_text(text, encoding="utf-8")
    gui_ctx.reload_workspace(block=True)
    both = PlayPickerDialog(gui_ctx, "acme", ["sa-as01", "sa-as02"]).offered_paths()
    one = PlayPickerDialog(gui_ctx, "acme", ["sa-as01"]).offered_paths()
    assert only not in both and ADD_VLAN in both
    assert only in one


def test_picker_explains_when_no_play_fits(main_window, gui_ctx):
    dialog = PlayPickerDialog(gui_ctx, "acme", ["no-such-device"])
    assert dialog.offered_paths() == []
    assert not dialog.explanation.isHidden()


def test_run_play_from_list_preselects_only_chosen_devices(main_window, picker_choice):
    _inventory(main_window).show_devices("acme")
    view = main_window._tabs["devices:acme"]
    view.table.selectRow(1)
    view.run_play()
    assert picker_choice and picker_choice[0].devices == ["sa-as02"]
    wizard = main_window._tabs[f"run-wizard:{ADD_VLAN.as_posix()}"]
    assert isinstance(wizard, RunWizard)
    assert [d.name for d in wizard.selected_devices()] == ["sa-as02"]
    assert len(wizard.devices) == 2


def test_wizard_without_devices_ticks_all(main_window, gui_ctx):
    from tsunami.gui.screens.run_wizard import open_run_wizard

    wizard = open_run_wizard(gui_ctx, main_window, ADD_VLAN)
    assert [d.name for d in wizard.selected_devices()] == ["sa-as01", "sa-as02"]


def test_device_menu_has_run_play(main_window, picker_choice):
    inv = _inventory(main_window)
    actions = _actions(inv.menu_for(_node(inv.tree, "acme", "site-a", "sa-as01")))
    assert "Run play…" in actions
    actions["Run play…"].trigger()
    assert picker_choice[0].devices == ["sa-as01"]
    assert f"run-wizard:{ADD_VLAN.as_posix()}" in main_window._tabs
    assert "Show devices" in _actions(inv.menu_for(_node(inv.tree, "acme")))
    assert "Show devices" in _actions(inv.menu_for(_node(inv.tree, "acme", "site-a")))


def test_plays_menu_actions(main_window, gui_ctx, monkeypatch):
    view = _plays(main_window)
    item = _play_item(view, ADD_VLAN)
    actions = _actions(view.menu_for(item))
    assert list(actions) == ["Open", "Edit in VS Code", "Run…", "Copy to customer…", "Delete…"]
    actions["Open"].trigger()
    assert f"play:{ADD_VLAN.as_posix()}" in main_window._tabs

    opened = []
    monkeypatch.setattr(main_window, "open_in_vscode", lambda p, line=None: opened.append(p))
    actions["Edit in VS Code"].trigger()
    assert opened == [gui_ctx.workspace.root / ADD_VLAN]

    actions["Run…"].trigger()
    assert f"run-wizard:{ADD_VLAN.as_posix()}" in main_window._tabs
    assert not view.menu_for(_node(view.tree, "Push")).actions()  # a group: empty


def test_plays_menu_run_for_initial_and_disabled_with_errors(main_window, gui_ctx, workspace_root):
    view = _plays(main_window)
    actions = _actions(view.menu_for(_play_item(view, DAY0)))
    assert actions["Run…"].isEnabled()
    actions["Run…"].trigger()
    assert f"run-wizard:{DAY0.as_posix()}" in main_window._tabs

    (workspace_root / ADD_VLAN).write_text(
        (workspace_root / ADD_VLAN).read_text(encoding="utf-8") + "\nbogus_key: 1\n",
        encoding="utf-8",
    )
    gui_ctx.reload_workspace(block=True)
    assert not _actions(view.menu_for(_play_item(view, ADD_VLAN)))["Run…"].isEnabled()


def test_plays_menu_delete_recycles_and_closes_tab(
    main_window, gui_ctx, workspace_root, monkeypatch, asked
):
    moved = []

    def fake_recycle(path):
        moved.append(path)
        path.unlink()

    monkeypatch.setattr("tsunami.core.inventory_edit.send_to_recycle_bin", fake_recycle)
    reload = gui_ctx.reload_workspace
    monkeypatch.setattr(gui_ctx, "reload_workspace", lambda block=False: reload(block=True))
    view = _plays(main_window)
    item = _play_item(view, ADD_VLAN)
    _actions(view.menu_for(item))["Open"].trigger()
    key = f"play:{ADD_VLAN.as_posix()}"
    assert key in main_window._tabs

    asked.answer = QMessageBox.StandardButton.No
    _actions(view.menu_for(item))["Delete…"].trigger()
    assert moved == [] and key in main_window._tabs

    asked.answer = YES
    _actions(view.menu_for(item))["Delete…"].trigger()
    assert moved == [workspace_root / ADD_VLAN]
    assert key not in main_window._tabs
    assert ADD_VLAN not in gui_ctx.workspace.plays
