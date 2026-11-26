"""Undoable scene edits (QUndoCommand)."""
from __future__ import annotations

from PySide6.QtGui import QUndoCommand

from gooslicer.scene import Part, PartState


class StateCommand(QUndoCommand):
    """Set transforms of parts: {id: (before, after)}."""

    def __init__(self, win, changes: dict[int, tuple[PartState, PartState]], text: str, applied=True):
        super().__init__(text)
        self.win, self.changes = win, {k: (a.copy(), b.copy()) for k, (a, b) in changes.items()}
        self._skip = applied  # first redo is a no-op when the change is already applied

    def _apply(self, idx: int):
        for pid, pair in self.changes.items():
            p = self.win.scene.get(pid)
            if p:
                p.set_state(pair[idx])
        self.win.scene_changed()

    def redo(self):
        if self._skip:
            self._skip = False
            self.win.scene_changed()
            return
        self._apply(1)

    def undo(self):
        self._apply(0)


class AddPartsCommand(QUndoCommand):
    def __init__(self, win, parts: list[Part], text="Add parts"):
        super().__init__(text)
        self.win, self.parts = win, parts

    def redo(self):
        for p in self.parts:
            if not self.win.scene.get(p.id):
                self.win.scene.add(p, place=False)
        self.win.scene_changed(select=[p.id for p in self.parts])

    def undo(self):
        for p in self.parts:
            if self.win.scene.get(p.id):
                self.win.scene.remove(p.id)
        self.win.scene_changed(select=[])


class RemovePartsCommand(QUndoCommand):
    def __init__(self, win, ids: list[int], text="Delete"):
        super().__init__(text)
        self.win, self.ids, self.removed = win, ids, []

    def redo(self):
        self.removed = [self.win.scene.remove(i) for i in self.ids if self.win.scene.get(i)]
        self.win.scene_changed(select=[])

    def undo(self):
        for p, idx in reversed(self.removed):
            self.win.scene.add(p, place=False, index=idx)
        self.win.scene_changed(select=[p.id for p, _ in self.removed])


class MeshCommand(QUndoCommand):
    """Replace a part's mesh (e.g. repair), keeping placement; optionally set its 3D-view
    opacity too (Hollow makes it see-through), which undo puts back."""

    def __init__(self, win, pid: int, new_mesh, text="Repair mesh", opacity: float | None = None,
                 repaired: bool | None = None):
        super().__init__(text)
        p = win.scene.get(pid)
        self.win, self.pid = win, pid
        self.old = (p.mesh, p.state(), p.opacity, p.repaired)
        p.set_mesh(new_mesh)
        self.new = (p.mesh, p.state(), p.opacity if opacity is None else float(opacity),
                    p.repaired if repaired is None else bool(repaired))
        p.mesh = self.old[0]
        p.set_state(self.old[1])

    def _apply(self, mesh, state, opacity, repaired):
        p = self.win.scene.get(self.pid)
        p.mesh = mesh
        p.set_state(state)
        p.opacity, p.repaired = opacity, repaired
        self.win.scene_changed(mesh_changed=self.pid)

    def redo(self):
        self._apply(*self.new)

    def undo(self):
        self._apply(*self.old)


class OpacityCommand(QUndoCommand):
    """Draw parts see-through at an opacity, or solid again (1.0)."""

    def __init__(self, win, ids, opacity: float):
        super().__init__("See-through" if opacity < 1.0 else "Solid view")
        self.win, self.opacity = win, float(opacity)
        self.before = {i: win.scene.get(i).opacity for i in ids if win.scene.get(i)}

    def _set(self, vals: dict):
        for i, v in vals.items():
            p = self.win.scene.get(i)
            if p is not None:
                p.opacity = v
        self.win.viewport.update()

    def redo(self):
        self._set({i: self.opacity for i in self.before})

    def undo(self):
        self._set(self.before)


def mirrored(part: Part, axis: int = 1):
    """(mesh, state, supports) of `part` mirrored across the world plane through its position
    perpendicular to `axis`. The stored mesh is mirrored along local Y and the rotation becomes
    Mw·R·My (a proper rotation again), so scale stays positive: Mw·(R·S·v) = (Mw·R·My)·S·(My·v)."""
    import dataclasses

    import numpy as np
    import trimesh
    my = np.array([1.0, -1.0, 1.0])
    mw = np.ones(3)
    mw[axis] = -1.0
    v = np.asarray(part.mesh.vertices) * my
    mesh = trimesh.Trimesh(v, np.asarray(part.mesh.faces)[:, ::-1], process=False)  # keep outward faces
    st = part.state()
    st.rotation = np.diag(mw) @ part.rotation @ np.diag(my)
    sup = part.supports
    if sup is not None:
        sup = dataclasses.replace(sup, points=np.asarray(sup.points) * my, normals=np.asarray(sup.normals) * my)
    return mesh, st, sup


class MirrorCommand(QUndoCommand):
    """Mirror a part (geometry, placement and supports) across a world axis plane."""

    def __init__(self, win, pid: int, axis: int = 1, text: str = "Mirror"):
        super().__init__(text)
        p = win.scene.get(pid)
        self.win, self.pid = win, pid
        self.old = (p.mesh, p.state(), p.supports)
        self.new = mirrored(p, axis)
        rep = win.reports.get(id(p.mesh))
        if rep is not None:                       # same checks result: a mirror image
            win.reports[id(self.new[0])] = rep

    def _apply(self, mesh, state, supports):
        p = self.win.scene.get(self.pid)
        if p is None:
            return
        p.mesh, p.supports = mesh, supports
        p.set_state(state)
        self.win.scene_changed(mesh_changed=self.pid)

    def redo(self):
        self._apply(*self.new)

    def undo(self):
        self._apply(*self.old)
