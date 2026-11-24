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


def test_axis_compass_top_right(app, win, tmp_path):
    from gooslicer_qt.viewport import COMPASS_COLORS
    from PySide6.QtGui import QColor
    assert wait_import(app, win, [box_file(tmp_path)]) == 1
    vp = win.viewport
    vp.set_view(-90, 89)                       # top view: X to the right, Y up
    pump(app)
    r = vp.compass_rect()
    assert r.right() <= vp.width() and r.top() >= 0 and r.left() > vp.width() / 2
    img = vp.grabFramebuffer()
    dpr = img.devicePixelRatio()
    arm = r.width() / 2 - 11

    def near(pt, name):
        """The axis's hue (red X, green Y, blue Z); far-pointing axes are drawn dimmer."""
        c = img.pixelColor(int(pt.x() * dpr), int(pt.y() * dpr))
        ch = {"X": c.red(), "Y": c.green(), "Z": c.blue()}
        top = ch.pop(name)
        assert QColor(COMPASS_COLORS[name]).isValid()
        return top > 90 and all(top > v + 40 for v in ch.values())

    cx, cy = r.center().x(), r.center().y()
    # sample along each arm (the disc centre holds the dark letter)
    assert near(QPointF(cx + arm * 0.55, cy), "X") and near(QPointF(cx, cy - arm * 0.55), "Y")
    # the 3D scene still renders after the painter overlay (GL state restored each frame)
    win.select([])
    vp.frame_selection()
    pump(app)
    img = vp.grabFramebuffer()
    c = img.pixelColor(int(project(vp, [0, 0, 10]).x() * dpr), int(project(vp, [0, 0, 10]).y() * dpr))
    assert c.blue() > c.red() and c.green() > 100
    vp.show_compass = False
    vp.update()
    pump(app)
    img = vp.grabFramebuffer()
    assert not near(QPointF(cx + arm * 0.55, cy), "X")


def test_front_label(app, win):
    vp = win.viewport
    sy = float(win.scene.size[1])
    vp.set_view(-90, 89)                       # top view: X right, Y up, front (−Y) at the bottom
    pump(app)
    quad = vp.front_label_quad()
    edge = project(vp, [0, -sy / 2, 0])
    assert quad is not None and min(q.y() for q in quad) > edge.y()          # in front of the plate
    assert quad[0].x() < quad[1].x() and quad[0].y() < quad[3].y()          # reads left to right, upright
    assert abs((quad[0].x() + quad[1].x()) / 2 - edge.x()) < 2               # centred on the front edge
    img = vp.grabFramebuffer()
    dpr = img.devicePixelRatio()
    x0, x1 = int(min(q.x() for q in quad) * dpr), int(max(q.x() for q in quad) * dpr)
    y0, y1 = int(min(q.y() for q in quad) * dpr), int(max(q.y() for q in quad) * dpr)

    def white():
        im = vp.grabFramebuffer()
        return sum(1 for x in range(x0, x1) for y in range(y0, y1)
                   if min(im.pixelColor(x, y).red(), im.pixelColor(x, y).green(), im.pixelColor(x, y).blue()) > 225)
    assert white() > 40                                                      # white text is drawn
    vp.show_front = False
    vp.update()
    pump(app)
    assert white() == 0
    vp.show_front = True
    vp.set_view(90, 30)                        # from behind: beyond the far edge, mirrored
    pump(app)
    q = vp.front_label_quad()
    assert q[0].x() > q[1].x() and max(p.y() for p in q) < project(vp, [0, -sy / 2, 0]).y()


def test_islands_drawn_red(app, win, tmp_path):
    import trimesh
    m = trimesh.creation.box((20, 20, 4))
    m.apply_translation((0, 0, 12))
    vp = win.viewport
    vp.set_view(-90, 89)                       # from above, onto a red surface (no part in the scene)
    pump(app)
    pt = project(vp, [0, 0, 10])

    def px():
        img = vp.grabFramebuffer()
        dpr = img.devicePixelRatio()
        return img.pixelColor(int(pt.x() * dpr), int(pt.y() * dpr))
    vp.island_meshes = [m.submesh([np.flatnonzero(m.face_normals[:, 2] > 0.5)], append=True)]
    vp.update()
    pump(app)
    c = px()
    assert c.red() > 120 and c.red() > 2 * c.green() and c.red() > 2 * c.blue()
    vp.island_meshes = []
    vp.update()
    pump(app)
    assert px().red() < 120


def test_island_guides_reach_the_edge():
    from gooslicer_qt.viewport import island_guides
    seg = island_guides([[10, 0, 5], [0, 0, 2], [-30, 20, 7], [80, 0, 1]], (100, 60, 200))
    assert seg.shape == (6, 3)                      # the point outside the build area gets none
    assert np.allclose(seg[0:2], [[10, 0, 5], [50, 0, 5]])
    assert np.allclose(seg[2:4], [[0, 0, 2], [0, -30, 2]])   # at the centre: toward the front
    a, b = seg[4:6]
    assert np.isclose(b[2], 7) and (np.isclose(abs(b[0]), 50) or np.isclose(abs(b[1]), 30))
    assert np.isclose(a[0] * b[1] - a[1] * b[0], 0) and b[0] < a[0] < 0   # straight out from the centre


def test_plate_see_through_and_supports_half_transparent(app, win):
    import trimesh
    from gooslicer_qt.viewport import island_guides
    vp = win.viewport
    vp.set_view(-90, 89)
    pump(app)
    sy = float(win.scene.size[1])
    # the plate between grid lines shows the background: no solid floor
    img = vp.grabFramebuffer()
    dpr = img.devicePixelRatio()
    pt = project(vp, [2.5, 2.5, 0])
    c = img.pixelColor(int(pt.x() * dpr), int(pt.y() * dpr))
    assert abs(c.redF() - 0.16) < 0.03 and abs(c.greenF() - 0.17) < 0.03 and abs(c.blueF() - 0.19) < 0.03

    # a support over the grid: blended with what is behind it
    sup = trimesh.creation.box((30, 30, 2))
    sup.apply_translation((0, 0, 1))
    vp.extra_meshes = lambda: [(sup, np.eye(4))]
    vp.update()
    pump(app)
    pt = project(vp, [2.5, 2.5, 2])

    def at(p):
        im = vp.grabFramebuffer()
        return im.pixelColor(int(p.x() * dpr), int(p.y() * dpr))
    half = at(pt)
    assert half.alpha() == 255          # blending keeps the view opaque (the window must not show through)
    import gooslicer_qt.viewport as V
    old = V.SUPPORT_ALPHA
    V.SUPPORT_ALPHA = 1.0
    vp.update()
    pump(app)
    solid = at(pt)
    V.SUPPORT_ALPHA = old
    # half of the support's colour plus half of the dark background behind it
    assert abs(half.blueF() - (solid.blueF() + 0.19) / 2) < 0.04 and half.blueF() < solid.blueF() - 0.15
    assert solid.blueF() > solid.redF() + 0.3 and solid.blueF() > solid.greenF() + 0.2      # blue supports
    vp.extra_meshes = lambda: []

    # guide lines: white where nothing hides them
    vp.island_lines = island_guides([[0, 0, 5]], win.scene.size)
    vp.update()
    pump(app)
    mid = project(vp, [0, -sy / 4, 5])
    img = vp.grabFramebuffer()
    best = max(min(img.pixelColor(int(mid.x() * dpr) + dx, int(mid.y() * dpr) + dy).getRgb()[:3])
               for dx in range(-2, 3) for dy in range(-2, 3))
    assert best > 220
    vp.island_lines = np.zeros((0, 3))


def _ring_point(vp, axis, deg):
    from gooslicer_qt.viewport import RING_BASIS
    c, r = vp.rings()
    u, w = RING_BASIS[axis]
    t = np.radians(deg)
    return project(vp, c + r * (np.cos(t) * u + np.sin(t) * w))


def test_rotation_rings(app, win, tmp_path):
    from gooslicer.scene import rotation_matrix
    assert wait_import(app, win, [box_file(tmp_path, (30, 10, 6))]) == 1
    vp = win.viewport
    p = win.scene.parts[0]
    win.select([])
    pump(app)
    assert vp.rings() is None                                   # only around a selection
    win.select([p.id])
    vp.set_view(-90, 89)                                        # top view: the Z ring is a circle
    pump(app)
    c, r = vp.rings()
    assert r >= 0.5 * np.linalg.norm(p.size())
    assert vp.ring_at(_ring_point(vp, 2, 45)) == 2
    assert vp.ring_at(QPointF(4, vp.height() - 4)) is None      # away from every ring
    # hovering shows it can be grabbed
    mouse(vp, QEvent.MouseMove, _ring_point(vp, 2, 45), Qt.NoButton, Qt.NoButton)
    assert vp._ring_hover == 2
    # drag 47° round the Z ring: turns 45° (5° steps) about Z, one undo step
    before = p.state()
    n0 = win.undo.index()
    mouse(vp, QEvent.MouseButtonPress, _ring_point(vp, 2, 45))
    pts = [_ring_point(vp, 2, 45 + k) for k in range(0, 48, 4)] + [_ring_point(vp, 2, 92)]
    for q in pts:
        mouse(vp, QEvent.MouseMove, q)
    assert vp._rot["deg"] == 45.0
    mouse(vp, QEvent.MouseButtonRelease, pts[-1])
    assert vp._rot is None and win.undo.index() == n0 + 1 and win.undo.text(n0) == "Rotate Z +45°"
    assert np.allclose(p.rotation, rotation_matrix([0, 0, 1], 45) @ before.rotation)
    assert np.allclose(p.position[:2], before.position[:2])     # turned about its own centre
    assert abs(p.bounds()[0][2]) < 1e-6                         # still on the plate
    win.undo.undo()
    assert p.state() == before
    # backwards past zero
    mouse(vp, QEvent.MouseButtonPress, _ring_point(vp, 2, 45))
    for k in range(0, 31, 3):
        mouse(vp, QEvent.MouseMove, _ring_point(vp, 2, 45 - k))
    mouse(vp, QEvent.MouseButtonRelease, _ring_point(vp, 2, 15))
    assert win.undo.text(win.undo.index() - 1) == "Rotate Z -30°"
    win.undo.undo()
    # from above, the X and Y rings are edge-on (lines across the part): hidden, and the middle
    # of the part moves it as before
    assert not vp.ring_visible(0, c) and not vp.ring_visible(1, c) and vp.ring_visible(2, c)
    assert vp.ring_at(project(vp, c)) is None
    # from an angle the X ring can be grabbed; it turns in 5° steps about X
    vp.set_view(-60, 25)
    pump(app)
    a = _ring_point(vp, 0, 60)
    assert vp.ring_visible(0, c) and vp.ring_at(a) == 0
    mouse(vp, QEvent.MouseButtonPress, a)
    for k in range(0, 33, 4):
        mouse(vp, QEvent.MouseMove, _ring_point(vp, 0, 60 + k))
    deg = vp._rot["deg"]
    mouse(vp, QEvent.MouseButtonRelease, _ring_point(vp, 0, 92))
    assert 25 <= deg <= 35 and deg % 5 == 0
    assert np.allclose(p.rotation, rotation_matrix([1, 0, 0], deg) @ before.rotation)
    win.undo.undo()
    # a part's support mode hides the rings
    vp.start_support_mode()
    assert vp.rings() is None
    vp.stop_support_mode()


def test_right_click_menu_on_a_part(app, win, tmp_path, monkeypatch):
    assert wait_import(app, win, [box_file(tmp_path, (16, 8, 6))]) == 1
    vp = win.viewport
    p = win.scene.parts[0]
    win.select([])
    vp.set_view(-90, 89)
    pump(app)
    asked = []
    vp.partContextMenu.connect(lambda pid, gpos: asked.append(pid))
    shown = []
    monkeypatch.setattr(win, "_popup_menu", lambda menu, pos: shown.append([x.text() for x in menu.actions()]))
    at = project(vp, p.bounds().mean(axis=0))
    mouse(vp, QEvent.MouseButtonPress, at, Qt.RightButton)
    mouse(vp, QEvent.MouseButtonRelease, at, Qt.RightButton, Qt.NoButton)
    assert asked == [p.id] and win.selected_ids() == [p.id]
    assert shown == [["Add instance", "Mirror"]]
    # a right drag orbits and opens nothing
    yaw = vp.camera.yaw
    mouse(vp, QEvent.MouseButtonPress, at, Qt.RightButton)
    mouse(vp, QEvent.MouseMove, QPointF(at.x() + 30, at.y()), Qt.RightButton)
    mouse(vp, QEvent.MouseButtonRelease, QPointF(at.x() + 30, at.y()), Qt.RightButton, Qt.NoButton)
    assert len(asked) == 1 and vp.camera.yaw != yaw
    # off the parts: nothing
    mouse(vp, QEvent.MouseButtonPress, QPointF(5, 5), Qt.RightButton)
    mouse(vp, QEvent.MouseButtonRelease, QPointF(5, 5), Qt.RightButton, Qt.NoButton)
    assert len(asked) == 1
