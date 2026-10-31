"""Credentials and Settings views (work package 4.2C2)."""

from __future__ import annotations

import json

import pytest
from PySide6.QtWidgets import QMessageBox
from tests.gui.conftest import LOGIN_PASSWORD, TACACS_SECRET, all_widget_text

from tsunami.core.issues import TsunamiError
from tsunami.core.models.settings import load_settings
from tsunami.core.policy import load_policy
from tsunami.gui.screens import credentials, settings

NEW_PASSWORD = "Brand-New-Pass-9981"
NEW_SECRET = "Brand-New-Secret-5512"


@pytest.fixture
def notes(main_window, monkeypatch):
    seen: list[tuple[str, str]] = []
    original = main_window.notify

    def record(message, level="info"):
        seen.append((message, level))
        main_window.statusBar().showMessage(message)

    monkeypatch.setattr(main_window, "notify", record)
    yield seen
    monkeypatch.setattr(main_window, "notify", original)


@pytest.fixture
def cred_view(main_window):
    return main_window._views["credentials"].findChild(credentials.CredentialsView)


def _tab(main_window):
    return settings.open_settings(main_window.ctx, main_window)


def test_lists_names_only(cred_view, main_window):
    assert [cred_view.login_list.item(i).text() for i in range(cred_view.login_list.count())] == [
        "acme-netadmin"
    ]
    assert cred_view.customer_combo.findText("acme") >= 0
    text = all_widget_text(main_window)
    assert LOGIN_PASSWORD not in text and TACACS_SECRET not in text
    assert "tacacs" in text


def test_create_and_overwrite_login_and_secret(cred_view, main_window, gui_ctx, notes):
    assert cred_view.save_login("acme-new", "admin", NEW_PASSWORD, "en-pass")
    assert "acme-new" in gui_ctx.secrets.list_logins()
    assert gui_ctx.secrets.get_login("acme-new").password == NEW_PASSWORD

    cred_view.customer_combo.setCurrentText("acme")
    assert cred_view.save_secret("acme", "snmp", NEW_SECRET)
    assert gui_ctx.secrets.get_secret("acme", "snmp") == NEW_SECRET

    assert cred_view.save_login("acme-new", "admin2", "Another-Pass-1", None)
    assert gui_ctx.secrets.get_login("acme-new").username == "admin2"
    assert cred_view.save_secret("acme", "snmp", "Another-Secret-2")

    text = all_widget_text(main_window)
    for value in (
        LOGIN_PASSWORD,
        TACACS_SECRET,
        NEW_PASSWORD,
        NEW_SECRET,
        "Another-Pass-1",
        "Another-Secret-2",
        "en-pass",
    ):
        assert value not in text
    assert all(value not in msg for msg, _ in notes for value in (NEW_PASSWORD, NEW_SECRET))
    assert "snmp" in text and "acme-new" in text


def test_dialog_path_clears_fields_and_never_prefills(cred_view, gui_ctx, monkeypatch):
    seen = {}

    def fake_exec(self):
        seen["prefill"] = (
            self.username_edit.text(),
            self.value_edit.text(),
            self.enable_edit.text(),
        )
        seen["mode"] = self.value_edit.echoMode()
        seen["dialog"] = self
        if self.name_edit.isReadOnly():
            self.username_edit.setText("netadmin")
        else:
            self.name_edit.setText("acme-dlg")
            self.username_edit.setText("dlguser")
        self.value_edit.setText(NEW_PASSWORD)
        return credentials.QDialog.DialogCode.Accepted

    monkeypatch.setattr(credentials._ValueDialog, "exec", fake_exec)
    cred_view.new_login()
    assert gui_ctx.secrets.get_login("acme-dlg").password == NEW_PASSWORD
    assert seen["prefill"] == ("", "", "")
    assert seen["mode"] == credentials.QLineEdit.EchoMode.Password
    assert seen["dialog"].value_edit.text() == ""

    cred_view.login_list.setCurrentRow(0)
    cred_view.overwrite_login()
    assert seen["prefill"] == ("", "", "")


def test_delete_removes_entry(cred_view, gui_ctx, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    cred_view.login_list.setCurrentRow(0)
    cred_view.delete_login()
    assert gui_ctx.secrets.list_logins() == []
    assert cred_view.login_list.count() == 0

    cred_view.customer_combo.setCurrentText("acme")
    cred_view.secret_list.setCurrentRow(0)
    cred_view.delete_secret()
    assert gui_ctx.secrets.list_secrets("acme") == []


def test_bad_name_and_oversize_show_error(cred_view, gui_ctx, notes):
    assert not cred_view.save_login("Bad Name", "u", "p", None)
    assert not cred_view.save_secret("acme", "x", "y" * 2000)
    errors = [m for m, level in notes if level == "error"]
    assert len(errors) == 2
    assert "slug" in errors[0] and "2560" in errors[1]
    assert "Bad Name" not in gui_ctx.secrets.list_logins()
    assert "y" * 2000 not in all_widget_text(cred_view)


def test_login_test_reports_success_and_error(cred_view, qtbot, monkeypatch):
    opened = []

    class Session:
        def close(self):
            opened.append("closed")

    class FakeTransport:
        outcome: Exception | None = None

        def __init__(self, paths, settings):
            pass

        def open(self, target):
            opened.append((target.customer, target.device, target.credential.username))
            if FakeTransport.outcome:
                raise FakeTransport.outcome
            return Session()

        def close(self):
            pass

    monkeypatch.setattr(credentials, "DeviceTransport", FakeTransport)
    assert cred_view.devices_using("acme-netadmin")
    customer, device = cred_view.devices_using("acme-netadmin")[0]
    assert cred_view.start_test("acme-netadmin", customer, device)
    qtbot.waitUntil(lambda: cred_view.last_test_result is not None, timeout=5000)
    name, ok, message = cred_view.last_test_result
    assert ok and opened[-1] == "closed"
    assert opened[0] == (customer, device, "netadmin")

    from tsunami.core.transports.base import AuthenticationError

    FakeTransport.outcome = AuthenticationError("login refused")
    cred_view.last_test_result = None
    cred_view.start_test("acme-netadmin", customer, device)
    qtbot.waitUntil(lambda: cred_view.last_test_result is not None, timeout=5000)
    assert not cred_view.last_test_result[1]
    assert "AuthenticationError: login refused" in cred_view.test_label.text()
    assert LOGIN_PASSWORD not in cred_view.test_label.text()


# -- Settings ---------------------------------------------------------------------------


def test_settings_command_and_sidebar(main_window):
    assert "settings.open" in main_window._commands
    main_window.run_command("settings.open")
    assert "settings" in main_window._tabs
    sidebar = main_window._views["settings"].findChild(settings.SettingsSidebar)
    assert sidebar.count() == len(settings.SECTIONS)
    sidebar.itemClicked.emit(sidebar.item(2))  # MCP section; must not raise


def test_theme_change_saves_settings_json(main_window, gui_ctx, qtbot):
    tab = _tab(main_window)
    tab.theme_combo.setCurrentText("dark")
    tab.retention_spin.setValue(12)
    with qtbot.waitSignal(gui_ctx.settings_changed):
        assert tab.save_general()
    saved = load_settings(gui_ctx.paths.settings_file)
    assert saved.theme == "dark" and saved.backup_retention == 12


def test_invalid_sink_pattern_refused(main_window, gui_ctx, notes):
    tab = _tab(main_window)
    tab.sink_lines.edits[settings.PlatformFamily.IOS].setPlainText("no secret group (")
    assert not tab.save_errors()
    assert notes and notes[-1][1] == "error"
    assert "extra_sink_patterns" in notes[-1][0]
    assert not load_settings(gui_ctx.paths.settings_file).extra_sink_patterns

    tab.sink_lines.edits[settings.PlatformFamily.IOS].setPlainText(r"tok (?P<secret>\S+)")
    tab.benign_lines.edits[settings.PlatformFamily.IOS].setPlainText("already exists")
    assert tab.save_errors()
    saved = load_settings(gui_ctx.paths.settings_file)
    assert saved.extra_sink_patterns[settings.PlatformFamily.IOS] == [r"tok (?P<secret>\S+)"]
    assert saved.benign_messages[settings.PlatformFamily.IOS] == ["already exists"]


def test_mcp_editor_saves_policy(main_window, gui_ctx, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.Yes)
    tab = _tab(main_window)
    assert "customer configuration" in all_widget_text(tab)
    tab.mcp_enabled.setChecked(True)
    tab.apply_combo.setCurrentText("never")
    tab.rate_limit.setValue(11)
    blocked = next(iter(tab.blocked_boxes))
    tab.blocked_boxes[blocked].setChecked(True)
    tab.allow_lines.edits[settings.PlatformFamily.IOS].setPlainText("show version\nshow clock")
    assert tab.save_mcp()
    policy = load_policy(gui_ctx.paths.policy_file)
    assert policy.enabled and policy.apply == "never" and policy.rate_limit_per_minute == 11
    assert policy.customers_blocked == [blocked]
    assert policy.show_allowlist[settings.PlatformFamily.IOS] == ["show version", "show clock"]


def test_mcp_editor_refuses_bad_allowlist(main_window, gui_ctx, notes):
    tab = _tab(main_window)
    tab.allow_lines.edits[settings.PlatformFamily.IOS].setPlainText("configure terminal")
    assert not tab.save_mcp()
    assert notes[-1][1] == "error"
    assert not gui_ctx.paths.policy_file.exists()


def test_enable_declined_does_not_save(main_window, gui_ctx, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    tab = _tab(main_window)
    tab.mcp_enabled.setChecked(True)
    assert not tab.save_mcp()
    assert not gui_ctx.paths.policy_file.exists()


def test_register_claude_desktop(main_window, tmp_path, monkeypatch, notes):
    config = tmp_path / "Claude" / "claude_desktop_config.json"
    config.parent.mkdir()
    config.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}), encoding="utf-8")
    exe = tmp_path / "tsunami-mcp.exe"
    exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(settings.registration, "claude_desktop_config_paths", lambda: [config])
    monkeypatch.setattr(settings, "find_mcp_exe", lambda: exe)
    tab = _tab(main_window)
    tab.refresh_claude()
    assert tab.register_button.isEnabled()
    assert "claude mcp add" in tab.code_command.text()
    assert tab.register_claude()
    data = json.loads(config.read_text(encoding="utf-8"))
    assert data["mcpServers"]["tsunami"]["command"] == str(exe)
    assert "other" in data["mcpServers"]
    assert "Quit Claude Desktop" in tab.claude_notice.text()
    assert config.with_name(config.name + ".bak").exists()
    tab.copy_command()
    from PySide6.QtGui import QGuiApplication

    assert QGuiApplication.clipboard().text() == tab.code_command.text()


def test_register_in_development_shows_notice(main_window, monkeypatch):
    monkeypatch.setattr(settings, "find_mcp_exe", lambda: None)
    tab = _tab(main_window)
    tab.refresh_claude()
    assert not tab.register_button.isEnabled()
    assert "not found" in tab.claude_notice.text()


def test_login_test_goes_through_the_customers_jump_host(
    cred_view, gui_ctx, qtbot, monkeypatch, notes
):
    from tsunami.core.inventory_edit import InventoryEditor

    InventoryEditor(gui_ctx.workspace_path).update_customer(
        "acme", jump_host={"host": "198.51.100.10", "port": 2200, "credential": "acme-netadmin"}
    )
    gui_ctx.reload_workspace(block=True)
    targets = []

    class Session:
        def close(self):
            pass

    class FakeTransport:
        def __init__(self, paths, settings):
            pass

        def open(self, target):
            targets.append(target)
            return Session()

        def close(self):
            pass

    monkeypatch.setattr(credentials, "DeviceTransport", FakeTransport)
    customer, device = cred_view.devices_using("acme-netadmin")[0]
    assert cred_view.start_test("acme-netadmin", customer, device)
    qtbot.waitUntil(lambda: cred_view.last_test_result is not None, timeout=5000)
    [target] = targets
    assert (target.jump.host, target.jump.port) == ("198.51.100.10", 2200)
    assert target.jump.credential.username == "netadmin"

    def locked(customer):
        raise TsunamiError("Tsunami is locked")

    monkeypatch.setattr(gui_ctx, "jump_target", locked)
    assert not cred_view.start_test("acme-netadmin", customer, device)
    assert len(targets) == 1  # never tried direct
    assert any("jump host" in m and level == "error" for m, level in notes)
