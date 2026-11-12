"""Masked text where one masked value can be revealed while the mouse button is held.

Press a ******** or <redacted> token: Tsunami asks for its password again (once every
CONFIRM_SECONDS), then pressing and holding the token shows its value over it; releasing
the button hides it. The value is read from the store or the backup file at that moment
(core/reveal.py), audited first, and shown only in an overlay label: it is never put into
the document, so selecting and copying the text copies the mask, and the label itself
cannot be selected, copied or right-clicked.

See design.md, GUI: Revealing a masked value.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, Protocol

from PySide6.QtCore import QPoint, Qt
from PySide6.QtGui import QCursor, QMouseEvent
from PySide6.QtWidgets import QLabel, QToolTip, QWidget

from tsunami.core import reveal
from tsunami.core.issues import TsunamiError
from tsunami.core.reveal import MaskSpan
from tsunami.gui import theme
from tsunami.gui.widgets.masked_text import MaskedTextView

CONFIRM_SECONDS = 120.0  # how long one password confirmation allows reveals in a view
HINT = "Press and hold to reveal (asks for the Tsunami password; every reveal is recorded)"


class Revealer(Protocol):
    def confirm(self, parent: QWidget, span: MaskSpan) -> bool: ...

    def value(self, span: MaskSpan, shown_text: str) -> str: ...


class RevealTextView(MaskedTextView):
    """A MaskedTextView whose masks can be revealed one at a time (see the module doc)."""

    def __init__(self, text: str = "", parent: QWidget | None = None) -> None:
        super().__init__(text, parent)
        self._spans: list[MaskSpan] = []
        self._revealer: Revealer | None = None
        self._confirmed_at: float | None = None
        self.clock: Callable[[], float] = time.monotonic
        self.overlay = QLabel(self.viewport())
        self.overlay.setTextFormat(Qt.TextFormat.PlainText)
        self.overlay.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.overlay.setContextMenuPolicy(Qt.ContextMenuPolicy.NoContextMenu)
        self.overlay.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.overlay.setFont(theme.mono_font())
        colors = theme.current()
        self.overlay.setStyleSheet(
            f"background: {colors.accent}; color: white; padding: 0 2px; border-radius: 2px;"
        )
        self.overlay.hide()
        self.viewport().setMouseTracking(True)

    def set_reveal(self, text: str, spans: list[MaskSpan], revealer: Revealer | None) -> None:
        self.hide_value()
        self.setPlainText(text)
        self._spans, self._revealer = list(spans), revealer

    def clear(self) -> None:
        """Empty the view; nothing is left to reveal."""
        self.hide_value()
        self._spans, self._revealer = [], None
        super().clear()

    def setPlainText(self, text: str) -> None:  # noqa: N802 - Qt API
        """Plain text with no reveal targets (set_reveal sets both)."""
        self._spans, self._revealer = [], None
        super().setPlainText(text)

    @property
    def spans(self) -> list[MaskSpan]:
        return list(self._spans)

    def span_at(self, point: QPoint) -> MaskSpan | None:
        position = self.cursorForPosition(point).position()
        return next((s for s in self._spans if s.start <= position <= s.end), None)

    def point_of(self, span: MaskSpan) -> QPoint:
        """A viewport point on the span, for tests and for placing the overlay."""
        cursor = self.textCursor()
        cursor.setPosition(span.start + 1)
        return self.cursorRect(cursor).center()

    # -- the reveal -----------------------------------------------------------------------

    def press(self, span: MaskSpan) -> bool:
        """Start a reveal of span: ask for the password if due, else show the value. True
        if the value is now showing."""
        if self._revealer is None:
            return False
        now = self.clock()
        if self._confirmed_at is None or now - self._confirmed_at > CONFIRM_SECONDS:
            if not self._revealer.confirm(self, span):
                return False
            self._confirmed_at = self.clock()
            QToolTip.showText(QCursor.pos(), "Confirmed. Press and hold the value to reveal it.")
            return False
        try:
            value = self._revealer.value(span, self.toPlainText())
        except TsunamiError as exc:
            QToolTip.showText(QCursor.pos(), f"Cannot reveal: {exc}")
            return False
        self._show(span, value)
        return True

    def _show(self, span: MaskSpan, value: str) -> None:
        cursor = self.textCursor()
        cursor.setPosition(span.start)
        rect = self.cursorRect(cursor)
        self.overlay.setText(value)
        self.overlay.adjustSize()
        self.overlay.move(rect.left(), rect.top())
        self.overlay.show()
        self.overlay.raise_()

    def hide_value(self) -> None:
        self.overlay.clear()
        self.overlay.hide()

    @property
    def revealing(self) -> bool:
        return not self.overlay.isHidden()

    # -- events ---------------------------------------------------------------------------

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if event.button() == Qt.MouseButton.LeftButton and self._revealer is not None:
            span = self.span_at(event.position().toPoint())
            if span is not None:
                event.accept()
                self.press(span)
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        if self.revealing:
            self.hide_value()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802 - Qt API
        over = self._revealer is not None and self.span_at(event.position().toPoint())
        self.viewport().setCursor(
            Qt.CursorShape.PointingHandCursor if over else Qt.CursorShape.IBeamCursor
        )
        self.viewport().setToolTip(HINT if over else "")
        if not self.revealing:
            super().mouseMoveEvent(event)

    def focusOutEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self.hide_value()
        super().focusOutEvent(event)

    def leaveEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self.hide_value()
        super().leaveEvent(event)

    def hideEvent(self, event: Any) -> None:  # noqa: N802 - Qt API
        self.hide_value()
        super().hideEvent(event)


class _BaseRevealer:
    kind = ""

    def __init__(self, ctx: Any, *, customer: str, device: str | None, context: str) -> None:
        self.ctx, self.customer, self.device, self.context = ctx, customer, device, context

    def confirm(self, parent: QWidget, span: MaskSpan) -> bool:
        lock = getattr(self.ctx, "lock", None)
        if lock is None:
            QToolTip.showText(QCursor.pos(), "Revealing needs the Tsunami password set.")
            return False
        return lock.confirm_password(span.item, parent)

    def _audit(self, span: MaskSpan, shown_text: str) -> None:
        reveal.audit(
            self.ctx.history,
            revealed_by=self.ctx.windows_user(),
            kind=self.kind,
            customer=self.customer,
            device=self.device,
            span=span,
            context=self.context,
            shown_text=shown_text,
        )
        changed = getattr(self.ctx, "jobs_changed", None)
        if changed is not None:  # History's Reveals tab
            changed.emit()


class PreviewRevealer(_BaseRevealer):
    """A run preview's masks: values come from the store, or the prompt typed here."""

    kind = reveal.PREVIEW

    def __init__(
        self,
        ctx: Any,
        *,
        customer: str,
        device: str | None,
        play_path: str,
        typed: Mapping[str, Any],
    ) -> None:
        super().__init__(ctx, customer=customer, device=device, context=f"preview of {play_path}")
        self._typed = dict(typed)

    def value(self, span: MaskSpan, shown_text: str) -> str:
        self._audit(span, shown_text)
        return reveal.preview_value(self.ctx.secrets, span.ref, self._typed)


class BackupRevealer(_BaseRevealer):
    """A backup's <redacted> tokens: values come from the backup file."""

    kind = reveal.BACKUP

    def __init__(self, ctx: Any, *, customer: str, device: str, path: Path) -> None:
        super().__init__(ctx, customer=customer, device=device, context=Path(path).name)
        self.path = Path(path)

    def value(self, span: MaskSpan, shown_text: str) -> str:
        self._audit(span, shown_text)
        line = reveal.line_text(shown_text, span.line)
        return reveal.backup_value(self.path, self.ctx.paths.backups_dir, line, span.ref)
