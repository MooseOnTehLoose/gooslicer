"""Edit → Preferences…: every GooSlicer option in one window, by category. Changes apply at once
(and are remembered), the same as changing them where they live in the main window."""
from __future__ import annotations

import time
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QIcon, QPixmap
from PySide6.QtWidgets import (QAbstractButton, QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGridLayout,
                               QGroupBox, QHBoxLayout, QLabel, QListWidget, QListWidgetItem, QLineEdit,
                               QPushButton, QScrollArea, QSlider, QSpinBox, QSplitter, QStackedWidget, QTableWidget,
                               QVBoxLayout, QWidget)

from gooslicer.support_defaults import DrainHoleSettings, HollowSettings

from . import colors as colormod
from .hollowing import DrainHoleFields, HollowFields
from .supporting import support_choices
from .theme import THEMES

AUTO_SUPPORT = "Automatic (SatelLite's rule: by part height)"
FOLDERS = (("projectDir", "Projects", "Where Open project… and Save project as… start"),
           ("lastDir", "Models", "Where Import model… starts"),
           ("lastGooDir", ".goo files", "Where Slice and save, Export and Open .goo start"),
           ("lastResinDir", "Resin profile imports", "Where Import resin profile… and resin exports start"))


def _note(text: str) -> QLabel:
    lab = QLabel(text)
    lab.setWordWrap(True)
    lab.setEnabled(False)                     # the palette's dimmed text
    return lab


def _path_label() -> QLabel:
    lab = QLabel()
    lab.setWordWrap(True)
    lab.setTextInteractionFlags(Qt.TextSelectableByMouse)
    return lab


class _Page:
    def __init__(self, name: str, widget: QWidget, reset=None, refresh=None):
        self.name, self.widget, self.reset, self.refresh = name, widget, reset, refresh

    def text(self) -> str:
        """Everything a search can match on this page."""
        out = [self.name]
        for w in self.widget.findChildren(QWidget):
            if isinstance(w, QGroupBox):
                out.append(w.title())
            elif isinstance(w, (QLabel, QAbstractButton)):
                out.append(w.text())
            elif isinstance(w, QComboBox):
                out += [w.itemText(i) for i in range(w.count())]
            elif isinstance(w, QTableWidget):
                out += [w.item(r, 0).text() for r in range(w.rowCount()) if w.item(r, 0)]
            out.append(w.toolTip())
        return " ".join(out).lower()


class PreferencesDialog(QDialog):
    def __init__(self, win):
        super().__init__(win)
        self.win = win
        self.setWindowTitle("Preferences")
        self.setWindowModality(Qt.WindowModal)
        g = win.geometry()
        w, h = min(900, g.width()), min(640, g.height())
        self.setGeometry(g.x() + (g.width() - w) // 2, g.y() + (g.height() - h) // 2, w, h)

        self.search = QLineEdit()
        self.search.setPlaceholderText("Search preferences")
        self.search.setClearButtonEnabled(True)
        self.categories = QListWidget()
        self.categories.setSpacing(2)
        self.stack = QStackedWidget()
        self.pages: list[_Page] = []
        for build in (self._appearance, self._colors, self._printer, self._resin, self._slicing, self._hollowing,
                      self._drain, self._supports, self._network, self._folders, self._windows, self._keyboard):
            page = build()
            self.pages.append(page)
            scroll = QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QScrollArea.NoFrame)
            holder = QWidget()
            lay = QVBoxLayout(holder)
            title = QLabel(page.name)
            f = title.font()
            f.setBold(True)
            f.setPointSizeF(f.pointSizeF() * 1.25)
            title.setFont(f)
            lay.addWidget(title)
            lay.addWidget(page.widget)
            lay.addStretch(1)
            scroll.setWidget(holder)
            self.stack.addWidget(scroll)
            QListWidgetItem(page.name, self.categories)

        left = QWidget()
        ll = QVBoxLayout(left)
        ll.setContentsMargins(0, 0, 0, 0)
        ll.addWidget(self.search)
        ll.addWidget(self.categories, 1)
        split = QSplitter()
        split.addWidget(left)
        split.addWidget(self.stack)
        split.setStretchFactor(1, 1)
        split.setSizes([200, w - 200])
        split.setChildrenCollapsible(False)

        self.reset_btn = QPushButton("Restore defaults")
        self.reset_btn.setToolTip("Put this page's options back to GooSlicer's defaults")
        self.reset_btn.clicked.connect(self.reset_page)
        close = QPushButton("Close")
        close.setDefault(True)
        close.clicked.connect(self.accept)
        bottom = QHBoxLayout()
        bottom.addWidget(self.reset_btn)
        bottom.addStretch(1)
        applied = _note("Changes apply straight away.")
        applied.setWordWrap(False)
        bottom.addWidget(applied)
        bottom.addWidget(close)
        lay = QVBoxLayout(self)
        lay.addWidget(split, 1)
        lay.addLayout(bottom)

        self.categories.currentRowChanged.connect(self._show_page)
        self.search.textChanged.connect(self._filter)
        self.categories.setCurrentRow(0)

    # ---------------------------------------------------------------- navigation
    def page(self, name: str) -> _Page:
        return next(p for p in self.pages if p.name == name)

    def show_page(self, name: str) -> None:
        row = self.pages.index(self.page(name))
        if self.categories.currentRow() == row:
            self._show_page(row)                     # already there: just bring it up to date
        else:
            self.categories.setCurrentRow(row)

    def _show_page(self, row: int) -> None:
        if row < 0:
            return
        self.stack.setCurrentIndex(row)
        p = self.pages[row]
        if p.refresh:
            p.refresh()
        self.reset_btn.setEnabled(p.reset is not None)

    def _filter(self, text: str) -> None:
        words = text.lower().split()
        first = None
        for i, p in enumerate(self.pages):
            hay = p.text()
            hit = all(wd in hay for wd in words)
            self.categories.item(i).setHidden(not hit)
            if hit and first is None:
                first = i
        cur = self.categories.currentRow()
        if first is not None and (cur < 0 or self.categories.item(cur).isHidden()):
            self.categories.setCurrentRow(first)

    def reset_page(self) -> None:
        p = self.pages[self.stack.currentIndex()]
        if p.reset:
            p.reset()
            if p.refresh:
                p.refresh()

    # ---------------------------------------------------------------- pages
    def _appearance(self) -> _Page:
        w = QWidget()
        f = QFormLayout(w)
        self.theme = QComboBox()
        for key, label in THEMES:
            self.theme.addItem(label.replace("&", ""), key)
        self.theme.setToolTip("System follows the desktop's light or dark setting")
        self.theme.currentIndexChanged.connect(lambda *_: self.win.set_theme(self.theme.currentData()))
        f.addRow("Theme", self.theme)
        f.addRow(_note("Also under View → Theme."))

        def refresh():
            self.theme.blockSignals(True)
            self.theme.setCurrentIndex(max(0, self.theme.findData(self.win.theme.mode)))
            self.theme.blockSignals(False)
        refresh()
        return _Page("Appearance", w, reset=lambda: self.win.set_theme("system"), refresh=refresh)

    def _colors(self) -> _Page:
        """Colour (a wheel, R/G/B or hex) and transparency of each kind of object in the 3D view."""
        w = QWidget()
        g = QGridLayout(w)
        g.setColumnStretch(3, 1)
        g.addWidget(_note("Color"), 0, 1)
        g.addWidget(_note("Transparency"), 0, 2, 1, 2)
        self.color_buttons, self.color_sliders, self.color_values = {}, {}, {}
        for row, (key, label, _rgb, _a, tip) in enumerate(colormod.VIEW_COLORS, start=1):
            name = QLabel(label)
            name.setToolTip(tip)
            btn = QPushButton()
            btn.setToolTip(f"Pick the color of: {label.lower()}")
            btn.setFixedWidth(110)
            btn.clicked.connect(lambda _=False, k=key, t=label: self._pick_color(k, t))
            sl = QSlider(Qt.Horizontal)
            sl.setRange(0, 100)
            sl.setToolTip(f"Transparency of: {label.lower()} (0% is solid)")
            sl.valueChanged.connect(lambda v, k=key: self._set_transparency(k, v))
            val = QLabel()
            val.setMinimumWidth(40)
            self.color_buttons[key], self.color_sliders[key], self.color_values[key] = btn, sl, val
            g.addWidget(name, row, 0)
            g.addWidget(btn, row, 1)
            g.addWidget(sl, row, 2)
            g.addWidget(val, row, 3)
        g.addWidget(_note("Parts outside the build volume stay red, and flaw surfaces stay red, "
                          "whatever the model colors."), len(colormod.VIEW_COLORS) + 1, 0, 1, 4)

        def refresh():
            for key in colormod.KEYS:
                self._show_color(key)

        def reset():
            for key in colormod.KEYS:
                self.win.set_view_color(key, colormod.default_color(key))
        refresh()
        return _Page("3D view colors", w, reset=reset, refresh=refresh)

    def _show_color(self, key: str) -> None:
        c = self.win.viewport.colors[key]
        pm = QPixmap(18, 18)
        pm.fill(QColor(c.rgb()))
        btn = self.color_buttons[key]
        btn.setIcon(QIcon(pm))
        btn.setText(c.name(QColor.HexRgb))
        sl = self.color_sliders[key]
        sl.blockSignals(True)
        sl.setValue(round((1 - c.alphaF()) * 100))
        sl.blockSignals(False)
        self.color_values[key].setText(f"{sl.value()}%")

    def _set_transparency(self, key: str, value: int) -> None:
        c = QColor(self.win.viewport.colors[key])
        c.setAlphaF(1 - value / 100)
        self.win.set_view_color(key, c)
        self.color_values[key].setText(f"{value}%")

    def _pick_color(self, key: str, title: str) -> None:
        """The colour wheel dialog; the 3D view follows it live and goes back on Cancel."""
        before = QColor(self.win.viewport.colors[key])
        dlg = colormod.ColorDialog(before, title, self)
        dlg.colorChanged.connect(lambda c: (self.win.set_view_color(key, c), self._show_color(key)))
        self._color_dialog = dlg                       # tests reach it while it's open
        ok = dlg.exec()
        self._color_dialog = None
        if not ok:
            self.win.set_view_color(key, before)
        self._show_color(key)

    def _printer(self) -> _Page:
        win = self.win
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        f = QFormLayout()
        self.printer = QComboBox()
        self.printer.setModel(win.printer_panel.combo.model())        # the same list as Printer settings
        self.printer.currentIndexChanged.connect(
            lambda i: win.printer_panel.combo.setCurrentIndex(i) if i >= 0 else None)
        self.printer.setToolTip("The printer to slice for: sets the build volume, resolution and the "
                                "resin profiles offered")
        f.addRow("Printer", self.printer)
        lay.addLayout(f)

        g = QGroupBox("SatelLite profiles")
        gf = QFormLayout(g)
        self.profile_path = _path_label()
        locate = QPushButton("Copy again…")
        locate.setToolTip("Pick a SatelLite install's officialPlatform.ini to copy its printer and resin "
                          "profiles and support templates again, e.g. after updating SatelLite (also "
                          "File → Locate SatelLite profiles…)")
        locate.clicked.connect(lambda: (win.locate_profiles(), self._refresh_printer()))
        gf.addRow("Printer profiles", self._row(self.profile_path, locate))
        self.profile_source = _path_label()
        gf.addRow("Copied from", self.profile_source)
        self.resin_dir = _path_label()
        gf.addRow("Resin profiles", self._folder_row(
            self.resin_dir, "Copy resin profiles (.data files) from", lambda: win.resin_dir(), win.set_resin_dir))
        self.script_dir = _path_label()
        gf.addRow("Support templates", self._folder_row(
            self.script_dir, "Copy support templates (.satsup files) from", lambda: win.template_dir(),
            win.set_template_dir))
        gf.addRow(_note("GooSlicer keeps its own copy of your SatelLite profiles, made the first time "
                        "they were found, so SatelLite doesn't need to stay installed. Replace… copies "
                        "a different folder of resin profiles or support templates over the copy."))
        lay.addWidget(g)
        self._refresh_printer()
        return _Page("Printer", w, refresh=self._refresh_printer)

    def _refresh_printer(self) -> None:
        win = self.win
        self.printer.blockSignals(True)
        self.printer.setCurrentIndex(win.printer_panel.combo.currentIndex())
        self.printer.blockSignals(False)
        ini = getattr(win, "_profile_ini", None)
        info = win.profile_info()
        self.profile_path.setText(str(ini) if ini else "Not found")
        if info is not None:
            when = time.strftime("%Y-%m-%d %H:%M", time.localtime(info.copied)) if info.copied else "unknown date"
            self.profile_source.setText(f"{info.source or 'unknown'} ({when})")
        else:
            self.profile_source.setText("Not copied: read in place" if ini else "—")
        for lab, d, n, what in ((self.resin_dir, win.resin_dir(), info.resins if info else None, "profiles"),
                                (self.script_dir, win.template_dir(), info.templates if info else None,
                                 "templates")):
            lab.setText((f"{n} {what} · {d}" if n is not None else str(d)) if d else "Not found")

    def _resin(self) -> _Page:
        win = self.win
        w = QWidget()
        f = QFormLayout(w)
        src = win.print_settings.resin
        self.resin = QComboBox()
        self.resin.setModel(src.model())                       # the same list (and stars) as Printer settings
        self.resin.setIconSize(src.iconSize())
        self.resin.currentIndexChanged.connect(lambda i: src.setCurrentIndex(i) if i >= 0 else None)
        self.resin_for = QLabel()
        f.addRow("Resin profile", self.resin)
        f.addRow("", self.resin_for)
        manage = QPushButton("Manage resins…")
        manage.setToolTip("Favourites, hidden profiles, copies to edit, deleting and exporting "
                          "(also Resin → Manage resins…)")
        manage.clicked.connect(self._manage_resins)
        f.addRow("Resin library", manage)
        self.library = _path_label()
        f.addRow("Imported profiles", self.library)
        f.addRow(_note("The resin profile fills in layer height, exposures, delays and light power. "
                       "Change those under Printer settings → Advanced settings."))

        def refresh():
            self.resin.blockSignals(True)
            self.resin.setCurrentIndex(src.currentIndex())
            self.resin.blockSignals(False)
            p = win.printer_panel.current()
            self.resin_for.setText(f"Remembered for {p.name}" if p else "No printer selected")
            self.library.setText(str(win.resin_library_dir()))
        refresh()
        return _Page("Resin", w, refresh=refresh)

    def _manage_resins(self) -> None:
        self.accept()
        self.win.manage_resins()

    def _slicing(self) -> _Page:
        jp = self.win.job_panel
        w = QWidget()
        f = QFormLayout(w)
        self.gpu = QCheckBox(jp.gpu_box.text())
        self.gpu.setToolTip(jp.gpu_box.toolTip())
        self.gpu.toggled.connect(jp.gpu_box.setChecked)
        f.addRow("Slicing", self.gpu)
        f.addRow(_note("Draws the layers with OpenGL: several times faster. A few edge pixels per layer "
                       "can differ from the CPU, and a mesh with holes can slice wrongly (repair it "
                       "first). Falls back to the CPU if the GPU can't be used."))

        def refresh():
            self.gpu.blockSignals(True)
            self.gpu.setChecked(jp.gpu_box.isChecked())
            self.gpu.setEnabled(jp.gpu_box.isEnabled())
            self.gpu.blockSignals(False)
        refresh()
        return _Page("Slicing", w, reset=lambda: jp.gpu_box.setChecked(False), refresh=refresh)

    def _hollowing(self) -> _Page:
        self.hollow = HollowFields(self.win.settings)
        self.hollow.changed.connect(self.hollow.save)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.hollow)
        lay.addWidget(_note("The values the Hollow dialog starts with. Defaults are SatelLite's."))

        def reset():
            self.hollow.set_value(HollowSettings())
            self.hollow.save()
        return _Page("Hollowing", w, reset=reset, refresh=self.hollow.load)

    def _drain(self) -> _Page:
        self.drain = DrainHoleFields(self.win.settings)
        self.drain.changed.connect(self.drain.save)
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.drain)
        lay.addWidget(_note("The size and direction of drain holes added by clicking. Defaults are "
                            "SatelLite's."))

        def reset():
            self.drain.set_value(DrainHoleSettings(), "normal")
            self.drain.radius_lock.setChecked(True)
            self.drain.depth_lock.setChecked(True)
            self.drain.save()
        return _Page("Drain holes", w, reset=reset, refresh=self.drain.load)

    def _supports(self) -> _Page:
        win = self.win
        w = QWidget()
        f = QFormLayout(w)
        self.support = QComboBox()
        self.support.setToolTip("The support type the Supports pane uses. Automatic picks SatelLite's "
                                "template for the part's height.")
        self.support.activated.connect(
            lambda i: win.job_panel.set_support_choice(None if i == 0 else self.support.itemText(i)))
        f.addRow("Support type", self.support)
        f.addRow(_note("Also in Models → Supports. Templates come from your SatelLite install "
                       "(Printer page)."))

        def refresh():
            choices = [AUTO_SUPPORT] + [c[0] for c in support_choices(win.support_templates())]
            self.support.clear()
            self.support.addItems(choices)
            cur = win.job_panel.support_choice()
            self.support.setCurrentIndex(choices.index(cur) if cur in choices else 0)
        refresh()
        return _Page("Supports", w, reset=lambda: win.job_panel.set_support_choice(None), refresh=refresh)

    def _network(self) -> _Page:
        lan = self.win.lan
        w = QWidget()
        lay = QVBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(QLabel("Saved printers"))
        self.saved = QListWidget()
        self.saved.setToolTip("Printers found on the network or added by IP")
        lay.addWidget(self.saved)
        forget = QPushButton("Forget printer")
        forget.setEnabled(False)
        self.saved.currentRowChanged.connect(lambda r: forget.setEnabled(r >= 0))
        lay.addWidget(forget, 0, Qt.AlignLeft)
        lay.addWidget(_note("Find printers and add them by IP in Printer settings (or the Printer menu). "
                            "The printer in use can't be forgotten while connected."))

        def refresh():
            self.saved.clear()
            for p in lan.known:
                it = QListWidgetItem(f"{p.name or 'Printer'} — {p.machine_name or '?'} ({p.ip})", self.saved)
                it.setData(Qt.UserRole, p.mainboard_id or p.ip)
            forget.setEnabled(False)

        def go():
            it = self.saved.currentItem()
            if it is not None and lan.forget(it.data(Qt.UserRole)):
                refresh()
        forget.clicked.connect(go)
        refresh()
        return _Page("Network", w, refresh=refresh)

    def _folders(self) -> _Page:
        s = self.win.settings
        w = QWidget()
        f = QFormLayout(w)
        self.folder_labels = {}
        for key, label, tip in FOLDERS:
            lab = _path_label()
            lab.setToolTip(tip)
            self.folder_labels[key] = lab
            f.addRow(label, self._folder_row(lab, label, lambda k=key: s.value(k, "") or None,
                                             lambda path, k=key: s.setValue(k, path) if path else s.remove(k)))
        f.addRow(_note("Where file dialogs open. They follow the last folder used; Automatic means "
                       "your home folder."))
        proj = self.win.project
        self.autosave_spin = QSpinBox()
        self.autosave_spin.setRange(0, 120)
        self.autosave_spin.setSuffix(" min")
        self.autosave_spin.setSpecialValueText("Off")
        self.autosave_spin.setValue(proj.autosave_minutes())
        self.autosave_spin.valueChanged.connect(proj.set_autosave_minutes)
        f.addRow("Autosave every", self.autosave_spin)
        f.addRow(_note("While there are unsaved changes, a copy is kept in GooSlicer's own folder, "
                       "never over your file. If GooSlicer doesn't quit properly, it offers the copy "
                       "the next time it starts."))
        self.recent_spin = QSpinBox()
        self.recent_spin.setRange(1, 30)
        self.recent_spin.setValue(proj.recent_max())

        def set_recent(n):
            self.win.settings.setValue("recent/max", n)
            proj.update_recent_menu()
        self.recent_spin.valueChanged.connect(set_recent)
        f.addRow("Recent projects", self.recent_spin)

        def refresh():
            for key, lab in self.folder_labels.items():
                lab.setText(str(s.value(key, "") or Path.home()))
            for spin, value in ((self.autosave_spin, proj.autosave_minutes()), (self.recent_spin, proj.recent_max())):
                spin.blockSignals(True)
                spin.setValue(value)
                spin.blockSignals(False)

        def reset():
            for key, *_ in FOLDERS:
                s.remove(key)
            proj.set_autosave_minutes(5)
            s.remove("recent/max")
            proj.update_recent_menu()
        refresh()
        return _Page("Files and folders", w, reset=reset, refresh=refresh)

    def _keyboard(self) -> _Page:
        from .shortcuts import KeyboardPage
        self.keyboard = KeyboardPage(self.win.shortcuts)
        self.keyboard.setMinimumHeight(420)
        return _Page("Keyboard", self.keyboard, reset=self.keyboard.reset, refresh=self.keyboard.refresh)

    def _windows(self) -> _Page:
        w = QWidget()
        f = QFormLayout(w)
        restore = QPushButton("Restore all windows to default")
        restore.setToolTip("Reopen closed panels and put every panel back in its default place "
                           "(also View → Restore all windows to default)")
        restore.clicked.connect(self.win.restore_default_windows)
        f.addRow("Panels", restore)
        f.addRow(_note("Panel positions, the window size and the Logs and Layers windows' sizes are "
                       "remembered when you close GooSlicer."))
        return _Page("Windows", w)

    # ---------------------------------------------------------------- helpers
    @staticmethod
    def _row(label: QWidget, *buttons) -> QWidget:
        w = QWidget()
        h = QHBoxLayout(w)
        h.setContentsMargins(0, 0, 0, 0)
        h.addWidget(label, 1)
        for b in buttons:
            h.addWidget(b)
        return w

    def _folder_row(self, label: QLabel, title: str, current, apply) -> QWidget:
        """A path with a Replace… button: apply(folder) copies that folder's files over the copy."""
        choose = QPushButton("Replace…")
        choose.setToolTip(title + " another folder, replacing GooSlicer's copy")

        def pick():
            path = QFileDialog.getExistingDirectory(self, title, str(Path.home()))
            if path:
                apply(path)
                self._refresh_current()
        choose.clicked.connect(pick)
        return self._row(label, choose)

    def _refresh_current(self) -> None:
        p = self.pages[self.stack.currentIndex()] if self.pages else None
        if p and p.refresh:
            p.refresh()
