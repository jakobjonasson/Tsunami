"""Restore from backup in the GUI: the dialog, the confirmation, and the restore tab
running against the mock transport. Nothing reaches the device before the person
confirms, and no secret reaches a widget."""

from __future__ import annotations

import pytest
from tests.gui.conftest import LOGIN_PASSWORD, TACACS_SECRET, all_widget_text

from tsunami.core.backups import PRE_RESTORE_TAG, RESTORE_TAG, list_backups
from tsunami.core.models.jobs import DeviceStatus
from tsunami.core.models.play import ConnectionMethod
from tsunami.core.operations import RESTORE_MODE
from tsunami.core.vault import LockedCipher
from tsunami.gui.screens import backups as backups_screen
from tsunami.gui.screens import restore as restore_screen
from tsunami.gui.screens.restore import RestoreDialog, RestoreTab

ENABLE = "Enable-Secret-4321"
TYPED_PASSWORD = "Typed-Pass-5150"
BACKUP = f"""Building configuration...
!
hostname sa-as01
enable secret 9 {ENABLE}
interface GigabitEthernet1/0/1
 description <b>uplink</b> & <a href="x">core</a>
vlan 30
 name Vlan30
tacacs server T1
 key 7 {TACACS_SECRET}
end
"""


@pytest.fixture
def backup(gui_ctx):
    folder = gui_ctx.paths.backups_dir / "acme" / "sa-as01"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / "20261101T010101Z_drift.cfg"
    path.write_text(BACKUP, encoding="utf-8", newline="\n")
    return path


@pytest.fixture
def device(mock_transport):
    return mock_transport.device("sa-as01")


@pytest.fixture
def backups_tab(main_window, backup):
    backups_screen.register(main_window)
    main_window.run_command_with("backups.open", "acme", "sa-as01")
    tab = main_window._tabs["backups:acme/sa-as01"]
    tab.table.selectRow(0)
    return tab


@pytest.fixture
def flow(monkeypatch):
    """Fill the dialog with `fill` and answer the confirmation with `confirmed`."""

    class Flow:
        fill = None
        confirmed = True
        dialogs: list[RestoreDialog] = []
        prompts: list[str] = []

    def run_dialog(dialog):
        Flow.dialogs.append(dialog)
        if Flow.fill is not None:
            Flow.fill(dialog)
        dialog.accept()
        return dialog.request is not None

    def confirm(parent, title, text):
        Flow.prompts.append(text)
        return Flow.confirmed

    monkeypatch.setattr(restore_screen, "run_dialog", run_dialog)
    monkeypatch.setattr(restore_screen, "confirm", confirm)
    Flow.dialogs, Flow.prompts = [], []
    return Flow


def _finish(qtbot, tab: RestoreTab) -> None:
    qtbot.waitUntil(lambda: not tab.running and tab.result is not None, timeout=20000)


def test_restore_over_ssh_with_the_devices_login(
    main_window, gui_ctx, backups_tab, device, flow, qtbot
):
    before = list(device.running)
    tab = backups_tab.restore()

    (dialog,) = flow.dialogs
    assert dialog.method() == ConnectionMethod.SSH
    assert dialog.host_edit.text()  # the inventory address
    assert dialog.login_choice() == ("saved", "acme-netadmin")  # the device's own, first
    assert "acme-netadmin  (this device's login)" == dialog.login_combo.itemText(0)
    (prompt,) = flow.prompts
    assert "Send 8 lines" in prompt and "sa-as01 over SSH" in prompt
    assert "lines on the device that the backup does not have are kept" in prompt

    assert isinstance(tab, RestoreTab)
    _finish(qtbot, tab)
    assert tab.result.status == DeviceStatus.SUCCESS, tab.result.report()
    assert "vlan 30" in device.running and device.startup == device.running
    assert before[0] in device.running  # a merge: nothing removed
    tags = [e.job_id for e in list_backups(gui_ctx.paths, "acme", "sa-as01")]
    assert RESTORE_TAG in tags and PRE_RESTORE_TAG in tags
    assert tab.progress.value() == tab.result.total
    assert tab.job_button.isEnabled()

    job = gui_ctx.history.get_job(tab.result.job_id)
    assert job.mode == RESTORE_MODE
    shown = all_widget_text(main_window)
    for secret in (LOGIN_PASSWORD, TACACS_SECRET, ENABLE):
        assert secret not in shown
        assert secret not in (gui_ctx.history.devices(job.id)[0].output_redacted or "")
    # Device text is shown as text: the description's HTML is in the log literally.
    assert '<b>uplink</b> & <a href="x">core</a>' not in tab.status_label.text()


def test_typed_login_and_rejected_lines(main_window, gui_ctx, backups_tab, device, flow, qtbot):
    device.reject_lines = [r"^description "]

    def fill(dialog):
        dialog.choose_login("typed")
        dialog.username_edit.setText("netadmin")
        dialog.password_edit.setText(TYPED_PASSWORD)

    flow.fill = fill
    tab = backups_tab.restore()
    _finish(qtbot, tab)

    assert tab.result.status == DeviceStatus.FAILED
    assert "1 rejected" in tab.result.summary
    assert tab.result.rejected[0][0].strip().startswith("description <b>uplink</b>")
    log = tab.log_view.toPlainText()
    assert "Rejected by the device:" in log
    assert '<b>uplink</b> & <a href="x">core</a>' in log  # plain text, not rendered
    assert "vlan 30" in device.running
    job = gui_ctx.history.get_job(tab.result.job_id)
    assert "typed for this restore (user netadmin)" == job.summary["login"]
    assert TYPED_PASSWORD not in all_widget_text(main_window)


def test_nothing_is_sent_when_the_confirmation_is_declined(main_window, backups_tab, device, flow):
    flow.confirmed = False
    assert backups_tab.restore() is None
    assert device.sent == []
    assert not any(k.startswith("restore:") for k in main_window._tabs)


def test_the_dialog_says_what_is_missing(main_window, gui_ctx, backup, monkeypatch):
    monkeypatch.setattr(restore_screen.PortPicker, "port", lambda self: None)
    entry = list_backups(gui_ctx.paths, "acme", "sa-as01")[0]
    dialog = RestoreDialog(gui_ctx, "acme", "sa-as01", entry)

    dialog.choose_login("typed")
    assert dialog.build_request() == "Type the username."
    dialog.choose_login("none")
    assert "SSH needs a login" in dialog.build_request()
    dialog.host_edit.setText("")
    dialog.login_combo.setCurrentIndex(0)
    assert dialog.build_request() == "Type the device's address."

    dialog.console_radio.setChecked(True)
    assert dialog.where_stack.currentIndex() == 1
    assert "declines the initial configuration dialog" in dialog.hint_label.text()
    assert "Choose a serial port" in dialog.build_request()
    dialog.accept()
    assert dialog.request is None and "Choose a serial port" in dialog.error_label.text()
    dialog.deleteLater()


def test_console_with_no_login_for_a_factory_new_cisco_switch(gui_ctx, backup, monkeypatch):
    monkeypatch.setattr(restore_screen.PortPicker, "port", lambda self: "COM7")
    entry = list_backups(gui_ctx.paths, "acme", "sa-as01")[0]
    dialog = RestoreDialog(gui_ctx, "acme", "sa-as01", entry)
    dialog.console_radio.setChecked(True)
    dialog.choose_login("none")
    request = dialog.build_request()
    assert request.method == ConnectionMethod.SERIAL and request.credential is None
    assert request.serial_port == "COM7" and request.baud == 9600
    assert request.where == "console COM7 at 9600 baud"
    assert request.ssh_keys and request.save
    dialog.deleteLater()


def test_the_dialog_warns_when_the_address_is_not_in_the_backup(gui_ctx, backup):
    entry = list_backups(gui_ctx.paths, "acme", "sa-as01")[0]
    dialog = RestoreDialog(gui_ctx, "acme", "sa-as01", entry)
    dialog.host_edit.setText("192.0.2.200")
    assert "does not give 192.0.2.200 to any interface" in dialog.warning_label.text()
    dialog.console_radio.setChecked(True)
    assert dialog.warning_label.text() == ""
    dialog.deleteLater()


def test_a_locked_tsunami_cannot_restore(main_window, gui_ctx, backups_tab, flow, monkeypatch):
    notes = []
    monkeypatch.setattr(main_window, "notify", lambda message, level="info": notes.append(message))
    gui_ctx.secrets.cipher = LockedCipher()
    assert backups_tab.restore() is None
    assert flow.dialogs == [] and notes == ["Unlock Tsunami to restore a backup"]


def test_the_full_backup_view_restores_its_backup(main_window, backups_tab, device, flow, qtbot):
    view = backups_tab.view_full()
    tab = view.restore()
    _finish(qtbot, tab)
    assert tab.result.ok
    assert flow.dialogs[0].entry.path == view.entry.path  # the backup the view shows


def test_restore_button_needs_one_selected_backup(backups_tab):
    assert backups_tab.restore_button.isEnabled()
    backups_tab.table.clearSelection()
    assert not backups_tab.restore_button.isEnabled()
    assert backups_tab.restore() is None
