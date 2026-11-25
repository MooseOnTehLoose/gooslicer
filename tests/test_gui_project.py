"""Project commands in the window: title and unsaved marker, prompts, save and open, recent
files, drops (09 §4–5, §9)."""
from pathlib import Path

import numpy as np
import pytest

QtCore = pytest.importorskip("PySide6.QtCore")
from PySide6.QtWidgets import QApplication, QFileDialog  # noqa: E402

from test_gui_smoke import _wait  # noqa: E402
from test_profiles import FIXTURE  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def make_window(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer_qt.main import MainWindow
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    wins = []

    def make(name="s.ini"):
        s = QtCore.QSettings(str(tmp_path / name), QtCore.QSettings.IniFormat)
        w = MainWindow(settings=s)
        if w.printer_panel.current() is None:
            assert w.import_profiles(str(src))
        w.project.mark_saved()
        wins.append(w)
        return w
    yield make
    for w in wins:
        w.close()


def _stl(tmp_path, name="cube", size=(10, 10, 10)):
    import trimesh
    f = tmp_path / f"{name}.stl"
    trimesh.creation.box(size).export(f)
    return f


def _import(app, w, f):
    n = len(w.scene.parts)
    w.import_files([str(f)])
    assert _wait(app, lambda: len(w.scene.parts) > n)


def _save_as(app, w, monkeypatch, path):
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(path), ""))
    done = []
    w.project.saved.connect(done.append)
    w.project.save_as()
    assert _wait(app, lambda: done)
    return done[0]


def test_title_follows_edits_and_undo(app, make_window, tmp_path):
    w = make_window()
    assert w.windowTitle() == "Untitled[*] — GooSlicer" and not w.isWindowModified()
    _import(app, w, _stl(tmp_path))
    assert w.isWindowModified()
    while w.undo.canUndo():
        w.undo.undo()
    assert not w.isWindowModified() and not w.scene.parts
    # a settings change counts too, and changing it back clears it
    spin = w.print_settings.widgets["exposure"]
    old = spin.value()
    spin.setValue(old + 1)
    assert w.isWindowModified()
    spin.setValue(old)
    assert not w.isWindowModified()


def test_save_and_open_round_trip(app, make_window, tmp_path, monkeypatch):
    from gooslicer.supports import SupportParams, SupportPlan
    w = make_window()
    _import(app, w, _stl(tmp_path, "cube"))
    _import(app, w, _stl(tmp_path, "slab", (20, 8, 3)))
    a, b = w.scene.parts
    w.scene.rotate(a, [0, 0, 1], 30)
    a.supports = SupportPlan(SupportParams(tip_length=1.3), np.array([[0.0, 0, -5]]),
                             np.array([[0.0, 0, -1]]), ["manual"], "EVO_30-60mm")
    w.print_settings.widgets["exposure"].setValue(3.3)
    w.set_dismissed({"island": [np.array([1.0, 2.0, 3.0])]})
    w.viewport.camera.yaw = 42.0
    out = tmp_path / "proj"                                  # ".3mf" is added
    assert _save_as(app, w, monkeypatch, out) == str(tmp_path / "proj.3mf")
    assert w.windowTitle() == "proj.3mf[*] — GooSlicer" and not w.isWindowModified()
    assert w.project.recent()[0] == str(tmp_path / "proj.3mf")
    from gooslicer import project as projmod
    assert projmod.thumbnail(tmp_path / "proj.3mf")[:4] == b"\x89PNG"

    w2 = make_window("s2.ini")
    opened = []
    w2.project.opened.connect(opened.append)
    w2.project.open_project(str(tmp_path / "proj.3mf"))
    assert _wait(app, lambda: opened)
    assert [p.name for p in w2.scene.parts] == ["cube", "slab"]
    a2, b2 = w2.scene.parts
    assert np.allclose(a2.rotation, a.rotation) and np.allclose(a2.position, a.position)
    assert a2.supports is not None and a2.supports.kinds == ["manual"] and b2.supports is None
    assert w2.print_settings.settings().exposure == pytest.approx(3.3)
    assert np.allclose(w2.dismissed_points()["island"][0], [1, 2, 3])
    assert w2.viewport.camera.yaw == 42.0
    assert w2.windowTitle() == "proj.3mf[*] — GooSlicer" and not w2.isWindowModified()
    assert not w2.undo.canUndo()
    assert all(id(p.mesh) in w2.reports for p in w2.scene.parts)


def test_prompt_before_new_and_open(app, make_window, tmp_path, monkeypatch):
    from gooslicer_qt import projectio
    w = make_window()
    _import(app, w, _stl(tmp_path))
    asked = []

    def answer(a):
        def f(self):
            asked.append(a)
            return a
        monkeypatch.setattr(projectio.ProjectController, "ask_unsaved", f)
    answer(projectio.CANCEL)
    w.project.new_project()
    assert asked == ["cancel"] and len(w.scene.parts) == 1
    answer(projectio.DISCARD)
    w.project.new_project()
    assert not w.scene.parts and not w.isWindowModified() and w.windowTitle().startswith("Untitled")
    asked.clear()
    w.project.new_project()                                   # clean: no question
    assert asked == []
    # Save from the prompt: Save as…, then the action goes ahead
    _import(app, w, _stl(tmp_path))
    answer(projectio.SAVE)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "keep.3mf"), ""))
    w.project.new_project()
    assert _wait(app, lambda: not w.scene.parts)
    assert (tmp_path / "keep.3mf").exists() and w.project.path is None


def test_close_with_save(app, make_window, tmp_path, monkeypatch):
    from gooslicer_qt import projectio
    w = make_window()
    _import(app, w, _stl(tmp_path))
    monkeypatch.setattr(projectio.ProjectController, "ask_unsaved", lambda self: projectio.SAVE)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(tmp_path / "onclose.3mf"), ""))
    w.show()
    w.close()
    assert w.isVisible()                                      # waits for the save
    assert _wait(app, lambda: not w.isVisible())
    assert (tmp_path / "onclose.3mf").exists()


def test_recent_menu(app, make_window, tmp_path, monkeypatch):
    w = make_window()
    _import(app, w, _stl(tmp_path))
    _save_as(app, w, monkeypatch, tmp_path / "one.3mf")
    _save_as(app, w, monkeypatch, tmp_path / "two.3mf")
    gone = tmp_path / "gone.3mf"
    w.project.add_recent(str(gone))
    acts = [a for a in w.project.recent_menu.actions() if not a.isSeparator()]
    assert [a.text() for a in acts] == ["gone.3mf (missing)", "two.3mf", "one.3mf", "Clear list"]
    assert not acts[0].isEnabled() and acts[1].isEnabled()
    opened = []
    w.project.opened.connect(opened.append)
    acts[2].trigger()                                         # one.3mf
    assert _wait(app, lambda: opened) and w.project.path == tmp_path / "one.3mf"
    assert w.project.recent()[0] == str(tmp_path / "one.3mf")
    acts = [a for a in w.project.recent_menu.actions() if not a.isSeparator()]
    acts[-1].trigger()
    assert w.project.recent() == []
    assert [a.text() for a in w.project.recent_menu.actions()] == ["Clear list"]


def test_drop_opens_or_imports(app, make_window, tmp_path, monkeypatch):
    w = make_window()
    _import(app, w, _stl(tmp_path))
    _save_as(app, w, monkeypatch, tmp_path / "p.3mf")
    w2 = make_window("s2.ini")
    opened = []
    w2.project.opened.connect(opened.append)
    w2.open_files([str(tmp_path / "p.3mf")])                  # empty plate: opens it
    assert _wait(app, lambda: opened) and w2.project.path == tmp_path / "p.3mf"
    w2.open_files([str(tmp_path / "p.3mf")])                  # not empty: its parts are added
    assert _wait(app, lambda: len(w2.scene.parts) == 2)
    assert w2.project.path == tmp_path / "p.3mf" and w2.isWindowModified()


def test_plain_3mf_opens_as_new_project(app, make_window, tmp_path):
    import trimesh
    f = tmp_path / "plain.3mf"
    trimesh.creation.box((5, 5, 5)).export(f)
    w = make_window()
    w.project.open_project(str(f))
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    assert w.project.path is None and w.windowTitle().startswith("Untitled")


def test_open_damaged_project_leaves_scene(app, make_window, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    w = make_window()
    _import(app, w, _stl(tmp_path))
    _save_as(app, w, monkeypatch, tmp_path / "ok.3mf")
    bad = tmp_path / "bad.3mf"
    import zipfile
    from gooslicer import project as projmod
    with zipfile.ZipFile(tmp_path / "ok.3mf") as zi, zipfile.ZipFile(bad, "w") as zo:
        for info in zi.infolist():
            data = zi.read(info)
            if info.filename == projmod.MODEL_PATH:
                data = data.replace(b'v1="0"', b'v1="99999"', 1)
            zo.writestr(info, data)
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    w.project.open_project(str(bad))
    assert _wait(app, lambda: warned)
    assert "bad.3mf" in warned[0] and len(w.scene.parts) == 1 and w.project.path == tmp_path / "ok.3mf"
