"""The Magnet pocket and Cut panels (11 §1–2): settings floating over the 3D view while the
tool is in use, like the drain-hole panel."""
from __future__ import annotations

import numpy as np
import trimesh
from PySide6.QtCore import QSettings, Signal
from PySide6.QtWidgets import QComboBox, QFormLayout, QLabel, QPushButton, QSpinBox, QWidget

from gooslicer import cut as C
from gooslicer import magnet as M

from .hollowing import FloatingPanel, _dspin


def _f(settings: QSettings, key: str, default: float) -> float:
    try:
        return float(settings.value(key, default))
    except (TypeError, ValueError):
        return default


# --------------------------------------------------------------------------- magnet pockets
class MagnetFields(QWidget):
    changed = Signal()

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self.settings = settings
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.size = QComboBox()
        for d, t in M.MAGNET_SIZES:
            self.size.addItem(M.size_name(d, t), (d, t))
        self.size.addItem("Custom", None)
        self.size.setToolTip("Common disc magnets, diameter × thickness")
        self.clearance = _dspin(0.0, 1.0, 0.15, 2, step=0.05)
        self.clearance.setToolTip("Added to the pocket's radius, and twice it to its depth, when a size is chosen")
        self.diameter = _dspin(0.5, 100.0, 6.3, 2)
        self.depth = _dspin(0.2, 50.0, 3.3, 2)
        f.addRow("Magnet", self.size)
        f.addRow("Clearance", self.clearance)
        f.addRow("Pocket diameter", self.diameter)
        f.addRow("Pocket depth", self.depth)
        self.size.currentIndexChanged.connect(self._size_chosen)
        self.clearance.valueChanged.connect(self._size_chosen)
        self.diameter.valueChanged.connect(self._edited)
        self.depth.valueChanged.connect(self._edited)
        self._quiet = False

    def _size_chosen(self, *_):
        if self._quiet:
            return
        dt = self.size.currentData()
        if dt is not None:
            s = M.MagnetSettings(clearance=self.clearance.value())
            s.choose(*dt)
            self._quiet = True
            self.diameter.setValue(s.diameter)
            self.depth.setValue(s.depth)
            self._quiet = False
        self.save()
        self.changed.emit()

    def _edited(self, *_):
        if self._quiet:
            return
        self._quiet = True
        self.size.setCurrentIndex(self.size.count() - 1)        # Custom
        self._quiet = False
        self.save()
        self.changed.emit()

    def value(self) -> M.MagnetSettings:
        dt = self.size.currentData()
        return M.MagnetSettings(M.size_name(*dt) if dt else M.CUSTOM, self.clearance.value(),
                                self.diameter.value(), self.depth.value())

    def load(self) -> None:
        st = self.settings
        d = M.MagnetSettings()
        self._quiet = True
        name = str(st.value("magnet/size", d.size))
        i = self.size.findText(name)
        self.size.setCurrentIndex(i if i >= 0 else self.size.count() - 1)
        self.clearance.setValue(_f(st, "magnet/clearance", d.clearance))
        self.diameter.setValue(_f(st, "magnet/diameter", d.diameter))
        self.depth.setValue(_f(st, "magnet/depth", d.depth))
        self._quiet = False

    def save(self) -> None:
        v = self.value()
        st = self.settings
        st.setValue("magnet/size", v.size if v.size != M.CUSTOM else "Custom")
        st.setValue("magnet/clearance", v.clearance)
        st.setValue("magnet/diameter", v.diameter)
        st.setValue("magnet/depth", v.depth)


class MagnetPanel(FloatingPanel):
    def __init__(self, settings, parent=None):
        super().__init__("Magnet pocket", MagnetFields(settings),
                         "Click a part to cut a pocket where the blue preview is, straight into "
                         "the surface. Right-drag orbits, the wheel zooms.",
                         "Stop adding magnet pockets (Esc)", parent)
        self.setObjectName("magnetPanel")
        self.fit_width()


# --------------------------------------------------------------------------- planar cuts
NORMALS = (("X", (1.0, 0.0, 0.0)), ("Y", (0.0, 1.0, 0.0)), ("Z", (0.0, 0.0, 1.0)), ("Face", None))


class CutFields(QWidget):
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        f = QFormLayout(self)
        f.setContentsMargins(0, 0, 0, 0)
        self.normal = QComboBox()
        for label, n in NORMALS:
            self.normal.addItem(label, n)
        self.normal.setCurrentIndex(2)
        self.normal.setToolTip("The plane's normal: a world axis, or Face: click the part to put the "
                               "plane through that point, square to the face")
        self.position = _dspin(-10000, 10000, 0.0, 2, step=0.5)
        self.position.setToolTip("Where the plane is along its normal")
        self.connector = QComboBox()
        for label, key in (("None", "none"), ("Round peg", "round"), ("Square peg", "square")):
            self.connector.addItem(label, key)
        self.size = _dspin(0.5, 100.0, 5.0, 2)
        self.size.setToolTip("The peg's diameter, or a square peg's side")
        self.height = _dspin(0.2, 100.0, 4.0, 2)
        self.height.setToolTip("How far the peg stands out of the cut face")
        self.clearance = _dspin(0.0, 2.0, 0.15, 2, step=0.05)
        self.clearance.setToolTip("The socket is this much wider all round, and twice it deeper")
        self.count = QSpinBox()
        self.count.setRange(1, 4)
        self.peg_on = QComboBox()
        self.peg_on.addItem("Lower half", "lower")
        self.peg_on.addItem("Upper half", "upper")
        self.gap = _dspin(0.0, 1000.0, 5.0, 1, step=1.0)
        self.gap.setToolTip("How far the halves are moved apart")
        f.addRow("Normal", self.normal)
        f.addRow("Position", self.position)
        f.addRow("Connector", self.connector)
        self.conn_rows = []
        for label, w in (("Size", self.size), ("Height", self.height), ("Clearance", self.clearance),
                         ("Count", self.count), ("Peg on", self.peg_on)):
            f.addRow(label, w)
            self.conn_rows.append(w)
        f.addRow("Gap", self.gap)
        for w in (self.normal, self.connector, self.peg_on):
            w.currentIndexChanged.connect(self._changed)
        for w in (self.position, self.size, self.height, self.clearance, self.gap):
            w.valueChanged.connect(self._changed)
        self.count.valueChanged.connect(self._changed)
        self._sync()

    def _sync(self):
        on = self.connector.currentData() != "none"
        for w in self.conn_rows:
            w.setEnabled(on)

    def _changed(self, *_):
        self._sync()
        self.changed.emit()

    def settings(self) -> C.CutSettings:
        return C.CutSettings(self.connector.currentData(), self.size.value(), self.height.value(),
                             self.clearance.value(), self.count.value(), self.peg_on.currentData(),
                             self.gap.value())

    def axis(self):
        return self.normal.currentData()


class CutPanel(FloatingPanel):
    cut = Signal()

    def __init__(self, parent=None):
        super().__init__("Cut", CutFields(),
                         "Select a part, set the plane (blue), then Cut. With Normal = Face, click "
                         "the part where the plane should go.", "Close the Cut panel (Esc)", parent)
        self.setObjectName("cutPanel")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.layout().insertWidget(self.layout().indexOf(self.hint) + 1, self.status)
        self.cut_btn = QPushButton("Cut")
        self.cut_btn.setDefault(True)
        self.buttons.insertWidget(1, self.cut_btn)
        self.cut_btn.clicked.connect(self.cut)
        self.fit_width()

    def set_status(self, text: str) -> None:
        self.status.setText(text)
        self.status.setVisible(bool(text))
        if not self.isHidden():
            self.place()


def plane_preview(part_mesh_world: trimesh.Trimesh, origin, normal, margin: float = 3.0) -> trimesh.Trimesh:
    """A thin translucent square across the part, showing the cut plane."""
    R = C.plane_frame(normal)
    V = (np.asarray(part_mesh_world.vertices) - np.asarray(origin, float)) @ R.T
    lo, hi = V[:, :2].min(axis=0) - margin, V[:, :2].max(axis=0) + margin
    box = trimesh.creation.box((hi[0] - lo[0], hi[1] - lo[1], 0.05))
    T = np.eye(4)
    T[:3, :3] = R.T
    T[:3, 3] = np.asarray(origin, float) + R.T @ np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, 0.0])
    box.apply_transform(T)
    return box
