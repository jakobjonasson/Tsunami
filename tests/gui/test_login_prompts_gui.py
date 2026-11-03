"""login prompts in the run wizard, push_logins in the customer dialog and in Credentials
(New login and Customers…), and the Credentials delete warning for a login plays push."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QMessageBox

from tsunami.core.inventory_edit import InventoryEditor
from tsunami.core.secret_store import LoginCredential
from tsunami.gui.screens import credentials
from tsunami.gui.screens.credentials import credential_usage, usage_summary
from tsunami.gui.screens.inventory_dialogs import CustomerDialog
from tsunami.gui.screens.run_wizard import open_run_wizard
from tsunami.gui.widgets.prompt_widgets import LOGIN_HINT

from .conftest import LOGIN_PASSWORD, all_widget_text

ADMIN2_PASSWORD = "Admin2-Gui-Pw-77"
PLAY = "plays/push/add-admin2.yml"
PLAY_TEXT = """name: Add admin2
type: push
customer: acme
targets:
  devices: [sa-as01]
prompts:
  admin2:
    type: login
    label: Account to add
steps:
  - config:
      - username {{ login(admin2).username }} privilege 15 secret {{ login(admin2).password }}
"""


def setup(gui_ctx, push=("netadmin2", "not-saved")):
    InventoryEditor(gui_ctx.workspace_path).update_customer("acme", push_logins=list(push))
    (gui_ctx.workspace_path / PLAY).write_text(PLAY_TEXT, encoding="utf-8")
    gui_ctx.secrets.set_login("netadmin2", LoginCredential("netadmin2", ADMIN2_PASSWORD))
    gui_ctx.secrets.set_login("other-customer", LoginCredential("oc", "Other-Cust-Pw-1"))
    gui_ctx.reload_workspace(block=True)


def combo_items(combo) -> list[str]:
    return [combo.itemData(i) for i in range(combo.count()) if combo.itemData(i)]


def test_the_wizard_offers_saved_allowed_logins_by_name_only(main_window, gui_ctx):
    setup(gui_ctx)
    wizard = open_run_wizard(gui_ctx, main_window, PLAY)
    field = wizard.fields["admin2"]
    # The customer's own login and netadmin2; not the unsaved one, not another customer's.
    assert combo_items(field.editor) == ["acme-netadmin", "netadmin2"]
    assert LOGIN_HINT in all_widget_text(field)
    field.set_value("netadmin2")
    assert wizard.preview()
    preview = wizard.preview_tabs.widget(0).toPlainText()
    assert "username netadmin2 privilege 15 secret ********" in preview
    text = all_widget_text(wizard) + preview
    assert ADMIN2_PASSWORD not in text and LOGIN_PASSWORD not in text
    assert wizard.build_request().prompts == {"admin2": "netadmin2"}


def test_the_customer_dialog_edits_push_logins(qtbot, main_window, gui_ctx):
    setup(gui_ctx, push=("netadmin2",))
    dialog = CustomerDialog(gui_ctx, main_window, slug="acme")
    qtbot.addWidget(dialog)
    items = {
        dialog.push_list.item(i).text(): dialog.push_list.item(i)
        for i in range(dialog.push_list.count())
    }
    assert items["netadmin2"].checkState() == Qt.CheckState.Checked
    assert items["other-customer"].checkState() == Qt.CheckState.Unchecked
    items["other-customer"].setCheckState(Qt.CheckState.Checked)
    assert dialog.save()
    gui_ctx.reload_workspace(block=True)
    config = gui_ctx.workspace.customers["acme"].config
    assert config.push_logins == ["netadmin2", "other-customer"]

    dialog = CustomerDialog(gui_ctx, main_window, slug="acme")
    qtbot.addWidget(dialog)
    for i in range(dialog.push_list.count()):
        dialog.push_list.item(i).setCheckState(Qt.CheckState.Unchecked)
    assert dialog.save()
    gui_ctx.reload_workspace(block=True)
    assert gui_ctx.workspace.customers["acme"].config.push_logins == []
    text = (gui_ctx.workspace_path / "inventory/customers/acme/customer.yml").read_text("utf-8")
    assert "push_logins" not in text  # an empty list removes the key


def _credentials_view(main_window):
    return main_window._views["credentials"].findChild(credentials.CredentialsView)


def _add_globex(gui_ctx):
    editor = InventoryEditor(gui_ctx.workspace_path)
    editor.create_customer("globex", name="Globex", credential="globex-admin")


def test_a_new_login_can_name_the_customers_that_may_push_it(main_window, gui_ctx, monkeypatch):
    setup(gui_ctx, push=())
    _add_globex(gui_ctx)
    gui_ctx.reload_workspace(block=True)
    seen = {}

    def fill(dialog):
        seen["rows"] = {s: i.text() for s, i in dialog.customers.items().items()}
        dialog.name_edit.setText("spare-admin")
        dialog.username_edit.setText("spare")
        dialog.value_edit.setText("Spare-Admin-Pw-31")
        dialog.customers.items()["acme"].setCheckState(Qt.CheckState.Checked)
        return True

    monkeypatch.setattr(credentials, "run_dialog", fill)
    _credentials_view(main_window).new_login()
    gui_ctx.reload_workspace(block=True)

    assert seen["rows"] == {"acme": "acme", "globex": "globex"}
    customers = gui_ctx.workspace.customers
    assert customers["acme"].config.push_logins == ["spare-admin"]
    assert customers["globex"].config.push_logins == []
    # The play that asks for a login now offers it for acme.
    wizard = open_run_wizard(gui_ctx, main_window, PLAY)
    assert combo_items(wizard.fields["admin2"].editor) == ["acme-netadmin", "spare-admin"]
    assert "Spare-Admin-Pw-31" not in all_widget_text(main_window)


def test_customers_menu_changes_who_may_push_a_saved_login(main_window, gui_ctx, monkeypatch):
    setup(gui_ctx, push=())
    _add_globex(gui_ctx)
    gui_ctx.reload_workspace(block=True)
    view = _credentials_view(main_window)
    menu = view.menu_for(view.login_list.findItems("netadmin2", Qt.MatchFlag.MatchExactly)[0])

    def tick(*slugs):
        def fill(dialog):
            for slug, item in dialog.customers.items().items():
                wanted = slug in slugs
                item.setCheckState(Qt.CheckState.Checked if wanted else Qt.CheckState.Unchecked)
            return True

        return fill

    monkeypatch.setattr(credentials, "run_dialog", tick("acme", "globex"))
    next(a for a in menu.actions() if a.text() == "Customers…").trigger()
    gui_ctx.reload_workspace(block=True)
    customers = gui_ctx.workspace.customers
    assert customers["acme"].config.push_logins == ["netadmin2"]
    assert customers["globex"].config.push_logins == ["netadmin2"]

    monkeypatch.setattr(credentials, "run_dialog", tick("globex"))
    view.edit_customers("netadmin2")
    gui_ctx.reload_workspace(block=True)
    customers = gui_ctx.workspace.customers
    assert customers["acme"].config.push_logins == []
    assert customers["globex"].config.push_logins == ["netadmin2"]


def test_a_customer_that_logs_in_with_a_login_cannot_be_unticked(main_window, gui_ctx):
    setup(gui_ctx, push=())
    rows = credentials.PushCustomers(gui_ctx.workspace, "acme-netadmin")
    item = rows.items()["acme"]
    assert item.text() == "acme (logs in with it)"
    assert item.checkState() == Qt.CheckState.Checked
    assert not item.flags() & Qt.ItemFlag.ItemIsEnabled
    assert rows.choices() == {}  # never written: it is always allowed
    view = _credentials_view(main_window)
    assert view.save_customers("acme-netadmin", {"acme": True})
    gui_ctx.reload_workspace(block=True)
    assert gui_ctx.workspace.customers["acme"].config.push_logins == []


def test_used_by_lists_the_customers_whose_plays_push_it(main_window, gui_ctx):
    setup(gui_ctx, push=("netadmin2",))
    credentials.open_credentials(gui_ctx, main_window)
    tab = main_window._tabs[credentials.CREDENTIALS_TAB_KEY]
    tab.refresh()
    tab.login_list.setCurrentItem(
        tab.login_list.findItems("netadmin2", Qt.MatchFlag.MatchExactly)[0]
    )
    tree = tab.used_by_tree
    groups = [tree.topLevelItem(i).text(0) for i in range(tree.topLevelItemCount())]
    assert groups == [f"{credentials.PUSHED} (1)"]
    assert tree.topLevelItem(0).child(0).text(0) == "acme"


def test_deleting_a_pushed_login_warns_about_the_plays(main_window, gui_ctx, monkeypatch):
    setup(gui_ctx)
    usage = credential_usage(gui_ctx.workspace, "netadmin2")
    assert usage == [(credentials.PUSHED, "acme")]
    assert usage_summary(usage) == "1 customer's plays"
    view = main_window._views["credentials"].findChild(credentials.CredentialsView)
    asked: list[str] = []
    monkeypatch.setattr(
        QMessageBox,
        "question",
        lambda parent, title, text, *a, **k: asked.append(text) or QMessageBox.StandardButton.No,
    )
    view.delete_login("netadmin2")
    assert "Plays for acme push it" in asked[0]
    assert "will not be able to log in" not in asked[0]
