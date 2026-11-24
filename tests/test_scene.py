from pathlib import Path

import numpy as np
import pytest
import trimesh

from gooslicer import camera, mesh as M, scene as S

ARC = Path(__file__).resolve().parent.parent / "samples" / "arc-1a-attitude-4.stl"


# ------------------------------------------------------------------ mesh
def test_load_arc_stl():
    if not ARC.exists():
        pytest.skip("sample missing")
    (name, m), = M.load_meshes(ARC)
    assert name == "arc-1a-attitude-4" and len(m.faces) == 40074
    assert np.allclose(m.extents, [32.548, 23.437, 52.88], atol=1e-3)


def test_load_formats_roundtrip(tmp_path):
    box = trimesh.creation.box((10, 20, 30))
    for ext in ("stl", "obj", "3mf"):
        f = tmp_path / f"b.{ext}"
        box.export(f)
        (_, m), = M.load_meshes(f)
        assert np.allclose(m.extents, [10, 20, 30]) and M.analyze(m).ok, ext


def test_load_multi_object_3mf(tmp_path):
    sc = trimesh.Scene()
    sc.add_geometry(trimesh.creation.box((5, 5, 5)), "a")
    sc.add_geometry(trimesh.creation.icosphere(radius=3), "b",
                    transform=trimesh.transformations.translation_matrix((20, 0, 0)))
    f = tmp_path / "multi.3mf"
    sc.export(f)
    parts = M.load_meshes(f)
    assert len(parts) == 2
    assert max(m.bounds[1][0] for _, m in parts) == pytest.approx(23, abs=0.1)


def test_load_errors(tmp_path):
    with pytest.raises(M.MeshError):
        M.load_meshes(tmp_path / "x.step")
    bad = tmp_path / "bad.stl"
    bad.write_bytes(b"solid x\nendsolid x\n")
    with pytest.raises(M.MeshError):
        M.load_meshes(bad)


def test_analyze_good_box():
    r = M.analyze(trimesh.creation.box((10, 10, 10)))
    assert r.ok and r.watertight and r.volume == pytest.approx(1000) and r.components == 1


def test_analyze_and_repair_hole_flip_duplicate():
    box = trimesh.creation.box((10, 10, 10))
    faces = box.faces.copy()
    faces[0] = faces[0][::-1]                         # flipped triangle
    faces = np.vstack([faces[:-1], faces[2:3]])       # drop one (hole) + add duplicate
    bad = trimesh.Trimesh(box.vertices, faces, process=False)
    r = M.analyze(bad)
    assert not r.ok and r.boundary_edges > 0 and r.duplicate_faces == 1
    assert not r.winding_consistent
    fixed = M.repair(bad)
    r2 = M.analyze(fixed)
    assert r2.ok, r2.problems()
    assert r2.volume == pytest.approx(1000)


def test_repair_inverted():
    box = trimesh.creation.box((10, 10, 10))
    box.invert()
    assert M.analyze(box).inverted
    assert M.analyze(M.repair(box)).ok


def test_nonmanifold_detected():
    a = trimesh.creation.box((10, 10, 10))
    b = a.copy()
    b.apply_translation((10, 0, 0))
    m = trimesh.util.concatenate([a, b])
    m.merge_vertices()
    assert M.analyze(m).nonmanifold_edges > 0


# ------------------------------------------------------------------ scene
def make(size=(10, 20, 30)):
    return S.Part.from_mesh("box", trimesh.creation.box(size))


def test_add_places_on_plate_centre():
    sc = S.Scene()
    p = sc.add(make())
    lo, hi = p.bounds()
    assert np.allclose(lo, [-5, -10, 0]) and np.allclose(hi, [5, 10, 30])
    assert sc.in_volume(p)


def test_rotate_scale_and_drop():
    sc = S.Scene()
    p = sc.add(make())
    sc.rotate(p, [1, 0, 0], 90)
    assert np.allclose(p.size(), [10, 30, 20]) and p.bounds()[0][2] == pytest.approx(0)
    sc.set_scale(p, [2, 1, 1])
    assert np.allclose(p.size(), [20, 30, 20])
    with pytest.raises(ValueError):
        sc.set_scale(p, [0, 1, 1])


def test_lay_flat_on_slanted_face():
    tet = trimesh.Trimesh([[0, 0, 0], [10, 0, 0], [0, 10, 0], [0, 0, 10]],
                          [[0, 2, 1], [0, 1, 3], [0, 3, 2], [1, 2, 3]])
    trimesh.repair.fix_normals(tet)
    sc = S.Scene()
    p = sc.add(S.Part.from_mesh("tet", tet))
    slanted = int(np.argmax(tet.face_normals @ np.array([1, 1, 1]) / np.sqrt(3)))
    sc.lay_flat(p, slanted)
    n_world = p.rotation @ p.mesh.face_normals[slanted]
    assert np.allclose(n_world, [0, 0, -1], atol=1e-9)
    assert p.bounds()[0][2] == pytest.approx(0)
    zs = p.world_vertices()[p.mesh.faces[slanted]][:, 2]
    assert np.allclose(zs, 0, atol=1e-9)


def test_state_roundtrip_and_duplicate():
    sc = S.Scene()
    p = sc.add(make())
    s0 = p.state()
    sc.rotate(p, [0, 0, 1], 45)
    assert p.state() != s0
    p.set_state(s0)
    assert p.state() == s0
    d = sc.duplicate(p.id)
    assert d.id != p.id and d.mesh is p.mesh and d.bounds()[0][0] == pytest.approx(p.bounds()[1][0] + 2)


def test_out_of_volume():
    sc = S.Scene((50, 50, 50))
    p = sc.add(make((10, 10, 60)))
    assert not sc.in_volume(p)
    q = sc.add(make((10, 10, 10)))
    q.position[0] = 24
    assert not sc.in_volume(q)


def test_arrange_no_overlap_and_fits():
    sc = S.Scene((100, 100, 100))
    parts = [sc.add(make((20, 15, 5))) for _ in range(8)]
    assert sc.arrange(spacing=2)
    boxes = [p.bounds() for p in parts]
    for i in range(len(boxes)):
        assert sc.in_volume(parts[i])
        for j in range(i + 1, len(boxes)):
            a, b = boxes[i], boxes[j]
            overlap = np.all(a[0][:2] < b[1][:2] - 1e-9) and np.all(b[0][:2] < a[1][:2] - 1e-9)
            assert not overlap
    many = S.Scene((30, 30, 30))
    for _ in range(9):
        many.add(make((20, 20, 5)))
    assert not many.arrange()


def test_pick():
    sc = S.Scene()
    p = sc.add(make())
    hit, face, loc = sc.pick([0, 0, 100], [0, 0, -1])
    assert hit is p and loc[2] == pytest.approx(30)
    assert p.rotation @ p.mesh.face_normals[face] @ [0, 0, 1] == pytest.approx(1)
    assert sc.pick([100, 100, 100], [0, 0, -1])[0] is None


def test_set_mesh_keeps_world_placement():
    p = make()
    p.position = np.array([5.0, 6.0, 15.0])
    before = p.bounds()
    p.set_mesh(p.mesh.copy())
    assert np.allclose(p.bounds(), before)


# ----------------------------------------------------------------- camera
def test_camera_ray_through_centre_hits_target():
    cam = camera.OrbitCamera(target=np.array([10.0, 5.0, 0.0]), distance=200, yaw=30, pitch=40)
    o, d = cam.ray(400, 300, 800, 600)
    hit = camera.ray_plane(o, d, [0, 0, 0], [0, 0, 1])
    assert np.allclose(hit, [10, 5, 0], atol=1e-6)


def test_camera_projection_roundtrip():
    cam = camera.OrbitCamera(distance=150, yaw=-60, pitch=25)
    pt = np.array([12.0, -7.0, 3.0, 1.0])
    clip = cam.projection(1.5) @ cam.view() @ pt
    ndc = clip[:3] / clip[3]
    x, y = (ndc[0] + 1) / 2 * 900, (1 - ndc[1]) / 2 * 600
    o, d = cam.ray(x, y, 900, 600)
    t = np.dot(pt[:3] - o, d)
    assert np.linalg.norm(o + t * d - pt[:3]) < 1e-6


def test_camera_orbit_clamps_and_frame():
    cam = camera.OrbitCamera()
    cam.orbit(10, 200)
    assert cam.pitch == 89.0
    cam.frame([-10, -10, 0], [10, 10, 20])
    assert np.allclose(cam.target, [0, 0, 10]) and cam.distance > 20
