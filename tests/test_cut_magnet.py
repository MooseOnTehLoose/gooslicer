"""Planar cuts with pegs and sockets, and magnet pockets (11 §1–2)."""
import math

import numpy as np
import pytest
import trimesh

from gooslicer import cut as C
from gooslicer import magnet as M


def _box(size=(20, 20, 10)):
    return trimesh.creation.box(size)


def test_magnet_sizes_and_pocket():
    s = M.MagnetSettings()
    assert (s.diameter, s.depth) == (6.3, 3.3) and s.magnet() == (6, 3)
    s.choose(10, 5)
    assert (s.size, s.diameter, s.depth) == ("10 × 5 mm", 10.3, 5.3)
    m, wall = M.cut_pocket(_box(), (0, 0, 5), (0, 0, 1), s)
    assert m.is_watertight
    r = s.diameter / 2 / math.cos(math.pi / 50)                # clearance across the flats
    removed = _box().volume - m.volume
    assert removed == pytest.approx(math.pi * (s.diameter / 2) ** 2 * s.depth, rel=0.01)
    assert removed <= math.pi * r ** 2 * s.depth
    assert wall == pytest.approx(10 - 5.3)
    # a pocket on the side goes in along the side's normal
    side, _ = M.cut_pocket(_box(), (10, 0, 0), (1, 0, 0), M.MagnetSettings())
    assert side.bounds[1][0] == pytest.approx(10) and side.volume < _box().volume


def test_magnet_wall_warning_and_limits():
    thin = _box((20, 20, 3))
    _, wall = M.cut_pocket(thin, (0, 0, 1.5), (0, 0, 1), M.MagnetSettings(diameter=6, depth=2.5))
    assert wall == pytest.approx(0.5) and wall < M.MIN_WALL
    with pytest.raises(ValueError):
        M.cut_pocket(thin, (0, 0, 1.5), (0, 0, 1), M.MagnetSettings(diameter=200))


def test_plain_cut_halves_are_closed_and_add_up():
    b = _box()
    lo, up, n = C.cut(b, (0, 0, 1), (0, 0, 1), C.CutSettings(gap=4))
    assert n == 0 and lo.is_watertight and up.is_watertight
    assert lo.volume + up.volume == pytest.approx(b.volume)
    assert lo.bounds[1][2] == pytest.approx(-1) and up.bounds[0][2] == pytest.approx(3)
    with pytest.raises(C.CutError):
        C.cut(b, (0, 0, 20), (0, 0, 1))


@pytest.mark.parametrize("kind", ["round", "square"])
@pytest.mark.parametrize("peg_on", ["lower", "upper"])
def test_peg_and_socket(kind, peg_on):
    b = _box()
    s = C.CutSettings(connector=kind, peg_on=peg_on, gap=0)
    lo, up, n = C.cut(b, (0, 0, 0), (0, 0, 1), s)
    assert n == 1 and lo.is_watertight and up.is_watertight
    w, ws, h, hs = s.size, s.size + 2 * s.clearance, s.height, s.height + 2 * s.clearance
    peg = (math.pi * (w / 2) ** 2 if kind == "round" else w * w) * h
    sock = (math.pi * (ws / 2) ** 2 if kind == "round" else ws * ws) * hs
    pegged, socketed = (lo, up) if peg_on == "lower" else (up, lo)
    assert pegged.volume - b.volume / 2 == pytest.approx(peg, rel=0.01)
    assert b.volume / 2 - socketed.volume == pytest.approx(sock, rel=0.01)
    # the peg fits in the socket: assembled (gap 0) they don't overlap
    inter = trimesh.boolean.intersection([pegged, socketed], engine="manifold")
    assert inter.is_empty or abs(inter.mass_properties.volume) < 1e-6


def test_connectors_spread_and_skip():
    b = _box((40, 20, 10))
    _, _, n = C.cut(b, (0, 0, 0), (0, 0, 1), C.CutSettings(connector="round", count=4))
    assert n == 4
    _, _, n = C.cut(b, (0, 0, 0), (0, 0, 1), C.CutSettings(connector="round", count=4, size=12))
    assert n == 2                                              # 7.65 mm inset: 24.7 × 4.7 left
    _, _, n = C.cut(_box((30, 20, 10)), (0, 0, 0), (0, 0, 1), C.CutSettings(connector="round", count=4, size=14))
    assert n == 1                                              # room for one only
    thin = _box((3, 20, 10))
    lo, up, n = C.cut(thin, (0, 0, 0), (0, 0, 1), C.CutSettings(connector="square"))
    assert n == 0 and lo.volume + up.volume == pytest.approx(thin.volume)


def test_tilted_plane_and_frame():
    R = C.plane_frame((0, 0, 1))
    assert np.allclose(R, np.eye(3))
    R = C.plane_frame((1, 1, 0))
    assert np.allclose(R @ R.T, np.eye(3)) and np.allclose(R[2], np.array([1, 1, 0]) / math.sqrt(2))
    b = _box()
    lo, up, n = C.cut(b, (0, 0, 0), (1, 1, 0), C.CutSettings(connector="round", gap=0))
    assert n == 1 and lo.is_watertight and up.is_watertight
    assert lo.volume + up.volume == pytest.approx(b.volume, rel=0.02)
    assert C.extent(b, (0, 0, 1)) == (-5.0, 5.0)
