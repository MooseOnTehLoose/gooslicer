"""Vertical layer slider over the 3D view: hides everything above a height (09 §11)."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen
from PySide6.QtWidgets import QWidget

TICK_STEPS = (1, 2, 5, 10, 20, 50, 100)   # mm between labelled ticks
MIN_LABEL_GAP = 28                         # px between labels
PAD = 16                                   # px above the top and below the bottom stop
GROOVE_X = 80                              # px from the left edge


class LayerSlider(QWidget):
    """Heights 0 … `top` mm in `step` mm steps. At the top nothing is hidden and `cut()` is
    None; `cutChanged` carries the new cut (mm or None)."""
    cutChanged = Signal(object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("layerSlider")
        self.setFixedWidth(96)
        self.setFocusPolicy(Qt.ClickFocus)
        self.setCursor(Qt.PointingHandCursor)
        self.setToolTip("Layer view: hide everything above this height\n(drag, wheel or ↑/↓; top shows all)")
        self.step = 0.05
        self.top = 0.0
        self._n = 0          # steps from 0 to the top
        self._i = 0          # current step; == _n means "all"

    # ------------------------------------------------------------------ model
    def set_range(self, top: float, step: float) -> None:
        """Set the highest height and the step; a slider at the top stays there, otherwise
        it keeps its height (clamped)."""
        step = max(float(step), 0.001)
        at_top = self._i >= self._n
        old = self.cut()
        self.top, self.step = max(float(top), 0.0), step
        self._n = max(int(math.ceil(self.top / step - 1e-6)), 0)
        self._i = self._n if at_top or old is None else min(int(round(old / step)), self._n)
        if self.cut() != old:
            self.cutChanged.emit(self.cut())
        self.update()

    def cut(self) -> float | None:
        return None if self._i >= self._n else self._i * self.step

    def set_cut(self, mm: float | None) -> None:
        self._set_index(self._n if mm is None else int(round(float(mm) / self.step)))

    def _set_index(self, i: int) -> None:
        i = min(max(int(i), 0), self._n)
        if i != self._i:
            self._i = i
            self.cutChanged.emit(self.cut())
            self.update()

    # --------------------------------------------------------------- geometry
    def _span(self) -> tuple[float, float]:
        return PAD, max(self.height() - PAD, PAD + 1)

    def y_of(self, mm: float) -> float:
        y0, y1 = self._span()
        top = max(self._n * self.step, 1e-9)
        return y1 - (min(max(mm, 0.0), top) / top) * (y1 - y0)

    def _index_at(self, y: float) -> int:
        y0, y1 = self._span()
        f = (y1 - y) / (y1 - y0)
        return int(round(f * self._n))

    def tick_interval(self) -> float:
        y0, y1 = self._span()
        per_mm = (y1 - y0) / max(self._n * self.step, 1e-9)
        return next((s for s in TICK_STEPS if s * per_mm >= MIN_LABEL_GAP), TICK_STEPS[-1])

    # ----------------------------------------------------------------- events
    def mousePressEvent(self, e):
        if e.button() == Qt.LeftButton:
            self._set_index(self._index_at(e.position().y()))
            e.accept()

    def mouseMoveEvent(self, e):
        if e.buttons() & Qt.LeftButton:
            self._set_index(self._index_at(e.position().y()))
            e.accept()

    def wheelEvent(self, e):
        d = e.angleDelta().y()
        if d:
            n = 10 if e.modifiers() & Qt.ShiftModifier else 1
            self._set_index(self._i + (n if d > 0 else -n))
        e.accept()

    def keyPressEvent(self, e):
        k = {Qt.Key_Up: 1, Qt.Key_Down: -1, Qt.Key_PageUp: 20, Qt.Key_PageDown: -20}.get(e.key())
        if k is None:
            if e.key() in (Qt.Key_Home, Qt.Key_End):
                self._set_index(self._n if e.key() == Qt.Key_Home else 0)
                return
            super().keyPressEvent(e)
            return
        self._set_index(self._i + k)

    # ------------------------------------------------------------------ paint
    def label_text(self) -> str:
        c = self.cut()
        return "All" if c is None else f"{c:.2f} mm"

    def paintEvent(self, _e):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        y0, y1 = self._span()
        x = GROOVE_X
        f = QFont(self.font())
        f.setPointSizeF(max(f.pointSizeF() * 0.8, 7))
        p.setFont(f)
        # groove: the hidden part above the handle dim, the shown part below bright
        hy = self.y_of(self._i * self.step)
        p.setPen(QPen(QColor(255, 255, 255, 60), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x, y0), QPointF(x, hy))
        p.setPen(QPen(QColor("#4c9be8"), 4, Qt.SolidLine, Qt.RoundCap))
        p.drawLine(QPointF(x, hy), QPointF(x, y1))
        # mm scale
        if self._n:
            iv = self.tick_interval()
            p.setPen(QColor(210, 214, 220, 200))
            top = self._n * self.step
            k = 0
            while k * iv <= top + 1e-6:
                y = self.y_of(k * iv)
                p.drawLine(QPointF(x - 9, y), QPointF(x - 4, y))
                p.drawText(QRectF(0, y - 8, x - 12, 16), Qt.AlignRight | Qt.AlignVCenter, f"{k * iv:g}")
                k += 1
            p.drawText(QRectF(0, y0 - PAD - 4, x - 12, PAD), Qt.AlignRight | Qt.AlignVCenter, "mm")
        # handle and its height
        p.setPen(QPen(QColor(20, 22, 26), 1))
        p.setBrush(QColor("#e8eaee"))
        p.drawRoundedRect(QRectF(x - 9, hy - 5, 18, 10), 3, 3)
        if self.cut() is not None:
            text = self.label_text()
            w = p.fontMetrics().horizontalAdvance(text) + 8
            box = QRectF(x - 12 - w, hy - 9, w, 18)
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(40, 44, 52, 235))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor("#ffffff"))
            p.drawText(box, Qt.AlignCenter, text)
        p.end()
