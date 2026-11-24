"""Hollow dialog, drain-hole settings and helpers that apply them to a scene Part."""
from __future__ import annotations

import numpy as np
import trimesh
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QSpinBox)

from gooslicer import hollow as H
from gooslicer.support_defaults import HOLLOW_TYPES, DrainHoleSettings, HollowSettings

TYPE_LABELS = ("Inside", "Outside", "Center", "Open bottom")
DIRECTIONS = (("Along surface normal", "normal"), ("Vertical (from below)", "vertical"))


def _dspin(lo, hi, val, dec=2, suffix=" mm", step=0.1):
    w = QDoubleSpinBox()
    w.setRange(lo, hi)
    w.setDecimals(dec)
    w.setSuffix(suffix)
    w.setSingleStep(step)
    w.setValue(val)
    return w


class HollowDialog(QDialog):
    """Fields and defaults as SatelLite's Hollow dialog (06 §2)."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hollow part")
        self.qs = settings
        d = HollowSettings()
        v = lambda k, dflt, typ=float: typ(settings.value(f"hollow/{k}", dflt))
        self.thickness = _dspin(0.2, 20, v("thickness", d.thickness))
        self.accuracy = _dspin(0.05, 5, v("accuracy", d.accuracy), step=0.05)
        self.accuracy.setToolTip("Voxel size of the inner wall. Smaller is more precise and slower.")
        self.kind = QComboBox()
        self.kind.addItems(TYPE_LABELS)
        self.kind.setCurrentIndex(max(0, HOLLOW_TYPES.index(settings.value("hollow/type", d.hollow_type))))
        self.angle = _dspin(5, 85, v("surfaceAngle", d.surface_angle), 0, "°", 1)
        self.angle.setToolTip("Open bottom: stored for SatelLite parity; GooSlicer opens the floor "
                              "under each cavity regardless (spec 07 §2).")
        self.filter = QCheckBox("Keep only the largest cavities")
        self.filter.setChecked(str(settings.value("hollow/isFilter", "false")).lower() == "true")
        self.cavities = QSpinBox()
        self.cavities.setRange(1, 9999)
        self.cavities.setValue(v("cavities", d.max_cavities, int))
        self.cavities.setEnabled(self.filter.isChecked())
        self.filter.toggled.connect(self.cavities.setEnabled)
        f = QFormLayout(self)
        f.addRow("Wall thickness", self.thickness)
        f.addRow("Accuracy", self.accuracy)
        f.addRow("Hollow type", self.kind)
        f.addRow("Surface angle", self.angle)
        f.addRow(self.filter)
        f.addRow("Maximum cavities", self.cavities)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def value(self) -> HollowSettings:
        return HollowSettings(thickness=self.thickness.value(), accuracy=self.accuracy.value(),
                              hollow_type=HOLLOW_TYPES[self.kind.currentIndex()],
                              surface_angle=self.angle.value(),
                              filter_small_shells=self.filter.isChecked(),
                              max_cavities=self.cavities.value())

    def save(self):
        s = self.value()
        for k, val in (("thickness", s.thickness), ("accuracy", s.accuracy), ("type", s.hollow_type),
                       ("surfaceAngle", s.surface_angle), ("isFilter", s.filter_small_shells),
                       ("cavities", s.max_cavities)):
            self.qs.setValue(f"hollow/{k}", val)


class DrainHoleDialog(QDialog):
    """Drain-hole size and direction (defaults: SatelLite's Perforation page, 06 §3)."""

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Drain holes")
        self.qs = settings
        d = DrainHoleSettings()
        v = lambda k, dflt: float(settings.value(f"drain/{k}", dflt))
        self.r1 = _dspin(0.2, 20, v("radius1", d.radius1))
        self.r2 = _dspin(0.2, 20, v("radius2", d.radius2))
        self.e1 = _dspin(0, 50, v("extend1", d.extend1))
        self.e2 = _dspin(0.1, 50, v("extend2", d.extend2))
        self.direction = QComboBox()
        for label, key in DIRECTIONS:
            self.direction.addItem(label, key)
        self.direction.setCurrentIndex(max(0, self.direction.findData(settings.value("drain/direction", "normal"))))
        f = QFormLayout(self)
        f.addRow("Outer radius", self.r1)
        f.addRow("Inner radius", self.r2)
        f.addRow("Extend outside", self.e1)
        f.addRow("Depth inside", self.e2)
        f.addRow("Direction", self.direction)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def value(self) -> tuple[DrainHoleSettings, str]:
        return (DrainHoleSettings(radius1=self.r1.value(), radius2=self.r2.value(),
                                  extend1=self.e1.value(), extend2=self.e2.value()),
                self.direction.currentData())

    def save(self):
        s, direction = self.value()
        for k, val in (("radius1", s.radius1), ("radius2", s.radius2), ("extend1", s.extend1),
                       ("extend2", s.extend2), ("direction", direction)):
            self.qs.setValue(f"drain/{k}", val)


# ------------------------------------------------------------------ part-frame helpers
# Work in the part's *scaled* local frame: world = R·(S·v) + t, so distances there are true
# millimetres (rotation is rigid). Results are unscaled back into the part's local mesh.
def _scaled(part) -> tuple[trimesh.Trimesh, bool]:
    m = trimesh.Trimesh(np.asarray(part.mesh.vertices) * part.scale, part.mesh.faces, process=False)
    flipped = bool(np.prod(np.sign(part.scale)) < 0)
    if flipped:
        m.invert()
    return m, flipped


def _unscaled(mesh: trimesh.Trimesh, part, flipped: bool) -> trimesh.Trimesh:
    out = trimesh.Trimesh(np.asarray(mesh.vertices) / part.scale, mesh.faces, process=False)
    if flipped:
        out.invert()
    return out


def hollow_part(part, s: HollowSettings) -> tuple[trimesh.Trimesh, H.HollowResult]:
    m, flipped = _scaled(part)
    res = H.hollow(m, s)
    return _unscaled(res.mesh, part, flipped), res


def drill_part(part, face: int, hit_world, s: DrainHoleSettings, direction: str = "normal"):
    m, flipped = _scaled(part)
    p_local = part.rotation.T @ (np.asarray(hit_world, float) - part.position)
    if direction == "vertical":                 # world +Z into the part, expressed locally
        axis = part.rotation.T @ np.array([0.0, 0.0, 1.0])
    else:
        n = np.asarray(m.face_normals[face], float)
        axis = -n
    hole = H.DrainHole(tuple(p_local), tuple(axis), s)
    return _unscaled(H.drill(m, [hole]), part, flipped)
