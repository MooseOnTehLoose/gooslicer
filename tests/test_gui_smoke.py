import os

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
    from gooslicer_qt.hollowing import DrainHoleDialog, HollowDialog
    from gooslicer_qt.main import MainWindow
    s = QtCore.QSettings(str(tmp_path / "s.ini"), QtCore.QSettings.IniFormat)
    w = MainWindow(settings=s)
    # dialogs start at SatelLite's defaults
    d = HollowDialog(s)
    assert d.value() == HollowSettings()
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
    w.hollow_selected(HollowSettings(thickness=1.5, accuracy=0.5))
    assert _wait(app, lambda: done, 60) and done == [True]
    wm = part.world_mesh()
    assert wm.is_watertight
    assert wm.volume == pytest.approx(20 * 20 * 10 - 17 * 17 * 7, rel=0.02)
    cav = w.trapped_resin()
    assert len(cav) == 1

    asked = []
    monkeypatch.setattr(QMessageBox, "warning", lambda *a, **k: asked.append(a[2]) or QMessageBox.No)
    assert not w._confirm_no_trapped_resin() and "sealed cavit" in asked[0]

    # drill from below at the cavity's lowest point
    c = wm.bounds.mean(axis=0)
    hit_part, face, hit = w.scene.pick(np.array([c[0], c[1], wm.bounds[0][2] - 20]), np.array([0.0, 0, 1]))
    assert hit_part is part
    w._hole_settings = DrainHoleDialog(s).value()
    w._hole_picked(part.id, face, hit)
    assert part.world_mesh().is_watertight and w.trapped_resin() == []
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
