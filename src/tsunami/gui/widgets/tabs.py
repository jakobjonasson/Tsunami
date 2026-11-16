"""Tab widgets whose tabs are all the same width.

Use TabWidget wherever the GUI would use QTabWidget. Qt sizes each tab to its own title,
so a row of tabs is ragged and a tab's close button moves when a neighbour's title changes.
Here every tab of a bar is as wide as the bar's widest tab, up to MAX_TAB_WIDTH. When the
tabs do not fit the tab widget at that width they all narrow together, down to
MIN_TAB_WIDTH, and only then does the bar scroll. A title that does not fit its tab is
elided and shown in full in a tooltip.
"""

from __future__ import annotations

import html

from PySide6.QtCore import QEvent, QSize, Qt
from PySide6.QtGui import QResizeEvent
from PySide6.QtWidgets import QTabBar, QTabWidget, QToolTip, QWidget

MAX_TAB_WIDTH = 220
MIN_TAB_WIDTH = 170


class EqualTabBar(QTabBar):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        # The width every tab gets. Qt asks for every tab's size in each layout pass, so
        # the answer is kept for the pass and dropped when the pass ends (tabLayoutChange):
        # nothing tells a tab bar that a title changed, but a title change starts a pass.
        self._width: int | None = None
        self.setElideMode(Qt.TextElideMode.ElideRight)

    def own_width(self, index: int) -> int:
        """The width Qt would give this tab for its own title, icon and buttons."""
        return super().tabSizeHint(index).width()

    def tab_width(self) -> int:
        if self._width is None:
            count = self.count()
            width = min(MAX_TAB_WIDTH, max((self.own_width(i) for i in range(count)), default=0))
            # The room is the tab widget's, not the bar's own: outside document mode Qt
            # sizes the bar from these very hints.
            parent = self.parentWidget()
            room = parent.width() if isinstance(parent, QTabWidget) else self.width()
            if count * width > room:
                width = max(room // count, min(width, MIN_TAB_WIDTH))
            self._width = width
        return self._width

    def tabSizeHint(self, index: int) -> QSize:  # noqa: N802 (Qt API)
        size = super().tabSizeHint(index)
        size.setWidth(self.tab_width())
        return size

    def tabInserted(self, index: int) -> None:  # noqa: N802 (Qt API)
        self._width = None
        super().tabInserted(index)

    def tabRemoved(self, index: int) -> None:  # noqa: N802 (Qt API)
        self._width = None
        super().tabRemoved(index)

    def tabLayoutChange(self) -> None:  # noqa: N802 (Qt API)
        self._width = None
        super().tabLayoutChange()

    def relayout(self) -> None:
        """Make Qt lay the tabs out again. It has no call for that; setting the elide mode,
        even to the value it has, is the one setter that always does it."""
        self._width = None
        self.setElideMode(self.elideMode())

    def is_elided(self, index: int) -> bool:
        return self.own_width(index) > self.tabRect(index).width()

    def elided_tooltip(self, index: int) -> str:
        """The full title of a tab whose title is cut short, as tooltip markup, or ''.

        A tooltip renders anything that looks like HTML, and a title can come from a file
        or a device name, so the title is escaped.
        """
        if index < 0 or self.tabToolTip(index) or not self.is_elided(index):
            return ""
        return f"<qt>{html.escape(self.tabText(index))}</qt>"

    def event(self, event: QEvent) -> bool:
        if event.type() == QEvent.Type.ToolTip:
            tip = self.elided_tooltip(self.tabAt(event.pos()))
            if tip:
                QToolTip.showText(event.globalPos(), tip, self)
                return True
        return super().event(event)


class TabWidget(QTabWidget):
    def __init__(self, parent: QWidget | None = None, **properties: object) -> None:
        super().__init__(parent, **properties)
        # Before any tab is added: Qt does not support replacing a bar that has tabs.
        self.setTabBar(EqualTabBar(self))

    def resizeEvent(self, event: QResizeEvent) -> None:  # noqa: N802 (Qt API)
        # The tab width depends on this widget's width. Qt lays the tabs out again only
        # when the bar itself is resized, and outside document mode a bar that fits is not.
        bar = self.tabBar()
        if isinstance(bar, EqualTabBar):
            bar.relayout()
        super().resizeEvent(event)
