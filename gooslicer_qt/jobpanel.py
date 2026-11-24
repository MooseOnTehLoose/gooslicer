"""Supports and Slicing sections of the Properties tab (below Mesh): pick the support type and
generate supports, check for unsupported islands, and slice the build area into memory with the
estimates shown underneath."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QComboBox, QFormLayout, QGroupBox, QLabel, QProgressBar, QPushButton,
                               QVBoxLayout, QWidget)

from .supporting import recommended_choice, support_choices, support_note, support_params


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
        form = QFormLayout()
        form.addRow("Support type", self.support_type)
        sl.addLayout(form)
        sl.addWidget(self.support_note)
        sl.addWidget(self.generate_btn)

        # ---- Slicing
        sli = QGroupBox("Slicing")
        vl = QVBoxLayout(sli)
        self.islands_btn = QPushButton("Check for unsupported islands")
        self.islands_result = QLabel()
        self.islands_result.setWordWrap(True)
        self.islands_result.hide()
        self.slice_btn = QPushButton("Slice")
        self.slice_btn.setToolTip("Slice everything on the build plate and keep the result in memory: "
                                  "File → Slice and save, and Slice and send, then use it without slicing again")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1)
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.hide()
        self.results = QWidget()
        rf = QFormLayout(self.results)
        rf.setContentsMargins(0, 0, 0, 0)
        self.result = {k: QLabel("–") for k in
                       ("Print time", "Volume", "Weight", "Layers", "Layer thickness")}
        for k, w in self.result.items():
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            rf.addRow(k, w)
        self.results.hide()
        vl.addWidget(self.islands_btn)
        vl.addWidget(self.islands_result)
        vl.addWidget(self.slice_btn)
        vl.addWidget(self.progress)
        vl.addWidget(self.status)
        vl.addWidget(self.results)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(9, 0, 9, 9)
        lay.addWidget(sup)
        lay.addWidget(sli)

        self.support_type.currentIndexChanged.connect(self._type_picked)
        self.generate_btn.clicked.connect(self.generate)
        self.islands_btn.clicked.connect(self.check_islands)
        self.slice_btn.clicked.connect(self._slice_clicked)
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

    def _update_support_text(self, parts=None, h=None) -> None:
        parts = self._targets() if parts is None else parts
        h = self._height(parts) if h is None else h
        self.support_note.setText(support_note(self.support_type.currentData(), h, bool(self._templates)))
        n = len(parts)
        if not n:
            target = ""
        elif self.win.selected_ids():
            target = " (selected part)" if n == 1 else f" ({n} selected parts)"
        else:
            target = " (1 part)" if n == 1 else f" (all {n} parts)"
        self.generate_btn.setText("Generate supports" + target)
        self.generate_btn.setEnabled(n > 0)

    def _type_picked(self, *_):
        self._user_choice = self.support_type.currentText()
        self.win.settings.setValue("supportType", self._user_choice)
        self._update_support_text()

    def generate(self) -> None:
        parts = self._targets()
        if not parts:
            return
        params, name = support_params(self.support_type.currentData())
        for p in parts:
            self.win.auto_support(p, params, name)

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
                                            "would print in mid-air. Details are in the Log; add supports "
                                            "there (Supports → Edit supports).")
        self.win.check_islands_async(done)

    # ---------------------------------------------------------------- slicing
    def _slice_clicked(self):
        if self.win.slicing():
            self.win.cancel_slice()
        else:
            self.win.slice_to_memory()

    def slicing_started(self, layers: int) -> None:
        self.slice_btn.setText("Cancel slicing")
        self.progress.setRange(0, max(1, layers))
        self.progress.setValue(0)
        self.status.setText(f"Slicing {layers} layers…")
        self.status.show()
        self.results.hide()

    def slicing_progress(self, done: int, total: int) -> None:
        self.progress.setRange(0, max(1, total))
        self.progress.setValue(done)

    def slicing_finished(self) -> None:
        self.slice_btn.setText("Slice")

    def slicing_failed(self, msg: str) -> None:
        self.progress.setValue(0)
        self.status.setText("Slicing cancelled." if msg == "cancelled" else f"Slicing failed: {msg}")
        self.status.show()

    def show_result(self, res, held: bool = True) -> None:
        self.progress.setRange(0, max(1, res.layers))
        self.progress.setValue(res.layers)
        r = self.result
        r["Print time"].setText(format_duration(res.print_time))
        r["Volume"].setText(f"{res.volume / 1000:.2f} ml")
        r["Weight"].setText(f"{res.weight:.2f} g")
        r["Layers"].setText(f"{res.layers:,}")
        r["Layer thickness"].setText(f"{res.layer_height * 1000:g} µm ({res.layer_height:g} mm)")
        self.results.show()
        if held:
            self.set_stale(False)
        else:
            self.status.setText(f"Saved to {res.path}")
            self.status.show()

    def set_stale(self, stale: bool) -> None:
        """The plate or settings changed since the held slice was made."""
        if self.results.isHidden() and not stale:
            self.status.hide()
            return
        if stale:
            self.status.setText("The plate or print settings changed since this slice. Slice again "
                                "to update it.")
        else:
            self.status.setText("Sliced and held in memory: File → Slice and save, and Slice and "
                                "send, use it without slicing again.")
        self.status.show()
