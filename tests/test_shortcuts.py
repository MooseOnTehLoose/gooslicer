"""Keyboard shortcuts: one table of ids and defaults, Preferences → Keyboard, Help → Keyboard
shortcuts, and single-key shortcuts staying out of text fields (09 §7, §9)."""
import pytest

QtCore = pytest.importorskip("PySide6.QtCore")
from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QKeySequence  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from test_gui_smoke import _wait  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer_qt.main import MainWindow
    wins = []

    def make():
        s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
        w = MainWindow(settings=s)
        wins.append(w)
        return w
    yield make
    for w in wins:
        w.close()


def _key(w, aid):
    return w.shortcuts.entries[aid].action.shortcut().toString(QKeySequence.PortableText)


def test_defaults_table_matches_menus(make_window):
    from gooslicer_qt.shortcuts import DEFAULTS
    w = make_window()
    assert set(w.shortcuts.entries) == set(DEFAULTS)                  # nothing missing, nothing stale
    keys = [QKeySequence(v).toString(QKeySequence.PortableText) for v in DEFAULTS.values() if v]
    assert len(keys) == len(set(keys)), "two actions share a default shortcut"
    assert _key(w, "file.save") == "Ctrl+S" and _key(w, "file.save_as") == "Ctrl+Shift+S"
    assert _key(w, "edit.undo") == "Ctrl+Z" and _key(w, "help.shortcuts") == "F1"
    assert _key(w, "edit.repair") == ""
    assert w.shortcuts.entries["file.save"].path == "File → Save project"


def test_keyboard_page_changes_persist_and_reset(app, make_window):
    w = make_window()
    d = w.open_preferences("Keyboard")
    page = d.keyboard
    assert page.assign("file.save", "Ctrl+Alt+S")
    assert _key(w, "file.save") == "Ctrl+Alt+S" and w.settings.value("shortcuts/file.save") == "Ctrl+Alt+S"
    assert not w.settings.contains("shortcuts/file.open")              # only changes are stored
    assert page.edits["file.save"].keySequence().toString(QKeySequence.PortableText) == "Ctrl+Alt+S"
    assert page.assign("edit.lay_flat", "")                            # Clear
    assert _key(w, "edit.lay_flat") == "" and w.settings.value("shortcuts/edit.lay_flat") == ""
    d.close()
    w2 = make_window()                                                 # same settings file
    assert _key(w2, "file.save") == "Ctrl+Alt+S" and _key(w2, "edit.lay_flat") == ""
    d2 = w2.open_preferences("Keyboard")
    d2.reset_page()
    assert _key(w2, "file.save") == "Ctrl+S" and _key(w2, "edit.lay_flat") == "L"
    assert not any(k.startswith("shortcuts/") for k in w2.settings.allKeys())
    d2.close()


def test_conflict_moves_only_when_agreed(make_window, monkeypatch):
    w = make_window()
    page = w.open_preferences("Keyboard").keyboard
    asked = []
    monkeypatch.setattr(page, "ask_move", lambda seq, other: asked.append((seq, other)) or False)
    assert not page.assign("edit.repair", "Ctrl+S")
    assert asked == [("Ctrl+S", "file.save")]
    assert _key(w, "edit.repair") == "" and _key(w, "file.save") == "Ctrl+S"
    assert page.edits["edit.repair"].keySequence().isEmpty()           # the editor is put back
    monkeypatch.setattr(page, "ask_move", lambda seq, other: True)
    assert page.assign("edit.repair", "Ctrl+S")
    assert _key(w, "edit.repair") == "Ctrl+S" and _key(w, "file.save") == ""
    assert w.shortcuts.conflict("Ctrl+S", "edit.repair") is None


def test_search_finds_actions(make_window):
    w = make_window()
    d = w.open_preferences()
    d.search.setText("drain holes")
    shown = [d.categories.item(i).text() for i in range(d.categories.count()) if not d.categories.item(i).isHidden()]
    assert "Keyboard" in shown
    d.keyboard.filter.setText("ctrl+shift+s")
    visible = [d.keyboard.table.item(r, 0).text() for r in range(d.keyboard.table.rowCount())
               if not d.keyboard.table.isRowHidden(r)]
    assert visible == ["File → Save project as…"]
    d.close()


def test_help_lists_shortcuts(make_window):
    w = make_window()
    dlg = w.show_shortcuts()
    rows = {dlg.table.item(r, 0).text(): dlg.table.item(r, 1).text() for r in range(dlg.table.rowCount())}
    assert rows["File → Save project"] == QKeySequence("Ctrl+S").toString(QKeySequence.NativeText)
    assert "Edit → Repair mesh" not in rows                            # no shortcut: not listed
    dlg.close()


def test_single_keys_stay_out_of_fields(app, make_window):
    w = make_window()
    w.show()
    w.activateWindow()
    assert _wait(app, lambda: w.isActiveWindow(), 5)
    hits = []
    w.shortcuts.entries["edit.arrange"].action.triggered.connect(lambda: hits.append(1))
    w.viewport.setFocus()
    app.processEvents()
    QTest.keyClick(w.viewport, Qt.Key_A)
    assert hits == [1]                                                 # the 3D view: A arranges
    spin = w.print_settings.widgets["exposure"]
    spin.setFocus()
    app.processEvents()
    for key in (Qt.Key_A, Qt.Key_L, Qt.Key_S, Qt.Key_F, Qt.Key_1, Qt.Key_3, Qt.Key_7, Qt.Key_Home):
        QTest.keyClick(spin, key)
    assert hits == [1]                                                 # typed into the field instead
