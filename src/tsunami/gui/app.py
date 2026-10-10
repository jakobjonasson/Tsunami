"""tsunami.exe entry point (windowed). Phase 4 builds the real window (design.md, GUI)."""

from __future__ import annotations

import sys

from tsunami import bootstrap


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    code = bootstrap.handle_common_flags(argv, "tsunami")
    if code is not None:
        return code
    bootstrap.configure_keyring()

    from PySide6.QtWidgets import QApplication, QLabel, QMainWindow

    app = QApplication([sys.argv[0], *argv])
    window = QMainWindow()
    window.setWindowTitle("Tsunami")
    window.setCentralWidget(QLabel("Skeleton build. The GUI arrives in Phase 4."))
    window.resize(640, 360)
    window.show()
    return app.exec()
