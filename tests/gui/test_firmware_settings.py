"""Settings, General, Firmware folder: where upgrade plays find their image files."""

from __future__ import annotations

import json
import os

import pytest

from tsunami.core.firmware.library import firmware_dir
from tsunami.core.models.settings import load_settings
from tsunami.gui.screens import settings


@pytest.fixture
def notes(main_window, monkeypatch):
    seen: list[tuple[str, str]] = []
    monkeypatch.setattr(main_window, "notify", lambda m, level="info": seen.append((m, level)))
    return seen


@pytest.fixture
def opened(monkeypatch):
    calls: list[object] = []
    monkeypatch.setattr(settings, "open_folder", calls.append)
    return calls


@pytest.fixture
def tab(main_window, gui_ctx):
    return settings.open_settings(gui_ctx, main_window)


def _pick(monkeypatch, answer: str) -> list[str]:
    asked: list[str] = []

    def choose(parent, title, start):
        asked.append(title)
        return answer

    monkeypatch.setattr(settings, "choose_folder", choose)
    return asked


def test_the_field_is_empty_and_shows_the_default_folder(tab, gui_ctx):
    assert tab.firmware_edit.text() == ""
    assert tab.firmware_edit.placeholderText() == str(gui_ctx.paths.firmware_dir)
    assert tab.firmware_edit.toolTip()
    assert tab.firmware_folder() == gui_ctx.paths.firmware_dir


def test_browse_then_save_sets_the_folder_the_engine_uses(tab, gui_ctx, monkeypatch, tmp_path):
    images = tmp_path / "images"
    images.mkdir()
    asked = _pick(monkeypatch, str(images))
    tab._browse_firmware()
    assert asked == ["Choose firmware folder"]
    assert tab.firmware_edit.text() == str(images)
    assert tab.is_dirty("settings") and tab.save_button.isEnabled()
    tab.save_button.click()
    assert not tab.is_dirty()
    assert gui_ctx.settings.firmware_dir == str(images)
    assert load_settings(gui_ctx.paths.settings_file).firmware_dir == str(images)
    assert firmware_dir(gui_ctx.paths, gui_ctx.settings) == images


def test_cancelled_browse_changes_nothing(tab, monkeypatch):
    _pick(monkeypatch, "")
    tab._browse_firmware()
    assert tab.firmware_edit.text() == "" and not tab.is_dirty()


def test_clearing_the_field_goes_back_to_the_default(tab, gui_ctx, tmp_path):
    tab.firmware_edit.setText(str(tmp_path))
    assert tab.save_general()
    tab.firmware_edit.setText("   ")
    assert tab.save_general()
    assert gui_ctx.settings.firmware_dir is None
    assert firmware_dir(gui_ctx.paths, gui_ctx.settings) == gui_ctx.paths.firmware_dir


def test_a_missing_folder_is_saved_with_a_warning(tab, gui_ctx, notes, tmp_path):
    missing = tmp_path / "<b>not here</b>"
    tab.firmware_edit.setText(str(missing))
    assert tab.save_general()
    assert gui_ctx.settings.firmware_dir == str(missing)
    warnings = [m for m, level in notes if level == "warning"]
    assert warnings and str(missing) in warnings[0]  # the path as typed, not as markup
    assert "stop before connecting" in warnings[0]


def test_open_folder_creates_the_default_folder(tab, gui_ctx, opened):
    assert not gui_ctx.paths.firmware_dir.exists()
    assert tab.open_firmware_folder()
    assert gui_ctx.paths.firmware_dir.is_dir()
    assert opened == [gui_ctx.paths.firmware_dir]


def test_open_folder_opens_the_unsaved_choice(tab, opened, tmp_path):
    tab.firmware_edit.setText(str(tmp_path))
    assert tab.open_firmware_folder()
    assert opened == [tmp_path]


def test_open_folder_does_not_create_a_missing_chosen_folder(tab, notes, opened, tmp_path):
    missing = tmp_path / "gone"
    tab.firmware_edit.setText(str(missing))
    assert not tab.open_firmware_folder()
    assert not missing.exists() and opened == []
    assert notes[-1] == (f"Firmware folder not found: {missing}", "warning")


def test_the_open_button_is_wired(tab, opened):
    tab.firmware_open.click()
    assert len(opened) == 1


def test_an_external_edit_reloads_the_field(tab, gui_ctx, tmp_path, qtbot):
    path = gui_ctx.paths.settings_file
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    data["firmware_dir"] = str(tmp_path)
    before = path.stat().st_mtime_ns if path.exists() else 0
    path.write_text(json.dumps(data), encoding="utf-8")
    os.utime(path, ns=(before + 5_000_000_000, before + 5_000_000_000))
    with qtbot.waitSignal(gui_ctx.settings_changed):
        tab.check_external()
    assert tab.firmware_edit.text() == str(tmp_path)
    assert not tab.is_dirty()
