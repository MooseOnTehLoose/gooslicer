import os
import struct
from pathlib import Path

import pytest

from gooslicer import qdatastream as Q
from gooslicer import support_defaults as SD

P = "support/scriptmanager/scripcustomtnum0/scriptcontainername/"
INSTALL = Path(os.environ.get("GOOSLICER_SATELLITE_DIR", "/nonexistent"))


def qs(s):
    b = s.encode("utf-16-be")
    return struct.pack(">I", len(b)) + b


def qv(v):
    if isinstance(v, bool):
        return struct.pack(">IB?", 1, 0, v)
    if isinstance(v, int):
        return struct.pack(">IBi", 2, 0, v)
    if isinstance(v, float):
        return struct.pack(">IBd", 38, 0, v)      # Qt5 float, double on the wire
    return struct.pack(">IB", 10, 0) + qs(v)


def write_map(path, m):
    data = struct.pack(">I", len(m)) + b"".join(qs(k) + qv(v) for k, v in m.items())
    Path(path).write_bytes(data)


def template(path, name, crit=50.0, plat_h=0.5, evo=True):
    s = 7 if evo else 0
    write_map(path, {
        P.rstrip("/"): name, P + "scriptNum": 2, P + "scriptNum/script0/type": s,
        P + "scriptNum/script0/type/script1/type": 9,
        f"{name}/LiftHeight": 5.0, f"{name}/EnableInnerSupport": True,
        f"{name}/{s}/area.criticalAngle": crit, f"{name}/{s}/area.criticalAngle/ConfidentState": 3,
        f"{name}/9/platHeight": plat_h, f"{name}/9/hasHole": True})


def test_qdatastream_types(tmp_path):
    f = tmp_path / "m.bin"
    write_map(f, {"a": True, "b": -3, "c": 0.35, "d": "x", "e": ""})
    assert Q.read_variant_map(str(f)) == {"a": True, "b": -3, "c": 0.35, "d": "x", "e": ""}
    f.write_bytes(f.read_bytes() + b"\0")
    with pytest.raises(ValueError, match="trailing"):
        Q.read_variant_map(str(f))


def test_parse_and_recommend(tmp_path):
    for n, c in (("EVO_0-30mm", 37.0), ("EVO_30-60mm", 50.0), ("EVO_60-100mm", 45.0), ("EVO_100+mm", 50.0)):
        template(tmp_path / f"{n}.satsup", n, c)
    template(tmp_path / "Tree_Small.satsup", "Tree_Small", 45.0, evo=False)
    ts = SD.load_templates(tmp_path)
    assert len(ts) == 5
    t = next(t for t in ts if t.name == "EVO_30-60mm")
    assert t.family == "evo" and t.structure["area.criticalAngle"] == 50.0
    assert t.raft == {"platHeight": 0.5, "hasHole": True} and t.lift_height == 5.0
    assert next(t for t in ts if t.name == "Tree_Small").family == "tree"
    rec = lambda h: SD.recommended_template(ts, h).name
    assert [rec(h) for h in (10, 30, 30.01, 52.9, 60, 99, 100, 250)] == [
        "EVO_0-30mm", "EVO_0-30mm", "EVO_30-60mm", "EVO_30-60mm", "EVO_30-60mm",
        "EVO_60-100mm", "EVO_60-100mm", "EVO_100+mm"]
    assert t.fits_height(30) and t.fits_height(60) and not t.fits_height(61) and not t.fits_height(29)


def test_printer_subfolder(tmp_path):
    template(tmp_path / "EVO_0-30mm.satsup", "EVO_0-30mm")
    (tmp_path / "ELEGOO Jupiter 2").mkdir()
    template(tmp_path / "ELEGOO Jupiter 2" / "EVO_0-30mm.satsup", "EVO_0-30mm", plat_h=0.7)
    assert SD.load_templates(tmp_path, "ELEGOO Jupiter 2")[0].raft["platHeight"] == 0.7
    assert SD.load_templates(tmp_path, "ELEGOO Saturn 4 Ultra 16K")[0].printer is None


def test_hollow_and_drain_defaults():
    h = SD.HollowSettings()
    assert (h.thickness, h.accuracy, h.hollow_type, h.surface_angle, h.max_cavities) == (1.0, 1.0, "inside", 45.0, 1)
    assert h.validate() == []
    assert SD.HollowSettings(surface_angle=90, hollow_type="x").validate() == [
        "unknown hollow type 'x'", "surface angle must be 5–85°"]
    d = SD.DrainHoleSettings()
    assert (d.radius1, d.radius2, d.extend1, d.extend2, d.direction) == (2.0, 2.0, 2.0, 2.0, (1.0, 0.0, 0.0))


@pytest.mark.skipif(not (INSTALL / "resources/script/common").is_dir(), reason="needs GOOSLICER_SATELLITE_DIR")
def test_real_templates_and_arc_sample():
    ts = SD.load_templates(INSTALL / "resources/script/common", "ELEGOO Saturn 4 Ultra 16K")
    names = [t.name for t in ts]
    assert len(ts) == 7 and "EVO_30-60mm" in names and "Tree_Medium" in names
    rec = SD.recommended_template(ts, 52.9)             # arc-1a-attitude-4.stl height
    assert rec.name == "EVO_30-60mm"
    assert rec.raft["platHeight"] == 0.5 and rec.structure["area.criticalAngle"] == 50.0
    sample = Path(__file__).parent.parent / "samples" / "arc.goo"
    if sample.exists():                                 # raft = first platHeight/layer_h layers
        from elegoo_goo import goo, rle
        import numpy as np
        g = goo.read(sample)
        W, H = g.header.resolution_x, g.header.resolution_y
        lit = [int((np.frombuffer(bytes(rle.decode(g.layers[i].data, W, H, strict=False)), np.uint8) > 0).sum())
               for i in (9, 10)]
        n_raft = round(rec.raft["platHeight"] / g.header.layer_height)
        assert n_raft == 10 and lit[0] > 5 * lit[1]   # layer 9 is raft, layer 10 is pillars
