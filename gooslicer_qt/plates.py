"""Build plates in the GUI (11 §3): the Plate menu, the current plate, spillover of new parts,
Arrange across plates, and Slice all plates."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import QObject
from PySide6.QtGui import QActionGroup
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .commands import AddPartsCommand, PlatesCommand


class PlateController(QObject):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.menu = None
        self.current_menu = None
        self.delete_action = None
        self._queue: list[tuple[int, str]] = []

    @property
    def scene(self):
        return self.win.scene

    # ------------------------------------------------------------------ menu
    def build_menu(self, mb) -> None:
        w = self.win
        m = self.menu = mb.addMenu("P&late")
        w._act(m, "plate.add", "&Add plate", self.add_plate, "Another build plate, to the right of the last")
        self.delete_action = w._act(m, "plate.delete", "&Delete plate", self.delete_plate,
                                    "Remove the current plate; its parts move to the other plates")
        self.current_menu = m.addMenu("&Current plate")
        m.addSeparator()
        w._act(m, "plate.arrange", "A&rrange all plates", w.arrange_all,
               "Pack every part onto plate 1, then 2, …, adding plates as needed")
        w._act(m, "plate.next", "&Next plate", lambda: self.step(1))
        w._act(m, "plate.previous", "&Previous plate", lambda: self.step(-1))
        m.aboutToShow.connect(self.update_menu)
        self.update_menu()

    def update_menu(self) -> None:
        if self.current_menu is None:
            return
        self.current_menu.clear()
        group = QActionGroup(self.current_menu)
        for i in range(self.scene.plates):
            n = len(self.scene.on_plate(i))
            a = self.current_menu.addAction(f"Plate {i + 1}  ({n} part{'' if n == 1 else 's'})",
                                            lambda _=False, k=i: self.win.set_current_plate(k))
            a.setCheckable(True)
            a.setChecked(i == self.win.current_plate)
            group.addAction(a)
        self.delete_action.setEnabled(self.scene.plates > 1)

    def step(self, d: int) -> None:
        self.win.set_current_plate((self.win.current_plate + d) % self.scene.plates)

    # --------------------------------------------------------------- edits
    def add_plate(self) -> None:
        n = self.scene.plates
        self.scene.plates = n + 1
        self.win.undo.push(PlatesCommand(self.win, (n, n + 1), {}, "Add plate",
                                         current=(self.win.current_plate, n)))
        self.win.set_current_plate(n)
        self.win.log_msg(f"Added plate {n + 1}.")

    def delete_plate(self, i: int | None = None) -> bool:
        w = self.win
        i = w.current_plate if i is None else int(i)
        n = self.scene.plates
        if n <= 1:
            w.log_msg("There must always be at least one build plate.")
            return False
        before = {p.id: p.state() for p in self.scene.parts}
        left = self.scene.delete_plate(i)
        changes = {p.id: (before[p.id], p.state()) for p in self.scene.parts if p.state() != before[p.id]}
        cur = min(i, n - 2)
        w.undo.push(PlatesCommand(w, (n, n - 1), changes, f"Delete plate {i + 1}",
                                  current=(w.current_plate, cur)))
        w.set_current_plate(cur)
        w.log_msg(f"Deleted plate {i + 1}."
                  + (f" No room for {', '.join(p.name for p in left)}: left beside the last plate."
                     if left else ""))
        return True

    def place_new(self, parts, text: str, near=None) -> None:
        """Add new parts as one undo step: each where `near` says on the current plate if
        there's room, else spilling to later plates and adding plates as needed (11 §3)."""
        w = self.win
        n0 = self.scene.plates
        placed = []
        for k, p in enumerate(parts):
            self.scene.parts.append(p)            # so later parts avoid it
            i, added = self.scene.place_spill(p, near=near if k == 0 else None, plate=w.current_plate)
            placed.append(p)
            if i != w.current_plate:
                w.log_msg(f"{p.name}: no room on plate {w.current_plate + 1}, placed on plate {i + 1}"
                          + (" (added)" if added else ""))
        for p in placed:
            self.scene.parts.remove(p)
        n1 = self.scene.plates
        if n1 != n0:
            w.undo.beginMacro(text)
            w.undo.push(PlatesCommand(w, (n0, n1), {}, "Add plates"))
            w.undo.push(AddPartsCommand(w, placed, text))
            w.undo.endMacro()
        else:
            w.undo.push(AddPartsCommand(w, placed, text))

    def arrange(self) -> bool:
        w = self.win
        n0 = self.scene.plates
        before = {p.id: p.state() for p in self.scene.parts}
        fits = self.scene.arrange()
        changes = {p.id: (before[p.id], p.state()) for p in self.scene.parts if p.state() != before[p.id]}
        n1 = self.scene.plates
        if changes or n1 != n0:
            w.undo.push(PlatesCommand(w, (n0, n1), changes, "Arrange"))
        if n1 != n0:
            w.log_msg(f"Arrange: added {n1 - n0} plate{'' if n1 - n0 == 1 else 's'} ({n1} in all).")
        if not fits:
            w.log_msg("Arrange: some parts are too large for a build plate.")
        return fits

    # --------------------------------------------------------------- slicing
    def slice_all(self) -> None:
        """Slice every plate with parts into <name>-plate<N>.goo, one after another."""
        w = self.win
        if w.slicing():
            return
        plates = [i for i in range(self.scene.plates) if self.scene.on_plate(i)]
        if not plates:
            QMessageBox.warning(w, "GooSlicer", "Nothing to slice — import a model first.")
            return
        try:
            jobs = {i: w.make_slice_job(i) for i in plates}
        except ValueError as e:
            QMessageBox.warning(w, "GooSlicer", str(e))
            return
        if not w._confirm_no_trapped_resin():
            return
        default = Path(w.settings.value("lastGooDir", str(Path.home()))) / f"{self.scene.parts[0].name}.goo"
        path, _ = QFileDialog.getSaveFileName(w, "Slice all plates: base name", str(default),
                                              "ELEGOO GOO (*.goo)")
        if not path:
            return
        base = Path(path)
        if base.suffix.lower() == ".goo":
            base = base.with_suffix("")
        w.settings.setValue("lastGooDir", str(base.parent))
        queue = [(i, str(base.with_name(f"{base.name}-plate{i + 1}.goo"))) for i in plates]
        everything = SimpleNamespace(meshes=[m for j in jobs.values() for m in getattr(j, "check_meshes", j.meshes)],
                                     settings=next(iter(jobs.values())).settings)

        def go():
            self._queue = queue[1:]
            self._jobs = jobs
            w.log_msg(f"Slicing {len(queue)} plates → {base.name}-plate*.goo")
            w.start_slice(jobs[queue[0][0]], queue[0][1])
        w._island_check_then(everything, go)

    def next_in_queue(self) -> bool:
        """After a slice finished: start the next plate of Slice all plates, if any."""
        if not self._queue:
            return False
        i, path = self._queue.pop(0)
        self.win.start_slice(self._jobs[i], path)
        return True

    def cancel_queue(self) -> None:
        self._queue = []
