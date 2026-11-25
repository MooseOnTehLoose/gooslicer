import numpy as np
import pytest
import trimesh

from gooslicer import hollow as H
from gooslicer.support_defaults import DrainHoleSettings, HollowSettings


@pytest.fixture(scope="module")
def cube():
    return trimesh.creation.box((20, 20, 20))


def test_inside_hollow_cube_wall_volume(cube):
    r = H.hollow(cube, HollowSettings(thickness=1.0, accuracy=0.5))
    assert r.mesh.is_watertight and r.cavities == 1
    assert r.mesh.volume == pytest.approx(8000 - 18 ** 3, rel=0.01)


def test_sphere_and_offset_types(cube):
    sph = trimesh.creation.icosphere(5, 10)
    r = H.hollow(sph, HollowSettings(thickness=1.0, accuracy=0.5))
    assert r.mesh.volume == pytest.approx(sph.volume * (1 - 0.729), rel=0.01)
    out = H.hollow(cube, HollowSettings(thickness=1.0, accuracy=0.5, hollow_type="outside")).mesh
    assert np.allclose(out.bounds, [[-11] * 3, [11] * 3], atol=0.05) and out.is_watertight
    cen = H.hollow(cube, HollowSettings(thickness=1.0, accuracy=0.5, hollow_type="center")).mesh
    assert np.allclose(cen.bounds, [[-10.5] * 3, [10.5] * 3], atol=0.05)
    ob = H.hollow(cube, HollowSettings(thickness=1.0, accuracy=0.5, hollow_type="open_bottom")).mesh
    assert H.sealed_cavities([ob]) == [] and ob.volume < 8000 - 18 ** 3


def test_drain_hole_clears_sealed_cavity(cube):
    shell = H.hollow(cube, HollowSettings(thickness=1.5, accuracy=0.5)).mesh
    cav = H.sealed_cavities([shell])
    assert len(cav) == 1 and cav[0].volume == pytest.approx(17 ** 3, rel=0.05)
    hole = H.DrainHole((0, 0, -10), (0, 0, 1), DrainHoleSettings())
    d = H.drill(shell, [hole])
    assert d.is_watertight and shell.volume - d.volume == pytest.approx(np.pi * 4 * 1.5, rel=0.03)
    assert H.sealed_cavities([d]) == []
    assert H.sealed_cavities([cube]) == []


def test_errors(cube):
    with pytest.raises(H.HollowError, match="surface angle"):
        H.hollow(cube, HollowSettings(surface_angle=1))
    with pytest.raises(H.HollowError, match="voxels"):
        H.hollow(cube, HollowSettings(accuracy=0.005))
    with pytest.raises(H.HollowError, match="manifold"):
        H.hollow(trimesh.Trimesh(cube.vertices, cube.faces[:-1]), HollowSettings())


def test_cavity_faces_mark_the_walls_of_sealed_cavities():
    outer = trimesh.creation.box((20, 20, 20))
    inner = trimesh.creation.box((10, 10, 10))
    inner.invert()                                   # the cavity's wall faces into the void
    shell = trimesh.util.concatenate([outer, inner])
    other = trimesh.creation.box((5, 5, 5))
    other.apply_translation((30, 0, 0))
    cav, masks = H.cavity_faces([shell, other])
    assert len(cav) == 1 and cav[0].volume == pytest.approx(1000, rel=0.05)
    assert len(masks) == 2 and masks[0].sum() == 12 and masks[0][12:].all()   # only the inner box
    assert not masks[1].any()
    assert [c.volume for c in cav] == [c.volume for c in H.sealed_cavities([shell, other])]
    cav, masks = H.cavity_faces([outer])
    assert cav == [] and not masks[0].any()
    assert H.cavity_faces([]) == ([], [])


def test_cavity_shells_seal_a_hollowed_part(cube):
    shell = H.hollow(cube, HollowSettings(thickness=1.5, accuracy=0.5)).mesh
    other = cube.copy()
    other.apply_translation((40, 0, 0))
    cav, rm = H.cavity_shells([shell, other])
    assert len(cav) == 1 and not rm[1].any()
    assert len(shell.faces) - rm[0].sum() == 12                      # all but the outer box
    solid = H.without_faces(shell, rm[0])
    assert solid.is_watertight and solid.volume == pytest.approx(8000, rel=1e-6)
    assert H.sealed_cavities([solid]) == []


def test_cavity_closed_by_another_part_is_not_removed(cube):
    cup = H.hollow(cube, HollowSettings(thickness=1.5, accuracy=0.5, hollow_type="open_bottom")).mesh
    lid = trimesh.creation.box((30, 30, 2))
    lid.apply_translation((0, 0, cup.bounds[0][2] - 1))               # closes the open bottom
    cav, rm = H.cavity_shells([cup, lid])
    assert len(cav) == 1 and not rm[0].any() and not rm[1].any()      # its wall is the outside too


def _box_shell(slit=None):
    import manifold3d as m3

    def box(size, at):
        return m3.Manifold.cube(size, center=True).translate(at)
    s = box((20, 20, 10), (0, 0, 5)) - box((16, 16, 6), (0, 0, 5))
    if slit is not None:            # a slit through two walls, between the 0.5 mm voxel centres
        s = s - box((slit, 30, 3), (0.25, 0, 5))
    m = s.to_mesh()
    return trimesh.Trimesh(np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts))


def test_narrow_gap_is_not_a_sealed_cavity(monkeypatch):
    """A gap narrower than two voxels (a crease, a slot between a model and its supports) closes
    up at the coarse pitch and the space behind it looked sealed: a false positive on supported
    models. Each candidate is confirmed at a finer pitch."""
    slit = _box_shell(0.3)
    sealed = _box_shell()
    assert len(H.sealed_cavities([sealed], pitch=0.5)) == 1
    assert H.sealed_cavities([slit], pitch=0.5) == []
    assert H.cavity_faces([slit], pitch=0.5)[0] == []
    monkeypatch.setattr(H, "_opens_out", lambda *a: False)            # without the fine check
    assert len(H.sealed_cavities([slit], pitch=0.5)) == 1



def test_cavity_faces_each_keeps_cavities_apart():
    def shell(at):
        outer = trimesh.creation.box((20, 20, 20))
        inner = trimesh.creation.box((10, 10, 10))
        inner.invert()
        m = trimesh.util.concatenate([outer, inner])
        m.apply_translation(at)
        return m
    a, b = shell((0, 0, 0)), shell((40, 0, 0))
    cav, each = H.cavity_faces_each([a, b])
    assert len(cav) == 2 and len(each) == 2
    assert sorted(tuple(int(m.sum()) for m in per_mesh) for per_mesh in each) == [(0, 12), (12, 0)]
    combined = H.cavity_faces([a, b])[1]
    assert [int(m.sum()) for m in combined] == [12, 12]


def test_hole_preview_mesh_matches_the_cut():
    import numpy as np
    import trimesh
    from gooslicer.scene import Part
    from gooslicer.support_defaults import DrainHoleSettings
    from gooslicer_qt.hollowing import hole_preview_mesh
    part = Part.from_mesh("b", trimesh.creation.box((20, 20, 20)))
    face = int(np.argmax(part.mesh.face_normals[:, 0]))               # a +X side face
    hit = np.array([part.position[0] + 10, part.position[1], part.position[2]])
    s = DrainHoleSettings(radius1=2.0, radius2=1.0, extend1=1.0, extend2=4.0)
    m = hole_preview_mesh(part, face, hit, s)
    assert m.is_watertight and m.volume > 0
    h = 5.0
    assert np.isclose(m.volume, np.pi * h / 3 * (4 + 2 + 1), rtol=0.01)   # cone frustum
    lo, hi = m.bounds
    assert np.isclose(hi[0], hit[0] + 1) and np.isclose(lo[0], hit[0] - 4)  # 1 out, 4 in (axis -X)


def _cup(lift=0.0, upside_down=False, drain=0.0, r=8.0):
    """A cylinder cup, 2 mm walls and floor, 12 mm tall."""
    outer = trimesh.creation.cylinder(radius=r + 2, height=12, sections=48)
    inner = trimesh.creation.cylinder(radius=r, height=12, sections=48)
    inner.apply_translation([0, 0, 2])
    m = H.to_manifold(outer) - H.to_manifold(inner)
    if drain:
        m = m - H.to_manifold(trimesh.creation.cylinder(radius=drain, height=30, sections=24))
    m = H.to_trimesh(m)
    if upside_down:
        m.apply_transform(trimesh.transformations.rotation_matrix(np.pi, [1, 0, 0]))
    m.apply_translation([0, 0, lift - m.bounds[0][2]])
    return m


@pytest.mark.parametrize("pose, cups", [
    (dict(), 1),                                   # upright on the plate
    (dict(lift=5), 1),                             # upright on supports
    (dict(lift=5, drain=1.0), 0),                  # a drain hole in the floor empties it
    (dict(lift=5, upside_down=True), 0),           # opens downward: resin runs out
    (dict(upside_down=True), 1),                   # rim down on the plate: closed by the plate
    (dict(lift=5, r=1.5), 0),                      # a pit too small to pull on the print
])
def test_suction_cups(pose, cups):
    """07 §5: a pocket closed on every side but the top (toward the film) is a suction cup."""
    found = H.sealed_cavities([_cup(**pose)], cups=True)
    assert [c.kind for c in found] == [H.CUP] * cups
    if cups:
        assert found[0].area == pytest.approx(np.pi * 64, rel=0.1)
        assert found[0].volume == pytest.approx(np.pi * 64 * 10, rel=0.1)
    assert H.sealed_cavities([_cup(**pose)]) == []       # not unless asked for


def test_cups_and_sealed_cavities_together():
    shell = H.to_trimesh(H.to_manifold(trimesh.creation.box((20, 20, 20)))
                         - H.to_manifold(trimesh.creation.box((14, 14, 14))))
    shell.apply_translation([30, 0, 15])
    found = H.sealed_cavities([_cup(lift=5), shell], cups=True)
    assert sorted(c.kind for c in found) == [H.CUP, H.SEALED]
    cav, each = H.cavity_faces_each([_cup(lift=5), shell], cups=True)
    cup = [k for k, c in enumerate(cav) if c.kind == H.CUP][0]
    assert each[cup][0].any() and not each[cup][1].any()      # the cup's walls are on the cup


@pytest.mark.parametrize("kind", ["bcc", "fcc"])
def test_infill_lattice(kind):
    """07 §2b: SatelLite's infill fills the cavity with struts that join the walls."""
    box = trimesh.creation.box((20, 20, 20))
    plain = H.hollow(box, HollowSettings(thickness=1.5, accuracy=0.5))
    r = H.hollow(box, HollowSettings(thickness=1.5, accuracy=0.5, infill=True, infill_type=kind))
    assert r.mesh.is_watertight and r.cavities == 1
    assert plain.mesh.volume * 1.5 < r.mesh.volume < box.volume * 0.95
    # the struts are tied into the shell: one solid piece, inner and outer surfaces together
    assert sum(c.volume() > 0 for c in H.to_manifold(r.mesh).decompose()) == 1   # the other is the void
    # a cross-section through the middle cuts struts inside the cavity
    sec = r.mesh.section([0, 0, 1], [0, 0, 1.0])
    loops = [np.ptp(loop[:, :2], axis=0).max() for loop in sec.discrete]
    assert sum(x < 10 for x in loops) >= 4                  # strut sections, besides the two walls


def test_infill_settings():
    assert HollowSettings().infill is False                     # off, as in SatelLite
    d = HollowSettings()
    assert (d.infill_type, d.infill_size, d.infill_radius) == ("bcc", 4.0, 1.0)
    assert HollowSettings(infill=True, infill_radius=1.5).validate()
    assert HollowSettings(infill=True, infill_type="gyroid").validate()
    # Open bottom ignores it, as SatelLite doesn't offer it there
    box = trimesh.creation.box((20, 20, 20))
    a = H.hollow(box, HollowSettings(thickness=1.5, accuracy=0.5, hollow_type="open_bottom"))
    b = H.hollow(box, HollowSettings(thickness=1.5, accuracy=0.5, hollow_type="open_bottom", infill=True))
    assert b.mesh.volume == pytest.approx(a.mesh.volume)
