"""The support planner's ray queries agree with trimesh's (slow, memory-hungry) ray tester."""
import numpy as np
import trimesh

from gooslicer.raycast import Caster, caster


def _model():
    # a closed, non-convex shape with a large base face and many small ones
    a = trimesh.creation.icosphere(4, radius=10)
    b = trimesh.creation.box((30, 30, 2))
    b.apply_translation([0, 0, -12])
    c = trimesh.creation.torus(8, 2)
    c.apply_translation([0, 0, 12])
    return trimesh.util.concatenate([a, b, c])


def test_vertical_rays_match_trimesh():
    m = _model()
    C = caster(m)
    rng = np.random.default_rng(3)
    O = rng.uniform(m.bounds[0] - 1, m.bounds[1] + 1, (400, 3))
    for up, d in ((False, [0, 0, -1.0]), (True, [0, 0, 1.0])):
        t, f = C.vertical(O, up=up)
        loc, ri, tri = m.ray.intersects_location(O, np.tile(d, (len(O), 1)), multiple_hits=False)
        ref = np.full(len(O), np.inf)
        ref[ri] = np.abs(O[ri, 2] - loc[:, 2])
        assert (np.isfinite(t) == np.isfinite(ref)).all()
        hit = np.isfinite(t)
        assert np.allclose(t[hit], ref[hit], atol=1e-9)


def test_segments_rays_contains_and_clearance_match_trimesh():
    m = _model()
    C = caster(m)
    rng = np.random.default_rng(4)
    O = rng.uniform(m.bounds[0], m.bounds[1], (300, 3))
    D = rng.normal(size=(300, 3))
    D /= np.linalg.norm(D, axis=1)[:, None]
    t, _ = C.rays(O, D, 4.0)
    loc, ri, _ = m.ray.intersects_location(O, D, multiple_hits=False)
    ref = np.full(len(O), np.inf)
    ref[ri] = np.linalg.norm(loc - O[ri], axis=1)
    ref[ref > 4.0] = np.inf
    assert (np.isfinite(t) == np.isfinite(ref)).mean() > 0.99      # exact edge grazes may differ
    both = np.isfinite(t) & np.isfinite(ref)
    assert both.sum() > 20 and np.allclose(t[both], ref[both], atol=1e-9)
    assert (C.contains(O) == m.contains(O)).all()
    _, dist, _ = trimesh.proximity.closest_point(m, O)
    assert (C.clear_of(O, 1.5) == (dist >= 1.5)).all()


def test_cached_per_mesh_and_empty():
    m = _model()
    assert caster(m) is caster(m)
    assert caster(m.copy()) is not caster(m)
    e = Caster(trimesh.Trimesh())
    t, f = e.vertical(np.zeros((2, 3)))
    assert np.isinf(t).all() and (f == -1).all()
    assert not e.contains(np.zeros((1, 3))).any() and e.clear_of(np.zeros((1, 3)), 1.0).all()


def test_far_outside_is_a_proven_lower_bound():
    """A point the distance grid calls far is outside the model and at least that far from it,
    measured exactly; most points well clear of the model are recognised."""
    m = _model()
    C = Caster(m)
    rng = np.random.default_rng(5)
    P = rng.uniform(m.bounds[0] - 3, m.bounds[1] + 3, (3000, 3))
    for need in (0.3, 1.0, 2.5):
        far = C.far_outside(P, need)
        _, d, _ = trimesh.proximity.closest_point(m, P[far])
        assert (d >= need - 1e-9).all()
        assert not C.contains(P[far]).any()
        _, dall, _ = trimesh.proximity.closest_point(m, P)
        # within the model's box (beyond it only the distance to the box is known), clear by
        # more than the bound's margin of about 2 voxels: nearly always recognised
        boxed = np.all((P >= m.bounds[0]) & (P <= m.bounds[1]), axis=1)
        clear = boxed & (dall > need + 4 * C.voxel) & ~C.contains(P)
        assert clear.sum() > 200 and far[clear].mean() > 0.95
    # just beyond the grid, where the model's box gives the bound
    top = np.c_[rng.uniform(m.bounds[0][0], m.bounds[1][0], 300), rng.uniform(m.bounds[0][1], m.bounds[1][1], 300),
                m.bounds[1][2] + rng.uniform(0.1, 1.0, 300)]
    far = C.far_outside(top, 0.5)
    _, d, _ = trimesh.proximity.closest_point(m, top[far])
    assert (d >= 0.5 - 1e-9).all()
    per_point = rng.uniform(0.2, 2.0, len(P))         # one distance per point
    far = C.far_outside(P, per_point)
    _, d, _ = trimesh.proximity.closest_point(m, P[far])
    assert (d >= per_point[far] - 1e-9).all()


def test_hits_both_ways_matches_segments():
    m = _model()
    C = Caster(m)
    rng = np.random.default_rng(6)
    A = rng.uniform(m.bounds[0] - 1, m.bounds[1] + 1, (500, 3))
    B = A + rng.normal(0, 1.5, (500, 3))
    fwd, back = C.hits_both_ways(A, B)
    assert (fwd == np.isfinite(C.segments(A, B)[0])).all()
    assert (back == np.isfinite(C.segments(B, A)[0])).all()
    assert fwd.any() and (~fwd).any()
