"""Colours of the 3D view's objects (Edit → Preferences → 3D view colors), each with a
transparency, kept in QSettings as #AARRGGBB under colors/<key>."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, Qt, Signal
from PySide6.QtGui import QColor, QConicalGradient, QImage, QPainter, QPen, QRadialGradient
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLineEdit, QSlider,
                               QSpinBox, QVBoxLayout, QWidget)

# key, label, default (r, g, b) 0..1, default opacity 0..1, tooltip
VIEW_COLORS = (
    ("model", "Models", (0.55, 0.70, 0.78), 1.0, "Parts that aren't selected"),
    ("selected", "Selected models", (0.98, 0.62, 0.20), 1.0, "Selected parts"),
    ("support", "Supports", (0.22, 0.48, 0.96), 0.5, "Supports and raft (see-through by default, so "
                                                    "the model shows behind them)"),
    ("flaw", "Flaw lines and labels", (1.0, 1.0, 1.0), 1.0,
     "The lines from unsupported islands, sealed cavities and suction cups to the edge of the "
     "build area, and the text of their labels"),
    ("flaw_selected", "Selected flaw lines and labels", (0.95, 0.20, 0.18), 1.0,
     "A flaw's line and label outline after its label is clicked"),
)
KEYS = tuple(k for k, *_ in VIEW_COLORS)
PREFIX = "colors/"


def default_color(key: str) -> QColor:
    _, _, rgb, a, _ = next(c for c in VIEW_COLORS if c[0] == key)
    return QColor.fromRgbF(*rgb, a)


def default_colors() -> dict[str, QColor]:
    return {k: default_color(k) for k in KEYS}


def load_colors(settings) -> dict[str, QColor]:
    out = default_colors()
    for k in KEYS:
        v = settings.value(PREFIX + k)
        c = QColor(str(v)) if v else QColor()
        if c.isValid():
            out[k] = c
    return out


def save_color(settings, key: str, c: QColor) -> None:
    if c == default_color(key):
        settings.remove(PREFIX + key)
    else:
        settings.setValue(PREFIX + key, c.name(QColor.HexArgb))


def rgba(c: QColor) -> tuple[float, float, float, float]:
    return c.redF(), c.greenF(), c.blueF(), c.alphaF()


# ------------------------------------------------------------------ the picker
class ColorWheel(QWidget):
    """Hue round the wheel, saturation from the centre out; brightness is a separate slider."""
    changed = Signal()
    SIZE = 200

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.h, self.s, self.v = 0.0, 0.0, 1.0
        self._img = None

    def set_hsv(self, h: float, s: float, v: float) -> None:
        if v != self.v:
            self._img = None
        self.h, self.s, self.v = h, s, v
        self.update()

    def _radius(self) -> float:
        return self.SIZE / 2 - 6

    def _wheel(self) -> QImage:
        if self._img is None:
            img = QImage(self.SIZE, self.SIZE, QImage.Format_ARGB32_Premultiplied)
            img.fill(Qt.transparent)
            p = QPainter(img)
            p.setRenderHint(QPainter.Antialiasing)
            c = QPointF(self.SIZE / 2, self.SIZE / 2)
            r = self._radius()
            hue = QConicalGradient(c, 0)
            for i in range(7):                       # counter-clockwise from 3 o'clock, like the angle
                hue.setColorAt(i / 6, QColor.fromHsvF((i / 6) % 1.0, 1.0, self.v))
            p.setPen(Qt.NoPen)
            p.setBrush(hue)
            p.drawEllipse(c, r, r)
            white = QRadialGradient(c, r)
            white.setColorAt(0, QColor.fromHsvF(0, 0, self.v))
            col = QColor.fromHsvF(0, 0, self.v)
            col.setAlphaF(0)
            white.setColorAt(1, col)
            p.setBrush(white)
            p.drawEllipse(c, r, r)
            p.end()
            self._img = img
        return self._img

    def paintEvent(self, _):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        p.drawImage(0, 0, self._wheel())
        r = self._radius()
        a = self.h * 2 * math.pi
        pt = QPointF(self.SIZE / 2 + math.cos(a) * self.s * r, self.SIZE / 2 - math.sin(a) * self.s * r)
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(Qt.black, 3))
        p.drawEllipse(pt, 6, 6)
        p.setPen(QPen(Qt.white, 1.5))
        p.drawEllipse(pt, 6, 6)
        p.end()

    def _pick(self, pos) -> None:
        dx, dy = pos.x() - self.SIZE / 2, self.SIZE / 2 - pos.y()
        self.h = (math.atan2(dy, dx) / (2 * math.pi)) % 1.0
        self.s = min(1.0, math.hypot(dx, dy) / self._radius())
        self.update()
        self.changed.emit()

    def mousePressEvent(self, e):
        self._pick(e.position())

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._pick(e.position())


class ColorDialog(QDialog):
    """A colour wheel with a brightness slider, R/G/B values and a hex field, all kept in step.
    The colour's transparency is set beside it on the preferences page, not here."""
    colorChanged = Signal(QColor)

    def __init__(self, color: QColor, title: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.wheel = ColorWheel()
        self.value = QSlider(Qt.Vertical)
        self.value.setRange(0, 255)
        self.value.setToolTip("Brightness")
        self.swatch = QWidget()
        self.swatch.setFixedHeight(28)
        self.swatch.setAutoFillBackground(True)
        self.rgb = []
        form = QFormLayout()
        for name in ("Red", "Green", "Blue"):
            sp = QSpinBox()
            sp.setRange(0, 255)
            self.rgb.append(sp)
            form.addRow(name, sp)
        self.hex = QLineEdit()
        self.hex.setMaxLength(7)
        self.hex.setPlaceholderText("#rrggbb")
        form.addRow("Hex", self.hex)
        top = QHBoxLayout()
        top.addWidget(self.wheel)
        top.addWidget(self.value)
        right = QVBoxLayout()
        right.addWidget(self.swatch)
        right.addLayout(form)
        right.addStretch(1)
        top.addLayout(right)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(bb)
        self._alpha = color.alphaF()
        self._busy = False
        self.wheel.changed.connect(self._from_wheel)
        self.value.valueChanged.connect(self._from_wheel)
        for sp in self.rgb:
            sp.valueChanged.connect(self._from_rgb)
        self.hex.editingFinished.connect(self._from_hex)
        self.set_color(color)

    def color(self) -> QColor:
        c = QColor(*(sp.value() for sp in self.rgb))
        c.setAlphaF(self._alpha)
        return c

    def set_color(self, c: QColor, keep_wheel: bool = False) -> None:
        self._busy = True
        try:
            for sp, x in zip(self.rgb, (c.red(), c.green(), c.blue())):
                sp.setValue(x)
            self.hex.setText(c.name(QColor.HexRgb))
            h, s, v = c.hsvHueF(), c.hsvSaturationF(), c.valueF()
            if not keep_wheel:
                self.wheel.set_hsv(self.wheel.h if h < 0 else h, s, v)
                self.value.setValue(round(v * 255))
            pal = self.swatch.palette()
            pal.setColor(self.swatch.backgroundRole(), QColor(c.red(), c.green(), c.blue()))
            self.swatch.setPalette(pal)
        finally:
            self._busy = False
        self.colorChanged.emit(self.color())

    def _from_wheel(self, *_):
        if self._busy:
            return
        v = self.value.value() / 255
        self.wheel.set_hsv(self.wheel.h, self.wheel.s, v)
        self.set_color(QColor.fromHsvF(self.wheel.h, self.wheel.s, v), keep_wheel=True)

    def _from_rgb(self, *_):
        if not self._busy:
            self.set_color(QColor(*(sp.value() for sp in self.rgb)))

    def _from_hex(self):
        c = QColor(self.hex.text().strip())
        if c.isValid():
            self.set_color(c)
        else:
            self.hex.setText(self.color().name(QColor.HexRgb))

