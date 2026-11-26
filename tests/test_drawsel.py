"""Select by Drawing (11 §2): painting, the walled-off inside, and the edge cut along creases."""
import numpy as np
import pytest
import trimesh

from gooslicer import drawsel, modify


@pytest.fixture(scope="module")
def banded():
    """A cylinder (r 15, z 0..50) with a raised band (a torus, tube r 1.6) round z = 25."""
    import manifold3d as m3
    cyl = trimesh.creation.cylinder(radius=15, height=50, sections=72)
    cyl.apply_translation((0, 0, 25))
    band = trimesh.creation.torus(major_radius=15.2, minor_radius=1.6, major_sections=72, minor_sections=16)
    band.apply_translation((0, 0, 25))

    def M(t):
        return m3.Manifold(m3.Mesh(t.vertices.astype(np.float32), t.faces.astype(np.uint32)))
    u = (M(cyl) + M(band)).to_mesh()
    v, f = trimesh.remesh.subdivide_to_size(u.vert_properties[:, :3], u.tri_verts, max_edge=1.2)
    m = trimesh.Trimesh(v, f)
    s = modify.surface(m, np.eye(4))
    r = np.hypot(s.centers[:, 0], s.centers[:, 1])
    band_faces = (r > 15.05) & (np.abs(s.centers[:, 2] - 25) < 2.5)
    return s, modify.fold_angles(s), band_faces


def _loop(s, z, wobble, gap=0.0):
    """Faces nearest a wavy loop round the cylinder at height z (±wobble), less a gap (rad)."""
    t = np.linspace(0, 2 * np.pi, 400, endpoint=False)
    t = t[t >= gap]
    pts = np.c_[15.5 * np.cos(t), 15.5 * np.sin(t), z + wobble * np.sin(5 * t)]
    _, idx = trimesh.proximity.cKDTree(s.centers).query(pts) if hasattr(trimesh.proximity, "cKDTree") else \
        __import__("scipy.spatial", fromlist=["cKDTree"]).cKDTree(s.centers).query(pts)
    return idx


def test_a_rough_drawing_round_the_band_selects_the_band(banded):
    s, fold, band = banded
    strokes = np.r_[_loop(s, 29.5, 1.2), _loop(s, 20.5, 1.2)]
    painted = drawsel.paint(s, strokes, 1.5)
    assert not painted[band].any() or painted[band].mean() < 0.2
    sel = drawsel.select(s, painted, fold, 1.5)
    A = s.areas
    assert A[sel & band].sum() / A[band].sum() > 0.97                   # the band…
    assert A[sel & ~band].sum() / A[band].sum() < 0.1                   # …and little else


def test_a_gap_in_the_drawing_is_closed(banded):
    s, fold, band = banded
    strokes = np.r_[_loop(s, 29.5, 0.5, gap=0.25), _loop(s, 20.5, 0.5)]
    sel = drawsel.select(s, drawsel.paint(s, strokes, 1.5), fold, 1.5)
    assert sel is not None and s.areas[sel & band].sum() / s.areas[band].sum() > 0.9


def test_an_open_line_walls_nothing_off(banded):
    s, fold, band = banded
    stroke = _loop(s, 40, 0.0, gap=3.0)                     # a short arc
    assert drawsel.select(s, drawsel.paint(s, stroke, 1.0), fold, 1.0) is None


def test_the_brush_paints_along_the_surface_not_through_it():
    """A thin plate: painting one side doesn't reach the other, 1 mm away through the plate."""
    v, f = trimesh.remesh.subdivide_to_size(*(lambda b: (b.vertices, b.faces))(trimesh.creation.box((30, 30, 1))),
                                            max_edge=1.0)
    s = modify.surface(trimesh.Trimesh(v, f), np.eye(4))
    top = int(np.argmin(np.linalg.norm(s.centers - (0, 0, 0.5), axis=1)))
    p = drawsel.paint(s, [top], 3.0)
    assert p[s.normals[:, 2] > 0.9].sum() > 5 and not p[s.normals[:, 2] < -0.9].any()


def test_the_ai_moves_the_edge_only_within_the_drawing(banded):
    """An AI answer of 'everything' can't take faces outside the paint; 'nothing' keeps the core."""
    s, fold, band = banded
    painted = drawsel.paint(s, np.r_[_loop(s, 29.5, 1.2), _loop(s, 20.5, 1.2)], 1.5)
    inn, out, painted = drawsel.inside(s, painted, 1.5)
    allin = drawsel.cut(s, inn, out, painted, fold, ai=np.ones(len(inn), bool))
    assert not allin[out].any() and allin[inn | painted].mean() > 0.9
    none = drawsel.cut(s, inn, out, painted, fold, ai=np.zeros(len(inn), bool))
    core = drawsel.core(s, inn, painted)
    assert none[core].all() and none[inn | painted].mean() < allin[inn | painted].mean()
