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


def test_repair_keeps_a_hollow_cavity():
    """A cavity's wall faces into the cavity; repair must not turn it outwards (that fills the
    hollow back in). Nested twice: a solid ball inside the cavity faces out again."""
    outer = trimesh.creation.icosphere(subdivisions=3, radius=10)
    cavity = trimesh.creation.icosphere(subdivisions=3, radius=8)
    cavity.invert()
    core = trimesh.creation.icosphere(subdivisions=2, radius=3)
    hollow = trimesh.util.concatenate([outer, cavity, core])
    want = outer.volume + cavity.volume + core.volume
    bad = trimesh.Trimesh(hollow.vertices, np.vstack([hollow.faces, hollow.faces[:1]]), process=False)
    assert not M.analyze(bad).ok                     # a duplicate triangle: repair runs
    fixed = M.repair(bad)
    assert M.analyze(fixed).ok and fixed.volume == pytest.approx(want, rel=1e-6)
    flipped = trimesh.util.concatenate([outer, trimesh.creation.icosphere(subdivisions=3, radius=8), core])
    assert M.repair(flipped).volume == pytest.approx(want, rel=1e-6)   # a cavity facing out is turned in


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
    assert not many.arrange(grow=False) and many.plates == 1     # no plates added: they pile up
    assert many.arrange() and many.plates == 9                    # 11 §3: one 20 mm part per plate
    assert sorted(many.plate_of(p) for p in many.parts) == list(range(9))


def test_plates_membership_and_volume():
    """11 §3: plates side by side along +X, pitch = width + 25 mm; a part belongs to the plate
    nearest its centre and is in volume only inside that plate."""
    sc = S.Scene((100, 80, 100))
    sc.plates = 3
    assert sc.pitch == 125 and list(sc.plate_offset(2)) == [250, 0, 0]
    p = sc.add(make((10, 10, 10)))
    assert sc.plate_of(p) == 0 and sc.in_volume(p)
    p.position[0] = 250
    assert sc.plate_of(p) == 2 and sc.in_volume(p) and sc.on_plate(2) == [p]
    p.position[0] = 62.5                      # in the gap between plates 1 and 2
    assert not sc.in_volume(p)
    p.position[0] = 900                       # beyond the last plate
    assert sc.plate_of(p) == 2 and not sc.in_volume(p)


def test_arrange_spills_onto_new_plates():
    sc = S.Scene((50, 50, 50))
    parts = [sc.add(make((20, 20, 5))) for _ in range(6)]       # 4 fit a plate (2 × 2)
    assert sc.arrange(spacing=2) and sc.plates == 2
    assert [len(sc.on_plate(i)) for i in range(2)] == [4, 2]
    assert all(sc.in_volume(p) for p in parts)
    sc.plates = 4                             # extra empty plates are kept
    assert sc.arrange(spacing=2) and sc.plates == 4 and len(sc.on_plate(3)) == 0


def test_place_spill_and_delete_plate():
    """New parts spill to the next plate with room, else onto a new one; deleting a plate
    shifts later plates left and re-places its parts, adding no plate (11 §3)."""
    sc = S.Scene((30, 30, 50))                # room for one 20 mm part per plate
    a = sc.add(make((20, 20, 5)))
    assert sc.place_spill(a) == (0, False)
    b = sc.add(make((20, 20, 5)))
    assert sc.place_spill(b) == (1, True) and sc.plates == 2 and sc.in_volume(b)
    sc.plates = 3
    c = sc.add(make((20, 20, 5)))
    assert sc.place_spill(c) == (2, False)    # plate 2 existed and was empty
    with pytest.raises(ValueError):
        S.Scene().delete_plate(0)
    left = sc.delete_plate(1)                 # b has nowhere to go: left beside the last plate
    assert sc.plates == 2 and sc.plate_of(c) == 1 and sc.in_volume(c)
    assert left == [b] and not sc.in_volume(b) and sc.in_volume(a)
    roomy = S.Scene((30, 30, 50))
    x = roomy.add(make((20, 20, 5)))
    roomy.plates = 2
    x.position[0] = roomy.pitch
    roomy.delete_plate(1)                     # plate 0 is empty: x moves there
    assert roomy.plates == 1 and roomy.in_volume(x)

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


def test_axis_gizmo_follows_camera_rotation():
    def ends(cam):
        return {(n, sg): (x, y, d) for n, sg, x, y, d in camera.axis_gizmo(cam.view())}
    top = ends(camera.OrbitCamera(yaw=-90, pitch=89))
    assert top[("X", 1)][0] > 0.99 and top[("Y", 1)][1] < -0.99          # X right, Y up the screen
    assert top[("Z", 1)][2] > 0.99                                        # Z toward the viewer
    front = ends(camera.OrbitCamera(yaw=-90, pitch=0, target=np.array([50.0, 20, 0]), distance=900))
    assert front[("X", 1)][0] > 0.99 and front[("Z", 1)][1] < -0.99      # pan/zoom don't matter
    assert front[("Y", 1)][2] < -0.99                                     # +Y points away
    order = [d for *_, d in camera.axis_gizmo(camera.OrbitCamera(yaw=35, pitch=55).view())]
    assert order == sorted(order) and len(order) == 6                    # drawn far to near


def test_place_free_finds_the_nearest_gap():
    import trimesh
    from gooslicer.scene import Part, Scene
    sc = Scene(size=(100, 60, 100))
    a = sc.add(Part.from_mesh("a", trimesh.creation.box((20, 20, 10))))
    b = Part.from_mesh("b", trimesh.creation.box((20, 20, 10)))
    sc.add(b)                                         # on top of a, at the centre
    assert sc.place_free(b, near=[0, 0])
    (alo, ahi), (blo, bhi) = a.bounds(), b.bounds()
    gap = np.maximum(blo[:2] - ahi[:2], alo[:2] - bhi[:2]).max()
    assert 3 - 1e-6 <= gap <= 4 + 1e-6 and sc.in_volume(b)      # right next to it, 3 mm apart
    big = Part.from_mesh("big", trimesh.creation.box((95, 50, 5)))
    sc.add(big)
    assert not sc.place_free(big)                     # no room anywhere


def test_repair_removes_walls_debris_and_fins_and_closes_big_holes():
    cyl = trimesh.creation.cylinder(5, 10, sections=48)
    top = np.flatnonzero(np.isclose(cyl.triangles_center[:, 2], 5))
    f = np.delete(cyl.faces, top, axis=0)                 # a 48-sided hole
    v = cyl.vertices
    n = len(v)
    # a zero-thickness wall (a triangle and its reverse), a speck, and a fin on an edge
    v = np.vstack([v, [[20, 0, 0], [21, 0, 0], [20, 1, 0]],
                   trimesh.creation.box((0.05, 0.05, 0.05)).vertices + 30,
                   [[0, 0, 0]]])
    speck = trimesh.creation.box((0.05, 0.05, 0.05)).faces + n + 3
    e = f[0][:2]
    v[n + 11] = v[e].mean(axis=0) * 0.98                  # a sliver sticking inwards off a side edge
    f = np.vstack([f, [[n, n + 1, n + 2], [n + 2, n + 1, n]], speck, [[e[0], e[1], n + 11]]])
    bad = trimesh.Trimesh(v, f, process=False)
    r = M.analyze(bad)
    assert r.boundary_edges >= 48 and r.duplicate_faces == 1 and r.nonmanifold_edges == 1
    steps = []
    fixed = M.repair(bad, progress=steps.append)
    r2 = M.analyze(fixed)
    assert r2.ok and r2.components == 1, r2.problems()
    assert r2.volume == pytest.approx(cyl.volume, rel=1e-3)
    assert steps[0] == "welding vertices" and "closing holes" in steps and steps[-1] == "turning shells outwards"


def test_repair_keeps_separate_solids_and_small_real_parts():
    a = trimesh.creation.box((10, 10, 10))
    b = trimesh.creation.box((0.5, 0.5, 0.5))              # small but real: 0.125 mm³
    b.apply_translation((20, 0, 0))
    m = trimesh.util.concatenate([a, b])
    fixed = M.repair(m)
    r = M.analyze(fixed)
    assert r.ok and r.components == 2 and r.volume == pytest.approx(1000.125)


def test_ray_triangles_matches_trimesh():
    from gooslicer.scene import ray_triangles
    m = trimesh.creation.icosphere(4, 10)
    m.apply_translation((3, -2, 7))
    rng = np.random.default_rng(3)
    for _ in range(100):
        d = rng.normal(size=3)
        d /= np.linalg.norm(d)
        o = np.array([3, -2, 7]) - d * 40 + rng.normal(size=3) * 6
        got = ray_triangles(m, o, d)
        loc, _, tri = m.ray.intersects_location(o[None], d[None], multiple_hits=False)
        assert (got is None) == (len(loc) == 0)
        if got is not None:
            assert np.allclose(got[1], loc[0], atol=1e-6)
    assert ray_triangles(m, [0, 0, 100], [0, 0, 1]) is None          # pointing away


def test_slim_drops_caches_but_keeps_normals():
    from gooslicer import mesh as M
    m = trimesh.creation.icosphere(3)
    m.triangles, m.triangles_cross, m.area_faces, m.edges_sorted  # noqa: B018 (fill caches)
    n = m.face_normals.copy()
    M.analyze(m)
    M.slim(m)
    assert set(m._cache.cache) <= set(M.KEEP_CACHED)
    assert np.allclose(m.face_normals, n) and m.is_watertight             # recomputed on demand
    fresh = trimesh.Trimesh(trimesh.creation.box().vertices, trimesh.creation.box().faces)
    M.slim(fresh)
    assert "face_normals" in fresh._cache.cache and "triangles" not in fresh._cache.cache


def test_checked_repairs_only_when_asked_and_broken():
    """04, repair after edits: jobs.checked repairs a broken mesh when asked, and leaves a clean
    one (or any, when off) as it is."""
    from gooslicer import jobs
    box = trimesh.creation.box((10, 10, 10))
    holey = trimesh.Trimesh(box.vertices, box.faces[:-1], process=False)
    out, rep, fixed = jobs.checked(holey, True)
    assert rep.ok and fixed and out.volume == pytest.approx(1000)
    out, rep, fixed = jobs.checked(holey, False)
    assert out is holey and not rep.ok and fixed is None
    out, rep, fixed = jobs.checked(box, True)
    assert out is box and rep.ok and fixed is None
