"""Build-plate scene: parts, transforms, placement and volume checks (Qt-free).

Coordinates (mm): origin at the centre of the build plate, X right, Y back, Z up;
the printable volume is [-sx/2, sx/2] × [-sy/2, sy/2] × [0, sz].
A part's world transform is  T(position) · R(rotation) · S(scale)  applied to its
local mesh, which is centred on its bounding-box centre at import.
"""
from __future__ import annotations

import itertools
from dataclasses import dataclass, field

import numpy as np
import trimesh

_ids = itertools.count(1)


@dataclass
class PartState:
    position: np.ndarray
    rotation: np.ndarray
    scale: np.ndarray

    def copy(self) -> "PartState":
        return PartState(self.position.copy(), self.rotation.copy(), self.scale.copy())

    def __eq__(self, o) -> bool:
        return (isinstance(o, PartState) and np.allclose(self.position, o.position)
                and np.allclose(self.rotation, o.rotation) and np.allclose(self.scale, o.scale))


@dataclass(eq=False)
class Part:
    name: str
    mesh: trimesh.Trimesh                    # local coordinates, centred
    position: np.ndarray = field(default_factory=lambda: np.zeros(3))
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))
    scale: np.ndarray = field(default_factory=lambda: np.ones(3))
    id: int = field(default_factory=lambda: next(_ids))
    elevation: float = 0.0                   # height of the part's bottom above the plate (supports lift)
    supports: object = None                  # gooslicer.supports.SupportPlan | None
    _cache: tuple | None = field(default=None, repr=False)

    @classmethod
    def from_mesh(cls, name: str, mesh: trimesh.Trimesh) -> "Part":
        m = mesh.copy()
        centre = m.bounds.mean(axis=0)
        m.apply_translation(-centre)
        return cls(name, m, position=centre.astype(float))

    # ------------------------------------------------------------ transform
    def matrix(self) -> np.ndarray:
        M = np.eye(4)
        M[:3, :3] = self.rotation @ np.diag(self.scale)
        M[:3, 3] = self.position
        return M

    def state(self) -> PartState:
        return PartState(self.position.copy(), self.rotation.copy(), self.scale.copy())

    def set_state(self, s: PartState) -> None:
        self.position, self.rotation, self.scale = s.position.copy(), s.rotation.copy(), s.scale.copy()

    def set_mesh(self, mesh: trimesh.Trimesh) -> None:
        """Replace geometry keeping the world placement of the bounding-box centre."""
        m = mesh.copy()
        c = m.bounds.mean(axis=0)
        m.apply_translation(-c)
        self.position = self.position + self.rotation @ (self.scale * c)
        self.mesh = m

    def world_vertices(self) -> np.ndarray:
        key = (self.position.tobytes(), self.rotation.tobytes(), self.scale.tobytes(), id(self.mesh))
        if self._cache is None or self._cache[0] != key:
            v = (np.asarray(self.mesh.vertices) * self.scale) @ self.rotation.T + self.position
            self._cache = (key, v)
        return self._cache[1]

    def world_mesh(self) -> trimesh.Trimesh:
        m = trimesh.Trimesh(self.world_vertices(), self.mesh.faces, process=False)
        if np.linalg.det(self.rotation @ np.diag(self.scale)) < 0:
            m.invert()  # mirrored scale flips orientation
        return m

    def bounds(self) -> np.ndarray:
        v = self.world_vertices()
        return np.array([v.min(axis=0), v.max(axis=0)])

    def size(self) -> np.ndarray:
        b = self.bounds()
        return b[1] - b[0]


def rotation_matrix(axis, degrees: float) -> np.ndarray:
    return trimesh.transformations.rotation_matrix(np.radians(degrees), axis)[:3, :3]


def rotation_between(a, b) -> np.ndarray:
    """Smallest rotation taking unit vector a onto unit vector b."""
    a = np.asarray(a, float) / np.linalg.norm(a)
    b = np.asarray(b, float) / np.linalg.norm(b)
    v, c = np.cross(a, b), float(np.dot(a, b))
    if c > 1 - 1e-12:
        return np.eye(3)
    if c < -1 + 1e-12:  # opposite: rotate 180° about any perpendicular axis
        perp = np.cross(a, [1, 0, 0] if abs(a[0]) < 0.9 else [0, 1, 0])
        return rotation_matrix(perp / np.linalg.norm(perp), 180)
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1 + c)


class Scene:
    def __init__(self, size=(211.68, 118.37, 220.0)):
        self.size = np.asarray(size, float)
        self.parts: list[Part] = []

    # --------------------------------------------------------------- parts
    def get(self, part_id: int) -> Part | None:
        return next((p for p in self.parts if p.id == part_id), None)

    def add(self, part: Part, *, place: bool = True, index: int | None = None) -> Part:
        if place:
            part.position[:2] = 0.0
            self.drop_to_plate(part)
        if index is None:
            self.parts.append(part)
        else:
            self.parts.insert(index, part)
        return part

    def remove(self, part_id: int) -> tuple[Part, int]:
        p = self.get(part_id)
        if p is None:
            raise KeyError(part_id)
        i = self.parts.index(p)
        self.parts.pop(i)
        return p, i

    def duplicate(self, part_id: int, offset=None) -> Part:
        src = self.get(part_id)
        dup = Part(f"{src.name} copy", src.mesh, src.position.copy(), src.rotation.copy(), src.scale.copy(),
                   elevation=src.elevation, supports=src.supports)
        dup.position += np.asarray(offset if offset is not None else (src.size()[0] + 2.0, 0, 0))
        return self.add(dup, place=False)

    # ----------------------------------------------------------- placement
    def drop_to_plate(self, part: Part) -> None:
        """Rest the part on the plate, or at its support lift height when it has supports."""
        part.position[2] -= part.bounds()[0][2] - part.elevation

    def lay_flat(self, part: Part, face_index: int) -> None:
        """Rotate so the given (local) face's normal points straight down, then drop."""
        n_local = part.mesh.face_normals[face_index]
        n_world = part.rotation @ (n_local / part.scale)
        n_world /= np.linalg.norm(n_world)
        before = part.bounds().mean(axis=0)
        part.rotation = rotation_between(n_world, [0, 0, -1]) @ part.rotation
        after = part.bounds().mean(axis=0)
        part.position[:2] += (before - after)[:2]
        self.drop_to_plate(part)

    def rotate(self, part: Part, axis, degrees: float, *, drop: bool = True) -> None:
        part.rotation = rotation_matrix(axis, degrees) @ part.rotation
        if drop:
            self.drop_to_plate(part)

    def set_scale(self, part: Part, scale, *, drop: bool = True) -> None:
        s = np.asarray(scale, float)
        if np.any(s <= 0):
            raise ValueError("scale factors must be positive")
        part.scale = s
        if drop:
            self.drop_to_plate(part)

    def in_volume(self, part: Part, tol: float = 1e-6) -> bool:
        lo, hi = part.bounds()
        half = self.size[:2] / 2
        return bool(np.all(lo[:2] >= -half - tol) and np.all(hi[:2] <= half + tol)
                    and lo[2] >= -tol and hi[2] <= self.size[2] + tol)

    def arrange(self, spacing: float = 3.0, parts: list[Part] | None = None) -> bool:
        """Shelf-pack footprints (largest first), centre the layout on the plate.
        Returns False if the layout does not fit in the plate."""
        parts = list(parts or self.parts)
        if not parts:
            return True
        width = self.size[0]
        order = sorted(parts, key=lambda p: (p.size()[1], p.size()[0]), reverse=True)
        placed, x, y, row_h, max_w = [], 0.0, 0.0, 0.0, 0.0
        for p in order:
            w, d = p.size()[:2]
            if x > 0 and x + w > width:
                x, y, row_h = 0.0, y + row_h + spacing, 0.0
            placed.append((p, x, y))
            x += w + spacing
            row_h = max(row_h, d)
            max_w = max(max_w, x - spacing)
        total = np.array([max_w, y + row_h])
        origin = -total / 2
        for p, px, py in placed:
            lo = p.bounds()[0]
            p.position[:2] += np.array([origin[0] + px, origin[1] + py]) - lo[:2]
            self.drop_to_plate(p)
        return bool(np.all(total <= self.size[:2] + 1e-9))

    def place_free(self, part: Part, near=None, spacing: float = 3.0, step: float = 1.0) -> bool:
        """Move `part` (XY only) to the free spot on the plate nearest `near` (default: where it
        is), keeping `spacing` mm from the other parts' footprints. False if none fits."""
        lo, hi = part.bounds()
        half = (hi - lo)[:2] / 2
        centre = (lo + hi)[:2] / 2
        target = centre if near is None else np.asarray(near, float)[:2]
        others = [o.bounds() for o in self.parts if o is not part]
        lim = self.size[:2] / 2 - half
        if np.any(lim < 0):
            return False
        xs = np.arange(-lim[0], lim[0] + 1e-9, step)
        ys = np.arange(-lim[1], lim[1] + 1e-9, step)
        gx, gy = np.meshgrid(xs, ys)
        cand = np.column_stack([gx.ravel(), gy.ravel()])
        ok = np.ones(len(cand), bool)
        for olo, ohi in others:
            ok &= ~((np.abs(cand[:, 0] - (olo[0] + ohi[0]) / 2) < half[0] + (ohi[0] - olo[0]) / 2 + spacing)
                    & (np.abs(cand[:, 1] - (olo[1] + ohi[1]) / 2) < half[1] + (ohi[1] - olo[1]) / 2 + spacing))
        if not ok.any():
            return False
        cand = cand[ok]
        best = cand[np.argmin(np.linalg.norm(cand - target, axis=1))]
        part.position[:2] += best - centre
        return True

    def pick(self, origin, direction) -> tuple[Part | None, int, np.ndarray | None]:
        """Nearest part hit by a world-space ray: (part, local face index, world point)."""
        best = (None, -1, None, np.inf)
        o = np.asarray(origin, float)[None]
        d = np.asarray(direction, float)[None]
        for p in self.parts:
            lo, hi = p.bounds()
            if not _ray_hits_box(o[0], d[0], lo, hi):
                continue
            # intersect in local space so the mesh's cached ray index is reused
            inv = np.linalg.inv(p.matrix())
            lo_o = (inv @ np.append(o[0], 1.0))[:3]
            lo_d = inv[:3, :3] @ d[0]
            locs, _, tri = p.mesh.ray.intersects_location(lo_o[None], lo_d[None], multiple_hits=False)
            M = p.matrix()
            for loc, t in zip(locs, tri):
                w = (M @ np.append(loc, 1.0))[:3]
                dist = float(np.linalg.norm(w - o[0]))
                if dist < best[3]:
                    best = (p, int(t), w, dist)
        return best[0], best[1], best[2]


def _ray_hits_box(o, d, lo, hi) -> bool:
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / d
        t1, t2 = (lo - o) * inv, (hi - o) * inv
    tmin = np.nanmax(np.minimum(t1, t2))
    tmax = np.nanmin(np.maximum(t1, t2))
    return bool(tmax >= max(tmin, 0.0))
