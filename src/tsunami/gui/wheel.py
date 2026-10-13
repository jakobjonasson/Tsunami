"""Number boxes and dropdowns that ignore the mouse wheel, so it scrolls the page instead.

Use SpinBox and ComboBox wherever the GUI would use QSpinBox and QComboBox. Their own
wheelEvent ignores the event, and Qt passes an ignored wheel event to the parent, up to
the enclosing scroll area. Arrow keys and typing still work.

An earlier version did this with an app-wide event filter and then a style hook; both
made Qt call into Python for widgets it was destroying, and the GUI tests crashed
intermittently with access violations. Overriding a widget's own event handler is the
ordinary PySide pattern: it only ever runs for a live widget.
"""

from __future__ import annotations

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QComboBox, QSpinBox


class SpinBox(QSpinBox):
    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt API)
        event.ignore()


class ComboBox(QComboBox):
    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt API)
        event.ignore()
