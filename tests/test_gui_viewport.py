"""Interactive viewport tests (real GL). Run via scripts/test-gui.sh."""
import os
from pathlib import Path

import numpy as np
import pytest

if os.environ.get("GOOSLICER_GL_TESTS") != "1":
    pytest.skip("GL viewport tests need scripts/test-gui.sh (Xvfb)", allow_module_level=True)

import trimesh  # noqa: E402
from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QMouseEvent, QSurfaceFormat  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from gooslicer_qt.viewport import default_format  # noqa: E402

ARC = Path(__file__).resolve().parent.parent / "samples" / "arc-1a-attitude-4.stl"


@pytest.fixture(scope="module")
def app():
    QSurfaceFormat.setDefaultFormat(default_format())
    return QApplication.instance() or QApplication([])


@pytest.fixture
def win(app, tmp_path, monkeypatch):
    from PySide6.QtCore import QSettings
    from test_profiles import FIXTURE
    ini = tmp_path / "officialPlatform.ini"
    ini.write_bytes(FIXTURE.encode())
    monkeypatch.setenv("GOOSLICER_PROFILE_INI", str(ini))
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "d"))
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat))
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    yield w
    w.close()


def wait_import(app, w, files):
    loop = QEventLoop()
    got = []
    w.importFinished.connect(lambda n: (got.append(n), loop.quit()))
    QTimer.singleShot(60000, loop.quit)
    w.import_files([str(f) for f in files])
    loop.exec()
    w.importFinished.disconnect()
    return got[0] if got else None


def pump(app, n=5):
    for _ in range(n):
        app.processEvents()


def project(vp, pt):
    w, h = vp.width(), vp.height()
    c = vp.camera.projection(w / h) @ vp.camera.view() @ np.append(pt, 1.0)
    n = c[:3] / c[3]
    return QPointF((n[0] + 1) / 2 * w, (1 - n[1]) / 2 * h)


def mouse(vp, kind, pos, button=Qt.LeftButton, buttons=None, mods=Qt.NoModifier):
    buttons = button if buttons is None else buttons
    ev = QMouseEvent(kind, pos, vp.mapToGlobal(pos), button, buttons, mods)
    QApplication.sendEvent(vp, ev)


def drag(vp, a, b, steps=8):
    mouse(vp, QEvent.MouseButtonPress, a)
    for i in range(1, steps + 1):
        p = a + (b - a) * (i / steps)
        mouse(vp, QEvent.MouseMove, p, Qt.NoButton, Qt.LeftButton)
    mouse(vp, QEvent.MouseButtonRelease, b, Qt.LeftButton, Qt.NoButton)


def box_file(tmp_path, size=(20, 20, 10), name="box.stl", broken=False):
    m = trimesh.creation.box(size)
    if broken:
        m = trimesh.Trimesh(m.vertices, m.faces[:-1], process=False)
    f = tmp_path / name
    m.export(f)
    return f


def test_import_renders_and_selects(app, win, tmp_path):
    assert wait_import(app, win, [box_file(tmp_path)]) == 1
    vp = win.viewport
    p = win.scene.parts[0]
    assert np.allclose(p.bounds()[0], [-10, -10, 0])
    win.select([])
    vp.frame_selection()
    pump(app)
    img = vp.grabFramebuffer()
    c = img.pixelColor(int(project(vp, [0, 0, 10]).x()), int(project(vp, [0, 0, 10]).y()))
    assert c.blue() > c.red() and c.green() > 100  # part colour, not the dark background
    mouse(vp, QEvent.MouseButtonPress, project(vp, [0, 0, 10]))
    mouse(vp, QEvent.MouseButtonRelease, project(vp, [0, 0, 10]), Qt.LeftButton, Qt.NoButton)
    assert vp.selected == [p.id] and win.props.part is p
    pump(app)
    sel = vp.grabFramebuffer().pixelColor(int(project(vp, [0, 0, 10]).x()), int(project(vp, [0, 0, 10]).y()))
    assert sel.red() > sel.blue()  # orange highlight
    corner = project(vp, [80, 50, 0])
    mouse(vp, QEvent.MouseButtonPress, corner)
    mouse(vp, QEvent.MouseButtonRelease, corner, Qt.LeftButton, Qt.NoButton)
    assert vp.selected == []


def test_drag_move_and_undo(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path)])
    vp, p = win.viewport, win.scene.parts[0]
    vp.set_view(-90, 89)  # top view
    vp.frame_selection()
    vp.camera.distance *= 3
    pump(app)
    a, b = project(vp, [0, 0, 10]), project(vp, [25, 15, 10])
    drag(vp, a, b)
    assert np.allclose(p.position[:2], [25, 15], atol=0.6)
    assert win.undo.undoText() == "Move"
    win.undo.undo()
    assert np.allclose(p.position[:2], [0, 0], atol=1e-9)
    win.undo.redo()
    assert np.allclose(p.position[:2], [25, 15], atol=0.6)


def test_lay_flat_and_rotate_undo(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path, (10, 20, 30))])
    p = win.scene.parts[0]
    win.select([p.id])
    side = int(np.argmax(p.mesh.face_normals @ [1, 0, 0]))
    win.viewport.layFlatPicked.emit(p.id, side)
    assert np.allclose(p.size(), [30, 20, 10])
    win.props._rotate(2, 90)
    assert np.allclose(p.size(), [20, 30, 10])
    win.undo.undo()
    win.undo.undo()
    assert np.allclose(p.size(), [10, 20, 30]) and np.allclose(p.rotation, np.eye(3))


def test_properties_size_uniform(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path)])
    p = win.scene.parts[0]
    win.select([p.id])
    win.props.size[0].setValue(40.0)
    assert np.allclose(p.scale, 2.0) and np.allclose(p.size(), [40, 40, 20])
    assert p.bounds()[0][2] == pytest.approx(0)
    win.undo.undo()
    assert np.allclose(p.scale, 1.0)


def test_duplicate_delete_arrange_undo(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path)])
    win.select([win.scene.parts[0].id])
    win.duplicate_selected()
    win.duplicate_selected()
    assert len(win.scene.parts) == 3
    win.arrange_all()
    assert all(win.scene.in_volume(p) for p in win.scene.parts)
    win.select([win.scene.parts[1].id])
    win.delete_selected()
    assert len(win.scene.parts) == 2
    for _ in range(4):
        win.undo.undo()
    assert len(win.scene.parts) == 1


def test_repair_broken_mesh(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path, broken=True, name="holey.stl")])
    p = win.scene.parts[0]
    win.select([p.id])
    assert "open (hole) edges" in win.props.mesh_info.text() and win.props.btn_repair.isEnabled()
    old = p.mesh
    loop = QEventLoop()
    quit_ = lambda *_: loop.quit()  # noqa: E731
    win.undo.indexChanged.connect(quit_)
    QTimer.singleShot(30000, loop.quit)
    win.repair_selected()
    loop.exec()
    win.undo.indexChanged.disconnect(quit_)
    assert p.mesh is not old and "printable mesh" in win.props.mesh_info.text()
    win.undo.undo()
    assert p.mesh is old and "open (hole)" in win.props.mesh_info.text()


def test_arc_sample_and_printer_switch(app, win):
    if not ARC.exists():
        pytest.skip("sample missing")
    assert wait_import(app, win, [ARC]) == 1
    p = win.scene.parts[0]
    assert np.allclose(p.size(), [32.548, 23.437, 52.88], atol=1e-3) and win.scene.in_volume(p)
    assert np.allclose(win.scene.size, [211.68, 118.37, 220.0])


def test_import_error_is_reported(app, win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: None)
    bad = tmp_path / "bad.stl"
    bad.write_bytes(b"solid x\nendsolid x\n")
    assert wait_import(app, win, [bad]) == 0
    assert "Import failed" in win.log.toPlainText() and not win.scene.parts
