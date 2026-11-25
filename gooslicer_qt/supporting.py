"""Supports in the GUI: template dialog, per-part geometry cache with background rebuilds, undo."""
from __future__ import annotations

import json
from dataclasses import replace

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
                               QFormLayout, QGroupBox, QHBoxLayout, QLabel, QPushButton,
                               QRadioButton, QVBoxLayout)

from gooslicer import supports as S
from gooslicer.scene import prepare
from gooslicer.support_defaults import recommended_template

from . import workers

BUILTIN = "Built-in defaults (EVO 30-60 mm values)"
BUILTIN_TREE = "Built-in tree (Tree Medium values)"


def support_choices(templates) -> list[tuple[str, object]]:
    """The support types to offer: built-in EVO and tree values, then SatelLite's templates."""
    return [(BUILTIN, None), (BUILTIN_TREE, "tree")] + \
        [(t.name, t) for t in templates if t.family in ("evo", "tree")]


def recommended_choice(templates, max_height: float):
    """The template SatelLite's height rule picks (06 §1), or None for the built-in values."""
    return recommended_template([t for t in templates if t.family in ("evo", "tree")], max_height)


def support_note(t, max_height: float, have_templates: bool) -> str:
    if t is None:
        return "SatelLite's templates were not found; using the EVO 30-60 mm values." \
            if not have_templates else "Values of SatelLite's EVO 30-60 mm template."
    if t == "tree":
        return "Tree supports with the values of SatelLite's Tree Medium template."
    if t.family == "tree":
        return f"Tree supports. Tallest part {max_height:.1f} mm."
    txt = f"Tallest part {max_height:.1f} mm."
    if not t.fits_height(max_height):
        txt += f" “{t.name}” may cause printing failure at this height; " \
               "the recommended template is preselected."
    return txt


def support_params(t, tips: dict | None = None) -> tuple[S.SupportParams, str]:
    """(parameters, template name) for a support_choices() entry, with the user's tip
    overrides (08 §11) on top."""
    if t is None:
        p, name = S.SupportParams(), ""
    elif t == "tree":
        p, name = S.SupportParams.tree_defaults(), ""
    else:
        p, name = S.SupportParams.from_template(t), t.name
    return apply_tips(p, tips), name


# ------------------------------------------------------------------ tip settings (08 §11)
TIP_SHAPE_LABELS = {"cone": "Cone", "cylinder": "Cylinder", "cube": "Cube", "cross": "Cross"}
# field: (type, minimum, maximum); the order is the dialog's
TIP_FIELDS = {
    "tip_shape": (str, None, None), "tip_top_width": (float, 0.05, 5.0),
    "tip_bottom_width": (float, 0.05, 5.0), "tip_length": (float, 0.1, 20.0),
    "tip_in_part": (float, 0.0, 2.0), "tip_ball": (bool, None, None),
    "tip_ball_diameter": (float, 0.05, 5.0), "max_tip_angle": (float, 0.0, 85.0),
    "bot_tip_shape": (str, None, None), "bot_top_width": (float, 0.05, 5.0),
    "bot_in_part": (float, 0.0, 2.0), "bot_ball": (bool, None, None),
    "bot_ball_diameter": (float, 0.05, 5.0),
}
TIPS_KEY = "supportTips"


def clean_tips(d) -> dict:
    """Only known fields, of the right type and in range; anything else is dropped."""
    out = {}
    for k, v in (d or {}).items() if isinstance(d, dict) else ():
        if k not in TIP_FIELDS:
            continue
        typ, lo, hi = TIP_FIELDS[k]
        if typ is str:
            if v in S.TIP_SHAPES:
                out[k] = v
        elif typ is bool:
            if isinstance(v, bool):
                out[k] = v
        elif isinstance(v, (int, float)) and not isinstance(v, bool):
            out[k] = float(min(max(v, lo), hi))
    return out


def apply_tips(p: S.SupportParams, tips: dict | None) -> S.SupportParams:
    t = clean_tips(tips)
    return replace(p, **t) if t else p


def load_tips(settings) -> dict:
    try:
        return clean_tips(json.loads(settings.value(TIPS_KEY, "") or "{}"))
    except (TypeError, ValueError):
        return {}


def save_tips(settings, tips: dict) -> None:
    tips = clean_tips(tips)
    if tips:
        settings.setValue(TIPS_KEY, json.dumps(tips, sort_keys=True))
    else:
        settings.remove(TIPS_KEY)


def tip_summary(p: S.SupportParams, custom: bool) -> str:
    """One line for the panel: shape, contact diameter, break point."""
    txt = f"{TIP_SHAPE_LABELS.get(p.tip_shape, p.tip_shape)}, {p.tip_top_width:g} mm"
    if p.tip_ball:
        txt += f", break point {p.tip_ball_diameter:g} mm"
    return txt + (" (custom)" if custom else "")


class TipSettingsDialog(QDialog):
    """How supports meet the model (08 §11): shown with the support type's values (`base`)
    plus the saved overrides; `overrides()` returns the fields that differ from `base`."""

    def __init__(self, base: S.SupportParams, tips: dict | None, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Tip settings")
        self.base = base
        lay = QVBoxLayout(self)
        note = QLabel("How supports connect to the model. Changed values apply on top of every "
                      "support type until you use the template values again.")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.w: dict = {}
        top = QGroupBox("Tip (on overhangs)")
        lay.addWidget(top)
        f = QFormLayout(top)
        self._shape(f, "tip_shape", "Shape", "Cone narrows from the support to the contact; "
                    "Cylinder keeps the contact diameter all the way; Cube gives a square mark; "
                    "Cross gives a thin \u201c+\u201d mark that is easy to sand off")
        self._num(f, "tip_top_width", "Contact diameter", " mm", "Width where the tip touches the "
                  "model: bigger holds better but leaves a bigger mark")
        self._num(f, "tip_bottom_width", "Support end diameter", " mm", "Width where the tip meets "
                  "the support (not used by Cylinder)")
        self._num(f, "tip_length", "Length", " mm", "From the support to the model")
        self._num(f, "tip_in_part", "Penetration", " mm", "How far the tip sinks into the model")
        self._ball(f, "tip_ball", "tip_ball_diameter")
        self._num(f, "max_tip_angle", "Max tilt", "°", "Tips lean along the surface normal, "
                  "up to this angle from vertical, so they meet sloped surfaces square on", 0)
        bot = QGroupBox("Base tip (supports that land on the model)")
        lay.addWidget(bot)
        g = QFormLayout(bot)
        self._shape(g, "bot_tip_shape", "Shape", "Shape of the tip where a support stands on the model")
        self._num(g, "bot_top_width", "Contact diameter", " mm", "Width where it touches the model")
        self._num(g, "bot_in_part", "Penetration", " mm", "How far it sinks into the model")
        self._ball(g, "bot_ball", "bot_ball_diameter")
        self.set_values(apply_tips(base, tips))
        self.reset_btn = QPushButton("Use template values")
        self.reset_btn.setToolTip("Clear every change and use the support type's own tip values")
        self.reset_btn.clicked.connect(lambda: self.set_values(self.base))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        row = QHBoxLayout()
        lay.addLayout(row)
        row.addWidget(self.reset_btn)
        row.addStretch(1)
        row.addWidget(bb)

    def _shape(self, f, key, label, tip):
        c = QComboBox()
        f.addRow(label, c)
        for k, v in TIP_SHAPE_LABELS.items():
            c.addItem(v, k)
        c.setToolTip(tip)
        self.w[key] = c

    def _num(self, f, key, label, suffix, tip, decimals=3):
        _, lo, hi = TIP_FIELDS[key]
        b = QDoubleSpinBox()
        f.addRow(label, b)
        b.setRange(lo, hi)
        b.setDecimals(decimals)
        b.setSingleStep(0.05 if decimals else 5)
        b.setSuffix(suffix)
        b.setToolTip(tip)
        self.w[key] = b

    def _ball(self, f, key, dkey):
        row = QHBoxLayout()
        f.addRow("Break point", row)
        box = QCheckBox()
        row.addWidget(box)
        _, lo, hi = TIP_FIELDS[dkey]
        b = QDoubleSpinBox()
        row.addWidget(b, 1)
        b.setRange(lo, hi)
        b.setDecimals(3)
        b.setSingleStep(0.05)
        b.setSuffix(" mm")
        tip = ("A ball on the contact, wider than the tip: the support snaps at the neck and "
               "leaves a small cap that is easy to sand")
        box.setToolTip(tip)
        b.setToolTip("Ball diameter; make it larger than the contact diameter")
        box.toggled.connect(b.setEnabled)
        self.w[key], self.w[dkey] = box, b

    def set_values(self, p: S.SupportParams) -> None:
        for k, wd in self.w.items():
            v = getattr(p, k)
            if isinstance(wd, QComboBox):
                wd.setCurrentIndex(max(0, wd.findData(v)))
            elif isinstance(wd, QCheckBox):
                wd.setChecked(bool(v))
            else:
                wd.setValue(float(v))
        for k in ("tip_ball", "bot_ball"):
            self.w[k + "_diameter"].setEnabled(self.w[k].isChecked())

    def values(self) -> dict:
        out = {}
        for k, wd in self.w.items():
            if isinstance(wd, QComboBox):
                out[k] = wd.currentData()
            elif isinstance(wd, QCheckBox):
                out[k] = wd.isChecked()
            else:
                out[k] = round(wd.value(), 4)
        return out

    def overrides(self) -> dict:
        out = {}
        for k, v in self.values().items():
            b = getattr(self.base, k)
            if abs(v - b) > 5e-4 if isinstance(v, float) else v != b:
                out[k] = v
        return out


class SupportDialog(QDialog):
    """Pick a SatelLite template (from GooSlicer's copy of the user's install) or the built-in values.
    The recommended template follows SatelLite's height rule (06 §1)."""

    def __init__(self, templates, max_height: float, has_selection: bool, parent=None,
                 tips: dict | None = None):
        super().__init__(parent)
        self.tips = tips
        self.setWindowTitle("Auto-support")
        self.templates = [t for t in templates if t.family in ("evo", "tree")]
        self.combo = QComboBox()
        for label, data in support_choices(self.templates):
            self.combo.addItem(label, data)
        rec = recommended_template(self.templates, max_height)
        if rec is not None:
            self.combo.setCurrentIndex(self.templates.index(rec) + 2)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.combo.currentIndexChanged.connect(lambda _: self._update_note(max_height))
        self._update_note(max_height)
        self.sel = QRadioButton("Selected parts")
        self.all = QRadioButton("All parts")
        (self.sel if has_selection else self.all).setChecked(True)
        self.sel.setEnabled(has_selection)
        f = QFormLayout()
        f.addRow("Template", self.combo)
        f.addRow(self.note)
        v = QVBoxLayout(self)
        v.addLayout(f)
        v.addWidget(self.sel)
        v.addWidget(self.all)
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        v.addWidget(bb)

    def _update_note(self, h):
        self.note.setText(support_note(self.combo.currentData(), h, bool(self.templates)))

    def params(self) -> tuple[S.SupportParams, str]:
        return support_params(self.combo.currentData(), self.tips)

    def apply_to_all(self) -> bool:
        return self.all.isChecked()


class SupportsCommand(QUndoCommand):
    """Set a part's support plan and elevation (with the matching placement)."""

    def __init__(self, win, pid: int, before: tuple, after: tuple, text: str):
        super().__init__(text)
        self.win, self.pid, self.before, self.after = win, pid, before, after

    @staticmethod
    def snapshot(part) -> tuple:
        return (part.supports, part.elevation, part.state())

    def _apply(self, snap):
        p = self.win.scene.get(self.pid)
        if p is None:
            return
        p.supports, elevation, st = snap
        p.set_state(st)
        p.elevation = elevation
        self.win.scene_changed()

    def redo(self):
        self._apply(self.after)

    def undo(self):
        self._apply(self.before)


def _ready(res: S.SupportResult) -> S.SupportResult:
    """Get the support mesh ready for the 3D view (on the worker thread)."""
    prepare(res.mesh)
    return res


class _Entry:
    def __init__(self, key, result: S.SupportResult, pos: np.ndarray):
        self.key, self.result, self.pos = key, result, pos


class SupportManager(QObject):
    """World-space support geometry per part, rebuilt in the background when the plan, mesh,
    rotation, scale or height changes. A pure XY move just offsets the cached geometry."""
    updated = Signal()
    fixedReplaced = Signal(str, str)    # part name, source: imported supports gave way to GooSlicer's

    def __init__(self, scene):
        super().__init__()
        self.scene = scene
        self.cache: dict[int, _Entry] = {}
        self._pending: dict[int, object] = {}      # pid -> key being built
        self._dirty: set[int] = set()
        self._replaced: set[int] = set()        # ids of plans whose fixed geometry was reported replaced

    @staticmethod
    def key(part):
        return (part.rotation.tobytes(), part.scale.tobytes(), round(float(part.position[2]), 6),
                id(part.mesh), id(part.supports))

    def busy(self) -> bool:
        return bool(self._pending)

    def entry(self, part) -> _Entry | None:
        e = self.cache.get(part.id)
        return e if e is not None and part.supports is not None and e.key == self.key(part) else None

    def offset(self, part, e: _Entry) -> np.ndarray:
        d = part.position - e.pos
        return np.array([d[0], d[1], 0.0])

    def display(self) -> list[tuple]:
        """(mesh, 4x4 matrix) for every part's supports that are ready (stale ones still show
        until their rebuild finishes)."""
        out = []
        for p in self.scene.parts:
            if p.supports is None:
                continue
            e = self.cache.get(p.id)
            if e is None or e.result.mesh is None:
                continue
            M = np.eye(4)
            M[:3, 3] = self.offset(p, e)
            out.append((e.result.mesh, M))
        return out

    def sync(self) -> None:
        """Drop entries of removed/unsupported parts; schedule rebuilds of stale ones."""
        live = {p.id: p for p in self.scene.parts if p.supports is not None}
        for pid in list(self.cache):
            if pid not in live:
                self.cache.pop(pid)
        for pid, p in live.items():
            if self.entry(p) is None:
                self.schedule(p)

    def schedule(self, part) -> None:
        k = self.key(part)
        fr = S.fixed_result(part)
        if fr is not None:                       # exact geometry from SatelLite: nothing to build
            self._pending.pop(part.id, None)
            self.cache[part.id] = _Entry(k, fr[0], fr[1])
            self.updated.emit()
            return
        if part.supports.fixed is not None and id(part.supports) not in self._replaced:
            self._replaced.add(id(part.supports))
            self.fixedReplaced.emit(part.name, part.supports.fixed.source)
        if part.id in self._pending:
            if self._pending[part.id] != k:
                self._dirty.add(part.id)
            return
        self._pending[part.id] = k
        model = part.world_mesh()
        contacts = part.supports.world_contacts(part)
        params = part.supports.params
        pos = part.position.copy()
        pid = part.id

        def done(res):
            self._pending.pop(pid, None)
            self.cache[pid] = _Entry(k, res, pos)
            if pid in self._dirty:
                self._dirty.discard(pid)
                p = self.scene.get(pid)
                if p is not None and p.supports is not None and self.key(p) != k:
                    self.schedule(p)
            self.updated.emit()

        def failed(msg):
            self._pending.pop(pid, None)
            self._dirty.discard(pid)
            self.updated.emit()
        workers.submit_process(S.build_supports, model, contacts, params, then=_ready,
                               on_done=done, on_failed=failed)

    def prime(self, pid: int, frame, result: S.SupportResult) -> None:
        """Store geometry built for `frame` (a Part-like snapshot with the final placement/plan)."""
        self.cache[pid] = _Entry(self.key(frame), result, frame.position.copy())

    def ensure(self, part) -> _Entry | None:
        """Up-to-date geometry, built synchronously if needed (slicing)."""
        if part.supports is None:
            return None
        e = self.entry(part)
        if e is None and (fr := S.fixed_result(part)) is not None:
            e = _Entry(self.key(part), fr[0], fr[1])
            self.cache[part.id] = e
        if e is None:
            res = S.build_supports(part.world_mesh(), part.supports.world_contacts(part), part.supports.params)
            e = _Entry(self.key(part), res, part.position.copy())
            self.cache[part.id] = e
        return e

    def world_mesh(self, part):
        e = self.ensure(part)
        if e is None or e.result.mesh is None:
            return None
        m = e.result.mesh.copy()
        m.apply_translation(self.offset(part, e))
        return m
