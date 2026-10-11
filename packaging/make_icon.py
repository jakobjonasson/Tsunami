"""Generate src/tsunami/gui/assets/tsunami.ico: a wave on a rounded square, in sizes 16 to 256.

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
    QLinearGradient,
    QPainter,
    QPainterPath,
)

SIZES = (16, 24, 32, 48, 64, 256)
OUT = Path(__file__).parents[1] / "src" / "tsunami" / "gui" / "assets" / "tsunami.ico"


def _wave(left: float, right: float, base: float, amp: float, phase: float) -> QPainterPath:
    """A closed shape: a sine-like crest along the top, flat bottom at y=1."""
    path = QPainterPath(QPointF(left, 1.0))
    steps = 48
    for i in range(steps + 1):
        x = left + (right - left) * i / steps
        t = i / steps
        # one rising swell that curls: sine plus a slow ramp toward the crest
        y = base - amp * math.sin(2 * math.pi * (t * 1.25 + phase)) - 0.10 * t
        path.lineTo(x, y)
    path.lineTo(right, 1.0)
    path.closeSubpath()
    return path


def render(size: int) -> QImage:
    img = QImage(size, size, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.transparent)
    p = QPainter(img)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.scale(size, size)  # draw in unit coordinates

    margin = 0.04
    tile = QRectF(margin, margin, 1 - 2 * margin, 1 - 2 * margin)
    clip = QPainterPath()
    clip.addRoundedRect(tile, 0.22, 0.22)

    bg = QLinearGradient(0, 0, 0, 1)
    bg.setColorAt(0.0, QColor("#2b7fb8"))
    bg.setColorAt(1.0, QColor("#0f5a73"))
    p.fillPath(clip, bg)

    p.setClipPath(clip)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor(255, 255, 255, 70))
    p.drawPath(_wave(0.0, 1.0, 0.50, 0.12, 0.10))
    p.setBrush(QColor("#7fd6d0"))
    p.drawPath(_wave(0.0, 1.0, 0.62, 0.10, 0.35))
    p.setBrush(QColor("#e9fbf8"))
    p.drawPath(_wave(0.0, 1.0, 0.76, 0.07, 0.60))
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
