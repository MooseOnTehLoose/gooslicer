"""Build-plate scene: parts, transforms, placement and volume checks (Qt-free).

Coordinates (mm): origin at the centre of the build plate, X right, Y back, Z up;
the printable volume is [-sx/2, sx/2] × [-sy/2, sy/2] × [0, sz].
A part's world transform is  T(position) · R(rotation) · S(scale)  applied to its
local mesh, which is centred on its bounding-box centre at import.
"""
from __future__ import annotations

import itertools
import weakref
from dataclasses import dataclass, field

import numpy as np
import trimesh

_ids = itertools.count(1)


def hull_index(mesh: trimesh.Trimesh) -> np.ndarray:
    """Indices of the mesh's convex-hull vertices: bounds under any rotation and scale come from
    these alone (a few thousand points instead of every vertex). They stay valid under any
    affine change, so they're kept on the mesh and carried over to translated copies."""
    n = len(mesh.vertices)
    h = getattr(mesh, "_gs_hull", None)
    if h is None or h[0] != n:
        idx = np.arange(n)
        if n > 64:
            try:
                from scipy.spatial import ConvexHull
                idx = ConvexHull(np.asarray(mesh.vertices)).vertices
            except Exception:  # flat or degenerate: qhull can't build a hull; use every vertex
                pass
        mesh._gs_hull = h = (n, idx)
    return h[1]


def _keep_hull(src: trimesh.Trimesh, dst: trimesh.Trimesh) -> trimesh.Trimesh:
    if getattr(src, "_gs_hull", None) is not None:
        dst._gs_hull = src._gs_hull
    return dst


GPU_KEY = "gs_gpu"


def gpu_arrays(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    """(float32 vertices, uint32 faces): what the 3D view uploads. Kept in trimesh's cache, which
    drops it when the mesh changes; the view frees it after uploading."""
    c = mesh._cache
    if GPU_KEY not in c:
        c[GPU_KEY] = (np.ascontiguousarray(mesh.vertices, np.float32),
                      np.ascontiguousarray(mesh.faces, np.uint32))
    return c[GPU_KEY]


def prepare(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Do the 3D view's per-mesh work now (on a worker thread), not when it is first drawn."""
    if mesh is not None and len(mesh.faces):
        hull_index(mesh)
        gpu_arrays(mesh)
    return mesh


@dataclass
class PartState:
    position: np.ndarray
    rotation: np.ndarray
    scale: np.ndarray
    elevation: float = 0.0          # Part.elevation: a bare part lifted off the plate keeps its lift

    def copy(self) -> "PartState":
        return PartState(self.position.copy(), self.rotation.copy(), self.scale.copy(), self.elevation)

    def __eq__(self, o) -> bool:
        return (isinstance(o, PartState) and np.allclose(self.position, o.position)
                and np.allclose(self.rotation, o.rotation) and np.allclose(self.scale, o.scale)
                and abs(self.elevation - o.elevation) < 1e-9)


@dataclass(eq=False)
class Part:
    name: str
    mesh: trimesh.Trimesh                    # local coordinates, centred
    position: np.ndarray = field(default_factory=lambda: np.zeros(3))
    rotation: np.ndarray = field(default_factory=lambda: np.eye(3))
    scale: np.ndarray = field(default_factory=lambda: np.ones(3))
    id: int = field(default_factory=lambda: next(_ids))
    elevation: float = 0.0                   # height of the part's bottom above the plate: the supports'
                                             # lift, or a bare part raised with the Z arrow
    supports: object = None                  # gooslicer.supports.SupportPlan | None
    opacity: float = 1.0                     # 3D view only: below 1 the part is drawn see-through
                                             # (hollowed parts start at the Hollow panel's value)
    repaired: bool = False                   # its mesh was repaired (on import or Repair mesh)
    _cache: tuple | None = field(default=None, repr=False)
    _bcache: tuple | None = field(default=None, repr=False)

    @classmethod
    def from_mesh(cls, name: str, mesh: trimesh.Trimesh) -> "Part":
        m = _keep_hull(mesh, mesh.copy())
        centre = m.bounds.mean(axis=0)
        m.apply_translation(-centre)
        return cls(name, m, position=centre.astype(float))

    @property
    def see_through(self) -> bool:
        return self.opacity < 1.0

    # ------------------------------------------------------------ transform
    def matrix(self) -> np.ndarray:
        M = np.eye(4)
        M[:3, :3] = self.rotation @ np.diag(self.scale)
        M[:3, 3] = self.position
        return M

    def state(self) -> PartState:
        return PartState(self.position.copy(), self.rotation.copy(), self.scale.copy(), float(self.elevation))

    def set_state(self, s: PartState) -> None:
        self.position, self.rotation, self.scale = s.position.copy(), s.rotation.copy(), s.scale.copy()
        self.elevation = float(s.elevation)

    def set_mesh(self, mesh: trimesh.Trimesh) -> None:
        """Replace geometry keeping the world placement of the bounding-box centre."""
        m = _keep_hull(mesh, mesh.copy())
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
        """World bounding box [[min xyz], [max xyz]], from the hull vertices (the 3D view asks
        for every part's bounds every frame)."""
        key = (self.position.tobytes(), self.rotation.tobytes(), self.scale.tobytes(), id(self.mesh),
               len(self.mesh.vertices))
        if self._bcache is None or self._bcache[0] != key:
            v = np.asarray(self.mesh.vertices)[hull_index(self.mesh)]
            w = (v * self.scale) @ self.rotation.T + self.position
            self._bcache = (key, np.array([w.min(axis=0), w.max(axis=0)]))
        return self._bcache[1].copy()

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


PLATE_GAP = 25.0        # mm between build plates laid side by side along +X (11 §3)


class Scene:
    def __init__(self, size=(211.68, 118.37, 220.0)):
        self.size = np.asarray(size, float)
        self.parts: list[Part] = []
        self.plates = 1                      # build plates, side by side along +X (11 §3)

    # --------------------------------------------------------------- plates
    @property
    def pitch(self) -> float:
        return float(self.size[0]) + PLATE_GAP

    def plate_offset(self, i: int) -> np.ndarray:
        """Plate i's centre on the floor (plate 0 at the origin)."""
        return np.array([i * self.pitch, 0.0, 0.0])

    def plate_at(self, x: float) -> int:
        """The plate nearest a world x."""
        return int(np.clip(round(float(x) / self.pitch), 0, self.plates - 1))

    def plate_of(self, part: "Part") -> int:
        lo, hi = part.bounds()
        return self.plate_at((lo[0] + hi[0]) / 2)

    def on_plate(self, i: int) -> list["Part"]:
        return [p for p in self.parts if self.plate_of(p) == i]

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
                   elevation=src.elevation, supports=src.supports, opacity=src.opacity,
                   repaired=src.repaired)
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
        """Inside the volume of the plate it is on (11 §3)."""
        lo, hi = part.bounds()
        c = self.plate_offset(self.plate_of(part))[:2]
        half = self.size[:2] / 2
        return bool(np.all(lo[:2] - c >= -half - tol) and np.all(hi[:2] - c <= half + tol)
                    and lo[2] >= -tol and hi[2] <= self.size[2] + tol)

    def arrange(self, spacing: float = 3.0, parts: list[Part] | None = None, grow: bool = True) -> bool:
        """Shelf-pack footprints (largest first) onto plate 0, then 1, … (11 §3), each plate's
        layout centred on it. With grow, plates are added until everything fits; without,
        what is left goes on the last plate anyway. False if some part is out of volume."""
        parts = list(parts or self.parts)
        if not parts:
            return True
        W, D = self.size[:2]
        order = sorted(parts, key=lambda p: (p.size()[1], p.size()[0]), reverse=True)
        plates: list[list] = [[]]
        x = y = row_h = 0.0
        for p in order:
            w, d = p.size()[:2]
            if x > 0 and x + w > W + 1e-9:
                x, y, row_h = 0.0, y + row_h + spacing, 0.0
            if plates[-1] and y + d > D + 1e-9 and (grow or len(plates) < self.plates):
                plates.append([])
                x = y = row_h = 0.0
            plates[-1].append((p, x, y))
            x += w + spacing
            row_h = max(row_h, d)
        self.plates = max(self.plates, len(plates))
        for i, placed in enumerate(plates):
            max_w = max(px + p.size()[0] for p, px, _ in placed)
            max_d = max(py + p.size()[1] for p, _, py in placed)
            origin = self.plate_offset(i)[:2] - np.array([max_w, max_d]) / 2
            for p, px, py in placed:
                lo = p.bounds()[0]
                p.position[:2] += origin + np.array([px, py]) - lo[:2]
                self.drop_to_plate(p)
        return all(self.in_volume(p) for p in parts)

    def place_free(self, part: Part, near=None, spacing: float = 3.0, step: float = 1.0,
                   plate: int | None = None) -> bool:
        """Move `part` (XY only) to the free spot nearest `near` (default: where it is) on its
        plate (or `plate`), keeping `spacing` mm from the other parts' footprints. False if
        none fits."""
        lo, hi = part.bounds()
        half = (hi - lo)[:2] / 2
        centre = (lo + hi)[:2] / 2
        i = self.plate_at(centre[0]) if plate is None else int(plate)
        c = self.plate_offset(i)[:2]
        target = centre if near is None else np.asarray(near, float)[:2]
        others = [o.bounds() for o in self.parts if o is not part]
        lim = self.size[:2] / 2 - half
        if np.any(lim < 0):
            return False
        xs = c[0] + np.arange(-lim[0], lim[0] + 1e-9, step)
        ys = c[1] + np.arange(-lim[1], lim[1] + 1e-9, step)
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

    def place_spill(self, part: Part, near=None, plate: int = 0, spacing: float = 3.0) -> tuple[int, bool]:
        """place_free on `plate`, else the first later plate with room, else a new plate
        (11 §3 Spillover): (the plate it went on, whether a plate was added)."""
        start = int(np.clip(plate, 0, self.plates - 1))
        for i in range(start, self.plates):
            tgt = near if i == start and near is not None else self.plate_offset(i)[:2]
            if self.place_free(part, near=tgt, spacing=spacing, plate=i):
                return i, False
        self.plates += 1
        i = self.plates - 1
        lo, hi = part.bounds()
        part.position[:2] += self.plate_offset(i)[:2] - (lo + hi)[:2] / 2
        self.place_free(part, spacing=spacing, plate=i)
        return i, True

    def delete_plate(self, i: int, spacing: float = 3.0) -> list[Part]:
        """Remove plate i (not the last one left): later plates and their parts move one pitch
        left; i's parts go where they fit on the remaining plates, first plate first, adding
        none. Returns the parts that fit nowhere (left beside the last plate)."""
        if self.plates <= 1:
            raise ValueError("there must always be at least one build plate")
        i = int(i)
        own = self.on_plate(i)
        later = [p for p in self.parts if self.plate_of(p) > i]
        for p in later:
            p.position[0] -= self.pitch
        self.plates -= 1
        others = [p for p in self.parts if p not in own]
        left = []
        for p in own:
            p.position[0] += 10 * self.pitch * self.plates + 10 * self.pitch    # out of the way
            placed = False
            for k in range(self.plates):
                if self.place_free(p, near=self.plate_offset(k)[:2], spacing=spacing, plate=k):
                    placed = True
                    break
            if not placed:
                left.append(p)
            others.append(p)
        edge = self.plate_offset(self.plates - 1)[0] + self.size[0] / 2 + PLATE_GAP / 2
        for p in left:                         # beside the last plate, in the gap (out of volume)
            lo, hi = p.bounds()
            p.position[0] += edge + (hi[0] - lo[0]) / 2 - (lo[0] + hi[0]) / 2 + 1.0
            edge += (hi[0] - lo[0]) + spacing
        return left

    def pick(self, origin, direction) -> tuple[Part | None, int, np.ndarray | None]:
        """Nearest part hit by a world-space ray: (part, local face index, world point)."""
        best = (None, -1, None, np.inf)
        o = np.asarray(origin, float)[None]
        d = np.asarray(direction, float)[None]
        for p in self.parts:
            lo, hi = p.bounds()
            if not _ray_hits_box(o[0], d[0], lo, hi):
                continue
            # intersect in local space, where the mesh's triangle arrays are cached
            inv = np.linalg.inv(p.matrix())
            lo_o = (inv @ np.append(o[0], 1.0))[:3]
            lo_d = inv[:3, :3] @ d[0]
            hit = ray_triangles(p.mesh, lo_o, lo_d)
            if hit is None:
                continue
            t, loc = hit
            w = (p.matrix() @ np.append(loc, 1.0))[:3]
            dist = float(np.linalg.norm(w - o[0]))
            if dist < best[3]:
                best = (p, int(t), w, dist)
        return best[0], best[1], best[2]


_TRI: dict[int, tuple] = {}


def _triangles(mesh: trimesh.Trimesh):
    """(v0, edge1, edge2, centre, radius²) of every face, float32, cached per mesh object
    (meshes are replaced, not edited)."""
    k = id(mesh)
    c = _TRI.get(k)
    if c is None or c[0]() is not mesh or len(c[1]) != len(mesh.faces):
        tri = np.asarray(mesh.vertices, np.float32)[np.asarray(mesh.faces)]
        v0 = tri[:, 0]
        centre = tri.mean(axis=1)
        r2 = ((tri - centre[:, None]) ** 2).sum(axis=2).max(axis=1)
        c = (weakref.ref(mesh), v0, tri[:, 1] - v0, tri[:, 2] - v0, centre, r2 * 1.0001 + 1e-12)
        _TRI[k] = c
        weakref.finalize(mesh, _TRI.pop, k, None)
    return c[1:]


def ray_triangles(mesh: trimesh.Trimesh, origin, direction):
    """The first face a ray hits: (face index, point), or None. Every face's bounding sphere is
    tested against the ray at once, then Möller–Trumbore runs on the few left: a few ms for 300k
    faces, with no index to build first (trimesh's ray index takes about a second, which stalled
    picking right after a mesh changed)."""
    if not len(mesh.faces):
        return None
    v0, e1, e2, centre, r2 = _triangles(mesh)
    o = np.asarray(origin, np.float64)
    d = np.asarray(direction, np.float64)
    dn = d / np.linalg.norm(d)
    # squared distance from each face centre to the ray's line, against the face's radius
    w = centre - o.astype(np.float32)
    along = w @ dn.astype(np.float32)
    near = np.flatnonzero(((w * w).sum(axis=1) - along * along <= r2) & (along >= -np.sqrt(r2)))
    if not len(near):
        return None
    v0, e1, e2 = v0[near].astype(np.float64), e1[near].astype(np.float64), e2[near].astype(np.float64)
    p = np.cross(d, e2)
    det = np.einsum("ij,ij->i", e1, p)
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / det
        s = o - v0
        u = np.einsum("ij,ij->i", s, p) * inv
        q = np.cross(s, e1)
        v = (q @ d) * inv
        t = np.einsum("ij,ij->i", e2, q) * inv
        ok = (np.abs(det) > 1e-12) & (u >= -1e-6) & (v >= -1e-6) & (u + v <= 1 + 1e-6) & (t > 1e-9)
    idx = np.flatnonzero(ok)
    if not len(idx):
        return None
    i = int(idx[np.argmin(t[idx])])
    return int(near[i]), o + d * t[i]


def _ray_hits_box(o, d, lo, hi) -> bool:
    with np.errstate(divide="ignore", invalid="ignore"):
        inv = 1.0 / d
        t1, t2 = (lo - o) * inv, (hi - o) * inv
    tmin = np.nanmax(np.minimum(t1, t2))
    tmax = np.nanmin(np.maximum(t1, t2))
    return bool(tmax >= max(tmin, 0.0))
