"""Detected flaws (unsupported islands, sealed cavities, suction cups) as the 3D view and the Flaws tab show
them: numbered per kind in the order the check reports them, each with its own red surface.
A flaw dismissed as a false positive is remembered by where it is, so checking again (or the
checks before slicing) doesn't bring it back while the parts stay as they are."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from gooslicer.hollow import CUP

from .viewport import LABEL_CAVITY, LABEL_CUP, LABEL_ISLAND

KINDS = ("island", "cavity")
TITLES = {"island": LABEL_ISLAND, "cavity": LABEL_CAVITY}
DISMISS_TOLERANCE = 1.0          # mm: a new detection this close to a dismissed one is the same flaw


@dataclass
class Flaw:
    kind: str                    # "island" | "cavity"
    number: int                  # 1, 2, … per kind
    point: np.ndarray            # world: the island's point, the cavity's lowest point
    red: list = field(default_factory=list)   # world trimeshes drawn red
    detail: str = ""
    source: object = None        # the Island or Cavity

    @property
    def key(self) -> tuple[str, int]:
        return (self.kind, self.number)

    @property
    def is_cup(self) -> bool:
        return getattr(self.source, "kind", None) == CUP

    @property
    def title(self) -> str:
        return f"{LABEL_CUP if self.is_cup else TITLES[self.kind]} {self.number}"


def _vol(v: float) -> str:
    return f"{v:.1f} mm³" if v < 10 else f"{v:,.0f} mm³"


def island_flaws(islands, reds) -> list[Flaw]:
    return [Flaw("island", n, np.asarray(i.point, float), list(r),
                 f"z {i.z:.2f} mm, up to {i.area:.2f} mm², hangs {i.height:.2f} mm", i)
            for n, (i, r) in enumerate(zip(islands, reds), 1)]


def _cavity_detail(c) -> str:
    at = f"lowest point ({c.lowest[0]:.1f}, {c.lowest[1]:.1f}, {c.lowest[2]:.1f})"
    if getattr(c, "kind", None) == CUP:
        return f"{_vol(c.volume)}, open toward the film, {c.area:.0f} mm² across, {at}"
    return f"{_vol(c.volume)}, {at}"


def cavity_flaws(cavities, reds) -> list[Flaw]:
    return [Flaw("cavity", n, np.asarray(c.lowest, float), list(r), _cavity_detail(c), c)
            for n, (c, r) in enumerate(zip(cavities, reds), 1)]


def trapped_summary(cavities) -> str:
    """"2 sealed cavities and 1 suction cup" for a trapped-resin check's result."""
    cups = sum(getattr(c, "kind", None) == CUP for c in cavities)
    sealed = len(cavities) - cups
    parts = []
    if sealed:
        parts.append(f"{sealed} sealed cavit{'y' if sealed == 1 else 'ies'}")
    if cups:
        parts.append(f"{cups} suction cup{'' if cups == 1 else 's'}")
    return " and ".join(parts)


def trapped_advice(cavities) -> str:
    """Why the check's findings matter, and what to do, for the kinds found."""
    cups = any(getattr(c, "kind", None) == CUP for c in cavities)
    sealed = any(getattr(c, "kind", None) != CUP for c in cavities)
    why = []
    if sealed:
        why.append("a sealed cavity keeps uncured resin inside, which can crack the print or burst during curing")
    if cups:
        why.append("a suction cup faces the film, which pulls on it at every lift and can tear the print loose")
    return "; ".join(why).capitalize() + ". " + trapped_fix(cavities) + "."


def trapped_fix(cavities) -> str:
    fix = "Add a drain hole near the bottom of each (Add drain holes)"
    if any(getattr(c, "kind", None) == CUP for c in cavities):
        fix += ", or turn the part so its cups open toward the plate"
    return fix


def dismissed(point, points) -> bool:
    p = np.asarray(point, float)
    return any(float(np.linalg.norm(p - q)) <= DISMISS_TOLERANCE for q in points)
