"""Global plays in the GUI: Plays view group, run wizard, picker, copy to customer, wheel."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
from PySide6.QtCore import QPoint, QPointF, Qt
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QApplication,
)

from tsunami.core.models.play import RunMode
from tsunami.gui.screens import plays as plays_screen
from tsunami.gui.screens.device_list import PlayPickerDialog, pick_play_and_run
from tsunami.gui.screens.plays import CopyPlayDialog, PlaysView
from tsunami.gui.screens.run_wizard import RunWizard, open_run_wizard

ADD_VLAN = Path("plays/push/add-vlan.yml")
GLOBAL_VLAN = Path("plays/push/global-vlan.yml")


@pytest.fixture
def global_play(gui_ctx, workspace_root) -> Path:
    text = (workspace_root / ADD_VLAN).read_text(encoding="utf-8")
    text = text.replace("customer: acme", "customer: global").replace(
        "name: Add VLAN", "name: Global VLAN"
    )
    (workspace_root / GLOBAL_VLAN).write_text(text, encoding="utf-8")
    gui_ctx.open_workspace(workspace_root, block=True)
    assert gui_ctx.workspace.plays[GLOBAL_VLAN].play is not None
    return GLOBAL_VLAN


def _plays(main_window) -> PlaysView:
    return main_window._views["plays"].findChild(PlaysView)


def _find(view, path):
    stack = [view.tree.topLevelItem(i) for i in range(view.tree.topLevelItemCount())]
    while stack:
        item = stack.pop()
        if item.data(0, plays_screen._PATH_ROLE) == path:
            return item
        stack.extend(item.child(i) for i in range(item.childCount()))
    raise AssertionError(path)


def test_global_group_comes_first(main_window, global_play):
    view = _plays(main_window)
    push = next(
        view.tree.topLevelItem(i)
        for i in range(view.tree.topLevelItemCount())
        if view.tree.topLevelItem(i).text(0) == "Push"
    )
    assert [push.child(i).text(0) for i in range(push.childCount())] == ["Global", "acme"]
    item = _find(view, global_play)
    assert item.parent() is push.child(0)
    assert item.text(0) == "Global VLAN"


def test_wizard_requires_a_customer_and_uses_it(main_window, gui_ctx, global_play):
    wizard = open_run_wizard(gui_ctx, main_window, global_play)
    assert isinstance(wizard, RunWizard)
    assert wizard.customer_combo is not None
    assert wizard.customer_name() is None
    assert not wizard.preview_button.isEnabled()
    assert not wizard.preview()
    assert wizard.devices == ()

    wizard.set_customer("acme")
    assert wizard.customer_name() == "acme"
    assert [d.name for d in wizard.selected_devices()] == ["sa-as01", "sa-as02"]
    wizard.fields["vlan_id"].set_value(30)
    wizard.fields["vlan_name"].set_value("Cameras")
    assert wizard.preview()
    assert "vlan 30" in wizard.preview_tabs.widget(0).toPlainText()
    request = wizard.build_request()
    assert request.customer == "acme"
    assert request.play_path == global_play

    started = []
    main_window.start_run = lambda req, title: started.append(req)
    wizard.start_button.click()
    assert [r.customer for r in started] == ["acme"]
    assert started[0].mode == RunMode.CHECK

    wizard.customer_combo.setCurrentIndex(0)  # back to none: everything resets
    assert wizard.devices == ()
    assert not wizard.start_button.isEnabled()


def test_wizard_opened_with_customer_preselects_it(main_window, gui_ctx, global_play):
    wizard = open_run_wizard(
        gui_ctx, main_window, global_play, devices=["sa-as02"], customer="acme"
    )
    assert wizard.customer_combo.currentData() == "acme"
    assert [d.name for d in wizard.selected_devices()] == ["sa-as02"]
    # a customer-specific play ignores the parameter
    other = open_run_wizard(gui_ctx, main_window, ADD_VLAN, customer="acme")
    assert other.customer_combo is None and other.customer_name() == "acme"


def test_picker_offers_global_play_for_acme_devices(main_window, gui_ctx, global_play, monkeypatch):
    dialog = PlayPickerDialog(gui_ctx, "acme", ["sa-as01", "sa-as02"])
    assert global_play in dialog.offered_paths()
    names = [dialog.tree.topLevelItem(i).text(0) for i in range(dialog.tree.topLevelItemCount())]
    assert "Global VLAN (global)" in names and "Add VLAN" in names

    def run(d):
        for i in range(d.tree.topLevelItemCount()):
            if d.tree.topLevelItem(i).data(0, 256) == global_play:
                d.tree.setCurrentItem(d.tree.topLevelItem(i))
        return True

    monkeypatch.setattr("tsunami.gui.screens.device_list.run_picker", run)
    assert pick_play_and_run(gui_ctx, main_window, "acme", ["sa-as01"])
    wizard = main_window._tabs[f"run-wizard:{global_play.as_posix()}"]
    assert wizard.customer_name() == "acme"
    assert [d.name for d in wizard.selected_devices()] == ["sa-as01"]


def test_copy_to_customer(qtbot, main_window, gui_ctx, workspace_root, monkeypatch):
    customers = workspace_root / "inventory" / "customers"
    shutil.copytree(customers / "acme", customers / "beta")
    gui_ctx.open_workspace(workspace_root, block=True)
    view = _plays(main_window)
    menu = view.menu_for(_find(view, ADD_VLAN))
    action = next(a for a in menu.actions() if a.text() == "Copy to customer…")
    assert action.isEnabled()

    seen = []

    def run(dialog: CopyPlayDialog) -> bool:
        seen.append(dialog)
        assert [
            dialog.customer_combo.itemText(i) for i in range(dialog.customer_combo.count())
        ] == ["Global (any customer)", "beta"]
        dialog.customer_combo.setCurrentIndex(1)
        assert dialog.name_edit.text() == "add-vlan-beta"
        dialog.name_edit.setText("add-vlan")  # exists: the error shows inline
        dialog.accept()
        assert not dialog.error_label.isHidden() and "already exists" in dialog.error_label.text()
        dialog.name_edit.setText("add-vlan-beta")
        dialog.accept()
        return dialog.new_path is not None

    monkeypatch.setattr(plays_screen, "run_dialog", run)
    action.trigger()
    new = Path("plays/push/add-vlan-beta.yml")
    assert seen and (workspace_root / new).is_file()
    assert "customer: beta" in (workspace_root / new).read_text(encoding="utf-8")
    qtbot.waitUntil(lambda: new in gui_ctx.workspace.plays, timeout=10000)
    assert f"play:{new.as_posix()}" in main_window._tabs


def test_wheel_never_changes_a_number_box_or_dropdown(qtbot):
    """The boxes leave the wheel unhandled, so Qt scrolls the page with it. (Qt 6 passes
    only real, spontaneous wheel events up to the parent, so the scroll itself is Qt's.)"""
    from tsunami.gui.wheel import ComboBox, SpinBox

    spin = SpinBox()
    spin.setValue(5)
    combo = ComboBox()
    combo.addItems(["a", "b", "c"])
    for widget in (spin, combo):
        qtbot.addWidget(widget)

    def wheel(target, delta):
        pos = QPointF(target.width() / 2, target.height() / 2)
        event = QWheelEvent(
            pos,
            target.mapToGlobal(pos),
            QPoint(0, 0),
            QPoint(0, delta),
            Qt.MouseButton.NoButton,
            Qt.KeyboardModifier.NoModifier,
            Qt.ScrollPhase.NoScrollPhase,
            False,
        )
        QApplication.sendEvent(target, event)
        return event.isAccepted()

    assert wheel(spin, -120) is False and spin.value() == 5
    assert wheel(combo, -120) is False and combo.currentIndex() == 0
    qtbot.keyClick(spin, Qt.Key.Key_Up)  # keys still work
    assert spin.value() == 6


def test_copy_as_a_global_play(qtbot, main_window, gui_ctx, workspace_root, monkeypatch):
    view = _plays(main_window)
    menu = view.menu_for(_find(view, ADD_VLAN))
    action = next(a for a in menu.actions() if a.text() == "Copy to customer…")

    def run(dialog: CopyPlayDialog) -> bool:
        assert dialog.customer_combo.itemText(0) == "Global (any customer)"
        assert dialog.customer_combo.currentData() == "global"
        assert dialog.name_edit.text() == "add-vlan-global"  # "add-vlan" is taken
        dialog.accept()
        return dialog.new_path is not None

    monkeypatch.setattr(plays_screen, "run_dialog", run)
    action.trigger()
    new = Path("plays/push/add-vlan-global.yml")
    assert "customer: global" in (workspace_root / new).read_text(encoding="utf-8")
    qtbot.waitUntil(lambda: new in gui_ctx.workspace.plays, timeout=10000)
    assert gui_ctx.workspace.plays[new].play.customer == "global"
    labels = [view.tree.topLevelItem(i).text(0) for i in range(view.tree.topLevelItemCount())]
    push = next(
        view.tree.topLevelItem(i)
        for i in range(view.tree.topLevelItemCount())
        if view.tree.topLevelItem(i).text(0) == "Push"
    )
    assert push.child(0).text(0) == "Global", labels  # Global first under the type


def test_copy_dialog_strips_the_customer_from_a_global_name(main_window, gui_ctx):
    dialog = CopyPlayDialog(gui_ctx, Path("plays/baseline/acme-baseline.yml"), main_window)
    assert dialog.customer_combo.currentData() == "global"
    assert dialog.name_edit.text() == "baseline"
