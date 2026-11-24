"""Detected flaws (unsupported islands, sealed cavities) as the 3D view and the Flaws tab show
them: numbered per kind in the order the check reports them, each with its own red surface.
A flaw dismissed as a false positive is remembered by where it is, so checking again (or the
checks before slicing) doesn't bring it back while the parts stay as they are."""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .viewport import LABEL_CAVITY, LABEL_ISLAND

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
    def title(self) -> str:
        return f"{TITLES[self.kind]} {self.number}"


def _vol(v: float) -> str:
    return f"{v:.1f} mm³" if v < 10 else f"{v:,.0f} mm³"


def island_flaws(islands, reds) -> list[Flaw]:
    return [Flaw("island", n, np.asarray(i.point, float), list(r),
                 f"z {i.z:.2f} mm, up to {i.area:.2f} mm², hangs {i.height:.2f} mm", i)
            for n, (i, r) in enumerate(zip(islands, reds), 1)]


def cavity_flaws(cavities, reds) -> list[Flaw]:
    return [Flaw("cavity", n, np.asarray(c.lowest, float), list(r),
                 f"{_vol(c.volume)}, lowest point ({c.lowest[0]:.1f}, {c.lowest[1]:.1f}, {c.lowest[2]:.1f})", c)
            for n, (c, r) in enumerate(zip(cavities, reds), 1)]


def dismissed(point, points) -> bool:
    p = np.asarray(point, float)
    return any(float(np.linalg.norm(p - q)) <= DISMISS_TOLERANCE for q in points)
