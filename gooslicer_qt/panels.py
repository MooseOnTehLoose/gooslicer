"""Docked panels: parts list and part properties (transform + mesh health)."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, QRect, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPalette, QPen
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDoubleSpinBox, QFormLayout,
                               QGridLayout, QGroupBox, QHBoxLayout, QLabel, QListWidget,
                               QListWidgetItem, QPushButton, QSizePolicy, QSlider, QStyle, QStyledItemDelegate,
                               QStyleOptionViewItem, QToolButton, QVBoxLayout, QWidget)

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


def show_popup(popup: QWidget, button: QWidget) -> None:
    """Open a details pop-up under an (i) button, right-aligned to it (above it if there's no
    room below)."""
    popup.adjustSize()
    anchor = button.mapToGlobal(button.rect().bottomRight())
    x, y = anchor.x() - popup.width() + 1, anchor.y() + 4
    scr = button.screen().availableGeometry() if button.screen() else None
    if scr is not None:
        x = max(scr.left(), min(x, scr.right() - popup.width()))
        if y + popup.height() > scr.bottom():
            y = button.mapToGlobal(button.rect().topLeft()).y() - popup.height() - 4
    popup.move(x, y)
    popup.show()


def disclosure_button(text: str, tooltip: str = "") -> QToolButton:
    """A full-width "▸ text" toggle that shows or hides a section (collapsed to start with)."""
    b = QToolButton()
    b.setText(text)
    b.setCheckable(True)
    b.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
    b.setArrowType(Qt.RightArrow)
    b.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    if tooltip:
        b.setToolTip(tooltip)
    return b


REMOVE_COLOR = "#e5484d"
REMOVE_SIZE = 16        # px: the clickable square of the red x at the right of each row


class _RemoveDelegate(QStyledItemDelegate):
    """Each row: its text, and a red x at the right that removes it."""

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


class _RemovableList(QListWidget):
    """A list whose rows end in a red x (emits removeRequested with the row's UserRole data)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.hover_row = -1
        self.setMouseTracking(True)
        self.setItemDelegate(_RemoveDelegate(self))

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


class PartsList(_RemovableList):
    selectionChangedIds = Signal(list)
    removeRequested = Signal(int)               # part id, from the red x of its row

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QListWidget.ExtendedSelection)
        self.itemSelectionChanged.connect(
            lambda: self.selectionChangedIds.emit([i.data(Qt.UserRole) for i in self.selectedItems()]))

    def refresh(self, scene, selected: list[int]):
        self.blockSignals(True)
        self.clear()
        for p in scene.parts:
            warn = "" if scene.in_volume(p) else "  ⚠ outside build volume"
            fixed = " (repaired)" if p.repaired else ""
            it = QListWidgetItem(f"{p.name}{fixed}{warn}")
            it.setData(Qt.UserRole, p.id)
            it.setToolTip(f"{p.name}{warn}"
                          + ("\nIts mesh was repaired (holes, flipped or stray triangles fixed)" if fixed else "")
                          + "\nThe red x removes it from the build plate")
            self.addItem(it)
            it.setSelected(p.id in selected)
        self.blockSignals(False)


class FlawsList(_RemovableList):
    """Every detected flaw. Clicking a row selects it (its guide line turns red in the 3D view,
    as clicking its label does) and clicking again deselects; the red x dismisses it."""
    selectionChangedKeys = Signal(object)       # set of (kind, number)
    removeRequested = Signal(object)            # (kind, number), from the red x of its row

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setSelectionMode(QListWidget.MultiSelection)       # a click toggles the row
        # rows fit the column (the red x must stay in view): no sideways scrolling, long text cut
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setTextElideMode(Qt.ElideRight)
        self.itemSelectionChanged.connect(
            lambda: self.selectionChangedKeys.emit({i.data(Qt.UserRole) for i in self.selectedItems()}))

    def refresh(self, flaws, selected) -> None:
        self.blockSignals(True)
        self.clear()
        for f in flaws:
            it = QListWidgetItem(f"{f.title}\n{f.detail}")
            it.setData(Qt.UserRole, f.key)
            it.setToolTip(f"{f.title}: {f.detail}\nClick to highlight its guide line in red; "
                          "the red x dismisses it as a false positive")
            self.addItem(it)
            it.setSelected(f.key in selected)
        self.blockSignals(False)

    def set_selected(self, keys) -> None:
        self.blockSignals(True)
        for i in range(self.count()):
            it = self.item(i)
            it.setSelected(it.data(Qt.UserRole) in keys)
        self.blockSignals(False)


class FlawsPanel(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.list = FlawsList()
        self.empty = QLabel("No flaws found. Use Detect islands (Supports) or Check for voids "
                            "(Hollowing) in the Models tab, or slice.")
        self.empty.setWordWrap(True)
        self.hint = QLabel("Click a flaw to highlight it in red. The red × dismisses a false "
                           "positive until a part changes.")
        self.hint.setWordWrap(True)
        lay = QVBoxLayout(self)
        lay.addWidget(self.empty)
        lay.addWidget(self.hint)
        lay.addWidget(self.list, 1)

    def refresh(self, flaws, selected) -> None:
        self.list.refresh(flaws, selected)
        self.empty.setVisible(not flaws)
        self.hint.setVisible(bool(flaws))


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
        tl = QVBoxLayout(g)
        # position, size and scale are tucked away behind a toggle (collapsed), like Advanced settings
        self.transform_btn = disclosure_button("Position, size and scale",
                                               "Exact position, size and scale of the selected part")
        self.transform_btn.toggled.connect(self.set_transform_visible)
        tl.addWidget(self.transform_btn)
        self.transform = QWidget()
        tl.addWidget(self.transform)
        grid = QGridLayout(self.transform)
        grid.setContentsMargins(0, 0, 0, 0)
        self.pos = [_spin(-1000, 1000) for _ in AXES]
        self.pos[2].setToolTip("Height of the part's centre. Without supports, raising it lifts the part "
                               "off the plate; it can't go below the plate.")
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
        self.transform.hide()
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
        self.btn_auto = QPushButton("Auto Position")
        self.btn_auto.setToolTip("Turn the selected part to the angle that needs the fewest supports "
                                 "(and has no large flat areas facing the plate)")
        self.btn_arrange = QPushButton("Arrange All")
        self.btn_arrange.setToolTip("Lay out all parts side by side on the plate (Edit → Arrange all, A)")
        auto_row = QHBoxLayout()
        auto_row.addWidget(self.btn_auto)
        auto_row.addWidget(self.btn_arrange)
        lay.addLayout(auto_row)

        g = QGroupBox("Mesh")
        gl = QVBoxLayout(g)
        # folded away like Position, size and scale; the button carries the check's verdict
        self.mesh_btn = disclosure_button("Mesh", "The mesh check (holes, flipped or stray triangles) and Repair")
        self.mesh_btn.toggled.connect(self.set_mesh_visible)
        gl.addWidget(self.mesh_btn)
        self.mesh_box = QWidget()
        gl.addWidget(self.mesh_box)
        ml = QFormLayout(self.mesh_box)
        ml.setContentsMargins(0, 0, 0, 0)
        self.mesh_info = QLabel("–")
        self.mesh_info.setWordWrap(True)
        self.mesh_info.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.btn_repair = QPushButton("Repair")
        ml.addRow(self.mesh_info)
        ml.addRow(self.btn_repair)
        self.mesh_box.setVisible(False)
        lay.addWidget(g)

        # Modifying (11): select part of the surface by curvature, cut it off as its own part
        g = QGroupBox("Modifying")
        g.setObjectName("modifyingBox")
        fl = QFormLayout(g)
        self.select_btn = QPushButton("Select surface")
        self.select_btn.setCheckable(True)
        self.select_btn.setToolTip("Click a part's surface to select the area around the click, up to "
                                   "where it curves away; Ctrl+click adds another area. Esc stops.")
        self.clear_region_btn = QPushButton("Clear")
        self.clear_region_btn.setToolTip("Clear the selected surface")
        row = QHBoxLayout()
        fl.addRow(row)                      # attached before it gets widgets (PySide ownership)
        row.addWidget(self.select_btn, 1)
        row.addWidget(self.clear_region_btn)
        self._modify_form = fl
        self.select_by = QComboBox()
        self.select_by.setToolTip("What a click selects: the surface round it (spread and contours), "
                                  "the whole part of the model it's on, or what an image model sees "
                                  "there in the 3D view")
        for text, key in (("Surface", "surface"), ("Parts", "parts"), ("Picture (AI)", "picture")):
            self.select_by.addItem(text, key)
        from gooslicer import picture
        ok, why = picture.available()
        if not ok:
            item = self.select_by.model().item(2)
            item.setEnabled(False)
            item.setToolTip("Not available: " + why)
        fl.addRow("Select by", self.select_by)
        self.spread = QSlider(Qt.Horizontal)
        self.spread.setRange(5, 90)
        self.spread.setValue(35)
        self.spread.setToolTip("How far the selection spreads: surface turned less than this from "
                               "where you clicked is selected")
        self.spread_label = QLabel("35°")
        self.spread_label.setMinimumWidth(32)
        srow = QHBoxLayout()
        fl.addRow("Spread", srow)
        srow.addWidget(self.spread, 1)
        srow.addWidget(self.spread_label)
        self.stop_contours = QCheckBox("Stop at contours")
        self.stop_contours.setChecked(True)
        self.stop_contours.setToolTip("Don't let the selection spread across a contour: a groove, a "
                                      "strap's edge or a hairline (drawn on the part while selecting)")
        self.contour_angle = QSlider(Qt.Horizontal)
        self.contour_angle.setRange(10, 90)
        self.contour_angle.setValue(35)
        self.contour_angle.setToolTip("How sharply the surface must fold (within about a millimetre) "
                                      "to count as a contour: lower finds more, fainter contours")
        self.contour_label = QLabel("35°")
        self.contour_label.setMinimumWidth(32)
        crow = QHBoxLayout()
        fl.addRow(crow)
        crow.addWidget(self.stop_contours)
        crow.addWidget(self.contour_angle, 1)
        crow.addWidget(self.contour_label)
        self.part_edges = QSlider(Qt.Horizontal)
        self.part_edges.setRange(2, 30)
        self.part_edges.setValue(10)
        self.part_edges.setToolTip("How deep a valley must run along a border for it to stay a part "
                                   "edge: lower gives more, smaller parts")
        self.part_edges_label = QLabel("10°")
        self.part_edges_label.setMinimumWidth(32)
        prow = QHBoxLayout()
        fl.addRow("Part edges", prow)
        prow.addWidget(self.part_edges, 1)
        prow.addWidget(self.part_edges_label)
        self.outline_size = QSlider(Qt.Horizontal)
        self.outline_size.setRange(0, 2)
        self.outline_size.setValue(1)
        self.outline_size.setPageStep(1)
        self.outline_size.setToolTip("Which of the image model's outlines to use, from the smallest "
                                     "thing round the click to the largest")
        self.outline_label = QLabel("Medium")
        self.outline_label.setMinimumWidth(52)
        orow = QHBoxLayout()
        fl.addRow("Outline", orow)
        orow.addWidget(self.outline_size, 1)
        orow.addWidget(self.outline_label)
        self._mode_rows = {"surface": (srow, crow), "parts": (prow,), "picture": (orow,)}
        self.region_info = QLabel("Nothing selected")
        self.region_info.setWordWrap(True)
        fl.addRow(self.region_info)
        self.cut_depth = _spin(0.2, 100, 2, " mm", 0.5)
        self.cut_depth.setValue(3.0)
        self.cut_depth.setToolTip("How far the cut reaches below the selection's lowest edge. On a "
                                  "hollowed part, more than its wall thickness opens the cavity.")
        fl.addRow("Cut depth", self.cut_depth)
        self.cut_smooth = _spin(0, 5, 2, " mm", 0.1)
        self.cut_smooth.setValue(0.5)
        self.cut_smooth.setToolTip("Round off the outline's small notches and spikes (0: follow every "
                                   "triangle edge)")
        fl.addRow("Smoothing", self.cut_smooth)
        self.cut_gap = _spin(0, 1, 2, " mm", 0.05)
        self.cut_gap.setValue(0.1)
        self.cut_gap.setToolTip("Room left between the piece and the part all round its cut, for "
                                "glue (0: an exact fit)")
        fl.addRow("Fit gap", self.cut_gap)
        self.lay_cut = QCheckBox("Lay cut side on the plate")
        self.lay_cut.setChecked(True)
        self.lay_cut.setToolTip("Turn the new piece so its cut side faces the plate, beside the part")
        fl.addRow(self.lay_cut)
        self.grow_btn = QPushButton("Grow selection")
        self.grow_btn.setToolTip("Grow the selection by this distance along the surface (negative "
                                 "shrinks it)")
        self.grow_mm = _spin(-20, 20, 2, " mm", 0.5)
        self.grow_mm.setValue(1.0)
        self.grow_mm.setToolTip("How far Grow selection grows the selection (negative shrinks)")
        grow_row = QHBoxLayout()
        fl.addRow(grow_row)
        grow_row.addWidget(self.grow_btn, 1)
        grow_row.addWidget(self.grow_mm)
        self.cut_btn = QPushButton("Cut out selection")
        self.cut_btn.setToolTip("Cut the selection off along its outline (shown in the 3D view) as a "
                                "new part, to print separately and glue back on. The cut follows "
                                "the outline's curves, so the piece fits back only one way.")
        fl.addRow(self.cut_btn)
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
        self.select_btn.toggled.connect(win.set_region_mode)
        self.clear_region_btn.clicked.connect(win.clear_region)
        self.spread.valueChanged.connect(lambda v: (self.spread_label.setText(f"{v}°"), win.region_settings_changed()))
        for w in (self.cut_depth, self.cut_smooth, self.cut_gap):
            w.valueChanged.connect(lambda _v: win.region_settings_changed())
        self.stop_contours.toggled.connect(lambda _on: win.region_settings_changed())
        self.contour_angle.valueChanged.connect(
            lambda v: (self.contour_label.setText(f"{v}°"), win.region_settings_changed()))
        self.select_by.currentIndexChanged.connect(lambda _i: (self.show_select_by(), win.select_by_changed()))
        self.part_edges.valueChanged.connect(
            lambda v: (self.part_edges_label.setText(f"{v}°"), win.region_settings_changed()))
        self.outline_size.valueChanged.connect(lambda v: (self.outline_label.setText(
            ("Small", "Medium", "Large")[v]), win.region_settings_changed()))
        self.grow_btn.clicked.connect(win.grow_selection)
        self.grow_mm.valueChanged.connect(lambda v: win.settings.setValue("modify/grow", float(v)))
        self.lay_cut.toggled.connect(lambda on: win.settings.setValue("modify/layFlat", bool(on)))
        self.cut_btn.clicked.connect(win.cut_selection)
        self.show_select_by()
        self.btn_auto.clicked.connect(win.auto_position)
        self.btn_arrange.clicked.connect(win.arrange_all)
        self.set_part(None, None)

    def set_mesh_visible(self, on: bool) -> None:
        self.mesh_box.setVisible(on)
        self.mesh_btn.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        if self.mesh_btn.isChecked() != on:
            self.mesh_btn.setChecked(on)

    def set_transform_visible(self, on: bool) -> None:
        self.transform.setVisible(on)
        self.transform_btn.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        if self.transform_btn.isChecked() != on:
            self.transform_btn.setChecked(on)

    # ---------------------------------------------------------------- view
    def show_select_by(self) -> None:
        """Show the controls of the Select by mode only."""
        key = self.select_by.currentData()
        for mode, rows in self._mode_rows.items():
            for r in rows:
                self._modify_form.setRowVisible(r, mode == key)

    def set_part(self, part, report: MeshReport | None):
        self.part = part
        enabled = part is not None
        for t in (QDoubleSpinBox, QPushButton, QCheckBox, QComboBox):
            for w in self.findChildren(t):
                w.setEnabled(enabled)
        if getattr(self.win, "_orienting", False):
            self.btn_auto.setEnabled(False)             # one Auto Position at a time
        self.btn_arrange.setEnabled(bool(self.win.scene.parts))    # works on all parts, selected or not
        self.spread.setEnabled(enabled)
        if not enabled:
            self.title.setText("No part selected")
            self.mesh_info.setText("–")
            self.mesh_btn.setText("Mesh")
            self.win.update_region_panel()
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
        self.win.update_region_panel()

    def set_report(self, r: MeshReport | None):
        if r is None:
            self.mesh_info.setText("Analysing…")
            self.mesh_btn.setText("Mesh: checking…")
            self.btn_repair.setEnabled(False)
            return
        n = len(r.problems())
        self.mesh_btn.setText("Mesh: OK" if r.ok else f"Mesh: ⚠ {max(n, 1)} problem{'s' if n != 1 else ''}")
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
            if i == 2 and p.supports is None:
                # Z of a bare part is its lift off the plate (never below it)
                p.elevation = max(0.0, p.elevation + v - p.position[2])
                self.win.scene.drop_to_plate(p)
            else:
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


class ToolWindow(QWidget):
    """A panel in its own top-level window (View → Logs, View → Layers): it stays out of the
    main window and remembers where it was."""
    closed = Signal()                     # the user closed the window

    def __init__(self, title: str, widget: QWidget, settings, key: str, parent):
        super().__init__(parent, Qt.Window)
        self.setWindowTitle(f"{title} — GooSlicer")
        self.setObjectName(f"window{title}")
        self._settings, self._key = settings, f"{key}Geometry"
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(widget)
        self.resize(900, 460)
        if settings.value(self._key):
            self.restoreGeometry(settings.value(self._key))

    def popup(self) -> None:
        self.show()
        self.raise_()
        self.activateWindow()

    def closeEvent(self, e):
        self.closed.emit()
        super().closeEvent(e)

    def hideEvent(self, e):
        self._settings.setValue(self._key, self.saveGeometry())
        super().hideEvent(e)
