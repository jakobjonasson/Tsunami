"""Diff view for missing and extra lines.

Lines starting with "+ " are missing from the device (apply would add them) and show in
green. Lines starting with "- " are extra on the device and show in red. The text is the
redacted diff stored with each device in history.

See design.md, GUI: Job view; Execution engine: Diff and compliance.
"""

from __future__ import annotations

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from tsunami.gui import theme


class _DiffHighlighter(QSyntaxHighlighter):
    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        p = theme.current()
        color = None
        if text.startswith("+ "):
            color = p.success
        elif text.startswith("- "):
            color = p.error
        if color is not None:
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(color))
            self.setFormat(0, len(text), fmt)


class DiffView(QPlainTextEdit):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(theme.mono_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._highlighter = _DiffHighlighter(self.document())
        self.setPlainText(text)

    def set_diff(self, text: str | None) -> None:
        text = text or ""
        if text != self.toPlainText():
            self.setPlainText(text)
