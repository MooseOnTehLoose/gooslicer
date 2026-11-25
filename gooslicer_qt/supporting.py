"""Supports in the GUI: template dialog, per-part geometry cache with background rebuilds, undo."""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QUndoCommand
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QLabel,
                               QRadioButton, QVBoxLayout)

from gooslicer import supports as S
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


def support_params(t) -> tuple[S.SupportParams, str]:
    """(parameters, template name) for a support_choices() entry."""
    if t is None:
        return S.SupportParams(), ""
    if t == "tree":
        return S.SupportParams.tree_defaults(), ""
    return S.SupportParams.from_template(t), t.name


class SupportDialog(QDialog):
    """Pick a SatelLite template (from GooSlicer's copy of the user's install) or the built-in values.
    The recommended template follows SatelLite's height rule (06 §1)."""

    def __init__(self, templates, max_height: float, has_selection: bool, parent=None):
        super().__init__(parent)
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
        return support_params(self.combo.currentData())

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
        p.supports, p.elevation, st = snap
        p.set_state(st)
        self.win.scene_changed()

    def redo(self):
        self._apply(self.after)

    def undo(self):
        self._apply(self.before)


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
        workers.submit(S.build_supports, model, contacts, params, on_done=done, on_failed=failed)

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
