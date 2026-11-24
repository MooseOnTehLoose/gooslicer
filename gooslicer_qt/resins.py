"""Resin → Manage resins…: browse every resin profile, see all of its values, copy it under a new
name, hide it from the Print settings list, delete it, or export it as a SatelLite .satres."""
from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import QEvent, QObject, QSize, Qt, QTimer
from PySide6.QtGui import QFont, QPalette
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
                               QDoubleSpinBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox,
                               QFrame, QPushButton, QScrollArea, QSpinBox, QSplitter, QTreeWidget,
                               QTreeWidgetItem, QVBoxLayout, QWidget)

from gooslicer import resin_library as L
from gooslicer import settings as S

from .slicing import STAR_SIZE, star_icon

SOURCES = {"satellite": "ELEGOO (SatelLite)", "imported": "Imported", "custom": "Your copy"}
ALL_PRINTERS = "All printers"
COLS = ("Name", "Printer", "Type", "Source", "Status")

# (field, label, unit, decimals | None for whole numbers, max)
GROUPS = (
    ("Layers and exposure", (
        ("layer_height", "Layer height", "mm", 3, 1.0),
        ("bottom_layers", "Bottom layers", "", None, 1000),
        ("transition_layers", "Transition layers", "", None, 1000),
        ("exposure", "Exposure", "s", 2, 1000.0),
        ("bottom_exposure", "Bottom exposure", "s", 2, 1000.0))),
    ("Delays and waits", (
        ("light_off_delay", "Light-off delay", "s", 2, 1000.0),
        ("bottom_light_off_delay", "Bottom light-off delay", "s", 2, 1000.0),
        ("wait_after_cure", "Wait after cure", "s", 2, 1000.0),
        ("bottom_wait_before_lift", "Bottom wait before lift", "s", 2, 1000.0),
        ("bottom_wait_after_lift", "Bottom wait after lift", "s", 2, 1000.0),
        ("bottom_wait_after_retract", "Bottom wait after retract", "s", 2, 1000.0))),
    ("Lift and retract", (
        ("bottom_lift_distance", "Bottom lift distance", "mm", 2, 1000.0),
        ("lift_distance", "Lift distance", "mm", 2, 1000.0),
        ("bottom_lift_speed", "Bottom lift speed", "mm/min", 1, 100000.0),
        ("lift_speed", "Lift speed", "mm/min", 1, 100000.0),
        ("bottom_retract_speed", "Bottom retract speed", "mm/min", 1, 100000.0),
        ("retract_speed", "Retract speed", "mm/min", 1, 100000.0))),
    ("Second-stage lift and retract", (
        ("slow_bottom_lift_distance", "Bottom lift distance", "mm", 2, 1000.0),
        ("slow_lift_distance", "Lift distance", "mm", 2, 1000.0),
        ("slow_bottom_lift_speed", "Bottom lift speed", "mm/min", 1, 100000.0),
        ("slow_lift_speed", "Lift speed", "mm/min", 1, 100000.0),
        ("slow_bottom_retract_distance", "Bottom retract distance", "mm", 2, 1000.0),
        ("slow_retract_distance", "Retract distance", "mm", 2, 1000.0),
        ("slow_bottom_retract_speed", "Bottom retract speed", "mm/min", 1, 100000.0),
        ("slow_retract_speed", "Retract speed", "mm/min", 1, 100000.0))),
    ("Light", (
        ("light_pwm", "Light PWM", "", None, 255),
        ("bottom_light_pwm", "Bottom light PWM", "", None, 255))),
    ("Resin", (
        ("density", "Density", "g/cm³", 3, 100.0),
        ("cost", "Cost (per kg)", "", 2, 100000.0))),
)
# SatelLite values GooSlicer keeps (and exports) but does not use: (.satres key, label, unit)
OTHER = (
    ("print/first_layer_exposure_time", "First-layer exposure", "s"),
    ("print/parameter_mode", "Parameter mode", ""),
    ("advanced/isanti", "Anti-aliasing", ""),
    ("advanced/antilevel", "Anti-aliasing level", ""),
    ("advanced/greylevel", "Grey level", ""),
    ("advanced/imageblur", "Image blur", ""),
    ("advanced/imageblurpixel", "Image blur pixels", ""),
    ("advanced/doubleblur", "Double blur", ""),
    ("advanced/supportAA", "Support anti-aliasing", ""),
    ("advanced/beamCompensation", "Beam compensation", ""),
    ("advanced/innertolerancecomp", "Inner tolerance compensation", ""),
    ("advanced/reducepoint", "Reduce point", ""),
    ("advanced/scale_x", "Scale X", ""),
    ("advanced/scale_y", "Scale Y", ""),
    ("advanced/scale_z", "Scale Z", ""),
    ("advanced/notes", "Notes", ""),
)


FAV, DIVIDER, REST = 0, 1, 2          # list groups: favourites, the divider line, the others
GROUP_ROLE = Qt.UserRole + 1


class _Item(QTreeWidgetItem):
    """Sorts within its group (favourites stay on top, then the divider) by the sorted column."""

    def __lt__(self, other):
        a, b = self.data(0, GROUP_ROLE), other.data(0, GROUP_ROLE)
        tw = self.treeWidget()
        if a != b:
            asc = tw is None or tw.header().sortIndicatorOrder() == Qt.AscendingOrder
            return (a < b) if asc else (a > b)       # Qt reverses the result for descending
        col = tw.sortColumn() if tw is not None else 0
        return self.text(col).casefold() < other.text(col).casefold()


class _StarClicks(QObject):
    """A click on a row's star toggles the favourite without selecting the row."""

    def __init__(self, mgr: "ResinManager"):
        super().__init__(mgr)
        self.mgr = mgr

    def star_hit(self, pos) -> str | None:
        tree = self.mgr.tree
        it = tree.itemAt(pos)
        if it is None or it.data(0, GROUP_ROLE) == DIVIDER:
            return None
        rect = tree.visualItemRect(it)
        if tree.header().logicalIndexAt(pos.x()) != 0 or pos.x() > rect.left() + STAR_SIZE + 8:
            return None
        return it.data(0, Qt.UserRole)

    def eventFilter(self, obj, ev):
        t = ev.type()
        if t in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick):
            key = self.star_hit(ev.position().toPoint())
            if key is not None:
                if t == QEvent.MouseButtonRelease and ev.button() == Qt.LeftButton:
                    self.mgr.toggle_favorite(key)
                return True
        return False


def _raw_value(r: S.ResinProfile, satres_key: str) -> str:
    group, jk = S._SATRES[satres_key]
    v = r.raw.get(group, {}).get(jk)
    if v is None or v == "":
        return "–"
    if isinstance(v, bool) or str(v).lower() in ("true", "false"):
        return "on" if str(v).lower() == "true" else "off"
    try:
        return f"{float(v):g}"
    except (TypeError, ValueError):
        return str(v)


class ResinManager(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Manage resins")
        self.setWindowModality(Qt.WindowModal)
        self.setGeometry(win.geometry())                 # the size of the main window
        self.profiles: dict[str, S.ResinProfile] = {}
        self._items: dict[str, QTreeWidgetItem] = {}     # listed rows by profile key
        self._divider: QTreeWidgetItem | None = None
        self.current: S.ResinProfile | None = None

        # ---- left: filters + list
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search resin profiles")
        self.search.setClearButtonEnabled(True)
        self.printer_filter = QComboBox()
        self.tree = QTreeWidget()
        self.tree.setColumnCount(len(COLS))
        self.tree.setHeaderLabels(COLS)
        self.tree.setRootIsDecorated(False)
        self.tree.setIconSize(QSize(STAR_SIZE, STAR_SIZE))
        self.tree.setToolTip("Click a star to add or remove a favourite. Favourites are listed first, "
                             "here and in Print settings.")
        self._star_on, self._star_off = star_icon(True), star_icon(False)
        self._star_clicks = _StarClicks(self)
        self.tree.viewport().installEventFilter(self._star_clicks)
        self.tree.setAlternatingRowColors(True)
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setSortingEnabled(True)
        self.tree.sortByColumn(0, Qt.AscendingOrder)
        hdr = self.tree.header()
        hdr.setStretchLastSection(False)
        hdr.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, len(COLS)):
            hdr.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        hdr.setMinimumSectionSize(48)
        self.count = QLabel()
        filters = QHBoxLayout()
        filters.addWidget(self.search, 1)
        filters.addWidget(self.printer_filter)
        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addLayout(filters)
        ll.addWidget(self.tree, 1)
        ll.addWidget(self.count)

        # ---- right: details
        self.title = QLabel()
        f = QFont(self.title.font())
        f.setPointSizeF(f.pointSizeF() * 1.4)
        f.setBold(True)
        self.title.setFont(f)
        self.title.setWordWrap(True)
        self.title.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.meta = {k: QLabel() for k in ("Printer", "Type", "Maker", "Source")}
        meta = QFormLayout()
        for k, w in self.meta.items():
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            w.setWordWrap(True)
            meta.addRow(k, w)
        self.copy_btn = QPushButton("Copy…")
        self.copy_btn.setToolTip("Make your own copy of this profile under a new name; copies can be changed")
        self.export_btn = QPushButton("Export…")
        self.export_btn.setToolTip("Save this profile as a SatelLite resin profile file (.satres)")
        self.delete_btn = QPushButton("Delete…")
        self.hide_chk = QCheckBox("Hide from the Resin profile list in Print settings")
        buttons = QHBoxLayout()
        for b in (self.copy_btn, self.export_btn, self.delete_btn):
            buttons.addWidget(b)
        buttons.addStretch(1)
        self.note = QLabel()
        self.note.setWordWrap(True)
        self.note.setForegroundRole(QPalette.PlaceholderText)
        self.status = QLabel()
        self.status.setWordWrap(True)

        self._forms: list[QFormLayout] = []
        self.fields: dict[str, QSpinBox | QDoubleSpinBox] = {}
        self.other: dict[str, QLabel] = {}
        props = QWidget()
        pl = QVBoxLayout(props)
        pl.setContentsMargins(0, 0, 0, 0)
        for gname, items in GROUPS:
            box = QGroupBox(gname)
            form = QFormLayout(box)
            self._forms.append(form)
            for field, label, unit, dec, hi in items:
                if dec is None:
                    w = QSpinBox()
                    w.setRange(0, int(hi))
                else:
                    w = QDoubleSpinBox()
                    w.setDecimals(dec)
                    w.setRange(0.0, hi)
                    w.setSingleStep(10 ** -dec * (5 if dec > 1 else 1))
                if unit:
                    w.setSuffix(f" {unit}")
                w.setMinimumWidth(170)
                w.valueChanged.connect(self._edited)
                self.fields[field] = w
                form.addRow(label, w)
            pl.addWidget(box)
        box = QGroupBox("Other SatelLite settings (kept and exported, not used by GooSlicer)")
        form = QFormLayout(box)
        self._forms.append(form)
        for key, label, unit in OTHER:
            w = QLabel()
            w.setTextInteractionFlags(Qt.TextSelectableByMouse)
            self.other[key] = w
            form.addRow(label, w)
        pl.addWidget(box)
        pl.addStretch(1)
        # one label column for every group
        labels = [f.itemAt(i, QFormLayout.LabelRole).widget() for f in self._forms for i in range(f.rowCount())
                  if f.itemAt(i, QFormLayout.LabelRole)]
        width = max(lbl.sizeHint().width() for lbl in labels)
        for lbl in labels:
            lbl.setMinimumWidth(width)
        for f in self._forms:
            f.setFieldGrowthPolicy(QFormLayout.FieldsStayAtSizeHint)

        self.details = QWidget()
        dl = QVBoxLayout(self.details)
        dl.addWidget(self.title)
        dl.addLayout(meta)
        dl.addLayout(buttons)
        dl.addWidget(self.hide_chk)
        dl.addWidget(self.note)
        dl.addWidget(self.status)
        dl.addWidget(props, 1)
        self.scroll = scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        scroll.setWidget(self.details)
        self.empty = QLabel("No resin profiles yet. Use File → Locate SatelLite profiles…, or import "
                            "a .satres file with File → Import resin profile…")
        self.empty.setWordWrap(True)
        self.empty.setAlignment(Qt.AlignCenter)
        right = QWidget()
        rl = QVBoxLayout(right)
        rl.setContentsMargins(0, 0, 0, 0)
        rl.addWidget(scroll, 1)
        rl.addWidget(self.empty, 1)

        split = QSplitter(Qt.Horizontal)
        split.addWidget(left)
        split.addWidget(right)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        split.setSizes([int(self.width() * 0.58), int(self.width() * 0.42)])
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        outer = QVBoxLayout(self)
        outer.addWidget(split, 1)
        outer.addWidget(close)

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(400)
        self._save_timer.timeout.connect(self.save_edits)

        self.search.textChanged.connect(lambda *_: self.populate())
        self.printer_filter.currentIndexChanged.connect(lambda *_: self.populate())
        self.tree.currentItemChanged.connect(self._current_changed)
        self.copy_btn.clicked.connect(self.copy_current)
        self.export_btn.clicked.connect(self.export_current)
        self.delete_btn.clicked.connect(self.delete_current)
        self.hide_chk.toggled.connect(self.set_hidden)

        self._fill_printer_filter()
        self.populate()

    # ---------------------------------------------------------------- list
    def _listed(self) -> list[S.ResinProfile]:
        """Profiles for the printers GooSlicer supports (SatelLite ships ones for every ELEGOO
        printer), plus everything imported or copied."""
        supported = set(getattr(self.win.printer_panel, "_profiles", {}))
        return [r for r in self.win.all_resins() if r.printer in supported or r.source != "satellite"]

    def _fill_printer_filter(self):
        printers = sorted({r.printer for r in self._listed() if r.printer}
                          | set(getattr(self.win.printer_panel, "_profiles", {})))
        self.printer_filter.blockSignals(True)
        self.printer_filter.clear()
        self.printer_filter.addItem(ALL_PRINTERS)
        self.printer_filter.addItems(printers)
        self.printer_filter.blockSignals(False)

    def populate(self, select: str | None = None):
        """Refill the list from the main window's resin sources, keeping (or moving to) a selection."""
        if select is None and self.current is not None:
            select = self.current.key
        everything = self._listed()
        self.profiles = {r.key: r for r in everything}
        hidden = self.win.resin_marks("resinHidden")
        q = self.search.text().strip().casefold()
        pf = self.printer_filter.currentText()
        dim = self.palette().color(QPalette.Disabled, QPalette.Text)
        self.tree.blockSignals(True)
        self.tree.setSortingEnabled(False)
        self.tree.clear()
        self._items, self._divider = {}, None
        pick = None
        favs = self.win.print_settings.favorites
        for r in everything:
            if pf and pf != ALL_PRINTERS and r.printer != pf:
                continue
            if q and q not in r.name.casefold() and q not in r.company.casefold():
                continue
            it = _Item([r.name, r.printer, r.resin_type, SOURCES.get(r.source, r.source),
                        "Hidden" if r.key in hidden else ""])
            it.setData(0, Qt.UserRole, r.key)
            self._set_star(it, r.key in favs)          # per printer, shared with Print settings
            if r.key in hidden:
                for c in range(len(COLS)):
                    it.setForeground(c, dim)
            self.tree.addTopLevelItem(it)
            self._items[r.key] = it
            if r.key == select:
                pick = it
        self._sync_divider()
        self.tree.setSortingEnabled(True)
        self.tree.blockSignals(False)
        n = len(self._items)
        self.count.setText(f"{n} of {len(everything)} resin profile(s)")
        if pick is None and n:
            pick = next(it for i in range(self.tree.topLevelItemCount())
                        if (it := self.tree.topLevelItem(i)) is not self._divider)
        self.tree.setCurrentItem(pick)
        if pick is not None:
            self.tree.scrollToItem(pick)
        self.show_profile(self.profiles.get(pick.data(0, Qt.UserRole)) if pick else None)

    def _current_changed(self, item, _prev):
        self.status.clear()
        self.show_profile(self.profiles.get(item.data(0, Qt.UserRole)) if item else None)

    # ---------------------------------------------------------------- details
    def show_profile(self, r: S.ResinProfile | None):
        self.save_edits()                                 # pending edits of the previous one
        self.current = r
        self.scroll.setVisible(r is not None)
        self.empty.setVisible(r is None and not self.profiles)
        if r is None:
            return
        self.title.setText(r.name)
        self.meta["Printer"].setText(r.printer or "–")
        self.meta["Type"].setText(r.resin_type or "–")
        self.meta["Maker"].setText(r.company or "–")
        src = SOURCES.get(r.source, r.source)
        if r.source == "imported" and r.path:
            src += f" ({Path(r.path).name})"
        self.meta["Source"].setText(src)
        editable = r.source == "custom"
        self.note.setText("Your copy: changes are saved as you make them." if editable else
                          "Read-only. Use Copy… to make your own version that you can change.")
        self.hide_chk.blockSignals(True)
        self.hide_chk.setChecked(r.key in self.win.resin_marks("resinHidden"))
        self.hide_chk.blockSignals(False)
        s = r.settings()
        for name, w in self.fields.items():
            w.blockSignals(True)
            w.setValue(getattr(s, name))
            w.setReadOnly(not editable)
            w.setButtonSymbols(QSpinBox.UpDownArrows if editable else QSpinBox.NoButtons)
            w.blockSignals(False)
        for key, w in self.other.items():
            w.setText(_raw_value(r, key))

    def _edited(self, *_):
        if self.current is not None and self.current.source == "custom":
            self._save_timer.start()

    def save_edits(self):
        """Write a copy's changed values to its file and refresh the Print settings list."""
        if not self._save_timer.isActive() or self.current is None:
            return
        self._save_timer.stop()
        r = self.current
        s = replace(r.settings(), **{k: w.value() for k, w in self.fields.items()})
        try:
            self.current = L.save_custom(L.with_settings(r, s))
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Manage resins", f"Could not save “{r.name}”:\n{e}")
            return
        self.win.refresh_resins()
        self.profiles[self.current.key] = self.current
        self.status.setText("Saved.")

    # ---------------------------------------------------------------- actions
    def copy_current(self):
        r = self.current
        if r is None:
            return None
        self.save_edits()
        existing = self.win.all_resins()
        name = L.copy_name(r.name, existing)
        while True:
            name, ok = QInputDialog.getText(self, "Copy resin profile",
                                            f"Name for the copy of “{r.name}”:", QLineEdit.Normal, name)
            if not ok:
                return None
            problem = L.check_new_name(name, existing)
            if problem is None:
                break
            QMessageBox.warning(self, "Copy resin profile", problem)
        try:
            new = L.create_copy(r, name, self.win.resin_library_dir() / "custom")
        except OSError as e:
            QMessageBox.warning(self, "Copy resin profile", f"Could not save the copy:\n{e}")
            return None
        self.win.set_resin_mark("resinDeleted", new.key, False)
        self.win.refresh_resins()
        self.search.blockSignals(True)
        self.search.clear()                             # make sure the new one is in view
        self.search.blockSignals(False)
        self.populate(select=new.key)
        self.status.setText(f"Copied to “{new.name}”. You can change its values below.")
        return new

    def export_current(self):
        r = self.current
        if r is None:
            return None
        self.save_edits()
        start = Path(self.win.settings.value("lastResinDir", str(Path.home())))
        safe = "".join(c if c not in '/\\:*?"<>|' else "_" for c in r.name).strip() or "resin"
        path, _ = QFileDialog.getSaveFileName(self, "Export resin profile", str(start / f"{safe}{S.SATRES_EXT}"),
                                              "SatelLite resin profiles (*.satres)")
        if not path:
            return None
        if not path.lower().endswith(S.SATRES_EXT):
            path += S.SATRES_EXT
        try:
            L.write_satres(path, [r])
        except OSError as e:
            QMessageBox.warning(self, "Export resin profile", f"Could not write {path}:\n{e}")
            return None
        self.win.settings.setValue("lastResinDir", str(Path(path).parent))
        self.status.setText(f"Exported to {path}")
        self.win.log_msg(f"Exported resin profile “{r.name}” to {path}")
        return path

    def delete_current(self):
        r = self.current
        if r is None:
            return False
        self._save_timer.stop()
        if r.source == "satellite":
            detail = ("It comes with SatelLite. GooSlicer will stop listing it; your SatelLite "
                      "installation is not changed.")
        elif r.source == "imported":
            detail = "It is removed from GooSlicer's copy of the imported file."
        else:
            detail = "Your copy is deleted for good."
        if QMessageBox.question(self, "Delete resin profile", f"Delete “{r.name}” ({r.printer})?\n\n{detail}",
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        try:
            if r.source in ("imported", "custom"):
                L.delete_from_file(r)
        except (OSError, ValueError) as e:
            QMessageBox.warning(self, "Delete resin profile", f"Could not delete “{r.name}”:\n{e}")
            return False
        self.win._imported_cache = None
        if r.source == "satellite" or any(x.key == r.key for x in self.win.all_resins()):
            # a built-in, or an import that was shadowing a built-in of the same name
            self.win.set_resin_mark("resinDeleted", r.key, True)
        self.win.set_resin_mark("resinHidden", r.key, False)
        row = self.tree.indexOfTopLevelItem(self.tree.currentItem())
        self.win.refresh_resins()
        self.current = None
        nxt = self.tree.topLevelItem(row + 1) or self.tree.topLevelItem(row - 1)
        self.populate(select=nxt.data(0, Qt.UserRole) if nxt else None)
        self.status.setText(f"Deleted “{r.name}”.")
        self.win.log_msg(f"Deleted resin profile “{r.name}” ({r.printer})")
        return True

    def _set_star(self, it: QTreeWidgetItem, fav: bool) -> None:
        it.setData(0, GROUP_ROLE, FAV if fav else REST)
        it.setIcon(0, self._star_on if fav else self._star_off)

    def _sync_divider(self) -> None:
        """A divider row between favourites and the rest, only when both are listed."""
        groups = {it.data(0, GROUP_ROLE) for it in self._items.values()}
        want = groups == {FAV, REST}
        if want and self._divider is None:
            div = _Item([""] * len(COLS))
            div.setData(0, GROUP_ROLE, DIVIDER)
            div.setFlags(Qt.NoItemFlags)
            div.setSizeHint(0, QSize(0, 9))
            self.tree.addTopLevelItem(div)
            div.setFirstColumnSpanned(True)
            line = QFrame()
            line.setFrameShape(QFrame.HLine)
            line.setFrameShadow(QFrame.Sunken)
            self.tree.setItemWidget(div, 0, line)
            self._divider = div
        elif not want and self._divider is not None:
            self.tree.takeTopLevelItem(self.tree.indexOfTopLevelItem(self._divider))
            self._divider = None

    def toggle_favorite(self, key: str) -> None:
        """Star / unstar one profile (this printer only), in place: shared with Print settings."""
        if key not in self.profiles:
            return
        self.win.print_settings.toggle_favorite(key)
        it = self._items.get(key)
        if it is None:
            return
        bar = self.tree.verticalScrollBar()
        pos = bar.value()
        self._set_star(it, key in self.win.print_settings.favorites)
        self._sync_divider()
        self.tree.sortItems(self.tree.sortColumn(), self.tree.header().sortIndicatorOrder())
        bar.setValue(pos)

    def set_hidden(self, on: bool):
        r = self.current
        if r is None:
            return
        self.win.set_resin_mark("resinHidden", r.key, on)
        self.win.refresh_resins()
        it = self.tree.currentItem()
        if it is not None:
            it.setText(4, "Hidden" if on else "")
            dim = self.palette().color(QPalette.Disabled, QPalette.Text)
            norm = self.palette().color(QPalette.Text)
            for c in range(len(COLS)):
                it.setForeground(c, dim if on else norm)
        self.status.setText("Hidden from the Resin profile list." if on else "Shown in the Resin profile list.")

    def done(self, result):
        self.save_edits()
        super().done(result)
