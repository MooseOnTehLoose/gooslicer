"""Print-settings panel, slicing thread and layer preview."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QHBoxLayout, QLabel,
                               QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget)

from elegoo_goo import goo, rle
from gooslicer import settings as S
from gooslicer import slicer as SL

SLICE_POS = (("Layer bottom", 0.0), ("Layer middle", 0.5), ("Layer top", 1.0))


class PrintSettingsPanel(QWidget):
    changed = Signal()

    FIELDS = (  # attr, label, kind, min, max, decimals/suffix
        ("layer_height", "Layer height", float, 0.01, 0.2, (3, " mm")),
        ("bottom_layers", "Bottom layers", int, 0, 100, None),
        ("bottom_exposure", "Bottom exposure", float, 0.1, 300, (2, " s")),
        ("exposure", "Exposure", float, 0.1, 60, (2, " s")),
        ("transition_layers", "Transition layers", int, 0, 100, None),
        ("bottom_wait_after_retract", "Wait after retract", float, 0, 60, (2, " s")),
        ("light_off_delay", "Light-off delay", float, 0, 60, (2, " s")),
        ("light_pwm", "Light PWM", int, 1, 255, None),
        ("bottom_light_pwm", "Bottom light PWM", int, 1, 255, None),
    )

    def __init__(self, parent=None):
        super().__init__(parent)
        self.resins: list[S.ResinProfile] = []
        self.base = S.PrintSettings()
        self.resin = QComboBox()
        self.resin.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.resin.setMinimumContentsLength(18)
        self.slice_pos = QComboBox()
        for label, _ in SLICE_POS:
            self.slice_pos.addItem(label)
        self.slice_pos.setCurrentIndex(1)
        self.slice_pos.setToolTip("Height inside each layer at which the model is cut "
                                  "(SatelLite default: layer middle).")
        self.widgets = {}
        lay = QFormLayout(self)
        lay.addRow("Resin profile", self.resin)
        for attr, label, kind, lo, hi, fmt in self.FIELDS:
            if kind is int:
                w = QSpinBox()
                w.setRange(lo, hi)
                w.valueChanged.connect(self.changed)
            else:
                w = QDoubleSpinBox()
                w.setRange(lo, hi)
                w.setDecimals(fmt[0])
                w.setSuffix(fmt[1])
                w.setSingleStep(10 ** -fmt[0] * (5 if fmt[0] > 1 else 1))
                w.valueChanged.connect(self.changed)
            self.widgets[attr] = w
            lay.addRow(label, w)
        lay.addRow("Slice at", self.slice_pos)
        self.resin.currentIndexChanged.connect(self._resin_changed)

    def set_sources(self, base: S.PrintSettings, resins: list[S.ResinProfile], current: str | None = None):
        self.base, self.resins = base, resins
        self.resin.blockSignals(True)
        self.resin.clear()
        self.resin.addItem("Printer default")
        for r in resins:
            self.resin.addItem(f"{r.name}" + (f"  ({r.resin_type})" if r.resin_type else ""), r.id)
        idx = next((i + 1 for i, r in enumerate(resins) if r.name == current), 0)
        self.resin.setCurrentIndex(idx)
        self.resin.blockSignals(False)
        self._resin_changed()

    def current_resin(self) -> S.ResinProfile | None:
        i = self.resin.currentIndex()
        return self.resins[i - 1] if 0 < i <= len(self.resins) else None

    def _resin_changed(self, *_):
        r = self.current_resin()
        self._load(r.settings() if r else self.base)
        self.changed.emit()

    def _load(self, s: S.PrintSettings):
        self._loaded = s
        for attr, w in self.widgets.items():
            w.blockSignals(True)
            w.setValue(getattr(s, attr))
            w.blockSignals(False)

    def settings(self) -> S.PrintSettings:
        s = getattr(self, "_loaded", self.base)
        kw = {attr: (w.value() if isinstance(w, QSpinBox) else round(w.value(), 4)) for attr, w in self.widgets.items()}
        kw["slice_z_fraction"] = SLICE_POS[self.slice_pos.currentIndex()][1]
        return S.with_overrides(s, **kw)


class SliceThread(QThread):
    progress = Signal(int, int)
    finishedOk = Signal(object)
    failed = Signal(str)

    def __init__(self, job: SL.SliceJob, path: str, parent=None):
        super().__init__(parent)
        self.job, self.path = job, path
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            res = self.job.write(self.path, progress=lambda d, n: self.progress.emit(d, n),
                                 cancelled=lambda: self._cancel)
        except SL.SliceCancelled:
            self.failed.emit("cancelled")
        except Exception as e:  # surface every failure in the UI
            self.failed.emit(str(e) or type(e).__name__)
        else:
            self.finishedOk.emit(res)


def layer_thumbnail(g: goo.GooFile, index: int, max_w: int = 1600) -> np.ndarray:
    """Decode one layer and downsample (max-pool) to at most max_w columns, plate-oriented."""
    h = g.header
    W, H = h.resolution_x, h.resolution_y
    img = np.frombuffer(bytes(rle.decode(g.layers[index].data, W, H, strict=False)), np.uint8).reshape(H, W)
    f = max(1, int(np.ceil(W / max_w)))
    Hp, Wp = -(-H // f) * f, -(-W // f) * f
    if (Hp, Wp) != (H, W):
        img = np.pad(img, ((0, Hp - H), (0, Wp - W)))
    small = img.reshape(Hp // f, f, Wp // f, f).max(axis=(1, 3))
    if h.mirror_y:          # undo the LCD mirroring so +Y (back of plate) is at the top
        small = small[::-1]
    if h.mirror_x:
        small = small[:, ::-1]
    return np.ascontiguousarray(small)


class LayerPreview(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.goo: goo.GooFile | None = None
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setEnabled(False)
        self.info = QLabel("Slice a model or open a .goo file to inspect its layers.")
        self.image = QLabel()
        self.image.setAlignment(Qt.AlignCenter)
        self.image.setMinimumHeight(60)
        self.image.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)
        self.image.setStyleSheet("background:#111;")
        top = QHBoxLayout()
        top.addWidget(self.slider, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.info)
        lay.addWidget(self.image, 1)
        self.slider.valueChanged.connect(self.show_layer)
        self._last = None

    def set_file(self, g: goo.GooFile):
        self.goo = g
        self.slider.blockSignals(True)
        self.slider.setRange(0, max(0, len(g.layers) - 1))
        self.slider.setValue(0)
        self.slider.blockSignals(False)
        self.slider.setEnabled(bool(g.layers))
        self.show_layer(0)

    def show_layer(self, i: int):
        if not self.goo or not self.goo.layers:
            return
        ld = self.goo.layers[i].definition
        arr = layer_thumbnail(self.goo, i)
        self._last = arr
        qi = QImage(arr.data, arr.shape[1], arr.shape[0], arr.strides[0], QImage.Format_Grayscale8).copy()
        self._pix = QPixmap.fromImage(qi)
        self._rescale()
        lit = int((arr > 0).sum())
        self.info.setText(f"Layer {i + 1} / {len(self.goo.layers)}   z = {ld.z:.3f} mm   "
                          f"exposure {ld.exposure_time:.2f} s   lit (preview px): {lit:,}")

    def _rescale(self):
        if not getattr(self, "_pix", None):
            return
        h = self.goo.header
        # pixels are not square on every printer (16K: 14 × 19 µm) -> show at physical aspect
        aspect = (h.display_width / h.display_height) if h.display_height else \
            self._pix.width() / max(1, self._pix.height())
        box = self.image.size()
        w = min(box.width(), int(box.height() * aspect))
        self.image.setPixmap(self._pix.scaled(max(1, w), max(1, int(w / aspect)),
                                              Qt.IgnoreAspectRatio, Qt.SmoothTransformation))

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._rescale()
