"""Settings tab: one Save bar, Advanced section, and editing the files outside Tsunami."""

from __future__ import annotations

import json
import os

import pytest
from PySide6.QtWidgets import QMessageBox

from tsunami.core.models.settings import load_settings
from tsunami.core.policy import load_policy
from tsunami.gui.screens import settings


@pytest.fixture
def notes(main_window, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(main_window, "notify", lambda m, level="info": seen.append((m, level)))
    return seen


@pytest.fixture
def opened(main_window, monkeypatch):
    calls: list[tuple[object, object]] = []
    monkeypatch.setattr(
        main_window, "open_in_vscode", lambda path, line=None: calls.append((path, line))
    )
    return calls


@pytest.fixture
def tab(main_window, gui_ctx):
    tab = settings.open_settings(gui_ctx, main_window)
    assert isinstance(tab, settings.SettingsTab)
    return tab


def _edit_file(path, text: str) -> None:
    """Write like an editor does: new content, and a modification time that differs."""
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(text, encoding="utf-8")
    os.utime(path, ns=(before + 5_000_000_000, before + 5_000_000_000))


def _edit_settings(gui_ctx, **changes) -> None:
    data = json.loads(gui_ctx.paths.settings_file.read_text(encoding="utf-8"))
    data.update(changes)
    _edit_file(gui_ctx.paths.settings_file, json.dumps(data))


def test_advanced_is_collapsed_by_default(tab):
    assert not tab.advanced_toggle.isChecked()
    assert tab.advanced_body.isHidden()
    assert not tab.advanced_summary.isHidden()
    assert "Most setups need neither" in tab.advanced_summary.text()
    tab.advanced_toggle.setChecked(True)
    assert not tab.advanced_body.isHidden()
    assert tab.advanced_summary.isHidden()
    tab.scroll_to("errors")  # the side bar entry expands it too
    assert not tab.advanced_body.isHidden()


def test_retention_shows_its_unit(tab):
    assert tab.retention_spin.suffix() == " backups per device"
    assert tab.retention_spin.toolTip()


def test_open_buttons_call_vscode_with_the_right_paths(tab, gui_ctx, opened):
    gui_ctx.paths.settings_file.unlink(missing_ok=True)
    assert not gui_ctx.paths.policy_file.exists()
    tab.open_settings_button.click()
    tab.open_policy_button.click()
    assert [c[0] for c in opened] == [gui_ctx.paths.settings_file, gui_ctx.paths.policy_file]
    # Missing files are created with the current values before they open.
    assert load_settings(gui_ctx.paths.settings_file) == gui_ctx.settings
    assert load_policy(gui_ctx.paths.policy_file).enabled is False
    assert gui_ctx.paths.policy_file.is_file()
    tab.check_external()  # creating the files is not an external edit
    assert not tab.is_dirty() and not tab.notice.isVisibleTo(tab)


def test_external_settings_edit_is_reloaded(tab, gui_ctx, qtbot):
    assert gui_ctx.settings.auto_lock_minutes == 15
    _edit_settings(gui_ctx, auto_lock_minutes=42, backup_retention=7)
    with qtbot.waitSignal(gui_ctx.settings_changed):
        tab.check_external()
    assert tab.auto_lock_spin.value() == 42 and tab.retention_spin.value() == 7
    assert gui_ctx.settings.auto_lock_minutes == 42
    assert not tab.is_dirty() and not tab.save_button.isEnabled()


def test_reload_button_rereads_both_files(tab, gui_ctx, notes):
    _edit_settings(gui_ctx, theme="dark")
    _edit_file(gui_ctx.paths.policy_file, "enabled: false\nrate_limit_per_minute: 9\n")
    assert tab.reload_button.isEnabled()
    tab.reload_button.click()
    assert tab.theme_combo.currentText() == "dark" and gui_ctx.settings.theme == "dark"
    assert tab.rate_limit.value() == 9
    assert notes[-1][1] == "info"


def test_invalid_external_settings_keep_the_old_ones(tab, gui_ctx, notes):
    _edit_file(gui_ctx.paths.settings_file, "{ not json")
    tab.check_external()
    assert tab.notice.isVisibleTo(tab)
    assert "settings.json" in tab.notice_label.text()
    assert gui_ctx.settings.auto_lock_minutes == 15
    assert tab.auto_lock_spin.value() == 15

    _edit_settings_invalid = json.dumps({"auto_lock_minutes": 9999})
    _edit_file(gui_ctx.paths.settings_file, _edit_settings_invalid)
    assert not tab.reload_from_disk()
    assert tab.notice_label.property("level") == "error"
    assert gui_ctx.settings.auto_lock_minutes == 15 and tab.auto_lock_spin.value() == 15

    _edit_settings(gui_ctx, auto_lock_minutes=20)
    assert tab.reload_from_disk()
    assert not tab.notice.isVisibleTo(tab)
    assert gui_ctx.settings.auto_lock_minutes == 20


def test_invalid_external_policy_keeps_the_old_one(tab, gui_ctx):
    _edit_file(gui_ctx.paths.policy_file, "enabled: true\nshow_allowlist:\n  ios: [conf t]\n")
    tab.check_external()
    assert "mcp-policy.yml" in tab.notice_label.text()
    assert not tab.mcp_enabled.isChecked()
    assert tab.save_all()  # nothing dirty: no write, the bad file is left alone
    assert "conf t" in gui_ctx.paths.policy_file.read_text(encoding="utf-8")
    assert tab.notice.isVisibleTo(tab)


def test_external_policy_edit_is_picked_up(tab, gui_ctx, main_window):
    _edit_file(
        gui_ctx.paths.policy_file,
        "enabled: true\napply: lab_only\ntrust_new_host_keys: true\nrate_limit_per_minute: 17\n",
    )
    tab.check_external()
    assert tab.mcp_enabled.isChecked() and tab.apply_combo.currentText() == "lab_only"
    assert tab.trust_keys.isChecked() and tab.rate_limit.value() == 17
    assert not tab.is_dirty()
    assert main_window.status_mcp.text() == "MCP: lab only"
    # What the MCP server enforces on its next call is this file, read fresh (McpApp.policy).
    assert load_policy(gui_ctx.paths.policy_file).trust_new_host_keys is True
    # Saving again does not ask to enable MCP, because the file already had it on.
    tab.rate_limit.setValue(18)
    assert tab.save_all()
    assert load_policy(gui_ctx.paths.policy_file).rate_limit_per_minute == 18


def test_save_is_disabled_until_a_change(tab, gui_ctx, notes):
    assert not tab.save_button.isEnabled() and not tab.discard_button.isEnabled()
    assert tab.dirty_label.isHidden()
    tab.theme_combo.setCurrentText("dark")
    assert tab.save_button.isEnabled() and not tab.dirty_label.isHidden()
    tab.theme_combo.setCurrentText("system")  # back to what is saved
    assert not tab.save_button.isEnabled()
    tab.theme_combo.setCurrentText("dark")
    tab.save_button.click()
    assert load_settings(gui_ctx.paths.settings_file).theme == "dark"
    assert not tab.save_button.isEnabled() and tab.dirty_label.isHidden()


def test_tab_title_shows_unsaved_marker(tab, main_window):
    index = main_window.editor.indexOf(tab)
    assert main_window.editor.tabText(index) == "Settings"
    tab.log_combo.setCurrentText("DEBUG")
    assert main_window.editor.tabText(index) == "Settings ●"
    tab.discard_changes()
    assert main_window.editor.tabText(index) == "Settings"
    assert tab.log_combo.currentText() == "INFO"


def test_save_writes_only_the_changed_file(tab, gui_ctx, monkeypatch):
    saved: list[str] = []
    real_policy, real_settings = settings.save_policy, gui_ctx.save_settings
    monkeypatch.setattr(
        settings, "save_policy", lambda p, path: (saved.append("policy"), real_policy(p, path))
    )
    monkeypatch.setattr(
        gui_ctx, "save_settings", lambda s: (saved.append("settings"), real_settings(s))
    )
    tab.rate_limit.setValue(12)
    assert tab.save_all()
    assert saved == ["policy"]
    tab.retention_spin.setValue(3)
    assert tab.save_all()
    assert saved == ["policy", "settings"]
    tab.retention_spin.setValue(4)
    tab.rate_limit.setValue(13)
    assert tab.save_all()
    assert saved == ["policy", "settings", "settings", "policy"]
    assert load_settings(gui_ctx.paths.settings_file).backup_retention == 4
    assert load_policy(gui_ctx.paths.policy_file).rate_limit_per_minute == 13


def test_invalid_form_writes_neither_file(tab, gui_ctx, notes):
    tab.retention_spin.setValue(3)
    tab.allow_lines.edits[settings.PlatformFamily.IOS].setPlainText("configure terminal")
    assert not tab.save_all()
    assert notes[-1][1] == "error"
    assert load_settings(gui_ctx.paths.settings_file).backup_retention == 30
    assert not gui_ctx.paths.policy_file.exists()


def test_invalid_pattern_expands_advanced(tab, notes):
    tab.sink_lines.edits[settings.PlatformFamily.IOS].setPlainText("no secret group (")
    assert not tab.save_all()
    assert "extra_sink_patterns" in notes[-1][0]
    assert not tab.advanced_body.isHidden()


def test_enable_mcp_still_asks(tab, gui_ctx, monkeypatch):
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.StandardButton.No)
    tab.mcp_enabled.setChecked(True)
    assert not tab.save_all()
    assert not gui_ctx.paths.policy_file.exists()
    assert tab.save_button.isEnabled()


def test_external_edit_does_not_discard_unsaved_edits(tab, gui_ctx, notes):
    tab.auto_lock_spin.setValue(33)
    _edit_settings(gui_ctx, auto_lock_minutes=44)
    tab.check_external()
    # My edit is still in the form, the live settings are untouched, and I am asked.
    assert tab.auto_lock_spin.value() == 33
    assert gui_ctx.settings.auto_lock_minutes == 15
    assert tab.notice.isVisibleTo(tab) and tab.notice_keep.isVisibleTo(tab)
    assert tab.notice_label.property("level") == "warning"
    assert tab.is_dirty()
    # Saving is refused until I choose.
    assert not tab.save_all()
    assert json.loads(gui_ctx.paths.settings_file.read_text())["auto_lock_minutes"] == 44

    tab.notice_reload.click()  # take the file's values
    assert tab.auto_lock_spin.value() == 44 and gui_ctx.settings.auto_lock_minutes == 44
    assert not tab.is_dirty() and not tab.notice.isVisibleTo(tab)


def test_keep_my_edits_then_save_overwrites(tab, gui_ctx):
    tab.auto_lock_spin.setValue(33)
    _edit_settings(gui_ctx, auto_lock_minutes=44)
    tab.check_external()
    tab.notice_keep.click()
    assert not tab.notice.isVisibleTo(tab) and tab.auto_lock_spin.value() == 33
    tab.check_external()  # the same disk state is not raised again
    assert not tab.notice.isVisibleTo(tab)
    assert tab.save_all()
    assert load_settings(gui_ctx.paths.settings_file).auto_lock_minutes == 33


def test_save_notices_an_external_edit_it_has_not_seen(tab, gui_ctx):
    tab.auto_lock_spin.setValue(33)
    _edit_settings(gui_ctx, auto_lock_minutes=44)
    assert not tab.save_all()  # no focus event yet: save looks first
    assert tab.notice.isVisibleTo(tab)
    assert load_settings(gui_ctx.paths.settings_file).auto_lock_minutes == 44


def test_own_save_is_not_an_external_change(tab, gui_ctx):
    tab.auto_lock_spin.setValue(33)
    tab.rate_limit.setValue(8)
    assert tab.save_all()
    tab.check_external()
    assert not tab.notice.isVisibleTo(tab) and not tab.is_dirty()


def test_edit_in_one_file_leaves_edits_for_the_other(tab, gui_ctx):
    tab.rate_limit.setValue(8)  # unsaved policy edit
    _edit_settings(gui_ctx, theme="dark")  # external settings edit
    tab.check_external()
    assert tab.theme_combo.currentText() == "dark" and tab.rate_limit.value() == 8
    assert tab.is_dirty("policy") and not tab.is_dirty("settings")
    assert not tab.notice.isVisibleTo(tab)
