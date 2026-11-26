"""Interactive viewport tests (real GL). Run via scripts/test-gui.sh."""
import os
import time
from pathlib import Path

import numpy as np
import pytest

if os.environ.get("GOOSLICER_GL_TESTS") != "1":
    pytest.skip("GL viewport tests need scripts/test-gui.sh (Xvfb)", allow_module_level=True)

import trimesh  # noqa: E402
from PySide6.QtCore import QEvent, QEventLoop, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QMouseEvent, QSurfaceFormat  # noqa: E402
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


def test_repair_on_import(app, win, tmp_path):
    """A model failing its check is repaired as it's imported and listed "(repaired)"; a clean
    one is left alone; the preference turns it off."""
    from gooslicer_qt.preferences import PreferencesDialog
    assert win.auto_repair()
    wait_import(app, win, [box_file(tmp_path, broken=True, name="holey.stl")])   # one file per wait
    wait_import(app, win, [box_file(tmp_path)])
    by = {p.name: p for p in win.scene.parts}
    assert by["holey"].repaired and win.reports[id(by["holey"].mesh)].ok
    assert not by["box"].repaired
    texts = [win.parts.item(i).text() for i in range(win.parts.count())]
    assert "holey (repaired)" in texts and "box" in texts
    d = PreferencesDialog(win)
    assert d.auto_repair.isChecked()
    d.auto_repair.setChecked(False)
    assert not win.auto_repair()
    wait_import(app, win, [box_file(tmp_path, broken=True, name="holey2.stl")])
    p2 = next(p for p in win.scene.parts if p.name == "holey2")
    assert not p2.repaired and not win.reports[id(p2.mesh)].ok
    d.pages[[pg.name for pg in d.pages].index("Files and folders")].reset()
    assert win.auto_repair()
    d.close()


def test_repair_broken_mesh(app, win, tmp_path):
    win.settings.setValue("import/autoRepair", False)      # import it broken, repair by hand
    wait_import(app, win, [box_file(tmp_path, broken=True, name="holey.stl")])
    p = win.scene.parts[0]
    win.select([p.id])
    assert "open (hole) edges" in win.props.mesh_info.text() and win.props.btn_repair.isEnabled()
    old = p.mesh
    loop = QEventLoop()
    quit_ = lambda *_: loop.quit()  # noqa: E731
    win.undo.indexChanged.connect(quit_)
    QTimer.singleShot(30000, loop.quit)
    seen = []
    win.statusBar().messageChanged.connect(seen.append)
    win.repair_selected()
    loop.exec()
    win.undo.indexChanged.disconnect(quit_)
    win.statusBar().messageChanged.disconnect(seen.append)
    assert p.mesh is not old and "printable mesh" in win.props.mesh_info.text()
    # each step showed in the status bar, then the result
    assert "Repairing holey…" in seen and any(m.startswith("Repairing holey: closing holes") for m in seen)
    assert win.statusBar().currentMessage() == "Repaired holey"
    assert p.repaired and win.parts.item(0).text() == "holey (repaired)"
    win.undo.undo()
    assert p.mesh is old and "open (hole)" in win.props.mesh_info.text()
    assert not p.repaired and win.parts.item(0).text() == "holey"


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
    # a model standing on the label hides it (the label was painted over everything)
    import trimesh
    from gooslicer.scene import Part
    part = Part.from_mesh("block", trimesh.creation.box((200, 60, 5)))
    part.position[:] = [0, -sy / 2 - 15, 2.5]
    win.scene.add(part, place=False)
    vp.update()
    pump(app)
    assert white() == 0
    win.scene.remove(part.id)
    vp.update()
    pump(app)
    assert white() > 40
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
    old = QColor(vp.colors["support"])
    vp.colors["support"].setAlphaF(1.0)
    vp.update()
    pump(app)
    solid = at(pt)
    vp.colors["support"] = old
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

    # a label at the outer end of each line, just outside the build plate, naming the problem
    from gooslicer_qt.viewport import LABEL_CAVITY, LABEL_ISLAND
    vp.cavity_lines = island_guides([[10, 5, 3]], win.scene.size)
    labels = vp.guide_labels()
    assert sorted(lab.text for lab in labels) == sorted([LABEL_ISLAND + " 1", LABEL_CAVITY + " 1"])
    for r, t, *_ in labels:
        lines = vp.island_lines if t.startswith(LABEL_ISLAND) else vp.cavity_lines
        end, start = project(vp, lines[1]), project(vp, lines[0])
        # beyond the end, continuing the line
        assert (r.center().x() - end.x()) * (end.x() - start.x()) + (r.center().y() - end.y()) * (end.y() - start.y()) > 0
        assert not r.contains(end)
    # the island label (front edge) is drawn: light text on a dark box
    r = next(lab.rect for lab in labels if lab.text.startswith(LABEL_ISLAND))
    img = vp.grabFramebuffer()
    px = [img.pixelColor(int(x * dpr), int(r.center().y() * dpr)) for x in np.linspace(r.left() + 4, r.right() - 4, 60)]
    assert max(c.lightness() for c in px) > 200 and min(c.lightness() for c in px) < 60
    # overlapping labels of the same kind collapse into one
    vp.island_lines = island_guides([[5, 0, 5], [5, 0.3, 5]], win.scene.size)
    vp.cavity_lines = np.zeros((0, 3))
    labels = vp.guide_labels()
    assert [lab.text for lab in labels] == [LABEL_ISLAND + "s 1, 2"] and len(labels[0].keys) == 2
    vp.island_numbers = [14, 3]                          # numbers come from the flaw list
    assert [lab.text for lab in vp.guide_labels()] == [LABEL_ISLAND + "s 3, 14"]
    vp.island_numbers = None
    vp.island_lines = np.zeros((0, 3))
    assert vp.guide_labels() == []


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
    assert shown == [["Add instance", "Mirror", "See-through"]]
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


def test_click_guide_label_selects_its_line(app, win, tmp_path):
    from gooslicer_qt.viewport import LABEL_CAVITY, LABEL_ISLAND, island_guides
    vp = win.viewport
    vp.set_view(-90, 89)                                 # from above
    # a tall box hides the middle of the island's guide line
    wait_import(app, win, [box_file(tmp_path, (20, 20, 60))])
    win.scene.parts[0].position[:2] = (0, -20)
    win.select([])
    vp.island_lines = island_guides([[0, 0, 5]], win.scene.size)        # runs to the front (−Y)
    vp.update()
    pump(app)
    dpr = vp.grabFramebuffer().devicePixelRatio()
    behind = project(vp, [0, -20, 5])                    # under the box: hidden from above

    def colour(pt):
        im = vp.grabFramebuffer()
        return im.pixelColor(int(pt.x() * dpr), int(pt.y() * dpr))
    c = colour(behind)
    assert not (c.red() > 200 and c.green() < 90)        # white guide, dimmed under the box
    lab = vp.guide_labels()[0]
    assert lab.text == LABEL_ISLAND + " 1" and not lab.selected

    mouse(vp, QEvent.MouseButtonPress, lab.rect.center())
    mouse(vp, QEvent.MouseButtonRelease, lab.rect.center(), Qt.LeftButton, Qt.NoButton)
    pump(app)
    assert vp.selected_guides == {("island", 1)} and vp.guide_labels()[0].selected
    assert win.selected_ids() == []                      # the click didn't pick or move a part
    c = colour(behind)                                   # red, and drawn over the box
    assert c.red() > 200 and c.green() < 90 and c.blue() < 90
    # a selected label is never left out, even where another label would cover it
    vp.cavity_lines = vp.island_lines.copy()
    vp.cavity_lines[0, 2] = vp.cavity_lines[1, 2] = 5.01
    texts = [x.text for x in vp.guide_labels()]
    assert texts[0] == LABEL_ISLAND + " 1" and not any(t.startswith(LABEL_CAVITY) for t in texts)
    vp.cavity_lines = np.zeros((0, 3))
    assert vp.selected_guides == {("island", 1)}         # a new cavity check keeps the island's

    lab = vp.guide_labels()[0]
    mouse(vp, QEvent.MouseButtonPress, lab.rect.center())    # clicking again deselects
    mouse(vp, QEvent.MouseButtonRelease, lab.rect.center(), Qt.LeftButton, Qt.NoButton)
    pump(app)
    assert vp.selected_guides == set() and not vp.guide_labels()[0].selected
    c = colour(behind)
    assert not (c.red() > 200 and c.green() < 90)
    vp.toggle_guide(vp.guide_labels()[0])
    assert vp.guide_selection()
    vp.clear_guide_selection("cavity")                   # another kind's check: kept
    assert vp.guide_selection() == {("island", 1)}
    vp.clear_guide_selection("island")                   # a new island check renumbers: cleared
    assert vp.guide_selection() == set()
    vp.toggle_guide(vp.guide_labels()[0])
    vp.island_lines = np.zeros((0, 3))                   # its line is gone: so is the selection
    assert vp.guide_selection() == set()
    vp.island_lines = np.zeros((0, 3))



def test_auto_position(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path, size=(40, 40, 2), name="plate.stl")])
    p = win.scene.parts[0]
    pp = win.props
    # under the Lay flat / Drop to plate row, enabled with a part selected
    lay = pp.layout()
    row = next(i for i in range(lay.count())
               if lay.itemAt(i).layout() is not None and lay.itemAt(i).layout().indexOf(pp.btn_flat) >= 0)
    nxt = lay.itemAt(row + 1).layout()                 # the row after holds Auto Position
    assert nxt is not None and nxt.indexOf(pp.btn_auto) == 0
    win.select([])
    assert not pp.btn_auto.isEnabled()
    win.select([p.id])
    assert pp.btn_auto.isEnabled() and pp.btn_auto.text() == "Auto Position"
    old = p.state()
    loop = QEventLoop()
    quit_ = lambda *_: loop.quit()  # noqa: E731
    win.undo.indexChanged.connect(quit_)
    QTimer.singleShot(30000, loop.quit)
    seen = []
    win.statusBar().messageChanged.connect(seen.append)
    pp.btn_auto.click()
    assert not pp.btn_auto.isEnabled()                    # busy
    loop.exec()
    win.undo.indexChanged.disconnect(quit_)
    win.statusBar().messageChanged.disconnect(seen.append)
    assert not np.allclose(p.rotation, old.rotation)
    assert abs(p.bounds()[0][2] - p.elevation) < 1e-6     # still on the plate
    n = trimesh.Trimesh(p.world_vertices(), p.mesh.faces).face_normals
    assert (n[:, 2] > -0.95).all()                         # nothing flat against the plate
    assert any("angles tried" in m for m in seen) and "Auto Position: tilted" in win.statusBar().currentMessage()
    assert "Auto Position plate: tilted" in win.log.toPlainText()
    assert pp.btn_auto.isEnabled()
    win.undo.undo()
    assert p.state() == old


def test_drain_hole_panel_and_preview(app, win, tmp_path):
    from gooslicer_qt.hollowing import DrainHolePanel
    wait_import(app, win, [box_file(tmp_path, size=(30, 30, 20), name="block.stl")])
    p = win.scene.parts[0]
    vp = win.viewport
    vp.reset_view()
    win.drain_hole_mode()
    app.processEvents()
    panel = win.hole_panel
    # floats over the top-left of the 3D view, no dialog: the view stays usable
    assert isinstance(panel, DrainHolePanel) and panel.isVisible() and panel.parentWidget() is vp
    assert (panel.x(), panel.y()) == (12, 12) and QApplication.activeModalWidget() is None
    assert vp.hole_mode and vp.hole_preview is None
    f = panel.fields
    # linked by default: one value sets both
    assert f.radii_linked and f.depths_linked
    f.r1.setValue(2.5)
    assert f.r2.value() == 2.5
    f.e2.setValue(4.0)
    assert f.e1.value() == 4.0
    # each padlock sits right of its pair, centred between the two rows
    for lock, a, b in ((f.radius_lock, f.r1, f.r2), (f.depth_lock, f.e1, f.e2)):
        assert lock.geometry().left() >= max(a.geometry().right(), b.geometry().right())
        mid = (a.geometry().center().y() + b.geometry().center().y()) / 2
        assert abs(lock.geometry().center().y() - mid) <= 2
    # unlocked: separately
    f.radius_lock.click()
    f.depth_lock.click()
    assert not f.radii_linked and win.settings.value("drain/linkRadius") in (False, "false")
    f.r1.setValue(2.0)
    f.r2.setValue(1.5)
    f.e1.setValue(1.0)
    f.e2.setValue(3.0)
    assert (f.r1.value(), f.r2.value(), f.e1.value(), f.e2.value()) == (2.0, 1.5, 1.0, 3.0)
    f.direction.setCurrentIndex(f.direction.findData("vertical"))      # straight up through the top
    top = np.array([p.position[0], p.position[1], p.bounds()[1][2]])
    pos = project(vp, top)
    mouse(vp, QEvent.MouseMove, pos, Qt.NoButton, Qt.NoButton)
    app.processEvents()                                                  # moves are picked once in
    prev = vp.hole_preview
    assert prev is not None
    lo, hi = prev.bounds
    assert np.allclose(hi[:2] - lo[:2], 4.0, atol=0.05)                 # outer radius 2
    assert np.isclose(hi[2] - lo[2], 4.0, atol=1e-6)                    # 1 outside + 3 inside
    # it follows the settings: wider, deeper
    f.r1.setValue(3.0)
    assert vp.hole_preview is not prev and np.allclose(np.ptp(vp.hole_preview.bounds[:, :2], axis=0), 6.0, atol=0.05)
    f.e2.setValue(5.0)
    assert np.isclose(np.ptp(vp.hole_preview.bounds[:, 2]), 6.0)
    assert float(win.settings.value("drain/radius1")) == 3.0          # remembered
    vp.update()
    app.processEvents()                                                 # draws (no GL errors)
    # a click drills there; the panel stays for the next hole
    old = p.mesh
    mouse(vp, QEvent.MouseButtonPress, pos)
    mouse(vp, QEvent.MouseButtonRelease, pos, Qt.LeftButton, Qt.NoButton)
    assert win.drilling() and p.mesh is old                              # drilled in the background
    loop = QEventLoop()
    quit_loop = lambda *_: loop.quit()
    win.undo.indexChanged.connect(quit_loop)
    QTimer.singleShot(30000, loop.quit)
    loop.exec()
    win.undo.indexChanged.disconnect(quit_loop)                         # later undo steps outlive the loop
    assert not win.drilling() and p.mesh is not old and len(p.mesh.faces) > len(old.faces)
    assert win.undo.undoText() == "Drain hole"
    assert panel.isVisible() and vp.hole_mode and vp.hole_preview is not None
    # over the panel: no preview; Done ends it
    QApplication.sendEvent(panel, QEvent(QEvent.Enter))
    assert vp.hole_preview is None
    panel.done_btn.click()
    assert not vp.hole_mode and not panel.isVisible() and vp.hole_preview is None
    # Esc ends it too
    win.drain_hole_mode()
    assert panel.isVisible() and vp.hole_mode
    from PySide6.QtGui import QKeyEvent
    QApplication.sendEvent(vp, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert not vp.hole_mode and not panel.isVisible()
    win.undo.undo()
    assert p.mesh is old
    # re-locking makes the pair equal again (the second takes the first's value)
    win.drain_hole_mode()
    f.radius_lock.click()
    assert f.radii_linked and f.r2.value() == f.r1.value() == 3.0
    win.viewport.stop_hole_mode()


def test_drain_hole_keeps_its_pin(app, win, tmp_path):
    """07 §3: "Keep hole pin" adds the plug as a new part, in the same undo step as the hole;
    the hole goes through the wall to the next surface."""
    wait_import(app, win, [box_file(tmp_path, size=(30, 30, 20), name="block.stl")])
    p = win.scene.parts[0]
    vp = win.viewport
    vp.reset_view()
    win.drain_hole_mode()
    f = win.hole_panel.fields
    f.more_btn.setChecked(True)
    assert f.more.isVisible() and not f.clearance.isEnabled()
    f.keep.setChecked(True)
    assert f.clearance.isEnabled()
    f.clearance.setValue(0.2)
    f.direction.setCurrentIndex(f.direction.findData("normal"))
    top = np.array([p.position[0], p.position[1], p.bounds()[1][2]])
    pos = project(vp, top)
    mouse(vp, QEvent.MouseMove, pos, Qt.NoButton, Qt.NoButton)
    app.processEvents()
    assert np.isclose(np.ptp(vp.hole_preview.bounds[:, 2]), 20 + 2 + 2, atol=0.01)   # through the block
    n0, count = win.undo.index(), len(win.scene.parts)
    mouse(vp, QEvent.MouseButtonPress, pos)
    mouse(vp, QEvent.MouseButtonRelease, pos, Qt.LeftButton, Qt.NoButton)
    loop = QEventLoop()
    quit_loop = lambda *_: loop.quit()
    win.undo.indexChanged.connect(quit_loop)
    QTimer.singleShot(30000, loop.quit)
    loop.exec()
    win.undo.indexChanged.disconnect(quit_loop)
    assert win.undo.index() == n0 + 1 and win.undo.undoText() == "Drain hole"
    assert len(win.scene.parts) == count + 1
    pin = win.scene.parts[-1]
    assert pin.name == "block pin" and abs(pin.bounds()[0][2]) < 1e-6           # on the plate
    assert np.isclose(pin.mesh.extents[2], 20.0) and np.isclose(pin.mesh.extents[0], 3.6, atol=0.01)
    win.undo.undo()
    assert len(win.scene.parts) == count
    win.viewport.stop_hole_mode()


def test_drain_hole_fields_round_trip(app, tmp_path):
    from PySide6.QtCore import QSettings
    from gooslicer.support_defaults import DrainHoleSettings
    from gooslicer_qt.hollowing import DrainHoleFields
    qs = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    f = DrainHoleFields(qs)
    assert f.value() == (DrainHoleSettings(), "normal")                       # SatelLite's defaults
    s = DrainHoleSettings(hole_form="slot", depth=3.5, edges=12, use_angle_slot=True, angle_slot=8,
                          use_notch=True, notch_width=0.8, notch_height=0.6, notch_angle=45,
                          keep_subtracted=True, use_clearance=True, clearance=0.15, clearance_type=3,
                          direction_id=1, direction=(0.0, 1.0, 0.0))
    f.set_value(s, "custom")
    assert all(w.isEnabled() for w in f.dir_xyz) and f.depth.isEnabled() and not f.e2.isEnabled()
    assert not f.use_angle.isEnabled()                                         # slots don't taper by angle
    f.save()
    g = DrainHoleFields(qs)
    assert g.value() == (s, "custom")


def test_support_mode_click_and_drag(app, win, tmp_path):
    """Support mode: a press becomes a click on release, or a drag that shows where the
    contact will go; Ctrl+press removes at once (08 §6)."""
    assert wait_import(app, win, [box_file(tmp_path, (16, 8, 6))]) == 1
    vp = win.viewport
    vp.set_view(-90, 89)
    clicks, drags = [], []
    vp.supportClicked.connect(lambda o, d, r: clicks.append(r))
    vp.supportDragged.connect(lambda *a: drags.append(a))
    vp.supportClicked.disconnect(win._support_click)
    vp.supportDragged.disconnect(win._support_drag)
    vp.start_support_mode()
    a, b = project(vp, [-4, 0, 6]), project(vp, [4, 0, 6])
    mouse(vp, QEvent.MouseButtonPress, a)
    mouse(vp, QEvent.MouseButtonRelease, a)
    assert clicks == [False] and drags == []
    mouse(vp, QEvent.MouseButtonPress, a)
    for k in range(1, 6):
        mouse(vp, QEvent.MouseMove, a + (b - a) * (k / 5), buttons=Qt.LeftButton, button=Qt.NoButton)
    assert vp.support_drag_marker is not None and vp.support_drag_marker[0] == pytest.approx(4, abs=0.3)
    mouse(vp, QEvent.MouseButtonRelease, b)
    assert len(drags) == 1 and clicks == [False] and vp.support_drag_marker is None
    mouse(vp, QEvent.MouseButtonPress, a, mods=Qt.ControlModifier)
    assert clicks == [False, True]
    mouse(vp, QEvent.MouseButtonRelease, a, mods=Qt.ControlModifier)
    assert clicks == [False, True] and len(drags) == 1
    vp.stop_support_mode()


def test_arrange_all_button(app, win, tmp_path):
    """Arrange All sits to the right of Auto Position and arranges every part, selected or not,
    as Edit → Arrange all does; one undo step."""
    files = [box_file(tmp_path, (60, 40, 10), f"b{i}.stl") for i in range(3)]
    win.import_files([str(f) for f in files])
    end = time.time() + 60
    while len(win.scene.parts) < 3 and time.time() < end:
        app.processEvents()
    parts = win.scene.parts
    assert len(parts) == 3
    for p in parts:                                  # all piled on the centre
        p.position[:2] = 0.0
    win.scene_changed()
    before = {p.id: p.position.copy() for p in parts}
    win.select([])
    props = win.props
    assert props.btn_arrange.isEnabled() and props.btn_arrange.text() == "Arrange All"
    lay = props.btn_auto.parentWidget().layout()
    row = next(lay.itemAt(i).layout() for i in range(lay.count())
               if lay.itemAt(i).layout() is not None and lay.itemAt(i).layout().indexOf(props.btn_auto) >= 0)
    assert row.indexOf(props.btn_arrange) == row.indexOf(props.btn_auto) + 1      # to its right
    props.btn_arrange.click()
    b = [p.bounds() for p in parts]
    for i in range(3):
        assert win.scene.in_volume(parts[i])
        for j in range(i + 1, 3):
            assert np.maximum(b[j][0][:2] - b[i][1][:2], b[i][0][:2] - b[j][1][:2]).max() > 0
    assert win.undo.undoText() == "Arrange"
    win.undo.undo()
    assert all(np.allclose(p.position, before[p.id]) for p in parts)


def test_view_colors_and_transparency(app, win, tmp_path):
    """Models take their colour and transparency from Preferences → 3D view colors; a
    see-through model blends with the background behind it; selected models use theirs."""
    from gooslicer.scene import Part
    vp = win.viewport
    part = Part("box", trimesh.creation.box((40, 40, 10)))
    win.scene.add(part)
    win.scene.drop_to_plate(part)
    win.scene_changed()
    vp.camera.yaw, vp.camera.pitch = -90.0, 89.0                   # from above
    vp.update()
    pump(app)
    dpr = vp.grabFramebuffer().devicePixelRatio()
    pt = project(vp, [2.5, 2.5, 10])                               # on the top face, between grid lines

    def at():
        vp.update()
        pump(app)
        im = vp.grabFramebuffer()
        return im.pixelColor(int(pt.x() * dpr), int(pt.y() * dpr))
    win.set_view_color("model", QColor(0, 255, 0))
    solid = at()
    assert solid.greenF() > 0.5 and solid.redF() < 0.1 and solid.blueF() < 0.1
    c = QColor(0, 255, 0)
    c.setAlphaF(0.5)
    win.set_view_color("model", c)
    half = at()
    assert half.alpha() == 255 and abs(half.greenF() - (solid.greenF() + 0.17) / 2) < 0.05
    assert abs(half.redF() - 0.16 / 2) < 0.05                    # half of the background shows
    win.set_view_color("selected", QColor(255, 0, 255))
    vp.set_selection([part.id])
    sel = at()
    assert sel.redF() > 0.5 and sel.blueF() > 0.5 and sel.greenF() < 0.1


def test_tip_settings_overrides(app, tmp_path):
    """08 §11: the tip dialog shows the support type's values; changed ones are saved as
    overrides that apply on top of any type, until 'Use template values'."""
    from PySide6.QtCore import QSettings
    from gooslicer import supports as S
    from gooslicer_qt.supporting import (TipSettingsDialog, load_tips, save_tips, support_params,
                                         tip_summary)
    qs = QSettings(str(tmp_path / "s.ini"), QSettings.IniFormat)
    base = S.SupportParams()
    d = TipSettingsDialog(base, load_tips(qs))
    assert d.overrides() == {} and d.values()["tip_shape"] == "cone"
    d.w["tip_shape"].setCurrentIndex(d.w["tip_shape"].findData("cross"))
    d.w["tip_top_width"].setValue(0.5)
    d.w["bot_ball"].setChecked(True)
    assert d.w["bot_ball_diameter"].isEnabled()
    assert d.overrides() == {"tip_shape": "cross", "tip_top_width": 0.5, "bot_ball": True}
    save_tips(qs, d.overrides())
    tips = load_tips(qs)
    p, _ = support_params(None, tips)
    assert (p.tip_shape, p.tip_top_width, p.bot_ball, p.tip_length) == ("cross", 0.5, True, base.tip_length)
    t, _ = support_params("tree", tips)                       # the same overrides on a tree type
    assert t.style == "tree" and t.tip_shape == "cross" and t.tip_length == S.SupportParams.tree_defaults().tip_length
    assert tip_summary(p, True) == "Cross, 0.5 mm, break point 0.35 mm (custom)"
    # reopened with the saved overrides; 'Use template values' clears them
    d2 = TipSettingsDialog(base, tips)
    assert d2.overrides() == tips
    d2.reset_btn.click()
    assert d2.overrides() == {}
    save_tips(qs, d2.overrides())
    assert load_tips(qs) == {} and qs.value("supportTips") is None
    # bad stored values are ignored
    qs.setValue("supportTips", '{"tip_shape": "star", "tip_length": 999, "bogus": 1}')
    assert load_tips(qs) == {"tip_length": 20.0}


def test_see_through_nearest_surface_on_top(app, win, tmp_path):
    """A see-through hollow part shows its outer skin on top at its opacity whatever order its
    faces are stored in (unsorted blending painted inner walls over the skin)."""
    from gooslicer_qt.commands import MeshCommand
    assert wait_import(app, win, [box_file(tmp_path)]) == 1
    p = win.scene.parts[0]
    win.select([])
    outer = trimesh.creation.icosphere(subdivisions=3, radius=15)
    inner = trimesh.creation.icosphere(subdivisions=3, radius=12)
    inner.invert()
    px = []
    for parts in ([outer, inner], [inner, outer]):
        win.undo.push(MeshCommand(win, p.id, trimesh.util.concatenate(parts), "Hollow", opacity=0.75))
        win.viewport.frame_selection()
        pump(app)
        c = project(win.viewport, p.bounds().mean(axis=0) + [0, 0, 9])   # skin faces up, inner wall down
        px.append(win.viewport.grabFramebuffer().pixelColor(int(c.x()), int(c.y())))
    a, b = px
    assert max(abs(a.red() - b.red()), abs(a.green() - b.green()), abs(a.blue() - b.blue())) <= 3


def test_layer_slider_hides_above_cut(app, win, tmp_path):
    vp = win.viewport
    assert vp.layer_slider.isHidden()                       # empty plate
    assert wait_import(app, win, [box_file(tmp_path, size=(20, 20, 20))]) == 1
    win.select([])
    pump(app)
    sl = vp.layer_slider
    assert not sl.isHidden() and sl.cut() is None and sl.label_text() == "All"
    assert abs(sl.top - 20) < 1e-6
    assert sl.geometry().top() > vp.compass_rect().bottom()  # under the compass, right side
    assert sl.geometry().right() > vp.width() - 30
    vp.frame_selection()
    pump(app)
    hi, lo = project(vp, [0, -10, 16]), project(vp, [0, -10, 4])
    def px(pt):
        return vp.grabFramebuffer().pixelColor(int(pt.x()), int(pt.y()))
    bg = vp.grabFramebuffer().pixelColor(5, vp.height() - 5)
    before = px(hi)
    assert before != bg and px(lo) != bg
    sl.set_cut(10.0)
    assert vp.clip_z == 10.0 and sl.label_text() == "10.00 mm"
    pump(app)
    a, b = px(hi), px(lo)
    # the outer wall above the cut is gone: the box's inside (back-face colour) shows there
    assert a != before and a.red() > a.green() + 80 and a.blue() > a.green() + 80
    assert b != bg
    from PySide6.QtGui import QColor
    win.set_view_color("cut", QColor(20, 200, 40))         # Preferences → 3D view colors
    pump(app)
    g = px(hi)
    assert g.green() > 150 and g.red() < 80 and g.blue() < 100
    win.scene_changed()                                     # keeps its height when parts change

    assert sl.cut() == 10.0
    sl.set_cut(None)
    pump(app)
    assert vp.clip_z is None and px(hi) == before
    sl.set_cut(5.0)
    win.undo.undo()                                         # plate empty again: slider hidden, all shown
    pump(app)
    assert sl.isHidden() and vp.clip_z is None


def wait_cut(app, w):
    loop = QEventLoop()
    got = []
    w.cutFinished.connect(lambda ok: (got.append(ok), loop.quit()))
    QTimer.singleShot(60000, loop.quit)
    started = w.cut_selection()
    if started:
        loop.exec()
    w.cutFinished.disconnect()
    return bool(got and got[0])


def test_modifying_select_and_cut(app, win, tmp_path):
    """Models tab → Modifying (11): the Mesh pane starts folded with its verdict; Select surface
    picks a cap that the spread grows; Cut out selection cuts along its outline into the
    cavity of a hollow ball, giving two parts in one undo step."""
    import manifold3d as m3
    import trimesh

    def mf(m):
        return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))
    shell = (mf(trimesh.creation.icosphere(subdivisions=4, radius=12))
             - mf(trimesh.creation.icosphere(subdivisions=4, radius=10.5))).to_mesh()
    f = tmp_path / "ball.stl"
    trimesh.Trimesh(np.asarray(shell.vert_properties)[:, :3], np.asarray(shell.tri_verts)).export(f)
    assert wait_import(app, win, [f]) == 1
    p = win.scene.parts[0]
    win.select([p.id])
    pump(app)
    pp = win.props
    assert pp.mesh_btn.text() == "Mesh: OK" and pp.mesh_box.isHidden() and not pp.mesh_btn.isChecked()
    assert not pp.cut_btn.isEnabled() and pp.region_info.text() == "Nothing selected"
    assert pp.cut_depth.value() == 3.0 and pp.cut_smooth.value() == 0.5 and pp.cut_gap.value() == 0.1
    pp.select_btn.click()
    vp = win.viewport
    assert vp.region_mode and vp.rings() is None
    top = p.bounds()[1].copy()
    top[:2] = p.bounds().mean(axis=0)[:2]
    vp.set_view(-90, 89)                                     # from above
    pump(app)
    at = project(vp, top)
    mouse(vp, QEvent.MouseButtonPress, at)
    mouse(vp, QEvent.MouseButtonRelease, at, Qt.LeftButton, Qt.NoButton)
    r = win._region
    assert r is not None and r["pid"] == p.id and vp.region_mesh is not None
    n35 = int(r["mask"].sum())
    assert "triangles" in pp.region_info.text() and pp.cut_btn.isEnabled()
    pp.spread.setValue(60)
    assert int(win._region["mask"].sum()) > n35 and int(win.settings.value("modify/spread")) == 60
    pp.spread.setValue(40)
    # contours: shown while selecting (a smooth ball has none); Grow selection by mm
    assert pp.stop_contours.isChecked() and pp.contour_angle.value() == 35
    assert vp.contour_lines is not None and len(vp.contour_lines) == 0
    assert pp.grow_btn.isEnabled() and pp.grow_mm.value() == 1.0
    n40 = int(win._region["mask"].sum())
    pp.grow_btn.click()
    assert int(win._region["mask"].sum()) > n40 and "(grown +1 mm)" in pp.region_info.text()
    pp.spread.setValue(41)                                   # a grow is kept through changes
    assert win._region["grows"] == [1.0]
    pp.grow_mm.setValue(-1.0)
    pp.grow_btn.click()
    pp.spread.setValue(40)
    assert abs(int(win._region["mask"].sum()) - n40) < 0.1 * n40 and float(win.settings.value("modify/grow")) == -1.0
    loop = QEventLoop()                                      # the cutter preview follows
    QTimer.singleShot(1500, loop.quit)
    loop.exec()
    assert vp.cut_preview is not None and len(vp.cut_preview.faces)    # the cut's faces inside the ball
    from PySide6.QtTest import QTest
    QTest.keyClick(vp, Qt.Key_Escape)                        # stops picking, keeps the selection
    assert not vp.region_mode and not pp.select_btn.isChecked() and win._region is not None
    vol = p.mesh.volume
    assert wait_cut(app, win)
    assert len(win.scene.parts) == 2 and win._region is None and vp.region_mesh is None
    piece = next(q for q in win.scene.parts if q.id != p.id)
    assert piece.name == "ball piece" and win.primary() is piece
    assert win.reports[id(piece.mesh)].ok and win.reports[id(p.mesh)].ok
    # the fit gap took a 0.1 mm ring out: the two add up to a little less than the ball
    assert 0.9 * vol < piece.mesh.volume + p.mesh.volume < vol
    assert piece.mesh.volume < 0.2 * vol                     # a curved cap of wall, no floor
    assert abs(piece.bounds()[0][2]) < 1e-6                  # cut side down on the plate
    assert piece.bounds()[0][0] > p.bounds()[1][0]           # beside the rest
    win.undo.undo()                                          # one step
    assert len(win.scene.parts) == 1 and abs(win.scene.parts[0].mesh.volume - vol) < 1e-6 * vol


def test_cut_refuses_open_mesh(app, win, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    win.settings.setValue("import/autoRepair", False)
    wait_import(app, win, [box_file(tmp_path, broken=True, name="holey.stl")])
    p = win.scene.parts[0]
    win.select([p.id])
    top = int(np.argmax(p.world_mesh().triangles_center[:, 2]))
    win.region_pick(p.id, top, False)
    assert win._region["mask"].any()
    said = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a: said.append(a[2]))
    assert not win.cut_selection() and "Repair it first" in said[0] and len(win.scene.parts) == 1
