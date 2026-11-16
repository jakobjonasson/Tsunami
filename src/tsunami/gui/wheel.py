"""Number boxes and dropdowns that ignore the mouse wheel, so it scrolls the page instead.

Use SpinBox and ComboBox wherever the GUI would use QSpinBox and QComboBox. Their own
wheelEvent ignores the event, and Qt passes an ignored wheel event to the parent, up to
the enclosing scroll area. Arrow keys and typing still work.

An earlier version did this with an app-wide event filter and then a style hook; both
made Qt call into Python for widgets it was destroying, and the GUI tests crashed
intermittently with access violations. Overriding a widget's own event handler is the
ordinary PySide pattern: it only ever runs for a live widget.

SpinBox also lines its text up with the other fields. Under a stylesheet Qt lays a spin
box's inner line edit over the whole width of the box, border and side padding included,
so the number sat about 7 px left of the text in a line edit. The inner edit gets that
distance as text margins.
"""

from __future__ import annotations

from PySide6.QtGui import QWheelEvent
from PySide6.QtWidgets import QComboBox, QSpinBox, QWidget

from tsunami.gui import theme


class SpinBox(QSpinBox):
    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        inset = theme.FIELD_BORDER + theme.FIELD_PADDING_X
        self.lineEdit().setTextMargins(inset, 0, inset, 0)

    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt API)
        event.ignore()


class ComboBox(QComboBox):
    def wheelEvent(self, event: QWheelEvent) -> None:  # noqa: N802 (Qt API)
        event.ignore()
