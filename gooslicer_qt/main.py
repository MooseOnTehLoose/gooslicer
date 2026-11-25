"""GooSlicer main window: scene, 3D viewport, panels, undo/redo, import and repair."""
from __future__ import annotations

import os
import sys
from types import SimpleNamespace
from pathlib import Path

import numpy as np
from PySide6.QtCore import QEvent, QObject, QSettings, QStandardPaths, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QColor, QKeySequence, QSurfaceFormat, QUndoStack
from PySide6.QtWidgets import (QApplication, QComboBox, QDockWidget, QFileDialog, QFormLayout, QMenu,
                               QFrame, QHBoxLayout, QLabel, QMainWindow, QMessageBox,
                               QGroupBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea,
                               QSplitter, QStyle, QVBoxLayout, QWidget)

from elegoo_goo import goo
from gooslicer import __version__, mesh as meshmod, preview, profiles
from gooslicer import hollow as hollowmod
from gooslicer import jobs
from gooslicer import settings as settingsmod
from gooslicer import slicer as slicermod
from gooslicer import profile_store, support_defaults
from gooslicer import project as projmod
from gooslicer import supports as supportmod
from gooslicer.raster import RasterSpec
from gooslicer.scene import Part, Scene, prepare, ray_triangles, rotation_between

from . import colors as colormod
from . import workers
from .commands import AddPartsCommand, MeshCommand, MirrorCommand, RemovePartsCommand, StateCommand
from .camera import CameraPanel
from .hollowing import (DrainHoleDialog, DrainHolePanel, HollowDialog, drill_part_at, hole_axis_world,
                        hole_preview_mesh)
from .jobpanel import JobPanel
from .memory import MemoryMonitor, StatusBar, trim_memory_soon
from .panels import FlawsPanel, InfoButton, PartsList, PropertiesPanel, ToolWindow, show_popup
from .printers import PrinterControlPanel
from .projectio import ProjectController
from .shortcuts import MOUSE_HELP, ShortcutRegistry, ShortcutsDialog
from .slicing import LayerPreview, PrintSettingsPanel, SliceThread
from .supporting import SupportDialog, SupportManager, SupportsCommand, support_params
from .theme import THEMES, ThemeManager
from . import flaws as flawmod
from .viewport import Viewport, default_format, island_guides

APP = "GooSlicer"
# Bumped when the dock layout changes enough that an older saved layout should be dropped
# (2: Parts inside Properties; Print settings and Properties are tabs).
LAYOUT_VERSION = 5          # 3: Log and Layers became own windows; 4: tabs renamed; 5: Flaws tab
MODEL_FILTER = "3D models (*.stl *.STL *.obj *.OBJ *.3mf *.3MF);;All files (*)"


def profile_root() -> Path:
    """GooSlicer's copy of the user's SatelLite profiles (gooslicer/profile_store.py)."""
    return data_dir() / "satellite"


def data_dir() -> Path:
    """XDG data dir (~/.local/share/GooSlicer)."""
    p = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
    p.mkdir(parents=True, exist_ok=True)
    return p


class PrinterPanel(QWidget):
    """The printer row of the Printer settings tab: model dropdown plus an (i) button that pops up
    the printer's build volume, resolution, pixel pitch and mirroring."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.combo = QComboBox()
        self.combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.combo.setMinimumContentsLength(12)
        self.info_btn = InfoButton("Printer details")
        self.info_btn.clicked.connect(self.show_details)
        row = QHBoxLayout(self)
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.combo, 1)
        row.addWidget(self.info_btn)
        # details pop-up (closes on any click outside it)
        self.details = QFrame(self, Qt.Popup)
        self.details.setFrameShape(QFrame.StyledPanel)
        self.details_title = QLabel()
        f = self.details_title.font()
        f.setBold(True)
        self.details_title.setFont(f)
        self.info = {k: QLabel("–") for k in ("Build volume", "Resolution", "Pixel pitch", "Mirror")}
        form = QFormLayout(self.details)
        form.setContentsMargins(12, 10, 12, 10)
        form.addRow(self.details_title)
        for k, w in self.info.items():
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow(k, w)
        self.combo.currentIndexChanged.connect(self._show)
        self._profiles: dict[str, profiles.PrinterProfile] = {}
        self._show()

    def show_details(self) -> None:
        """Open the details pop-up under the (i) button, right-aligned to it."""
        show_popup(self.details, self.info_btn)

    def set_profiles(self, ps: dict[str, profiles.PrinterProfile], current: str | None = None):
        self._profiles = ps
        self.combo.blockSignals(True)
        self.combo.clear()
        self.combo.addItems(sorted(ps))
        if current in ps:
            self.combo.setCurrentText(current)
        self.combo.blockSignals(False)
        self._show()
        self.combo.currentIndexChanged.emit(self.combo.currentIndex())

    def current(self) -> profiles.PrinterProfile | None:
        return self._profiles.get(self.combo.currentText())

    def _show(self, *_):
        p = self.current()
        self.info_btn.setEnabled(p is not None)
        self.details_title.setText(p.name if p else "No printer selected")
        if not p:
            for w in self.info.values():
                w.setText("–")
            return
        self.info["Build volume"].setText(f"{p.size_x:g} × {p.size_y:g} × {p.size_z:g} mm")
        self.info["Resolution"].setText(f"{p.res_x} × {p.res_y} px")
        self.info["Pixel pitch"].setText(f"{p.pixel_x_mm * 1000:.1f} × {p.pixel_y_mm * 1000:.1f} µm")
        self.info["Mirror"].setText(", ".join(a for a, m in (("X", p.mirror_x), ("Y", p.mirror_y)) if m) or "none")


class _DropForwarder(QObject):
    """Hands file drops on a dock (docked or floating) to the main window's handlers."""

    def __init__(self, win):
        super().__init__(win)
        self.win = win

    def eventFilter(self, obj, e):
        h = {QEvent.DragEnter: self.win.dragEnterEvent, QEvent.DragMove: self.win.dragMoveEvent,
             QEvent.Drop: self.win.dropEvent}.get(e.type())
        if h is not None:
            h(e)
            return e.isAccepted()
        return False


def _reds_ready(res):
    """(flaws, [[red meshes] per flaw]) with the red meshes ready for the 3D view."""
    for group in res[1]:
        for m in group:
            prepare(m)
    return res


def _islands_and_faces(meshes, layer_height):
    return _reds_ready(jobs.islands_and_faces(meshes, layer_height))


def _cavities_and_faces(meshes):
    return _reds_ready(jobs.cavities_and_faces(meshes))


def _auto_supported(res):
    contacts, result, islands = res
    prepare(result.mesh)                         # ready for the 3D view, off the GUI thread
    return res


def _load_and_analyze(path: str):
    if Path(path).suffix.lower() == ".3mf" and projmod.is_project(path):
        # a GooSlicer project imported into this one: its parts with their supports, no settings
        proj = projmod.load(path)
        out, seen = [], {}
        for part in proj.parts:
            if id(part.mesh) not in seen:
                seen[id(part.mesh)] = meshmod.analyze(part.mesh)
                meshmod.slim(part.mesh)
                prepare(part.mesh)
            out.append((part, seen[id(part.mesh)]))
        return out
    out = []
    for name, m in meshmod.load_meshes(path):
        part = Part.from_mesh(name, m)
        rep = meshmod.analyze(part.mesh)
        meshmod.slim(part.mesh)                 # picking needs no index (scene.ray_triangles)
        prepare(part.mesh)                      # hull and GPU arrays, off the GUI thread
        out.append((part, rep))
    return out


def _repair_and_analyze(m, progress=None):
    fixed = meshmod.repair(m, progress=progress)
    if progress:
        progress("checking the result")
    return fixed, meshmod.analyze(fixed)


def cpu_name() -> str:
    """The processor's model name and thread count, e.g. "AMD Ryzen 7 7800X3D, 16 threads"."""
    import os
    import platform
    name = ""
    try:
        with open("/proc/cpuinfo") as fh:
            for line in fh:
                if line.startswith("model name"):
                    name = line.split(":", 1)[1].strip()
                    break
    except OSError:
        pass
    name = name or platform.processor() or platform.machine() or "CPU"
    n = os.cpu_count()
    return f"{name}, {n} threads" if n else name


class MainWindow(QMainWindow):
    importFinished = Signal(int)  # number of parts added (for tests / status)
    hollowFinished = Signal(bool)
    supportsFinished = Signal(int)      # islands left (-1 = failed)

    def __init__(self, settings: QSettings | None = None, lan_ports: dict | None = None):
        super().__init__()
        self.settings = settings or QSettings(APP, APP)
        self.theme = ThemeManager.for_app(QApplication.instance(), str(self.settings.value("theme", "system")))
        self.setWindowTitle(f"Untitled[*] — {APP}")
        self.resize(1400, 900)
        self.scene = Scene()
        self.reports: dict[int, meshmod.MeshReport] = {}   # id(mesh) -> report
        self.undo = QUndoStack(self)
        self.supports = SupportManager(self.scene)
        self.supports.fixedReplaced.connect(
            lambda name, src: self.log_msg(f"{name} changed: GooSlicer now builds its supports from "
                                           f"{src}'s support points."))
        self._pending = 0

        self.viewport = Viewport(self.scene)
        self.setCentralWidget(self.viewport)

        self.printer_panel = PrinterPanel()
        self.parts = PartsList()
        self.import_btn = QPushButton(self.style().standardIcon(QStyle.SP_DialogOpenButton), "Import model…")
        self.import_btn.setToolTip("Import STL, OBJ or 3MF models (Ctrl+I)")
        self.import_btn.clicked.connect(self.import_dialog)
        self.parts_box = QGroupBox("Parts")
        pl = QVBoxLayout(self.parts_box)
        pl.addWidget(self.import_btn)                   # above the list
        pl.addWidget(self.parts, 1)
        self.props = PropertiesPanel(self)
        props_scroll = QScrollArea()
        props_scroll.setWidgetResizable(True)
        props_scroll.setFrameShape(QFrame.NoFrame)
        self.job_panel = JobPanel(self)                 # Supports and Slicing, below Mesh
        props_box = QWidget()
        pbl = QVBoxLayout(props_box)
        pbl.setContentsMargins(0, 0, 0, 0)
        pbl.setSpacing(0)
        pbl.addWidget(self.props)
        pbl.addWidget(self.job_panel)
        pbl.addStretch(1)
        props_scroll.setWidget(props_box)
        # the Models tab: parts on top, the selected part's properties below (resizable)
        self.properties_split = QSplitter(Qt.Vertical)
        self.properties_split.addWidget(self.parts_box)
        self.properties_split.addWidget(props_scroll)
        self.properties_split.setStretchFactor(0, 1)
        self.properties_split.setStretchFactor(1, 2)
        self.properties_split.setChildrenCollapsible(False)
        self.log = QPlainTextEdit(readOnly=True)
        self.log.setMaximumBlockCount(5000)
        self.print_settings = PrintSettingsPanel()
        self.print_settings.insert_top_row("Printer", self.printer_panel)   # directly above the resin
        fav = self.settings.value("resinFavorites", [])
        fav = [fav] if isinstance(fav, str) else list(fav or [])
        if any("|" not in k for k in fav):
            # older versions saved bare resin names (a favourite on every printer); keep them for
            # the printer that was in use, which is where they were starred
            printer = str(self.settings.value("printer", "") or "")
            fav = sorted({k if "|" in k else f"{printer}|{k}" for k in fav if printer or "|" in k})
            self.settings.setValue("resinFavorites", fav)
        self.print_settings.set_favorites(fav)
        self.print_settings.favoritesChanged.connect(lambda keys: self.settings.setValue("resinFavorites", keys))
        self.print_settings.resinSelected.connect(self._remember_resin)
        self.print_settings.changed.connect(lambda: self._check_held())
        self.held = None                 # (SliceResult with .goo, plate fingerprint) of the last in-memory slice
        self._slicing_fp = None
        self.layer_preview = LayerPreview()
        self.lan = PrinterControlPanel(self.settings, ports=lan_ports)
        self.lan.log.connect(self.log_msg)
        self.print_settings.insert_top_widget(self.lan)          # above the Printer dropdown
        self.camera = CameraPanel(self.lan, self.settings)
        self.camera.log.connect(self.log_msg)
        self.print_settings.add_bottom_widget(self.camera)       # below Advanced settings
        self.settings_scroll = QScrollArea()                     # the tab is taller than the column
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setFrameShape(QFrame.NoFrame)
        self.settings_scroll.setWidget(self.print_settings)
        self._send_after_slice = False
        self.docks = {}
        self._drops = _DropForwarder(self)
        # the right column (Printer settings, Models) runs the full height
        self.setCorner(Qt.BottomRightCorner, Qt.RightDockWidgetArea)
        self.flaws_panel = FlawsPanel()
        for title, w, area in (("Printer settings", self.settings_scroll, Qt.RightDockWidgetArea),
                               ("Models", self.properties_split, Qt.RightDockWidgetArea),
                               ("Flaws", self.flaws_panel, Qt.RightDockWidgetArea)):
            d = QDockWidget(title, self)
            d.setObjectName(f"dock{title.replace(' ', '')}")
            d.setWidget(w)
            self.addDockWidget(area, d)
            self.docks[title] = d
            d.setAcceptDrops(True)      # also when floated out as its own window
            d.installEventFilter(self._drops)
        # the Log and the layer preview open in their own windows (View → Logs, View → Layers)
        self.log_window = ToolWindow("Logs", self.log, self.settings, "logWindow", self)
        self.layers_window = ToolWindow("Layers", self.layer_preview, self.settings, "layersWindow", self)
        for tw in (self.log_window, self.layers_window):
            tw.setAcceptDrops(True)
            tw.installEventFilter(self._drops)
        self._islands_fp = None          # plate fingerprint of the island check shown in red
        self._cavities_fp = None         # … and of the trapped-resin check
        self.flaws = {k: [] for k in flawmod.KINDS}          # shown flaws, numbered per kind
        self._dismissed = {k: [] for k in flawmod.KINDS}     # points of flaws dismissed as false positives
        self._dismissed_parts = None     # the parts (meshes, placement) the dismissals are for
        self._hole_queue: list = []      # drain-hole clicks waiting to be drilled
        self._drilling = False
        self._layout_done = False
        self._slice_thread = None
        self._progress = QProgressBar()             # only shown while slicing
        self._progress.setMaximumWidth(260)
        self._progress.setFormat("%p%")
        self._progress.hide()
        self._cancel_btn = QPushButton("Cancel")
        self._cancel_btn.hide()
        self._cancel_btn.clicked.connect(self.cancel_slice)
        self.setStatusBar(StatusBar(self))                     # memory use at the left
        self.memory_monitor = MemoryMonitor(self)
        self.memory_monitor.updated.connect(self.statusBar().set_usage)
        self.memory_monitor.start()
        self.statusBar().addPermanentWidget(self._progress)
        self.statusBar().addPermanentWidget(self._cancel_btn)

        self.project = ProjectController(self)
        self.shortcuts = ShortcutRegistry(self.settings, self)
        self.viewport.set_handle_mode(str(self.settings.value("view/handleMode", "rotate")))
        self.viewport.colors = colormod.load_colors(self.settings)
        self._menus()
        self.viewport.selectionChanged.connect(self._on_viewport_selection)
        self.viewport.moveFinished.connect(lambda ch: self.undo.push(StateCommand(self, ch, "Move")))
        self.viewport.rotateFinished.connect(lambda ch, text: self.undo.push(StateCommand(self, ch, text)))
        self.viewport.transformFinished.connect(lambda ch, text: self.undo.push(StateCommand(self, ch, text)))
        self.viewport.transformLive.connect(self._handle_live)
        self.viewport.uniform_scale = lambda: self.props.uniform.isChecked()
        self.viewport.handleModeChanged.connect(self._handle_mode_changed)
        self.viewport.partContextMenu.connect(self._part_context)
        self.viewport.layFlatPicked.connect(self._lay_flat)
        self.viewport.holePicked.connect(self._hole_picked)
        self.viewport.holeModeEnded.connect(lambda: self.statusBar().clearMessage())
        self.viewport.extra_meshes = self.supports.display
        self.supports.updated.connect(self._supports_updated)
        self.viewport.supportClicked.connect(self._support_click)
        self.viewport.supportDragged.connect(self._support_drag)
        self.viewport.supportModeEnded.connect(lambda: self.statusBar().clearMessage())
        self.viewport.filesDropped.connect(self.open_files)
        self.setAcceptDrops(True)       # drops anywhere in the window, not only on the 3D view
        self.parts.selectionChangedIds.connect(lambda ids: self.select(ids))
        self.parts.removeRequested.connect(self.remove_part)
        self.flaws_panel.list.selectionChangedKeys.connect(self.viewport.set_guide_selection)
        self.flaws_panel.list.removeRequested.connect(self.dismiss_flaw)
        self.viewport.guideSelectionChanged.connect(self.flaws_panel.list.set_selected)
        self.flaws_panel.refresh([], set())
        self.printer_panel.combo.currentIndexChanged.connect(self._printer_changed)
        self.load_profiles()
        if self.settings.value("geometry"):
            self.restoreGeometry(self.settings.value("geometry"))
        self.project.mark_saved()              # the profiles' settings are the starting point
        self.statusBar().showMessage("Import a model (Ctrl+I) or drop STL/OBJ/3MF or .satres files here")

    def showEvent(self, e):
        super().showEvent(e)
        if self._layout_done:
            return
        self._layout_done = True
        # Dock tab groups are formed only after the first show: with two groups created before
        # the first layout, Qt 6.12 leaves a stale, visible QTabBar behind (plain-Qt repro).
        # The default layout is always built first and kept, for View → Restore all windows to default.
        self._default_layout()
        self._default_state = self.saveState(LAYOUT_VERSION)
        if self.settings.value("windowState"):
            self.restoreState(self.settings.value("windowState"), LAYOUT_VERSION)   # older: ignored

    def _default_layout(self) -> None:
        # right-hand tabs: Printer settings first, then Models (with the parts list), then Flaws
        self.tabifyDockWidget(self.docks["Printer settings"], self.docks["Models"])
        self.tabifyDockWidget(self.docks["Models"], self.docks["Flaws"])
        self.docks["Printer settings"].raise_()

    def restore_default_windows(self) -> None:
        """Bring back every panel (closed or floated out) in its default place."""
        for d in self.docks.values():
            d.setFloating(False)
            d.show()
        if getattr(self, "_default_state", None) is None or not self.restoreState(self._default_state, LAYOUT_VERSION):
            self._default_layout()
        self.docks["Printer settings"].raise_()
        self.settings.setValue("windowState", self.saveState(LAYOUT_VERSION))

    # ----------------------------------------------------------------- setup
    def _act(self, menu, aid, text, slot, tip=None):
        """A menu action with a stable id; its shortcut comes from shortcuts.DEFAULTS or the
        user's change (Preferences → Keyboard)."""
        a = QAction(text, self)
        if tip:
            a.setStatusTip(tip)
        a.triggered.connect(slot)
        menu.addAction(a)
        return self.shortcuts.register(aid, a, f"{menu.title()} → {text}")

    def _menus(self):
        mb = self.menuBar()
        m = mb.addMenu("&File")
        self._act(m, "file.new", "&New project", self.project.new_project)
        self._act(m, "file.open", "&Open project…", self.project.open_dialog,
                  "Open a GooSlicer project, or any 3MF file as a new project")
        self.project.recent_menu = m.addMenu("Open &recent")
        self.project.update_recent_menu()
        self._act(m, "file.save", "&Save project", self.project.save)
        self._act(m, "file.save_as", "Save project &as…", self.project.save_as)
        m.addSeparator()
        self._act(m, "file.import", "&Import model…", self.import_dialog)
        self._act(m, "file.import_resin", "Import &resin profile…", self.import_resin_dialog,
                  "Add resin profiles exported from SatelLite (.satres)")
        m.addSeparator()
        self.slice_action = self._act(m, "file.slice_save", "&Slice and save .goo…", self.slice_dialog)
        self.send_action = self._act(m, "file.slice_send", "Slice and sen&d to printer…", self.slice_and_send)
        self._act(m, "file.open_goo", "&Open .goo for inspection…", self.open_goo_dialog)
        self._act(m, "file.locate_profiles", "Locate SatelLite &profiles…", self.locate_profiles)
        m.addSeparator()
        self._act(m, "file.quit", "&Quit", self.close)

        e = mb.addMenu("&Edit")
        u = self.undo.createUndoAction(self, "&Undo")
        r = self.undo.createRedoAction(self, "&Redo")
        e.addActions([u, r])
        self.shortcuts.register("edit.undo", u, "Edit → Undo")
        self.shortcuts.register("edit.redo", r, "Edit → Redo")
        e.addSeparator()
        self._act(e, "edit.select_all", "Select &all", lambda: self.select([p.id for p in self.scene.parts]))
        self._act(e, "edit.duplicate", "&Duplicate", self.duplicate_selected)
        self._act(e, "edit.delete", "De&lete", self.delete_selected)
        e.addSeparator()
        self._act(e, "edit.arrange", "&Arrange all", self.arrange_all)
        self._act(e, "edit.lay_flat", "Lay &flat (pick face)", self.start_lay_flat)
        self._act(e, "edit.repair", "&Repair mesh", self.repair_selected)
        hm = e.addMenu("Ha&ndles")
        hgroup = QActionGroup(self)
        self.handle_actions = {}
        for mode, label in (("move", "&Move"), ("rotate", "&Rotate"), ("scale", "&Scale")):
            a = self._act(hm, f"edit.handles.{mode}", label, lambda _=False, m=mode: self.viewport.set_handle_mode(m),
                          f"Show the {mode} handles in the 3D view")
            a.setCheckable(True)
            a.setChecked(mode == self.viewport.handle_mode)
            hgroup.addAction(a)
            self.handle_actions[mode] = a
        e.addSeparator()
        self._act(e, "edit.hollow", "&Hollow…", self.hollow_dialog)
        self._act(e, "edit.drain_holes", "Add drain &holes…", self.drain_hole_mode)
        self._act(e, "edit.check_voids", "Check for &voids", self.check_trapped_resin,
                  "Look for sealed cavities and suction cups that would trap uncured resin")
        e.addSeparator()
        self.prefs_action = self._act(e, "edit.preferences", "&Preferences…", self.open_preferences,
                                      "Every GooSlicer option, by category")

        sm = mb.addMenu("&Supports")
        self._act(sm, "supports.auto", "&Auto-support…", self.auto_support_dialog)
        self._act(sm, "supports.edit", "&Edit supports (click to add, drag to move, Ctrl+click to remove)", self.support_edit_mode)
        self._act(sm, "supports.clear", "&Clear supports of selected", self.clear_supports)
        sm.addSeparator()
        self._act(sm, "supports.islands", "&Detect islands", self.check_islands,
                  "Look for unsupported islands: regions with nothing under them in the layer below")

        pm = mb.addMenu("&Printer")
        self._act(pm, "printer.find", "&Find printers on network", self._show_lan(self.lan.find_printers))
        self._act(pm, "printer.add_ip", "&Add printer by IP…", self._show_lan(self.lan.add_by_ip_dialog))
        self._act(pm, "printer.connect", "&Connect / disconnect", self._show_lan(self.lan.toggle_connection))
        self._act(pm, "printer.send", "&Send .goo file…", self._show_lan(self.lan.send_dialog))

        rm = mb.addMenu("&Resin")
        self._act(rm, "resin.manage", "&Manage resins…", self.manage_resins,
                  "Browse, copy, hide, delete and export resin profiles")

        v = mb.addMenu("&View")
        self._act(v, "view.reset", "&Reset view", self.viewport.reset_view)
        self._act(v, "view.frame", "&Frame selection", self.viewport.frame_selection)
        self._act(v, "view.top", "&Top", lambda: self.viewport.set_view(-90, 89))
        self._act(v, "view.front", "F&ront", lambda: self.viewport.set_view(-90, 0))
        self._act(v, "view.side", "&Side", lambda: self.viewport.set_view(0, 0))
        v.addSeparator()
        self._act(v, "view.logs", "&Logs", self.log_window.popup, "Open the log in its own window")
        self._act(v, "view.layers", "&Layers", self.layers_window.popup,
                  "Open the layer preview of the sliced or opened .goo in its own window")
        v.addSeparator()
        self._act(v, "view.restore_windows", "Restore all &windows to default", self.restore_default_windows,
                  "Reopen closed panels and put every panel back in its default place")
        tm = v.addMenu("T&heme")
        group = QActionGroup(self)
        self.theme_actions = {}
        for key, label in THEMES:
            a = self._act(tm, f"view.theme.{key}", label, lambda _=False, k=key: self.set_theme(k))
            a.setCheckable(True)
            a.setChecked(key == self.theme.mode)
            group.addAction(a)
            self.theme_actions[key] = a

        h = mb.addMenu("&Help")
        self._act(h, "help.shortcuts", "&Keyboard shortcuts", self.show_shortcuts,
                  "Every keyboard shortcut and the mouse controls")
        self._act(h, "help.about", "&About GooSlicer", lambda: QMessageBox.about(
            self, APP, f"<b>{APP} {__version__}</b><p>Native Linux slicer for ELEGOO Saturn 4 "
                       "printers (.goo, SDCP v3 LAN).</p><p>Not affiliated with ELEGOO.</p>"
                       f"<p>{MOUSE_HELP}</p>"))

    def show_shortcuts(self):
        dlg = ShortcutsDialog(self.shortcuts, self)
        dlg.open()
        self._shortcuts_dialog = dlg
        return dlg

    def set_view_color(self, key: str, color) -> None:
        """A 3D view colour with its opacity (Edit → Preferences → 3D view colors); remembered."""
        self.viewport.colors[key] = QColor(color)
        colormod.save_color(self.settings, key, QColor(color))
        self.viewport.update()

    def set_theme(self, mode: str) -> None:
        """System (follow the desktop), light or dark; remembered across sessions."""
        self.theme.apply(mode)
        self.settings.setValue("theme", self.theme.mode)
        self.theme_actions[self.theme.mode].setChecked(True)

    def _show_lan(self, fn):
        def go():
            self.docks["Printer settings"].show()
            self.docks["Printer settings"].raise_()
            self.settings_scroll.ensureWidgetVisible(self.lan, 0, 0)
            fn()
        return go

    def log_msg(self, text: str):
        self.log.appendPlainText(text)

    # ------------------------------------------------------------ selection
    def selected_ids(self) -> list[int]:
        return list(self.viewport.selected)

    def primary(self) -> Part | None:
        ids = self.selected_ids()
        return self.scene.get(ids[0]) if ids else None

    def select(self, ids: list[int]):
        self.viewport.set_selection(ids, emit=False)
        self._sync_panels()

    def _on_viewport_selection(self, ids):
        self._sync_panels()

    def _sync_panels(self):
        self.parts.refresh(self.scene, self.selected_ids())
        p = self.primary()
        self.props.set_part(p, self.reports.get(id(p.mesh)) if p else None)
        if hasattr(self, "job_panel"):
            self.job_panel.refresh()
            self._check_held()

    def _handle_live(self) -> None:
        """A handle drag moved or scaled parts: the Mesh panel follows (the rest updates when the
        drag ends and becomes an undo step)."""
        p = self.primary()
        self.props.set_part(p, self.reports.get(id(p.mesh)) if p else None)

    def _handle_mode_changed(self, mode: str) -> None:
        self.settings.setValue("view/handleMode", mode)
        if mode in getattr(self, "handle_actions", {}):
            self.handle_actions[mode].setChecked(True)

    def scene_changed(self, select=None, mesh_changed=None):
        self.supports.sync()
        if select is not None:
            self.viewport.set_selection(select, emit=False)
        else:
            self.viewport.set_selection(self.selected_ids(), emit=False)
        self._sync_panels()
        self.viewport.update()

    # ---------------------------------------------------------------- edits
    def edit_parts(self, ids: list[int], text: str, fn) -> None:
        parts = [self.scene.get(i) for i in ids if self.scene.get(i)]
        before = {p.id: p.state() for p in parts}
        for p in parts:
            fn(p)
        changes = {p.id: (before[p.id], p.state()) for p in parts if p.state() != before[p.id]}
        if changes:
            self.undo.push(StateCommand(self, changes, text))
        else:
            self._sync_panels()

    def duplicate_selected(self):
        new = []
        for pid in self.selected_ids():
            d = self.scene.duplicate(pid)
            self.scene.remove(d.id)
            new.append(d)
        if new:
            self.undo.push(AddPartsCommand(self, new, "Duplicate"))

    def remove_part(self, pid: int) -> None:
        """Take one model off the build plate (the red x in the list); undoable."""
        if self.scene.get(pid) is not None:
            self.undo.push(RemovePartsCommand(self, [pid]))

    def add_instance(self, pid: int) -> None:
        """Another copy of a model, placed beside it on the plate (where it fits)."""
        if self.scene.get(pid) is None:
            return
        src = self.scene.get(pid)
        d = self.scene.duplicate(pid)
        # the free spot nearest the right of the original (a little apart), else anywhere free
        lo, hi = src.bounds()
        if not self.scene.place_free(d, near=[hi[0] + (hi[0] - lo[0]) / 2 + 3, (lo[1] + hi[1]) / 2]):
            self.log_msg(f"Add instance: no free room on the plate for another {src.name}.")
        self.scene.remove(d.id)
        self.undo.push(AddPartsCommand(self, [d], "Add instance"))

    def mirror_part(self, pid: int, axis: int = 1) -> None:
        """Mirror a model across the plane through its centre perpendicular to `axis` (1 = Y:
        front to back). Its supports are mirrored with it; undoable."""
        p = self.scene.get(pid)
        if p is None:
            return
        self.undo.push(MirrorCommand(self, pid, axis, f"Mirror {'XYZ'[axis]}"))

    def part_menu(self, pid: int) -> QMenu:
        """The right-click menu of a model in the 3D view."""
        m = QMenu(self)
        a = m.addAction("Add instance", lambda: self.add_instance(pid))
        a.setToolTip("Duplicate the model")
        a = m.addAction("Mirror", lambda: self.mirror_part(pid, 1))
        a.setToolTip("Mirror the model along Y (front to back)")
        return m

    def _part_context(self, pid: int, global_pos) -> None:
        self.select([pid])
        self._popup_menu(self.part_menu(pid), global_pos)

    def _popup_menu(self, menu: QMenu, global_pos) -> None:
        menu.exec(global_pos)

    def delete_selected(self):
        ids = self.selected_ids()
        if ids:
            self.undo.push(RemovePartsCommand(self, ids))

    def arrange_all(self):
        if not self.scene.parts:
            return
        before = {p.id: p.state() for p in self.scene.parts}
        fits = self.scene.arrange()
        changes = {p.id: (before[p.id], p.state()) for p in self.scene.parts if p.state() != before[p.id]}
        if changes:
            self.undo.push(StateCommand(self, changes, "Arrange"))
        if not fits:
            self.log_msg("Arrange: the parts do not all fit on the build plate.")

    def auto_position(self):
        """Turn the selected parts to the angles needing the fewest supports (orient.py), in a
        background job; one undo step."""
        from gooslicer import orient
        ids = [i for i in self.selected_ids() if self.scene.get(i)]
        if not ids or getattr(self, "_orienting", False):
            return
        params, _ = support_params(self.job_panel.support_type.currentData())
        jobs = [(p.id, p.name, p.world_vertices().copy(), np.asarray(p.mesh.faces).copy())
                for p in (self.scene.get(i) for i in ids)]
        self._orienting = True
        self.props.btn_auto.setEnabled(False)
        self.statusBar().showMessage("Auto Position: finding the angle that needs the fewest supports…")

        def work(progress=None):
            out = []
            for n, (pid, name, v, f) in enumerate(jobs):
                def tick(done, total, name=name, n=n):
                    if progress:
                        progress(f"{name}{f' ({n + 1}/{len(jobs)})' if len(jobs) > 1 else ''}: "
                                 f"{done}/{total} angles tried")
                out.append((pid, name, orient.best_orientation(v, f, params.critical_angle, params.anchor_dis,
                                                               progress=tick)))
            return out

        def step(text):
            self.statusBar().showMessage(f"Auto Position: {text}…")

        def finish():
            self._orienting = False
            self.props.btn_auto.setEnabled(self.primary() is not None)

        def failed(msg):
            finish()
            self.log_msg(f"Auto Position failed: {msg}")
            self.statusBar().showMessage("Auto Position failed", 8000)

        def done(results):
            finish()
            turn = {pid: r for pid, _, r in results if r.improves and self.scene.get(pid) is not None}
            for pid, name, r in results:
                if pid in turn:
                    self.log_msg(f"Auto Position {name}: tilted {r.tilt_deg:.0f}°; about "
                                 f"{r.current.contacts:.0f} → {r.best.contacts:.0f} support contacts, area "
                                 f"needing support {r.current.overhang_area:.0f} → {r.best.overhang_area:.0f} mm², "
                                 f"height {r.current.height:.1f} → {r.best.height:.1f} mm")
                else:
                    self.log_msg(f"Auto Position {name}: already at a good angle "
                                 f"(about {r.current.contacts:.0f} support contacts); left as it is")

            def apply(p):
                r = turn[p.id]
                before = p.bounds().mean(axis=0)
                p.rotation = rotation_between(r.up, [0, 0, 1]) @ p.rotation
                p.position[:2] += (before - p.bounds().mean(axis=0))[:2]
                self.scene.drop_to_plate(p)
            if turn:
                self.edit_parts(list(turn), "Auto Position", apply)
                self.statusBar().showMessage(
                    f"Auto Position: turned {len(turn)} part(s)" if len(results) > 1 else
                    f"Auto Position: tilted {next(iter(turn.values())).tilt_deg:.0f}° — see the Log", 8000)
            else:
                self.statusBar().showMessage("Auto Position: already at a good angle", 8000)
        workers.submit(work, on_done=done, on_failed=failed, on_progress=step)

    def start_lay_flat(self):
        if self.scene.parts:
            self.viewport.start_lay_flat()
            self.statusBar().showMessage("Lay flat: click the face that should rest on the plate (Esc cancels)")

    def _lay_flat(self, pid: int, face: int):
        self.edit_parts([pid], "Lay flat", lambda p: self.scene.lay_flat(p, face))
        self.statusBar().clearMessage()

    # ------------------------------------------------------- hollow / drain holes
    def _replace_mesh(self, pid, mesh, text, report=None):
        """Swap in a new mesh as an undo step. Pass the mesh's analysis if it was made off the
        GUI thread (it takes about a second for 300k triangles)."""
        if self.scene.get(pid) is None:
            return
        self.reports.pop(id(self.scene.get(pid).mesh), None)
        self.undo.push(MeshCommand(self, pid, mesh, text))
        new = self.scene.get(pid).mesh
        self.reports[id(new)] = report if report is not None else meshmod.analyze(new)
        meshmod.slim(new)
        trim_memory_soon()
        self._sync_panels()

    def hollow_dialog(self):
        p = self.primary()
        if not p:
            QMessageBox.information(self, APP, "Select a part to hollow.")
            return
        dlg = HollowDialog(self.settings, self)
        if dlg.exec() != HollowDialog.Accepted:
            return
        dlg.save()
        self.hollow_selected(dlg.value())

    def hollow_selected(self, hs):
        p = self.primary()
        if not p:
            return
        pid, name = p.id, p.name
        infill = (f", {hs.infill_type.upper()} infill {hs.infill_size:g} mm cells, struts r {hs.infill_radius:g} mm"
                  if hs.infill and hs.hollow_type != "open_bottom" else "")
        self.log_msg(f"Hollowing {name} ({hs.hollow_type}, wall {hs.thickness} mm, accuracy {hs.accuracy} mm{infill})…")
        self.statusBar().showMessage(f"Hollowing {name}…")

        def done(res):
            mesh, r = res
            self._replace_mesh(pid, mesh, "Hollow")
            self.log_msg(f"Hollowed {name}: {r.cavities} cavit{'y' if r.cavities == 1 else 'ies'}, "
                         f"volume {r.original_volume:.1f} → {mesh.volume * float(np.prod(np.abs(self.scene.get(pid).scale))):.1f} mm³. "
                         "Add drain holes (Ctrl+Shift+D) so resin can escape.")
            self.statusBar().showMessage("Hollowed — add drain holes", 8000)
            self.hollowFinished.emit(True)

        def failed(msg):
            self.log_msg("Hollow failed: " + msg.splitlines()[0])
            QMessageBox.warning(self, APP, "Hollow failed: " + msg.splitlines()[0])
            self.statusBar().clearMessage()
            self.hollowFinished.emit(False)
        workers.submit_process(jobs.hollow_mesh, p.mesh, p.scale.copy(), hs, on_done=done, on_failed=failed)

    def drain_hole_mode(self):
        """Show the drain-hole settings over the top-left of the 3D view and drill where the
        user clicks; the blue preview under the cursor follows the settings."""
        if not self.scene.parts:
            return
        panel = getattr(self, "hole_panel", None)
        if panel is None:
            panel = self.hole_panel = DrainHolePanel(self.settings, self.viewport)
            panel.fields.changed.connect(self._hole_settings_changed)
            panel.finished.connect(self.viewport.stop_hole_mode)
            panel.entered.connect(self.viewport.clear_hole_hover)
            self.viewport.holeModeEnded.connect(panel.hide)
            self.viewport.hole_preview_maker = lambda part, face, hit: hole_preview_mesh(
                part, face, hit, *self.hole_panel.fields.value())
        if self.viewport.support_mode:
            self.viewport.stop_support_mode()
        self.viewport.lay_flat_mode = False
        panel.fields.load()
        self._hole_settings = panel.fields.value()
        panel.place()
        panel.show()
        self.viewport.start_hole_mode()
        self.viewport.setFocus()
        self.statusBar().showMessage("Drain holes: click on a part to drill (Esc or Done to finish)")

    def _hole_settings_changed(self) -> None:
        f = self.hole_panel.fields
        f.save()
        self._hole_settings = f.value()
        self.viewport.refresh_hole_preview()

    def _hole_picked(self, pid, face, hit):
        """A click in drain-hole mode: the hole is drilled in the background (the view stays
        responsive); clicks made meanwhile wait their turn."""
        p = self.scene.get(pid)
        if p is None:
            return
        s, direction = getattr(self, "_hole_settings", None) or DrainHoleDialog(self.settings).value()
        self._hole_queue.append((pid, np.asarray(hit, float), hole_axis_world(p, face, direction), s))
        self._drill_next()

    def drilling(self) -> bool:
        return self._drilling or bool(self._hole_queue)

    def _drill_next(self) -> None:
        if self._drilling:
            return
        while self._hole_queue:
            pid, hit, axis, s = self._hole_queue.pop(0)
            p = self.scene.get(pid)
            if p is not None:
                break
        else:
            return
        src = p.mesh
        snap = SimpleNamespace(mesh=src, position=p.position.copy(), rotation=p.rotation.copy(),
                               scale=p.scale.copy())
        name = p.name
        self._drilling = True
        left = len(self._hole_queue)
        self.statusBar().showMessage(f"Drilling a drain hole in {name}…" + (f" ({left} more waiting)" if left else ""))

        def work():
            mesh = drill_part_at(snap, hit, axis, s)
            return mesh, meshmod.analyze(mesh)

        def done(res):
            self._drilling = False
            mesh, rep = res
            now = self.scene.get(pid)
            if now is not None and now.mesh is src:         # not undone or changed meanwhile
                self._replace_mesh(pid, mesh, "Drain hole", report=rep)
                self.log_msg(f"Drain hole in {name} at ({hit[0]:.1f}, {hit[1]:.1f}, {hit[2]:.1f}), "
                             f"Ø{2 * s.radius1:.1f} mm")
                self.viewport.repick_hole()                 # the surface under the cursor changed
            self._drill_finished()

        def failed(msg):
            self._drilling = False
            self.log_msg(f"Drain hole failed: {msg.splitlines()[0]}")
            self._drill_finished()
        workers.submit(work, on_done=done, on_failed=failed)

    def _drill_finished(self) -> None:
        if self._hole_queue:
            self._drill_next()
        elif self.viewport.hole_mode:
            self.statusBar().showMessage("Drain holes: click on a part to drill (Esc or Done to finish)")
        else:
            self.statusBar().clearMessage()

    def trapped_resin(self) -> list:
        return hollowmod.sealed_cavities([x.world_mesh() for x in self.scene.parts], cups=True)

    # ------------------------------------------------------------------- flaws
    @staticmethod
    def _same_parts(a, b) -> bool:
        """Same parts, meshes and placement (supports aside) in two plate fingerprints' part lists."""
        return len(a) == len(b) and all(x[0] == y[0] and x[1] is y[1] and x[3:] == y[3:] for x, y in zip(a, b))

    def _is_dismissed(self, kind: str, point) -> bool:
        return flawmod.dismissed(point, self._dismissed[kind])

    def _set_flaws(self, kind: str, flaws: list) -> None:
        """A check's result: numbered afresh, so that kind's selection is cleared."""
        self.flaws[kind] = flaws
        self.viewport.clear_guide_selection(kind)
        self._apply_flaws(kind)

    def _apply_flaws(self, kind: str) -> None:
        """Show `kind`'s flaws in the 3D view (red surfaces, numbered guide lines) and the list."""
        fl = self.flaws[kind]
        meshes = [m for f in fl for m in f.red]
        segs, nums = [], []
        for f in fl:
            seg = island_guides([f.point], self.scene.size)
            if len(seg):                         # none for a point outside the build area
                segs.append(seg)
                nums.append(f.number)
        lines = np.concatenate(segs) if segs else np.zeros((0, 3))
        vp = self.viewport
        if kind == "island":
            vp.island_meshes, vp.island_lines, vp.island_numbers = meshes, lines, nums
        else:
            vp.cavity_meshes, vp.cavity_lines, vp.cavity_numbers = meshes, lines, nums
            vp.cavity_cups = {f.number for f in fl if f.is_cup}
        vp.update()
        if hasattr(self, "flaws_panel"):
            self.flaws_panel.refresh(self.flaws["island"] + self.flaws["cavity"], vp.guide_selection())

    def dismiss_flaw(self, key) -> None:
        """Drop a flaw the user judged a false positive (Flaws tab, red x). Checking again leaves
        it out until a part's mesh or placement changes."""
        kind, number = key
        f = next((x for x in self.flaws[kind] if x.number == number), None)
        if f is None:
            return
        if not any(self._dismissed.values()):
            self._dismissed_parts = self._plate_fingerprint()[2]
        self._dismissed[kind].append(f.point)
        self.flaws[kind] = [x for x in self.flaws[kind] if x is not f]
        self.viewport.selected_guides.discard(key)
        if not self.flaws[kind]:
            if kind == "island":
                self._islands_fp = None
            else:
                self._cavities_fp = None
        self._apply_flaws(kind)
        self.log_msg(f"Dismissed {f.title} ({f.detail}) as a false positive.")
        if hasattr(self, "job_panel"):
            self.job_panel.flaws_changed(kind, len(self.flaws[kind]))

    def clear_flaws(self) -> None:
        """Forget every check's result and dismissal (a new or opened project)."""
        for kind in flawmod.KINDS:
            self._set_flaws(kind, [])
            if hasattr(self, "job_panel"):
                self.job_panel.flaws_changed(kind, 0)
        self._islands_fp = self._cavities_fp = None
        self.set_dismissed({})

    def dismissed_points(self) -> dict:
        return {k: list(v) for k, v in self._dismissed.items()}

    def set_dismissed(self, d: dict) -> None:
        """Dismissals saved with a project: they belong to its parts as they are now."""
        self._dismissed = {k: [np.asarray(x, float) for x in d.get(k, [])] for k in flawmod.KINDS}
        self._dismissed_parts = self._plate_fingerprint()[2] if any(self._dismissed.values()) else None

    def _check_dismissed(self) -> None:
        if self._dismissed_parts is not None and not self._same_parts(self._dismissed_parts, self._plate_fingerprint()[2]):
            self._dismissed = {k: [] for k in flawmod.KINDS}
            self._dismissed_parts = None

    def show_cavities(self, cav, reds, fp) -> list:
        """Show the sealed cavities' walls in red, with a numbered guide line to the edge of the
        build area, until a part changes. Dismissed ones are left out; returns those shown."""
        kept = [(c, r) for c, r in zip(cav or [], list(reds or []) + [[]] * len(cav or []))
                if not self._is_dismissed("cavity", c.lowest)]
        self._set_flaws("cavity", flawmod.cavity_flaws([c for c, _ in kept], [r for _, r in kept]))
        self._cavities_fp = fp if kept else None
        return [c for c, _ in kept]

    def _check_cavities_shown(self) -> None:
        if self._cavities_fp is None:
            return
        # supports don't change cavities: only the parts' meshes and placement count
        if not self._same_parts(self._cavities_fp[2], self._plate_fingerprint()[2]):
            self.show_cavities([], [], None)
            if hasattr(self, "job_panel"):
                self.job_panel.cavities_out_of_date()

    def _trapped_resin_shown(self) -> list:
        """The trapped-resin check, shown in the 3D view."""
        fp = self._plate_fingerprint()
        meshes = [x.world_mesh() for x in self.scene.parts]
        cav, red = _cavities_and_faces(meshes) if meshes else ([], [])
        return self.show_cavities(cav, red, fp)

    def check_trapped_resin(self) -> bool:
        cav = self._trapped_resin_shown()
        if not cav:
            self.log_msg("No sealed cavities or suction cups: resin can drain from every part.")
            self.statusBar().showMessage("No trapped resin", 5000)
            return True
        self._report_cavities(cav)
        return False

    def check_trapped_resin_async(self, on_done) -> None:
        """The trapped-resin check in the background; shows the cavities in red, reports to the
        Log and calls on_done(cavities), or on_done(None) if it failed."""
        meshes = [x.world_mesh() for x in self.scene.parts]
        fp = self._plate_fingerprint()
        if not meshes:
            self.show_cavities([], [], None)
            on_done([])
            return

        def done(res):
            cav, red = res
            cav = self.show_cavities(cav, red, fp)
            if cav:
                self._report_cavities(cav)
            else:
                self.log_msg("No sealed cavities or suction cups: resin can drain from every part.")
            on_done(cav)

        def failed(msg):
            self.log_msg("Trapped-resin check failed: " + msg.splitlines()[0])
            on_done(None)
        workers.submit_process(jobs.cavities_and_faces, meshes, then=_reds_ready, on_done=done, on_failed=failed)

    def seal_cavities(self, on_done) -> None:
        """Make every sealed cavity solid: check in the background, delete the shells that only
        line a cavity (one undo step), then check again. Calls on_done(message)."""
        parts = [(p.id, p.mesh) for p in self.scene.parts]
        meshes = [p.world_mesh() for p in self.scene.parts]
        if not meshes:
            on_done("No parts to check.")
            return

        def done(res):
            cav, removes = res
            cav = [c for c in cav if not self._is_dismissed("cavity", c.lowest)]
            if not cav:
                cups = sum(f.is_cup for f in self.flaws["cavity"])
                if not cups:
                    self.show_cavities([], [], None)
                on_done("✓ No sealed cavities: nothing to seal." + (
                    f" {cups} suction cup{'' if cups == 1 else 's'} can't be sealed: add a drain hole or "
                    "turn the part." if cups else ""))
                return
            todo = [(pid, mesh, rm) for (pid, mesh), rm in zip(parts, removes)
                    if rm.any() and self.scene.get(pid) is not None and self.scene.get(pid).mesh is mesh]
            if todo:
                self.undo.beginMacro("Seal cavities")
                for pid, mesh, rm in todo:
                    self._replace_mesh(pid, hollowmod.without_faces(mesh, rm), "Seal cavity")
                self.undo.endMacro()

            def rechecked(left):
                if left is None:
                    on_done("The check after sealing failed; see the Log.")
                    return
                cups = [c for c in left if c.kind == hollowmod.CUP]
                left = [c for c in left if c.kind != hollowmod.CUP]
                sealed = max(len(cav) - len(left), 0)
                msg = (f"Sealed {sealed} cavit{'y' if sealed == 1 else 'ies'}: "
                       "the part is solid there now (Edit → Undo brings the hollow back)." if sealed
                       else "Nothing was sealed.")
                if left:
                    msg += (f" ⚠ {len(left)} cavit{'y' if len(left) == 1 else 'ies'} could not be sealed "
                            "(shown in red): its wall is also the outside of a part. Add a drain hole "
                            "instead.")
                if cups:
                    msg += (f" {len(cups)} suction cup{'' if len(cups) == 1 else 's'} can't be sealed: "
                            "add a drain hole or turn the part.")
                self.log_msg("Seal cavities: " + msg)
                on_done(msg)
            self.check_trapped_resin_async(rechecked)

        def failed(msg):
            self.log_msg("Seal cavities failed: " + msg.splitlines()[0])
            on_done("Sealing failed; see the Log.")
        workers.submit_process(jobs.cavity_shells, meshes, on_done=done, on_failed=failed)

    def _report_cavities(self, cav):
        lines = [f"• {f.title}: {f.detail}" for f in flawmod.cavity_flaws(cav[:8], [[]] * len(cav[:8]))]
        self.log_msg(f"{flawmod.trapped_summary(cav)} will trap resin:\n" + "\n".join(lines))
        return lines

    def _confirm_no_trapped_resin(self) -> bool:
        cav = self._trapped_resin_shown()
        if not cav:
            return True
        lines = self._report_cavities(cav)
        return QMessageBox.warning(
            self, APP, f"{flawmod.trapped_summary(cav)} will trap uncured resin:\n\n" + "\n".join(lines) +
            "\n\n" + flawmod.trapped_advice(cav) + " Slice anyway?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    # ------------------------------------------------------------------- supports
    def template_dir(self) -> Path | None:
        ini = getattr(self, "_profile_ini", None)
        return support_defaults.script_dir_for(ini) if ini else None

    def support_templates(self) -> list:
        d = self.template_dir()
        p = self.printer_panel.current()
        return support_defaults.load_templates(d, p.name if p else None) if d else []

    def auto_support_dialog(self):
        if not self.scene.parts:
            return
        sel = [self.scene.get(i) for i in self.selected_ids()]
        h = max(float(np.ptp(p.world_vertices()[:, 2])) for p in (sel or self.scene.parts))
        dlg = SupportDialog(self.support_templates(), h, bool(sel), self)
        if dlg.exec() != SupportDialog.Accepted:
            return
        params, name = dlg.params()
        targets = self.scene.parts if dlg.apply_to_all() else sel
        for p in targets:
            self.auto_support(p, params, name)

    def auto_support(self, part, params, template: str = ""):
        """Lift the part to params.lift, generate supports in the background, push one undo step."""
        pid = part.id
        before = SupportsCommand.snapshot(part)
        lifted = part.state()
        lifted.position[2] += params.lift - part.bounds()[0][2]
        model = part.world_mesh()
        model.apply_translation([0, 0, lifted.position[2] - part.position[2]])
        frame = Part(part.name, part.mesh, lifted.position.copy(), lifted.rotation.copy(), lifted.scale.copy())
        self.log_msg(f"Generating supports for {part.name} ({template or 'built-in values'})…")
        self.statusBar().showMessage(f"Generating supports for {part.name}…")

        def done(res):
            contacts, result, islands = res
            p = self.scene.get(pid)
            if p is None:
                return
            plan = supportmod.SupportPlan.from_world(frame, contacts, params, template)
            frame.supports = plan
            frame.mesh = p.mesh
            self.supports.prime(pid, frame, result)      # reuse the geometry just built
            self.undo.push(SupportsCommand(self, pid, before, (plan, params.lift, lifted), "Auto-support"))
            ground = sum(not x.on_model for x in result.pillars)
            raft = f"{ground} to the raft" + (f" on {result.trunks} trunks" if result.trunks else "")
            self.log_msg(f"Supports for {p.name}: {len(result.pillars)} ({raft}, "
                         f"{len(result.pillars) - ground} on the model), {len(result.rejected)} contact(s) "
                         f"skipped, raft holes {result.raft_holes}"
                         + (f"; {len(islands)} island(s) still unsupported — add supports by hand (S)" if islands else ""))
            self.statusBar().showMessage("Supports generated", 5000)
            self.viewport.update()
            self.supportsFinished.emit(len(islands))

        def failed(msg):
            self.log_msg("Auto-support failed: " + msg.splitlines()[0])
            self.statusBar().clearMessage()
            self.supportsFinished.emit(-1)
        workers.submit_process(supportmod.auto_support, model, params, then=_auto_supported,
                               on_done=done, on_failed=failed)

    def clear_supports(self):
        for pid in self.selected_ids() or [p.id for p in self.scene.parts]:
            p = self.scene.get(pid)
            if p is None or p.supports is None:
                continue
            before = SupportsCommand.snapshot(p)
            st = p.state()
            st.position[2] -= p.bounds()[0][2]
            self.undo.push(SupportsCommand(self, pid, before, (None, 0.0, st), "Clear supports"))

    def support_edit_mode(self):
        if not self.scene.parts:
            return
        self.viewport.start_support_mode()
        self.statusBar().showMessage("Supports: click a part to add a support, drag a support to move "
                                     "it, Ctrl+click a support to remove it (Esc to finish)")

    def _support_hit(self, o, d):
        """Nearest support hit: (distance, part, world point) or None."""
        best = None
        for p in self.scene.parts:
            e = self.supports.entry(p) or self.supports.cache.get(p.id)
            if e is None or e.result.mesh is None:
                continue
            off = self.supports.offset(p, e)
            hit = ray_triangles(e.result.mesh, np.asarray(o) - off, np.asarray(d))
            if hit is not None:
                dist = float(np.linalg.norm(hit[1] + off - o))
                if best is None or dist < best[0]:
                    best = (dist, p, hit[1])
        return best

    def _contact_at(self, o, d):
        """The support under a ray: (part, its world contacts, index of the contact) or None."""
        sup = self._support_hit(o, d)
        if sup is None:
            return None
        _, p, local_hit = sup
        e = self.supports.cache.get(p.id)
        cs = p.supports.world_contacts(p) if p.supports is not None else []
        if not cs or e is None:
            return None
        if e.result.pillars:
            i = supportmod.nearest_pillar(e.result.pillars, local_hit)
            if i is None:
                return None
            target = e.result.pillars[i].contact.point + self.supports.offset(p, e)
            j = int(np.argmin([np.linalg.norm(c.point - target) for c in cs]))
        else:
            # imported geometry (SatelLite) has no pillars: the contact above the click in plan view
            w = local_hit + self.supports.offset(p, e)
            xy = np.array([np.linalg.norm(c.point[:2] - w[:2]) if c.point[2] >= w[2] - 0.5 else np.inf
                           for c in cs])
            j = int(np.argmin(xy))
            if xy[j] > 2.5:
                return None
        return p, cs, j

    @staticmethod
    def _world_normal(part, face) -> np.ndarray:
        nw = part.rotation @ (part.mesh.face_normals[face] / part.scale)
        return nw / np.linalg.norm(nw)

    def _support_drag(self, o0, d0, o1, d1):
        """A drag that starts on a support moves its contact to where it is dropped on the
        same part (08 §6)."""
        found = self._contact_at(np.asarray(o0, float), np.asarray(d0, float))
        if found is None:
            self.statusBar().showMessage("Supports: start a drag on a support to move it", 4000)
            return
        p, cs, j = found
        part, face, hit = self.scene.pick(np.asarray(o1, float), np.asarray(d1, float))
        if part is None or part.id != p.id:
            self.statusBar().showMessage("Supports: drop the support on the same part", 4000)
            return
        moved = supportmod.Contact(np.asarray(hit, float), self._world_normal(part, face), supportmod.MANUAL)
        before = SupportsCommand.snapshot(p)
        plan = p.supports.with_contacts(p, cs[:j] + [moved] + cs[j + 1:])
        self.undo.push(SupportsCommand(self, p.id, before, (plan, p.elevation, p.state()), "Move support"))

    def _support_click(self, o, d, remove: bool):
        o, d = np.asarray(o, float), np.asarray(d, float)
        part, face, hit = self.scene.pick(o, d)
        sup = self._support_hit(o, d)
        if remove:
            found = self._contact_at(o, d)
            if found is None:
                return
            p, cs, j = found
            before = SupportsCommand.snapshot(p)
            plan = p.supports.with_contacts(p, cs[:j] + cs[j + 1:])
            self.undo.push(SupportsCommand(self, p.id, before, (plan, p.elevation, p.state()), "Remove support"))
            return
        if part is None or (sup is not None and sup[0] < float(np.linalg.norm(hit - o))):
            return
        nw = self._world_normal(part, face)
        before = SupportsCommand.snapshot(part)
        if part.supports is None:
            params = supportmod.SupportParams()
            st = part.state()
            dz = params.lift - part.bounds()[0][2]
            st.position[2] += dz
            contact = supportmod.Contact(np.asarray(hit) + [0, 0, dz], nw, supportmod.MANUAL)
            frame = Part(part.name, part.mesh, st.position, st.rotation, st.scale)
            plan = supportmod.SupportPlan.from_world(frame, [contact], params)
            self.undo.push(SupportsCommand(self, part.id, before, (plan, params.lift, st), "Add support"))
            return
        cs = part.supports.world_contacts(part) + [supportmod.Contact(np.asarray(hit), nw, supportmod.MANUAL)]
        plan = part.supports.with_contacts(part, cs)
        self.undo.push(SupportsCommand(self, part.id, before, (plan, part.elevation, part.state()), "Add support"))

    def _supports_updated(self):
        self.viewport.update()

    def unsupported_islands(self) -> list:
        meshes = [p.world_mesh() for p in self.scene.parts]
        meshes += [m for m in (self.supports.world_mesh(p) for p in self.scene.parts) if m is not None]
        h = self.print_settings.settings().layer_height if self.printer_panel.current() else 0.05
        return supportmod.unsupported_islands(meshes, layer_height=max(h, 0.05))

    def _island_meshes(self):
        meshes = [p.world_mesh() for p in self.scene.parts]
        meshes += [m for m in (self.supports.world_mesh(p) for p in self.scene.parts) if m is not None]
        return meshes

    def show_islands(self, isl, reds, fp) -> list:
        """Show the islands' unsupported surface in red, with a numbered guide line, until the
        plate changes. Dismissed ones are left out; returns those shown."""
        kept = [(i, r) for i, r in zip(isl or [], list(reds or []) + [[]] * len(isl or []))
                if not self._is_dismissed("island", i.point)]
        self._set_flaws("island", flawmod.island_flaws([i for i, _ in kept], [r for _, r in kept]))
        self._islands_fp = fp if kept else None
        return [i for i, _ in kept]

    def _check_islands_shown(self) -> None:
        if self._islands_fp is None:
            return
        now = self._plate_fingerprint()
        if not self._same_plate((None, None, self._islands_fp[2]), (None, None, now[2])):
            self.show_islands([], [], None)
            self.job_panel.islands_out_of_date()

    def check_islands(self) -> list:
        fp = self._plate_fingerprint()
        meshes = self._island_meshes()
        h = self.print_settings.settings().layer_height if self.printer_panel.current() else 0.05
        isl, red = _islands_and_faces(meshes, max(h, 0.05)) if meshes else ([], [])
        isl = self.show_islands(isl, red, fp)
        if not isl:
            self.log_msg("No unsupported islands: every region connects to the layer below.")
            self.statusBar().showMessage("No unsupported islands", 5000)
        else:
            self._report_islands(isl)
        return isl

    def check_islands_async(self, on_done) -> None:
        """The island check in the background (seconds on detailed models); reports to the Log and
        calls on_done(islands), or on_done(None) if it failed."""
        meshes = self._island_meshes()
        fp = self._plate_fingerprint()
        if not meshes:
            self.show_islands([], [], None)
            on_done([])
            return
        h = self.print_settings.settings().layer_height if self.printer_panel.current() else 0.05

        def done(res):
            isl, red = res
            isl = self.show_islands(isl, red, fp)
            if isl:
                self._report_islands(isl)
            else:
                self.log_msg("No unsupported islands: every region connects to the layer below.")
            on_done(isl)

        def failed(msg):
            self.log_msg("Island check failed: " + msg.splitlines()[0])
            on_done(None)
        workers.submit_process(jobs.islands_and_faces, meshes, max(h, 0.05), then=_reds_ready,
                               on_done=done, on_failed=failed)

    def fix_islands(self, params, template: str, on_done) -> None:
        """Check for islands, then support each one: a contact under it for a part that already
        has supports; a full set of supports (`params`) for a part that has none, since lifting a
        bare part onto a single support would leave its base hanging. on_done(message)."""
        def checked(isl):
            if isl is None:
                on_done("The island check failed; see the Log.")
                return
            if not isl:
                on_done("✓ No unsupported islands: nothing to fix.")
                return
            added, full, missed = self.support_islands(isl, params, template)
            bits = []
            if added:
                bits.append(f"Added {added} support{'s' if added != 1 else ''} under islands")
            if full:
                bits.append("generating supports for " + ", ".join(p.name for p in full))
            if missed:
                bits.append(f"{missed} island{'s' if missed != 1 else ''} could not be reached "
                            "(add those by hand: Supports → Edit supports)")
            msg = "; ".join(bits) + "." if bits else "Nothing could be added."
            msg = msg[0].upper() + msg[1:]
            self.log_msg("Fix islands: " + msg)

            def rechecked(left):
                if left is None:
                    on_done(msg + " The check afterwards failed; see the Log.")
                elif not left:
                    on_done(msg + " ✓ No unsupported islands remain.")
                else:
                    on_done(msg + f" ⚠ {len(left)} island{'s' if len(left) != 1 else ''} remain"
                            f"{'s' if len(left) == 1 else ''} (shown in red).")
            if not full:
                self.check_islands_async(rechecked)
                return
            waiting = {"n": len(full)}             # check again once every new support set is built

            def one_done(_n):
                waiting["n"] -= 1
                if waiting["n"] <= 0:
                    self.supportsFinished.disconnect(one_done)
                    self.check_islands_async(rechecked)
            self.supportsFinished.connect(one_done)
        self.check_islands_async(checked)

    def support_islands(self, isl, params, template: str = ""):
        """Add supports for `isl`. Returns (contacts added, parts given full supports, missed)."""
        meshes = {p.id: p.world_mesh() for p in self.scene.parts}
        per_part: dict[int, list] = {}
        missed = 0
        for i in isl:
            best = None
            for p in self.scene.parts:
                c = supportmod.island_contact(meshes[p.id], i.point)
                if c is None:
                    continue
                gap = float(i.point[2] - c.point[2])        # the underside straight below it
                if gap >= -1e-6 and (best is None or gap < best[0]):
                    best = (gap, p.id, c)
            if best is None:
                missed += 1
                continue
            per_part.setdefault(best[1], []).append(best[2])
        added, full = 0, []
        self.undo.beginMacro("Fix islands")
        try:
            for pid, cs in per_part.items():
                p = self.scene.get(pid)
                if p.supports is None:
                    full.append(p)
                    continue
                before = SupportsCommand.snapshot(p)
                plan = p.supports.with_contacts(p, p.supports.world_contacts(p) + cs)
                self.undo.push(SupportsCommand(self, pid, before, (plan, p.elevation, p.state()),
                                               "Fix islands"))
                added += len(cs)
        finally:
            self.undo.endMacro()
        for p in full:
            self.auto_support(p, params, template)
        return added, full, missed

    def _report_islands(self, isl) -> list[str]:
        lines = [f"• {f.title}: {f.detail}, at ({f.point[0]:.1f}, {f.point[1]:.1f})"
                 for f in flawmod.island_flaws(isl[:10], [[]] * len(isl[:10]))]
        if len(isl) > 10:
            lines.append(f"• … and {len(isl) - 10} more")
        self.log_msg(f"{len(isl)} unsupported island(s) would print in mid-air:\n" + "\n".join(lines))
        return lines

    def repair_selected(self):
        p = self.primary()
        if not p:
            return
        pid, name = p.id, p.name
        self.log_msg(f"Repairing {name}…")
        self.statusBar().showMessage(f"Repairing {name}…")

        def step(text):
            self.statusBar().showMessage(f"Repairing {name}: {text}…")

        def failed(msg):
            self.log_msg(f"Repair failed: {msg}")
            self.statusBar().showMessage(f"Repair of {name} failed", 8000)

        def done(res):
            fixed, rep = res
            if self.scene.get(pid) is None:
                self.statusBar().clearMessage()
                return
            self.reports[id(fixed)] = rep
            self.undo.push(MeshCommand(self, pid, fixed))
            # MeshCommand.set_mesh copies the mesh; register the stored one too
            self.reports[id(self.scene.get(pid).mesh)] = rep
            meshmod.slim(self.scene.get(pid).mesh)
            trim_memory_soon()
            self.log_msg(f"Repaired {self.scene.get(pid).name}: "
                         + ("OK" if rep.ok else "; ".join(rep.problems())))
            self.statusBar().showMessage(f"Repaired {name}" + ("" if rep.ok else " — problems remain (see the Mesh pane)"),
                                         8000)
            self._sync_panels()
        workers.submit(_repair_and_analyze, p.mesh, on_done=done, on_failed=failed, on_progress=step)

    # --------------------------------------------------------------- import
    def import_dialog(self):
        files, _ = QFileDialog.getOpenFileNames(self, "Import models", self.settings.value("lastDir", str(Path.home())),
                                                MODEL_FILTER)
        if files:
            self.settings.setValue("lastDir", str(Path(files[0]).parent))
            self.import_files(files)

    # ------------------------------------------------------------ drag and drop
    def dragEnterEvent(self, e):
        if any(u.isLocalFile() for u in e.mimeData().urls()):
            e.acceptProposedAction()
        else:
            e.ignore()

    def dragMoveEvent(self, e):
        self.dragEnterEvent(e)

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            e.acceptProposedAction()
            self.open_files(files)
        else:
            e.ignore()

    def open_files(self, files: list[str]):
        """Dropped or command-line files: .satres are resin profiles, anything else a model. A
        single GooSlicer project onto an empty, unchanged plate opens as the project (09 §4)."""
        resins = [f for f in files if Path(f).suffix.lower() == settingsmod.SATRES_EXT]
        models = [f for f in files if f not in resins]
        if (len(models) == 1 and not self.scene.parts and not self.project.is_modified()
                and Path(models[0]).suffix.lower() == ".3mf" and projmod.is_project(models[0])):
            if resins:
                self.import_resin_profiles(resins)
            self.project.open_project(models[0])
            return
        if resins:
            self.import_resin_profiles(resins)
        if models:
            self.import_files(models)

    def import_files(self, files: list[str]):
        for f in files:
            self._pending += 1
            self.log_msg(f"Loading {f}…")
            workers.submit(_load_and_analyze, f,
                           on_done=lambda res, f=f: self._imported(f, res),
                           on_failed=lambda msg, f=f: self._import_failed(f, msg))

    def _import_failed(self, f, msg):
        self._pending -= 1
        self.log_msg(f"Import failed for {Path(f).name}: {msg.splitlines()[0]}")
        QMessageBox.warning(self, APP, f"Could not import {Path(f).name}:\n{msg.splitlines()[0]}")
        self.importFinished.emit(0)

    def _imported(self, f, res):
        self._pending -= 1
        new = []
        for part, rep in res:
            part.position[:2] = 0.0
            self.reports[id(part.mesh)] = rep
            new.append(part)
            self.log_msg(f"Imported {part.name}: {rep.triangles:,} triangles, "
                         f"{' × '.join(f'{x:.2f}' for x in part.mesh.extents)} mm"
                         + ("" if rep.ok else f" — ⚠ {'; '.join(rep.problems())}"))
        for p in new:
            self.scene.drop_to_plate(p)
        self.undo.beginMacro(f"Import {Path(f).name}")
        self.undo.push(AddPartsCommand(self, new, "Add"))
        if self._overlaps(new) or len(new) > 1:
            self.arrange_all()
        self.undo.endMacro()
        for p in new:
            if not self.scene.in_volume(p):
                self.log_msg(f"⚠ {p.name} does not fit in the build volume of the selected printer.")
        self.viewport.frame_selection() if len(self.scene.parts) == len(new) else None
        self.importFinished.emit(len(new))

    def _overlaps(self, new: list[Part]) -> bool:
        ids = {p.id for p in new}
        others = [p for p in self.scene.parts if p.id not in ids]
        for a in new:
            la, ha = a.bounds()
            for b in others:
                lb, hb = b.bounds()
                if np.all(la[:2] < hb[:2]) and np.all(lb[:2] < ha[:2]):
                    return True
        return False

    # --------------------------------------------------------------- slicing
    def make_slice_job(self) -> slicermod.SliceJob:
        p = self.printer_panel.current()
        if p is None:
            raise ValueError("No printer selected — locate the SatelLite profiles first.")
        if not self.scene.parts:
            raise ValueError("Nothing to slice — import a model first.")
        outside = [x.name for x in self.scene.parts if not self.scene.in_volume(x)]
        if outside:
            raise ValueError("These parts are outside the build volume: " + ", ".join(outside))
        meshes = [x.world_mesh() for x in self.scene.parts]
        for x in self.scene.parts:
            sm = self.supports.world_mesh(x)
            if sm is not None:
                meshes.append(sm)
        rgb = slicermod.packed_rgb(preview.render(meshes))
        return slicermod.SliceJob(meshes, RasterSpec.from_profile(p), self.print_settings.settings(),
                                  p.name, machine_z=p.size_z, preview_rgb=rgb)

    # ---- the slice held in memory (Models → Slicing → Slice)
    def _plate_fingerprint(self):
        """What a slice depends on. Meshes and support plans are compared by identity: every edit
        replaces them (MeshCommand, SupportPlan.with_contacts) rather than changing them in place."""
        p = self.printer_panel.current()
        parts = tuple((x.id, x.mesh, x.supports, x.position.tobytes(), x.rotation.tobytes(), x.scale.tobytes())
                      for x in self.scene.parts)
        return (p.name if p else None, self.print_settings.settings(), parts)

    @staticmethod
    def _same_plate(a, b) -> bool:
        if a is None or b is None or a[0] != b[0] or a[1] != b[1] or len(a[2]) != len(b[2]):
            return False
        return all(x[0] == y[0] and x[1] is y[1] and x[2] is y[2] and x[3:] == y[3:]
                   for x, y in zip(a[2], b[2]))

    def held_slice(self):
        """The in-memory slice if it still matches the plate and settings, else None."""
        if self.held is not None and self._same_plate(self.held[1], self._plate_fingerprint()):
            return self.held[0]
        return None

    def _check_held(self) -> None:
        if not hasattr(self, "job_panel"):
            return
        self._check_dismissed()
        self._check_islands_shown()
        self._check_cavities_shown()
        if self.held is not None:
            self.job_panel.set_stale(self.held_slice() is None)

    def slicing(self) -> bool:
        return self._slice_thread is not None

    def slice_to_memory(self) -> None:
        """Slice the build area and keep it in memory (with the island check first)."""
        if self.slicing():
            return
        try:
            job = self.make_slice_job()
        except ValueError as e:
            QMessageBox.warning(self, APP, str(e))
            return
        if not self._confirm_no_trapped_resin():
            return
        fp = self._plate_fingerprint()

        def go():
            self._slicing_fp = fp
            self.start_slice(job, None)
        self._island_check_then(job, go)

    def export_held(self) -> bool:
        """Save the slice held in memory to a .goo file of the user's choice."""
        held = self.held_slice()
        if held is None:
            return False
        default = Path(self.settings.value("lastGooDir", str(Path.home()))) / f"{self.scene.parts[0].name}.goo"
        path, _ = QFileDialog.getSaveFileName(self, "Export sliced file", str(default), "ELEGOO GOO (*.goo)")
        if not path:
            return False
        if not path.lower().endswith(".goo"):
            path += ".goo"
        self.settings.setValue("lastGooDir", str(Path(path).parent))
        return self._save_held(held, path)

    def send_held(self) -> bool:
        """Upload the slice held in memory to the connected printer and start the print."""
        held = self.held_slice()
        if held is None:
            return False
        out = Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "sent"
        out.mkdir(parents=True, exist_ok=True)
        name = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.scene.parts[0].name) or "model"
        path = str(out / f"{name}.goo")
        if not self._save_held(held, path):
            return False
        self._show_lan(lambda: None)()
        return bool(self.lan.send_file(path, start=True))

    def _save_held(self, res, path: str) -> bool:
        try:
            goo.write(path, res.goo.header, res.goo.layers)
        except OSError as e:
            QMessageBox.warning(self, APP, f"Could not save {path}:\n{e}")
            return False
        self.log_msg(f"Saved the sliced plate held in memory to {path} ({res.layers} layers)")
        return True

    def slice_and_send(self):
        """Slice to the cache dir, then upload and (after confirmation) start on the connected printer."""
        sess = self.lan.session
        if not (sess and sess.connected):
            QMessageBox.warning(self, APP, "Connect to a printer first (Printer → Find printers).")
            self._show_lan(lambda: None)()
            return
        if self.held_slice() is not None:    # already sliced: send what is in memory
            self.send_held()
            return
        try:
            job = self.make_slice_job()
        except ValueError as e:
            QMessageBox.warning(self, APP, str(e))
            return
        if not self._confirm_no_trapped_resin():
            return

        def go():
            out = Path(QStandardPaths.writableLocation(QStandardPaths.CacheLocation)) / "sent"
            out.mkdir(parents=True, exist_ok=True)
            name = "".join(c if c.isalnum() or c in "-_." else "_" for c in self.scene.parts[0].name) or "model"
            self._send_after_slice = True
            self.start_slice(job, str(out / f"{name}.goo"))
        self._island_check_then(job, go)

    def slice_dialog(self):
        if self.held_slice() is not None:    # already sliced: just save what is in memory
            self.export_held()
            return
        try:
            job = self.make_slice_job()
        except ValueError as e:
            QMessageBox.warning(self, APP, str(e))
            return
        if not self._confirm_no_trapped_resin():
            return

        def go():
            default = Path(self.settings.value("lastGooDir", str(Path.home()))) / f"{self.scene.parts[0].name}.goo"
            path, _ = QFileDialog.getSaveFileName(self, "Save sliced file", str(default), "ELEGOO GOO (*.goo)")
            if path:
                if not path.lower().endswith(".goo"):
                    path += ".goo"
                self.settings.setValue("lastGooDir", str(Path(path).parent))
                self.start_slice(job, path)
        self._island_check_then(job, go)

    def _island_check_then(self, job: slicermod.SliceJob, proceed) -> None:
        """Look for unsupported islands at the job's layer height in the background (it takes
        seconds on detailed models), then call `proceed` unless the user cancels at the warning.
        A failed check is logged and does not block slicing."""
        self._set_slicing_enabled(False)
        self.statusBar().showMessage("Checking for unsupported islands…")
        h = max(job.settings.layer_height, 0.01)
        fp = self._plate_fingerprint()

        def done(res):
            isl, red = res
            self._set_slicing_enabled(True)
            self.statusBar().clearMessage()
            isl = self.show_islands(isl, red, fp)
            if isl:
                lines = self._report_islands(isl)
                if QMessageBox.warning(
                        self, APP, f"{len(isl)} unsupported island{'' if len(isl) == 1 else 's'} would "
                        "print in mid-air (they stick to the film and the print fails there):\n\n"
                        + "\n".join(lines) + "\n\nThey are shown in red. Add supports there "
                        "(Supports → Edit supports). Slice anyway?",
                        QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                    return
            proceed()

        def failed(msg):
            self._set_slicing_enabled(True)
            self.log_msg("Island check failed (slicing anyway): " + msg.splitlines()[0])
            proceed()
        workers.submit_process(jobs.islands_and_faces, job.meshes, h, then=_reds_ready,
                               on_done=done, on_failed=failed)

    def _set_slicing_enabled(self, on: bool) -> None:
        self.slice_action.setEnabled(on)
        self.send_action.setEnabled(on)

    def start_slice(self, job: slicermod.SliceJob, path: str | None):
        self.log_msg(f"Slicing {job.layer_count} layers ({job.settings.layer_height} mm) → "
                     + (path or "memory"))
        self.job_panel.slicing_started(job.layer_count)
        gpu = self.gpu_slicing()
        t = SliceThread(job, path, self, gpu=gpu)
        if gpu:
            self.log_msg("Slicing on the GPU (experimental)…")
        t.progress.connect(self._slice_progress)
        t.finishedOk.connect(self._slice_done)
        t.failed.connect(self._slice_failed)
        t.finished.connect(self._slice_cleanup)
        self._slice_thread = t
        self.slice_action.setEnabled(False)
        self.send_action.setEnabled(False)
        self._progress.setRange(0, max(1, job.layer_count))
        self._progress.setValue(0)
        self._progress.show()
        self._cancel_btn.show()
        t.start()

    def gpu_slicing(self) -> bool:
        return str(self.settings.value("gpuSlicing", "false")).lower() in ("true", "1")

    def set_gpu_slicing(self, on: bool) -> None:
        self.settings.setValue("gpuSlicing", bool(on))

    def _backend_text(self, res, detail: bool = False) -> str:
        """"GPU, 1.4 s"; with detail, "GPU (AMD Radeon …), 1.4 s"."""
        where = "GPU" if res.backend.startswith("gpu") else "CPU"
        dev = self._backend_device(res) if detail else ""
        return f"{where}{f' ({dev})' if dev else ''}, {res.seconds:.1f} s"

    @staticmethod
    def _backend_device(res) -> str:
        """The GPU's renderer string, or the CPU's model name."""
        if res.backend.startswith("gpu ("):
            return res.backend[5:-1]
        return cpu_name()

    def cancel_slice(self):
        if self._slice_thread:
            self._slice_thread.cancel()

    def _slice_progress(self, done, total):
        self._progress.setValue(done)
        self.job_panel.slicing_progress(done, total)
        self.statusBar().showMessage(f"Slicing… {done}/{total} layers")

    def _slice_done(self, res):
        m, s = divmod(res.print_time, 60)
        h, m = divmod(m, 60)
        note = self._slice_thread.job.backend_note if self._slice_thread else ""
        if note:
            self.log_msg(note)
        self.log_msg(f"Layers made on the {self._backend_text(res, detail=True)}")
        if res.goo is not None:              # sliced into memory
            self.held = (res, self._slicing_fp)
            self._slicing_fp = None
            self.log_msg(f"Sliced {res.layers} layers into memory: est. print time {h}h {m:02d}m {s:02d}s, "
                         f"volume {res.volume / 1000:.2f} ml, weight {res.weight:.2f} g")
            self.statusBar().showMessage(f"Sliced {res.layers} layers — estimated {h}h {m:02d}m", 15000)
            self.layer_preview.set_file(res.goo)
            self.job_panel.show_result(res)
            self._check_held()
            return
        self.job_panel.show_result(res, held=False)
        self.log_msg(f"Saved {res.path}: {res.layers} layers, est. print time {h}h {m:02d}m {s:02d}s, "
                     f"volume {res.volume:.1f} mm³, weight {res.weight:.2f} g, cost {res.price:.2f}")
        self.statusBar().showMessage(f"Sliced {res.layers} layers — estimated {h}h {m:02d}m", 15000)
        self.open_goo(res.path)
        if self._send_after_slice:
            self._send_after_slice = False
            self._show_lan(lambda: None)()
            self.lan.send_file(str(res.path), start=True)

    def _slice_failed(self, msg):
        self._send_after_slice = False
        self._slicing_fp = None
        self.job_panel.slicing_failed(msg)
        self.log_msg(f"Slicing {'cancelled' if msg == 'cancelled' else 'failed: ' + msg}")
        self.statusBar().showMessage("Slicing cancelled" if msg == "cancelled" else "Slicing failed", 8000)

    def _slice_cleanup(self):
        t = self._slice_thread
        if t is not None and t.gl is not None:
            t.gl.destroy()                       # the offscreen surface belongs to the GUI thread
        self.job_panel.slicing_finished()
        self._progress.hide()
        self._cancel_btn.hide()
        self.slice_action.setEnabled(True)
        self.send_action.setEnabled(True)
        self._slice_thread = None
        self._check_held()                   # Export / Send become available

    def open_goo_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open .goo", self.settings.value("lastGooDir", str(Path.home())),
                                              "ELEGOO GOO (*.goo)")
        if path:
            self.open_goo(path)

    def open_goo(self, path: str):
        try:
            g = goo.read(path)
        except (OSError, goo.GooError) as e:
            QMessageBox.warning(self, APP, f"Cannot read {Path(path).name}: {e}")
            return
        self.layer_preview.set_file(g)
        self.layers_window.popup()
        h = g.header
        self.log_msg(f"Opened {Path(path).name}: {h.machine_type}, {h.layer_count} layers, "
                     f"{h.resolution_x}×{h.resolution_y}, resin '{h.resin_profile}'")

    def closeEvent(self, e):
        if not getattr(self, "_close_ok", False):
            if self.project.busy():
                e.ignore()
                self.statusBar().showMessage("Wait for the project to finish saving.", 4000)
                return
            if self.project.is_modified():
                # Qt ignores close() from inside closeEvent, so only a save closes later
                answer = self.project.ask_unsaved()
                if answer == "cancel":
                    e.ignore()
                    return
                if answer == "save":
                    e.ignore()

                    def saved():
                        self._close_ok = True
                        self.close()
                    self.project.save(then=saved)
                    return
        for tw in (self.log_window, self.layers_window):
            if tw.isVisible():
                tw.close()                   # saves where it was
        if self._slice_thread:
            self._slice_thread.cancel()
            self._slice_thread.wait(10000)
        workers.shutdown_processes()
        self.camera.shutdown()
        self.memory_monitor.stop()
        self.project.shutdown()
        self.lan.shutdown()
        self.settings.setValue("geometry", self.saveGeometry())
        self.settings.setValue("windowState", self.saveState(LAYOUT_VERSION))
        super().closeEvent(e)

    # -------------------------------------------------------------- profiles
    def _printer_changed(self, *_):
        p = self.printer_panel.current()
        if p:
            self.settings.setValue("printer", p.name)
            self.scene.size = np.array([p.size_x, p.size_y, p.size_z])
            self._load_resins(p)
            if hasattr(self, "job_panel"):
                self.job_panel.refresh(reload=True)          # the printer's support templates
            self.scene_changed()

    def _load_resins(self, p):
        hidden = self.resin_marks("resinHidden")
        resins = [r for r in self.all_resins() if r.printer == p.name and r.key not in hidden]
        self.print_settings.set_sources(settingsmod.from_printer_profile(p), resins, self._last_resin(p.name))
        self.log_msg(f"{p.name}: {len(resins)} resin profile(s) available")

    def _official_resins(self) -> list:
        """SatelLite's own resin profiles (all printers), parsed once per install."""
        d = self.resin_dir()
        if not d:
            return []
        key = str(d)
        if getattr(self, "_resin_cache_key", None) != key:   # parse the ~2000 files once
            self._resin_cache = settingsmod.load_resin_profiles(d)
            self._resin_cache_key = key
        return self._resin_cache

    def resin_library_dir(self) -> Path:
        return data_dir() / "resins"

    def _imported_resins(self) -> list:
        if getattr(self, "_imported_cache", None) is None:
            lib = self.resin_library_dir()
            self._imported_cache = (settingsmod.load_imported_resins(lib)
                                    + settingsmod.load_imported_resins(lib / "custom", "custom"))
        return self._imported_cache

    def all_resins(self) -> list:
        """Every resin profile GooSlicer knows (all printers, hidden ones included), sorted."""
        deleted = self.resin_marks("resinDeleted")
        return [r for r in settingsmod.merge_resins(self._official_resins(), self._imported_resins())
                if r.key not in deleted]

    def resin_marks(self, key: str) -> set[str]:
        """'resinHidden' / 'resinDeleted': sets of ResinProfile.key."""
        v = self.settings.value(key, [])
        return {v} if isinstance(v, str) and v else set(v or [])

    def set_resin_mark(self, key: str, profile_key: str, on: bool) -> None:
        marks = self.resin_marks(key)
        marks = marks | {profile_key} if on else marks - {profile_key}
        self.settings.setValue(key, sorted(marks))

    def refresh_resins(self) -> None:
        """Re-read the resin library and refill the Resin profile list (after manager changes)."""
        self._imported_cache = None
        p = self.printer_panel.current()
        if p:
            self._load_resins(p)

    def open_preferences(self, page: str | None = None):
        from .preferences import PreferencesDialog
        dlg = PreferencesDialog(self)
        if page:
            dlg.show_page(page)
        dlg.open()
        self._preferences = dlg
        return dlg

    def set_resin_dir(self, path: str) -> bool:
        """Replace GooSlicer's copy of the resin .data files with those in another folder."""
        return self._replace_stored("resin", path)

    def set_template_dir(self, path: str) -> bool:
        """Replace GooSlicer's copy of the support templates with those in another folder."""
        return self._replace_stored("templates", path)

    def _replace_stored(self, kind: str, path: str) -> bool:
        what = "resin profiles" if kind == "resin" else "support templates"
        if not path:
            return False
        if self.profile_info() is None:
            QMessageBox.information(self, APP, "Locate your SatelLite profiles first (File → Locate "
                                               "SatelLite profiles…).")
            return False
        try:
            st = profile_store.replace_folder(profile_root(), kind, path)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, APP, f"Could not copy the {what}:\n{e}")
            return False
        n = st.resins if kind == "resin" else st.templates
        self.log_msg(f"Copied {n} {what} from {path}")
        if kind == "resin":
            self._resin_cache_key = None          # same folder, new files
            self.refresh_resins()
        else:
            self.job_panel.refresh(reload=True)
        return True

    def profile_info(self):
        """Where GooSlicer's copy of the SatelLite profiles came from (None when reading in place)."""
        ini = getattr(self, "_profile_ini", None)
        root = profile_root()
        return profile_store.info(root) if ini and profile_store.is_inside(ini, root) else None

    def manage_resins(self):
        from .resins import ResinManager
        dlg = ResinManager(self)
        dlg.open()
        self._resin_manager = dlg
        return dlg

    def import_resin_dialog(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self, "Import resin profile", self.settings.value("lastResinDir", str(Path.home())),
            "SatelLite resin profiles (*.satres);;All files (*)")
        if paths:
            self.settings.setValue("lastResinDir", str(Path(paths[0]).parent))
            self.import_resin_profiles(paths)

    def import_resin_profiles(self, paths: list[str]) -> int:
        """Copy .satres files into the data dir and add their profiles to the resin list. If the
        import holds one profile for the current printer, it is selected. Returns the count."""
        got = []
        for path in paths:
            try:
                _dest, profs = settingsmod.store_satres(path, data_dir() / "resins")
            except (OSError, ValueError) as e:
                self.log_msg(f"Resin import failed for {Path(path).name}: {e}")
                QMessageBox.warning(self, APP, f"Could not import {Path(path).name}:\n{e}")
                continue
            got.extend(profs)
            for r in profs:                                 # importing brings a deleted one back
                self.set_resin_mark("resinDeleted", r.key, False)
            by_printer: dict[str, int] = {}
            for r in profs:
                by_printer[r.printer or "no printer named"] = by_printer.get(r.printer or "no printer named", 0) + 1
            self.log_msg(f"Imported {len(profs)} resin profile(s) from {Path(path).name}: "
                         + ", ".join(f"{n} for {pr}" for pr, n in by_printer.items()))
        if not got:
            return 0
        self._imported_cache = None
        p = self.printer_panel.current()
        if p is None:
            self.log_msg("They will be listed once printer profiles are loaded.")
            return len(got)
        self._load_resins(p)
        mine = [r for r in got if r.printer == p.name]
        if len(mine) == 1:                                  # select a single new profile
            ps = self.print_settings
            i = next((i for i, r in enumerate(ps.resins) if r.name == mine[0].name), None)
            if i is not None:
                ps.resin.setCurrentIndex(ps.resin.findData(i))
        if mine:
            self.statusBar().showMessage(f"Imported {len(got)} resin profile(s); {len(mine)} for {p.name} "
                                         "added to the Resin profile list", 8000)
        else:
            others = sorted({r.printer for r in got if r.printer})
            self.statusBar().showMessage(f"Imported {len(got)} resin profile(s), for "
                                         f"{', '.join(others) or 'no named printer'}: choose that printer "
                                         "to use them", 10000)
        return len(got)

    def _remember_resin(self, name: str) -> None:
        """The user's resin pick, per printer ("" = Printer default), plus the last pick overall."""
        p = self.printer_panel.current()
        if p:
            self.settings.setValue(f"resin/{p.name}", name)
        self.settings.setValue("lastResin", name)

    def _last_resin(self, printer: str) -> str:
        """The resin to preselect: this printer's last pick, else the last pick on any printer
        (resin names are shared across the Saturn 4 models)."""
        key = f"resin/{printer}"
        if self.settings.contains(key):
            return str(self.settings.value(key) or "")
        return str(self.settings.value("lastResin", "") or "")

    def resin_dir(self) -> Path | None:
        ini = getattr(self, "_profile_ini", None)
        return settingsmod.resin_dir_for(ini) if ini else None

    def load_profiles(self) -> bool:
        """Printer profiles from GooSlicer's own copy. The first time, they're found in a SatelLite
        install (or where an older GooSlicer read them) and copied, with the resin profiles and
        support templates, so SatelLite isn't needed afterwards. $GOOSLICER_PROFILE_INI is read in
        place (development and tests)."""
        env = os.environ.get("GOOSLICER_PROFILE_INI")
        ini = Path(env) if env and Path(env).is_file() else profile_store.stored_ini(profile_root())
        if ini is None:
            saved = self.settings.value("profileIni", "") or None
            src = profiles.find_official_ini([x for x in (saved, data_dir() / "officialPlatform.ini") if x])
            if src:
                ini = self._store_profiles(src)
        if not ini:
            self.log_msg("No printer profiles found. Use File → Locate SatelLite profiles… and "
                         "pick your SatelLite install folder or officialPlatform.ini.")
            self.printer_panel.set_profiles({})
            return False
        ps = profiles.load_profiles(ini)
        self._profile_ini = ini
        self.printer_panel.set_profiles(ps, self.settings.value("printer"))
        self.log_msg(f"Loaded {len(ps)} Saturn 4 profile(s) from {ini}")
        return bool(ps)

    def locate_profiles(self):
        path, _ = QFileDialog.getOpenFileName(self, "SatelLite officialPlatform.ini", str(Path.home()),
                                              "officialPlatform.ini (officialPlatform.ini);;INI (*.ini)")
        if path:
            self.import_profiles(path)

    def _store_profiles(self, src, resin_src=None, script_src=None) -> Path:
        """Copy SatelLite's profiles from src (an officialPlatform.ini) into GooSlicer's data folder
        and return the copied ini; on failure, src itself (read in place). Resin profiles and
        support templates come from the given folders, else those an older GooSlicer remembered,
        else next to src, else the Wine install; none found keeps what the copy already has."""
        src = Path(src)
        install = profiles.find_official_ini()
        found = []
        for key, fn, cur in (("resinDir", settingsmod.resin_dir_for, resin_src),
                             ("scriptDir", support_defaults.script_dir_for, script_src)):
            if cur is None:
                saved = self.settings.value(key, "")
                cur = (Path(saved) if saved and Path(saved).is_dir() else
                       fn(src) or (fn(install) if install else None))
            found.append(cur)
        try:
            st = profile_store.copy_profiles(src, profile_root(), resin_src=found[0], script_src=found[1])
        except OSError as e:
            self.log_msg(f"Could not copy the SatelLite profiles ({e}); reading them from {src}")
            return src
        for key in ("profileIni", "resinDir", "scriptDir"):       # superseded by the copy
            self.settings.remove(key)
        self._resin_cache_key = None                              # same folder, new files
        self.log_msg(f"Copied SatelLite's printer profiles, {st.resins} resin profiles and {st.templates} "
                     f"support templates from {src} into {st.root}. SatelLite is no longer needed.")
        return st.ini

    def import_profiles(self, path: str) -> bool:
        """Copy (or refresh the copy of) the profiles from a SatelLite officialPlatform.ini."""
        if not profiles.load_profiles(path):
            QMessageBox.warning(self, APP, "That file contains no Saturn 4 printer profiles.")
            return False
        resins = settingsmod.resin_dir_for(path)
        scripts = support_defaults.script_dir_for(path)
        had = profile_store.info(profile_root())
        if os.environ.get("FLATPAK_ID") and not (resins and scripts) and not (had and had.resins and had.templates):
            # the file portal exposes only the picked file, not the install folder around it
            self.log_msg("Resin profiles or support templates were not found next to this file. In the "
                         "Flatpak, give GooSlicer read access to your SatelLite folder, then locate the "
                         "profiles again:\n  flatpak override --user --filesystem=\"/path/to/SatelLite:ro\" "
                         f"{os.environ['FLATPAK_ID']}")
        stored = self._store_profiles(path, resins, scripts)
        if not profile_store.is_inside(stored, profile_root()):     # couldn't copy: read in place
            ps = profiles.load_profiles(stored)
            self._profile_ini = Path(stored)
            self.printer_panel.set_profiles(ps, self.settings.value("printer"))
            return bool(ps)
        return self.load_profiles()



def main(argv=None) -> int:
    QSurfaceFormat.setDefaultFormat(default_format())
    app = QApplication(sys.argv if argv is None else argv)
    app.setApplicationName(APP)
    app.setOrganizationName(APP)
    app.setDesktopFileName(os.environ.get("FLATPAK_ID", "gooslicer"))   # must match the .desktop file
    w = MainWindow()
    w.show()
    w.project.check_recovery()           # before any files from the command line
    files = [a for a in app.arguments()[1:]
             if Path(a).suffix.lower() in (*meshmod.SUPPORTED, settingsmod.SATRES_EXT)]
    if files:
        w.open_files(files)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
