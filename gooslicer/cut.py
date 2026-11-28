"""Planar cuts with locating pegs and sockets (11 §2). Qt-free.

The part is moved into the plane's own frame (u, v in the plane, n its normal, the plane at
z = 0), split there with manifold3d, given its pegs and sockets, and moved back."""
from __future__ import annotations

import math
from dataclasses import dataclass

import manifold3d as m3
import numpy as np
import trimesh

from .hollow import to_manifold, to_trimesh

CONNECTORS = ("none", "round", "square")
WALL = 1.5               # mm kept between a connector's socket and the cross-section's edge
EMBED = 0.5              # mm a peg reaches into its own half, so the union fuses
ROUND_SIDES = 48


class CutError(ValueError):
    pass


@dataclass
class CutSettings:
    connector: str = "none"          # CONNECTORS
    size: float = 5.0                # peg diameter, or a square peg's side
    height: float = 4.0              # how far the peg stands out of the cut face
    clearance: float = 0.15          # socket radius (half-width) over the peg's; twice it in depth
    count: int = 1                   # 1–4
    peg_on: str = "lower"            # "lower" (negative side of the normal) or "upper"
    gap: float = 5.0                 # mm the halves are moved apart along the normal

    def validate(self) -> list[str]:
        errs = []
        if self.connector not in CONNECTORS:
            errs.append(f"unknown connector {self.connector!r}")
        if self.connector != "none":
            if not 0.5 <= self.size <= 100:
                errs.append("connector size must be 0.5–100 mm")
            if not 0.2 <= self.height <= 100:
                errs.append("connector height must be 0.2–100 mm")
            if not 0 <= self.clearance <= 2:
                errs.append("clearance must be 0–2 mm")
            if not 1 <= int(self.count) <= 4:
                errs.append("1–4 connectors")
        if self.peg_on not in ("lower", "upper"):
            errs.append("the peg goes on the lower or upper half")
        if not 0 <= self.gap <= 1000:
            errs.append("gap must be 0–1000 mm")
        return errs


def plane_frame(normal) -> np.ndarray:
    """Rows u, v, n: n the unit normal, u the world axis lying most nearly in the plane (X
    first), made perpendicular to n."""
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    axes = np.eye(3)
    k = int(np.argmin(np.abs(axes @ n) - np.array([1e-9, 0, 0])))   # ties go to X
    u = axes[k] - (axes[k] @ n) * n
    u /= np.linalg.norm(u)
    return np.vstack([u, np.cross(n, u), n])


def _to_frame(origin, R) -> np.ndarray:
    M = np.zeros((3, 4))
    M[:, :3] = R
    M[:, 3] = -R @ np.asarray(origin, float)
    return M


def _from_frame(origin, R) -> np.ndarray:
    M = np.zeros((3, 4))
    M[:, :3] = R.T
    M[:, 3] = np.asarray(origin, float)
    return M


def _inside(px, py, contours) -> np.ndarray:
    """Even-odd point-in-polygon over all contours (manifold's are disjoint, holes inside)."""
    ins = np.zeros(len(px), bool)
    for c in contours:
        x0, y0 = c[:, 0], c[:, 1]
        x1, y1 = np.roll(x0, -1), np.roll(y0, -1)
        for a, b, c2, d in zip(x0, y0, x1, y1):
            cross = ((b > py) != (d > py)) & (px < (c2 - a) * (py - b) / np.where(d == b, 1e-300, d - b) + a)
            ins ^= cross
    return ins


def _edge_distance(P, contours) -> np.ndarray:
    d = np.full(len(P), np.inf)
    for c in contours:
        A, B = c, np.roll(c, -1, axis=0)
        for s in range(0, len(A), 256):
            a, b = A[s:s + 256], B[s:s + 256]
            ab = b - a
            t = np.clip(((P[:, None, :] - a[None]) * ab[None]).sum(2) / np.maximum((ab * ab).sum(1), 1e-300), 0, 1)
            q = a[None] + t[..., None] * ab[None]
            d = np.minimum(d, np.sqrt(((P[:, None, :] - q) ** 2).sum(2)).min(axis=1))
    return d


def connector_points(section: m3.CrossSection, s: CutSettings) -> list[np.ndarray]:
    """Where the connectors go in the plane (2D): in the section inset by size/2 + clearance +
    WALL, the first farthest from the inset's edge, each next farthest from the edge and the
    ones placed; skipped when there's no room."""
    inset = section.offset(-(s.size / 2 + s.clearance + WALL), m3.JoinType.Round)
    if inset.is_empty() or inset.area() <= 0:
        return []
    contours = [np.asarray(c, float) for c in inset.to_polygons()]
    lo = np.min([c.min(axis=0) for c in contours], axis=0)
    hi = np.max([c.max(axis=0) for c in contours], axis=0)
    step = max(float((hi - lo).max()) / 160, 0.05)
    gx, gy = np.meshgrid(np.arange(lo[0], hi[0] + step, step), np.arange(lo[1], hi[1] + step, step))
    P = np.c_[gx.ravel(), gy.ravel()]
    P = P[_inside(P[:, 0], P[:, 1], contours)]
    if not len(P):
        c = np.mean(contours[0], axis=0)                     # a sliver: its centre, if inside
        return [c] if _inside(c[:1], c[1:], contours)[0] else []
    edge = _edge_distance(P, contours)
    placed: list[np.ndarray] = []
    for _ in range(int(s.count)):
        score = edge.copy()
        if placed:
            near = np.min([np.linalg.norm(P - q, axis=1) for q in placed], axis=0)
            score = np.minimum(score, near / 2)
            ok = near >= s.size + 2 * s.clearance + WALL
            if not ok.any():
                break
            score[~ok] = -np.inf
        placed.append(P[int(np.argmax(score))])
    return placed


def _connector(kind: str, width: float, z0: float, z1: float, at) -> m3.Manifold:
    h = z1 - z0
    if kind == "round":
        solid = m3.Manifold.cylinder(h, width / 2, width / 2, ROUND_SIDES)
    else:
        solid = m3.Manifold.cube((width, width, h), False).translate((-width / 2, -width / 2, 0))
    return solid.translate((float(at[0]), float(at[1]), z0))


def cut(mesh: trimesh.Trimesh, origin, normal, s: CutSettings | None = None
        ) -> tuple[trimesh.Trimesh, trimesh.Trimesh, int]:
    """(lower half, upper half, connectors placed), in the mesh's frame. Lower is the side the
    normal points away from. The halves are moved apart by s.gap along the normal."""
    s = s or CutSettings()
    errs = s.validate()
    if errs:
        raise CutError("; ".join(errs))
    R = plane_frame(normal)
    body = to_manifold(mesh).transform(_to_frame(origin, R))
    upper, lower = body.split_by_plane((0.0, 0.0, 1.0), 0.0)
    if upper.is_empty() or lower.is_empty() or upper.volume() < 1e-9 or lower.volume() < 1e-9:
        raise CutError("the plane doesn't cut the part")
    placed = 0
    if s.connector != "none":
        pts = connector_points(body.slice(0.0), s)
        placed = len(pts)
        if pts:
            depth = s.height + 2 * s.clearance
            sock_w = s.size + 2 * s.clearance
            if s.peg_on == "lower":
                pegs = [_connector(s.connector, s.size, -EMBED, s.height, p) for p in pts]
                socks = [_connector(s.connector, sock_w, -EMBED, depth, p) for p in pts]
                lower = lower + m3.Manifold.batch_boolean(pegs, m3.OpType.Add)
                upper = upper - m3.Manifold.batch_boolean(socks, m3.OpType.Add)
            else:
                pegs = [_connector(s.connector, s.size, -s.height, EMBED, p) for p in pts]
                socks = [_connector(s.connector, sock_w, -depth, EMBED, p) for p in pts]
                upper = upper + m3.Manifold.batch_boolean(pegs, m3.OpType.Add)
                lower = lower - m3.Manifold.batch_boolean(socks, m3.OpType.Add)
    back = _from_frame(origin, R)
    g = float(s.gap) / 2
    lo = to_trimesh(lower.translate((0, 0, -g)).transform(back))
    up = to_trimesh(upper.translate((0, 0, g)).transform(back))
    return lo, up, placed


def extent(mesh: trimesh.Trimesh, normal) -> tuple[float, float]:
    """The mesh's range along the normal (for the Position field)."""
    n = np.asarray(normal, float)
    n = n / np.linalg.norm(n)
    d = np.asarray(mesh.vertices) @ n
    return float(d.min()), float(d.max())


def angle_between(a, b) -> float:
    a, b = np.asarray(a, float), np.asarray(b, float)
    return math.degrees(math.acos(np.clip(a @ b / np.linalg.norm(a) / np.linalg.norm(b), -1, 1)))
