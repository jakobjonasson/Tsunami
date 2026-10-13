"""Editor tab context menu, the Run play button's disabled look, and the device table fit."""

from __future__ import annotations

from PySide6.QtWidgets import QLabel, QPushButton

from tsunami.gui.screens.device_list import RUN_DISABLED_TIP, DeviceListView


def _open_tabs(window, n: int = 5) -> list[str]:
    keys = [f"t{i}" for i in range(n)]
    for key in keys:
        window.open_tab(key, key.upper(), lambda k=key: QLabel(k))
    return keys


def _remaining(window) -> list[str]:
    return [window.editor.widget(i).property("tab_key") for i in range(window.editor.count())]


def _trigger(menu, text: str) -> None:
    (action,) = [a for a in menu.actions() if a.text() == text]
    assert action.isEnabled()
    action.trigger()


def test_tab_menu_close_closes_that_tab(main_window):
    _open_tabs(main_window)
    _trigger(main_window.menu_for_tab(2), "Close")
    assert _remaining(main_window) == ["t0", "t1", "t3", "t4"]
    assert "t2" not in main_window._tabs


def test_tab_menu_close_others(main_window):
    _open_tabs(main_window)
    _trigger(main_window.menu_for_tab(2), "Close others")
    assert _remaining(main_window) == ["t2"]
    assert list(main_window._tabs) == ["t2"]


def test_tab_menu_close_to_the_right(main_window):
    _open_tabs(main_window)
    _trigger(main_window.menu_for_tab(1), "Close to the right")
    assert _remaining(main_window) == ["t0", "t1"]
    assert sorted(main_window._tabs) == ["t0", "t1"]


def test_tab_menu_close_all_and_command(main_window):
    _open_tabs(main_window)
    _trigger(main_window.menu_for_tab(3), "Close all")
    assert _remaining(main_window) == []
    assert main_window._tabs == {}
    _open_tabs(main_window, 3)
    main_window.run_command("tab.close_all")
    assert main_window.editor.count() == 0


def test_tab_menu_disables_pointless_actions(main_window):
    _open_tabs(main_window, 2)
    last = {a.text(): a.isEnabled() for a in main_window.menu_for_tab(1).actions() if a.text()}
    assert last["Close to the right"] is False
    assert last["Close others"] is True
    main_window.close_tab("t0")
    only = {a.text(): a.isEnabled() for a in main_window.menu_for_tab(0).actions() if a.text()}
    assert only["Close others"] is False


def _device_view(main_window) -> DeviceListView:
    from tsunami.gui.screens.device_list import open_device_list

    open_device_list(main_window.ctx, main_window, "acme")
    return main_window._tabs["devices:acme"]


def test_run_play_button_tooltip_until_selection(main_window):
    view = _device_view(main_window)
    button = view.run_button
    assert isinstance(button, QPushButton)
    assert button.property("primary") is True
    assert not button.isEnabled()
    assert button.toolTip() == RUN_DISABLED_TIP
    view.table.selectRow(0)
    assert button.isEnabled()
    assert button.toolTip() == ""
    view.table.clearSelection()
    assert not button.isEnabled()
    assert button.toolTip() == RUN_DISABLED_TIP


def test_disabled_primary_button_has_no_accent_fill(gui_ctx):
    from tsunami.gui import theme

    for palette in (theme.LIGHT, theme.DARK):
        css = theme.stylesheet(palette)
        rule = css[css.index('QPushButton[primary="true"]:disabled') :].split("}}")[0]
        assert palette.accent not in rule.split("{")[1]
        assert palette.muted in rule


def test_device_table_fits_without_horizontal_scroll(main_window, qtbot):
    main_window.resize(1100, 700)
    main_window.show()
    view = _device_view(main_window)
    qtbot.wait(50)
    bar = view.table.horizontalScrollBar()
    assert bar.maximum() == 0
    # The Name and Host columns follow their contents, not a fixed width.
    header = view.table.horizontalHeader()
    from PySide6.QtWidgets import QHeaderView

    assert header.sectionResizeMode(0) == QHeaderView.ResizeMode.ResizeToContents
    assert header.sectionResizeMode(4) == QHeaderView.ResizeMode.ResizeToContents
