"""Print-settings panel, slicing thread and layer preview."""
from __future__ import annotations

import numpy as np
import math

from PySide6.QtCore import QEvent, QObject, QPointF, QSize, QThread, Qt, Signal
from PySide6.QtGui import QColor, QIcon, QImage, QPainter, QPen, QPixmap, QPolygonF
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QFormLayout, QFrame, QHBoxLayout, QLabel,
                               QSizePolicy, QSlider, QSpinBox, QVBoxLayout, QWidget)

from elegoo_goo import goo, rle
from gooslicer import settings as S
from gooslicer import slicer as SL

SLICE_POS = (("Layer bottom", 0.0), ("Layer middle", 0.5), ("Layer top", 1.0))

STAR_SIZE = 16
STAR_OFF = "#9aa0a6"                    # grey outline
STAR_ON, STAR_ON_EDGE = "#f5c518", "#c99a00"   # filled yellow


def star_icon(filled: bool, size: int = STAR_SIZE) -> QIcon:
    """A five-pointed star: grey outline (not a favourite) or filled yellow (favourite)."""
    icon = QIcon()
    for scale in (1, 2):                # crisp on HiDPI
        px = QPixmap(size * scale, size * scale)
        px.setDevicePixelRatio(scale)
        px.fill(Qt.transparent)
        p = QPainter(px)
        p.setRenderHint(QPainter.Antialiasing)
        c, ro, ri = size / 2, size / 2 - 1.2, (size / 2 - 1.2) * 0.42
        pts = [QPointF(c + (ro if k % 2 == 0 else ri) * math.sin(k * math.pi / 5),
                       c + 0.6 - (ro if k % 2 == 0 else ri) * math.cos(k * math.pi / 5)) for k in range(10)]
        if filled:
            p.setPen(QPen(QColor(STAR_ON_EDGE), 1.0))
            p.setBrush(QColor(STAR_ON))
        else:
            p.setPen(QPen(QColor(STAR_OFF), 1.3))
            p.setBrush(Qt.NoBrush)
        p.drawPolygon(QPolygonF(pts))
        p.end()
        icon.addPixmap(px)
    return icon


def blank_icon(size: int = STAR_SIZE) -> QIcon:
    px = QPixmap(size, size)
    px.fill(Qt.transparent)
    return QIcon(px)


class _StarClicks(QObject):
    """Clicks on the star column of the resin list toggle the favourite instead of selecting
    the row, and the list stays open."""

    def __init__(self, panel: "PrintSettingsPanel"):
        super().__init__(panel)
        self.panel = panel

    def star_hit(self, pos) -> int | None:
        view = self.panel.resin.view()
        idx = view.indexAt(pos)
        if not idx.isValid():
            return None
        ri = idx.data(Qt.UserRole)
        if not isinstance(ri, int):
            return None                 # "Printer default" or the divider
        if pos.x() > view.visualRect(idx).left() + STAR_SIZE + 8:
            return None
        return ri

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            ri = self.star_hit(ev.position().toPoint())
            if ri is not None:
                if t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton:
                    self.panel.toggle_favorite(self.panel.resins[ri].key)
                return True             # swallow: no selection, popup stays open
        return False


class PrintSettingsPanel(QWidget):
    changed = Signal()
    favoritesChanged = Signal(list)     # favourite profile keys (printer|name), to persist
    resinSelected = Signal(str)         # picked by the user ("" = Printer default), to persist

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
        self.favorites: set[str] = set()    # ResinProfile.key: printer|name, so per printer
        self.base = S.PrintSettings()
        self.resin = QComboBox()
        self.resin.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.resin.setMinimumContentsLength(18)
        self.resin.setIconSize(QSize(STAR_SIZE, STAR_SIZE))
        self.resin.setToolTip("Click a star in the list to add or remove a favourite. "
                              "Favourites are listed first.")
        self._star_on, self._star_off, self._blank = star_icon(True), star_icon(False), blank_icon()
        self._star_clicks = _StarClicks(self)
        self.resin.view().viewport().installEventFilter(self._star_clicks)
        self.slice_pos = QComboBox()
        for label, _ in SLICE_POS:
            self.slice_pos.addItem(label)
        self.slice_pos.setCurrentIndex(1)
        self.slice_pos.setToolTip("Height inside each layer at which the model is cut "
                                  "(SatelLite default: layer middle).")
        self.widgets = {}
        outer = QVBoxLayout(self)
        self._top = QFormLayout()
        self._top.addRow("Resin profile", self.resin)
        outer.addLayout(self._top)
        # everything below the resin is tucked away behind "Advanced settings" (collapsed)
        from .panels import disclosure_button
        self.advanced_btn = disclosure_button(
            "Advanced settings", "Layer height, exposures, delays, light power and slice position. "
                                 "Choosing a resin profile fills these in.")
        self.advanced_btn.toggled.connect(self.set_advanced_visible)
        outer.addWidget(self.advanced_btn)
        self.advanced = QWidget()
        lay = QFormLayout(self.advanced)
        lay.setContentsMargins(0, 0, 0, 0)
        outer.addWidget(self.advanced)
        outer.addStretch(1)
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
        self.set_advanced_visible(False)
        self.resin.currentIndexChanged.connect(self._resin_changed)
        # list rebuilds block signals, so this fires only for the user's own picks
        self.resin.currentIndexChanged.connect(
            lambda *_: self.resinSelected.emit(r.name if (r := self.current_resin()) else ""))

    def insert_top_widget(self, widget: QWidget) -> None:
        """A section above the Printer/Resin rows, with a divider line (the main window puts
        printer control there)."""
        line = QFrame()
        line.setFrameShape(QFrame.HLine)
        line.setFrameShadow(QFrame.Sunken)
        self.layout().insertWidget(0, line)
        self.layout().insertWidget(0, widget)

    def insert_top_row(self, label: str, widget: QWidget) -> None:
        """A row above the resin profile (the main window puts the printer there)."""
        self._top.insertRow(0, label, widget)

    def set_advanced_visible(self, on: bool) -> None:
        self.advanced.setVisible(on)
        self.advanced_btn.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        if self.advanced_btn.isChecked() != on:
            self.advanced_btn.setChecked(on)

    def set_sources(self, base: S.PrintSettings, resins: list[S.ResinProfile], current: str | None = None):
        self.base, self.resins = base, resins
        cur = next((i for i, r in enumerate(resins) if r.name == current), None)
        self._rebuild(cur)
        self._resin_changed()

    def set_favorites(self, keys) -> None:
        """Favourite profile keys (from settings); re-sorts the list, keeping the selection."""
        self.favorites = {str(k) for k in keys or [] if k}
        self._rebuild(self._current_index())

    def is_favorite(self, key: str) -> bool:
        return key in self.favorites

    def toggle_favorite(self, key: str) -> None:
        self.favorites ^= {key}
        self._rebuild(self._current_index())
        self.favoritesChanged.emit(sorted(self.favorites))

    def _rebuild(self, current: int | None) -> None:
        """'Printer default', then favourites, a divider, then the rest; each in profile order.
        Item data is the index into self.resins (None for 'Printer default' and the divider)."""
        fav = [i for i, r in enumerate(self.resins) if r.key in self.favorites]
        rest = [i for i, r in enumerate(self.resins) if r.key not in self.favorites]
        c = self.resin
        c.blockSignals(True)
        c.clear()
        c.addItem(self._blank, "Printer default", None)
        for group in (fav, rest):
            if group is rest and fav and rest:
                c.insertSeparator(c.count())
            for i in group:
                r = self.resins[i]
                c.addItem(self._star_on if group is fav else self._star_off,
                          r.name + (f"  ({r.resin_type})" if r.resin_type else ""), i)
        pos = c.findData(current) if current is not None else 0
        c.setCurrentIndex(max(pos, 0))
        c.blockSignals(False)

    def _current_index(self) -> int | None:
        d = self.resin.currentData(Qt.UserRole)
        return d if isinstance(d, int) else None

    def current_resin(self) -> S.ResinProfile | None:
        i = self._current_index()
        return self.resins[i] if i is not None and 0 <= i < len(self.resins) else None

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

    def __init__(self, job: SL.SliceJob, path: str | None, parent=None, gpu: bool = False):
        super().__init__(parent)
        self.job, self.path = job, path          # path None: slice into memory (result.goo)
        self._cancel = False
        self.gl = None
        self.gpu_error = ""
        if gpu:                                  # the offscreen surface must be made on the GUI thread
            from .gpuslice import ThreadGL
            try:
                self.gl = ThreadGL()
            except Exception as e:  # noqa: BLE001
                self.gpu_error = str(e)

    def cancel(self):
        self._cancel = True

    def _slice(self, **kw):
        return self.job.build(**kw) if self.path is None else self.job.write(self.path, **kw)

    def run(self):
        kw = dict(progress=lambda d, n: self.progress.emit(d, n), cancelled=lambda: self._cancel)
        try:
            if self.gl is not None:
                try:
                    self.gl.__enter__()
                except RuntimeError as e:        # no context on this thread: the CPU then
                    self.gpu_error = str(e)
                    res = self._slice(**kw)
                else:
                    try:
                        res = self._slice(gpu=True, **kw)
                    finally:
                        self.gl.__exit__(None, None, None)
            else:
                res = self._slice(**kw)
            if self.gpu_error and not self.job.backend_note:
                self.job.backend_note = f"GPU unavailable ({self.gpu_error}); sliced on the CPU"
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
