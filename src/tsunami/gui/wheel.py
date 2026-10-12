"""App-wide guard: the mouse wheel never changes a number box or dropdown.

The wheel is forwarded to the enclosing scroll area instead, so scrolling a form with the
pointer over a field scrolls the page. Arrow keys and typing still work.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QWidget,
)


class WheelGuard(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt API)
        if event.type() != QEvent.Type.Wheel or not isinstance(watched, QWidget):
            return False
        # The spin box's inner line edit receives the event first, so look at the parents too.
        owner: QWidget | None = watched
        while owner is not None and not isinstance(owner, QAbstractSpinBox | QComboBox):
            owner = owner.parentWidget()
        if owner is None:
            return False
        if isinstance(event, QWheelEvent):
            area = owner.parentWidget()
            while area is not None and not isinstance(area, QAbstractScrollArea):
                area = area.parentWidget()
            if area is not None:
                bar = area.verticalScrollBar()
                delta = event.angleDelta().y()
                if delta:
                    steps = bar.singleStep() * 3 * (-1 if delta > 0 else 1)
                    bar.setValue(bar.value() + steps * max(1, abs(delta) // 120))
        return True


def install(app: QApplication) -> WheelGuard:
    """Install the guard on the application; keep the returned object alive."""
    existing = getattr(app, "_tsunami_wheel_guard", None)
    if existing is not None:
        return existing
    guard = WheelGuard(app)
    app.installEventFilter(guard)
    app._tsunami_wheel_guard = guard  # type: ignore[attr-defined]
    return guard
