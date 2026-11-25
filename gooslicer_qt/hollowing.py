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
from gooslicer.support_defaults import HOLLOW_TYPES, INFILL_TYPES, DrainHoleSettings, HollowSettings

TYPE_LABELS = ("Inside", "Outside", "Center", "Open bottom")
INFILL_LABELS = ("BCC", "FCC")
DIRECTIONS = (("Along surface normal", "normal"), ("Vertical (from below)", "vertical"))


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
        self.angle.setToolTip("Open bottom: stored for SatelLite parity; GooSlicer opens the floor "
                              "under each cavity regardless (spec 07 §2).")
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
        self.load()
        for w in (self.thickness, self.accuracy, self.angle, self.cavities, self.infill_size, self.infill_radius):
            w.valueChanged.connect(self.changed)
        self.kind.currentIndexChanged.connect(self.changed)
        self.infill_type.currentIndexChanged.connect(self.changed)
        self.filter.toggled.connect(self.changed)
        self.infill.toggled.connect(self.changed)
        self.infill.toggled.connect(self._infill_enabled)
        self.kind.currentIndexChanged.connect(self._infill_enabled)
        self._infill_enabled()

    def _infill_enabled(self, *_):
        """SatelLite offers infill for every type but Open bottom."""
        allowed = HOLLOW_TYPES[self.kind.currentIndex()] != "open_bottom"
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
                       ("infillRadius", s.infill_radius)):
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
    """Drain-hole size and direction (defaults: SatelLite's Perforation page, 06 §3). A padlock
    beside each pair (outer/inner radius, extend outside/depth inside) links them: while it's
    closed, changing one sets the other to the same value."""
    changed = Signal()
    LINKS = (("linkRadius", "Outer and inner radius"), ("linkDepth", "Extend outside and depth inside"))

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.qs = settings
        d = DrainHoleSettings()
        self.r1 = _dspin(0.2, 20, d.radius1)
        self.r2 = _dspin(0.2, 20, d.radius2)
        self.e1 = _dspin(0, 50, d.extend1)
        self.e2 = _dspin(0.1, 50, d.extend2)
        self.direction = QComboBox()
        for label, key in DIRECTIONS:
            self.direction.addItem(label, key)
        self.radius_lock = LockButton(self.LINKS[0][1])
        self.depth_lock = LockButton(self.LINKS[1][1])
        g = QGridLayout(self)
        g.setContentsMargins(0, 0, 0, 0)
        g.setColumnStretch(1, 1)
        for row, (label, w) in enumerate((("Outer radius", self.r1), ("Inner radius", self.r2),
                                          ("Extend outside", self.e1), ("Depth inside", self.e2),
                                          ("Direction", self.direction))):
            g.addWidget(QLabel(label), row, 0)
            g.addWidget(w, row, 1)
        # each padlock to the right of its pair, centred between the two rows
        g.addWidget(self.radius_lock, 0, 2, 2, 1, Qt.AlignCenter)
        g.addWidget(self.depth_lock, 2, 2, 2, 1, Qt.AlignCenter)
        self.load()
        for a, b, lock in ((self.r1, self.r2, self.radius_lock), (self.r2, self.r1, self.radius_lock),
                           (self.e1, self.e2, self.depth_lock), (self.e2, self.e1, self.depth_lock)):
            a.valueChanged.connect(lambda v, b=b, lock=lock: self._edited(v, b, lock))
        for lock, (key, _), a, b in ((self.radius_lock, self.LINKS[0], self.r1, self.r2),
                                     (self.depth_lock, self.LINKS[1], self.e1, self.e2)):
            lock.toggled.connect(lambda on, key=key, a=a, b=b: self._lock_toggled(on, key, a, b))
        self.direction.currentIndexChanged.connect(self.changed)

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
        v = lambda k, dflt: float(self.qs.value(f"drain/{k}", dflt))  # noqa: E731
        self.set_value(DrainHoleSettings(radius1=v("radius1", d.radius1), radius2=v("radius2", d.radius2),
                                         extend1=v("extend1", d.extend1), extend2=v("extend2", d.extend2)),
                       str(self.qs.value("drain/direction", "normal")))
        for lock, (key, _), a, b in ((self.radius_lock, self.LINKS[0], self.r1, self.r2),
                                     (self.depth_lock, self.LINKS[1], self.e1, self.e2)):
            lock.blockSignals(True)
            # linked unless turned off, or the saved pair differs (set before the padlocks existed)
            lock.setChecked(str(self.qs.value(f"drain/{key}", "true")).lower() in ("true", "1")
                            and abs(a.value() - b.value()) < 1e-9)
            lock.blockSignals(False)
            lock._tip(lock.isChecked())

    def set_value(self, s: DrainHoleSettings, direction: str = "normal") -> None:
        """Show these values as they are (no linking, no changed signal)."""
        ws = (self.r1, self.r2, self.e1, self.e2, self.direction)
        for w in ws:
            w.blockSignals(True)
        self.r1.setValue(s.radius1)
        self.r2.setValue(s.radius2)
        self.e1.setValue(s.extend1)
        self.e2.setValue(s.extend2)
        self.direction.setCurrentIndex(max(0, self.direction.findData(direction)))
        for w in ws:
            w.blockSignals(False)

    def value(self) -> tuple[DrainHoleSettings, str]:
        return (DrainHoleSettings(radius1=self.r1.value(), radius2=self.r2.value(),
                                  extend1=self.e1.value(), extend2=self.e2.value()),
                self.direction.currentData())

    def save(self):
        s, direction = self.value()
        for k, val in (("radius1", s.radius1), ("radius2", s.radius2), ("extend1", s.extend1),
                       ("extend2", s.extend2), ("direction", direction)):
            self.qs.setValue(f"drain/{k}", val)


class DrainHolePanel(QFrame):
    """The drain-hole settings floating over the top-left of the 3D view while holes are added:
    the view stays live, so parts can be clicked, orbited and zoomed with it open."""
    finished = Signal()
    entered = Signal()          # the cursor is over the panel, not the model

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setObjectName("drainHolePanel")
        self.setFrameShape(QFrame.StyledPanel)
        self.setAutoFillBackground(True)
        self.setCursor(Qt.ArrowCursor)
        title = QLabel("Drain holes")
        f = title.font()
        f.setBold(True)
        title.setFont(f)
        self.fields = DrainHoleFields(settings)
        hint = QLabel("Click a part to drill a hole where the blue preview is. "
                      "Right-drag orbits, the wheel zooms.")
        hint.setWordWrap(True)
        hint.setEnabled(False)
        self.done_btn = QPushButton("Done")
        self.done_btn.setToolTip("Stop adding drain holes (Esc)")
        self.done_btn.clicked.connect(self.finished)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.addWidget(title)
        lay.addWidget(self.fields)
        lay.addWidget(hint)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(self.done_btn)
        lay.addLayout(row)
        self.setFixedWidth(max(300, self.sizeHint().width()))

    def enterEvent(self, e):
        self.entered.emit()
        super().enterEvent(e)

    def place(self) -> None:
        """Top-left corner of the parent (the 3D view)."""
        self.adjustSize()
        self.move(12, 12)
        self.raise_()


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


class HollowDialog(_FieldsDialog):
    """Fields and defaults as SatelLite's Hollow dialog (06 §2)."""

    def __init__(self, settings, parent=None):
        super().__init__(HollowFields(settings), "Hollow part", parent)


class DrainHoleDialog(_FieldsDialog):
    """Drain-hole size and direction (defaults: SatelLite's Perforation page, 06 §3)."""

    def __init__(self, settings, parent=None):
        super().__init__(DrainHoleFields(settings), "Drain holes", parent)


# ------------------------------------------------------------------ part-frame helpers
# Work in the part's *scaled* local frame: world = R·(S·v) + t, so distances there are true
# millimetres (rotation is rigid). Results are unscaled back into the part's local mesh.
def hole_axis_world(part, face: int, direction: str = "normal") -> np.ndarray:
    """The drill direction in world space (into the part), as drill_part uses it."""
    if direction == "vertical":
        return np.array([0.0, 0.0, 1.0])
    n_local = np.asarray(part.mesh.face_normals[face], float)
    n = part.rotation @ (n_local / part.scale)
    n /= np.linalg.norm(n)
    return -n


def hole_preview_mesh(part, face: int, hit_world, s: DrainHoleSettings, direction: str = "normal",
                      segments: int = 40) -> trimesh.Trimesh:
    """The solid a click would cut, in world space: a cone frustum along the drill axis from
    extend1 outside the surface (outer radius) to extend2 inside it (inner radius), as
    hollow.DrainHole.solid."""
    a = hole_axis_world(part, face, direction)
    t = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
    t /= np.linalg.norm(t)
    b = np.cross(a, t)
    ang = np.linspace(0, 2 * np.pi, segments, endpoint=False)
    ring = np.outer(np.cos(ang), t) + np.outer(np.sin(ang), b)
    start = np.asarray(hit_world, float) - a * s.extend1
    end = start + a * (s.extend1 + s.extend2)
    v = np.vstack([start + ring * s.radius1, end + ring * s.radius2, start, end])
    i = np.arange(segments)
    j = (i + 1) % segments
    n = segments
    faces = np.vstack([np.c_[i, j, n + j], np.c_[i, n + j, n + i],            # side
                       np.c_[np.full(n, 2 * n), j, i],                           # outer cap
                       np.c_[np.full(n, 2 * n + 1), n + i, n + j]])              # inner cap
    m = trimesh.Trimesh(v, faces, process=False)
    if m.volume < 0:
        m.invert()
    return m


def drill_part(part, face: int, hit_world, s: DrainHoleSettings, direction: str = "normal"):
    return drill_part_at(part, hit_world, hole_axis_world(part, face, direction), s)


def drill_part_at(part, hit_world, axis_world, s: DrainHoleSettings):
    """The part's mesh with a hole drilled at a world point along a world direction (into the
    part). Works on any object with the part's mesh, position, rotation and scale, so a snapshot
    can be drilled off the GUI thread."""
    m, flipped = jobs.scaled(part)
    p_local = part.rotation.T @ (np.asarray(hit_world, float) - part.position)
    axis = part.rotation.T @ np.asarray(axis_world, float)      # the scaled frame is rigid
    hole = H.DrainHole(tuple(p_local), tuple(axis / np.linalg.norm(axis)), s)
    return jobs.unscaled(H.drill(m, [hole]), part.scale, flipped)
