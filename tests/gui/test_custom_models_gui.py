"""Custom hardware models from the GUI, and the MCP tier setting (0.7.1)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QMessageBox

from tsunami.core.catalog import CUSTOM_MODELS, load_catalog
from tsunami.core.inventory_edit import InventoryEditor
from tsunami.core.policy import load_policy
from tsunami.gui.screens import inventory_dialogs, settings
from tsunami.gui.screens.inventory import InventoryView
from tsunami.gui.screens.inventory_dialogs import (
    DeviceDialog,
    HardwareModelsDialog,
    ModelDialog,
)


@pytest.fixture(autouse=True)
def _blocking_reload(monkeypatch, gui_ctx):
    reload = gui_ctx.reload_workspace
    monkeypatch.setattr(gui_ctx, "reload_workspace", lambda block=False: reload(block=True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.StandardButton.Yes)


class Dialogs:
    """Replaces run_dialog: fill the dialog, then save() it as OK would."""

    def __init__(self) -> None:
        self.fill = lambda dialog: None
        self.dialogs: list = []

    def __call__(self, dialog) -> bool:
        self.dialogs.append(dialog)
        self.fill(dialog)
        return dialog.save()


@pytest.fixture
def dialogs(monkeypatch) -> Dialogs:
    seam = Dialogs()
    monkeypatch.setattr(inventory_dialogs, "run_dialog", seam)
    return seam


def _fill_model(model_id: str, label: str, platform: str = "ios-xe"):
    def fill(dialog):
        if isinstance(dialog, ModelDialog):
            dialog.id_edit.setText(model_id)
            dialog.label_edit.setText(label)
            dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(platform))
            dialog.pattern_edit.setText("^IE-3300")
            dialog.examples_edit.setText("IE-3300-8T2S, IE-3300-8P2S")

    return fill


def _inventory(window) -> InventoryView:
    for page in window._views.values():
        found = page.findChild(InventoryView)
        if found is not None:
            return found
    raise AssertionError("InventoryView not hosted")


def test_the_inventory_menu_opens_hardware_models(main_window, gui_ctx, dialogs):
    view = _inventory(main_window)
    [action] = [a for a in view.menu_for(None).actions() if a.text() == "Hardware models…"]
    action.trigger()
    [dialog] = dialogs.dialogs
    assert isinstance(dialog, HardwareModelsDialog)
    sources = {
        dialog.tree.topLevelItem(i).text(1): dialog.tree.topLevelItem(i).text(4)
        for i in range(dialog.tree.topLevelItemCount())
    }
    assert sources["cisco-c9300"] == "built in"


def test_add_edit_and_delete_a_custom_model(main_window, gui_ctx, dialogs):
    dialog = HardwareModelsDialog(gui_ctx, main_window)
    hostile = "<b>IE</b> <a href='x'>3300</a>"
    dialogs.fill = _fill_model("cisco-ie3300", hostile)
    assert dialog.add_model()
    entry = load_catalog(gui_ctx.workspace_path).get("cisco-ie3300")
    assert entry.label == hostile and entry.examples == ["IE-3300-8T2S", "IE-3300-8P2S"]
    item = dialog.tree.currentItem()
    assert (item.text(0), item.text(4)) == (hostile, "custom")  # a tree item is plain text
    assert dialog.edit_button.isEnabled() and dialog.delete_button.isEnabled()

    def relabel(d):
        assert not d.id_edit.isEnabled()  # devices name the id
        d.label_edit.setText("Catalyst IE3300")

    dialogs.fill = relabel
    assert dialog.edit_selected()
    assert load_catalog(gui_ctx.workspace_path).get("cisco-ie3300").label == "Catalyst IE3300"
    assert "IE-3300-8P2S" in (gui_ctx.workspace_path / CUSTOM_MODELS).read_text("utf-8")

    InventoryEditor(gui_ctx.workspace_path).update_device(
        "acme", "sa-as01", {"model": "cisco-ie3300"}
    )
    gui_ctx.reload_workspace()
    assert not dialog.delete_selected()
    assert "used by acme/sa-as01" in dialog.error_label.text()
    InventoryEditor(gui_ctx.workspace_path).update_device("acme", "sa-as01", {"model": None})
    assert dialog.delete_selected()
    assert "cisco-ie3300" not in load_catalog(gui_ctx.workspace_path).ids()


def test_built_in_models_cannot_be_edited(main_window, gui_ctx):
    dialog = HardwareModelsDialog(gui_ctx, main_window)
    builtin = next(
        dialog.tree.topLevelItem(i)
        for i in range(dialog.tree.topLevelItemCount())
        if dialog.tree.topLevelItem(i).text(1) == "cisco-c9300"
    )
    dialog.tree.setCurrentItem(builtin)
    assert not dialog.edit_button.isEnabled() and not dialog.delete_button.isEnabled()
    assert not dialog.edit_selected() and not dialog.delete_selected()


def test_a_bad_model_is_refused_inline(main_window, gui_ctx, dialogs):
    dialog = ModelDialog(gui_ctx, main_window)
    dialog.id_edit.setText("cisco-c9300")
    dialog.label_edit.setText("Shadow")
    assert not dialog.save()
    assert "built-in" in dialog.error_label.text()
    dialog.id_edit.setText("new-one")
    dialog.pattern_edit.setText("(unclosed")
    assert not dialog.save()
    assert "regex" in dialog.error_label.text()


def test_the_device_dialog_can_add_a_model_and_select_it(main_window, gui_ctx, dialogs):
    device = gui_ctx.workspace.customers["acme"].device("sa-as01")
    dialog = DeviceDialog(gui_ctx, main_window, "acme", device=device)
    dialogs.fill = _fill_model("aruba-cx4100", "CX 4100i", platform="aos-cx")
    assert dialog.new_model()
    assert dialog.platform_combo.currentData() == "aos-cx"
    assert dialog.model_combo.currentData() == "aruba-cx4100"
    assert dialog.save()
    saved = gui_ctx.workspace.customers["acme"].device("sa-as01")
    assert (saved.model, str(saved.platform)) == ("aruba-cx4100", "aos-cx")


def test_highest_tool_tier_is_saved_to_the_policy(main_window, gui_ctx):
    tab = settings.open_settings(gui_ctx, main_window)
    assert tab.tier_combo.currentData() == 2
    tab.tier_combo.setCurrentIndex(tab.tier_combo.findData(1))
    assert tab.save_all()
    assert load_policy(gui_ctx.paths.policy_file).max_tier == 1
