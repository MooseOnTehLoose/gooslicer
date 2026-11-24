"""Docked panels: parts list and part properties (transform + mesh health)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QVBoxLayout, QWidget)

from gooslicer.mesh import MeshReport

AXES = ("X", "Y", "Z")


class InfoButton(QAbstractButton):
    """A small circular button with a lower-case "i", drawn from the current palette so it
    follows the light/dark theme."""
    SIZE = 22

    def __init__(self, tooltip: str = "", parent=None):
        super().__init__(parent)
        self.setToolTip(tooltip)
        self.setAccessibleName(tooltip or "Information")
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedSize(self.SIZE, self.SIZE)
        self.setAttribute(Qt.WA_Hover)

    def sizeHint(self) -> QSize:
        return QSize(self.SIZE, self.SIZE)

    def paintEvent(self, _e) -> None:
        pal = self.palette()
        active = self.isDown() or self.underMouse() or self.hasFocus()
        ring = pal.color(QPalette.Highlight) if active else pal.color(QPalette.ButtonText)
        text = ring
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        r = QRectF(self.rect()).adjusted(1.5, 1.5, -1.5, -1.5)
        p.setBrush(Qt.NoBrush)
        if self.isDown():
            p.setBrush(pal.color(QPalette.Highlight))
            text = pal.color(QPalette.HighlightedText)
        if not self.isEnabled():
            ring = text = pal.color(QPalette.Disabled, QPalette.ButtonText)
        p.setPen(QPen(ring, 1.5))
        p.drawEllipse(r)
        f = QFont(self.font())
        f.setFamily("serif")
        f.setBold(True)
        f.setPixelSize(14)
        p.setFont(f)
        p.setPen(text)
        p.drawText(r, Qt.AlignCenter, "i")
        p.end()


REMOVE_COLOR = "#e5484d"
REMOVE_SIZE = 16        # px: the clickable square of the red x at the right of each row


class _RemoveDelegate(QStyledItemDelegate):
    """Each row: the model's name, and a red x at the right that removes it."""

    def __init__(self, view):
        super().__init__(view)
        self.view = view

    @staticmethod
    def x_rect(row_rect) -> QRect:
        r = REMOVE_SIZE
        return QRect(row_rect.right() - r - 4, row_rect.center().y() - r // 2 + 1, r, r)

    def paint(self, painter, option, index):
        opt = QStyleOptionViewItem(option)
        opt.rect = option.rect.adjusted(0, 0, -(REMOVE_SIZE + 8), 0)   # the name stops short of the x
        super().paint(painter, opt, index)
        if option.state & QStyle.State_Selected:                      # carry the highlight under the x
            painter.fillRect(option.rect.adjusted(option.rect.width() - REMOVE_SIZE - 8, 0, 0, 0),
                             option.palette.highlight())
        r = QRectF(self.x_rect(option.rect)).adjusted(4, 4, -4, -4)
        hot = self.view.hover_row == index.row()
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing)
        painter.setPen(QPen(QColor(REMOVE_COLOR).lighter(125 if hot else 100), 2.4 if hot else 2.0,
                            Qt.SolidLine, Qt.RoundCap))
        painter.drawLine(r.topLeft(), r.bottomRight())
        painter.drawLine(r.topRight(), r.bottomLeft())
        painter.restore()

    def sizeHint(self, option, index):
        sz = super().sizeHint(option, index)
        return QSize(sz.width() + REMOVE_SIZE + 8, max(sz.height(), REMOVE_SIZE + 4))

    def editorEvent(self, event, model, option, index):
        if event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonDblClick) and \
                self.x_rect(option.rect).contains(event.position().toPoint()):
            return True                         # not a selection click
        if event.type() == QEvent.MouseButtonRelease and event.button() == Qt.LeftButton and \
                self.x_rect(option.rect).contains(event.position().toPoint()):
            self.view.removeRequested.emit(index.data(Qt.UserRole))
            return True
        return super().editorEvent(event, model, option, index)


class PartsList(QListWidget):
    selectionChangedIds = Signal(list)
    removeRequested = Signal(int)               # part id, from the red x of its row

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.hover_row = -1
        self.setMouseTracking(True)
        self.setItemDelegate(_RemoveDelegate(self))
        self.itemSelectionChanged.connect(
            lambda: self.selectionChangedIds.emit([i.data(Qt.UserRole) for i in self.selectedItems()]))

    def refresh(self, scene, selected: list[int]):
        self.blockSignals(True)
        self.clear()
        for p in scene.parts:
            warn = "" if scene.in_volume(p) else "  ⚠ outside build volume"
            it = QListWidgetItem(f"{p.name}{warn}")
            it.setData(Qt.UserRole, p.id)
            it.setToolTip(f"{p.name}{warn}\nThe red x removes it from the build plate")
            self.addItem(it)
            it.setSelected(p.id in selected)
        self.blockSignals(False)


    def x_rect(self, row: int) -> QRect:
        return _RemoveDelegate.x_rect(self.visualRect(self.model().index(row, 0)))

    def mouseMoveEvent(self, e):
        idx = self.indexAt(e.position().toPoint())
        row = idx.row() if idx.isValid() and self.x_rect(idx.row()).contains(e.position().toPoint()) else -1
        if row != self.hover_row:
            self.hover_row = row
            self.viewport().setCursor(Qt.PointingHandCursor if row >= 0 else Qt.ArrowCursor)
            self.viewport().update()
        super().mouseMoveEvent(e)

    def leaveEvent(self, e):
        if self.hover_row != -1:
            self.hover_row = -1
            self.viewport().unsetCursor()
            self.viewport().update()
        super().leaveEvent(e)


def _spin(lo, hi, dec=2, suffix=" mm", step=1.0):
    s = QDoubleSpinBox()
    s.setRange(lo, hi)
    s.setDecimals(dec)
    s.setSuffix(suffix)
    s.setSingleStep(step)
    s.setKeyboardTracking(False)
    return s


class PropertiesPanel(QWidget):
    """Edits the primary selected part through MainWindow.edit_part (undoable)."""

    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self.part = None
        lay = QVBoxLayout(self)
        self.title = QLabel("No part selected")
        self.title.setWordWrap(True)
        lay.addWidget(self.title)

        g = QGroupBox("Transform")
        grid = QGridLayout(g)
        self.pos = [_spin(-1000, 1000) for _ in AXES]
        self.size = [_spin(0.01, 2000) for _ in AXES]
        self.scale = [_spin(0.1, 10000, 1, " %", 5) for _ in AXES]
        for c, a in enumerate(AXES):
            grid.addWidget(QLabel(a), 0, c + 1, alignment=Qt.AlignHCenter)
        for r, (label, row) in enumerate((("Position", self.pos), ("Size", self.size), ("Scale", self.scale)), 1):
            grid.addWidget(QLabel(label), r, 0)
            for c, w in enumerate(row):
                w.setMinimumWidth(80)        # may shrink below its size hint: fits the right column
                grid.addWidget(w, r, c + 1)
        self.uniform = QCheckBox("Uniform scale")
        self.uniform.setChecked(True)
        grid.addWidget(self.uniform, 4, 1, 1, 3)
        lay.addWidget(g)

        g = QGroupBox("Rotate")
        rl = QGridLayout(g)
        for c, a in enumerate(AXES):
            b = QPushButton(f"{a} −45°")
            b.clicked.connect(lambda _=False, i=c: self._rotate(i, -45))
            rl.addWidget(b, 0, c)
        self.angle = _spin(-360, 360, 1, "°", 5)
        self.axis = QComboBox()
        self.axis.addItems(AXES)
        apply = QPushButton("Rotate")
        apply.clicked.connect(lambda: self._rotate(self.axis.currentIndex(), self.angle.value()))
        rl.addWidget(self.angle, 1, 0)
        rl.addWidget(self.axis, 1, 1)
        rl.addWidget(apply, 1, 2)
        lay.addWidget(g)

        row = QHBoxLayout()
        self.btn_flat = QPushButton("Lay flat…")
        self.btn_flat.setToolTip("Click a face in the 3D view to place it on the plate")
        self.btn_drop = QPushButton("Drop to plate")
        self.btn_center = QPushButton("Center")
        self.btn_reset = QPushButton("Reset")
        self.btn_reset.setToolTip("Reset rotation and scale")
        for b in (self.btn_flat, self.btn_drop, self.btn_center, self.btn_reset):
            row.addWidget(b)
        lay.addLayout(row)

        g = QGroupBox("Mesh")
        ml = QFormLayout(g)
        self.mesh_info = QLabel("–")
        self.mesh_info.setWordWrap(True)
        self.mesh_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_repair = QPushButton("Repair")
        ml.addRow(self.mesh_info)
        ml.addRow(self.btn_repair)
        lay.addWidget(g)
        lay.addStretch(1)

        for i, s in enumerate(self.pos):
            s.valueChanged.connect(lambda v, i=i: self._set_pos(i, v))
        for i, s in enumerate(self.size):
            s.valueChanged.connect(lambda v, i=i: self._set_size(i, v))
        for i, s in enumerate(self.scale):
            s.valueChanged.connect(lambda v, i=i: self._set_scale(i, v))
        self.btn_flat.clicked.connect(win.start_lay_flat)
        self.btn_drop.clicked.connect(lambda: self._edit("Drop to plate", lambda p: win.scene.drop_to_plate(p)))
        self.btn_center.clicked.connect(lambda: self._edit("Center", self._center))
        self.btn_reset.clicked.connect(lambda: self._edit("Reset transform", self._reset))
        self.btn_repair.clicked.connect(win.repair_selected)
        self.set_part(None, None)

    # ---------------------------------------------------------------- view
    def set_part(self, part, report: MeshReport | None):
        self.part = part
        enabled = part is not None
        for t in (QDoubleSpinBox, QPushButton, QCheckBox, QComboBox):
            for w in self.findChildren(t):
                w.setEnabled(enabled)
        if not enabled:
            self.title.setText("No part selected")
            self.mesh_info.setText("–")
            return
        self.title.setText(f"<b>{part.name}</b>")
        lo, hi = part.bounds()
        vals = ((self.pos, part.position), (self.size, hi - lo), (self.scale, part.scale * 100))
        for widgets, v in vals:
            for w, x in zip(widgets, v):
                w.blockSignals(True)
                w.setValue(float(x))
                w.blockSignals(False)
        self.set_report(report)

    def set_report(self, r: MeshReport | None):
        if r is None:
            self.mesh_info.setText("Analysing…")
            self.btn_repair.setEnabled(False)
            return
        vol = f"{r.volume:,.1f} mm³" if r.volume is not None else "n/a (not closed)"
        status = "✔ printable mesh" if r.ok else "⚠ " + "; ".join(r.problems())
        self.mesh_info.setText(f"{r.triangles:,} triangles · {r.components} shell(s)<br>"
                               f"Volume: {vol}<br>{status}")
        self.btn_repair.setEnabled(self.part is not None and not r.ok)

    # ---------------------------------------------------------------- edits
    def _edit(self, text, fn):
        if self.part is not None:
            self.win.edit_parts([self.part.id], text, fn)

    def _set_pos(self, i, v):
        def f(p):
            p.position[i] = v
        self._edit(f"Move {AXES[i]}", f)

    def _local_axis(self, p, i):
        return int(np.argmax(np.abs(p.rotation[i, :])))

    def _set_size(self, i, v):
        def f(p):
            cur = p.size()[i]
            if cur <= 0:
                return
            k = v / cur
            s = p.scale * k if self.uniform.isChecked() else p.scale.copy()
            if not self.uniform.isChecked():
                s[self._local_axis(p, i)] *= k
            self.win.scene.set_scale(p, s)
        self._edit("Resize", f)

    def _set_scale(self, i, v):
        def f(p):
            s = np.full(3, v / 100) if self.uniform.isChecked() else p.scale.copy()
            if not self.uniform.isChecked():
                s[i] = v / 100
            self.win.scene.set_scale(p, s)
        self._edit("Scale", f)

    def _rotate(self, i, deg):
        axis = np.eye(3)[i]
        self._edit(f"Rotate {AXES[i]} {deg:g}°", lambda p: self.win.scene.rotate(p, axis, deg))

    def _center(self, p):
        lo, hi = p.bounds()
        p.position[:2] -= ((lo + hi) / 2)[:2]

    def _reset(self, p):
        p.rotation = np.eye(3)
        p.scale = np.ones(3)
        self.win.scene.drop_to_plate(p)
