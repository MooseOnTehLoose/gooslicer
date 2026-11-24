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
