"""Hollowing, Supports and Slicing sections of the Models tab (below Mesh): hollow parts, drill
drain holes and check for voids (sealed cavities); pick the support type and generate supports,
detect unsupported islands; and slice the build area into memory with the estimates shown underneath."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QEvent, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (QCheckBox, QComboBox, QFormLayout, QFrame, QGridLayout, QGroupBox, QHBoxLayout,
                               QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from . import flaws as flawmod
from .panels import InfoButton, show_popup

from .supporting import (TipSettingsDialog, load_tips, recommended_choice, save_tips,
                         support_choices, support_note, support_params, tip_summary)


def format_duration(seconds: int) -> str:
    m, s = divmod(int(seconds), 60)
    h, m = divmod(m, 60)
    return f"{h} h {m:02d} min" if h else f"{m} min {s:02d} s"


class JobPanel(QWidget):
    def __init__(self, win, parent=None):
        super().__init__(parent)
        self.win = win
        self._user_choice: str | None = win.settings.value("supportType", None) or None
        self._templates: list = []
        self._trapped_busy = False

        # ---- Hollowing
        hol = QGroupBox("Hollowing")
        hl = QVBoxLayout(hol)
        self.hollow_btn = QPushButton("Hollow…")
        self.hollow_btn.setToolTip("Open the Hollow settings over the 3D view, then hollow the parts "
                                   "you select (Ctrl+Shift+H)")
        self.drain_btn = QPushButton("Add drain holes…")
        self.drain_btn.setToolTip("Click on a part to drill drain holes so resin can escape "
                                  "(Ctrl+Shift+D)")
        self.trapped_btn = QPushButton("Check for voids")
        self.trapped_btn.setToolTip("Look for sealed cavities and suction cups (pockets open only "
                                    "toward the film) that would trap uncured resin; they are shown "
                                    "in red")
        self.seal_btn = QPushButton("Seal cavity")
        self.seal_btn.setToolTip("Fill every sealed cavity, making the part solid there instead of "
                                 "hollow (Edit → Undo brings it back)")
        self.trapped_result = QLabel()
        self.trapped_result.setWordWrap(True)
        self.trapped_result.hide()
        hl.addLayout(self._button_grid([self.hollow_btn, self.drain_btn, self.trapped_btn, self.seal_btn]))
        hl.addWidget(self.trapped_result)

        # ---- Supports
        sup = QGroupBox("Supports")
        sl = QVBoxLayout(sup)
        self.support_type = QComboBox()
        self.support_type.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.support_type.setMinimumContentsLength(16)
        self.support_type.setToolTip("The type of supports Generate supports will build")
        self.support_note = QLabel()
        self.support_note.setWordWrap(True)
        self.generate_btn = QPushButton("Generate supports")
        self.remove_btn = QPushButton("Remove supports")
        self.remove_btn.setToolTip("Remove the supports of the selected parts (all parts if none "
                                   "is selected); Edit → Undo brings them back")
        self.islands_btn = QPushButton("Detect islands")
        self.fix_btn = QPushButton("Fix islands")
        self.fix_btn.setToolTip("Detect unsupported islands and add a support under each one "
                                "(parts without supports get a full set of the type above)")
        self.islands_result = QLabel()
        self.islands_result.setWordWrap(True)
        self.islands_result.hide()
        self.tips_label = QLabel()
        self.tips_label.setWordWrap(True)
        self.tips_btn = QPushButton("Tip settings…")
        self.tips_btn.setToolTip("How supports connect to the model: tip shape, contact diameter, "
                                 "length, penetration and break point")
        form = QFormLayout()
        sl.addLayout(form)
        form.addRow("Support type", self.support_type)
        trow = QHBoxLayout()
        form.addRow("Tips", trow)
        trow.addWidget(self.tips_label, 1)
        trow.addWidget(self.tips_btn)
        sl.addWidget(self.support_note)
        sl.addLayout(self._button_grid([self.generate_btn, self.remove_btn, self.islands_btn, self.fix_btn]))
        sl.addWidget(self.islands_result)
        # every Hollowing and Supports button the same width: the widest one's
        same = [self.hollow_btn, self.drain_btn, self.trapped_btn, self.seal_btn,
                self.generate_btn, self.remove_btn, self.islands_btn, self.fix_btn]
        w = max(b.sizeHint().width() for b in same)
        for b in same:
            b.setMinimumWidth(w)

        # ---- Slicing
        sli = QGroupBox("Slicing")
        vl = QVBoxLayout(sli)
        self.slice_btn = QPushButton("Slice")
        self.slice_btn.setToolTip("Slice everything on the build plate and keep the result in memory "
                                  "for Export and Send to printer")
        self.gpu_box = QCheckBox("Use the GPU (experimental)")
        self.gpu_box.setToolTip("Draw the layers on the graphics card instead of the CPU: several times "
                                "faster. Edge pixels can differ slightly from the CPU slicer (which "
                                "matches SatelLite), and models with holes in their mesh may slice "
                                "wrongly. Falls back to the CPU if the GPU can't be used.")
        self.gpu_box.setChecked(win.gpu_slicing())
        self.gpu_box.toggled.connect(win.set_gpu_slicing)
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip("Save the sliced plate held in memory as a .goo file")
        self.send_btn = QPushButton("Send to printer")
        self.send_btn.setToolTip("Upload the sliced plate held in memory to the connected printer "
                                 "and start the print")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()
        self.results = QWidget()
        rf = QFormLayout(self.results)
        rf.setContentsMargins(0, 0, 0, 0)
        self.result = {k: QLabel("–") for k in
                       ("Print time", "Volume", "Weight", "Layers", "Layer thickness", "Sliced on")}
        for k, w in self.result.items():
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            if k != "Sliced on":
                rf.addRow(k, w)
        # "GPU, 1.4 s" with an (i) to the right: the device in a pop-up
        self.backend_btn = InfoButton("Slicing hardware")
        self.backend_btn.clicked.connect(self.show_backend_details)
        brow = QHBoxLayout()
        brow.setContentsMargins(0, 0, 0, 0)
        brow.addWidget(self.result["Sliced on"], 1)
        brow.addWidget(self.backend_btn)
        rf.addRow("Sliced on", brow)
        self.backend_row = brow
        self.backend_details = QFrame(self, Qt.Popup)
        self.backend_details.setFrameShape(QFrame.StyledPanel)
        bf = QFormLayout(self.backend_details)
        bf.setContentsMargins(12, 10, 12, 10)
        self.backend_title = QLabel("Slicing hardware")
        f = self.backend_title.font()
        f.setBold(True)
        self.backend_title.setFont(f)
        bf.addRow(self.backend_title)
        self.backend_info = {k: QLabel("–") for k in ("Processor", "Device", "Time")}
        for k, w in self.backend_info.items():
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            bf.addRow(k, w)
        self.results.hide()
        vl.addWidget(self.slice_btn)
        vl.addWidget(self.gpu_box)
        out = QHBoxLayout()                      # progress is in the status bar while slicing
        out.addWidget(self.export_btn, 1)
        out.addWidget(self.send_btn, 1)
        vl.addLayout(out)
        self._update_icons()
        vl.addWidget(self.status)
        vl.addWidget(self.results)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 0, 9, 9)
        lay.addWidget(hol)
        lay.addWidget(sup)
        lay.addWidget(sli)

        self.hollow_btn.clicked.connect(lambda: self.win.hollow_dialog())
        self.drain_btn.clicked.connect(lambda: self.win.drain_hole_mode())
        self.trapped_btn.clicked.connect(self.check_trapped_resin)
        self.seal_btn.clicked.connect(self.seal_cavities)
        self.support_type.currentIndexChanged.connect(self._type_picked)
        self.generate_btn.clicked.connect(self.generate)
        self.tips_btn.clicked.connect(self.edit_tips)
        self.remove_btn.clicked.connect(lambda: self.win.clear_supports())
        self.islands_btn.clicked.connect(self.check_islands)
        self.fix_btn.clicked.connect(self.fix_islands)
        self.slice_btn.clicked.connect(self._slice_clicked)
        self.export_btn.clicked.connect(lambda: self.win.export_held())
        self.send_btn.clicked.connect(lambda: self.win.send_held())
        self._set_held(False)
        self.refresh(reload=True)

    # ---------------------------------------------------------------- supports
    def _targets(self) -> list:
        sel = [p for p in (self.win.scene.get(i) for i in self.win.selected_ids()) if p]
        return sel or list(self.win.scene.parts)

    def _height(self, parts) -> float:
        return max((float(np.ptp(p.world_vertices()[:, 2])) for p in parts), default=0.0)

    def refresh(self, reload: bool = False) -> None:
        """Follow the selection; with reload, re-read the templates (printer change). The user's
        pick is kept while it exists, otherwise SatelLite's height rule picks the template."""
        if reload:
            self._templates = self.win.support_templates()
        parts = self._targets()
        h = self._height(parts)
        choices = support_choices(self._templates)
        labels = [c[0] for c in choices]
        if self._user_choice in labels:
            want = self._user_choice
        else:
            rec = recommended_choice(self._templates, h)
            want = rec.name if rec is not None else labels[0]
        c = self.support_type
        c.blockSignals(True)
        if [c.itemText(i) for i in range(c.count())] != labels:
            c.clear()
            for label, data in choices:
                c.addItem(label, data)
        c.setCurrentIndex(max(0, labels.index(want) if want in labels else 0))
        c.blockSignals(False)
        self._update_support_text(parts, h)

    def tips(self) -> dict:
        """The user's tip overrides (08 §11)."""
        return load_tips(self.win.settings)

    def params(self):
        """(SupportParams, template name) of the picked support type with the tip overrides."""
        return support_params(self.support_type.currentData(), self.tips())

    def edit_tips(self) -> None:
        base, _ = support_params(self.support_type.currentData())
        dlg = TipSettingsDialog(base, self.tips(), self)
        if dlg.exec() == TipSettingsDialog.Accepted:
            save_tips(self.win.settings, dlg.overrides())
            self._update_support_text()

    def _update_support_text(self, parts=None, h=None) -> None:
        parts = self._targets() if parts is None else parts
        h = self._height(parts) if h is None else h
        n = len(parts)
        if not n:
            target = ""
        elif self.win.selected_ids():
            target = "the selected part" if n == 1 else f"the {n} selected parts"
        else:
            target = "the part on the plate" if n == 1 else f"all {n} parts"
        note = support_note(self.support_type.currentData(), h, bool(self._templates))
        # the button keeps one short label (the Hollowing and Supports buttons share a width)
        self.support_note.setText(f"{note} Supports go on {target}." if target else note)
        tips = self.tips()
        self.tips_label.setText(tip_summary(self.params()[0], bool(tips)))
        self.hollow_btn.setEnabled(n > 0)          # opens the Hollow panel; parts are picked there
        self.drain_btn.setEnabled(n > 0)
        self.trapped_btn.setEnabled(n > 0 and not self._trapped_busy)
        self.seal_btn.setEnabled(n > 0 and not self._trapped_busy)
        self.generate_btn.setToolTip(f"Generate supports for {target}" if target else
                                     "Generate supports (import a model first)")
        self.generate_btn.setEnabled(n > 0)
        if self.islands_btn.isEnabled():
            self.fix_btn.setEnabled(bool(self.win.scene.parts))
        self.remove_btn.setEnabled(any(p.supports is not None for p in parts))

    @staticmethod
    def _button_grid(buttons) -> QGridLayout:
        """Two equal columns: the buttons' widths ignore their text, so every column matches."""
        g = QGridLayout()
        for n, b in enumerate(buttons):
            b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
            g.addWidget(b, n // 2, n % 2)
        g.setColumnStretch(0, 1)
        g.setColumnStretch(1, 1)
        return g

    def support_choice(self) -> str | None:
        """The support type picked by the user, or None to let the height rule pick."""
        return self._user_choice

    def set_support_choice(self, label: str | None) -> None:
        self._user_choice = label or None
        if label:
            self.win.settings.setValue("supportType", label)
        else:
            self.win.settings.remove("supportType")
        self.refresh()

    def _type_picked(self, *_):
        self._user_choice = self.support_type.currentText()
        self.win.settings.setValue("supportType", self._user_choice)
        self._update_support_text()

    def generate(self) -> None:
        parts = self._targets()
        if not parts:
            return
        params, name = self.params()
        for p in parts:
            self.win.auto_support(p, params, name)

    # ---------------------------------------------------------------- trapped resin
    def _trapped_idle(self) -> None:
        self._trapped_busy = False
        self.trapped_btn.setEnabled(bool(self.win.scene.parts))
        self.seal_btn.setEnabled(bool(self.win.scene.parts))

    def check_trapped_resin(self) -> None:
        self._trapped_busy = True
        self.trapped_btn.setEnabled(False)
        self.seal_btn.setEnabled(False)
        self.trapped_result.setText("Checking…")
        self.trapped_result.show()

        def done(cav):
            self._trapped_idle()
            if cav is None:
                self.trapped_result.setText("The check failed; see the Log.")
            elif not cav:
                self.trapped_result.setText("✓ No sealed cavities or suction cups: resin can drain from "
                                            "every part.")
            else:
                self.trapped_result.setText(
                    f"⚠ {flawmod.trapped_summary(cav)} would trap uncured resin, shown in red. "
                    + flawmod.trapped_fix(cav) + "; details are in the Log.")
        self.win.check_trapped_resin_async(done)

    def seal_cavities(self) -> None:
        self._trapped_busy = True
        self.trapped_btn.setEnabled(False)
        self.seal_btn.setEnabled(False)
        self.trapped_result.setText("Sealing cavities…")
        self.trapped_result.show()

        def done(msg: str):
            self._trapped_idle()
            self.trapped_result.setText(msg)
            self.trapped_result.show()
        self.win.seal_cavities(done)

    def flaws_changed(self, kind: str, left: int) -> None:
        """A flaw was dismissed in the Flaws tab: update that check's result."""
        label = self.islands_result if kind == "island" else self.trapped_result
        if label.isHidden():
            return
        what = ("unsupported island", "unsupported islands") if kind == "island" \
            else ("trapped-resin flaw", "trapped-resin flaws")
        if left:
            label.setText(f"⚠ {left} {what[0] if left == 1 else what[1]} left, shown in red (the rest "
                          "were dismissed in Flaws).")
        else:
            label.setText(f"✓ No {what[1]} left: the rest were dismissed as false positives in Flaws.")

    def cavities_out_of_date(self) -> None:
        if not self.trapped_result.isHidden():
            self.trapped_result.setText("A part changed since the trapped-resin check. Check again.")

    # ---------------------------------------------------------------- islands
    def check_islands(self) -> None:
        self.islands_btn.setEnabled(False)
        self.islands_result.setText("Checking…")
        self.islands_result.show()

        def done(isl):
            self.islands_btn.setEnabled(True)
            if isl is None:
                self.islands_result.setText("The check failed; see the Log.")
            elif not isl:
                self.islands_result.setText("✓ No unsupported islands: every region connects to the layer below.")
            else:
                self.islands_result.setText(f"⚠ {len(isl)} unsupported island{'s' if len(isl) != 1 else ''} "
                                            "would print in mid-air, shown in red. Details are in the "
                                            "Log (View → Logs); add supports there (Supports → Edit "
                                            "supports).")
        self.win.check_islands_async(done)

    def fix_islands(self) -> None:
        self.islands_btn.setEnabled(False)
        self.fix_btn.setEnabled(False)
        self.islands_result.setText("Checking for islands…")
        self.islands_result.show()
        params, name = self.params()

        def done(msg: str):
            self.islands_btn.setEnabled(True)
            self.fix_btn.setEnabled(bool(self.win.scene.parts))
            self.islands_result.setText(msg)
            self.islands_result.show()
        self.win.fix_islands(params, name, done)

    def islands_out_of_date(self) -> None:
        if not self.islands_result.isHidden():
            self.islands_result.setText("The plate changed since the island check. Check again.")

    # ---------------------------------------------------------------- icons
    def _update_icons(self) -> None:
        from .icons import floppy_icon, printer_icon
        pal = self.palette()
        dark = pal.color(pal.ColorRole.Window).lightness() < 128
        color = QColor("white") if dark else pal.color(pal.ColorRole.ButtonText)
        self.export_btn.setIcon(floppy_icon(color, dpr=self.devicePixelRatioF() or 1.0))
        self.send_btn.setIcon(printer_icon(color, dpr=self.devicePixelRatioF() or 1.0))

    def changeEvent(self, e):
        if e.type() == QEvent.PaletteChange and hasattr(self, "send_btn"):
            self._update_icons()               # theme switched
        super().changeEvent(e)

    # ---------------------------------------------------------------- slicing
    def _set_held(self, ok: bool) -> None:
        """Export and Send work on a slice held in memory that still matches the plate."""
        self.export_btn.setEnabled(ok)
        self.send_btn.setEnabled(ok)
    def _slice_clicked(self):
        if self.win.slicing():
            self.win.cancel_slice()
        else:
            self.win.slice_to_memory()

    def slicing_started(self, layers: int) -> None:
        self.slice_btn.setText("Cancel slicing")
        self.gpu_box.setEnabled(False)
        self.status.setText(f"Slicing {layers} layers…")
        self.status.show()
        self.results.hide()
        self._set_held(False)

    def slicing_progress(self, done: int, total: int) -> None:
        self.status.setText(f"Slicing… {done} / {total} layers")

    def slicing_finished(self) -> None:
        self.slice_btn.setText("Slice")
        self.gpu_box.setEnabled(True)

    def slicing_failed(self, msg: str) -> None:
        self.status.setText("Slicing cancelled." if msg == "cancelled" else f"Slicing failed: {msg}")
        self.status.show()

    def show_backend_details(self) -> None:
        show_popup(self.backend_details, self.backend_btn)

    def show_result(self, res, held: bool = True) -> None:
        r = self.result
        r["Print time"].setText(format_duration(res.print_time))
        r["Volume"].setText(f"{res.volume / 1000:.2f} ml")
        r["Weight"].setText(f"{res.weight:.2f} g")
        r["Layers"].setText(f"{res.layers:,}")
        r["Layer thickness"].setText(f"{res.layer_height * 1000:g} µm ({res.layer_height:g} mm)")
        r["Sliced on"].setText(self.win._backend_text(res))
        gpu = res.backend.startswith("gpu")
        self.backend_info["Processor"].setText("GPU (OpenGL)" if gpu else "CPU")
        self.backend_info["Device"].setText(self.win._backend_device(res))
        self.backend_info["Time"].setText(f"{res.seconds:.2f} s to make {res.layers:,} layers")
        self.results.show()
        if held:
            self.set_stale(False)
        else:
            self._set_held(False)
            self.status.setText(f"Saved to {res.path}")
            self.status.show()

    def set_stale(self, stale: bool) -> None:
        """The plate or settings changed since the held slice was made."""
        self._set_held(not stale and not self.win.slicing())
        if self.results.isHidden() and not stale:
            self.status.hide()
            return
        if stale:
            self.status.setText("The plate or print settings changed since this slice. Slice again "
                                "to update it.")
        else:
            self.status.setText("Sliced and held in memory: Export saves it, Send to printer uploads "
                                "it and starts the print.")
        self.status.show()
