"""Icons drawn from VS Code's Codicons font (CC BY 4.0, gui/assets/CODICONS-LICENSE.txt).

See design.md, GUI: Layout.
"""

from __future__ import annotations

import csv
import io
from functools import cache
from importlib.resources import files

from PySide6.QtCore import QRect, Qt
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap

_ASSETS = files("tsunami.gui.assets")


@cache
def _family() -> str:
    font_id = QFontDatabase.addApplicationFontFromData(_ASSETS.joinpath("codicon.ttf").read_bytes())
    families = QFontDatabase.applicationFontFamilies(font_id) if font_id >= 0 else []
    return families[0] if families else ""


@cache
def _codepoints() -> dict[str, int]:
    text = _ASSETS.joinpath("codicon.csv").read_text(encoding="utf-8-sig")
    return {row["short_name"]: int(row["unicode"], 16) for row in csv.DictReader(io.StringIO(text))}


def glyph(name: str) -> str:
    return chr(_codepoints().get(name, _codepoints()["symbol-misc"]))


@cache
def icon(name: str, color: str, size: int = 32) -> QIcon:
    """A Codicon as a QIcon in one color. Cached; call after QApplication exists."""
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    font = QFont(_family())
    font.setPixelSize(int(size * 0.8))
    painter.setFont(font)
    painter.setPen(QColor(color))
    painter.drawText(QRect(0, 0, size, size), Qt.AlignmentFlag.AlignCenter, glyph(name))
    painter.end()
    return QIcon(pixmap)
