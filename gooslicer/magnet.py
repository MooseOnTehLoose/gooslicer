"""Magnet pockets (11 §1): a blind round pocket cut into a part's surface for a disc magnet.
Qt-free; the cutter is a Slot-form drain hole (hollow.DrainHole)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import trimesh

from .hollow import DrainHole, drill, hole_length
from .support_defaults import DrainHoleSettings

# common neodymium disc magnets: (diameter, thickness) in mm
MAGNET_SIZES: tuple[tuple[float, float], ...] = (
    (2, 1), (3, 1), (3, 2), (4, 2), (4, 3), (5, 1), (5, 2), (5, 3), (6, 2), (6, 3), (8, 2), (8, 3),
    (10, 2), (10, 3), (10, 5), (12, 3), (12, 5), (15, 3), (20, 3), (20, 5))
CUSTOM = "custom"
OUTSIDE = 0.5            # mm the cutter starts outside the surface, so the pocket opens cleanly
MIN_WALL = 0.8           # mm of material wanted behind the pocket


def size_name(d: float, t: float) -> str:
    return f"{d:g} × {t:g} mm"


@dataclass
class MagnetSettings:
    size: str = size_name(6, 3)        # a size_name() of MAGNET_SIZES, or CUSTOM
    clearance: float = 0.15
    diameter: float = 6.3              # the pocket's
    depth: float = 3.3

    def choose(self, d: float, t: float) -> None:
        """Pick a magnet size: the pocket is the magnet plus the clearance (twice it in depth)."""
        self.size = size_name(d, t)
        self.diameter = round(d + 2 * self.clearance, 4)
        self.depth = round(t + 2 * self.clearance, 4)

    def magnet(self) -> tuple[float, float] | None:
        return next(((d, t) for d, t in MAGNET_SIZES if size_name(d, t) == self.size), None)

    def validate(self) -> list[str]:
        errs = []
        if not 0.5 <= self.diameter <= 100:
            errs.append("pocket diameter must be 0.5–100 mm")
        if not 0.2 <= self.depth <= 50:
            errs.append("pocket depth must be 0.2–50 mm")
        if not 0 <= self.clearance <= 1:
            errs.append("clearance must be 0–1 mm")
        return errs

    def hole_settings(self) -> DrainHoleSettings:
        r = self.diameter / 2
        return DrainHoleSettings(radius1=r, radius2=r, extend1=OUTSIDE, extend2=0.0, hole_form="slot",
                                 depth=float(self.depth), shape="cylinder")


def pocket(point, normal, s: MagnetSettings) -> DrainHole:
    """The pocket's cutter at a surface point, going in against the (outward) face normal."""
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    return DrainHole(tuple(map(float, point)), tuple(map(float, -n)), s.hole_settings())


def wall_behind(mesh: trimesh.Trimesh, point, normal, s: MagnetSettings) -> float | None:
    """Material left behind the pocket's bottom along its axis (mm), or None if the ray meets
    nothing (the far side is open)."""
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    d = hole_length(mesh, point, -n)
    return None if d <= 0 else d - float(s.depth)


def cut_pocket(mesh: trimesh.Trimesh, point, normal, s: MagnetSettings) -> tuple[trimesh.Trimesh, float | None]:
    """(the mesh with the pocket cut, the wall left behind it)."""
    errs = s.validate()
    if errs:
        raise ValueError("; ".join(errs))
    wall = wall_behind(mesh, point, normal, s)
    return drill(mesh, [pocket(point, normal, s)]), wall
