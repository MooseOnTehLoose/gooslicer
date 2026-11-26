"""Hollow dialog, drain-hole settings and helpers that apply them to a scene Part."""
from __future__ import annotations

import numpy as np
import trimesh
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QPainter, QPalette, QPen
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QSpinBox,
                               QVBoxLayout, QWidget)

from gooslicer import hollow as H
from gooslicer import jobs
from gooslicer.support_defaults import (CLEARANCE_SIDES, HOLE_FORMS, HOLLOW_TYPES, INFILL_TYPES, DrainHoleSettings,
                                        HollowSettings)

TYPE_LABELS = ("Inside", "Outside", "Center (self-supporting)", "Open bottom")
INFILL_LABELS = ("BCC", "FCC")
DIRECTIONS = (("Perpendicular to surface", "normal"), ("Vertical (from below)", "vertical"),
              ("Custom", "custom"))
HOLE_FORM_LABELS = ("Hole (through the wall)", "Slot (fixed depth)")
CLEARANCE_LABELS = ("Pin smaller", "Hole larger", "Both, half each")    # clearanceType 1, 2, 3


DEFAULT_OPACITY = 0.85          # hollowed parts are drawn at 85 % opacity [GOOSLICER]


def see_through_opacity(settings) -> float:
    """The Hollow panel's see-through opacity (0.05–1), saved as hollow/opacity in percent."""
    try:
        v = float(settings.value("hollow/opacity", DEFAULT_OPACITY * 100))
    except (TypeError, ValueError):
        v = DEFAULT_OPACITY * 100
    return min(max(v, 5.0), 100.0) / 100.0


def _dspin(lo, hi, val, dec=2, suffix=" mm", step=0.1):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSuffix(suffix)
    w.setSingleStep(step)
    w.setValue(val)
    return w


class HollowFields(QWidget):
    """The Hollow settings (06 §2) as a form; used by the Hollow dialog and Preferences."""
    changed = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.qs = settings
        d = HollowSettings()
        self.thickness = _dspin(0.2, 20, d.thickness)
        self.accuracy = _dspin(0.05, 5, d.accuracy, step=0.05)
        self.accuracy.setToolTip("Voxel size of the inner wall. Smaller is more precise and slower.")
        self.kind = QComboBox()
        self.kind.addItems(TYPE_LABELS)
        self.angle = _dspin(5, 85, d.surface_angle, 0, "°", 1)
        self.angle.setToolTip("Center (self-supporting): pillars hold up any cavity ceiling flatter than "
                              "this angle from horizontal, so the inside prints without supports.")
        self.filter = QCheckBox("Keep only the largest cavities")
        self.cavities = QSpinBox()
        self.cavities.setRange(1, 9999)
        self.filter.toggled.connect(self.cavities.setEnabled)
        self.infill = QCheckBox("Infill structure")
        self.infill.setToolTip("Fill the cavity with a lattice of struts that brace the walls (SatelLite's "
                               "infill). Not for Open bottom.")
        self.infill_type = QComboBox()
        self.infill_type.addItems(INFILL_LABELS)
        self.infill_type.setToolTip("BCC: struts from each cell's centre to its corners. FCC: struts "
                                    "along the diagonals of each cell's faces.")
        self.infill_size = _dspin(1, 100, d.infill_size, step=1)
        self.infill_size.setToolTip("Lattice cell size")
        self.infill_radius = _dspin(0.1, 10, d.infill_radius, step=0.1)
        self.infill_radius.setToolTip("Strut radius")
        self.opacity = QSpinBox()
        self.opacity.setRange(5, 100)
        self.opacity.setSuffix(" %")
        self.opacity.setSingleStep(5)
        self.opacity.setValue(round(DEFAULT_OPACITY * 100))
        self.opacity.setToolTip("How solid hollowed parts look in the 3D view afterwards, so the cavity "
                                "shows through (100 % = solid). Only the view: printing is not affected.")
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        f.addRow("Wall thickness", self.thickness)
        f.addRow("Accuracy", self.accuracy)
        f.addRow("Hollow type", self.kind)
        f.addRow("Surface angle", self.angle)
        f.addRow(self.filter)
        f.addRow("Maximum cavities", self.cavities)
        f.addRow(self.infill)
        f.addRow("Infill type", self.infill_type)
        f.addRow("Cell size", self.infill_size)
        f.addRow("Strut radius", self.infill_radius)
        f.addRow("Opacity", self.opacity)
        self.load()
        for w in (self.thickness, self.accuracy, self.angle, self.cavities, self.infill_size, self.infill_radius,
                  self.opacity):
            w.valueChanged.connect(self.changed)
        self.kind.currentIndexChanged.connect(self.changed)
        self.infill_type.currentIndexChanged.connect(self.changed)
        self.filter.toggled.connect(self.changed)
        self.infill.toggled.connect(self.changed)
        self.infill.toggled.connect(self._infill_enabled)
        self.kind.currentIndexChanged.connect(self._infill_enabled)
        self._infill_enabled()

    def _infill_enabled(self, *_):
        """SatelLite offers infill for every type but Open bottom, and the surface angle only
        for Center."""
        kind = HOLLOW_TYPES[self.kind.currentIndex()]
        self.angle.setEnabled(kind == "center")
        allowed = kind != "open_bottom"
        self.infill.setEnabled(allowed)
        for w in (self.infill_type, self.infill_size, self.infill_radius):
            w.setEnabled(allowed and self.infill.isChecked())

    def load(self) -> None:
        """Show the saved values (SatelLite's defaults where nothing is saved)."""
        d = HollowSettings()
        v = lambda k, dflt, typ=float: typ(self.qs.value(f"hollow/{k}", dflt))  # noqa: E731
        kind = self.qs.value("hollow/type", d.hollow_type)
        self.set_value(HollowSettings(
            thickness=v("thickness", d.thickness), accuracy=v("accuracy", d.accuracy),
            hollow_type=kind if kind in HOLLOW_TYPES else d.hollow_type,
            surface_angle=v("surfaceAngle", d.surface_angle),
            filter_small_shells=str(self.qs.value("hollow/isFilter", "false")).lower() == "true",
            max_cavities=v("cavities", d.max_cavities, int),
            infill=str(self.qs.value("hollow/infill", "false")).lower() == "true",
            infill_type=self.qs.value("hollow/infillType", d.infill_type)
            if self.qs.value("hollow/infillType", d.infill_type) in INFILL_TYPES else d.infill_type,
            infill_size=v("infillSize", d.infill_size), infill_radius=v("infillRadius", d.infill_radius)))
        self.set_view_opacity(see_through_opacity(self.qs))

    def set_view_opacity(self, v: float) -> None:
        self.opacity.blockSignals(True)
        self.opacity.setValue(round(v * 100))
        self.opacity.blockSignals(False)

    def view_opacity(self) -> float:
        """Opacity of hollowed parts in the 3D view (0.05–1)."""
        return self.opacity.value() / 100.0

    def set_value(self, s: HollowSettings) -> None:
        self.blockSignals(True)
        self.thickness.setValue(s.thickness)
        self.accuracy.setValue(s.accuracy)
        self.kind.setCurrentIndex(HOLLOW_TYPES.index(s.hollow_type))
        self.angle.setValue(s.surface_angle)
        self.filter.setChecked(s.filter_small_shells)
        self.cavities.setValue(s.max_cavities)
        self.cavities.setEnabled(s.filter_small_shells)
        self.infill.setChecked(s.infill)
        self.infill_type.setCurrentIndex(INFILL_TYPES.index(s.infill_type))
        self.infill_size.setValue(s.infill_size)
        self.infill_radius.setValue(s.infill_radius)
        self._infill_enabled()
        self.blockSignals(False)

    def value(self) -> HollowSettings:
        return HollowSettings(thickness=self.thickness.value(), accuracy=self.accuracy.value(),
                              hollow_type=HOLLOW_TYPES[self.kind.currentIndex()],
                              surface_angle=self.angle.value(),
                              filter_small_shells=self.filter.isChecked(),
                              max_cavities=self.cavities.value(),
                              infill=self.infill.isChecked() and self.infill.isEnabled(),
                              infill_type=INFILL_TYPES[self.infill_type.currentIndex()],
                              infill_size=self.infill_size.value(), infill_radius=self.infill_radius.value())

    def save(self):
        s = self.value()
        for k, val in (("thickness", s.thickness), ("accuracy", s.accuracy), ("type", s.hollow_type),
                       ("surfaceAngle", s.surface_angle), ("isFilter", s.filter_small_shells),
                       ("cavities", s.max_cavities), ("infill", self.infill.isChecked()),
                       ("infillType", s.infill_type), ("infillSize", s.infill_size),
                       ("infillRadius", s.infill_radius), ("opacity", self.opacity.value())):
            self.qs.setValue(f"hollow/{k}", val)


class LockButton(QAbstractButton):
    """A padlock that links two values: closed (checked) keeps them equal, open lets them differ.
    Drawn from the palette, so it follows the light/dark theme."""

    def __init__(self, what: str, parent=None):
        super().__init__(parent)
        self.what = what
        self.setCheckable(True)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setFixedSize(QSize(24, 30))
        self.setAttribute(Qt.WA_Hover)
        self.toggled.connect(self._tip)
        self._tip(self.isChecked())

    def _tip(self, on: bool) -> None:
        self.setToolTip(f"{self.what} are linked: changing one sets the other. Click to set them separately."
                        if on else f"{self.what} are set separately. Click to link them.")
        self.setAccessibleName(f"Link {self.what.lower()}")
        self.update()

    def sizeHint(self) -> QSize:
        return QSize(24, 30)

    def paintEvent(self, _e) -> None:
        pal = self.palette()
        hot = self.underMouse() or self.hasFocus()
        col = pal.color(QPalette.Highlight) if (hot or self.isChecked()) else pal.color(QPalette.ButtonText)
        if not self.isEnabled():
            col = pal.color(QPalette.Disabled, QPalette.ButtonText)
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        body = QRectF(w / 2 - 7, h / 2 - 1, 14, 11)
        p.setPen(QPen(col, 2.0))
        p.setBrush(Qt.NoBrush)
        # the shackle: closed into the body, or swung open to the right
        sh = QRectF(w / 2 - 4.5, h / 2 - 10, 9, 14)
        if not self.isChecked():
            sh.translate(5, -3)
        p.drawArc(sh, 0, 180 * 16)
        p.drawLine(QPointF(sh.right(), sh.center().y()), QPointF(sh.right(), body.top()))
        if self.isChecked():
            p.drawLine(QPointF(sh.left(), sh.center().y()), QPointF(sh.left(), body.top()))
        else:
            p.drawLine(QPointF(sh.left(), sh.center().y()), QPointF(sh.left(), sh.center().y() + 2))
        p.setPen(Qt.NoPen)
        p.setBrush(col)
        p.drawRoundedRect(body, 2, 2)
        p.setBrush(pal.color(QPalette.Window))
        p.drawEllipse(QPointF(body.center().x(), body.center().y() - 0.5), 1.6, 1.6)
        p.end()


class DrainHoleFields(QWidget):
    """Drain-hole settings (SatelLite's Perforation tool, 06 §3 / 07 §3). A padlock beside each
    pair (outer/inner radius, extend outside/inside) links them: while it's closed, changing one
    sets the other to the same value. The less used settings (edges, taper angle, notch, hole
    pin) sit under "More options"."""
    changed = Signal()
    resized = Signal()          # "More options" opened or closed
    LINKS = (("linkRadius", "Outer and inner radius"), ("linkDepth", "Extend outside and inside"))

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.qs = settings
        d = DrainHoleSettings()
        # layouts are attached before anything is added to them: a widget added to a loose layout
        # stays owned by Python, and PySide deletes it a second time when the app shuts down
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        g = QGridLayout()
        lay.addLayout(g)
        g.setContentsMargins(0, 0, 0, 0)
        g.setColumnStretch(1, 1)
        self.form = QComboBox()
        self.form.addItems(HOLE_FORM_LABELS)
        self.form.setToolTip("Hole: from the clicked point through the wall to the next surface. "
                             "Slot: a blind hole of a set depth.")
        self.depth = _dspin(0.1, 100, d.depth)
        self.depth.setToolTip("How deep a slot goes")
        self.r1 = _dspin(0.2, 20, d.radius1)
        self.r1.setToolTip("Radius at the clicked surface")
        self.r2 = _dspin(0.2, 20, d.radius2)
        self.r2.setToolTip("Radius at the far end (the inner wall, or the bottom of a slot). The taper "
                           "carries on into the extensions.")
        self.e1 = _dspin(0, 50, d.extend1)
        self.e1.setToolTip("How far the cut starts outside the clicked surface")
        self.e2 = _dspin(0, 50, d.extend2)
        self.e2.setToolTip("How far the cut runs on past the far wall (not for slots)")
        self.direction = QComboBox()
        for label, key in DIRECTIONS:
            self.direction.addItem(label, key)
        self.dir_xyz = [_dspin(-1, 1, v, 2, "", 0.1) for v in d.direction]
        for w, ax in zip(self.dir_xyz, "XYZ"):
            w.setToolTip(f"Custom drill direction, {ax} (world). It is turned to point into the part.")
        self.radius_lock = LockButton(self.LINKS[0][1])
        self.depth_lock = LockButton(self.LINKS[1][1])
        rows = (("Form", self.form), ("Slot depth", self.depth), ("Outer radius", self.r1),
                ("Inner radius", self.r2), ("Extend outside", self.e1), ("Extend inside", self.e2),
                ("Direction", self.direction))
        for row, (label, w) in enumerate(rows):
            g.addWidget(QLabel(label), row, 0)
            g.addWidget(w, row, 1)
        g.addWidget(QLabel("Custom X Y Z"), len(rows), 0)
        xyz = QHBoxLayout()
        g.addLayout(xyz, len(rows), 1)
        xyz.setContentsMargins(0, 0, 0, 0)
        for w in self.dir_xyz:
            xyz.addWidget(w)
        # each padlock to the right of its pair, centred between the two rows
        g.addWidget(self.radius_lock, 2, 2, 2, 1, Qt.AlignCenter)
        g.addWidget(self.depth_lock, 4, 2, 2, 1, Qt.AlignCenter)

        # ---- more options
        self.edges = QSpinBox()
        self.edges.setRange(3, 50)
        self.edges.setToolTip("Sides of the hole's polygon (SatelLite's edge number)")
        self.use_angle = QCheckBox("Taper by angle")
        self.use_angle.setToolTip("Narrow the hole going in by this angle instead of to the inner radius "
                                  "(holes only)")
        self.angle = _dspin(0, 60, d.angle_slot, 1, "°", 1)
        self.notch = QCheckBox("Notch")
        self.notch.setToolTip("A key on the side of the hole, so a pin goes back in one way only")
        self.notch_w = _dspin(0.05, 20, d.notch_width)
        self.notch_w.setToolTip("Notch width")
        self.notch_h = _dspin(0.05, 20, d.notch_height)
        self.notch_h.setToolTip("How far the notch reaches past the hole's edge")
        self.notch_a = _dspin(-180, 180, d.notch_angle, 0, "°", 5)
        self.notch_a.setToolTip("Where the notch sits around the hole")
        self.keep = QCheckBox("Keep hole pin")
        self.keep.setToolTip("Keep the plug cut out of the wall as a new part, to glue back in after "
                             "draining")
        self.clearance = _dspin(0, 2, d.clearance, 2, " mm", 0.05)
        self.clearance.setToolTip("Gap between the pin and the hole")
        self.clear_side = QComboBox()
        self.clear_side.addItems(CLEARANCE_LABELS)
        self.clear_side.setToolTip("Make the gap by shrinking the pin, growing the hole, or both")
        self.more_btn = QPushButton("More options")
        self.more_btn.setCheckable(True)
        self.more_btn.setFlat(True)
        self.more_btn.toggled.connect(self._show_more)
        lay.addWidget(self.more_btn, 0, Qt.AlignLeft)
        self.more = QWidget()
        lay.addWidget(self.more)
        m = QGridLayout(self.more)
        m.setContentsMargins(0, 0, 0, 0)
        m.setColumnStretch(1, 1)
        for row, (label, w) in enumerate((("Edges", self.edges), (self.use_angle, self.angle),
                                          (self.notch, None), ("Width", self.notch_w),
                                          ("Height", self.notch_h), ("Angle", self.notch_a),
                                          (self.keep, None), ("Clearance", self.clearance),
                                          ("Gap on", self.clear_side))):
            m.addWidget(QLabel(label) if isinstance(label, str) else label, row, 0, 1, 1 if w else 2)
            if w is not None:
                m.addWidget(w, row, 1)
        self.more.setVisible(False)
        self.load()
        for a, b, lock in ((self.r1, self.r2, self.radius_lock), (self.r2, self.r1, self.radius_lock),
                           (self.e1, self.e2, self.depth_lock), (self.e2, self.e1, self.depth_lock)):
            a.valueChanged.connect(lambda v, b=b, lock=lock: self._edited(v, b, lock))
        for lock, (key, _), a, b in ((self.radius_lock, self.LINKS[0], self.r1, self.r2),
                                     (self.depth_lock, self.LINKS[1], self.e1, self.e2)):
            lock.toggled.connect(lambda on, key=key, a=a, b=b: self._lock_toggled(on, key, a, b))
        for w in (self.form, self.direction, self.clear_side):
            w.currentIndexChanged.connect(self._any_changed)
        for w in (self.depth, self.angle, self.notch_w, self.notch_h, self.notch_a, self.clearance,
                  self.edges, *self.dir_xyz):
            w.valueChanged.connect(self._any_changed)
        for w in (self.use_angle, self.notch, self.keep):
            w.toggled.connect(self._any_changed)
        self._enable()

    def _show_more(self, on: bool) -> None:
        self.more.setVisible(on)
        self.more_btn.setText("Fewer options" if on else "More options")
        self.qs.setValue("drain/showMore", bool(on))
        self.updateGeometry()
        self.resized.emit()

    def _enable(self) -> None:
        slot = self.form.currentIndex() == 1
        self.depth.setEnabled(slot)
        self.e2.setEnabled(not slot)
        self.use_angle.setEnabled(not slot)
        by_angle = self.use_angle.isChecked() and not slot
        self.angle.setEnabled(by_angle)
        self.r2.setEnabled(not by_angle)
        for w in (self.notch_w, self.notch_h, self.notch_a):
            w.setEnabled(self.notch.isChecked())
        for w in (self.clearance, self.clear_side):
            w.setEnabled(self.keep.isChecked())
        custom = self.direction.currentData() == "custom"
        for w in self.dir_xyz:
            w.setEnabled(custom)

    def _any_changed(self, *_) -> None:
        self._enable()
        if not self.signalsBlocked():
            self.changed.emit()

    def _edited(self, v: float, other, lock) -> None:
        if lock.isChecked() and other.value() != v:
            other.blockSignals(True)
            other.setValue(v)
            other.blockSignals(False)
        if not self.signalsBlocked():
            self.changed.emit()

    def _lock_toggled(self, on: bool, key: str, a, b) -> None:
        self.qs.setValue(f"drain/{key}", bool(on))
        if on and b.value() != a.value():
            b.setValue(a.value())                     # linked again: the second follows the first

    @property
    def radii_linked(self) -> bool:
        return self.radius_lock.isChecked()

    @property
    def depths_linked(self) -> bool:
        return self.depth_lock.isChecked()

    def load(self) -> None:
        d = DrainHoleSettings()
        v = lambda k, dflt, typ=float: typ(self.qs.value(f"drain/{k}", dflt))  # noqa: E731
        b = lambda k, dflt: str(self.qs.value(f"drain/{k}", dflt)).lower() in ("true", "1")  # noqa: E731
        form = str(self.qs.value("drain/holeForm", d.hole_form))
        ctype = v("clearanceType", d.clearance_type, int)
        self.set_value(DrainHoleSettings(
            radius1=v("radius1", d.radius1), radius2=v("radius2", d.radius2),
            extend1=v("extend1", d.extend1), extend2=v("extend2", d.extend2),
            hole_form=form if form in HOLE_FORMS else d.hole_form, depth=v("depth", d.depth),
            edges=min(max(v("edges", d.edges, int), 3), 50),
            use_angle_slot=b("useAngle", d.use_angle_slot), angle_slot=v("angle", d.angle_slot),
            use_notch=b("useNotch", d.use_notch), notch_width=v("notchWidth", d.notch_width),
            notch_height=v("notchHeight", d.notch_height), notch_angle=v("notchAngle", d.notch_angle),
            keep_subtracted=b("keepPin", d.keep_subtracted), clearance=v("clearance", d.clearance),
            clearance_type=ctype if ctype in CLEARANCE_SIDES else d.clearance_type,
            direction=tuple(v(f"dir{a}", x) for a, x in zip("XYZ", d.direction))),
            str(self.qs.value("drain/direction", "normal")))
        for lock, (key, _), a, b_ in ((self.radius_lock, self.LINKS[0], self.r1, self.r2),
                                      (self.depth_lock, self.LINKS[1], self.e1, self.e2)):
            lock.blockSignals(True)
            # linked unless turned off, or the saved pair differs (set before the padlocks existed)
            lock.setChecked(b(key, "true") and abs(a.value() - b_.value()) < 1e-9)
            lock.blockSignals(False)
            lock._tip(lock.isChecked())
        self.more_btn.setChecked(b("showMore", "false"))

    def _widgets(self):
        return (self.form, self.depth, self.r1, self.r2, self.e1, self.e2, self.direction, *self.dir_xyz,
                self.edges, self.use_angle, self.angle, self.notch, self.notch_w, self.notch_h,
                self.notch_a, self.keep, self.clearance, self.clear_side)

    def set_value(self, s: DrainHoleSettings, direction: str = "normal") -> None:
        """Show these values as they are (no linking, no changed signal)."""
        ws = self._widgets()
        for w in ws:
            w.blockSignals(True)
        self.form.setCurrentIndex(HOLE_FORMS.index(s.hole_form))
        self.depth.setValue(s.depth)
        self.r1.setValue(s.radius1)
        self.r2.setValue(s.radius2)
        self.e1.setValue(s.extend1)
        self.e2.setValue(s.extend2)
        self.direction.setCurrentIndex(max(0, self.direction.findData(direction)))
        for w, x in zip(self.dir_xyz, s.direction):
            w.setValue(x)
        self.edges.setValue(int(s.edges))
        self.use_angle.setChecked(s.use_angle_slot)
        self.angle.setValue(s.angle_slot)
        self.notch.setChecked(s.use_notch)
        self.notch_w.setValue(s.notch_width)
        self.notch_h.setValue(s.notch_height)
        self.notch_a.setValue(s.notch_angle)
        self.keep.setChecked(s.keep_subtracted)
        self.clearance.setValue(s.clearance)
        self.clear_side.setCurrentIndex(CLEARANCE_SIDES.index(s.clearance_type))
        for w in ws:
            w.blockSignals(False)
        self._enable()

    def value(self) -> tuple[DrainHoleSettings, str]:
        direction = self.direction.currentData()
        return (DrainHoleSettings(
            radius1=self.r1.value(), radius2=self.r2.value(), extend1=self.e1.value(), extend2=self.e2.value(),
            hole_form=HOLE_FORMS[self.form.currentIndex()], depth=self.depth.value(), edges=self.edges.value(),
            use_angle_slot=self.use_angle.isChecked(), angle_slot=self.angle.value(),
            use_notch=self.notch.isChecked(), notch_width=self.notch_w.value(),
            notch_height=self.notch_h.value(), notch_angle=self.notch_a.value(),
            keep_subtracted=self.keep.isChecked(), use_clearance=self.keep.isChecked(),
            clearance=self.clearance.value(), clearance_type=CLEARANCE_SIDES[self.clear_side.currentIndex()],
            direction_id=1 if direction == "custom" else 0,
            direction=tuple(w.value() for w in self.dir_xyz)), direction)

    def save(self):
        s, direction = self.value()
        for k, val in (("radius1", s.radius1), ("radius2", s.radius2), ("extend1", s.extend1),
                       ("extend2", s.extend2), ("direction", direction), ("holeForm", s.hole_form),
                       ("depth", s.depth), ("edges", s.edges), ("useAngle", s.use_angle_slot),
                       ("angle", s.angle_slot), ("useNotch", s.use_notch), ("notchWidth", s.notch_width),
                       ("notchHeight", s.notch_height), ("notchAngle", s.notch_angle),
                       ("keepPin", s.keep_subtracted), ("clearance", s.clearance),
                       ("clearanceType", s.clearance_type), ("dirX", s.direction[0]),
                       ("dirY", s.direction[1]), ("dirZ", s.direction[2])):
            self.qs.setValue(f"drain/{k}", val)


class FloatingPanel(QFrame):
    """Settings floating over the top-left of the 3D view while a tool is in use: the view
    stays live, so parts can be clicked, orbited and zoomed with it open. Subclasses fill
    `self.body` and may add buttons left of Done to `self.buttons`."""
    finished = Signal()
    entered = Signal()          # the cursor is over the panel, not the model

    def __init__(self, title: str, fields, hint: str, done_tip: str, parent=None):
        super().__init__(parent)
        self.setFrameShape(QFrame.StyledPanel)
        self.setAutoFillBackground(True)
        self.setCursor(Qt.ArrowCursor)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        t = QLabel(title)
        lay.addWidget(t)
        f = t.font()
        f.setBold(True)
        t.setFont(f)
        self.fields = fields
        lay.addWidget(fields)
        self.hint = QLabel(hint)
        lay.addWidget(self.hint)
        self.hint.setWordWrap(True)
        self.hint.setEnabled(False)
        self.buttons = QHBoxLayout()
        lay.addLayout(self.buttons)
        self.buttons.addStretch(1)
        self.done_btn = QPushButton("Done")
        self.buttons.addWidget(self.done_btn)
        self.done_btn.setToolTip(done_tip)
        self.done_btn.clicked.connect(self.finished)

    def fit_width(self) -> None:
        self.setFixedWidth(max(300, self.sizeHint().width()))

    def enterEvent(self, e):
        self.entered.emit()
        super().enterEvent(e)

    def place(self) -> None:
        """Top-left corner of the parent (the 3D view)."""
        self.adjustSize()
        self.move(12, 12)
        self.raise_()


class DrainHolePanel(FloatingPanel):
    """The drain-hole settings, shown while holes are added."""

    def __init__(self, settings, parent=None):
        super().__init__("Drain holes", DrainHoleFields(settings),
                         "Click a part to drill a hole where the blue preview is. "
                         "Right-drag orbits, the wheel zooms.",
                         "Stop adding drain holes (Esc)", parent)
        self.setObjectName("drainHolePanel")
        self.fields.resized.connect(self.place)
        self.fit_width()


class HollowPanel(FloatingPanel):
    """The Hollow settings (06 §2), shown until Done: select a part in the view and press
    Hollow, as many times as needed. Hollowed parts are drawn see-through."""
    hollow = Signal()

    def __init__(self, settings, parent=None):
        super().__init__("Hollow", HollowFields(settings),
                         "Click a part to select it (Ctrl+click for more), then Hollow. Hollowed "
                         "parts turn see-through so the cavity shows. Right-drag orbits, the wheel zooms.",
                         "Stop hollowing (Esc)", parent)
        self.setObjectName("hollowPanel")
        self.status = QLabel()
        self.layout().insertWidget(self.layout().indexOf(self.hint) + 1, self.status)
        self.status.setWordWrap(True)
        self.status.hide()
        self.hollow_btn = QPushButton("Hollow")
        self.buttons.insertWidget(1, self.hollow_btn)
        self.hollow_btn.setDefault(True)
        self.hollow_btn.clicked.connect(self.hollow)
        self.fit_width()

    def set_state(self, selected: int, busy: int) -> None:
        """Hollow needs a selection; `busy` parts are being hollowed."""
        self.hollow_btn.setEnabled(selected > 0)
        self.hollow_btn.setText("Hollow" if selected <= 1 else f"Hollow {selected} parts")
        self.hollow_btn.setToolTip("Hollow the selected part" if selected == 1 else
                                   "Hollow the selected parts" if selected else "Select a part first")
        if busy:
            self.status.setText(f"Hollowing {busy} part{'' if busy == 1 else 's'}…")
        self.status.setVisible(bool(busy))
        if not self.isHidden():
            self.place()


class _FieldsDialog(QDialog):
    def __init__(self, fields, title, parent=None):
        super().__init__(parent)
        self.setWindowTitle(title)
        self.fields = fields
        lay = QVBoxLayout(self)
        lay.addWidget(fields)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        lay.addWidget(bb)

    def value(self):
        return self.fields.value()

    def save(self):
        self.fields.save()


class DrainHoleDialog(_FieldsDialog):
    """Drain-hole size and direction (defaults: SatelLite's Perforation page, 06 §3)."""

    def __init__(self, settings, parent=None):
        super().__init__(DrainHoleFields(settings), "Drain holes", parent)
        self.fields.resized.connect(self.adjustSize)


# ------------------------------------------------------------------ part-frame helpers
# Work in the part's *scaled* local frame: world = R·(S·v) + t, so distances there are true
# millimetres (rotation is rigid). Results are unscaled back into the part's local mesh.
def hole_axis_world(part, face: int, direction: str = "normal", custom=(1.0, 0.0, 0.0)) -> np.ndarray:
    """The drill direction in world space (into the part), as drill_part uses it. "normal" is
    SatelLite's "Perpendicular to surface"; a custom vector is turned round when it points out
    of the surface [OBSERVED Perforator::findNextInterPoint]."""
    if direction == "vertical":
        return np.array([0.0, 0.0, 1.0])
    n_local = np.asarray(part.mesh.face_normals[face], float)
    n = part.rotation @ (n_local / part.scale)
    n /= np.linalg.norm(n)
    if direction == "custom":
        v = np.asarray(custom, float)
        if np.linalg.norm(v) > 1e-9:
            v = v / np.linalg.norm(v)
            return -v if float(n @ v) > 0 else v
    return -n


def hole_length_world(part, hit_world, axis_world) -> float:
    """hollow.hole_length for a scene Part: from the clicked point along the axis to the next
    surface, in world millimetres."""
    from gooslicer.scene import ray_triangles
    M = part.matrix()
    Mi = np.linalg.inv(M)
    a = np.asarray(axis_world, float)
    p = np.asarray(hit_world, float)
    o = (Mi @ np.append(p + a * 1e-3, 1.0))[:3]
    hit = ray_triangles(part.mesh, o, Mi[:3, :3] @ a)
    if hit is None:
        return 0.0
    return float(np.linalg.norm((M @ np.append(hit[1], 1.0))[:3] - p))


def hole_preview_mesh(part, face: int, hit_world, s: DrainHoleSettings, direction: str = "normal",
                      length: float | None = None) -> trimesh.Trimesh:
    """The solid a click would cut, in world space: hollow.DrainHole's shape (07 §3) through
    the wall to the next surface (or a slot's depth)."""
    a = hole_axis_world(part, face, direction, s.direction)
    if length is None and s.hole_form != "slot":
        length = hole_length_world(part, hit_world, a)
    hole = H.DrainHole(tuple(np.asarray(hit_world, float)), tuple(a), s, length)
    return hole.mesh(H._clearances(s)[0])


def drill_part(part, face: int, hit_world, s: DrainHoleSettings, direction: str = "normal"):
    return drill_part_at(part, hit_world, hole_axis_world(part, face, direction, s.direction), s)


def drill_part_at(part, hit_world, axis_world, s: DrainHoleSettings):
    """The part's mesh with a hole drilled at a world point along a world direction (into the
    part). Works on any object with the part's mesh, position, rotation and scale, so a snapshot
    can be drilled off the GUI thread."""
    return drill_part_keep(part, hit_world, axis_world, s)[0]


def drill_part_keep(part, hit_world, axis_world, s: DrainHoleSettings):
    """drill_part_at, and the hole's pin in world space when the settings keep it (else None)."""
    m, flipped = jobs.scaled(part)
    p_local = part.rotation.T @ (np.asarray(hit_world, float) - part.position)
    axis = part.rotation.T @ np.asarray(axis_world, float)      # the scaled frame is rigid
    hole = H.DrainHole(tuple(p_local), tuple(axis / np.linalg.norm(axis)), s)
    out, pins = H.drill_keep(m, [hole])
    pin = None
    if pins:
        pin = pins[0]
        T = np.eye(4)
        T[:3, :3], T[:3, 3] = part.rotation, part.position
        pin.apply_transform(T)
    return jobs.unscaled(out, part.scale, flipped), pin
