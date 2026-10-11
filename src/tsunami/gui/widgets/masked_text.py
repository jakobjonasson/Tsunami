"""Read-only text view for masked and redacted content.

Shows ******** (masked secret values) and <redacted> (masked running-config tokens) in a
distinct color, and IOS-style error lines in the error color. Text given to this widget
must already be masked or redacted; the widget never holds raw secrets.

See design.md, GUI: Behavior rules.
"""

from __future__ import annotations

import re

from PySide6.QtGui import QColor, QSyntaxHighlighter, QTextCharFormat
from PySide6.QtWidgets import QPlainTextEdit, QWidget

from tsunami.gui import theme

_MASK_RE = re.compile(r"\*{8}|<redacted>")
_ERROR_RE = re.compile(r"^\s*(%\s|Invalid input:|Incomplete input:|Error:).*$")
_COMMENT_RE = re.compile(r"^\s*[!;].*$")


class _Highlighter(QSyntaxHighlighter):
    def highlightBlock(self, text: str) -> None:  # noqa: N802 - Qt API
        p = theme.current()
        if _COMMENT_RE.match(text):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(p.muted))
            self.setFormat(0, len(text), fmt)
        if _ERROR_RE.match(text):
            fmt = QTextCharFormat()
            fmt.setForeground(QColor(p.error))
            self.setFormat(0, len(text), fmt)
        mask = QTextCharFormat()
        mask.setForeground(QColor(p.accent))
        mask.setFontItalic(True)
        for match in _MASK_RE.finditer(text):
            self.setFormat(match.start(), match.end() - match.start(), mask)


class MaskedTextView(QPlainTextEdit):
    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setReadOnly(True)
        self.setFont(theme.mono_font())
        self.setLineWrapMode(QPlainTextEdit.LineWrapMode.NoWrap)
        self._highlighter = _Highlighter(self.document())
        self.setPlainText(text)

    def append_text(self, text: str) -> None:
        """Append and keep the view scrolled to the end if it already was."""
        bar = self.verticalScrollBar()
        at_end = bar.value() >= bar.maximum() - 4
        self.appendPlainText(text.rstrip("\n"))
        if at_end:
            bar.setValue(bar.maximum())
