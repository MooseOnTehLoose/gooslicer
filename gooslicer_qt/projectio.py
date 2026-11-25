"""Project commands for the main window: New, Open, Open recent, Save, Save as, the
unsaved-changes prompt and the title bar (09 §4–5). The file format is gooslicer.project."""
from __future__ import annotations

from pathlib import Path

import numpy as np
from PySide6.QtCore import QBuffer, QIODevice, QObject, Qt, Signal
from PySide6.QtWidgets import QFileDialog, QMenu, QMessageBox

from gooslicer import __version__
from gooslicer import mesh as meshmod
from gooslicer import project as projmod
from gooslicer.scene import Part

from . import workers

APP = "GooSlicer"
PROJECT_FILTER = "GooSlicer projects and 3MF files (*.3mf *.3MF);;All files (*)"
RECENT_KEY = "recent/projects"
RECENT_MAX_KEY = "recent/max"
RECENT_DEFAULT = 10
THUMB_PX = 256
SAVE, DISCARD, CANCEL = "save", "discard", "cancel"


def load_project(path: str):
    """Worker: read the project and check each distinct mesh, as an import does."""
    p = projmod.load(path)
    reports = {}
    for part in p.parts:
        if id(part.mesh) not in reports:
            reports[id(part.mesh)] = meshmod.analyze(part.mesh)
            meshmod.slim(part.mesh)
    return p, reports


def _snapshot_parts(parts: list[Part]) -> list[Part]:
    """Copies of the parts' placement for the save thread (meshes and plans are never changed in
    place, so they are shared)."""
    return [Part(p.name, p.mesh, p.position.copy(), p.rotation.copy(), p.scale.copy(),
                 elevation=p.elevation, supports=p.supports) for p in parts]


def _save_job(path, project, supports, thumb, progress=None):
    meshes = {}
    for pid, (mesh, offset) in supports.items():
        m = mesh.copy()
        m.apply_translation(offset)
        meshes[pid] = m
    projmod.save(path, project, support_meshes=meshes, thumbnail=thumb,
                 progress=(lambda f: progress(f"{f:.0%}")) if progress else None)
    return path


class ProjectController(QObject):
    opened = Signal(str)          # path (tests, status)
    saved = Signal(str)

    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.path: Path | None = None
        self._saved_state = None
        self._saving = False
        self.recent_menu: QMenu | None = None
        win.undo.cleanChanged.connect(self.update_title)
        win.undo.indexChanged.connect(self.update_title)
        win.print_settings.changed.connect(self.update_title)
        win.printer_panel.combo.currentIndexChanged.connect(self.update_title)
        self.mark_saved()

    # ------------------------------------------------------------ state
    def settings_state(self):
        w = self.win
        p = w.printer_panel.current()
        return (p.name if p else None, w.print_settings.settings(), w.job_panel.support_choice())

    def is_modified(self) -> bool:
        return not self.win.undo.isClean() or self.settings_state() != self._saved_state

    def mark_saved(self) -> None:
        self.win.undo.setClean()
        self._saved_state = self.settings_state()
        self.update_title()

    def update_title(self, *_):
        name = self.path.name if self.path else "Untitled"
        try:
            self.win.setWindowTitle(f"{name}[*] — {APP}")
            self.win.setWindowModified(self.is_modified())
        except RuntimeError:                 # the window is being destroyed
            pass

    def busy(self) -> bool:
        return self._saving

    # ------------------------------------------------------------ prompt
    def ask_unsaved(self) -> str:
        """Save / Don't save / Cancel. Tests replace this."""
        name = self.path.name if self.path else "Untitled"
        box = QMessageBox(QMessageBox.Warning, APP, f"Save changes to {name}?",
                          QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, self.win)
        box.setInformativeText("Your changes will be lost if you don't save them.")
        box.setDefaultButton(QMessageBox.Save)
        return {QMessageBox.Save: SAVE, QMessageBox.Discard: DISCARD}.get(box.exec(), CANCEL)

    def maybe_save(self, proceed) -> None:
        """Run proceed() once unsaved changes are saved or discarded; never when cancelled or the
        save fails."""
        if not self.is_modified():
            proceed()
            return
        answer = self.ask_unsaved()
        if answer == DISCARD:
            proceed()
        elif answer == SAVE:
            self.save(then=proceed)

    # ------------------------------------------------------------ commands
    def new_project(self) -> None:
        self.maybe_save(self._clear_and_reset)

    def _clear_and_reset(self) -> None:
        self._clear()
        self.path = None
        self.mark_saved()

    def _clear(self) -> None:
        w = self.win
        w.viewport.set_selection([], emit=False)
        w.undo.clear()
        w.scene.parts.clear()
        w.reports.clear()
        w.held = None
        w.clear_flaws()
        w.scene_changed(select=[])

    def open_dialog(self) -> None:
        def go():
            start = self.win.settings.value("projectDir", self.win.settings.value("lastDir", str(Path.home())))
            f, _ = QFileDialog.getOpenFileName(self.win, "Open project", str(start), PROJECT_FILTER)
            if f:
                self.win.settings.setValue("projectDir", str(Path(f).parent))
                self._open(f)
        self.maybe_save(go)

    def open_project(self, path: str) -> None:
        """Open after the unsaved-changes prompt (Open recent, command line, drops)."""
        self.maybe_save(lambda: self._open(path))

    def _open(self, path: str) -> None:
        path = str(path)
        if not Path(path).exists():
            QMessageBox.warning(self.win, APP, f"{path} no longer exists.")
            self.update_recent_menu()
            return
        if not projmod.is_project(path):
            # a plain 3MF: a new, untitled project holding its models
            self._clear()
            self.path = None
            self.mark_saved()
            self.win.import_files([path])
            return
        self.win.log_msg(f"Opening {path}…")
        workers.submit(load_project, path, on_done=lambda res: self._opened(path, res),
                       on_failed=lambda msg: self._open_failed(path, msg))

    def _open_failed(self, path, msg):
        first = msg.splitlines()[0]
        self.win.log_msg(f"Could not open {Path(path).name}: {first}")
        QMessageBox.warning(self.win, APP, f"Could not open {Path(path).name}:\n{first}")

    def _opened(self, path: str, res) -> None:
        proj, reports = res
        w = self.win
        self._clear()
        if proj.newer:
            QMessageBox.information(w, APP, f"{Path(path).name} was saved by a newer GooSlicer "
                                            f"({proj.app}). Some of its settings may be lost.")
        names = w.printer_panel._profiles
        if proj.printer and proj.printer in names:
            w.printer_panel.combo.setCurrentText(proj.printer)
        elif proj.printer:
            w.log_msg(f"⚠ {Path(path).name} was made for {proj.printer}, which isn't available; "
                      f"keeping {w.printer_panel.combo.currentText() or 'no printer'}.")
        if proj.print_settings and w.printer_panel.current():
            if not w.print_settings.apply_saved(proj.print_settings):
                w.log_msg(f"⚠ Resin '{proj.print_settings.get('resin_name')}' isn't available; "
                          "its saved settings are applied to Printer default.")
        if proj.support_type:
            w.job_panel.set_support_choice(proj.support_type)
        w.reports.update(reports)
        for p in proj.parts:
            w.scene.add(p, place=False)
        w.set_dismissed(proj.dismissed)
        if proj.view:
            self._apply_view(proj.view)
        w.scene_changed(select=[])
        self.path = Path(path)
        self.mark_saved()
        self.add_recent(path)
        n = len(proj.parts)
        w.log_msg(f"Opened {Path(path).name}: {n} part{'s' * (n != 1)}")
        for p in proj.parts:
            if not w.scene.in_volume(p):
                w.log_msg(f"⚠ {p.name} does not fit in the build volume of the selected printer.")
        self.opened.emit(path)

    def save(self, then=None) -> None:
        if self.path is None:
            self.save_as(then=then)
        else:
            self._write(self.path, then)

    def save_as(self, then=None) -> None:
        start = self.path or Path(self.win.settings.value("projectDir", str(Path.home()))) / self._default_name()
        f, _ = QFileDialog.getSaveFileName(self.win, "Save project", str(start), PROJECT_FILTER)
        if not f:
            return
        p = Path(f)
        if p.suffix.lower() != ".3mf":
            p = p.with_name(p.name + ".3mf")
        self.win.settings.setValue("projectDir", str(p.parent))
        self._write(p, then)

    def _default_name(self) -> str:
        parts = self.win.scene.parts
        return f"{parts[0].name}.3mf" if parts else "Untitled.3mf"

    def current_project(self) -> projmod.Project:
        w = self.win
        prn = w.printer_panel.current()
        cam = w.viewport.camera
        return projmod.Project(
            _snapshot_parts(w.scene.parts), printer=prn.name if prn else None,
            print_settings=w.print_settings.settings().to_dict() if prn else None,
            support_type=w.job_panel.support_choice(),
            dismissed={k: [np.asarray(x, float) for x in v] for k, v in w.dismissed_points().items()},
            view={"yaw": float(cam.yaw), "pitch": float(cam.pitch), "distance": float(cam.distance),
                  "target": [float(x) for x in cam.target]},
            app=f"{APP} {__version__}")

    def _support_meshes(self, snapshot: list[Part]) -> dict:
        """Ready support geometry only (saving never waits for a rebuild): {snapshot id: (mesh, offset)}."""
        out = {}
        for live, snap in zip(self.win.scene.parts, snapshot):
            e = self.win.supports.entry(live)
            if e is not None and e.result.mesh is not None:
                out[snap.id] = (e.result.mesh, self.win.supports.offset(live, e))
        return out

    def thumbnail(self) -> bytes | None:
        try:
            img = self.win.viewport.grabFramebuffer()
        except Exception:                                   # no GL context (headless edge cases)
            return None
        if img.isNull():
            return None
        side = min(img.width(), img.height())
        img = img.copy((img.width() - side) // 2, (img.height() - side) // 2, side, side)
        img = img.scaled(THUMB_PX, THUMB_PX, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        buf = QBuffer()
        buf.open(QIODevice.WriteOnly)
        img.save(buf, "PNG")
        return bytes(buf.data())

    def _write(self, path: Path, then=None) -> None:
        if self._saving:
            self.win.statusBar().showMessage("Already saving…", 3000)
            return
        proj = self.current_project()
        supports = self._support_meshes(proj.parts)
        state = self.settings_state()
        undo_index = self.win.undo.index()
        self._saving = True
        self.win.statusBar().showMessage(f"Saving {path.name}…")

        def done(_):
            self._saving = False
            self.path = path
            # edits made while the file was being written stay unsaved
            if self.win.undo.index() == undo_index:
                self.win.undo.setClean()
            self._saved_state = state
            self.update_title()
            self.add_recent(str(path))
            self.win.statusBar().showMessage(f"Saved {path.name}", 5000)
            self.win.log_msg(f"Saved {path}")
            self.saved.emit(str(path))
            if then:
                then()

        def failed(msg):
            self._saving = False
            first = msg.splitlines()[0]
            self.win.statusBar().clearMessage()
            self.win.log_msg(f"Could not save {path.name}: {first}")
            QMessageBox.warning(self.win, APP, f"Could not save {path.name}:\n{first}")

        workers.submit(_save_job, path, proj, supports, self.thumbnail(), on_done=done, on_failed=failed,
                       on_progress=lambda t: self.win.statusBar().showMessage(f"Saving {path.name}… {t}"))

    # ------------------------------------------------------------ recent
    def recent(self) -> list[str]:
        v = self.win.settings.value(RECENT_KEY, [])
        return [v] if isinstance(v, str) else [str(x) for x in (v or [])]

    def recent_max(self) -> int:
        try:
            return max(1, min(30, int(self.win.settings.value(RECENT_MAX_KEY, RECENT_DEFAULT))))
        except (TypeError, ValueError):
            return RECENT_DEFAULT

    def add_recent(self, path: str) -> None:
        path = str(Path(path).absolute())
        items = [path] + [x for x in self.recent() if x != path]
        self.win.settings.setValue(RECENT_KEY, items[: self.recent_max()])
        self.update_recent_menu()

    def clear_recent(self) -> None:
        self.win.settings.setValue(RECENT_KEY, [])
        self.update_recent_menu()

    def update_recent_menu(self) -> None:
        m = self.recent_menu
        if m is None:
            return
        m.clear()
        items = self.recent()[: self.recent_max()]
        for path in items:
            exists = Path(path).exists()
            a = m.addAction(Path(path).name + ("" if exists else " (missing)"),
                            lambda p=path: self.open_project(p))
            a.setToolTip(path)
            a.setStatusTip(path)
            a.setEnabled(exists)
        if items:
            m.addSeparator()
        clear = m.addAction("Clear list", self.clear_recent)
        clear.setEnabled(bool(items))
        m.setToolTipsVisible(True)

    def _apply_view(self, v: dict) -> None:
        cam = self.win.viewport.camera
        try:
            cam.yaw = float(v.get("yaw", cam.yaw))
            cam.pitch = float(v.get("pitch", cam.pitch))
            cam.distance = float(v.get("distance", cam.distance))
            if "target" in v:
                cam.target = np.asarray(v["target"], float).reshape(3)
        except (TypeError, ValueError):
            pass
        self.win.viewport.update()
