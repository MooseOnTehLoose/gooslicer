import os
from pathlib import Path

import pytest


QtCore = pytest.importorskip("PySide6.QtCore")
from PySide6.QtWidgets import QApplication  # noqa: E402

from test_profiles import FIXTURE  # noqa: E402


@pytest.fixture(scope="module")
def app():
    return QApplication.instance() or QApplication([])


def test_main_window_loads_profiles(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    assert w.printer_panel.current() is None and "No printer profiles" in w.log.toPlainText()
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    p = w.printer_panel.current()
    assert p.name == "ELEGOO Saturn 4 Ultra 16K"
    assert w.printer_panel.info["Pixel pitch"].text() == "14.0 × 19.0 µm"
    assert s.value("printer") == "ELEGOO Saturn 4 Ultra 16K"
    w.close()


def _wait(app, cond, timeout=60):
    import time
    end = time.time() + timeout
    while not cond() and time.time() < end:
        app.processEvents()
        time.sleep(0.01)
    return cond()


def test_slice_from_window(app, tmp_path, monkeypatch):
    import json

    import trimesh

    from elegoo_goo import goo
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    # fake SatelLite install layout: resources/machines/printer/*.ini + resources/resin/material/*.data
    res = tmp_path / "install" / "resources"
    ini = res / "machines" / "printer" / "officialPlatform.ini"
    ini.parent.mkdir(parents=True)
    ini.write_bytes(FIXTURE.encode())
    mat = res / "resin" / "material"
    mat.mkdir(parents=True)
    ap = {"vd_resine_profilename": "Grey Test",
          "cloudResinExtraParam": {"cloudResinPrinterName": "ELEGOO Saturn 4 Ultra 16K"},
          "vd_printer_printer_para": {"vd_printer_printer_para_exposure_time": 2.2,
                                      "vd_printer_printer_para_thickness": 0.1}}
    (mat / "1.data").write_text(json.dumps({"platitem": {"vd_printer": {"vd_active_printer": ap}}}))

    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    assert w.import_profiles(str(ini))
    ps = w.print_settings
    assert [r.name for r in ps.resins] == ["Grey Test"]
    ps.resin.setCurrentIndex(1)
    assert ps.settings().exposure == 2.2 and ps.settings().layer_height == 0.1

    stl = tmp_path / "box.stl"
    trimesh.creation.box((8, 6, 3)).export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    job = w.make_slice_job()
    assert job.layer_count == 30
    out = tmp_path / "box.goo"
    w.start_slice(job, str(out))
    assert _wait(app, lambda: w._slice_thread is None, 120)
    g = goo.read(out)
    assert g.header.layer_count == 30 and g.header.exposure_time == pytest.approx(2.2)
    assert g.header.resin_profile == "Grey Test" and any(g.header.big_preview)
    assert w.layer_preview.goo is not None and w.layer_preview.slider.maximum() == 29
    w.layer_preview.slider.setValue(10)
    assert "Layer 11 / 30" in w.layer_preview.info.text()
    assert w.layer_preview._last.max() == 255
    assert s.value("resin/ELEGOO Saturn 4 Ultra 16K") == "Grey Test"
    w.close()


def test_printer_control_against_simulator(app, tmp_path, monkeypatch):
    """Discover → connect → slice-and-send → start → pause/resume → stop → delete, on the simulator."""
    import json

    import trimesh

    from sim_printer import SimPrinter
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "cache"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    res = tmp_path / "install" / "resources"
    ini = res / "machines" / "printer" / "officialPlatform.ini"
    ini.parent.mkdir(parents=True)
    ini.write_bytes(FIXTURE.encode())
    (res / "resin" / "material").mkdir(parents=True)

    sim = SimPrinter(layer_period=0.05)
    old = SimPrinter(name="Old", model="Saturn 3 Ultra", version="V1.0.0", mainboard="0ld0000000000001")
    try:
        from gooslicer_qt.main import MainWindow
        from PySide6.QtWidgets import QMessageBox
        warned = []
        monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
        s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
        w = MainWindow(settings=s, lan_ports={"discovery": sim.udp_port, "ws": sim.ws_port,
                                              "http": sim.http_port, "targets": ["127.0.0.1"]})
        lan = w.lan
        asked = []
        lan.confirm = lambda title, text: asked.append(title) or True
        assert w.import_profiles(str(ini))

        # an incompatible (pre-V3) printer is listed with a warning and cannot be connected
        lan.ports = {**lan.ports, "discovery": old.udp_port}
        lan.find_printers()
        assert _wait(app, lambda: lan.combo.count() == 1)
        assert "[not compatible]" in lan.combo.itemText(0) and lan.warn_label.isVisibleTo(lan)
        lan.connect_printer()
        assert lan.session is None and "no longer" in warned[-1]

        lan.ports = {**lan.ports, "discovery": sim.udp_port}
        lan.find_printers()
        assert _wait(app, lambda: lan.combo.count() == 2)
        assert lan.current().name == "Sim" and not lan.warn_label.isVisibleTo(lan)
        lan.connect_printer()
        assert _wait(app, lambda: lan.session and lan.session.connected and lan.state.attributes)
        assert _wait(app, lambda: lan.state_label.text() == "Idle")
        assert "UV LED" in lan.temp_label.text() and lan.send_btn.isEnabled()
        assert not lan.pause_btn.isEnabled()

        stl = tmp_path / "box.stl"
        trimesh.creation.box((8, 6, 1)).export(stl)
        w.import_files([str(stl)])
        assert _wait(app, lambda: len(w.scene.parts) == 1)
        w.slice_and_send()
        assert _wait(app, lambda: "Start print" in asked, 120)
        assert asked[:2] == ["Send to printer", "Start print"]
        assert "box.goo" in sim.files
        assert _wait(app, lambda: lan.state.is_printing and lan.layer_label.text().endswith("/ 20"))
        assert _wait(app, lambda: lan.files.count() == 1)
        assert lan.pause_btn.isEnabled() and lan.stop_btn.isEnabled() and not lan.print_file_btn.isEnabled()

        lan.pause_resume()
        assert _wait(app, lambda: lan.pause_btn.text() == "Resume")
        assert "Paused" in lan.state_label.text()
        lan.pause_resume()                       # resume needs no confirmation
        assert _wait(app, lambda: lan.pause_btn.text() == "Pause")
        lan.stop_print()
        assert _wait(app, lambda: not lan.state.is_printing)
        assert asked == ["Send to printer", "Start print", "Pause print", "Stop print"]

        lan.files.item(0).setSelected(True)
        lan.delete_selected()
        assert _wait(app, lambda: lan.files.count() == 0 and not sim.files)

        # a file sliced for another resolution is refused before upload
        bad = tmp_path / "bad.goo"
        from elegoo_goo import goo
        goo.write(str(bad), goo.Header(machine_type="ELEGOO Saturn 4 Ultra", resolution_x=11520,
                                       resolution_y=5120, layer_count=0), [])
        assert lan.send_file(str(bad)) is False and "11520" in warned[-1]

        # a pause the app did not request, at layer 0, is the printer's own check (empty vat)
        from elegoo_lan.client import PrinterState
        lan._we_paused = False
        lan._status(PrinterState(status={"CurrentStatus": [1], "PrintInfo": {"Status": 6, "CurrentLayer": 0,
                                                                            "TotalLayer": 200}}))
        assert lan.state_label.text().startswith("Paused by printer")
        lan._status(PrinterState(status={"CurrentStatus": [1], "PrintInfo": {"Status": 6, "CurrentLayer": 7,
                                                                            "TotalLayer": 200}}))
        assert lan.state_label.text() == "Printing — Paused"

        # known printers persist
        assert len(json.loads(s.value("lan/printers"))) == 2
        w.close()
        assert lan.session is None
    finally:
        sim.close()
        old.close()


def test_hollow_drill_and_trapped_resin(app, tmp_path, monkeypatch):
    import numpy as np
    import trimesh
    from PySide6.QtWidgets import QMessageBox

    from gooslicer.support_defaults import HollowSettings
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer_qt.hollowing import DrainHoleDialog, HollowFields
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    # dialogs start at SatelLite's defaults
    assert HollowFields(s).value() == HollowSettings()
    hs, direction = DrainHoleDialog(s).value()
    assert (hs.radius1, hs.radius2, hs.extend1, hs.extend2, direction) == (2, 2, 2, 2, "normal")

    stl = tmp_path / "cube.stl"
    trimesh.creation.box((20, 20, 20)).export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    part = w.scene.parts[0]
    part.scale = np.array([1.0, 1.0, 0.5])        # hollow must work in true millimetres
    w.viewport.set_selection([part.id])
    assert w.trapped_resin() == []
    done = []
    w.hollowFinished.connect(done.append)
    # Hollow opens its settings over the 3D view's top-left and stays until Done or Esc
    w.viewport.set_selection([])
    app.processEvents()
    assert w.job_panel.hollow_btn.isEnabled()                  # opens the panel; no selection needed
    w.job_panel.hollow_btn.click()
    hp = w.hollow_panel
    app.processEvents()
    assert (not hp.isHidden()) and hp.parentWidget() is w.viewport and hp.pos().x() == 12
    assert not hp.hollow_btn.isEnabled()                       # nothing selected yet
    w.select([part.id])
    app.processEvents()
    assert hp.hollow_btn.isEnabled() and hp.hollow_btn.text() == "Hollow"
    hp.fields.set_value(HollowSettings(thickness=1.5, accuracy=0.5))
    assert not part.see_through and hp.fields.opacity.value() == 85     # 85 % by default
    hp.hollow_btn.click()
    assert (not hp.status.isHidden()) and "Hollowing 1 part" in hp.status.text()
    assert _wait(app, lambda: done, 60) and done == [True]
    app.processEvents()
    assert (not hp.isHidden()) and not (not hp.status.isHidden())       # still open for the next part
    assert part.opacity == 0.85                                 # hollowed parts are drawn see-through
    w.undo.undo()
    assert part.opacity == 1.0
    w.undo.redo()
    assert part.opacity == 0.85
    # the right-click menu and View → See-through switch it; undoable
    sm = next(a for a in w.part_menu(part.id).actions() if a.text() == "See-through")
    assert sm.isCheckable() and sm.isChecked()
    w.toggle_see_through()
    assert part.opacity == 1.0
    hp.fields.opacity.setValue(30)                             # saved at once; used from now on
    assert int(s.value("hollow/opacity")) == 30
    w.toggle_see_through()
    assert part.opacity == 0.3
    w.undo.undo()
    w.undo.undo()
    assert part.opacity == 0.85
    # opening the drain-hole tool closes the Hollow panel; Esc closes it too
    w.drain_hole_mode()
    assert not (not hp.isHidden()) and (not w.hole_panel.isHidden())
    w.viewport.stop_hole_mode()
    w.hollow_dialog()
    assert (not hp.isHidden())
    from PySide6.QtTest import QTest
    QTest.keyClick(w.viewport, QtCore.Qt.Key_Escape)
    assert not (not hp.isHidden())
    wm = part.world_mesh()
    assert wm.is_watertight
    assert wm.volume == pytest.approx(20 * 20 * 10 - 17 * 17 * 7, rel=0.02)
    cav = w.trapped_resin()
    assert len(cav) == 1

    # the Hollowing pane (above Supports) checks too, and shows the cavity in red with a guide
    jp = w.job_panel
    lay = jp.layout()
    assert [lay.itemAt(i).widget().title() for i in range(lay.count())] == ["Hollowing", "Supports", "Slicing"]
    assert jp.hollow_btn.isEnabled() and jp.drain_btn.isEnabled() and jp.trapped_btn.isEnabled()
    jp.trapped_btn.click()
    assert _wait(app, lambda: jp.trapped_btn.isEnabled(), 60)
    assert "1 sealed cavity" in jp.trapped_result.text() and "red" in jp.trapped_result.text()
    red = w.viewport.cavity_meshes
    assert len(red) == 1 and len(red[0].faces) >= 12
    lo, hi = wm.bounds
    assert (red[0].bounds[0] > lo + 1).all() and (red[0].bounds[1] < hi - 1).all()   # the inner wall only
    g = w.viewport.cavity_lines
    assert g.shape == (2, 3) and np.isclose(g[0, 2], g[1, 2])
    assert [lab.text for lab in w.viewport.guide_labels()] == ["Sealed cavity 1"]
    w.scene_changed()                                   # supports or nothing changed: still shown
    assert w.viewport.cavity_meshes

    # Seal cavity fills it in (one undo step brings the hollow back)
    hollow_mesh = part.mesh
    jp.seal_btn.click()
    assert _wait(app, lambda: jp.seal_btn.isEnabled() and "Seal" in jp.trapped_result.text(), 60)
    assert "Sealed 1 cavity" in jp.trapped_result.text()
    assert w.trapped_resin() == [] and w.viewport.cavity_meshes == []
    assert part.world_mesh().volume == pytest.approx(4000, rel=1e-6) and part.world_mesh().is_watertight
    assert w.undo.undoText() == "Seal cavities"
    w.undo.undo()
    assert part.mesh is hollow_mesh and len(w.trapped_resin()) == 1
    wm = part.world_mesh()

    asked = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    assert not w._confirm_no_trapped_resin() and "sealed cavit" in asked[0]

    # drill from below at the cavity's lowest point
    c = wm.bounds.mean(axis=0)
    hit_part, face, hit = w.scene.pick(np.array([c[0], c[1], wm.bounds[0][2] - 20]), np.array([0.0, 0, 1]))
    assert hit_part is part
    w._hole_settings = DrainHoleDialog(s).value()
    w._hole_picked(part.id, face, hit)
    assert _wait(app, lambda: not w.drilling() and w.undo.undoText() == "Drain hole", 60)
    assert part.world_mesh().is_watertight and w.trapped_resin() == []
    assert w.viewport.cavity_meshes == [] and len(w.viewport.cavity_lines) == 0   # the part changed
    assert "changed since the trapped-resin check" in jp.trapped_result.text()
    jp.trapped_btn.click()
    assert _wait(app, lambda: jp.trapped_btn.isEnabled(), 60)
    assert "No sealed cavities" in jp.trapped_result.text()
    assert w._confirm_no_trapped_resin()
    w.undo.undo()
    assert len(w.trapped_resin()) == 1
    w.undo.undo()
    assert w.trapped_resin() == [] and part.world_mesh().volume == pytest.approx(4000, rel=1e-6)
    w.close()


def test_supports_auto_edit_slice(app, tmp_path, monkeypatch):
    import numpy as np
    import trimesh

    from elegoo_goo import goo
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer import supports as S
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))

    # a "T": a 12x4 mm slab on a 4x4 mm post -> overhangs on both sides
    t = trimesh.boolean.union([trimesh.creation.box((4, 4, 6)),
                               trimesh.creation.box((12, 4, 2), trimesh.transformations.translation_matrix((0, 0, 4)))],
                              engine="manifold")
    stl = tmp_path / "t.stl"
    t.export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    part = w.scene.parts[0]
    w.viewport.set_selection([part.id])
    assert len(w.unsupported_islands()) == 0          # resting on the plate: no islands
    done = []
    w.supportsFinished.connect(done.append)
    w.auto_support(part, S.SupportParams())
    assert _wait(app, lambda: done, 120) and done == [0]
    assert part.supports is not None and part.elevation == 5.0
    assert part.bounds()[0][2] == pytest.approx(5.0)
    assert w.unsupported_islands() == []
    sm = w.supports.world_mesh(part)
    assert sm is not None and sm.is_watertight and sm.bounds[0][2] == pytest.approx(0.0, abs=0.02)
    n0 = len(part.supports)

    # moving in XY keeps the geometry (offset only), no rebuild
    e0 = w.supports.entry(part)
    part.position[:2] += (10, 5)
    w.scene_changed()
    assert w.supports.entry(part) is e0 and not w.supports.busy()
    assert w.supports.world_mesh(part).bounds[0][0] == pytest.approx(sm.bounds[0][0] + 10)

    # add one by clicking under the slab's far end, then remove it with Ctrl+click
    b = part.bounds()
    o = np.array([b[1][0] - 0.5, b[0][1] + 2, 1.0])
    w._support_click(o, np.array([0.0, 0, 1]), False)
    assert len(part.supports) == n0 + 1
    assert _wait(app, lambda: w.supports.entry(part) is not None, 60)
    w.undo.undo()
    assert len(part.supports) == n0
    # drag a support: its contact moves to the drop point on the same part
    assert _wait(app, lambda: w.supports.entry(part) is not None, 60)
    cs = part.supports.world_contacts(part)
    c0 = min(cs, key=lambda c: c.point[0])
    up = np.array([0.0, 0, 1])
    drop = np.array([b[1][0] - 1.0, b[0][1] + 1.0, 1.0])
    w._support_drag(np.array([c0.point[0], c0.point[1], 1.0]), up, drop, up)
    after = part.supports.world_contacts(part)
    assert len(after) == n0
    assert not any(np.allclose(c.point, c0.point) for c in after)
    assert any(np.allclose(c.point[:2], drop[:2], atol=1e-6) and c.kind == "manual" for c in after)
    w._support_drag(drop - [30, 30, 0], up, drop, up)       # not on a support: nothing happens
    assert len(part.supports.world_contacts(part)) == n0
    w.undo.undo()
    assert any(np.allclose(c.point, c0.point) for c in part.supports.world_contacts(part))
    # slicing includes supports and raft: first layer is the raft
    job = w.make_slice_job()
    assert job.height == pytest.approx(part.bounds()[1][2], abs=0.01)
    out = tmp_path / "t.goo"
    w.start_slice(job, str(out))
    assert _wait(app, lambda: w._slice_thread is None, 180)
    g = goo.read(out)
    assert g.header.layer_count == job.layer_count
    # clear supports: back on the plate
    w.clear_supports()
    assert part.supports is None and part.bounds()[0][2] == pytest.approx(0.0)
    w.undo.undo()
    assert part.supports is not None and part.bounds()[0][2] == pytest.approx(5.0)
    w.close()


def test_slice_warns_about_unsupported_islands(app, tmp_path, monkeypatch):
    """Slicing checks for islands at the print layer height in the background, warns, and
    stops at the warning when the user says No."""
    import trimesh
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtWidgets import QFileDialog, QMessageBox

    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    stl = tmp_path / "box.stl"
    trimesh.creation.box((6, 6, 2)).export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    part = w.scene.parts[0]

    warned, saved = [], []
    answer = [QMessageBox.No]
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]) or answer[0])
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: saved.append(1) or ("", ""))

    # resting on the plate: no warning, straight to the save dialog
    w.slice_dialog()
    assert _wait(app, lambda: saved) and warned == []
    assert w.slice_action.isEnabled()

    # floating 3 mm up with no supports: warned; "No" stops before the save dialog
    part.position[2] += 3
    w.scene_changed()
    saved.clear()
    w.slice_dialog()
    assert not w.slice_action.isEnabled()          # disabled while the check runs
    assert _wait(app, lambda: warned)
    assert "1 unsupported island" in warned[0] and "z 3.0" in warned[0]
    assert saved == [] and w.slice_action.isEnabled()

    # "Yes" slices anyway
    answer[0] = QMessageBox.Yes
    w.slice_dialog()
    assert _wait(app, lambda: saved) and len(warned) == 2
    w.close()


def test_tree_supports_from_dialog(app, tmp_path, monkeypatch):
    import trimesh
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer import supports as S
    from gooslicer_qt.main import MainWindow
    from gooslicer_qt.supporting import BUILTIN_TREE, SupportDialog
    dlg = SupportDialog([], 10.0, True)
    assert dlg.combo.itemText(1) == BUILTIN_TREE
    dlg.combo.setCurrentIndex(1)
    params, name = dlg.params()
    assert params == S.SupportParams.tree_defaults() and name == "" and "Tree" in dlg.note.text()

    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    t = trimesh.boolean.union([trimesh.creation.box((4, 4, 6)),
                               trimesh.creation.box((12, 4, 2), trimesh.transformations.translation_matrix((0, 0, 4)))],
                              engine="manifold")
    stl = tmp_path / "t.stl"
    t.export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    part = w.scene.parts[0]
    done = []
    w.supportsFinished.connect(done.append)
    w.auto_support(part, params)
    assert _wait(app, lambda: done, 120) and done == [0]
    assert part.supports.params.style == "tree" and "trunks" in w.log.toPlainText()
    sm = w.supports.world_mesh(part)
    assert sm is not None and sm.is_watertight and w.unsupported_islands() == []
    w.close()


def test_theme_switching(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtGui import QPalette
    from gooslicer_qt import theme
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    monkeypatch.setattr(theme, "system_is_dark", lambda a: False)
    w = MainWindow(settings=s)
    assert set(w.theme_actions) == {"system", "light", "dark"} and w.theme_actions["system"].isChecked()
    assert app.style().name().lower() == "fusion"
    light_bg = app.palette().color(QPalette.Window).lightness()

    w.theme_actions["dark"].trigger()
    assert w.theme_actions["dark"].isChecked() and s.value("theme") == "dark"
    assert app.palette().color(QPalette.Window).lightness() < 80 < light_bg
    assert app.palette().color(QPalette.Text).lightness() > 200
    assert theme.WARNING["dark"] in w.lan.warn_label.styleSheet()
    w.close()

    # remembered: a new window starts dark
    w = MainWindow(settings=s)
    assert w.theme.current == "dark" and w.theme_actions["dark"].isChecked()

    # "system" follows the desktop, live
    w.set_theme("system")
    assert w.theme.current == "light" and theme.WARNING["light"] in w.lan.warn_label.styleSheet()
    monkeypatch.setattr(theme, "system_is_dark", lambda a: True)
    w.theme._system_changed(None)
    assert w.theme.current == "dark" and app.palette().color(QPalette.Window).lightness() < 80
    # an explicit choice ignores desktop changes
    w.set_theme("light")
    w.theme._system_changed(None)
    assert w.theme.current == "light"
    w.close()


def test_parts_import_button(app, tmp_path, monkeypatch):
    import trimesh
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtWidgets import QFileDialog
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    stl = tmp_path / "cube.stl"
    trimesh.creation.box((5, 5, 5)).export(stl)
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(stl)], ""))
    assert w.docks["Models"].isAncestorOf(w.import_btn) and w.docks["Models"].isAncestorOf(w.parts)
    w.import_btn.click()
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    assert w.parts.count() == 1 and w.parts.item(0).text().startswith("cube")
    w.close()


def test_resin_favourites(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from gooslicer import settings as S
    from gooslicer_qt.slicing import PrintSettingsPanel
    P16 = "ELEGOO Saturn 4 Ultra 16K"
    resins = [S.ResinProfile(str(i), n, P16, "", {}) for i, n in enumerate("ABCD")]
    k = {n: f"{P16}|{n}" for n in "ABCD"}
    ps = PrintSettingsPanel()
    ps.resize(400, 400)
    ps.show()
    ps.set_sources(S.PrintSettings(), resins, current="B")
    c = ps.resin

    def names():
        return [c.itemText(i) or "---" for i in range(c.count())]
    assert names() == ["Printer default", "A", "B", "C", "D"] and ps.current_resin().name == "B"
    assert c.itemIcon(3).cacheKey() == ps._star_off.cacheKey()

    # click the star of "C" in the open list
    c.showPopup()
    _wait(app, lambda: c.view().isVisible(), 5)
    view = c.view()
    row = view.visualRect(c.model().index(3, 0))
    star = QPoint(row.left() + 6, row.center().y())
    changed = []
    ps.favoritesChanged.connect(changed.append)
    for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
        btns = Qt.LeftButton if kind == QEvent.MouseButtonPress else Qt.NoButton
        QApplication.sendEvent(view.viewport(), QMouseEvent(kind, QPointF(star), view.viewport().mapToGlobal(QPointF(star)),
                                                            Qt.LeftButton, btns, Qt.NoModifier))
    assert changed == [[k["C"]]] and ps.is_favorite(k["C"])
    assert names() == ["Printer default", "C", "---", "A", "B", "D"]
    assert c.itemIcon(1).cacheKey() == ps._star_on.cacheKey()
    assert ps.current_resin().name == "B"                  # selection kept, nothing re-selected
    assert view.isVisible()                                # list stays open
    # the name area is left for normal selection
    name_pt = QPoint(row.left() + 60, view.visualRect(c.model().index(4, 0)).center().y())
    assert ps._star_clicks.star_hit(name_pt) is None
    c.hidePopup()

    ps.toggle_favorite(k["A"])
    assert names() == ["Printer default", "A", "C", "---", "B", "D"]   # favourites keep profile order
    ps.toggle_favorite(k["A"])
    ps.toggle_favorite(k["C"])
    assert names() == ["Printer default", "A", "B", "C", "D"]          # no divider without favourites
    ps.close()

    # saved and restored by the main window
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    w.print_settings.toggle_favorite(f"{P16}|Space Grey")
    w.close()
    w = MainWindow(settings=s)
    assert w.print_settings.favorites == {f"{P16}|Space Grey"}
    w.print_settings.toggle_favorite("ELEGOO Saturn 4|Clear")
    w.close()
    assert MainWindow(settings=s).print_settings.favorites == {f"{P16}|Space Grey", "ELEGOO Saturn 4|Clear"}

    # same name on another printer is not a favourite
    ps = PrintSettingsPanel()
    ps.set_favorites([k["B"]])
    ps.set_sources(S.PrintSettings(), [S.ResinProfile("9", "B", "ELEGOO Saturn 4", "", {})] + resins)
    assert [ps.resin.itemText(i) for i in range(ps.resin.count())][:3] == ["Printer default", "B", ""]
    assert ps.resin.itemData(1) == 2 and ps.resin.itemIcon(5).cacheKey() == ps._star_off.cacheKey()  # S4's "B" unstarred
    ps.close()

    # favourites saved by older versions (bare names) stay with the printer they were starred on
    old = QtCore.QSettings(str(tmp_path / "old.ini"), QtCore.QSettings.IniFormat)
    old.setValue("printer", P16)
    old.setValue("resinFavorites", "Space Grey 8K ABS-Like 3.0")
    w = MainWindow(settings=old)
    assert w.print_settings.favorites == {f"{P16}|Space Grey 8K ABS-Like 3.0"}
    assert old.value("resinFavorites") == [f"{P16}|Space Grey 8K ABS-Like 3.0"]
    w.close()


def test_remembers_printer_and_resin(app, tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    res = tmp_path / "install" / "resources"
    ini = res / "machines" / "printer" / "officialPlatform.ini"
    ini.parent.mkdir(parents=True)
    ini.write_bytes(FIXTURE.encode())
    mat = res / "resin" / "material"
    mat.mkdir(parents=True)
    for i, name in enumerate(("Alpha", "Beta", "Gamma")):
        ap = {"vd_resine_profilename": name,
              "cloudResinExtraParam": {"cloudResinPrinterName": "ELEGOO Saturn 4 Ultra 16K"},
              "vd_printer_printer_para": {"vd_printer_printer_para_exposure_time": 2.0 + i}}
        (mat / f"{i}.data").write_text(json.dumps({"platitem": {"vd_printer": {"vd_active_printer": ap}}}))
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    assert w.import_profiles(str(ini))
    ps = w.print_settings
    assert ps.current_resin() is None                       # first run: Printer default
    ps.resin.setCurrentIndex(ps.resin.findText("Beta"))     # the user's pick is saved right away
    assert s.value("printer") == "ELEGOO Saturn 4 Ultra 16K" and s.value("lastResin") == "Beta"
    ps.toggle_favorite("ELEGOO Saturn 4 Ultra 16K|Gamma")   # re-sorting the list is not a pick
    assert s.value("lastResin") == "Beta" and ps.current_resin().name == "Beta"
    w.close()

    w = MainWindow(settings=s)                              # reopened: same printer and resin
    assert w.printer_panel.current().name == "ELEGOO Saturn 4 Ultra 16K"
    assert w.print_settings.current_resin().name == "Beta"
    assert w.print_settings.settings().exposure == pytest.approx(3.0)
    w.print_settings.resin.setCurrentIndex(0)              # Printer default is remembered too
    w.close()
    w = MainWindow(settings=s)
    assert w.print_settings.current_resin() is None
    # a printer with no pick of its own starts on the last resin picked on any printer
    s.setValue("lastResin", "Gamma")
    assert w._last_resin("ELEGOO Saturn 4") == "Gamma" and w._last_resin("ELEGOO Saturn 4 Ultra 16K") == ""
    w.close()


def test_advanced_settings_collapsed(app):
    from gooslicer_qt.slicing import PrintSettingsPanel
    ps = PrintSettingsPanel()
    ps.show()
    app.processEvents()
    fields = list(ps.widgets.values()) + [ps.slice_pos]
    assert len(fields) == 10 and all(ps.advanced.isAncestorOf(f) for f in fields)
    assert not ps.advanced_btn.isChecked() and not any(f.isVisible() for f in fields)
    assert ps.resin.isVisible()
    # directly below the resin profile
    assert ps.layout().indexOf(ps.advanced_btn) == 1 and ps.layout().itemAt(0).layout().rowCount() == 1
    ps.widgets["exposure"].setValue(3.3)                   # hidden values still count
    assert ps.settings().exposure == 3.3
    ps.advanced_btn.click()
    app.processEvents()
    assert ps.advanced_btn.isChecked() and all(f.isVisible() for f in fields)
    assert ps.advanced_btn.arrowType() == QtCore.Qt.DownArrow
    ps.advanced_btn.click()
    assert not ps.advanced.isVisible() and ps.advanced_btn.arrowType() == QtCore.Qt.RightArrow
    ps.close()


def test_printer_row_and_details_popup(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtWidgets import QFormLayout
    from gooslicer_qt.main import MainWindow
    from gooslicer_qt.panels import InfoButton
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    assert "Printer" not in w.docks and "Printer settings" in w.docks           # one merged tab
    pp, ps = w.printer_panel, w.print_settings
    assert w.docks["Printer settings"].isAncestorOf(ps) and ps.isAncestorOf(pp)
    top = ps._top
    assert top.itemAt(0, QFormLayout.LabelRole).widget().text() == "Printer"
    assert top.itemAt(0, QFormLayout.FieldRole).widget() is pp
    assert top.itemAt(1, QFormLayout.FieldRole).widget() is ps.resin            # printer directly above resin
    assert isinstance(pp.info_btn, InfoButton) and not pp.info_btn.isEnabled()  # no printer yet
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    w.show()
    app.processEvents()
    assert pp.info_btn.isEnabled()
    lay = pp.layout()
    assert lay.itemAt(0).widget() is pp.combo and lay.itemAt(1).widget() is pp.info_btn   # (i) right of the dropdown
    assert not pp.details.isVisible() and not any(l.isVisible() for l in pp.info.values())
    pp.info_btn.click()
    assert _wait(app, lambda: pp.details.isVisible(), 5)
    assert pp.details_title.text() == "ELEGOO Saturn 4 Ultra 16K"
    assert pp.info["Pixel pitch"].text() == "14.0 × 19.0 µm" and pp.info["Resolution"].text() == "15120 × 6230 px"
    assert set(pp.info) == {"Build volume", "Resolution", "Pixel pitch", "Mirror"}
    btn = pp.info_btn.mapToGlobal(pp.info_btn.rect().bottomRight())
    g = pp.details.frameGeometry()
    assert g.top() >= btn.y() - 2 or g.bottom() <= btn.y() - pp.info_btn.height()   # below (or above if no room)
    pp.details.hide()
    w.close()


def test_import_resin_profile(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtCore import QMimeData, QPointF, Qt, QUrl
    from PySide6.QtGui import QDragEnterEvent, QDropEvent
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from gooslicer_qt.main import MainWindow, data_dir
    from test_satres import satres
    P16 = "ELEGOO Saturn 4 Ultra 16K"
    one = satres(tmp_path / "one.satres", [("Siraya Blu", P16, {"print/exposuretime": 1.6, "resin/type": "Normal"})])
    many = satres(tmp_path / "many.satres", [(f"Maker {c}", P16, {}) for c in "AB"]
                  + [("Other printer", "ELEGOO Saturn 4", {})])
    elsewhere = satres(tmp_path / "s4.satres", [("S4 only", "ELEGOO Saturn 4", {})])
    bad = tmp_path / "bad.satres"
    bad.write_bytes(b"nope")
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    ps = w.print_settings
    file_menu = w.menuBar().actions()[0].menu()
    act = next(a for a in file_menu.actions() if a.text().replace("&", "") == "Import resin profile…")

    # File → Import resin profile…: one profile for this printer -> listed and selected
    monkeypatch.setattr(QFileDialog, "getOpenFileNames", lambda *a, **k: ([str(one)], ""))
    act.trigger()
    assert ps.current_resin().name == "Siraya Blu" and ps.settings().exposure == 1.6
    assert s.value("lastResin") == "Siraya Blu"                                  # remembered like a pick
    assert len(list((data_dir() / "resins").glob("*.satres"))) == 1

    def drop(target, *paths):
        md = QMimeData()
        md.setUrls([QUrl.fromLocalFile(str(p)) for p in paths])
        enter = QDragEnterEvent(QPointF(5, 5), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(target, enter)
        assert enter.isAccepted()
        ev = QDropEvent(QPointF(5, 5), Qt.CopyAction, md, Qt.LeftButton, Qt.NoModifier)
        QApplication.sendEvent(target, ev)
        return ev.isAccepted()

    # dropped on the Print settings dock: several profiles are added, the selection stays
    assert drop(w.docks["Printer settings"], many)
    names = [r.name for r in ps.resins]
    assert {"Siraya Blu", "Maker A", "Maker B"} <= set(names) and "Other printer" not in names
    assert ps.current_resin().name == "Siraya Blu"
    # dropped on the main window (and the 3D view): profiles for another printer wait there
    assert drop(w, elsewhere)
    assert "for ELEGOO Saturn 4" in w.statusBar().currentMessage() and "S4 only" not in [r.name for r in ps.resins]
    # a broken file is reported, nothing else changes
    w.viewport.filesDropped.emit([str(bad)])
    assert warned and "not a SatelLite resin profile" in warned[-1]
    assert len(list((data_dir() / "resins").glob("*.satres"))) == 3
    w.close()

    # still there after a restart, and the selection is remembered
    w = MainWindow(settings=s)
    assert w.print_settings.current_resin().name == "Siraya Blu"
    assert {"Maker A", "Maker B"} <= {r.name for r in w.print_settings.resins}
    w.close()


def test_restore_windows_to_default(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtCore import Qt
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    areas = {t: w.dockWidgetArea(d) for t, d in w.docks.items()}
    assert areas["Models"] == Qt.RightDockWidgetArea
    w.docks["Models"].close()                             # closed, as the user did
    w.docks["Printer settings"].setFloating(True)
    w.removeDockWidget(w.docks["Printer settings"])             # gone from the layout entirely
    w.close()                                                 # the broken layout is saved

    w = MainWindow(settings=s)
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    assert not w.docks["Models"].isVisible()              # stays closed after a restart
    view = next(a.menu() for a in w.menuBar().actions() if a.text() == "&View")
    act = next(a for a in view.actions() if a.text().replace("&", "") == "Restore all windows to default")
    act.trigger()
    app.processEvents()
    assert all(d.isVisible() or w.tabifiedDockWidgets(d) for d in w.docks.values())
    assert not any(d.isFloating() for d in w.docks.values())
    assert {t: w.dockWidgetArea(d) for t, d in w.docks.items()} == areas
    assert w.docks["Printer settings"].isVisible()
    assert set(w.tabifiedDockWidgets(w.docks["Printer settings"])) == {w.docks["Models"], w.docks["Flaws"]}
    w.close()
    w = MainWindow(settings=s)                                # and the restored layout is kept
    w.show()
    app.processEvents()
    assert set(w.tabifiedDockWidgets(w.docks["Printer settings"])) == {w.docks["Models"], w.docks["Flaws"]}
    w.close()


def test_manage_resins(app, tmp_path, monkeypatch):
    import json
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox
    from gooslicer import settings as S
    from gooslicer_qt.main import MainWindow
    from test_satres import satres
    P16 = "ELEGOO Saturn 4 Ultra 16K"
    res = tmp_path / "install" / "resources"
    ini = res / "machines" / "printer" / "officialPlatform.ini"
    ini.parent.mkdir(parents=True)
    ini.write_bytes(FIXTURE.encode())
    mat = res / "resin" / "material"
    mat.mkdir(parents=True)
    for i, (name, printer) in enumerate((("Alpha", P16), ("Beta", P16), ("Mars only", "ELEGOO Mars 4 Ultra"))):
        ap = {"vd_resine_profilename": name, "vd_resine_company": "ELEGOO",
              "cloudResinExtraParam": {"cloudResinPrinterName": printer},
              "vd_printer_printer_para": {"vd_printer_printer_para_exposure_time": 2.0 + i},
              "vd_printer_resin_para": {"vd_printer_resin_para_type": "Standard"}}
        (mat / f"{i}.data").write_text(json.dumps({"platitem": {"vd_printer": {"vd_active_printer": ap}}}))
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    assert w.import_profiles(str(ini))
    w.import_resin_profiles([str(satres(tmp_path / "pack.satres", [("Siraya", P16, {"print/exposuretime": 1.6}),
                                                                    ("Other", P16, {})]))])
    w.resize(1100, 750)
    w.show()
    app.processEvents()
    ps = w.print_settings

    def listed():
        return [r.name for r in ps.resins]

    # Resin menu sits between Printer and View
    titles = [a.text() for a in w.menuBar().actions()]
    assert titles.index("&Resin") == titles.index("&Printer") + 1 == titles.index("&View") - 1
    resin_menu = next(a.menu() for a in w.menuBar().actions() if a.text() == "&Resin")
    act = next(a for a in resin_menu.actions() if a.text().replace("&", "") == "Manage resins…")
    act.trigger()
    dlg = w._resin_manager
    assert _wait(app, dlg.isVisible, 5) and dlg.size() == w.size()
    names = {dlg.tree.topLevelItem(i).text(0) for i in range(dlg.tree.topLevelItemCount())}
    assert names == {"Alpha", "Beta", "Siraya", "Other"}               # not other printers' profiles

    def pick(name):
        it = next(dlg.tree.topLevelItem(i) for i in range(dlg.tree.topLevelItemCount())
                  if dlg.tree.topLevelItem(i).text(0) == name)
        dlg.tree.setCurrentItem(it)
        app.processEvents()
        return dlg.current

    # selecting shows the properties on the right, read-only for SatelLite's and imported ones
    r = pick("Beta")
    assert dlg.title.text() == "Beta" and dlg.fields["exposure"].value() == 3.0
    assert dlg.meta["Source"].text() == "ELEGOO (SatelLite)" and dlg.fields["exposure"].isReadOnly()
    assert pick("Siraya").source == "imported" and dlg.fields["exposure"].value() == 1.6
    dlg.search.setText("sir")
    assert dlg.tree.topLevelItemCount() == 1
    dlg.search.clear()

    # Copy… asks for a name, refuses one that exists (any case), then makes an editable copy
    pick("Beta")
    answers = iter([("alpha", True), ("Beta custom", True)])
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: next(answers))
    warned = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: warned.append(a[2]))
    new = dlg.copy_current()
    assert len(warned) == 1 and "already exists" in warned[0]
    assert new.name == "Beta custom" and dlg.current.key == new.key and not dlg.fields["exposure"].isReadOnly()
    assert "Beta custom" in listed()
    dlg.fields["exposure"].setValue(4.5)                                # edits are saved
    dlg.save_edits()
    ps.resin.setCurrentIndex(ps.resin.findText("Beta custom", Qt.MatchStartsWith))
    assert ps.settings().exposure == 4.5
    monkeypatch.setattr(QInputDialog, "getText", lambda *a, **k: ("", False))
    assert dlg.copy_current() is None                                   # cancelled

    # hide: gone from the Print settings list, still in the manager (marked)
    pick("Alpha")
    dlg.hide_chk.setChecked(True)
    assert "Alpha" not in listed() and dlg.tree.currentItem().text(4) == "Hidden"
    dlg.hide_chk.setChecked(False)
    assert "Alpha" in listed()

    # export as .satres
    out = tmp_path / "exported"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    pick("Beta custom")
    assert dlg.export_current() == str(out) + ".satres"
    (back,) = S.load_satres(str(out) + ".satres")
    assert (back.name, back.printer, back.settings().exposure) == ("Beta custom", P16, 4.5)

    # delete: a copy's file goes; SatelLite's own is only dropped from GooSlicer's lists
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.Yes)
    path = Path(dlg.current.path)
    assert dlg.delete_current() and not path.exists() and "Beta custom" not in listed()
    pick("Alpha")
    assert dlg.delete_current()
    assert "Alpha" not in listed() and (mat / "0.data").exists()
    pick("Other")
    assert dlg.delete_current() and "Other" not in listed() and "Siraya" in listed()
    names = {dlg.tree.topLevelItem(i).text(0) for i in range(dlg.tree.topLevelItemCount())}
    assert names == {"Beta", "Siraya"}
    monkeypatch.setattr(QMessageBox, "question", lambda *a, **k: QMessageBox.No)
    pick("Beta")
    assert not dlg.delete_current() and "Beta" in listed()               # declined
    dlg.close()
    w.close()

    w = MainWindow(settings=s)                                          # all of it sticks
    assert [r.name for r in w.print_settings.resins] == ["Beta", "Siraya"]
    w.close()


def test_manage_resins_favourites(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtCore import QEvent, QPoint, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from gooslicer_qt.main import MainWindow
    from gooslicer_qt.resins import DIVIDER, GROUP_ROLE
    from test_satres import satres
    P16 = "ELEGOO Saturn 4 Ultra 16K"
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    w.import_resin_profiles([str(satres(tmp_path / "p.satres", [(n, P16, {}) for n in ("Alpha", "Beta", "Gamma", "Delta")]
                                        + [("Delta", "ELEGOO Saturn 4", {})]))])
    w.print_settings.toggle_favorite(f"{P16}|Gamma")                   # starred in Print settings first
    w.resize(1100, 750)
    w.show()
    dlg = w.manage_resins()
    assert _wait(app, dlg.isVisible, 5)
    tree = dlg.tree

    def rows():
        out = []
        for i in range(tree.topLevelItemCount()):
            it = tree.topLevelItem(i)
            out.append("---" if it.data(0, GROUP_ROLE) == DIVIDER else it.text(0))
        return out

    def item(name, printer=P16):
        return next(tree.topLevelItem(i) for i in range(tree.topLevelItemCount())
                    if tree.topLevelItem(i).text(0) == name and tree.topLevelItem(i).text(1) == printer)

    def icon_of(name, printer=P16):
        return item(name, printer).icon(0).cacheKey()

    assert rows() == ["Gamma", "---", "Alpha", "Beta", "Delta", "Delta"]   # same favourites as Print settings
    assert icon_of("Gamma") == dlg._star_on.cacheKey() and icon_of("Beta") == dlg._star_off.cacheKey()
    assert not tree.topLevelItem(1).flags() & Qt.ItemIsSelectable    # the divider can't be picked
    sel = dlg.current.key

    # click the star left of "Delta"
    it = item("Delta")
    rect = tree.visualItemRect(it)
    star = QPoint(rect.left() + 6, rect.center().y())
    vp = tree.viewport()
    for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
        btns = Qt.LeftButton if kind == QEvent.MouseButtonPress else Qt.NoButton
        QApplication.sendEvent(vp, QMouseEvent(kind, QPointF(star), vp.mapToGlobal(QPointF(star)),
                                               Qt.LeftButton, btns, Qt.NoModifier))
    assert rows() == ["Delta", "Gamma", "---", "Alpha", "Beta", "Delta"]
    assert icon_of("Delta", "ELEGOO Saturn 4") == dlg._star_off.cacheKey()     # only this printer's Delta
    assert dlg.current.key == sel                                 # selection unchanged
    assert w.print_settings.favorites == {f"{P16}|Gamma", f"{P16}|Delta"}
    assert s.value("resinFavorites") == [f"{P16}|Delta", f"{P16}|Gamma"]
    names = [w.print_settings.resin.itemText(i) for i in range(w.print_settings.resin.count())]
    assert names[:4] == ["Printer default", "Delta", "Gamma", ""]   # the Print settings list follows
    name_pt = QPoint(rect.left() + 80, tree.visualItemRect(tree.topLevelItem(4)).center().y())
    assert dlg._star_clicks.star_hit(QPoint(6, tree.visualItemRect(tree.topLevelItem(2)).center().y())) is None  # divider
    assert dlg._star_clicks.star_hit(name_pt) is None             # the name itself still selects

    # sorting by a column keeps favourites on top, above the divider
    tree.sortByColumn(0, Qt.DescendingOrder)
    assert rows() == ["Gamma", "Delta", "---", "Delta", "Beta", "Alpha"]
    tree.sortByColumn(0, Qt.AscendingOrder)
    dlg.toggle_favorite(f"{P16}|Gamma")
    dlg.toggle_favorite(f"{P16}|Delta")
    assert rows() == ["Alpha", "Beta", "Delta", "Delta", "Gamma"] and not w.print_settings.favorites   # no divider
    dlg.toggle_favorite("ELEGOO Saturn 4|Delta")                  # the other printer's own favourite
    assert rows() == ["Delta", "---", "Alpha", "Beta", "Delta", "Gamma"] and icon_of("Delta", P16) == dlg._star_off.cacheKey()
    assert dlg.count.text().startswith("5 of")
    dlg.close()
    w.close()


def test_printer_control_in_print_settings(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtWidgets import QFormLayout
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    assert "Printer control" not in w.docks
    ps, lan = w.print_settings, w.lan
    assert w.docks["Printer settings"].isAncestorOf(lan) and ps.isAncestorOf(lan)
    # printer control comes first, then the Printer / Resin rows
    lay = ps.layout()
    assert lay.itemAt(0).widget() is lan
    top = ps._top
    assert top.itemAt(0, QFormLayout.LabelRole).widget().text() == "Printer"
    assert lan.mapTo(ps, lan.rect().bottomLeft()).y() < w.printer_panel.mapTo(ps, w.printer_panel.rect().topLeft()).y()
    assert lan.isVisible() and lan.find_btn.isVisible() and lan.files.isVisible()
    # it fits the column: nothing wider than the dock
    assert ps.minimumSizeHint().width() <= w.docks["Printer settings"].width() + 2
    # the Printer menu actions bring the tab forward
    w.docks["Models"].raise_()
    w.docks["Printer settings"].close()
    called = []
    w._show_lan(lambda: called.append(1))()                    # what the Printer menu items do
    app.processEvents()
    assert called and w.docks["Printer settings"].isVisible()
    w.close()


def test_right_hand_tabs(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtCore import QByteArray, Qt
    from PySide6.QtWidgets import QMainWindow, QTabBar
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    # a layout saved by an older version (Parts dock on the left) is not restored
    old = QMainWindow()
    s.setValue("windowState", old.saveState(1))
    w = MainWindow(settings=s)
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    assert "Parts" not in w.docks
    ps, pr = w.docks["Printer settings"], w.docks["Models"]
    assert w.dockWidgetArea(ps) == w.dockWidgetArea(pr) == Qt.RightDockWidgetArea
    assert set(w.tabifiedDockWidgets(ps)) == {pr, w.docks["Flaws"]}
    def right_bar():
        return next((b for b in w.findChildren(QTabBar) if b.isVisible()
                     and "Models" in [b.tabText(i) for i in range(b.count())]), None)
    assert _wait(app, right_bar, 5)
    bar = right_bar()
    assert [bar.tabText(i) for i in range(bar.count())] == ["Printer settings", "Models", "Flaws"]
    def on_screen(wid):                          # a dock behind another tab still reports isVisible()
        return wid.isVisible() and not wid.visibleRegion().isEmpty()
    assert bar.currentIndex() == 0 and on_screen(w.print_settings) and not on_screen(w.parts)
    # Models tab: the Parts box with Import model above the list, then the part's properties
    bar.setCurrentIndex(1)
    app.processEvents()
    assert on_screen(w.parts) and on_screen(w.import_btn) and on_screen(w.props) and not on_screen(w.print_settings)
    assert w.properties_split.widget(0) is w.parts_box and w.properties_split.widget(1).widget().isAncestorOf(w.props)
    assert w.import_btn.mapTo(w, w.import_btn.rect().bottomLeft()).y() < w.parts.mapTo(w, w.parts.rect().topLeft()).y()
    w.close()


def test_supports_and_slicing_sections(app, tmp_path, monkeypatch):
    import numpy as np
    import trimesh
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from PySide6.QtWidgets import QFileDialog, QMessageBox
    from elegoo_goo import goo
    from gooslicer_qt.main import MainWindow
    from gooslicer_qt.supporting import BUILTIN, BUILTIN_TREE
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    stl = tmp_path / "cube.stl"
    trimesh.creation.box((6, 6, 3)).export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    jp = w.job_panel
    # Supports and Slicing come right after Mesh in the Models tab
    box = w.props.parentWidget()
    assert box.layout().indexOf(w.props) == 0 and box.layout().indexOf(jp) == 1
    assert w.docks["Models"].isAncestorOf(jp)

    # ---- supports: the type shown is what Generate uses; a pick is remembered
    types = [jp.support_type.itemText(i) for i in range(jp.support_type.count())]
    assert types[:2] == [BUILTIN, BUILTIN_TREE]
    called = []
    def fake_auto_support(p, params, name):            # records the call; reports back later, as the real one
        called.append((p.name, params.style == "tree", name))
        QtCore.QTimer.singleShot(0, lambda: w.supportsFinished.emit(0))
    monkeypatch.setattr(w, "auto_support", fake_auto_support)
    w.select([])
    app.processEvents()
    assert jp.generate_btn.text() == "Generate supports" and "Supports go on the part on the plate." in jp.support_note.text()
    jp.support_type.setCurrentIndex(1)
    jp.generate_btn.click()
    assert called == [("cube", True, "")] and s.value("supportType") == BUILTIN_TREE
    w.select([w.scene.parts[0].id])
    app.processEvents()
    assert jp.generate_btn.toolTip() == "Generate supports for the selected part" and jp.support_type.currentIndex() == 1
    assert "Supports go on the selected part." in jp.support_note.text()
    jp.support_type.setCurrentIndex(0)
    jp.generate_btn.click()
    assert called[-1] == ("cube", False, "")
    # the Tips line shows the type's tip; saved tip overrides reach Generate (08 §11)
    assert jp.tips_label.text() == "Cone, 0.32 mm, break point 0.35 mm" and jp.tips_btn.isEnabled()
    s.setValue("supportTips", '{"tip_shape": "cube", "tip_ball": false}')
    jp.refresh()
    assert jp.tips_label.text() == "Cube, 0.32 mm (custom)" and jp.params()[0].tip_shape == "cube"
    s.remove("supportTips")
    jp.refresh()
    # Remove supports sits right of Generate; the island check below them, in Supports
    sup_box = jp.generate_btn.parentWidget()
    assert sup_box.title() == "Supports" and sup_box.isAncestorOf(jp.islands_btn)
    assert jp.remove_btn.parentWidget() is sup_box
    grid = next(sup_box.layout().itemAt(k).layout() for k in range(sup_box.layout().count())
                if sup_box.layout().itemAt(k).layout() and sup_box.layout().itemAt(k).layout().indexOf(jp.generate_btn) >= 0)
    pos = {b: grid.getItemPosition(grid.indexOf(b))[:2] for b in (jp.generate_btn, jp.remove_btn, jp.islands_btn, jp.fix_btn)}
    # Generate | Remove, then Detect islands | Fix islands below them
    assert pos == {jp.generate_btn: (0, 0), jp.remove_btn: (0, 1), jp.islands_btn: (1, 0), jp.fix_btn: (1, 1)}
    assert not jp.slice_btn.parentWidget().isAncestorOf(jp.islands_btn)
    assert not jp.remove_btn.isEnabled()                               # nothing to remove yet
    from gooslicer_qt.supporting import support_params
    got = []
    w.supportsFinished.connect(got.append)
    MainWindow.auto_support(w, w.scene.parts[0], *support_params(None))   # real supports this time
    assert _wait(app, lambda: w.scene.parts[0].supports is not None, 120)
    w.supportsFinished.disconnect()
    assert jp.remove_btn.isEnabled()
    jp.remove_btn.click()                                              # removes them (undoable)
    assert w.scene.parts[0].supports is None and not jp.remove_btn.isEnabled()
    w.undo.undo()
    assert w.scene.parts[0].supports is not None and jp.remove_btn.isEnabled()
    jp.remove_btn.click()
    assert w.scene.parts[0].supports is None

    # ---- unsupported islands: a part floating above the plate is one
    w.scene.parts[0].position[2] += 4
    w.scene_changed()
    jp.islands_btn.click()
    assert _wait(app, lambda: jp.islands_btn.isEnabled(), 60)
    assert "1 unsupported island" in jp.islands_result.text() and "red" in jp.islands_result.text()
    n_called = len(called)
    jp.fix_btn.click()                                  # a part without supports gets a full set
    assert _wait(app, lambda: jp.fix_btn.isEnabled() and len(called) > n_called, 60)
    assert called[-1][0] == "cube" and "Generating supports for cube" in jp.islands_result.text()
    assert "remains (shown in red)" in jp.islands_result.text()      # re-checked after (the fake adds none)
    jp.islands_btn.click()                              # show the red again for the checks below
    assert _wait(app, lambda: jp.islands_btn.isEnabled(), 60)
    # a white guide from the island out to the edge of the build area
    g = w.viewport.island_lines
    assert g.shape == (2, 3) and np.isclose(g[0, 2], g[1, 2])
    assert np.isclose(abs(g[1, 0]), w.scene.size[0] / 2) or np.isclose(abs(g[1, 1]), w.scene.size[1] / 2)
    # its unsupported surface (the cube's underside, 4 mm up) is shown in red
    red = w.viewport.island_meshes
    assert len(red) == 1 and len(red[0].faces) >= 2
    assert np.allclose(red[0].vertices[:, 2], 4.0, atol=1e-6) and (red[0].face_normals[:, 2] < 0).all()
    w.scene.parts[0].position[2] = 1.5
    w.scene_changed()                                                  # moving it clears the red
    assert w.viewport.island_meshes == [] and "changed since the island check" in jp.islands_result.text()
    assert len(w.viewport.island_lines) == 0
    jp.islands_btn.click()
    assert _wait(app, lambda: jp.islands_btn.isEnabled(), 60)
    assert "No unsupported islands" in jp.islands_result.text() and w.viewport.island_meshes == []

    # ---- slice into memory: progress, then the estimates
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    assert jp.results.isHidden() and not jp.export_btn.isEnabled() and not jp.send_btn.isEnabled()
    assert not hasattr(jp, "progress") and w._progress.isHidden()     # progress: status bar, only while slicing
    seen = []
    w._progress.valueChanged.connect(lambda v: seen.append((v, w._progress.isHidden(), w._progress.text())))
    jp.slice_btn.click()
    assert _wait(app, lambda: w.held is not None and not w.slicing(), 120)
    res = w.held[0]
    assert res.goo is not None and res.layers == 60 == len(res.goo.layers)
    assert seen and not any(hidden for v, hidden, _ in seen if v > 0) and seen[-1][0] == 60 and seen[-1][2] == "100%"
    assert w._progress.isHidden() and jp.slice_btn.text() == "Slice"
    assert not jp.results.isHidden()
    assert jp.result["Layers"].text() == "60" and jp.result["Layer thickness"].text() == "50 µm (0.05 mm)"
    assert jp.result["Volume"].text() == f"{res.volume / 1000:.2f} ml" == "0.11 ml"
    assert jp.result["Weight"].text() == f"{res.weight:.2f} g" and jp.result["Print time"].text().endswith(" s")
    assert "held in memory" in jp.status.text() and w.held_slice() is res
    assert len(w.layer_preview.goo.layers) == 60                       # View → Layers shows it
    # Export and Send to printer share a row under Slice (Send on the right), with icons
    lay = jp.slice_btn.parentWidget().layout()
    assert lay.itemAt(lay.indexOf(jp.slice_btn) + 1).widget() is jp.gpu_box     # the GPU option under Slice
    orow = lay.itemAt(lay.indexOf(jp.slice_btn) + 2).layout()
    assert orow.indexOf(jp.export_btn) == 0 and orow.indexOf(jp.send_btn) == 1
    assert not jp.export_btn.icon().isNull() and not jp.send_btn.icon().isNull()
    assert jp.export_btn.isEnabled() and jp.send_btn.isEnabled()
    exp = tmp_path / "exported"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(exp), ""))
    jp.export_btn.click()
    assert [x.data for x in goo.read(str(exp) + ".goo").layers] == [x.data for x in res.goo.layers]
    sent = []
    monkeypatch.setattr(w.lan, "send_file", lambda path, start=None: (sent.append((path, start)), True)[1])
    jp.send_btn.click()
    assert len(sent) == 1 and sent[0][1] is True and goo.read(sent[0][0]).header.layer_count == 60

    # File → Slice and save writes what is held, without slicing again
    out = tmp_path / "held.goo"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *a, **k: (str(out), ""))
    started = []
    monkeypatch.setattr(w, "start_slice", lambda *a: started.append(a))
    w.slice_dialog()
    back = goo.read(out)
    assert not started and [(x.data, x.definition.pack()) for x in back.layers] == \
        [(x.data, x.definition.pack()) for x in res.goo.layers]

    # any change to the plate or the settings makes it out of date
    w.scene.parts[0].position[0] += 5
    w.scene_changed()
    assert w.held_slice() is None and "changed since this slice" in jp.status.text()
    assert not jp.export_btn.isEnabled() and not jp.send_btn.isEnabled()
    w.scene.parts[0].position[0] -= 5
    w.scene_changed()
    assert w.held_slice() is res and jp.export_btn.isEnabled()
    w.print_settings.widgets["exposure"].setValue(w.print_settings.widgets["exposure"].value() + 1)
    assert w.held_slice() is None and "changed since this slice" in jp.status.text()
    w.slice_dialog()
    assert _wait(app, lambda: started, 60)                             # now it slices again (after the island check)
    w.close()


def test_log_and_layers_windows(app, tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    from PySide6.QtWidgets import QDockWidget
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    # not part of the main window any more
    assert set(w.docks) == {"Printer settings", "Models", "Flaws"}
    assert not any(d.isAncestorOf(w.log) or d.isAncestorOf(w.layer_preview) for d in w.findChildren(QDockWidget))
    assert not w.log.isVisible() and not w.layer_preview.isVisible()
    view = next(a.menu() for a in w.menuBar().actions() if a.text() == "&View")
    acts = {a.text().replace("&", ""): a for a in view.actions()}
    acts["Logs"].trigger()
    app.processEvents()
    assert w.log_window.isVisible() and w.log_window.isWindow() and w.log.isVisible()
    assert w.log_window.isAncestorOf(w.log) and "Logs" in w.log_window.windowTitle()
    acts["Layers"].trigger()
    app.processEvents()
    assert w.layers_window.isVisible() and w.layers_window.isWindow() and w.layer_preview.isVisible()
    w.log_window.move(140, 120)
    w.log_window.resize(700, 300)
    app.processEvents()
    size = w.log_window.size()
    w.close()                                                 # closes them too, remembering the size
    assert not w.log_window.isVisible() and not w.layers_window.isVisible()
    w = MainWindow(settings=s)
    w.log_window.popup()
    app.processEvents()
    assert w.log_window.size() == size
    w.close()


def test_fix_islands_adds_a_support_under_each(app, tmp_path, monkeypatch):
    import numpy as np
    import trimesh
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    from gooslicer import supports as S
    from gooslicer.scene import Part
    from gooslicer_qt.main import MainWindow
    from gooslicer_qt.supporting import SupportsCommand
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    # one part: a base block, and a block hanging 3 mm above it (not connected)
    base = trimesh.creation.box((10, 10, 2))
    base.apply_translation((0, 0, 1))
    hang = trimesh.creation.box((3, 3, 2))
    hang.apply_translation((0, 0, 6))
    stl = tmp_path / "two.stl"
    trimesh.util.concatenate([base, hang]).export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    p = w.scene.parts[0]
    # supports under the base only, as a user could leave them
    params = S.SupportParams()
    st = p.state()
    dz = params.lift - p.bounds()[0][2]
    st.position[2] += dz
    lo = p.bounds()[0]
    hits = [S.Contact(np.array([x, y, lo[2] + dz]), np.array([0, 0, -1.0]), S.MANUAL)
            for x in (-3, 3) for y in (-3, 3)]
    frame = Part(p.name, p.mesh, st.position, st.rotation, st.scale)
    plan = S.SupportPlan.from_world(frame, hits, params)
    w.undo.push(SupportsCommand(w, p.id, SupportsCommand.snapshot(p), (plan, params.lift, st), "Add"))
    assert _wait(app, lambda: w.supports.world_mesh(p) is not None, 60)
    isl = w.unsupported_islands()
    assert len(isl) == 1 and isl[0].point[2] > lo[2] + dz + 4                   # the hanging block
    n0, undo0 = len(p.supports), w.undo.index()
    added, full, missed = w.support_islands(isl, params)
    assert (added, full, missed) == (1, [], 0) and len(p.supports) == n0 + 1
    new = p.supports.world_contacts(p)[-1]
    assert new.kind == S.ISLAND and new.point[2] <= isl[0].point[2] and isl[0].point[2] - new.point[2] < 0.1
    assert abs(new.point[0]) < 1.6 and abs(new.point[1]) < 1.6 and new.normal[2] < 0
    assert w.undo.index() == undo0 + 1                                           # one undo step
    assert _wait(app, lambda: w.supports.world_mesh(p) is not None, 60)
    assert _wait(app, lambda: not w.unsupported_islands(), 60)                   # fixed
    w.undo.undo()
    assert len(p.supports) == n0
    w.close()


def test_models_tab_remove_instance_mirror(app, tmp_path, monkeypatch):
    import numpy as np
    import trimesh
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QTabBar
    from gooslicer import supports as S
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    bar = next(b for b in w.findChildren(QTabBar) if b.count() == 3 and b.tabText(1) == "Models")
    assert [bar.tabText(i) for i in range(2)] == ["Printer settings", "Models"]
    m = trimesh.creation.box((12, 6, 4))
    m.vertices[m.vertices[:, 1] > 0, 0] += 5                  # lopsided, so a mirror shows
    stl = tmp_path / "wedge.stl"
    m.export(stl)
    w.import_files([str(stl), str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 2)
    w.arrange_all()
    bar.setCurrentIndex(1)
    app.processEvents()
    a, b = w.scene.parts

    # the red x of a row removes that model (undoable); clicking the name still selects
    lst = w.parts
    lst.setCurrentRow(-1)

    def click(pt):
        for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            QApplication.sendEvent(lst.viewport(), QMouseEvent(kind, QPointF(pt), QPointF(lst.viewport().mapToGlobal(pt)),
                                                               Qt.LeftButton,
                                                               Qt.LeftButton if kind == QEvent.MouseButtonPress else Qt.NoButton,
                                                               Qt.NoModifier))
        app.processEvents()
    name_pt = lst.visualRect(lst.model().index(0, 0)).center()
    name_pt.setX(10)
    click(name_pt)
    assert w.selected_ids() == [a.id]
    click(lst.x_rect(1).center())
    assert [p.id for p in w.scene.parts] == [a.id]
    w.undo.undo()
    assert [p.id for p in w.scene.parts] == [a.id, b.id]

    # Add instance: a copy beside the original, overlapping nothing, on the plate
    n0 = w.undo.index()
    w.part_menu(a.id).actions()[0].trigger()
    assert len(w.scene.parts) == 3 and w.undo.index() == n0 + 1
    new = w.scene.parts[-1]
    assert new.mesh is a.mesh and w.scene.in_volume(new) and w.selected_ids() == [new.id]
    for o in (a, b):
        lo1, hi1 = new.bounds()
        lo2, hi2 = o.bounds()
        assert (hi1[:2] < lo2[:2]).any() or (hi2[:2] < lo1[:2]).any()      # footprints apart
    w.undo.undo()
    assert len(w.scene.parts) == 2

    # Mirror: front to back (Y), around the model's own centre; supports follow; undoable
    plan = S.SupportPlan.from_world(a, [S.Contact(a.world_vertices()[0], np.array([0, 0, -1.0]), S.MANUAL)],
                                    S.SupportParams())
    a.supports = plan
    v0 = a.world_vertices().copy()
    c0 = a.supports.world_contacts(a)[0].point.copy()
    st0 = a.state()
    w.part_menu(a.id).actions()[1].trigger()
    exp = v0.copy()
    exp[:, 1] = 2 * st0.position[1] - exp[:, 1]
    def rows(v):                                              # the vertex set, order-free
        return np.array(sorted(map(tuple, np.round(v, 6))))
    got = a.world_vertices()
    assert np.allclose(rows(got), rows(exp)) and not np.allclose(rows(got), rows(v0))
    assert np.all(a.scale > 0) and np.isclose(np.linalg.det(a.rotation), 1)
    assert a.world_mesh().volume > 0                          # faces still point out
    c1 = a.supports.world_contacts(a)[0].point
    assert np.allclose(c1, [c0[0], 2 * st0.position[1] - c0[1], c0[2]])
    assert w.undo.text(w.undo.index() - 1) == "Mirror Y"
    w.undo.undo()
    assert np.allclose(a.world_vertices(), v0) and a.supports is plan
    a.supports = None
    w.close()


def test_flaws_tab_select_and_dismiss(app, tmp_path, monkeypatch):
    import numpy as np
    import trimesh
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    from PySide6.QtCore import QEvent, QPointF, Qt
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtWidgets import QApplication, QTabBar
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    w.resize(1200, 800)
    w.show()
    app.processEvents()
    bar = next(b for b in w.findChildren(QTabBar) if b.count() == 3)
    assert [bar.tabText(i) for i in range(3)] == ["Printer settings", "Models", "Flaws"]   # after Models
    fp = w.flaws_panel
    assert fp.list.count() == 0 and not fp.empty.isHidden()

    files = []
    for name in ("a", "b"):
        f = tmp_path / f"{name}.stl"
        trimesh.creation.box((8, 8, 8)).export(f)
        files.append(str(f))
    w.import_files(files)
    assert _wait(app, lambda: len(w.scene.parts) == 2, 30)
    a, b = w.scene.parts
    a.position[:] = (-30, 0, 9)                # both float 5 mm up: two unsupported islands
    b.position[:] = (30, 0, 9)
    w.scene_changed()
    isl = w.check_islands()
    assert len(isl) == 2
    bar.setCurrentIndex(2)
    app.processEvents()
    texts = [fp.list.item(i).text() for i in range(fp.list.count())]
    assert [t.split("\n")[0] for t in texts] == ["Unsupported island 1", "Unsupported island 2"]
    for i in range(fp.list.count()):          # the red x is in view, not past a sideways scroll
        assert fp.list.viewport().rect().contains(fp.list.x_rect(i))
    assert fp.empty.isHidden()
    w.viewport.reset_view()
    labels = sorted(lab.text for lab in w.viewport.guide_labels())
    assert labels == ["Unsupported island 1", "Unsupported island 2"]
    assert len(w.viewport.island_meshes) == 2

    def click(pos, button=Qt.LeftButton):
        vp_ = fp.list.viewport()
        for kind in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
            QApplication.sendEvent(vp_, QMouseEvent(kind, QPointF(pos), vp_.mapToGlobal(QPointF(pos)), button,
                                                    button if kind == QEvent.MouseButtonPress else Qt.NoButton,
                                                    Qt.NoModifier))
        app.processEvents()

    # clicking a flaw selects it, as clicking its label does; again deselects
    row0 = fp.list.visualRect(fp.list.model().index(0, 0))
    click(row0.center())
    assert w.viewport.selected_guides == {("island", 1)}
    lab = next(x for x in w.viewport.guide_labels() if x.text == "Unsupported island 1")
    assert lab.selected
    click(row0.center())
    assert w.viewport.selected_guides == set()
    # and a label click selects its row
    lab2 = next(x for x in w.viewport.guide_labels() if x.text == "Unsupported island 2")
    w.viewport.toggle_guide(lab2)
    assert [fp.list.item(i).isSelected() for i in range(2)] == [False, True]

    # the red x dismisses a flaw: gone from the list, the 3D view and the next check
    click(fp.list.x_rect(0).center())
    assert fp.list.count() == 1 and fp.list.item(0).text().startswith("Unsupported island 2")
    assert len(w.viewport.island_meshes) == 1 and list(w.viewport.island_numbers) == [2]
    assert [x.text for x in w.viewport.guide_labels()] == ["Unsupported island 2"]
    assert w.viewport.selected_guides == {("island", 2)}             # the other one stays selected
    assert "Dismissed Unsupported island 1" in w.log.toPlainText()
    assert len(w.check_islands()) == 1 and fp.list.count() == 1       # it stays dismissed
    assert fp.list.item(0).text().startswith("Unsupported island 1")  # renumbered by the new check
    # until a part changes
    a.position[0] += 1
    w.scene_changed()
    assert fp.list.count() == 0                                        # the plate changed: cleared
    assert len(w.check_islands()) == 2 and fp.list.count() == 2
    w.close()


def test_gpu_slicing_setting(app, tmp_path, monkeypatch):
    import trimesh
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    jp = w.job_panel
    assert not jp.gpu_box.isChecked() and not w.gpu_slicing()       # off by default
    stl = tmp_path / "cube.stl"
    trimesh.creation.box((10, 10, 2)).export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)

    def slice_():
        jp.slice_btn.click()
        assert _wait(app, lambda: w.held_slice() is not None and not w.slicing(), 120)
        return w.held_slice()
    cpu = slice_()
    assert cpu.backend == "cpu" and jp.result["Sliced on"].text().startswith("CPU, ")
    assert jp.result["Sliced on"].text().count(",") == 1                 # just where and how long
    # the (i) beside it pops up the hardware
    row = jp.backend_row
    assert row.indexOf(jp.result["Sliced on"]) == 0 and row.indexOf(jp.backend_btn) == 1 and not jp.backend_btn.isHidden()
    jp.backend_btn.click()
    app.processEvents()
    assert jp.backend_details.isVisible() and jp.backend_info["Processor"].text() == "CPU"
    assert jp.backend_info["Device"].text() and jp.backend_info["Time"].text().endswith(" layers")
    jp.backend_details.hide()

    jp.gpu_box.setChecked(True)                                      # remembered
    assert w.gpu_slicing() and s.value("gpuSlicing") in (True, "true")
    w.scene.parts[0].position[0] += 1                                # a new slice
    w.scene.parts[0].position[0] -= 1
    w.held = None
    w.scene_changed()
    gpu = slice_()
    log = w.log.toPlainText()
    assert "Slicing on the GPU (experimental)" in log
    if gpu.backend.startswith("gpu"):
        assert jp.result["Sliced on"].text().startswith("GPU, ")
        assert jp.backend_info["Processor"].text() == "GPU (OpenGL)"
        assert jp.backend_info["Device"].text() == gpu.backend[5:-1] and jp.backend_info["Device"].text() in log
        # the same layers as the CPU made (a box: no edge pixels in doubt)
        assert [x.data for x in gpu.goo.layers] == [x.data for x in cpu.goo.layers]
    else:                                                            # no GL here: the CPU, and why
        assert jp.result["Sliced on"].text().startswith("CPU, ") and "sliced on the CPU" in log
    assert "Layers made on the " in log
    w.close()
    w2 = MainWindow(settings=s)
    assert w2.job_panel.gpu_box.isChecked()
    w2.close()


def test_transform_collapsed(app, tmp_path, monkeypatch):
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    w.show()
    app.processEvents()
    pp = w.props
    fields = pp.pos + pp.size + pp.scale + [pp.uniform]
    assert all(pp.transform.isAncestorOf(f) for f in fields)
    assert not pp.transform_btn.isChecked() and not any(f.isVisible() for f in fields)
    assert pp.transform_btn.arrowType() == Qt.RightArrow
    pp.transform_btn.click()
    app.processEvents()
    if pp.isVisible():
        assert all(f.isVisible() for f in fields)
    assert pp.transform_btn.arrowType() == Qt.DownArrow
    pp.transform_btn.click()
    assert not pp.transform.isVisible() and pp.transform_btn.arrowType() == Qt.RightArrow
    w.close()



def test_preferences_window(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    from elegoo_lan.protocol import Printer
    from gooslicer.support_defaults import HollowSettings
    from gooslicer_qt.main import MainWindow
    from gooslicer_qt.preferences import AUTO_SUPPORT, PreferencesDialog
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    src = tmp_path / "officialPlatform.ini"
    src.write_bytes(FIXTURE.encode())
    assert w.import_profiles(str(src))
    # Edit → Preferences… (after the other Edit items)
    edit = next(a.menu() for a in w.menuBar().actions() if a.text() == "&Edit")
    assert edit.actions()[-1] is w.prefs_action and w.prefs_action.text() == "&Preferences…"
    w.prefs_action.trigger()
    d = w._preferences
    assert isinstance(d, PreferencesDialog) and d.isVisible() and d.isModal()
    names = [d.categories.item(i).text() for i in range(d.categories.count())]
    assert names == ["Appearance", "3D view colors", "Printer", "Resin", "Slicing", "Hollowing", "Drain holes", "Supports",
                     "Network", "Files and folders", "Windows", "Keyboard"]

    # changes apply at once, both ways
    d.show_page("Appearance")
    d.theme.setCurrentIndex(d.theme.findData("dark"))
    assert w.theme.mode == "dark" and s.value("theme") == "dark" and w.theme_actions["dark"].isChecked()
    d.reset_page()
    assert w.theme.mode == "system"
    d.show_page("Slicing")
    d.gpu.setChecked(True)
    assert w.job_panel.gpu_box.isChecked() and w.gpu_slicing()
    d.reset_page()
    assert not w.job_panel.gpu_box.isChecked() and not d.gpu.isChecked()
    d.show_page("Printer")
    assert d.printer.currentText() == w.printer_panel.combo.currentText() != ""
    assert str(tmp_path / "data") in d.profile_path.text()
    d.show_page("Hollowing")
    d.hollow.thickness.setValue(3.5)
    assert float(s.value("hollow/thickness")) == 3.5
    from gooslicer_qt.hollowing import HollowFields
    assert HollowFields(s).value().thickness == 3.5               # the Hollow panel starts there
    d.reset_page()
    assert HollowFields(s).value() == HollowSettings() and HollowFields(s).view_opacity() == 0.85
    d.show_page("Drain holes")
    d.drain.r1.setValue(2.25)
    assert float(s.value("drain/radius1")) == 2.25
    d.show_page("Supports")
    assert d.support.itemText(0) == AUTO_SUPPORT and d.support.currentIndex() == 0
    d.support.setCurrentIndex(2)
    d.support.activated.emit(2)
    pick = d.support.itemText(2)
    assert w.job_panel.support_choice() == pick == s.value("supportType")
    assert w.job_panel.support_type.currentText() == pick
    d.reset_page()
    assert w.job_panel.support_choice() is None and not s.contains("supportType")
    d.show_page("Files and folders")
    s.setValue("lastGooDir", str(tmp_path))
    d.show_page("Files and folders")
    assert d.folder_labels["lastGooDir"].text() == str(tmp_path)
    d.reset_page()
    assert not s.contains("lastGooDir") and d.folder_labels["lastGooDir"].text() == str(Path.home())
    # saved printers can be forgotten
    w.lan._remember([Printer(name="A", ip="10.0.0.5", mainboard_id="m1"),
                     Printer(name="B", ip="10.0.0.6", mainboard_id="m2")])
    d.show_page("Network")
    assert d.saved.count() == 2
    d.saved.setCurrentRow(0)
    from PySide6.QtWidgets import QPushButton
    forget = next(b for b in d.page("Network").widget.findChildren(QPushButton)
                  if b.text() == "Forget printer")
    forget.click()
    assert d.saved.count() == 1 and [p.name for p in w.lan.known] == ["B"]
    assert "m1" not in s.value("lan/printers")
    # search narrows the categories
    d.search.setText("drain radius")
    shown = [d.categories.item(i).text() for i in range(d.categories.count()) if not d.categories.item(i).isHidden()]
    assert shown == ["Drain holes"] and d.categories.currentItem().text() == "Drain holes"
    d.search.setText("gpu")
    assert not d.categories.item(names.index("Slicing")).isHidden()
    d.search.clear()
    assert not any(d.categories.item(i).isHidden() for i in range(d.categories.count()))
    d.close()
    w.close()


def test_trapped_resin_check_reports_suction_cups(app, tmp_path, monkeypatch):
    """07 §5: the trapped-resin check also shows suction cups, labelled as such; sealing
    leaves them, and the slice warning says what to do."""
    import trimesh
    from PySide6.QtWidgets import QMessageBox

    from gooslicer import hollow as H
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    cup = H.to_trimesh(H.to_manifold(trimesh.creation.cylinder(radius=10, height=12, sections=48))
                       - H.to_manifold(trimesh.creation.cylinder(radius=8, height=12, sections=48,
                                       transform=trimesh.transformations.translation_matrix([0, 0, 2]))))
    stl = tmp_path / "cup.stl"
    cup.export(stl)
    w.import_files([str(stl)])
    assert _wait(app, lambda: len(w.scene.parts) == 1)
    assert [c.kind for c in w.trapped_resin()] == [H.CUP]
    jp = w.job_panel
    jp.trapped_btn.click()
    assert _wait(app, lambda: jp.trapped_btn.isEnabled(), 60)
    assert "1 suction cup" in jp.trapped_result.text() and "open toward the plate" in jp.trapped_result.text()
    assert [lab.text for lab in w.viewport.guide_labels()] == ["Suction cup 1"]
    assert w.viewport.cavity_meshes and "open toward the film" in w.flaws["cavity"][0].detail
    jp.seal_btn.click()
    assert _wait(app, lambda: jp.seal_btn.isEnabled() and "seal" in jp.trapped_result.text().lower(), 60)
    assert "nothing to seal" in jp.trapped_result.text() and "1 suction cup" in jp.trapped_result.text()
    assert w.viewport.cavity_meshes and len(w.trapped_resin()) == 1     # still shown
    asked = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    w.show_cavities(*_cavities(w))
    assert not w._confirm_no_trapped_resin() and "1 suction cup" in asked[0] and "film" in asked[0]
    w.close()


def _cavities(w):
    from gooslicer_qt.main import _cavities_and_faces
    cav, red = _cavities_and_faces([p.world_mesh() for p in w.scene.parts])
    return cav, red, w._plate_fingerprint()


def test_hollowing_and_supports_buttons_share_a_width(app, tmp_path, monkeypatch):
    from PySide6.QtWidgets import QMessageBox
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: QMessageBox.Yes)
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    w.resize(1500, 1000)
    w.show()
    app.processEvents()
    jp = w.job_panel
    assert jp.trapped_btn.text() == "Check for voids" and jp.islands_btn.text() == "Detect islands"
    buttons = [jp.hollow_btn, jp.drain_btn, jp.trapped_btn, jp.seal_btn,
               jp.generate_btn, jp.remove_btn, jp.islands_btn, jp.fix_btn]
    for width in (jp.width(), jp.width() + 150):             # also when the panel is resized
        w.docks["Models"].setMinimumWidth(width)
        app.processEvents()
        widths = {b.width() for b in buttons}
        assert len(widths) == 1, widths
        assert all(b.width() >= b.sizeHint().width() for b in buttons)   # no clipped labels
    menus = {a.text(): a.menu() for a in w.menuBar().actions()}
    assert "Check for &voids" in [a.text() for a in menus["&Edit"].actions()]
    assert "&Detect islands" in [a.text() for a in menus["&Supports"].actions()]
    w.close()


def test_drain_hole_links_load(app, tmp_path):
    from gooslicer_qt.hollowing import DrainHoleFields
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    f = DrainHoleFields(s)
    assert f.radii_linked and f.depths_linked                         # new: linked
    s.setValue("drain/radius1", 3.0)
    s.setValue("drain/radius2", 2.5)                                  # saved before the padlocks
    f = DrainHoleFields(s)
    assert not f.radii_linked and f.depths_linked and (f.r1.value(), f.r2.value()) == (3.0, 2.5)
    s.setValue("drain/radius2", 3.0)
    s.setValue("drain/linkDepth", False)
    f = DrainHoleFields(s)
    assert f.radii_linked and not f.depths_linked
    seen = []
    f.changed.connect(lambda: seen.append(1))
    f.r2.setValue(4.0)
    assert f.r1.value() == 4.0 and len(seen) == 1                     # one change for the pair


def _h264_clip(path, frames=60):
    """A short H.264 clip like the printer's (yuv420p), to stand in for its RTSP stream."""
    import av
    import numpy as np
    with av.open(str(path), "w") as out:
        st = out.add_stream("libx264", rate=30)
        st.width, st.height, st.pix_fmt = 320, 180, "yuv420p"
        st.options = {"g": "10"}
        for i in range(frames):
            img = np.zeros((180, 320, 3), np.uint8)
            img[:, : (i * 5) % 320] = (200, 40, 40)
            for pkt in st.encode(av.VideoFrame.from_ndarray(img, format="rgb24")):
                out.mux(pkt)
        for pkt in st.encode():
            out.mux(pkt)
    return path


def test_camera_panel(app, tmp_path, monkeypatch):
    pytest.importorskip("av")
    from sim_printer import SimPrinter
    from gooslicer_qt import camera as cam
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    sim = SimPrinter()
    sim.video_url = _h264_clip(tmp_path / "cam.mp4", 900).as_uri()      # 30 s: plays throughout
    try:
        from gooslicer_qt.main import MainWindow
        s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
        w = MainWindow(settings=s, lan_ports={"discovery": sim.udp_port, "ws": sim.ws_port,
                                              "http": sim.http_port, "targets": ["127.0.0.1"]})
        w.show()
        c = w.camera
        # below Advanced settings, in the Printer settings tab, folded and off
        ps = w.print_settings
        assert ps.isAncestorOf(c) and c.mapTo(ps, c.rect().topLeft()).y() > ps.advanced_btn.geometry().bottom()
        assert not c.is_open() and not c.body.isVisible()
        c.set_open(True)                                                 # not connected: nothing to show
        assert not c.wanted and not c.button.isEnabled() and "Connect to a printer" in c.status.text()
        c.set_open(False)

        w.lan.find_printers()
        assert _wait(app, lambda: w.lan.combo.count() == 1)
        w.lan.connect_printer()
        assert _wait(app, lambda: w.lan.session and w.lan.session.connected and c.button.isEnabled(), 15)
        c.set_open(True)                                                 # opening starts the feed
        assert _wait(app, lambda: c.view.image is not None, 30), (c.view.message, c.status.text(), c.isVisible(), c.stream)
        assert sim.video_on == [1] and c.playing() and c.status.text().startswith("Live")
        assert c.button.text() == "Stop camera"
        img = c.view.image
        assert 0 < img.width() <= c.view.device_size().width() + 2

        # another tab in front: decoding pauses, the printer's stream stays on; back: resumes
        w.docks["Models"].raise_()
        assert _wait(app, lambda: not c.playing(), 5) and c.wanted and sim.video_on == [1]
        w.docks["Printer settings"].raise_()
        assert _wait(app, lambda: c.playing() and c.reader.frames > 0, 30)

        # pop out: the picture moves to its own window, which keeps playing behind other tabs
        assert c.popout_btn.geometry().right() < c.button.geometry().left()   # left of Stop camera
        c.popout_btn.click()
        assert c.popped_out() and c.popout_btn.text() == "Pop in" and c.popout.isWindow()
        assert c.view.message == "Shown in its own window" and c.view.image is None
        assert _wait(app, lambda: c.window_view.image is not None, 30)
        # the window opens at the stream's own resolution (the clip is 320 x 180)
        assert _wait(app, lambda: c.window_view.size() == QtCore.QSize(320, 180), 5), c.window_view.size()
        assert c.stream_size == QtCore.QSize(320, 180) and c.popout.size() == QtCore.QSize(320, 180)
        assert _wait(app, lambda: c.window_view.image.size() == QtCore.QSize(320, 180), 5)   # full detail
        w.docks["Models"].raise_()
        n = c.reader.frames
        assert _wait(app, lambda: c.playing() and c.reader.frames > n + 5, 10)
        w.docks["Printer settings"].raise_()
        c.popout.close()                                                 # closing brings it back
        assert not c.popped_out() and c.popout_btn.text() == "Pop out" and c.wanted
        assert _wait(app, lambda: c.view.image is not None and c.view.message == "", 10)
        assert s.value("cameraWindowGeometry") is not None              # remembers where it was

        c.button.click()                                                 # stop: stream switched off
        assert _wait(app, lambda: sim.video_on == [1, 0], 5)
        assert not c.playing() and not c.wanted and c.view.image is None and c.button.text() == "Start camera"

        # a dead stream retries, then gives up and says why
        monkeypatch.setattr(cam, "RETRY_DELAY_MS", 10)
        monkeypatch.setattr(cam, "OPEN_TIMEOUT_S", 1.0)
        sim.video_url = (tmp_path / "missing.mp4").as_uri()
        c.button.click()
        assert _wait(app, lambda: not c.wanted, 30)
        assert "Camera feed error" in c.status.text() and sim.video_on[-1] == 0

        # disconnecting stops it
        sim.video_url = (tmp_path / "cam.mp4").as_uri()
        c.button.click()
        assert _wait(app, lambda: c.view.image is not None, 30)
        w.lan.disconnect_printer()
        assert not c.wanted and not c.playing() and not c.button.isEnabled()

        # a printer without a camera
        sim.camera = False
        w.lan.connect_printer()
        assert _wait(app, lambda: w.lan.session and w.lan.session.connected
                     and w.lan.session.state.attributes, 15)
        assert _wait(app, lambda: "no camera" in c.status.text(), 5) and not c.button.isEnabled()
        w.close()
    finally:
        sim.close()


def test_profiles_copied_so_satellite_can_go(app, tmp_path, monkeypatch):
    import shutil

    from test_profile_store import install
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer_qt.main import MainWindow, data_dir, profile_root
    from gooslicer import profile_store
    sat = tmp_path / "wine" / "drive_c" / "Program Files" / "ELEGOO SatelLite"
    ini = install(sat, ("Grey",), ("EVO_0-30mm", "Tree_Small"))
    # set up as an older GooSlicer left it: a copy of the ini only, the rest read from the install
    shutil.copyfile(ini, data_dir() / "officialPlatform.ini")
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    s.setValue("profileIni", str(data_dir() / "officialPlatform.ini"))
    s.setValue("resinDir", str(sat / "resources" / "resin" / "material"))
    s.setValue("scriptDir", str(sat / "resources" / "script" / "common"))

    def check(w):
        assert profile_store.is_inside(w._profile_ini, profile_root())
        assert w.printer_panel.combo.count() > 0
        assert [r.name for r in w.print_settings.resins] == ["Grey"]
        assert profile_store.is_inside(w.resin_dir(), profile_root())
        assert sorted(t.name for t in w.support_templates()) == ["EVO_0-30mm", "Tree_Small"]

    w = MainWindow(settings=s)
    check(w)
    assert "SatelLite is no longer needed" in w.log.toPlainText()
    assert s.value("resinDir") is None and s.value("scriptDir") is None and s.value("profileIni") is None
    w.close()

    shutil.rmtree(tmp_path / "wine")                                   # SatelLite uninstalled
    w = MainWindow(settings=s)
    check(w)
    assert "No printer profiles found" not in w.log.toPlainText()
    dlg = w.open_preferences("Printer")
    # copied from where the older GooSlicer read it
    assert "officialPlatform.ini (20" in dlg.profile_source.text() and dlg.resin_dir.text().startswith("1 profiles")
    assert dlg.script_dir.text().startswith("3 templates")              # two plus the Jupiter 2 one
    dlg.close()

    # Replace… copies another folder of resins over the copy
    more = tmp_path / "more"
    from test_profile_store import resin
    resin(more / "a.data", "Other")
    assert w.set_resin_dir(str(more))
    assert [r.name for r in w.print_settings.resins] == ["Other"]
    w.close()


def test_memory_in_status_bar(app, tmp_path, monkeypatch):
    import subprocess
    import sys

    from gooslicer_qt import memory as M
    # a child process is counted with GooSlicer
    child = subprocess.Popen([sys.executable, "-c", "import time; b = bytearray(200_000_000); time.sleep(30)"])
    try:
        assert _wait(app, lambda: child.pid in M.descendants(os.getpid()), 5)
        assert _wait(app, lambda: M.process_bytes(child.pid) > 150e6, 10)
        use = M.memory_use()
        assert use.processes >= 2 and use.used > 150e6 and use.total > use.used
        assert 0 < use.percent < 100
        assert use.text() == f"{use.used / 1e9:.2f} GB · {use.percent:.1f} %"
        assert "worker process" in use.tooltip() and " use " in use.tooltip()
    finally:
        child.kill()
        child.wait()
    assert M.process_bytes(child.pid) == 0                                  # gone: counts nothing

    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "wine"))
    monkeypatch.delenv("GOOSLICER_PROFILE_INI", raising=False)
    from gooslicer_qt.main import MainWindow
    w = MainWindow(settings=QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat))
    w.resize(1200, 800)
    w.show()
    sb = w.statusBar()
    assert isinstance(sb, M.StatusBar)
    assert _wait(app, lambda: sb.memory.text().startswith("Memory ") and " GB · " in sb.memory.text(), 10)
    # at the bottom left, and still shown with a message, which starts to its right
    sb.showMessage("Slicing part.stl…")
    app.processEvents()
    assert sb.memory.isVisible() and sb.memory.x() < 20
    r = sb.message_rect()
    assert r.left() > sb.memory.geometry().right() and r.width() > 100
    w._progress.show()                                                     # stops before the progress bar
    app.processEvents()
    assert sb.message_rect().right() < w._progress.x()
    img = sb.grab().toImage()
    assert img.width() == sb.width()
    w.close()
    assert not w.memory_monitor._thread.is_alive() or _wait(app, lambda: not w.memory_monitor._thread.is_alive(), 5)


def test_vertex_data_float32_and_memory_trim(app):
    import numpy as np
    import trimesh

    from gooslicer_qt import memory as M
    from gooslicer.scene import GPU_KEY, Part, gpu_arrays, hull_index, prepare
    m = trimesh.creation.icosphere(2)
    v, f = gpu_arrays(prepare(m))
    assert v.dtype == np.float32 and f.dtype == np.uint32 and np.allclose(v, m.vertices, atol=1e-6)
    assert (f == m.faces).all() and GPU_KEY in m._cache
    m.apply_translation([1, 0, 0])                          # a changed mesh drops the stale copy
    assert GPU_KEY not in m._cache
    # bounds from the hull vertices match bounds from every vertex, under any transform
    big = trimesh.creation.icosphere(4)
    part = Part.from_mesh("s", big)
    assert len(hull_index(part.mesh)) <= len(big.vertices)
    part.rotation = trimesh.transformations.rotation_matrix(0.7, [1, 2, 3])[:3, :3]
    part.scale = np.array([1.0, -2.0, 0.5])
    part.position = np.array([3.0, 4.0, 5.0])
    w = part.world_vertices()
    assert np.allclose(part.bounds(), [w.min(axis=0), w.max(axis=0)])
    flat = trimesh.Trimesh(np.c_[np.random.rand(100, 2), np.zeros(100)], np.zeros((0, 3), int))
    assert len(hull_index(flat)) == 100                     # qhull fails on a flat set: all of them
    M.trim_memory()                                         # gc + malloc_trim: harmless any time
    M.trim_memory_soon()
    M.trim_memory_soon()                                    # merged into one, after TRIM_DELAY_MS
    assert M._trim_timer.isActive()
    assert _wait(app, lambda: not M._trim_timer.isActive(), 5)


def test_hollow_dialog_infill_fields(app, tmp_path):
    """07 §2b: the Hollow dialog's infill fields, as SatelLite's: off by default, not for Open
    bottom, and kept between sessions."""
    from gooslicer.support_defaults import HollowSettings
    from gooslicer_qt.hollowing import HollowFields
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    f = HollowFields(s)
    assert f.value() == HollowSettings() and not f.infill_size.isEnabled()
    f.infill.setChecked(True)
    assert f.infill_type.isEnabled() and f.infill_size.isEnabled() and f.infill_radius.isEnabled()
    f.infill_type.setCurrentIndex(1)
    f.infill_size.setValue(6.0)
    f.infill_radius.setValue(0.8)
    v = f.value()
    assert (v.infill, v.infill_type, v.infill_size, v.infill_radius) == (True, "fcc", 6.0, 0.8)
    f.kind.setCurrentIndex(3)                                   # Open bottom
    assert not f.infill.isEnabled() and not f.infill_size.isEnabled() and f.value().infill is False
    f.kind.setCurrentIndex(0)
    f.save()
    g = HollowFields(s)
    assert g.value() == v


def test_view_color_preferences(app, tmp_path):
    """Edit → Preferences → 3D view colors: a colour (wheel, R/G/B or hex) and a transparency
    for models, selected models, supports, and flaw lines/labels; applied at once, remembered,
    Cancel in the picker puts the colour back, Restore defaults resets them all."""
    from PySide6.QtCore import QPoint, QTimer
    from PySide6.QtGui import QColor
    from gooslicer_qt import colors as C
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    w.prefs_action.trigger()
    d = w._preferences
    d.show_page("3D view colors")
    assert set(d.color_buttons) == {"model", "selected", "support", "flaw", "flaw_selected"}
    assert d.color_sliders["support"].value() == 50 and d.color_sliders["model"].value() == 0
    assert d.color_buttons["model"].text() == C.default_color("model").name()

    d.color_sliders["model"].setValue(30)                        # transparency, applied at once
    assert abs(w.viewport.colors["model"].alphaF() - 0.7) < 0.01 and d.color_values["model"].text() == "30%"

    def pick(color, ok):
        dlg = d._color_dialog
        assert isinstance(dlg, C.ColorDialog)
        for sp, x in zip(dlg.rgb, color):
            sp.setValue(x)
        assert w.viewport.colors["selected"].getRgb()[:3] == color     # the view follows live
        dlg.accept() if ok else dlg.reject()
    QTimer.singleShot(0, lambda: pick((10, 200, 30), True))
    d.color_buttons["selected"].click()
    assert w.viewport.colors["selected"].getRgb()[:3] == (10, 200, 30)
    assert d.color_buttons["selected"].text() == "#0ac81e"
    QTimer.singleShot(0, lambda: pick((1, 2, 3), False))           # Cancel: back as it was
    d.color_buttons["selected"].click()
    assert w.viewport.colors["selected"].getRgb()[:3] == (10, 200, 30)

    # remembered by a new window
    w2 = MainWindow(settings=s)
    assert w2.viewport.colors["selected"].getRgb()[:3] == (10, 200, 30)
    assert abs(w2.viewport.colors["model"].alphaF() - 0.7) < 0.01
    w2.close()

    d.reset_page()
    assert all(w.viewport.colors[k] == C.default_color(k) for k in C.KEYS)
    assert d.color_sliders["model"].value() == 0 and not any(k.startswith("colors/") for k in s.allKeys())

    # the wheel, brightness, R/G/B and hex stay in step
    dlg = C.ColorDialog(QColor("#336699"), "t")
    assert dlg.hex.text() == "#336699" and abs(dlg.wheel.v - 0x99 / 255) < 0.01
    dlg.hex.setText("#ff0000")
    dlg.hex.editingFinished.emit()
    assert [sp.value() for sp in dlg.rgb] == [255, 0, 0] and dlg.wheel.s == 1.0
    r = dlg.wheel._radius()
    c = dlg.wheel.SIZE / 2
    from PySide6.QtTest import QTest
    QTest.mouseClick(dlg.wheel, QtCore.Qt.LeftButton, pos=QPoint(int(c), int(c - r * 0.99)))  # top: hue 90°
    col = dlg.color()
    assert abs(col.hsvHueF() - 0.25) < 0.02 and col.saturationF() > 0.95
    dlg.value.setValue(0)                                          # brightness 0: black
    assert dlg.color().getRgb()[:3] == (0, 0, 0) and dlg.hex.text() == "#000000"
    d.close()
    w.close()
