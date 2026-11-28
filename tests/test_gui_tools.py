"""Magnet pockets, planar cuts, build plates and the project library in the window (11).
Real GL: run via scripts/test-gui.sh."""
import os

import numpy as np
import pytest

if os.environ.get("GOOSLICER_GL_TESTS") != "1":
    pytest.skip("GUI tests need scripts/test-gui.sh (Xvfb)", allow_module_level=True)

from PySide6.QtCore import QEventLoop, QTimer  # noqa: E402

from test_gui_viewport import app, box_file, pump, wait_import, win  # noqa: E402,F401


def wait_undo(win, n0, timeout=60000):
    loop = QEventLoop()
    win.undo.indexChanged.connect(loop.quit)
    QTimer.singleShot(timeout, loop.quit)
    if win.undo.index() == n0:
        loop.exec()
    win.undo.indexChanged.disconnect(loop.quit)


def test_magnet_pocket_click(app, win, tmp_path):
    """11 §1: a click in magnet mode cuts a pocket of the panel's size; one undo step."""
    wait_import(app, win, [box_file(tmp_path)])
    p = win.scene.parts[0]
    v0 = p.world_mesh().volume
    win.magnet_mode()
    assert win.magnet_panel.isVisible() and win.viewport.hole_mode
    f = win.magnet_panel.fields
    f.size.setCurrentIndex(f.size.findText("8 × 3 mm"))
    assert (f.diameter.value(), f.depth.value()) == (8.3, 3.3)
    top = np.array([0.0, 0.0, 10.0])
    face = int(np.argmax(p.mesh.face_normals @ [0, 0, 1]))
    n0 = win.undo.index()
    win._hole_picked(p.id, face, top)
    wait_undo(win, n0)
    p = win.scene.parts[0]
    removed = v0 - p.world_mesh().volume
    assert removed == pytest.approx(np.pi * 4.15 ** 2 * 3.3, rel=0.01)
    assert win.undo.undoText() == "Magnet pocket"
    assert win.settings.value("magnet/size") == "8 × 3 mm"
    win.viewport.stop_hole_mode()
    assert not win.magnet_panel.isVisible()
    win.undo.undo()
    assert win.scene.parts[0].world_mesh().volume == pytest.approx(v0)


def test_magnet_pocket_warns_about_a_thin_wall(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path, size=(20, 20, 3))])
    p = win.scene.parts[0]
    win.magnet_mode()
    f = win.magnet_panel.fields
    f.diameter.setValue(6.0)
    f.depth.setValue(2.6)
    face = int(np.argmax(p.mesh.face_normals @ [0, 0, 1]))
    n0 = win.undo.index()
    win._hole_picked(p.id, face, np.array([0.0, 0.0, 3.0]))
    wait_undo(win, n0)
    assert "only 0.4 mm of wall" in win.log.toPlainText()
    win.viewport.stop_hole_mode()


def _cut(win, n0):
    win.cut_selected()
    wait_undo(win, n0)


def test_cut_with_peg_and_undo(app, win, tmp_path):
    """11 §2: Cut splits the part in two named halves with a peg and socket; undo restores it."""
    wait_import(app, win, [box_file(tmp_path)])
    p = win.scene.parts[0]
    win.select([p.id])
    win.cut_mode()
    f = win.cut_panel.fields
    assert win.cut_panel.isVisible() and f.axis() == (0.0, 0.0, 1.0)
    assert f.position.value() == pytest.approx(5.0)              # the part's middle
    assert win.viewport.cut_preview is not None
    f.connector.setCurrentIndex(f.connector.findData("round"))
    f.gap.setValue(0.0)
    n0 = win.undo.index()
    _cut(win, n0)
    names = sorted(x.name for x in win.scene.parts)
    assert names == ["box A", "box B"]
    a = next(x for x in win.scene.parts if x.name == "box A")
    b = next(x for x in win.scene.parts if x.name == "box B")
    peg = np.pi * 2.5 ** 2 * 4
    assert a.world_mesh().volume == pytest.approx(2000 + peg, rel=0.01)
    assert a.bounds()[0][2] == pytest.approx(0) and b.bounds()[0][2] == pytest.approx(0)   # on the plate
    assert "1 of 1 round connector placed" in win.log.toPlainText()
    win.undo.undo()
    assert [x.name for x in win.scene.parts] == ["box"]
    win.end_cut_mode()
    assert win.viewport.cut_preview is None


def test_cut_along_a_face(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path)])
    p = win.scene.parts[0]
    win.select([p.id])
    win.cut_mode()
    f = win.cut_panel.fields
    f.normal.setCurrentIndex(f.normal.findText("Face"))
    assert win.viewport.hole_mode and win.cut_panel.status.text().startswith("Click")
    face = int(np.argmax(p.mesh.face_normals @ [1, 0, 0]))       # the +X side
    win._hole_picked(p.id, face, np.array([4.0, 0.0, 5.0]))      # a point 4 mm along X
    assert not win.viewport.hole_mode and win.viewport.cut_preview is not None
    assert f.position.value() == pytest.approx(4.0)
    n0 = win.undo.index()
    _cut(win, n0)
    vols = sorted(x.world_mesh().volume for x in win.scene.parts)
    assert vols == pytest.approx([6 * 20 * 10, 14 * 20 * 10])
    win.end_cut_mode()


def test_plates_add_place_slice_and_delete(app, win, tmp_path):
    """11 §3: a second plate, parts imported onto the current plate, its slice job centred,
    and Delete plate moving its parts back to plate 1 (undoable)."""
    wait_import(app, win, [box_file(tmp_path)])
    first = win.scene.parts[0]
    win.plate_ctl.add_plate()
    assert win.scene.plates == 2 and win.current_plate == 1
    assert win.job_panel.plate_row.isVisible() and win.job_panel.plate_combo.count() == 2
    wait_import(app, win, [box_file(tmp_path, name="second.stl")])
    second = next(p for p in win.scene.parts if p.name == "second")
    assert win.scene.plate_of(second) == 1 and win.scene.in_volume(second)
    job = win.make_slice_job()
    assert len(job.meshes) == 1 and abs(job.meshes[0].bounds.mean(axis=0)[0]) < 1.0      # centred
    assert job.check_meshes[0].bounds.mean(axis=0)[0] == pytest.approx(win.scene.pitch, abs=1.0)
    fp1 = win._plate_fingerprint()
    win.set_current_plate(0)
    assert not win._same_plate(fp1, win._plate_fingerprint())        # another plate: stale
    assert len(win.make_slice_job().meshes) == 1
    win.set_current_plate(1)
    win.plate_ctl.delete_plate()
    assert win.scene.plates == 1 and win.scene.plate_of(second) == 0
    assert win.scene.in_volume(second) and win.scene.in_volume(first)
    win.undo.undo()
    assert win.scene.plates == 2 and win.scene.plate_of(second) == 1
    win.undo.undo()                                                   # the import
    win.undo.undo()                                                   # Add plate
    assert win.scene.plates == 1
    with pytest.raises(ValueError):
        win.scene.delete_plate(0)
    assert not win.plate_ctl.delete_plate()


def test_arrange_spills_onto_new_plates(app, win, tmp_path):
    big = box_file(tmp_path, size=(150, 100, 10), name="big.stl")      # one per plate
    for _ in range(3):
        wait_import(app, win, [big])
    assert win.scene.plates == 3                                       # import spilled over
    assert "placed on plate 3 (added)" in win.log.toPlainText()
    win.scene.plates = 5
    win.arrange_all()
    assert win.scene.plates == 5 and all(win.scene.in_volume(p) for p in win.scene.parts)
    assert sorted(win.scene.plate_of(p) for p in win.scene.parts) == [0, 1, 2]
    win.undo.undo()
    assert win.scene.plates == 5


def test_project_keeps_plates(app, win, tmp_path):
    wait_import(app, win, [box_file(tmp_path)])
    win.plate_ctl.add_plate()
    out = tmp_path / "plates.3mf"
    loop = QEventLoop()
    win.project.saved.connect(lambda _: loop.quit())
    QTimer.singleShot(30000, loop.quit)
    win.project._write(out)
    loop.exec()
    win.project.new_project()
    assert win.scene.plates == 1
    loop = QEventLoop()
    win.project.opened.connect(lambda _: loop.quit())
    QTimer.singleShot(30000, loop.quit)
    win.project.open_project(str(out))
    loop.exec()
    assert win.scene.plates == 2 and win.current_plate == 1


def test_library_save_version_and_window(app, win, tmp_path, monkeypatch):
    """11 §4: Save version on a new project adds it to the library, then records V1; the
    window lists the project, design and version; Open version opens an unsaved copy."""
    from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox
    from gooslicer import library as L
    from gooslicer_qt import library as LW
    root = tmp_path / "Library"
    win.settings.setValue("library/root", str(root))
    wait_import(app, win, [box_file(tmp_path)])
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("first cut", True))

    def accept(self):
        self.project.setCurrentText("Lisa")
        self.design.setText("Clear parts")
        return QDialog.Accepted
    monkeypatch.setattr(LW.AddToLibraryDialog, "exec", accept)
    loop = QEventLoop()
    QTimer.singleShot(30000, loop.quit)
    win.project.save_version(then=loop.quit)
    loop.exec()
    d = L.Library(root).find(root / "Lisa" / "Clear parts.3mf")
    assert d is not None and [(v.n, v.note) for v in d.versions()] == [(1, "first cut")]
    assert win.project.path == root / "Lisa" / "Clear parts.3mf"
    assert win.windowTitle().startswith("Clear parts — Lisa")
    win.show_library()
    lw = win.library_window
    assert lw.projects.currentItem().text() == "Lisa"
    assert lw.designs.currentItem().text().startswith("Clear parts")
    assert lw.versions.count() == 1 and "first cut" in lw.versions.item(0).text()
    lw.status.setCurrentIndex(lw.status.findData("ready"))
    lw._status_changed()
    assert L.Library(root).find(d.path).status == "ready"
    loop = QEventLoop()
    win.project.opened.connect(lambda _: loop.quit())
    QTimer.singleShot(30000, loop.quit)
    lw.open_version()
    loop.exec()
    assert win.project.path == d.path and win.project.is_modified()
    assert "(V1)" in win.windowTitle()
    lw.close()


def _layout(win):
    """Each part's plate and its place on that plate."""
    s = win.scene
    return sorted((s.plate_of(p), round(float(p.position[0] - s.plate_offset(s.plate_of(p))[0]), 3),
                   round(float(p.position[1]), 3)) for p in s.parts)


def test_plate_buttons_in_the_view(app, win, tmp_path):
    """11 §3: the − beside a plate's label deletes that plate (an empty one moves nothing but
    the later plates, which keep their layout); the + adds a plate; no − with one plate."""
    from PySide6.QtCore import QEvent
    from test_gui_viewport import mouse
    f = box_file(tmp_path, size=(30, 30, 10))
    wait_import(app, win, [f, f])
    win.plate_ctl.add_plate()
    win.plate_ctl.add_plate()
    wait_import(app, win, [f, f])                           # onto plate 3
    vp = win.viewport
    vp.set_view(-90, 60)
    vp.frame_all_plates()
    pump(app)
    win.select([win.scene.parts[0].id])                     # plate 1 current: the − must not care
    before = _layout(win)
    assert [p for _, p, _ in before] and {k for k, _, _ in before} == {0, 2}
    btn = {(k, i): c for k, i, c in vp.plate_buttons()}
    assert set(btn) == {(k, i) for k in ("add", "delete") for i in range(3)}
    for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
        mouse(vp, kind, btn[("delete", 1)])
    pump(app)
    assert win.scene.plates == 2
    assert _layout(win) == [((1 if k == 2 else k), x, y) for k, x, y in before]
    win.undo.undo()
    assert win.scene.plates == 3 and _layout(win) == before
    btn = {(k, i): c for k, i, c in vp.plate_buttons()}
    for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
        mouse(vp, kind, btn[("add", 0)])
    assert win.scene.plates == 4 and _layout(win) == before
    win.scene.plates = 1
    for p in list(win.scene.parts):
        win.scene.remove(p.id)
    vp.update()
    assert {k for k, _, _ in vp.plate_buttons()} == {"add"}


def test_clicking_a_plate_label_makes_it_current(app, win, tmp_path):
    from PySide6.QtCore import QEvent
    from test_gui_viewport import mouse
    win.plate_ctl.add_plate()
    win.plate_ctl.add_plate()
    vp = win.viewport
    vp.set_view(-90, 60)
    vp.frame_all_plates()
    pump(app)
    assert win.current_plate == 2
    for i in (0, 1):
        q = vp.plate_label_quad(i)
        c = (q[0] + q[1] + q[2] + q[3]) / 4
        assert vp.plate_label_at(c) == i
        for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            mouse(vp, kind, c)
        assert win.current_plate == i and vp.current_plate == i
    assert win.scene.plates == 3 and win.undo.index() == 2      # nothing else changed
