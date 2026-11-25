"""Move arrows, scale handles and the handle mode in the 3D view (09 §8, §9). Real GL: run via
scripts/test-gui.sh."""
import os

import numpy as np
import pytest

if os.environ.get("GOOSLICER_GL_TESTS") != "1":
    pytest.skip("GL viewport tests need scripts/test-gui.sh (Xvfb)", allow_module_level=True)

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from test_gui_viewport import app, box_file, mouse, project, pump, wait_import, win  # noqa: E402,F401


def _one(app, win, tmp_path, size=(30, 10, 6)):
    assert wait_import(app, win, [box_file(tmp_path, size)]) == 1
    p = win.scene.parts[-1]
    win.select([p.id])
    pump(app)
    return p


def _drag_to(vp, a, b, steps=6, mods=Qt.NoModifier):
    mouse(vp, QEvent.MouseButtonPress, a)
    for k in range(1, steps + 1):
        mouse(vp, QEvent.MouseMove, a + (b - a) * (k / steps), mods=mods)


def _release(vp, b):
    mouse(vp, QEvent.MouseButtonRelease, b)


def test_mode_buttons_menu_and_keys_agree(app, win, tmp_path):
    from PySide6.QtCore import QSettings
    vp = win.viewport
    assert vp.handle_mode == "rotate" and vp.mode_buttons["rotate"].isChecked()
    vp.mode_buttons["move"].click()
    assert vp.handle_mode == "move" and win.handle_actions["move"].isChecked()
    win.handle_actions["scale"].trigger()
    assert vp.handle_mode == "scale" and vp.mode_buttons["scale"].isChecked()
    assert win.shortcuts.entries["edit.handles.move"].action.shortcut().toString() == "W"
    win.activateWindow()
    vp.setFocus()
    pump(app)
    from PySide6.QtTest import QTest
    QTest.keyClick(vp, Qt.Key_W)
    assert vp.handle_mode == "move"
    assert win.settings.value("view/handleMode") == "move"
    # remembered for the next start
    from gooslicer_qt.main import MainWindow
    w2 = MainWindow(settings=QSettings(win.settings.fileName(), QSettings.IniFormat))
    assert w2.viewport.handle_mode == "move" and w2.handle_actions["move"].isChecked()
    w2.close()
    # only the current mode's handles exist
    p = _one(app, win, tmp_path)
    assert vp.rings() is None and vp.handles()[0] == "move"
    vp.set_handle_mode("rotate")
    assert vp.rings() is not None and vp.handles() is None
    del p


def test_move_arrow(app, win, tmp_path):
    p = _one(app, win, tmp_path)
    vp = win.viewport
    vp.set_handle_mode("move")
    vp.set_view(-60, 25)
    pump(app)
    kind, base, ends = vp.handles()
    assert kind == "move" and [i for i, _ in ends] == [0, 1]
    assert abs(base[2] - p.bounds()[0][2]) < 1e-9                   # on the plate
    tip = dict(ends)[0]
    a = project(vp, tip)
    assert vp.handle_at(a) == 0
    mouse(vp, QEvent.MouseMove, a, Qt.NoButton, Qt.NoButton)
    assert vp._handle_hover == 0
    before = p.state()
    n0 = win.undo.index()
    b = project(vp, tip + np.array([10.0, 0, 0]))
    _drag_to(vp, a, b)
    assert vp._hdrag["value"] == pytest.approx(10.0, abs=0.02)
    _release(vp, b)
    assert vp._hdrag is None and win.undo.index() == n0 + 1
    assert win.undo.text(n0).startswith("Move X +")
    assert p.position[0] - before.position[0] == pytest.approx(10.0, abs=0.02)
    assert p.position[1] == before.position[1] and p.position[2] == before.position[2]
    win.undo.undo()
    assert p.state() == before
    # Ctrl: whole millimetres
    _drag_to(vp, a, project(vp, tip + np.array([7.4, 0, 0])), mods=Qt.ControlModifier)
    assert vp._hdrag["value"] == 7.0
    _release(vp, b)
    win.undo.undo()
    # the Y arrow moves along Y only
    tip_y = dict(ends)[1]
    ay = project(vp, tip_y)
    assert vp.handle_at(ay) == 1
    _drag_to(vp, ay, project(vp, tip_y + np.array([0, -5.0, 0])))
    _release(vp, ay)
    assert p.position[1] - before.position[1] == pytest.approx(-5.0, abs=0.02)
    assert p.position[0] == before.position[0]
    win.undo.undo()


def test_escape_cancels(app, win, tmp_path):
    p = _one(app, win, tmp_path)
    vp = win.viewport
    vp.set_handle_mode("move")
    vp.set_view(-60, 25)
    pump(app)
    tip = dict(vp.handles()[2])[0]
    before = p.state()
    n0 = win.undo.index()
    _drag_to(vp, project(vp, tip), project(vp, tip + np.array([12.0, 0, 0])))
    assert p.state() != before
    QApplication.sendEvent(vp, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert vp._hdrag is None and p.state() == before
    _release(vp, project(vp, tip))
    assert win.undo.index() == n0                                    # nothing to undo
    # rings too
    vp.set_handle_mode("rotate")
    vp.set_view(-90, 89)
    pump(app)
    c, r = vp.rings()
    a = project(vp, c + r * np.array([0.7071, 0.7071, 0]))
    b = project(vp, c + r * np.array([0.0, 1.0, 0]))
    _drag_to(vp, a, b)
    assert vp._rot is not None and p.state() != before
    QApplication.sendEvent(vp, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert vp._rot is None and p.state() == before
    _release(vp, b)
    assert win.undo.index() == n0


def _scale_drag(app, vp, index, factor, mods=Qt.NoModifier):
    """Drag a scale handle so the mouse ends `factor` times as far from the centre (on screen)."""
    kind, centre, ends = vp.handles()
    end = dict(ends)[index]
    if index == 3:
        lo, hi = vp._selection_box(vp._selected_parts())
        centre = (lo + hi) / 2
    c, e = project(vp, centre), project(vp, end)
    a = e
    b = c + (e - c) * factor
    assert vp.handle_at(a) == index
    _drag_to(vp, a, b, steps=10, mods=mods)
    value = vp._hdrag["value"]
    _release(vp, b)
    return value


def test_scale_one_axis_on_a_turned_part(app, win, tmp_path):
    p = _one(app, win, tmp_path, (20, 10, 6))
    vp = win.viewport
    win.scene.rotate(p, [0, 0, 1], 30)
    win.props.uniform.setChecked(False)
    vp.set_handle_mode("scale")
    vp.set_view(-60, 35)
    pump(app)
    kind, centre, ends = vp.handles()
    assert kind == "scale" and [i for i, _ in ends] == [0, 1, 2, 3]
    # the cubes sit on the part's own axes
    y_end = dict(ends)[1]
    d = (y_end - centre) / np.linalg.norm(y_end - centre)
    assert np.allclose(d, p.rotation[:, 1])
    before = p.state()
    f = _scale_drag(app, vp, 1, 1.5)
    assert 1.3 <= f <= 1.7 and round(f * 100) == f * 100              # 1 % steps
    assert p.scale[1] == pytest.approx(before.scale[1] * f)
    assert p.scale[0] == before.scale[0] and p.scale[2] == before.scale[2]
    assert np.allclose(p.rotation, before.rotation) and np.allclose(p.position[:2], before.position[:2])
    assert abs(p.bounds()[0][2]) < 1e-6                              # still on the plate
    assert win.undo.text(win.undo.index() - 1) == f"Scale Y {f * 100:.0f} %"
    # the Mesh panel followed
    assert win.props.scale[1].value() == pytest.approx(f * 100, abs=0.01)
    win.undo.undo()
    assert p.state() == before
    # Uniform scale ticked: an axis cube scales every axis
    win.props.uniform.setChecked(True)
    f = _scale_drag(app, vp, 0, 1.3, mods=Qt.ControlModifier)
    assert f == pytest.approx(1.3)                                   # 10 % steps with Ctrl
    assert np.allclose(p.scale, before.scale * f)
    assert win.undo.text(win.undo.index() - 1) == "Scale 130 %"
    win.undo.undo()
    # never below 1 %
    f = _scale_drag(app, vp, 3, -2.0)
    assert f == 0.01 and np.allclose(p.scale, before.scale * 0.01)
    win.undo.undo()


def test_scale_several_parts(app, win, tmp_path):
    a = _one(app, win, tmp_path, (10, 10, 10))
    b = _one(app, win, tmp_path, (10, 10, 10))
    win.scene.parts[0].position[:2] = [-15, 0]
    win.scene.parts[1].position[:2] = [15, 0]
    win.select([a.id, b.id])
    vp = win.viewport
    vp.set_handle_mode("scale")
    vp.set_view(-60, 30)
    pump(app)
    kind, centre, ends = vp.handles()
    assert [i for i, _ in ends] == [3]                                # only the uniform handle
    gap0 = np.linalg.norm(a.position[:2] - b.position[:2])
    f = _scale_drag(app, vp, 3, 1.4)
    assert np.allclose(a.scale, f) and np.allclose(b.scale, f)
    assert np.linalg.norm(a.position[:2] - b.position[:2]) == pytest.approx(gap0 * f)
    assert win.undo.text(win.undo.index() - 1) == f"Scale {f * 100:.0f} %"
    win.undo.undo()
    assert np.allclose(a.scale, 1) and np.allclose(b.scale, 1)


def test_hidden_in_picking_modes(app, win, tmp_path):
    p = _one(app, win, tmp_path)
    vp = win.viewport
    for mode in ("move", "scale"):
        vp.set_handle_mode(mode)
        assert vp.handles() is not None
        for start, stop in ((vp.start_support_mode, vp.stop_support_mode),
                            (vp.start_hole_mode, vp.stop_hole_mode),
                            (vp.start_lay_flat, lambda: setattr(vp, "lay_flat_mode", False))):
            start()
            assert vp.handles() is None and vp.handle_at(QPointF(10, 10)) is None
            stop()
    del p


def test_supported_part_keeps_its_plan(app, win, tmp_path):
    from gooslicer.supports import SupportParams, SupportPlan
    p = _one(app, win, tmp_path, (20, 10, 6))
    plan = SupportPlan(SupportParams(), np.array([[0.0, 0, -3]]), np.array([[0.0, 0, -1]]), ["manual"], "")
    p.supports = plan
    p.elevation = 5.0
    win.scene.drop_to_plate(p)
    win.scene_changed()
    vp = win.viewport
    vp.set_handle_mode("move")
    vp.set_view(-60, 25)
    pump(app)
    tip = dict(vp.handles()[2])[0]
    _drag_to(vp, project(vp, tip), project(vp, tip + np.array([6.0, 0, 0])))
    _release(vp, project(vp, tip))
    assert p.supports is plan and p.bounds()[0][2] == pytest.approx(5.0)
    from test_gui_smoke import _wait
    assert _wait(app, lambda: win.supports.entry(p) is not None, 60)   # rebuilt / followed
