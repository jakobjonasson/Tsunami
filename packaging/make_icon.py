"""Generate src/tsunami/gui/assets/tsunami.ico: four bands of water with wavy edges on a
rounded square, in sizes 16 to 256.

Run: uv run python packaging/make_icon.py
Needs PySide6 only. The ICO container (PNG entries) is written here, so Pillow is not needed.
"""

from __future__ import annotations

import math
import os
import struct
import sys
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QColor,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
)

SIZES = (16, 24, 32, 48, 64, 256)
OUT = Path(__file__).parents[1] / "src" / "tsunami" / "gui" / "assets" / "tsunami.ico"


# The tile's four bands, top to bottom, and the three wavy edges between them. Flat
# colors, no gradient: the bands still read as water at 16 px, where a gradient blurs.
DEEP, MID, BRIGHT, FOAM = "#12333d", "#1c6678", "#3bb6d0", "#a9e6f0"
# (color below the edge, height at the left, tilt across the tile, amplitude). An edge is
# one sine period, so a positive amplitude crests in the left half and dips in the right.
EDGES = (
    (MID, 0.437, -0.042, 0.056),
    (BRIGHT, 0.626, 0.0, -0.070),
    (FOAM, 0.781, 0.0, 0.041),
)
MARGIN = 0.02
RADIUS = 0.215


def _band(base: float, tilt: float, amp: float) -> QPainterPath:
    """Everything below one wavy edge, in unit coordinates (y grows downward)."""
    path = QPainterPath(QPointF(0.0, 1.0))
    steps = 64
    for i in range(steps + 1):
        x = i / steps
        path.lineTo(x, base + tilt * x - amp * math.sin(2 * math.pi * x))
    path.lineTo(1.0, 1.0)
    path.closeSubpath()
    return path


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size, size)  # draw in unit coordinates
    p.translate(MARGIN, MARGIN)
    p.scale(1 - 2 * MARGIN, 1 - 2 * MARGIN)  # the tile is now the unit square

    clip = QPainterPath()
    clip.addRoundedRect(QRectF(0, 0, 1, 1), RADIUS, RADIUS)
    p.fillPath(clip, QColor(DEEP))
    p.setClipPath(clip)
    p.setPen(Qt.PenStyle.NoPen)
    for color, base, tilt, amp in EDGES:
        p.setBrush(QColor(color))
        p.drawPath(_band(base, tilt, amp))
    p.end()
    return img


def png_bytes(img: QImage) -> bytes:
    data = QByteArray()
    buf = QBuffer(data)
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    img.save(buf, "PNG")
    buf.close()
    return bytes(data)


def build_ico(images: list[tuple[int, bytes]]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size, png in images:
        dim = 0 if size >= 256 else size  # 0 means 256 in the ICO directory
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(png), offset)
        blobs += png
        offset += len(png)
    return header + entries + blobs


def main() -> int:
    _app = QGuiApplication(sys.argv)
    images = [(size, png_bytes(render(size))) for size in SIZES]
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(build_ico(images))
    print(f"wrote {OUT} ({OUT.stat().st_size} bytes, sizes {', '.join(map(str, SIZES))})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
