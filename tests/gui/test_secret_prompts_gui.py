"""Secrets with fields in Credentials, and the secret and password prompt widgets (0.6.0)."""

from __future__ import annotations

import pytest
from PySide6.QtWidgets import QLabel, QLineEdit
from tests.gui.conftest import all_widget_text

from tsunami.core.models.play import Prompt, PromptType
from tsunami.core.prompts import PromptContext
from tsunami.core.secret_store import SecretFields
from tsunami.gui.screens import credentials
from tsunami.gui.widgets.prompt_widgets import PASSWORD_HINT, make_prompt_widget

AUTH = "Gui-Auth-Key-61"
PRIV = "Gui-Priv-Key-62"
TYPED = "Gui-Typed-Pw-63"


@pytest.fixture
def cred_tab(main_window):
    credentials.open_credentials(main_window.ctx, main_window)
    tab = main_window.findChild(credentials.CredentialsTab)
    tab.customer_combo.setCurrentText("acme")
    return tab


def fill_snmpv3(dialog: credentials._SecretDialog, name: str = "snmpv3") -> None:
    dialog.name_edit.setText(name)
    dialog.kind_combo.setCurrentText(credentials.SNMPV3)
    values = {
        "username": "monitor",
        "auth_protocol": "sha",
        "auth_key": AUTH,
        "priv_protocol": "aes 128",
        "priv_key": PRIV,
    }
    for row in dialog.rows:
        row.value_edit.setText(values[row.name_edit.text()])


def test_new_snmpv3_secret_saves_fields_and_shows_no_secret_value(
    cred_tab, main_window, gui_ctx, monkeypatch
):
    seen = {}

    def run(dialog):
        fill_snmpv3(dialog)
        seen["usage"] = dialog.usage.text()
        seen["echo"] = {r.name_edit.text(): r.value_edit.echoMode() for r in dialog.rows}
        return True

    monkeypatch.setattr(credentials, "run_dialog", run)
    cred_tab.new_secret()
    saved = gui_ctx.secrets.get_secret_fields("acme", "snmpv3")
    assert saved.values["auth_key"] == AUTH and saved.values["username"] == "monitor"
    assert saved.plain == {"username", "auth_protocol", "priv_protocol"}
    assert seen["echo"]["auth_key"] == QLineEdit.EchoMode.Password
    assert seen["echo"]["username"] == QLineEdit.EchoMode.Normal
    assert "secret('snmpv3', 'username')" in seen["usage"]
    text = all_widget_text(main_window)
    assert AUTH not in text and PRIV not in text


def test_overwrite_lists_field_names_but_never_values(cred_tab, gui_ctx, monkeypatch):
    gui_ctx.secrets.set_secret_fields(
        "acme",
        "ntp",
        SecretFields({"key_id": "7", "key": "Ntp-Key-64"}, frozenset({"key_id"})),
    )
    seen = {}

    def run(dialog):
        seen["names"] = [r.name_edit.text() for r in dialog.rows]
        seen["values"] = [r.value_edit.text() for r in dialog.rows]
        seen["plain"] = [r.plain_box.isChecked() for r in dialog.rows]
        return False

    monkeypatch.setattr(credentials, "run_dialog", run)
    cred_tab.overwrite_secret("ntp")
    assert seen == {"names": ["key_id", "key"], "values": ["", ""], "plain": [True, False]}


def test_a_hostile_secret_name_is_shown_as_text(cred_tab, main_window, monkeypatch, gui_ctx):
    notes: list[str] = []
    monkeypatch.setattr(main_window, "notify", lambda message, level="info": notes.append(message))
    hostile = "<b>x</b><a href='http://example.invalid'>y</a>"
    seen = {}

    def run(dialog):
        dialog.name_edit.setText(hostile)
        dialog.value_edit.setText("Some-Value-65")
        seen["usage"] = dialog.usage
        return True

    monkeypatch.setattr(credentials, "run_dialog", run)
    cred_tab.new_secret()  # refused: not a slug
    assert seen["usage"].textFormat().name == "PlainText"
    assert hostile not in gui_ctx.secrets.list_secrets("acme")
    assert notes  # the refusal is reported


def test_password_prompt_is_masked_and_never_filled(qtbot):
    field = make_prompt_widget("key", Prompt(type=PromptType.PASSWORD), PromptContext())
    qtbot.addWidget(field)
    assert field.editor.echoMode() == QLineEdit.EchoMode.Password
    field.set_value("********")  # what history keeps: never written back
    assert field.raw_value() == ""
    field.editor.setText(TYPED)
    assert field.raw_value() == TYPED
    hints = [w.text() for w in field.findChildren(QLabel)]
    assert PASSWORD_HINT in hints


def test_secret_prompt_offers_saved_names_only(qtbot):
    context = PromptContext(secrets=frozenset({"snmpv3", "tacacs"}))
    field = make_prompt_widget("snmp", Prompt(type=PromptType.SECRET), context)
    qtbot.addWidget(field)
    items = [field.editor.itemText(i) for i in range(field.editor.count())]
    assert items == ["", "snmpv3", "tacacs"]
    field.set_value("tacacs")
    assert field.raw_value() == "tacacs"
