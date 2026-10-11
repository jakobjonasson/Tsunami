"""Serial port picker, filled from the system's COM ports and refreshed when opened.

See design.md, GUI: Behavior rules.
"""

from __future__ import annotations

from collections.abc import Callable

from PySide6.QtWidgets import QComboBox, QWidget

from tsunami.core.transports.console import available_ports


class PortPicker(QComboBox):
    def __init__(
        self,
        parent: QWidget | None = None,
        *,
        provider: Callable[[], list[str]] = available_ports,
    ) -> None:
        super().__init__(parent)
        self._provider = provider
        self.refresh()

    def refresh(self) -> None:
        """Reload the port list, keeping the current choice if it is still there."""
        current = self.currentText()
        try:
            ports = self._provider()
        except Exception:  # noqa: BLE001 - a broken serial stack must not break the form
            ports = []
        self.blockSignals(True)
        self.clear()
        self.addItems(ports)
        if current in ports:
            self.setCurrentText(current)
        self.blockSignals(False)
        if self.currentText() != current:
            self.currentIndexChanged.emit(self.currentIndex())

    def showPopup(self) -> None:  # noqa: N802 - Qt API
        self.refresh()
        super().showPopup()

    def port(self) -> str | None:
        return self.currentText() or None
