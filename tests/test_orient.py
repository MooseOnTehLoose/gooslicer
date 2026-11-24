"""Auto Position (gooslicer/orient.py)."""
import numpy as np
import trimesh

from gooslicer import orient as O
from gooslicer.scene import rotation_between


def _turned(m, up):
    v = (rotation_between(up, [0, 0, 1]) @ np.asarray(m.vertices).T).T
    return v


def test_flat_plate_is_tilted_off_the_plate():
    plate = trimesh.creation.box((40, 40, 2))
    r = O.best_orientation(plate.vertices, plate.faces)
    assert r.improves and 35 <= r.tilt_deg <= 90
    # far fewer supports than lying flat, at the cost of some height
    assert r.best.contacts < r.current.contacts / 5 and r.best.height > r.current.height
    # no face of the turned plate is flat against the plate any more
    after = trimesh.Trimesh(_turned(plate, r.up), plate.faces)
    assert (after.face_normals[:, 2] > -np.cos(np.radians(O.FLAT_DEG))).all()


def test_lying_cylinder_stands_up():
    cyl = trimesh.creation.cylinder(5, 40)
    cyl.apply_transform(trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0]))   # on its side
    r = O.best_orientation(cyl.vertices, cyl.faces)
    assert r.improves and abs(r.up[1]) > 0.3                 # the axis is raised well off horizontal
    assert r.best.contacts < r.current.contacts / 3


def test_sphere_and_good_placement_left_alone():
    s = trimesh.creation.icosphere(3, 10)
    r = O.best_orientation(s.vertices, s.faces)
    assert not r.improves and np.allclose(r.up, [0, 0, 1])
    # a part already turned by Auto Position stays put
    plate = trimesh.creation.box((40, 40, 2))
    first = O.best_orientation(plate.vertices, plate.faces)
    v = _turned(plate, first.up)
    again = O.best_orientation(v, plate.faces)
    assert not again.improves


def test_symmetric_ties_take_the_smallest_turn():
    cube = trimesh.creation.box((10, 10, 10))
    r = O.best_orientation(cube.vertices, cube.faces)
    assert r.improves and r.up[2] > 0                         # tilted, not turned over
    assert r.tilt_deg < 90


def test_progress_and_score_terms():
    box = trimesh.creation.box((10, 10, 10))
    seen = []
    O.best_orientation(box.vertices, box.faces, progress=lambda d, n: seen.append((d, n)))
    assert seen and seen[-1][0] == seen[-1][1] == O.COARSE + O.REFINE_TOP * O.REFINE_N
    m = O._Mesh(box.vertices, box.faces)
    flat, = O.score_directions(m, np.array([[0, 0, 1.0]]), 50.0, 2.0)
    # the 10 x 10 underside, counted FLAT_COST times at one contact per 4 mm²
    assert flat.overhang_area == 100.0 and np.isclose(flat.contacts, 100 * O.FLAT_COST / 4)
    assert np.isclose(flat.height, 10) and np.isclose(flat.total, flat.contacts + O.HEIGHT_COST * 10)
    corner, = O.score_directions(m, np.array([[1.0, 1, 1]]) / np.sqrt(3), 50.0, 2.0)
    # balanced on a corner: no face needs support; the corner and its three edges do
    assert corner.overhang_area == 0 and np.isclose(corner.contacts, 1 + 3 * 10 / 2)
