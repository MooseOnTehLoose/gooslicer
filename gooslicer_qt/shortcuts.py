"""Keyboard shortcuts: every main-window action's id and default shortcut, the user's changes,
the Preferences → Keyboard page and Help → Keyboard shortcuts (09 §7).

Defaults are written out rather than taken from QKeySequence's standard keys, which differ by
platform and desktop. Only changes from the default are stored (`shortcuts/<id>`, PortableText;
"" = no shortcut).
"""
from __future__ import annotations

from dataclasses import dataclass

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtGui import QAction, QKeySequence
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QHBoxLayout, QHeaderView, QKeySequenceEdit, QLabel,
                               QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
                               QWidget)

# id -> default shortcut ("" = none). Every action built with MainWindow._act has an entry here.
DEFAULTS: dict[str, str] = {
    "file.new": "Ctrl+N",
    "file.open": "Ctrl+O",
    "file.save": "Ctrl+S",
    "file.save_as": "Ctrl+Shift+S",
    "file.import": "Ctrl+I",
    "file.import_resin": "",
    "file.slice_save": "Ctrl+R",
    "file.slice_send": "Ctrl+Shift+R",
    "file.open_goo": "Ctrl+Shift+O",
    "file.locate_profiles": "",
    "file.quit": "Ctrl+Q",
    "edit.undo": "Ctrl+Z",
    "edit.redo": "Ctrl+Shift+Z",
    "edit.select_all": "Ctrl+A",
    "edit.duplicate": "Ctrl+D",
    "edit.delete": "Del",
    "edit.arrange": "A",
    "edit.lay_flat": "L",
    "edit.repair": "",
    "edit.handles.move": "W",
    "edit.handles.rotate": "E",
    "edit.handles.scale": "R",
    "edit.hollow": "Ctrl+Shift+H",
    "edit.drain_holes": "Ctrl+Shift+D",
    "edit.check_voids": "",
    "edit.preferences": "Ctrl+,",
    "supports.auto": "Ctrl+Shift+A",
    "supports.edit": "S",
    "supports.clear": "",
    "supports.islands": "",
    "printer.find": "",
    "printer.add_ip": "",
    "printer.connect": "",
    "printer.send": "",
    "resin.manage": "",
    "view.reset": "Home",
    "view.frame": "F",
    "view.top": "7",
    "view.front": "1",
    "view.side": "3",
    "view.logs": "",
    "view.layers": "",
    "view.restore_windows": "",
    "view.theme.system": "",
    "view.theme.light": "",
    "view.theme.dark": "",
    "help.shortcuts": "F1",
    "help.about": "",
}

MOUSE_HELP = ("Left button: select; drag a part to move it on the plate; drag a handle to move, turn "
              "or scale along it. Right button: orbit. Middle button or Shift+drag: pan. Wheel: zoom.")


def _text(seq: QKeySequence | str) -> str:
    return (seq if isinstance(seq, QKeySequence) else QKeySequence(seq)).toString(QKeySequence.PortableText)


def _native(seq: str) -> str:
    return QKeySequence(seq).toString(QKeySequence.NativeText)


@dataclass
class Entry:
    id: str
    action: QAction
    path: str            # "File → Save project"


class ShortcutRegistry(QObject):
    changed = Signal(str)            # action id

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self.entries: dict[str, Entry] = {}

    @staticmethod
    def default(aid: str) -> str:
        return _text(DEFAULTS[aid])

    def current(self, aid: str) -> str:
        key = f"shortcuts/{aid}"
        return _text(str(self.settings.value(key) or "")) if self.settings.contains(key) else self.default(aid)

    def register(self, aid: str, action: QAction, path: str) -> QAction:
        if aid not in DEFAULTS:
            raise KeyError(f"no default shortcut entry for action {aid!r}")
        self.entries[aid] = Entry(aid, action, path.replace("&", ""))
        action.setShortcut(QKeySequence(self.current(aid)))
        return action

    def set(self, aid: str, seq: QKeySequence | str) -> None:
        text = _text(seq)
        if text == self.default(aid):
            self.settings.remove(f"shortcuts/{aid}")
        else:
            self.settings.setValue(f"shortcuts/{aid}", text)
        self.entries[aid].action.setShortcut(QKeySequence(text))
        self.changed.emit(aid)

    def conflict(self, seq: QKeySequence | str, except_id: str | None = None) -> str | None:
        """The action that already uses this shortcut, if any."""
        text = _text(seq)
        if not text:
            return None
        return next((a for a in self.entries if a != except_id and self.current(a) == text), None)

    def reset_all(self) -> None:
        for aid in self.entries:
            self.settings.remove(f"shortcuts/{aid}")
            self.entries[aid].action.setShortcut(QKeySequence(self.default(aid)))
            self.changed.emit(aid)

    def rows(self) -> list[tuple[str, str, str]]:
        """(id, path, current shortcut as shown) in menu order."""
        return [(e.id, e.path, _native(self.current(e.id))) for e in self.entries.values()]


class KeyboardPage(QWidget):
    """Preferences → Keyboard: every action with an editable shortcut."""

    def __init__(self, registry: ShortcutRegistry, parent=None):
        super().__init__(parent)
        self.reg = registry
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        self.filter = QLineEdit(placeholderText="Filter actions or shortcuts")
        self.filter.setClearButtonEnabled(True)
        self.filter.textChanged.connect(self._filter)
        lay.addWidget(self.filter)
        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(["Action", "Shortcut", ""])
        self.table.verticalHeader().hide()
        self.table.setSelectionMode(QTableWidget.NoSelection)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        h = self.table.horizontalHeader()
        h.setSectionResizeMode(0, QHeaderView.Stretch)
        h.setSectionResizeMode(1, QHeaderView.Fixed)
        h.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.table.setColumnWidth(1, 170)
        lay.addWidget(self.table, 1)
        note = QLabel("Click a shortcut and press the new keys. A shortcut can be used by only one action.")
        note.setWordWrap(True)
        lay.addWidget(note)
        self.edits: dict[str, QKeySequenceEdit] = {}
        self._build()

    def _build(self) -> None:
        t = self.table
        t.setRowCount(0)
        self.edits.clear()
        for aid, path, _ in self.reg.rows():
            r = t.rowCount()
            t.insertRow(r)
            item = QTableWidgetItem(path)
            item.setData(Qt.UserRole, aid)
            item.setToolTip(aid)
            t.setItem(r, 0, item)
            ed = QKeySequenceEdit(QKeySequence(self.reg.current(aid)))
            ed.setMaximumSequenceLength(1)
            ed.editingFinished.connect(lambda a=aid: self._edited(a))
            t.setCellWidget(r, 1, ed)
            self.edits[aid] = ed
            clear = QPushButton("Clear")
            clear.setToolTip("No shortcut for this action")
            clear.clicked.connect(lambda _=False, a=aid: self.assign(a, ""))
            t.setCellWidget(r, 2, clear)
        self._filter(self.filter.text())

    def _filter(self, text: str) -> None:
        text = text.strip().lower()
        for r in range(self.table.rowCount()):
            aid = self.table.item(r, 0).data(Qt.UserRole)
            hay = f"{self.table.item(r, 0).text()} {_native(self.reg.current(aid))}".lower()
            self.table.setRowHidden(r, bool(text) and text not in hay)

    def ask_move(self, seq: str, other: str) -> bool:
        """Offer to move a shortcut that another action uses. Tests replace this."""
        path = self.reg.entries[other].path
        return QMessageBox.question(self, "Shortcut in use",
                                    f"{_native(seq)} is used by “{path}”. Move it here?",
                                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) == QMessageBox.Yes

    def _edited(self, aid: str) -> None:
        self.assign(aid, _text(self.edits[aid].keySequence()))

    def assign(self, aid: str, seq: str) -> bool:
        """Give `aid` this shortcut, moving it from another action if the user agrees."""
        seq = _text(seq)
        if seq == self.reg.current(aid):
            return True
        other = self.reg.conflict(seq, aid)
        if other is not None:
            if not self.ask_move(seq, other):
                self.refresh()
                return False
            self.reg.set(other, "")
        self.reg.set(aid, seq)
        self.refresh()
        return True

    def refresh(self) -> None:
        for aid, ed in self.edits.items():
            ed.blockSignals(True)
            ed.setKeySequence(QKeySequence(self.reg.current(aid)))
            ed.blockSignals(False)
        self._filter(self.filter.text())

    def reset(self) -> None:
        self.reg.reset_all()
        self.refresh()


class ShortcutsDialog(QDialog):
    """Help → Keyboard shortcuts: the current shortcuts, read-only, and the mouse controls."""

    def __init__(self, registry: ShortcutRegistry, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Keyboard shortcuts")
        self.resize(520, 600)
        lay = QVBoxLayout(self)
        rows = [(path, key) for _, path, key in registry.rows() if key]
        self.table = QTableWidget(len(rows), 2)
        self.table.setHorizontalHeaderLabels(["Action", "Shortcut"])
        self.table.verticalHeader().hide()
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        for r, (path, key) in enumerate(rows):
            self.table.setItem(r, 0, QTableWidgetItem(path))
            self.table.setItem(r, 1, QTableWidgetItem(key))
        lay.addWidget(self.table, 1)
        mouse = QLabel(MOUSE_HELP)
        mouse.setWordWrap(True)
        lay.addWidget(mouse)
        foot = QHBoxLayout()
        change = QPushButton("Change shortcuts…")
        change.clicked.connect(lambda: (self.accept(), parent.open_preferences("Keyboard")) if parent else None)
        foot.addWidget(change)
        foot.addStretch(1)
        box = QDialogButtonBox(QDialogButtonBox.Close)
        box.rejected.connect(self.reject)
        foot.addWidget(box)
        lay.addLayout(foot)
