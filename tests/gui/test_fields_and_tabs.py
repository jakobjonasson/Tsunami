"""Number boxes line up with the other input fields, and a tab bar's tabs are equal width."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import QLabel, QLineEdit, QTabBar, QWidget

from tsunami.gui import theme, wheel
from tsunami.gui.widgets.tabs import MAX_TAB_WIDTH, MIN_TAB_WIDTH, EqualTabBar, TabWidget

GUI_SRC = Path(__file__).parents[2] / "src" / "tsunami" / "gui"
HOSTILE = "<b>bold</b> <a href='http://192.0.2.1/'>a device name far too long for one tab</a>"


def _widths(tabs) -> list[int]:
    bar = tabs.tabBar()
    return [bar.tabRect(i).width() for i in range(bar.count())]


def test_spin_box_is_as_tall_as_a_line_edit_and_insets_its_text(gui_ctx, qtbot):
    edit = QLineEdit("22")
    spin = wheel.SpinBox()
    spin.setValue(22)
    qtbot.addWidget(edit)
    qtbot.addWidget(spin)
    edit.ensurePolished()
    spin.ensurePolished()
    assert spin.sizeHint().height() == edit.sizeHint().height()
    margins = spin.lineEdit().textMargins()
    inset = theme.FIELD_BORDER + theme.FIELD_PADDING_X
    assert (margins.left(), margins.right()) == (inset, inset)
    assert (margins.top(), margins.bottom()) == (0, 0)


def test_stylesheet_field_padding_matches_the_spin_box_inset():
    css = theme.stylesheet(theme.DARK)
    assert f"padding: 4px {theme.FIELD_PADDING_X}px;" in css
    assert f"border: {theme.FIELD_BORDER}px solid" in css


def test_gui_uses_the_shared_spin_box_and_tab_widget():
    """A bare QSpinBox has misplaced text; a bare QTabWidget has ragged tabs."""
    offenders = []
    for path in GUI_SRC.rglob("*.py"):
        if path.name in ("wheel.py", "tabs.py"):
            continue
        for match in re.finditer(r"\bQ(SpinBox|TabWidget)\(", path.read_text(encoding="utf-8")):
            offenders.append(f"{path.relative_to(GUI_SRC)}: {match.group(0)}")
    assert offenders == []


def _shown(qtbot, *, document_mode: bool = False) -> TabWidget:
    """A TabWidget on screen (offscreen), so its bar is laid out as in the app."""
    tabs = TabWidget()
    tabs.setDocumentMode(document_mode)
    qtbot.addWidget(tabs)
    tabs.resize(900, 200)
    tabs.show()
    return tabs


def _wide_tab(tabs: TabWidget, index: int) -> None:
    """Make one tab need more than MAX_TAB_WIDTH, as a long title does."""
    filler = QWidget()
    filler.setFixedSize(2 * MAX_TAB_WIDTH, 10)
    tabs.tabBar().setTabButton(index, QTabBar.ButtonPosition.RightSide, filler)


def _icon() -> QIcon:
    pixmap = QPixmap(16, 16)
    pixmap.fill(QColor("red"))
    return QIcon(pixmap)


# The offscreen platform can measure every text as zero wide, so these tests make one tab
# need more room with an icon or a tab button, never with a longer title.


def test_editor_tabs_are_equal_width(main_window):
    main_window.open_tab("a", "A", lambda: QLabel("a"))
    main_window.open_tab("b", "Inventory", lambda: QLabel("b"), icon="close")
    main_window.open_tab("c", "A longer title", lambda: QLabel("c"))
    bar = main_window.editor.tabBar()
    assert isinstance(bar, EqualTabBar)
    own = [bar.own_width(i) for i in range(bar.count())]
    assert own[1] > own[0]  # the tab with an icon needs more room
    widths = _widths(main_window.editor)
    assert len(set(widths)) == 1
    assert widths[0] == max(own) <= MAX_TAB_WIDTH


def test_tabs_follow_a_change_to_one_tab_and_a_closed_tab(gui_ctx, qtbot):
    tabs = _shown(qtbot)
    for title in ("Jobs", "Compliance", "Reveals"):
        tabs.addTab(QLabel(title), title)
    before = _widths(tabs)
    assert len(set(before)) == 1
    # Nothing tells a tab bar that one tab's title or icon changed; it must still follow.
    tabs.setTabIcon(0, _icon())
    grown = _widths(tabs)
    assert len(set(grown)) == 1
    assert grown[0] > before[0]
    tabs.removeTab(0)
    assert _widths(tabs) == before[1:]


def test_wide_tab_is_capped_and_its_title_escaped_in_the_tooltip(gui_ctx, qtbot):
    tabs = _shown(qtbot)
    tabs.addTab(QLabel("r"), "Render")
    tabs.addTab(QLabel("h"), HOSTILE)
    bar = tabs.tabBar()
    _wide_tab(tabs, 1)
    assert _widths(tabs) == [MAX_TAB_WIDTH, MAX_TAB_WIDTH]
    assert bar.elided_tooltip(0) == ""
    tip = bar.elided_tooltip(1)
    assert "&lt;b&gt;bold&lt;/b&gt;" in tip
    assert "<b>" not in tip
    assert "<a " not in tip
    # A tooltip the screen set wins, and is not replaced.
    tabs.setTabToolTip(1, "set by the screen")
    assert bar.elided_tooltip(1) == ""
    assert bar.elided_tooltip(-1) == ""


@pytest.mark.parametrize("document_mode", [False, True])
def test_tabs_narrow_together_before_the_bar_scrolls_and_widen_again(gui_ctx, qtbot, document_mode):
    tabs = _shown(qtbot, document_mode=document_mode)
    for title in ("Jobs", "Compliance", "Reveals"):
        tabs.addTab(QLabel(title), title)
    _wide_tab(tabs, 2)
    bar = tabs.tabBar()
    assert _widths(tabs) == [MAX_TAB_WIDTH] * 3
    assert [bar.is_elided(i) for i in range(3)] == [False, False, True]
    tabs.resize(600, 200)  # three tabs of 220 no longer fit
    assert _widths(tabs) == [600 // 3] * 3
    tabs.resize(300, 200)  # too narrow even for the minimum: the bar scrolls
    assert _widths(tabs) == [MIN_TAB_WIDTH] * 3
    tabs.resize(900, 200)
    assert _widths(tabs) == [MAX_TAB_WIDTH] * 3


def test_narrow_tab_widget_does_not_stretch_tabs_that_need_less(gui_ctx, qtbot):
    tabs = _shown(qtbot)
    tabs.addTab(QLabel("r"), "Render")
    tabs.addTab(QLabel("d"), "Diff")
    before = _widths(tabs)
    assert before[0] < MIN_TAB_WIDTH  # two short titles
    tabs.resize(2 * before[0] - 10, 200)
    assert _widths(tabs) == before


def test_named_tab_widget_keeps_its_object_name(gui_ctx, qtbot):
    tabs = TabWidget(objectName="Editor")
    qtbot.addWidget(tabs)
    assert tabs.objectName() == "Editor"
