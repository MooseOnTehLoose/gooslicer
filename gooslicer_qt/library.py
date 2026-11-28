"""The Project library window (11 §4): projects, their designs, and each design's versions."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QDialogButtonBox, QFormLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QLineEdit, QListWidget, QListWidgetItem, QMessageBox,
                               QPlainTextEdit, QPushButton, QSplitter, QVBoxLayout, QWidget)

from gooslicer import library as L

APP = "GooSlicer"
STATUS_LABELS = {"draft": "Draft", "ready": "Ready", "printed": "Printed"}


def _when(stamp: str) -> str:
    try:
        t = datetime.strptime(stamp, "%Y-%m-%dT%H:%M:%SZ")
        return t.strftime("%d %b %Y %H:%M") + " UTC"
    except (TypeError, ValueError):
        return stamp or "–"


def _size(n: int) -> str:
    for unit in ("bytes", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.0f} {unit}" if unit == "bytes" else f"{n:.1f} {unit}"
        n /= 1024
    return str(n)


def trash(path: Path) -> bool:
    """Move a file or folder to the desktop trash (QFile.moveToTrash)."""
    from PySide6.QtCore import QFile
    ok = QFile.moveToTrash(str(path))
    return bool(ok[0] if isinstance(ok, tuple) else ok)


class AddToLibraryDialog(QDialog):
    """Pick (or create) a library project and a design name (Save version… outside the library,
    Add current)."""

    def __init__(self, lib: L.Library, name: str, parent=None, project: str | None = None):
        super().__init__(parent)
        self.setWindowTitle("Add to the project library")
        self.lib = lib
        f = QFormLayout(self)
        self.project = QComboBox()
        self.project.setEditable(True)
        self.project.addItems([p.name for p in lib.projects()])
        if project:
            self.project.setCurrentText(project)
        self.project.setToolTip("An existing project, or type a new name")
        self.design = QLineEdit(name)
        f.addRow("Project", self.project)
        f.addRow("Design", self.design)
        f.addRow(QLabel(f"Library: {lib.root}"))
        bb = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        bb.accepted.connect(self.accept)
        bb.rejected.connect(self.reject)
        f.addRow(bb)

    def target(self) -> Path:
        """The new design's path (creating the project if needed); raises LibraryError."""
        pname = L.check_name(self.project.currentText())
        proj = next((p for p in self.lib.projects() if p.name == pname), None)
        if proj is None:
            proj = self.lib.create_project(pname)
        return proj.new_design_path(self.design.text())


class LibraryWindow(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Project library")
        self.setModal(False)
        self.resize(1100, 620)
        lay = QVBoxLayout(self)
        self.root_label = QLabel()
        self.root_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        lay.addWidget(self.root_label)
        split = QSplitter()
        lay.addWidget(split, 1)

        # projects
        pg = QGroupBox("Projects")
        pl = QVBoxLayout(pg)
        self.projects = QListWidget()
        pl.addWidget(self.projects, 1)
        self.project_info = QLabel()
        self.project_info.setWordWrap(True)
        pl.addWidget(self.project_info)
        row = QHBoxLayout()
        pl.addLayout(row)
        self.new_project_btn = QPushButton("New…")
        self.rename_project_btn = QPushButton("Rename…")
        self.delete_project_btn = QPushButton("Delete")
        for b in (self.new_project_btn, self.rename_project_btn, self.delete_project_btn):
            row.addWidget(b)
        split.addWidget(pg)

        # designs
        dg = QGroupBox("Designs")
        dl = QVBoxLayout(dg)
        self.designs = QListWidget()
        self.designs.setIconSize(QSize(64, 64))
        dl.addWidget(self.designs, 1)
        grid = QHBoxLayout()
        dl.addLayout(grid)
        self.open_btn = QPushButton("Open")
        self.new_design_btn = QPushButton("New…")
        self.add_current_btn = QPushButton("Add current…")
        self.add_current_btn.setToolTip("Save the open project into this project as a new design")
        grid.addWidget(self.open_btn)
        grid.addWidget(self.new_design_btn)
        grid.addWidget(self.add_current_btn)
        grid2 = QHBoxLayout()
        dl.addLayout(grid2)
        self.rename_design_btn = QPushButton("Rename…")
        self.duplicate_btn = QPushButton("Duplicate…")
        self.delete_design_btn = QPushButton("Delete")
        for b in (self.rename_design_btn, self.duplicate_btn, self.delete_design_btn):
            grid2.addWidget(b)
        detail = QFormLayout()
        dl.addLayout(detail)
        self.status = QComboBox()
        for key, label in STATUS_LABELS.items():
            self.status.addItem(label, key)
        self.notes = QPlainTextEdit()
        self.notes.setMaximumHeight(70)
        self.notes.setPlaceholderText("Notes (resin, orientation, what's left to do…)")
        self.design_info = QLabel()
        self.design_info.setWordWrap(True)
        detail.addRow("Status", self.status)
        detail.addRow("Notes", self.notes)
        detail.addRow(self.design_info)
        split.addWidget(dg)

        # versions
        vg = QGroupBox("Versions")
        vl = QVBoxLayout(vg)
        self.versions = QListWidget()
        vl.addWidget(self.versions, 1)
        vrow = QHBoxLayout()
        vl.addLayout(vrow)
        self.open_version_btn = QPushButton("Open")
        self.open_version_btn.setToolTip("Open this version as an unsaved copy of the design")
        self.restore_btn = QPushButton("Restore")
        self.restore_btn.setToolTip("Make this version the design (the current state is kept as a "
                                    "version first if it isn't one already)")
        self.delete_version_btn = QPushButton("Delete")
        for b in (self.open_version_btn, self.restore_btn, self.delete_version_btn):
            vrow.addWidget(b)
        self.save_version_btn = QPushButton("Save version of the open project…")
        vl.addWidget(self.save_version_btn)
        split.addWidget(vg)
        split.setSizes([250, 500, 350])

        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.close)
        lay.addWidget(close)

        self.projects.currentRowChanged.connect(lambda _: self._fill_designs())
        self.designs.currentRowChanged.connect(lambda _: self._fill_detail())
        self.designs.itemDoubleClicked.connect(lambda _: self.open_design())
        self.new_project_btn.clicked.connect(self.new_project)
        self.rename_project_btn.clicked.connect(self.rename_project)
        self.delete_project_btn.clicked.connect(self.delete_project)
        self.open_btn.clicked.connect(self.open_design)
        self.new_design_btn.clicked.connect(self.new_design)
        self.add_current_btn.clicked.connect(self.add_current)
        self.rename_design_btn.clicked.connect(self.rename_design)
        self.duplicate_btn.clicked.connect(self.duplicate_design)
        self.delete_design_btn.clicked.connect(self.delete_design)
        self.status.activated.connect(self._status_changed)
        self.notes.textChanged.connect(self._notes_changed)
        self.open_version_btn.clicked.connect(self.open_version)
        self.restore_btn.clicked.connect(self.restore_version)
        self.delete_version_btn.clicked.connect(self.delete_version)
        self.save_version_btn.clicked.connect(lambda: self.win.project.save_version(then=self.refresh))
        self.refresh()

    # ------------------------------------------------------------ model
    @property
    def lib(self) -> L.Library:
        return L.Library(self.win.library_root())

    def project(self) -> L.LibProject | None:
        it = self.projects.currentItem()
        return L.LibProject(Path(it.data(Qt.UserRole))) if it else None

    def design(self) -> L.Design | None:
        p, it = self.project(), self.designs.currentItem()
        return L.Design(p, it.data(Qt.UserRole)) if p and it else None

    def version(self) -> L.Version | None:
        d, it = self.design(), self.versions.currentItem()
        if not d or not it:
            return None
        n = it.data(Qt.UserRole)
        return next((v for v in d.versions() if v.n == n), None)

    def _error(self, e) -> None:
        QMessageBox.warning(self, APP, str(e))

    # ------------------------------------------------------------ filling
    def refresh(self, project: str | None = None, design: str | None = None) -> None:
        keep_p = project or (self.projects.currentItem().text() if self.projects.currentItem() else None)
        keep_d = design or (self.designs.currentItem().text().split("\n")[0] if self.designs.currentItem() else None)
        lib = self.lib
        self.root_label.setText(f"Library: {lib.root}" + ("" if lib.root.is_dir() else " (created when you add a project)"))
        self.projects.blockSignals(True)
        self.projects.clear()
        for p in lib.projects():
            it = QListWidgetItem(p.name)
            it.setData(Qt.UserRole, str(p.path))
            self.projects.addItem(it)
            if p.name == keep_p:
                self.projects.setCurrentItem(it)
        if self.projects.currentRow() < 0 and self.projects.count():
            self.projects.setCurrentRow(0)
        self.projects.blockSignals(False)
        self._fill_designs(keep_d)

    def _fill_designs(self, keep: str | None = None) -> None:
        p = self.project()
        self.designs.blockSignals(True)
        self.designs.clear()
        total = 0
        if p:
            for d in p.designs():
                info = d.info()
                sub = " · ".join(x for x in (info.get("resin") or "", STATUS_LABELS.get(d.status, d.status),
                                             f"{len(d.versions())} versions") if x)
                it = QListWidgetItem(f"{d.name}\n{sub}")
                it.setData(Qt.UserRole, d.file)
                if info.get("thumbnail"):
                    pm = QPixmap()
                    if pm.loadFromData(info["thumbnail"]):
                        it.setIcon(QIcon(pm))
                self.designs.addItem(it)
                if d.name == keep:
                    self.designs.setCurrentItem(it)
                total += d.size_on_disk()
            if self.designs.currentRow() < 0 and self.designs.count():
                self.designs.setCurrentRow(0)
            self.project_info.setText((p.description + "\n" if p.description else "")
                                      + f"{self.designs.count()} designs, {_size(total)} on disk")
        else:
            self.project_info.setText("No projects yet: New… makes one.")
        self.designs.blockSignals(False)
        on = p is not None
        for b in (self.rename_project_btn, self.delete_project_btn, self.new_design_btn, self.add_current_btn):
            b.setEnabled(on)
        self._fill_detail()

    def _fill_detail(self) -> None:
        d = self.design()
        on = d is not None
        for b in (self.open_btn, self.rename_design_btn, self.duplicate_btn, self.delete_design_btn, self.status,
                  self.notes):
            b.setEnabled(on)
        self.status.blockSignals(True)
        self.notes.blockSignals(True)
        self.versions.clear()
        if d:
            self.status.setCurrentIndex(max(0, self.status.findData(d.status)))
            self.notes.setPlainText(d.notes)
            info = d.info()
            self.design_info.setText(
                f"{info.get('printer') or 'No printer'} · {info.get('resin') or 'no resin'} · "
                f"{info.get('parts', 0)} parts on {info.get('plates', 1)} plate(s)\n"
                f"Saved {_when(info.get('saved', ''))} · {_size(d.size_on_disk())} with versions")
            for v in d.versions():
                it = QListWidgetItem(f"V{v.n} · {_when(v.saved)}" + (f"\n{v.note}" if v.note else ""))
                it.setData(Qt.UserRole, v.n)
                self.versions.addItem(it)
        else:
            self.notes.setPlainText("")
            self.design_info.setText("")
        self.status.blockSignals(False)
        self.notes.blockSignals(False)
        has_v = self.versions.count() > 0
        for b in (self.open_version_btn, self.restore_btn, self.delete_version_btn):
            b.setEnabled(has_v)
        if has_v:
            self.versions.setCurrentRow(0)

    # ------------------------------------------------------------ projects
    def new_project(self) -> None:
        name, ok = QInputDialog.getText(self, "New project", "Project name:")
        if not ok:
            return
        try:
            p = self.lib.create_project(name)
        except (L.LibraryError, OSError) as e:
            return self._error(e)
        self.refresh(project=p.name)

    def rename_project(self) -> None:
        p = self.project()
        if not p:
            return
        name, ok = QInputDialog.getText(self, "Rename project", "New name:", text=p.name)
        if not ok or name == p.name:
            return
        if self._open_inside(p.path):
            return self._error("Close the open design of this project first (File → New project).")
        try:
            np_ = self.lib.rename_project(p, name)
        except (L.LibraryError, OSError) as e:
            return self._error(e)
        self.refresh(project=np_.name)

    def delete_project(self) -> None:
        p = self.project()
        if not p:
            return
        if self._open_inside(p.path):
            return self._error("Close the open design of this project first (File → New project).")
        designs = p.designs()
        nv = sum(len(d.versions()) for d in designs)
        if QMessageBox.question(self, APP, f"Move the project {p.name} ({len(designs)} designs, {nv} versions) "
                                "to the trash?") != QMessageBox.Yes:
            return
        if not trash(p.path):
            return self._error(f"Couldn't move {p.path} to the trash.")
        self.win.log_msg(f"Library: moved project {p.name} to the trash.")
        self.refresh()

    def _open_inside(self, path: Path) -> bool:
        cur = self.win.project.path
        try:
            return cur is not None and Path(path).resolve() in Path(cur).resolve().parents
        except OSError:
            return False

    # ------------------------------------------------------------ designs
    def open_design(self) -> None:
        d = self.design()
        if d:
            self.win.project.open_project(str(d.path))

    def new_design(self) -> None:
        p = self.project()
        if not p:
            return
        name, ok = QInputDialog.getText(self, "New design", "Design name:")
        if not ok:
            return
        try:
            path = p.new_design_path(name)
        except (L.LibraryError, OSError) as e:
            return self._error(e)
        self.win.project.new_in_library(path, then=lambda: self.refresh(design=Path(path).stem))

    def add_current(self) -> None:
        p = self.project()
        if not p:
            return
        default = self.win.project.path.stem if self.win.project.path else (
            self.win.scene.parts[0].name if self.win.scene.parts else "Design")
        name, ok = QInputDialog.getText(self, "Add current project", "Design name:", text=default)
        if not ok:
            return
        try:
            path = p.new_design_path(name)
        except (L.LibraryError, OSError) as e:
            return self._error(e)
        self.win.project.save_to(path, then=lambda: self.refresh(design=Path(path).stem))

    def rename_design(self) -> None:
        p, d = self.project(), self.design()
        if not d:
            return
        if self._is_open(d):
            return self._error("Close this design first (File → New project).")
        name, ok = QInputDialog.getText(self, "Rename design", "New name:", text=d.name)
        if not ok or name == d.name:
            return
        try:
            nd = p.rename_design(d, name)
        except (L.LibraryError, OSError) as e:
            return self._error(e)
        self.refresh(design=nd.name)

    def duplicate_design(self) -> None:
        p, d = self.project(), self.design()
        if not d:
            return
        name, ok = QInputDialog.getText(self, "Duplicate design", "Name of the copy:", text=f"{d.name} copy")
        if not ok:
            return
        try:
            nd = p.duplicate_design(d, name)
        except (L.LibraryError, OSError) as e:
            return self._error(e)
        self.refresh(design=nd.name)

    def delete_design(self) -> None:
        p, d = self.project(), self.design()
        if not d:
            return
        if self._is_open(d):
            return self._error("Close this design first (File → New project).")
        nv = len(d.versions())
        if QMessageBox.question(self, APP, f"Move the design {d.name} and its {nv} versions to the trash?") \
                != QMessageBox.Yes:
            return
        ok = (not d.path.exists() or trash(d.path)) and (not d.version_dir.exists() or trash(d.version_dir))
        if not ok:
            return self._error(f"Couldn't move {d.name} to the trash.")
        p.forget_design(d)
        self.win.log_msg(f"Library: moved design {d.name} to the trash.")
        self.refresh()

    def _is_open(self, d: L.Design) -> bool:
        cur = self.win.project.path
        try:
            return cur is not None and Path(cur).resolve() == d.path.resolve()
        except OSError:
            return False

    def _status_changed(self, _=None) -> None:
        d = self.design()
        if d:
            d.set_status(self.status.currentData())
            it = self.designs.currentItem()
            name = it.text().split("\n")[0]
            self.refresh(design=name)

    def _notes_changed(self) -> None:
        d = self.design()
        if d:
            d.set_notes(self.notes.toPlainText())

    # ------------------------------------------------------------ versions
    def open_version(self) -> None:
        d, v = self.design(), self.version()
        if d and v:
            self.win.project.open_version(d, v)

    def restore_version(self) -> None:
        d, v = self.design(), self.version()
        if not (d and v):
            return
        if QMessageBox.question(self, APP, f"Make V{v.n} the current {d.name}? The design as it is now "
                                "is kept as a version first (unless it already is one).") != QMessageBox.Yes:
            return

        def go():
            try:
                safety = d.restore(v.n, self.win.app_name())
            except (L.LibraryError, OSError) as e:
                return self._error(e)
            self.win.log_msg(f"Library: restored {d.name} to V{v.n}"
                             + (f" (its previous state is V{safety.n})." if safety else "."))
            self.win.project._open(str(d.path))
            self.refresh(design=d.name)
        if self._is_open(d):
            self.win.project.maybe_save(go)
        else:
            go()

    def delete_version(self) -> None:
        d, v = self.design(), self.version()
        if not (d and v):
            return
        if QMessageBox.question(self, APP, f"Delete V{v.n} of {d.name}?") != QMessageBox.Yes:
            return
        try:
            d.delete_version(v.n)
        except OSError as e:
            return self._error(e)
        self.refresh(design=d.name)
