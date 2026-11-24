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
    """Replace a part's mesh (e.g. repair), keeping placement."""

    def __init__(self, win, pid: int, new_mesh, text="Repair mesh"):
        super().__init__(text)
        p = win.scene.get(pid)
        self.win, self.pid = win, pid
        self.old = (p.mesh, p.state())
        p.set_mesh(new_mesh)
        self.new = (p.mesh, p.state())
        p.mesh, _ = self.old
        p.set_state(self.old[1])

    def _apply(self, mesh, state):
        p = self.win.scene.get(self.pid)
        p.mesh = mesh
        p.set_state(state)
        self.win.scene_changed(mesh_changed=self.pid)

    def redo(self):
        self._apply(*self.new)

    def undo(self):
        self._apply(*self.old)
