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
