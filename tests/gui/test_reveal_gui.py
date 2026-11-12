"""0.6.1 in the GUI: press-to-reveal, Ctrl+F in backups, operations and reveals in History,
and canary priority in the device editor."""

from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtWidgets import QApplication
from tests.gui.conftest import TACACS_SECRET, all_widget_text

from tsunami.core import vault as vault_module
from tsunami.core.backups import write_backup
from tsunami.core.models.jobs import Actor, DeviceStatus
from tsunami.core.operations import SHOW_MODE, OperationLog
from tsunami.core.vault import Vault
from tsunami.gui import lock as lock_module
from tsunami.gui.screens import backups as backups_screen
from tsunami.gui.screens.history import HistoryTab, HistoryView
from tsunami.gui.screens.run_wizard import open_run_wizard
from tsunami.gui.widgets.reveal_text import RevealTextView

RAW = f"hostname sa-as01\ntacacs server t1\n key 7 {TACACS_SECRET}\nntp server 192.0.2.9\n"


class FakeLock:
    unlocked = True

    def __init__(self, answer: bool = True) -> None:
        self.answer, self.asked = answer, []

    def confirm_password(self, what, parent=None) -> bool:
        self.asked.append(what)
        return self.answer


@pytest.fixture
def backup_tab(main_window, gui_ctx):
    write_backup(gui_ctx.paths, "acme", "sa-as01", RAW, "drift", keep=30)
    backups_screen.register(main_window)
    main_window.run_command_with("backups.open", "acme", "sa-as01")
    tab = main_window._tabs["backups:acme/sa-as01"]
    tab.table.selectRow(0)
    return tab


def release(view: RevealTextView) -> None:
    from PySide6.QtTest import QTest

    QTest.mouseRelease(
        view.viewport(), Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier, QPoint(5, 5)
    )


def test_a_backup_token_shows_only_while_held_and_is_audited(backup_tab, gui_ctx):
    lock = FakeLock()
    gui_ctx.lock = lock
    view = backup_tab.content_view
    assert TACACS_SECRET not in view.toPlainText()
    [span] = view.spans
    # Offscreen Qt has no fonts, so text positions collapse; press() is what a press on
    # the token calls, and the release is a real mouse event.
    assert not view.press(span)  # first press: the password is asked for, nothing shown
    assert lock.asked == ["line 3, token 1"] and not view.revealing
    assert view.press(span)  # within the confirmation window: shown while held
    assert view.revealing and view.overlay.text() == TACACS_SECRET
    assert TACACS_SECRET not in view.toPlainText()  # never in the document: cannot be copied
    view.selectAll()
    view.copy()
    assert TACACS_SECRET not in QApplication.clipboard().text()
    assert view.overlay.textInteractionFlags() == Qt.TextInteractionFlag.NoTextInteraction
    release(view)
    assert not view.revealing and view.overlay.text() == ""
    [row] = gui_ctx.history.list_reveals()
    assert row["kind"] == "backup" and row["device"] == "sa-as01"
    assert row["item"] == "line 3, token 1" and row["masked_line"] == " key 7 <redacted>"
    assert TACACS_SECRET not in str(row)


def test_the_password_is_asked_again_after_the_window(backup_tab, gui_ctx):
    lock = FakeLock()
    gui_ctx.lock = lock
    view = backup_tab.content_view
    now = [1000.0]
    view.clock = lambda: now[0]
    [span] = view.spans
    view.press(span)
    assert view.press(span) and view.revealing
    view.hide_value()
    now[0] += 3600
    assert not view.press(span) and len(lock.asked) == 2


def test_a_refused_password_reveals_nothing(backup_tab, gui_ctx):
    gui_ctx.lock = FakeLock(answer=False)
    view = backup_tab.content_view
    [span] = view.spans
    assert not view.press(span) and not view.press(span)
    assert gui_ctx.history.list_reveals() == []


def test_the_confirm_dialog_checks_the_real_password(gui_ctx, monkeypatch, qapp):
    monkeypatch.setattr(vault_module, "_SCRYPT_N", 2**10)
    vault = Vault(gui_ctx.paths.vault_file)
    vault.create("correct-horse-1")
    manager = lock_module.LockManager(gui_ctx.paths, gui_ctx.secrets, gui_ctx.settings)
    manager._unlocked = True
    answers = iter(["wrong-password-1", "correct-horse-1"])

    def run(dialog):
        dialog.password_edit.setText(next(answers))
        return dialog.save()

    monkeypatch.setattr(lock_module, "run_dialog", run)
    assert not manager.confirm_password("secret()")
    assert manager.confirm_password("secret()")
    manager._unlocked = False
    assert not manager.confirm_password("secret()")  # locked: never


def test_the_run_preview_reveals_from_the_store(main_window, gui_ctx, workspace_root):
    (workspace_root / "plays/push/snmp.yml").write_text(
        "name: snmp\ntype: push\ncustomer: acme\ntargets:\n  devices: [sa-as01]\n"
        "steps:\n  - config:\n      - \"tacacs-server key 7 {{ secret('tacacs') }}\"\n",
        encoding="utf-8",
    )
    gui_ctx.reload_workspace(block=True)
    gui_ctx.lock = FakeLock()
    wizard = open_run_wizard(gui_ctx, main_window, "plays/push/snmp.yml")
    assert wizard.preview()
    view = wizard.preview_tabs.widget(0)
    assert isinstance(view, RevealTextView)
    [span] = view.spans
    view.press(span)
    assert view.press(span) and view.overlay.text() == TACACS_SECRET
    assert TACACS_SECRET not in all_widget_text(main_window).replace(view.overlay.text(), "")
    view.hide_value()
    [row] = gui_ctx.history.list_reveals()
    assert row["kind"] == "preview" and row["context"] == "preview of plays/push/snmp.yml"
    assert row["masked_line"] == "tacacs-server key 7 ********"


def test_ctrl_f_finds_text_in_a_backup(backup_tab, qtbot):
    bar = backup_tab.find_bar
    bar.open_bar()
    assert not bar.isHidden()
    bar.edit.setText("server")
    assert bar.count_label.text() == "1 of 2"
    assert bar.find_next() and bar.count_label.text() == "2 of 2"
    assert bar.find_next() and bar.count_label.text() == "1 of 2"  # wraps
    bar.edit.setText(TACACS_SECRET)  # the secret is not in the shown text
    assert bar.count_label.text() == "No results"
    bar.close_bar()
    assert bar.isHidden()


def test_operations_show_in_history_only_when_asked(main_window, gui_ctx):
    record = OperationLog(
        gui_ctx.history, mode=SHOW_MODE, customer="acme", actor=Actor.MCP, devices=["sa-as01"]
    )
    record.device("sa-as01", DeviceStatus.SUCCESS, output="show version\nok")
    record.finish()
    tab = HistoryTab(gui_ctx, main_window)
    assert tab.jobs_table.rowCount() == 0
    tab.operations_box.setChecked(True)
    assert tab.jobs_table.rowCount() == 1 and tab.jobs_table.item(0, 2).text() == "Show command"
    side = HistoryView(gui_ctx, main_window)
    assert side.recent_list.count() == 0  # the side bar lists plays


def test_the_reveals_tab_lists_reveals(main_window, gui_ctx):
    gui_ctx.history.add_reveal(
        revealed_by="tech1",
        kind="backup",
        customer="acme",
        device="sa-as01",
        item="line 3, token 1",
        context="20261112T000000Z_drift.cfg",
        masked_line="<b>key</b> <redacted>",
    )
    tab = HistoryTab(gui_ctx, main_window)
    assert tab.reveals_table.rowCount() == 1
    assert tab.reveals_table.item(0, 1).text() == "tech1"
    assert tab.reveals_table.item(0, 7).text() == "<b>key</b> <redacted>"  # plain text cell


def test_canary_priority_in_the_device_editor(main_window, gui_ctx, monkeypatch):
    from tsunami.gui.screens import inventory_dialogs

    device = gui_ctx.workspace.customers["acme"].device("sa-as01")
    dialog = inventory_dialogs.DeviceDialog(gui_ctx, main_window, "acme", device=device)
    assert dialog.canary_spin.value() == 100
    dialog.canary_spin.setValue(0)
    assert dialog.save()
    gui_ctx.reload_workspace(block=True)
    assert gui_ctx.workspace.customers["acme"].device("sa-as01").canary_priority == 0
