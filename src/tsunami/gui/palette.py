"""Command palette (Ctrl+Shift+P) and quick open (Ctrl+P): one filterable popup list.

See design.md, GUI: Layout.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QDialog, QLineEdit, QListWidget, QListWidgetItem, QVBoxLayout, QWidget


@dataclass(frozen=True)
class PaletteItem:
    title: str
    callback: Callable[[], None]
    detail: str = ""  # shortcut or location, shown on the right


def matches(query: str, text: str) -> bool:
    """Case-insensitive subsequence match, like VS Code's quick pick."""
    position = 0
    text = text.lower()
    for char in query.lower().replace(" ", ""):
        position = text.find(char, position)
        if position < 0:
            return False
        position += 1
    return True


class PalettePopup(QDialog):
    def __init__(self, items: Sequence[PaletteItem], placeholder: str, parent: QWidget) -> None:
        super().__init__(parent, Qt.WindowType.Popup | Qt.WindowType.FramelessWindowHint)
        self.setObjectName("Card")
        self.items = list(items)
        self.chosen: PaletteItem | None = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self.query = QLineEdit(self)
        self.query.setPlaceholderText(placeholder)
        self.list = QListWidget(self)
        layout.addWidget(self.query)
        layout.addWidget(self.list)
        self.query.textChanged.connect(self._filter)
        self.query.returnPressed.connect(self._accept_current)
        self.list.itemActivated.connect(lambda _: self._accept_current())
        self.query.installEventFilter(self)
        self._filter("")
        width = min(640, max(360, parent.width() // 2))
        self.resize(width, 360)
        top_left = parent.mapToGlobal(parent.rect().topLeft())
        self.move(top_left.x() + (parent.width() - width) // 2, top_left.y() + 48)

    def _filter(self, text: str) -> None:
        self.list.clear()
        for item in self.items:
            if matches(text, item.title):
                row = QListWidgetItem(f"{item.title}    {item.detail}".rstrip())
                row.setData(Qt.ItemDataRole.UserRole, item)
                self.list.addItem(row)
        if self.list.count():
            self.list.setCurrentRow(0)

    def _accept_current(self) -> None:
        row = self.list.currentItem()
        if row is None:
            return
        self.chosen = row.data(Qt.ItemDataRole.UserRole)
        self.accept()
        self.chosen.callback()

    def eventFilter(self, watched, event) -> bool:  # noqa: N802 - Qt API
        # Up/Down in the query box move through the list.
        if watched is self.query and event.type() == event.Type.KeyPress:
            if event.key() in (Qt.Key.Key_Down, Qt.Key.Key_Up):
                row = self.list.currentRow() + (1 if event.key() == Qt.Key.Key_Down else -1)
                self.list.setCurrentRow(max(0, min(self.list.count() - 1, row)))
                return True
        return super().eventFilter(watched, event)
