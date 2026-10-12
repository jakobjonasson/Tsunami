"""Editing inventory from the Inventory view: context menus, dialogs, the site tab."""

from __future__ import annotations

import shutil

import pytest
from PySide6.QtWidgets import QMessageBox, QTreeWidgetItem

from tsunami.core.secret_store import LoginCredential
from tsunami.gui.screens import inventory_dialogs
from tsunami.gui.screens.inventory import InventoryView
from tsunami.gui.screens.inventory_dialogs import (
    CustomerDialog,
    DeviceDialog,
    SiteDialog,
    SiteView,
)

YES = QMessageBox.StandardButton.Yes
NO = QMessageBox.StandardButton.No


def _view(window) -> InventoryView:
    for page in window._views.values():
        found = page.findChild(InventoryView)
        if found is not None:
            return found
    raise AssertionError("InventoryView not hosted")


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


def _has(tree, *names: str) -> bool:
    try:
        _find(tree, *names)
    except StopIteration:
        return False
    return True


def _trigger(view: InventoryView, path: tuple[str, ...] | None, text: str) -> None:
    item = _find(view.tree, *path) if path else None
    for action in view.menu_for(item).actions():
        if action.text() == text:
            assert action.isEnabled()
            action.trigger()
            return
    raise AssertionError(f"no action {text!r}")


def _texts(menu) -> list[str]:
    return [a.text() for a in menu.actions() if not a.isSeparator()]


class Dialogs:
    """Replaces run_dialog: fill the dialog, call save(), remember both."""

    def __init__(self) -> None:
        self.fill = lambda dialog: None
        self.dialogs: list = []
        self.saved: list[bool] = []

    def __call__(self, dialog) -> bool:
        self.dialogs.append(dialog)
        self.fill(dialog)
        ok = dialog.save()
        self.saved.append(ok)
        return ok


@pytest.fixture(autouse=True)
def _no_blocking(monkeypatch, gui_ctx):
    # Reloads happen at once, so the tree is current as soon as an edit returns.
    reload = gui_ctx.reload_workspace
    monkeypatch.setattr(gui_ctx, "reload_workspace", lambda block=False: reload(block=True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: YES)

    def no_recycle_bin(path):
        raise AssertionError(f"test would send {path} to the real Recycle Bin")

    monkeypatch.setattr("tsunami.core.inventory_edit.send_to_recycle_bin", no_recycle_bin)


@pytest.fixture
def recycled(monkeypatch):
    moved: list = []

    def fake_recycle(path):
        moved.append(path)
        shutil.rmtree(path) if path.is_dir() else path.unlink()

    monkeypatch.setattr("tsunami.core.inventory_edit.send_to_recycle_bin", fake_recycle)
    return moved


@pytest.fixture
def dialogs(monkeypatch) -> Dialogs:
    seam = Dialogs()
    monkeypatch.setattr(inventory_dialogs, "run_dialog", seam)
    return seam


@pytest.fixture
def asked(monkeypatch):
    """Records QMessageBox.question calls; answer with asked.answer."""
    box = type("Asked", (), {"answer": YES, "texts": []})()

    def question(parent, title, text, *a, **k):
        box.texts.append(text)
        return box.answer

    monkeypatch.setattr(QMessageBox, "question", question)
    return box


def _device(ctx, name: str, customer: str = "acme"):
    return ctx.workspace.customers[customer].device(name)


def _fill_device(dialog: DeviceDialog, **values) -> None:
    if "name" in values:
        dialog.name_edit.setText(values["name"])
    if "host" in values:
        dialog.host_edit.setText(values["host"])
    if "port" in values:
        dialog.port_spin.setValue(values["port"])
    if "site" in values:
        dialog.site_combo.setCurrentIndex(dialog.site_combo.findData(values["site"]))
    if "type" in values:
        dialog.type_combo.setCurrentIndex(dialog.type_combo.findData(values["type"]))
    if "platform" in values:
        dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData(values["platform"]))
    if "model" in values:
        dialog.model_combo.setCurrentIndex(dialog.model_combo.findData(values["model"]))
    if "login" in values:
        picker = dialog.login.combo
        picker.setCurrentIndex(picker.findData(values["login"]))
    if "vars" in values:
        dialog.vars_edit.setPlainText(values["vars"])


# -- devices ---------------------------------------------------------------------------


def test_device_rows_show_type_label_and_platform(main_window):
    view = _view(main_window)
    row = _find(view.tree, "acme", "site-a", "sa-as01")
    assert row.text(0).split() == ["sa-as01", "Access", "switch", "·", "ios-xe"]
    assert not row.icon(0).isNull()


def test_custom_role_shows_slug(main_window, gui_ctx, workspace_root):
    (workspace_root / "inventory/roles/lab-gear.yml").write_text("x: 1\n")
    devices = workspace_root / "inventory/customers/acme/devices.yml"
    devices.write_text(
        devices.read_text().replace("role: access", "role: lab-gear", 1), encoding="utf-8"
    )
    main_window.refresh_all()
    row = _find(_view(main_window).tree, "acme", "site-a", "sa-as01")
    assert "lab-gear" in row.text(0)


def test_add_device(main_window, gui_ctx, dialogs):
    view = _view(main_window)
    dialogs.fill = lambda d: _fill_device(
        d,
        name="sa-co01",
        host="203.0.113.50",
        type="core",
        platform="aos-cx",
        vars="mgmt_ip: 203.0.113.50/24\n",
    )
    _trigger(view, ("acme", "site-a"), "New device…")
    assert dialogs.saved == [True]
    dev = _device(gui_ctx, "sa-co01")
    assert (dev.role, str(dev.platform), dev.site, dev.port) == ("core", "aos-cx", "site-a", 22)
    assert dev.vars == {"mgmt_ip": "203.0.113.50/24"}
    row = _find(view.tree, "acme", "site-a", "sa-co01")
    assert "Core switch" in row.text(0)


def test_site_menu_preselects_its_site(main_window, gui_ctx, dialogs):
    view = _view(main_window)
    _trigger(view, ("acme", "site-a"), "New device…")
    assert dialogs.dialogs[0].site_combo.currentData() == "site-a"


def test_edit_and_rename_device(main_window, gui_ctx, dialogs):
    view = _view(main_window)
    dialogs.fill = lambda d: _fill_device(
        d, name="sa-as09", host="203.0.113.99", port=2222, type="distribution"
    )
    _trigger(view, ("acme", "site-a", "sa-as01"), "Edit…")
    assert dialogs.saved == [True]
    assert _device(gui_ctx, "sa-as01") is None
    dev = _device(gui_ctx, "sa-as09")
    assert (str(dev.host), dev.port, dev.role) == ("203.0.113.99", 2222, "distribution")
    assert dev.vars == {"mgmt_ip": "203.0.113.11/24"}  # untouched
    assert _has(view.tree, "acme", "site-a", "sa-as09")
    assert not _has(view.tree, "acme", "site-a", "sa-as01")


def test_edit_without_changes_writes_nothing(main_window, gui_ctx, dialogs, workspace_root):
    path = workspace_root / "inventory/customers/acme/devices.yml"
    before = path.read_bytes()
    view = _view(main_window)
    _trigger(view, ("acme", "site-a", "sa-as02"), "Edit…")
    assert dialogs.saved == [True]
    assert path.read_bytes() == before


def test_device_login_combo(main_window, gui_ctx, dialogs):
    view = _view(main_window)
    gui_ctx.secrets.set_login("acme-lab", LoginCredential("lab", "Lab-Pass-1"))
    row = ("acme", "site-a", "sa-as01")

    dialogs.fill = lambda d: _fill_device(d, login="acme-lab")
    _trigger(view, row, "Edit…")
    assert _device(gui_ctx, "sa-as01").credential == "acme-lab"

    dialogs.fill = lambda d: _fill_device(d, login="acme-netadmin")
    _trigger(view, row, "Edit…")
    assert _device(gui_ctx, "sa-as01").credential == "acme-netadmin"

    def default(d):
        assert d.login.combo.itemText(0) == "Default (acme-netadmin)"
        d.login.combo.setCurrentIndex(0)

    dialogs.fill = default
    _trigger(view, row, "Edit…")
    assert dialogs.saved == [True, True, True]
    assert _device(gui_ctx, "sa-as01").credential is None


def test_default_login_text_follows_site_default(main_window, gui_ctx, dialogs):
    gui_ctx.secrets.set_login("acme-lab", LoginCredential("lab", "Lab-Pass-1"))
    editor = inventory_dialogs.make_editor(gui_ctx)
    editor.update_site("acme", "site-a", credential="acme-lab", set_credential=True)
    main_window.refresh_all()
    view = _view(main_window)
    seen: list[str] = []
    dialogs.fill = lambda d: seen.append(d.login.combo.itemText(0))
    _trigger(view, ("acme", "site-a", "sa-as01"), "Edit…")
    assert seen == ["Default (acme-lab)"]


def test_new_login_button_adds_and_selects(main_window, gui_ctx, monkeypatch):
    dialog = DeviceDialog(gui_ctx, main_window, "acme", site="site-a")
    gui_ctx.secrets.set_login("acme-new", LoginCredential("new", "New-Pass-1"))
    monkeypatch.setattr(inventory_dialogs, "prompt_new_login", lambda ctx, parent: "acme-new")
    dialog.login.new_login()
    assert dialog.login.login() == "acme-new"
    assert dialog.login.combo.itemText(0).startswith("Default (")


def test_model_follows_platform(main_window, gui_ctx):
    dialog = DeviceDialog(gui_ctx, main_window, "acme", site="site-a")
    dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData("aos-cx"))
    assert dialog.model_combo.itemText(0) == "(none)"
    assert dialog.model_combo.count() > 1
    aos_cx = {dialog.model_combo.itemData(i) for i in range(1, dialog.model_combo.count())}
    dialog.platform_combo.setCurrentIndex(dialog.platform_combo.findData("ios-xe"))
    ios_xe = {dialog.model_combo.itemData(i) for i in range(1, dialog.model_combo.count())}
    assert aos_cx and ios_xe and not aos_cx & ios_xe


def test_invalid_device_shows_inline_error_and_writes_nothing(main_window, gui_ctx, workspace_root):
    path = workspace_root / "inventory/customers/acme/devices.yml"
    before = path.read_bytes()
    dialog = DeviceDialog(gui_ctx, main_window, "acme", site="site-a")
    _fill_device(dialog, name="sa-as01", host="203.0.113.77")  # name already used
    assert dialog.save() is False
    assert not dialog.error_label.isHidden()
    assert "sa-as01" in dialog.error_label.text()
    assert path.read_bytes() == before

    _fill_device(dialog, name="Bad Name!")
    assert dialog.save() is False
    assert path.read_bytes() == before

    _fill_device(dialog, name="sa-as20", host="203.0.113.77", vars="[not, a, mapping]")
    assert dialog.save() is False
    assert "mapping" in dialog.error_label.text()
    _fill_device(dialog, vars="a: [")
    assert dialog.save() is False
    assert "YAML" in dialog.error_label.text()
    assert path.read_bytes() == before

    _fill_device(dialog, vars="")
    assert dialog.save() is True  # a later valid save clears the error
    assert dialog.error_label.isHidden()
    assert _device(gui_ctx, "sa-as20") is not None


def test_delete_device_asks_first(main_window, gui_ctx, asked):
    view = _view(main_window)
    row = ("acme", "site-a", "sa-as02")
    asked.answer = NO
    _trigger(view, row, "Delete…")
    assert _device(gui_ctx, "sa-as02") is not None
    assert "sa-as02 from devices.yml" in asked.texts[-1]
    asked.answer = YES
    _trigger(view, ("acme", "site-a", "sa-as02"), "Delete…")
    assert _device(gui_ctx, "sa-as02") is None
    assert not _has(view.tree, "acme", "site-a", "sa-as02")


def test_device_properties_opens_tab(main_window):
    view = _view(main_window)
    _trigger(view, ("acme", "site-a", "sa-as01"), "Properties")
    assert "device:acme/sa-as01" in main_window._tabs


# -- menus -----------------------------------------------------------------------------


def test_menu_contents(main_window):
    view = _view(main_window)
    tree = view.tree
    assert _texts(view.menu_for(_find(tree, "acme", "site-a", "sa-as01"))) == [
        "Run play…",
        "Edit…",
        "Properties",
        "Delete…",
    ]
    assert _texts(view.menu_for(_find(tree, "acme", "site-a"))) == [
        "Show devices",
        "New device…",
        "Edit…",
        "View settings",
        "Delete…",
    ]
    assert _texts(view.menu_for(_find(tree, "acme"))) == [
        "Show devices",
        "Add site…",
        "Properties…",
        "Delete…",
    ]
    assert _texts(view.menu_for(None)) == ["New customer…"]


def test_menu_actions_disabled_without_workspace(main_window, gui_ctx):
    view = _view(main_window)
    item = _find(view.tree, "acme", "site-a", "sa-as01")
    gui_ctx.workspace = None
    assert all(not a.isEnabled() for a in view.menu_for(item).actions() if not a.isSeparator())
    assert all(not a.isEnabled() for a in view.menu_for(None).actions() if not a.isSeparator())


def test_context_menu_policy(main_window):
    from PySide6.QtCore import Qt

    assert _view(main_window).tree.contextMenuPolicy() == Qt.ContextMenuPolicy.CustomContextMenu


# -- sites -----------------------------------------------------------------------------


def test_empty_site_and_empty_customer_show_in_tree(main_window, gui_ctx):
    editor = inventory_dialogs.make_editor(gui_ctx)
    editor.create_site("acme", "site-empty")
    editor.create_customer("globex", name="Globex", credential="acme-netadmin")
    main_window.refresh_all()
    tree = _view(main_window).tree
    assert _find(tree, "acme", "site-empty").childCount() == 0
    assert _find(tree, "globex").childCount() == 0
    assert _has(tree, "acme", "site-a", "sa-as01")


def test_filter_keeps_empty_site_visible_when_cleared(main_window, gui_ctx):
    inventory_dialogs.make_editor(gui_ctx).create_site("acme", "site-empty")
    main_window.refresh_all()
    view = _view(main_window)
    view.filter_box.setText("as01")
    assert _find(view.tree, "acme", "site-empty").isHidden()
    view.filter_box.setText("")
    assert not _find(view.tree, "acme", "site-empty").isHidden()


def test_new_site_with_login_and_vars(main_window, gui_ctx, dialogs):
    view = _view(main_window)

    def fill(d: SiteDialog):
        d.name_edit.setText("site-b")
        d.login.combo.setCurrentIndex(d.login.combo.findData("acme-netadmin"))
        d.vars_edit.setPlainText("site_code: SB\n")

    dialogs.fill = fill
    _trigger(view, ("acme",), "Add site…")
    assert dialogs.saved == [True]
    inv = gui_ctx.workspace.customers["acme"]
    assert inv.sites["site-b"] == {"site_code": "SB"}
    assert inv.config.site_credentials == {"site-b": "acme-netadmin"}
    assert _has(view.tree, "acme", "site-b")


def test_new_site_error_is_inline(main_window, gui_ctx):
    dialog = SiteDialog(gui_ctx, main_window, "acme")
    dialog.name_edit.setText("site-a")  # exists
    assert dialog.save() is False
    assert "already exists" in dialog.error_label.text()
    dialog.name_edit.setText("Bad Site")
    assert dialog.save() is False
    assert not dialog.error_label.isHidden()


def test_rename_site_moves_devices_and_default(main_window, gui_ctx, dialogs):
    gui_ctx.secrets.set_login("acme-lab", LoginCredential("lab", "Lab-Pass-1"))
    inventory_dialogs.make_editor(gui_ctx).update_site(
        "acme", "site-a", credential="acme-lab", set_credential=True
    )
    main_window.refresh_all()
    view = _view(main_window)
    prefilled: list[str] = []

    def fill(d: SiteDialog):
        prefilled.append(d.vars_edit.toPlainText())
        assert d.login.login() == "acme-lab"
        d.name_edit.setText("hq")

    dialogs.fill = fill
    _trigger(view, ("acme", "site-a"), "Edit…")
    assert "site_code: SA" in prefilled[0]  # the file's own text
    inv = gui_ctx.workspace.customers["acme"]
    assert "site-a" not in inv.sites and "hq" in inv.sites
    assert inv.config.site_credentials == {"hq": "acme-lab"}
    assert {d.site for d in inv.devices} == {"hq"}
    assert _has(view.tree, "acme", "hq", "sa-as01")


def test_edit_site_clear_default_login_and_keep_comments(
    main_window, gui_ctx, dialogs, workspace_root
):
    site_path = workspace_root / "inventory/customers/acme/sites/site-a.yml"
    site_path.write_text("# the main site\nsite_code: SA\n", encoding="utf-8")
    inventory_dialogs.make_editor(gui_ctx).update_site(
        "acme", "site-a", credential="acme-netadmin", set_credential=True
    )
    main_window.refresh_all()
    view = _view(main_window)
    dialogs.fill = lambda d: d.login.combo.setCurrentIndex(0)
    _trigger(view, ("acme", "site-a"), "Edit…")
    assert dialogs.saved == [True]
    assert gui_ctx.workspace.customers["acme"].config.site_credentials == {}
    assert site_path.read_text(encoding="utf-8") == "# the main site\nsite_code: SA\n"


def test_delete_site_without_devices(main_window, gui_ctx, asked, recycled):
    inventory_dialogs.make_editor(gui_ctx).create_site("acme", "site-empty")
    main_window.refresh_all()
    view = _view(main_window)
    _trigger(view, ("acme", "site-empty"), "Delete…")
    assert "Recycle Bin" in asked.texts[-1] and "devices" not in asked.texts[-1]
    assert [p.name for p in recycled] == ["site-empty.yml"]
    assert not _has(view.tree, "acme", "site-empty")


def test_delete_site_with_devices_needs_consent(main_window, gui_ctx, asked, recycled):
    # Another site stays: today the loader cannot load a customer with no sites at all
    # when a play has a site prompt (reported to the core owner).
    inventory_dialogs.make_editor(gui_ctx).create_site("acme", "site-b")
    main_window.refresh_all()
    view = _view(main_window)
    asked.answer = NO
    _trigger(view, ("acme", "site-a"), "Delete…")
    assert "2 devices" in asked.texts[-1]
    assert recycled == [] and len(gui_ctx.workspace.customers["acme"].devices) == 2
    asked.answer = YES
    _trigger(view, ("acme", "site-a"), "Delete…")
    inv = gui_ctx.workspace.customers["acme"]
    assert inv.devices == () and "site-a" not in inv.sites
    assert [p.name for p in recycled] == ["site-a.yml"]
    assert [c.text(0) for c in _children(_find(view.tree, "acme"))] == ["site-b"]


def test_view_site_tab(main_window, gui_ctx, dialogs):
    view = _view(main_window)
    _trigger(view, ("acme", "site-a"), "View settings")
    tab = main_window._tabs["site:acme/site-a"]
    assert isinstance(tab, SiteView)
    assert "acme-netadmin" in tab.details.text()
    assert "none set" in tab.details.text()
    assert tab.device_list.topLevelItemCount() == 2
    assert tab.device_list.topLevelItem(0).text(1) == "Access switch"
    assert "site_code: SA" in tab.vars_view.toPlainText()
    dialogs.fill = lambda d: d.vars_edit.setPlainText("site_code: SA\nfloor: 3\n")
    tab.edit_button.click()
    assert dialogs.saved == [True]
    assert "floor: 3" in tab.vars_view.toPlainText()


# -- customers -------------------------------------------------------------------------


def test_new_customer(main_window, gui_ctx, dialogs):
    view = _view(main_window)

    def fill(d: CustomerDialog):
        d.slug_edit.setText("globex")
        d.name_edit.setText("Globex Ltd")
        d.login.combo.setCurrentIndex(d.login.combo.findData("acme-netadmin"))
        d.lab_check.setChecked(True)
        d.jump_group.setChecked(True)
        d.jump_host_edit.setText("192.0.2.5")
        d.jump_port.setValue(2200)
        d.jump_login.combo.setCurrentIndex(d.jump_login.combo.findData("acme-netadmin"))

    dialogs.fill = fill
    _trigger(view, None, "New customer…")
    assert dialogs.saved == [True]
    config = gui_ctx.workspace.customers["globex"].config
    assert (config.name, config.credential, config.lab) == ("Globex Ltd", "acme-netadmin", True)
    assert (str(config.jump_host.host), config.jump_host.port) == ("192.0.2.5", 2200)
    assert _has(view.tree, "globex")


def test_new_customer_errors_inline(main_window, gui_ctx):
    dialog = CustomerDialog(gui_ctx, main_window)
    dialog.slug_edit.setText("acme")  # exists
    dialog.name_edit.setText("Again")
    assert dialog.save() is False
    assert "already exists" in dialog.error_label.text()
    dialog.slug_edit.setText("globex")
    dialog.name_edit.setText("")
    assert dialog.save() is False
    dialog.name_edit.setText("Globex")
    dialog.jump_group.setChecked(True)
    assert dialog.save() is False  # jump host without a host
    assert "globex" not in gui_ctx.workspace.customers


def test_edit_customer_login_and_jump_host(main_window, gui_ctx, dialogs):
    gui_ctx.secrets.set_login("acme-lab", LoginCredential("lab", "Lab-Pass-1"))
    view = _view(main_window)

    def fill(d: CustomerDialog):
        assert not d.slug_edit.isEnabled() or d.slug_edit.isReadOnly()
        assert d.name_edit.text() == "Acme Corporation"
        assert not d.jump_group.isChecked()
        d.login.combo.setCurrentIndex(d.login.combo.findData("acme-lab"))
        d.jump_group.setChecked(True)
        d.jump_host_edit.setText("192.0.2.9")
        d.jump_login.combo.setCurrentIndex(d.jump_login.combo.findData("acme-netadmin"))

    dialogs.fill = fill
    _trigger(view, ("acme",), "Properties…")
    config = gui_ctx.workspace.customers["acme"].config
    assert config.credential == "acme-lab"
    assert (str(config.jump_host.host), config.jump_host.port) == ("192.0.2.9", 22)

    dialogs.fill = lambda d: d.jump_group.setChecked(False)
    _trigger(view, ("acme",), "Properties…")
    assert gui_ctx.workspace.customers["acme"].config.jump_host is None
    assert dialogs.saved == [True, True]


def test_delete_customer(main_window, gui_ctx, asked, recycled):
    view = _view(main_window)
    asked.answer = NO
    _trigger(view, ("acme",), "Delete…")
    assert "acme folder, with its 1 site and 2 devices, to the Recycle Bin" in asked.texts[-1]
    assert recycled == [] and "acme" in gui_ctx.workspace.customers
    asked.answer = YES
    _trigger(view, ("acme",), "Delete…")
    assert [p.name for p in recycled] == ["acme"]
    assert "acme" not in gui_ctx.workspace.customers
    assert view.tree.topLevelItemCount() == 0


def test_no_secret_values_in_dialogs(main_window, gui_ctx):
    from tests.gui.conftest import LOGIN_PASSWORD, all_widget_text

    for dialog in (
        DeviceDialog(gui_ctx, main_window, "acme", site="site-a"),
        SiteDialog(gui_ctx, main_window, "acme", site="site-a"),
        CustomerDialog(gui_ctx, main_window, slug="acme"),
    ):
        text = all_widget_text(dialog)
        assert LOGIN_PASSWORD not in text
        assert "acme-netadmin" in text
