"""Select by Parts (AI) (11): reading PartField's planes, the reach from a click, the parts
offered, and (when PartField is installed) the real model on a boss and a band."""
import numpy as np
import pytest
import scipy.sparse as sp
import trimesh
from scipy.sparse import csgraph

from gooslicer import modify, partfeat


def test_read_planes_is_bilinear_and_clamps_at_the_border():
    rng = np.random.default_rng(0)
    planes = rng.normal(size=(3, 4, 5, 6)).astype(np.float32)     # (3, C, H, W)
    pts = np.array([[-1.0, -1.0, -1.0], [1.0, 1.0, 1.0], [0.1, -0.3, 0.7], [2.0, -3.0, 0.0]])
    got = partfeat.read_planes(planes, pts)

    def look(k, u, v):
        _, C, H, W = planes.shape
        x = np.clip((u + 1) / 2 * (W - 1), 0, W - 1)
        y = np.clip((v + 1) / 2 * (H - 1), 0, H - 1)
        x0, y0 = int(np.floor(x)), int(np.floor(y))
        x1, y1 = min(x0 + 1, W - 1), min(y0 + 1, H - 1)
        fx, fy = x - x0, y - y0
        p = planes[k]
        return (p[:, y0, x0] * (1 - fx) * (1 - fy) + p[:, y0, x1] * fx * (1 - fy) +
                p[:, y1, x0] * (1 - fx) * fy + p[:, y1, x1] * fx * fy)
    for i, (x, y, z) in enumerate(pts):
        want = look(0, x, y) + look(1, y, z) + look(2, x, z)
        assert np.allclose(got[i], want, atol=1e-5)


def test_normalise_turns_z_up_to_y_up_and_fits_the_box():
    v = np.array([[0, 0, 0], [10, 0, 0], [0, 20, 0], [0, 0, 40.0]])
    n, _, _ = partfeat.normalise(v)
    assert np.allclose(np.abs(n).max(), 0.9) and np.ptp(n[:, 1]) == pytest.approx(1.8)   # z → y


def _grid(nx=30, ny=30):
    """Faces of a flat grid (two triangles a cell) and their adjacency."""
    m = trimesh.creation.box((nx, ny, 1))
    v, f = trimesh.remesh.subdivide_to_size(m.vertices, m.faces, max_edge=1.0)
    m = trimesh.Trimesh(v, f)
    return m, modify.surface(m, np.eye(4))


def test_reach_is_the_threshold_component_containing_the_click():
    m, s = _grid()
    rng = np.random.default_rng(1)
    feat = rng.normal(size=(len(s.areas), 8))
    feat /= np.linalg.norm(feat, axis=1, keepdims=True)
    feat[np.linalg.norm(s.centers[:, :2], axis=1) < 6] = feat[0]          # a disc like the click
    seed = int(np.argmin(np.linalg.norm(s.centers - (0, 0, 0.5), axis=1)))
    feat = feat.astype(np.float16)
    b = partfeat.reach(s.adjacency, feat, seed)
    sim = feat.astype(np.float32) @ feat[seed].astype(np.float32)
    a = s.adjacency
    n = len(sim)
    for tau in (0.99, 0.5, 0.0, -0.5):
        ok = sim >= tau
        e = a[ok[a[:, 0]] & ok[a[:, 1]]]
        _, lab = csgraph.connected_components(sp.coo_matrix((np.ones(len(e)), (e[:, 0], e[:, 1])), shape=(n, n)),
                                              directed=False)
        want = ok & (lab == lab[seed])
        assert ((b >= tau) == want).all(), tau


def test_parts_are_nested_smallest_first_and_offer_the_clear_one():
    m, s = _grid()
    r = np.linalg.norm(s.centers[:, :2], axis=1)
    b = np.where(r < 3, 0.95, np.where(r < 8, 0.7, 0.2)).astype(np.float32)   # two clear steps
    b[s.centers[:, 2] < 0] = -1.0                                            # the far side: unreached
    found, k = partfeat.parts(s.areas, b)
    sizes = [s.areas[p].sum() for p in found]
    assert sizes == sorted(sizes) and all((found[i] <= found[i + 1]).all() for i in range(len(found) - 1))
    top = s.centers[:, 2] >= 0                                           # as b above
    for want in ((r < 3) & top, (r < 8) & top, top):                    # each step is offered
        assert any((p == want).all() for p in found)
    assert 0 <= k < len(found)


@pytest.mark.skipif(partfeat.model_path() is None, reason="PartField isn't installed (scripts/convert-partfield.sh)")
def test_partfield_finds_a_boss_on_a_block():
    import manifold3d as m3
    block = trimesh.creation.box((40, 40, 10))
    boss = trimesh.creation.cylinder(radius=6, height=6, sections=64)
    boss.apply_translation((0, 0, 8))

    def M(t):
        return m3.Manifold(m3.Mesh(t.vertices.astype(np.float32), t.faces.astype(np.uint32)))
    u = (M(block) + M(boss)).to_mesh()
    v, f = trimesh.remesh.subdivide_to_size(u.vert_properties[:, :3], u.tri_verts, max_edge=0.8)
    m = trimesh.Trimesh(v, f)
    s = modify.surface(m, np.eye(4))
    c = s.centers
    truth = (np.hypot(c[:, 0], c[:, 1]) < 6.05) & (c[:, 2] > 5.01)
    feat = partfeat.face_features(partfeat.FieldNet(), m.vertices, m.faces)
    assert feat.shape == (len(m.faces), partfeat.DIM) and np.isfinite(feat.astype(np.float32)).all()
    seed = int(np.argmax(np.where(truth, c[:, 2], -1e9)))                 # top of the boss
    found, k = partfeat.parts(s.areas, partfeat.reach(s.adjacency, feat, seed))
    A = s.areas
    assert A[found[k] & truth].sum() / A[found[k] | truth].sum() > 0.95
