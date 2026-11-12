"""Ctrl+F for read-only text views: a find bar that highlights every match.

Enter or F3 finds the next match, Shift+Enter or Shift+F3 the previous one, wrapping at the
ends; Esc closes the bar. Matching is plain text and ignores case unless Match case is on.
It searches what the view shows, so a masked value is never found by its secret text.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QKeySequence, QShortcut, QTextCursor, QTextDocument
from PySide6.QtWidgets import (
    QCheckBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTextEdit,
    QWidget,
)

from tsunami.gui import theme


class FindBar(QWidget):
    """Searches whichever view `target` returns (a stacked page can change)."""

    def __init__(self, host: QWidget, target: Callable[[], QWidget | None]) -> None:
        super().__init__(host)
        self._target = target
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        self.edit = QLineEdit()
        self.edit.setPlaceholderText("Find")
        self.edit.setClearButtonEnabled(True)
        self.case_box = QCheckBox("Match case")
        self.count_label = QLabel()
        self.count_label.setObjectName("Muted")
        self.prev_button = QPushButton("Previous")
        self.next_button = QPushButton("Next")
        self.close_button = QPushButton("Close")
        layout.addWidget(self.edit, 1)
        for widget in (
            self.case_box,
            self.count_label,
            self.prev_button,
            self.next_button,
            self.close_button,
        ):
            layout.addWidget(widget)
        self.edit.textChanged.connect(lambda _t: self.update_matches(move=True))
        self.case_box.toggled.connect(lambda _on: self.update_matches(move=True))
        self.edit.returnPressed.connect(self.find_next)
        self.next_button.clicked.connect(lambda _=False: self.find_next())
        self.prev_button.clicked.connect(lambda _=False: self.find_previous())
        self.close_button.clicked.connect(lambda _=False: self.close_bar())
        context = Qt.ShortcutContext.WidgetWithChildrenShortcut
        for keys, slot in (
            (QKeySequence.StandardKey.Find, self.open_bar),
            (QKeySequence(Qt.Key.Key_F3), self.find_next),
            (QKeySequence("Shift+F3"), self.find_previous),
        ):
            shortcut = QShortcut(keys, host)
            shortcut.setContext(context)
            shortcut.activated.connect(slot)
        for keys, slot in (
            (QKeySequence(Qt.Key.Key_Escape), self.close_bar),
            (QKeySequence("Shift+Return"), self.find_previous),
        ):
            shortcut = QShortcut(keys, self)
            shortcut.setContext(context)
            shortcut.activated.connect(slot)
        self.hide()

    def view(self) -> QPlainTextEdit | None:
        view = self._target()
        return view if isinstance(view, QPlainTextEdit) else None

    def open_bar(self) -> None:
        view = self.view()
        if view is not None and view.textCursor().hasSelection():
            selected = view.textCursor().selectedText()
            if " " not in selected:
                self.edit.setText(selected)
        self.show()
        self.edit.setFocus()
        self.edit.selectAll()
        self.update_matches(move=False)

    def close_bar(self) -> None:
        self.hide()
        view = self.view()
        if view is not None:
            view.setExtraSelections([])
            view.setFocus()

    def _flags(self, backward: bool = False) -> QTextDocument.FindFlag:
        flags = QTextDocument.FindFlag(0)
        if self.case_box.isChecked():
            flags |= QTextDocument.FindFlag.FindCaseSensitively
        if backward:
            flags |= QTextDocument.FindFlag.FindBackward
        return flags

    def matches(self) -> list[QTextCursor]:
        view, text = self.view(), self.edit.text()
        if view is None or not text:
            return []
        found, cursor = [], QTextCursor(view.document())
        while True:
            cursor = view.document().find(text, cursor, self._flags())
            if cursor.isNull():
                return found
            found.append(QTextCursor(cursor))

    def update_matches(self, *, move: bool) -> None:
        view = self.view()
        if view is None:
            return
        found = self.matches()
        colour = QColor(theme.current().warning)
        colour.setAlpha(90)
        selections = []
        for cursor in found:
            sel = QTextEdit.ExtraSelection()
            sel.cursor = cursor
            sel.format.setBackground(colour)
            selections.append(sel)
        view.setExtraSelections(selections)
        if move and found:
            start = QTextCursor(view.document())
            start.setPosition(view.textCursor().selectionStart())
            view.setTextCursor(start)
            self.find_next()
        else:
            self._count(found)

    def _count(self, found: list[QTextCursor] | None = None) -> None:
        found = self.matches() if found is None else found
        view = self.view()
        if not self.edit.text():
            self.count_label.setText("")
            return
        if not found:
            self.count_label.setText("No results")
            return
        current = view.textCursor() if view is not None else None
        index = next(
            (
                i + 1
                for i, c in enumerate(found)
                if current is not None and c.selectionStart() == current.selectionStart()
            ),
            0,
        )
        self.count_label.setText(f"{index} of {len(found)}" if index else f"{len(found)} found")

    def find_next(self) -> bool:
        return self._find(backward=False)

    def find_previous(self) -> bool:
        return self._find(backward=True)

    def _find(self, *, backward: bool) -> bool:
        view, text = self.view(), self.edit.text()
        if view is None or not text:
            return False
        if not self.isVisible():
            self.show()
        ok = view.find(text, self._flags(backward))
        if not ok:  # wrap around
            cursor = view.textCursor()
            cursor.movePosition(
                QTextCursor.MoveOperation.End if backward else QTextCursor.MoveOperation.Start
            )
            view.setTextCursor(cursor)
            ok = view.find(text, self._flags(backward))
        self._count()
        return ok
