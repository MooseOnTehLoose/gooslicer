"""Project files: 3MF + project.json round trip, other readers, damaged files (09 §2–3, §9)."""
import json
import time
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from gooslicer import project as proj
from gooslicer.mesh import load_meshes
from gooslicer.scene import Part, Scene, rotation_matrix
from gooslicer.supports import SupportParams, SupportPlan

SAMPLES = Path(__file__).resolve().parent.parent / "samples"


def _scene():
    s = Scene()
    a = s.add(Part.from_mesh("box", trimesh.creation.box((10, 20, 5))))
    a.rotation = rotation_matrix([1, 1, 0], 33.0)
    a.scale = np.array([1.5, 0.8, 2.0])
    a.position = np.array([12.5, -7.25, 4.0])
    b = s.add(Part.from_mesh("ball & <cup>", trimesh.creation.icosphere(3, 6.0)))
    b.position = np.array([-30.0, 10.0, 11.0])
    b.elevation = 5.0
    b.opacity = 0.4
    rng = np.random.default_rng(1)
    pts = rng.normal(size=(7, 3))
    nrm = pts / np.linalg.norm(pts, axis=1)[:, None]
    b.supports = SupportPlan(SupportParams(tip_length=1.25, style="tree"), pts * 6, nrm,
                             ["overhang"] * 5 + ["lowest", "manual"], "Tree_Medium")
    c = s.duplicate(b.id, offset=(25, 0, 0))     # shares b's mesh and plan
    return s, a, b, c


def _save(tmp_path, parts, **kw):
    p = proj.Project(parts, printer="ELEGOO Saturn 4 Ultra 16K",
                     print_settings={"layer_height": 0.03, "exposure": 2.2, "resin_name": "Siraya Fast"},
                     support_type="Tree_Medium", dismissed={"island": [np.array([1.0, 2.0, 3.0])], "cavity": []},
                     view={"yaw": -60.0, "pitch": 25.0}, app="GooSlicer test")
    out = tmp_path / "job.3mf"
    proj.save(out, p, **kw)
    return out


def test_round_trip(tmp_path):
    s, a, b, c = _scene()
    out = _save(tmp_path, s.parts, thumbnail=b"\x89PNG fake")
    assert proj.is_project(out) and proj.thumbnail(out) == b"\x89PNG fake"
    got = proj.load(out)
    assert [p.name for p in got.parts] == ["box", "ball & <cup>", "ball & <cup> copy"]
    for old, new in zip(s.parts, got.parts):
        assert np.array_equal(old.position, new.position)
        assert np.array_equal(old.rotation, new.rotation)
        assert np.array_equal(old.scale, new.scale)
        assert old.elevation == new.elevation and old.opacity == new.opacity
        assert np.array_equal(old.mesh.faces, new.mesh.faces)
        assert np.abs(old.mesh.vertices - new.mesh.vertices).max() <= 1e-6
        assert np.abs(old.world_vertices() - new.world_vertices()).max() <= 1e-5
    g_a, g_b, g_c = got.parts
    assert g_a.supports is None
    assert g_b.mesh is g_c.mesh and g_b.supports is g_c.supports            # shared again
    pl = g_b.supports
    assert pl.template == "Tree_Medium" and pl.kinds == b.supports.kinds
    assert pl.params == b.supports.params
    assert np.array_equal(pl.points, b.supports.points) and np.array_equal(pl.normals, b.supports.normals)
    assert got.printer == "ELEGOO Saturn 4 Ultra 16K" and got.print_settings["layer_height"] == 0.03
    assert got.support_type == "Tree_Medium" and got.view == {"yaw": -60.0, "pitch": 25.0}
    assert np.array_equal(got.dismissed["island"][0], [1, 2, 3]) and got.dismissed["cavity"] == []
    assert not got.newer
    with zipfile.ZipFile(out) as z:
        model = z.read(proj.MODEL_PATH).decode()
    assert model.count('type="model"') == 2                                   # b and c share one object
    assert model.count("<item ") == 3


def test_other_readers_place_models(tmp_path):
    s, *_ = _scene()
    sup = trimesh.creation.box((4, 4, 5))
    sup.apply_translation([-30, 10, 2.5])
    out = _save(tmp_path, s.parts, support_meshes={s.parts[1].id: sup})
    with zipfile.ZipFile(out) as z:
        assert z.read(proj.MODEL_PATH).decode().count('type="support"') == 1
    loaded = trimesh.load(str(out), force="scene", process=False)
    geoms = loaded.dump(concatenate=False)
    assert len(geoms) == 4                                                    # 3 parts + 1 support
    want = [p.bounds() for p in s.parts] + [sup.bounds]
    got = [g.bounds for g in geoms]
    for w in want:
        assert any(np.abs(w - g).max() <= 1e-5 for g in got), w
    assert proj.load(out).parts[1].supports is not None                      # support object ignored


def test_plain_3mf_is_not_a_project():
    f = SAMPLES / "sphere.3mf"
    if not f.exists():
        pytest.skip("samples/ not present")
    assert not proj.is_project(f)
    with pytest.raises(proj.ProjectError, match="missing"):
        proj.load(f)
    assert len(load_meshes(f)) == 1


def test_damaged_files(tmp_path):
    s, *_ = _scene()
    good = _save(tmp_path, s.parts)
    data = good.read_bytes()
    trunc = tmp_path / "trunc.3mf"
    trunc.write_bytes(data[: len(data) // 2])
    with pytest.raises(proj.ProjectError):
        proj.load(trunc)
    assert not proj.is_project(trunc)

    def rewrite(name, change):
        out = tmp_path / name
        with zipfile.ZipFile(good) as zi, zipfile.ZipFile(out, "w") as zo:
            for info in zi.infolist():
                b = change(info.filename, zi.read(info))
                if b is not None:
                    zo.writestr(info, b)
        return out

    no_json = rewrite("nojson.3mf", lambda n, b: None if n == proj.PROJECT_PATH else b)
    with pytest.raises(proj.ProjectError, match="missing"):
        proj.load(no_json)
    bad_json = rewrite("badjson.3mf", lambda n, b: b"{" if n == proj.PROJECT_PATH else b)
    with pytest.raises(proj.ProjectError, match="JSON"):
        proj.load(bad_json)
    bad_index = rewrite("badidx.3mf", lambda n, b: b.replace(b'v1="0"', b'v1="999999"', 1)
                        if n == proj.MODEL_PATH else b)
    with pytest.raises(proj.ProjectError, match="vertices"):
        proj.load(bad_index)

    def bump(n, b):
        if n != proj.PROJECT_PATH:
            return b
        d = json.loads(b)
        d["version"] = 99
        d["future_key"] = {"x": 1}
        return json.dumps(d).encode()
    newer = proj.load(rewrite("newer.3mf", bump))
    assert newer.newer and len(newer.parts) == 3


def test_failed_save_keeps_old_file(tmp_path, monkeypatch):
    s, *_ = _scene()
    out = _save(tmp_path, s.parts)
    before = out.read_bytes()

    def boom(*a, **k):
        raise RuntimeError("disk full")
    monkeypatch.setattr(proj, "_write_mesh", boom)
    with pytest.raises(RuntimeError):
        proj.save(out, proj.Project(s.parts))
    assert out.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_speed_large(tmp_path):
    """09 §9: ≤ 5 s each way for 2.24 M triangles (here 1.3 M, scaled, to keep the suite quick)."""
    m = trimesh.creation.icosphere(8, 20.0)          # 1,310,720 faces
    parts = [Part.from_mesh("big", m)]
    out = tmp_path / "big.3mf"
    t = time.perf_counter()
    proj.save(out, proj.Project(parts))
    t_save = time.perf_counter() - t
    t = time.perf_counter()
    got = proj.load(out)
    t_load = time.perf_counter() - t
    assert len(got.parts[0].mesh.faces) == len(m.faces)
    scale = 2.24e6 / len(m.faces)
    assert t_save * scale <= 5.0 and t_load * scale <= 5.0, (t_save, t_load)
