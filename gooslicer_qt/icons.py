"""Small outline icons drawn in code (no image files): a floppy disk for Export and a printer for
Send to printer. White on the dark theme; on the light theme white would vanish on the light
buttons, so they follow the button text colour there."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap

ICON_SIZE = 16


def _canvas(color: QColor, size: int, dpr: float, draw) -> QIcon:
    pm = QPixmap(round(size * dpr), round(size * dpr))
    pm.setDevicePixelRatio(dpr)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    pen = QPen(color, 1.3)
    pen.setJoinStyle(Qt.RoundJoin)
    pen.setCapStyle(Qt.RoundCap)
    p.setPen(pen)
    p.setBrush(Qt.NoBrush)
    p.scale(size / 16.0, size / 16.0)          # drawn on a 16 x 16 grid
    draw(p)
    p.end()
    return QIcon(pm)


def floppy_icon(color: QColor | str = "white", size: int = ICON_SIZE, dpr: float = 2.0) -> QIcon:
    def draw(p: QPainter):
        body = QPainterPath()                  # the disk, top-right corner cut off
        body.moveTo(2.5, 2.5)
        body.lineTo(11.5, 2.5)
        body.lineTo(13.5, 4.5)
        body.lineTo(13.5, 13.5)
        body.lineTo(2.5, 13.5)
        body.closeSubpath()
        p.drawPath(body)
        p.drawRect(QRectF(5.0, 2.5, 5.5, 3.5))   # the metal shutter
        p.drawLine(QPointF(8.8, 3.6), QPointF(8.8, 5.0))
        p.drawRect(QRectF(4.5, 8.5, 7.0, 5.0))   # the label
    return _canvas(QColor(color), size, dpr, draw)


def printer_icon(color: QColor | str = "white", size: int = ICON_SIZE, dpr: float = 2.0) -> QIcon:
    def draw(p: QPainter):
        p.drawRect(QRectF(4.5, 1.5, 7.0, 4.0))       # paper in
        body = QPainterPath()
        body.addRoundedRect(QRectF(1.5, 5.5, 13.0, 6.0), 1.5, 1.5)
        p.drawPath(body)
        p.drawRect(QRectF(4.5, 9.5, 7.0, 5.0))       # paper out
        p.drawLine(QPointF(6.2, 11.6), QPointF(9.8, 11.6))
        p.drawPoint(QPointF(12.3, 7.6))
    return _canvas(QColor(color), size, dpr, draw)
