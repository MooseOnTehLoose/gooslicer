import numpy as np
import pytest
import trimesh

from gooslicer import modify


def sphere():
    return trimesh.creation.icosphere(subdivisions=4, radius=10)


def top_face(m):
    return int(np.argmax(m.triangles_center[:, 2]))


def test_grow_region_cap_grows_with_spread():
    m = sphere()
    s = modify.surface(m, np.eye(4))
    f = top_face(m)
    sizes = []
    for deg in (5, 35, 90):
        r = modify.grow_region(s, f, deg)
        assert r[f]
        assert np.all(s.normals[r] @ [0, 0, 1] > np.cos(np.radians(deg)) - 0.05)
        sizes.append(r.sum())
    assert sizes[0] < sizes[1] < sizes[2]
    # 90°: the upper half
    assert abs(sizes[2] / len(m.faces) - 0.5) < 0.05


def test_world_normals_follow_placement():
    m = sphere()
    M = np.eye(4)
    M[:3, :3] = trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])[:3, :3]
    s = modify.surface(m, M)
    f = int(np.argmax(s.centers[:, 2]))
    assert modify.seed_normal(s, f) @ [0, 0, 1] > 0.99


def _mf(m):
    import manifold3d as m3
    return m3.Manifold(m3.Mesh(np.asarray(m.vertices, np.float32), np.asarray(m.faces, np.uint32)))


def _tm(mf):
    return modify.to_trimesh(mf)


def hollow_ball(r=12.0, wall=1.5):
    return _tm(_mf(trimesh.creation.icosphere(subdivisions=4, radius=r))
               - _mf(trimesh.creation.icosphere(subdivisions=4, radius=r - wall)))


def test_fill_holes_takes_surrounded_patches_only():
    m = hollow_ball()
    s = modify.surface(m, np.eye(4))
    cap = modify.grow_region(s, top_face(m), 40)
    holey = cap.copy()
    inside = np.nonzero(cap & (s.centers[:, 2] > 11.8))[0]
    holey[inside[: len(inside) // 3]] = False                  # punch holes near the top
    filled = modify.fill_holes(s, holey)
    assert np.array_equal(filled, cap)                          # holes back, nothing else
    assert not filled[s.normals[:, 2] < -0.5].any()             # the inside shell untouched


def test_fill_holes_leaves_the_ends_of_a_ring_alone():
    """A band selected all the way round a cylinder splits it in two: neither end is a hole."""
    v, f = trimesh.remesh.subdivide_to_size(*(lambda c: (c.vertices, c.faces))(
        trimesh.creation.cylinder(radius=10, height=40, sections=48)), max_edge=2.0)
    m = trimesh.Trimesh(v, f)
    s = modify.surface(m, np.eye(4))
    ring = (np.abs(s.centers[:, 2] - 5) < 2) & (np.abs(s.normals[:, 2]) < 0.5)
    assert np.array_equal(modify.fill_holes(s, ring), ring)
    holey = ring.copy()
    mid = np.nonzero(ring & (np.abs(s.centers[:, 2] - 5) < 0.6))[0]
    holey[mid[::7]] = False                                     # pinholes in its middle still close
    assert holey.sum() < ring.sum()
    assert np.array_equal(modify.fill_holes(s, holey), ring)


def test_cutter_follows_outline_and_opens_the_cavity():
    m = hollow_ball()
    s = modify.surface(m, np.eye(4))
    mask = modify.grow_region(s, top_face(m), 40)
    tool, n = modify.cutter(s, m.faces, mask, depth=3.0, smooth=0.3)
    assert n @ [0, 0, 1] > 0.999
    keep = m.vertices[np.unique(m.faces[mask])]
    rest, piece, dropped = modify.cut(m, tool, keep)
    assert rest.is_watertight and piece.is_watertight and dropped == 0
    assert abs(rest.volume + piece.volume - m.volume) < 1e-3 * m.volume
    # the piece is a curved cap of wall (the cut goes into the cavity: no flat floor), its rim
    # where the outline was: about 12·sin 40° across
    assert piece.bounds[1][2] == pytest.approx(12, abs=0.05)
    width = piece.extents[0] / 2
    assert abs(width - 12 * np.sin(np.radians(40))) < 0.6
    assert piece.volume < 0.2 * m.volume
    # the cut walls run straight along the direction: the piece's lowest points are where they
    # meet the inside of the wall (radius 10.5), and there is no floor below that
    rmax = float(np.max(np.linalg.norm(piece.vertices[:, :2], axis=1)))
    assert piece.bounds[0][2] == pytest.approx(np.sqrt(10.5 ** 2 - rmax ** 2), abs=0.15)


def test_cut_returns_unselected_bits_and_drops_debris():
    import manifold3d as m3
    base = _mf(trimesh.creation.box((30, 10, 5)).apply_translation([0, 0, 2.5]))
    for x in (-12.5, 12.5):
        base = base + _mf(trimesh.creation.box((5, 10, 20)).apply_translation([x, 0, 15]))
    u = _tm(base)
    tool = _mf(trimesh.creation.box((40, 20, 10)).apply_translation([0, 0, 25]))
    left_top = np.array([[-15, -5, 25], [-10, 5, 25]])
    rest, piece, dropped = modify.cut(u, tool, left_top)
    assert piece.bounds[0][0] == pytest.approx(-15) and piece.bounds[1][0] == pytest.approx(-10)
    assert rest.bounds[1][2] == pytest.approx(25)              # the right arm's top stayed
    assert abs(rest.volume + piece.volume - u.volume) < 1e-3 and dropped == 0


def test_gap_shrinks_the_piece():
    m = hollow_ball()
    s = modify.surface(m, np.eye(4))
    mask = modify.grow_region(s, top_face(m), 40)
    keep = m.vertices[np.unique(m.faces[mask])]
    tool, _ = modify.cutter(s, m.faces, mask, depth=3.0, smooth=0.3)
    rest0, piece0, _ = modify.cut(m, tool, keep)
    inner, _ = modify.cutter(s, m.faces, mask, depth=3.0, smooth=0.3, grow=-0.2)
    rest, piece, _ = modify.cut(m, tool, keep, inner)
    assert piece0.extents[0] - piece.extents[0] == pytest.approx(0.4, abs=0.05)
    assert rest.volume == pytest.approx(rest0.volume)          # the hole stays full size
    assert rest.volume + piece.volume < m.volume - 0.1


def test_cut_refuses_open_mesh_and_miss():
    m = trimesh.creation.box((10, 10, 10))
    far = _mf(trimesh.creation.box((2, 2, 2)).apply_translation([0, 0, 50]))
    with pytest.raises(modify.CutError):
        modify.cut(m, far, [[0, 0, 5]])
    broken = trimesh.Trimesh(m.vertices, m.faces[:-2], process=False)
    with pytest.raises(modify.CutError):
        modify.cut(broken, far, [[0, 0, 5]])


def test_lay_rotation():
    for n in ([0, 0, 1], [1, 0, 0], [0, 0, -1], [0.3, -0.5, 0.8]):
        n = np.asarray(n, float) / np.linalg.norm(n)
        R = modify.lay_on_plane_rotation(n)
        assert np.allclose(R @ n, [0, 0, 1]) and np.isclose(np.linalg.det(R), 1)


def _step(edge=0.4):
    """A 20×14×4 slab with a 4×4×6 post standing in its middle, finely meshed. The slab's top
    meets the post along four valley edges; every other edge is a ridge."""
    base = _mf(trimesh.creation.box((20, 14, 4)).apply_translation([0, 0, 2]))
    wall = _mf(trimesh.creation.box((4, 4, 6)).apply_translation([0, 0, 7]))
    m = _tm(base + wall)
    v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge=edge)
    out = trimesh.Trimesh(v, f)
    out.merge_vertices()
    return out


def test_contours_find_valleys_not_ridges():
    m = _step()
    s = modify.surface(m, np.eye(4))
    c = modify.contours(s, 10)
    mid = s.vertices[s.edges[c]].mean(axis=1)
    assert c.any()
    # every contour edge lies along a valley: round the post's foot (|x| or |y| = 2, z = 4)
    assert np.allclose(np.maximum(abs(mid[:, 0]), abs(mid[:, 1])), 2, atol=0.6)
    assert np.allclose(mid[:, 2], 4, atol=0.6)


def test_selection_stops_at_contours():
    m = _step()
    s = modify.surface(m, np.eye(4))
    seed = int(np.argmin(np.linalg.norm(s.centers - [-7, 0, 4], axis=1)))   # on the slab's top
    free = modify.grow_region(s, seed, 91)                     # takes the 90° sides too
    stopped = modify.grow_region(s, seed, 91, barrier=modify.contours(s, 10))
    post = s.centers[:, 2] > 4.3
    assert (free & post).any()                                 # 91° climbs the post…
    top = np.abs(s.centers[:, 2] - 4) < 0.01
    assert not (stopped & post).any()                          # …the contours stop it
    assert (stopped & top).sum() == (top & (np.abs(s.normals[:, 2]) > 0.99)).sum()   # all of the slab's top


def test_grow_by_mm_along_the_surface():
    m = trimesh.creation.icosphere(subdivisions=5, radius=10)
    s = modify.surface(m, np.eye(4))
    cap = modify.grow_region(s, top_face(m), 30)

    def reach(mask):                                           # arc length from the top, max
        return float(np.max(10 * np.arccos(np.clip(s.centers[mask][:, 2] / np.linalg.norm(s.centers[mask], axis=1), -1, 1))))
    r0 = reach(cap)
    assert reach(modify.grow_by(s, cap, 2.0, m.faces)) == pytest.approx(r0 + 2.0, abs=0.35)
    assert reach(modify.grow_by(s, cap, -2.0, m.faces)) == pytest.approx(r0 - 2.0, abs=0.35)
    assert np.array_equal(modify.grow_by(s, cap, 0, m.faces), cap)
    grown = modify.grow_by(s, cap, 2.0, m.faces)
    assert np.all(grown[cap]) and not np.any(modify.grow_by(s, cap, -2.0, m.faces)[~cap])


def test_cutter_takes_only_a_skin_under_the_selection():
    """The cutter's bottom follows the selected surface at the cut depth: on a solid ball the
    piece is a curved skin about that thick (no plug reaching into the ball), and the joint is
    the cut's inside faces only."""
    m = trimesh.creation.icosphere(subdivisions=5, radius=12)
    s = modify.surface(m, np.eye(4))
    mask = modify.grow_region(s, top_face(m), 50)
    tool, n = modify.cutter(s, m.faces, mask, depth=2.0, smooth=0.3)
    keep = m.vertices[np.unique(m.faces[mask])]
    rest, piece, _ = modify.cut(m, tool, keep)
    v = piece.vertices
    under = np.sqrt(np.maximum(144 - v[:, 0] ** 2 - v[:, 1] ** 2, 0)) - v[:, 2]   # depth below the surface
    # a cell (0.25 mm) of a 50° slope rises 0.3 mm: the bottom can sit that much deeper there
    assert under.max() < 2.0 + 0.45 and under.max() > 1.6
    assert abs(rest.volume + piece.volume - m.volume) < 1e-3 * m.volume
    j = modify.joint(m, tool)
    assert j is not None
    jd = np.sqrt(np.maximum(144 - j.vertices[:, 0] ** 2 - j.vertices[:, 1] ** 2, 0)) - j.vertices[:, 2]
    assert jd.min() > -1e-3 and jd.max() < 2.4                 # inside the ball, within the skin
