"""The mouse wheel never changes a number box or dropdown; it scrolls the page instead.

The application style (NoWheelStyle, Fusion underneath) attaches a small event filter to
every number box and dropdown when Qt polishes it, once per widget, while it is alive.
The filter turns the wheel into a scroll of the enclosing scroll area. Arrow keys and
typing still work.

There is deliberately no app-wide Python event filter: PySide would call it for every
event in the application, including events for objects being destroyed, which crashed
the GUI tests intermittently.
"""

from __future__ import annotations

from PySide6.QtCore import QEvent, QObject
from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import (
    QAbstractScrollArea,
    QAbstractSpinBox,
    QApplication,
    QComboBox,
    QProxyStyle,
    QStyle,
    QWidget,
)


class _WheelGuard(QObject):
    def eventFilter(self, watched: QObject, event: QEvent) -> bool:  # noqa: N802 (Qt API)
        if event.type() != QEvent.Type.Wheel:
            return False
        if isinstance(event, QWheelEvent) and isinstance(watched, QWidget):
            area = watched.parentWidget()
            while area is not None and not isinstance(area, QAbstractScrollArea):
                area = area.parentWidget()
            delta = event.angleDelta().y()
            if area is not None and delta:
                bar = area.verticalScrollBar()
                steps = bar.singleStep() * 3 * (-1 if delta > 0 else 1)
                bar.setValue(bar.value() + steps * max(1, abs(delta) // 120))
        return True


_guard: _WheelGuard | None = None


def guard(widget: QWidget) -> QWidget:
    """Make a number box or dropdown ignore the wheel, scrolling its page instead."""
    global _guard
    if _guard is None:
        _guard = _WheelGuard(QApplication.instance())
    widget.installEventFilter(_guard)  # installing twice keeps a single filter
    return widget


class NoWheelStyle(QProxyStyle):
    """Fusion, with every number box and dropdown guarded against the wheel."""

    def polish(self, target):  # noqa: D102 - QStyle.polish overloads (widget, app, palette)
        result = super().polish(target)
        if isinstance(target, QAbstractSpinBox | QComboBox):
            guard(target)
        return result

    def styleHint(self, hint, option=None, widget=None, return_data=None):  # noqa: N802
        if hint == QStyle.StyleHint.SH_ComboBox_AllowWheelScrolling:
            return 0
        return super().styleHint(hint, option, widget, return_data)


def style() -> QProxyStyle:
    return NoWheelStyle("Fusion")


def install(app: QApplication) -> None:
    """Use the no-wheel style (theme.apply does the same when it sets the theme)."""
    app.setStyle(style())
