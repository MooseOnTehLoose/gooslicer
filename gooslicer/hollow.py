"""Hollowing, drain holes and sealed-cavity detection (clean-room; spec 07-hollow-drain-spec.md).

Pipeline: winding-number voxelisation (column ray casting) → signed distance field from two
Euclidean distance transforms → manifold3d level set of the offset surface → exact boolean
with the original mesh. The part's outer surface is never resampled; only the new inner wall
is quantised to `accuracy` (the voxel pitch), as in SatelLite where "Accuracy" sets the
resolution of the hollow operation [INFERRED from the UI label].
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import manifold3d as m3
import numpy as np
import trimesh
from scipy import ndimage

from .support_defaults import DrainHoleSettings, HollowSettings

MAX_VOXELS = 60_000_000


class HollowError(ValueError):
    pass


# ----------------------------------------------------------------------------- conversions
def to_manifold(mesh: trimesh.Trimesh) -> m3.Manifold:
    mf = m3.Manifold(m3.Mesh(vert_properties=np.asarray(mesh.vertices, np.float32),
                             tri_verts=np.asarray(mesh.faces, np.uint32)))
    if mf.status() != m3.Error.NoError:
        raise HollowError(f"mesh is not a closed manifold ({mf.status().name}); repair it first")
    return mf


def to_trimesh(mf: m3.Manifold) -> trimesh.Trimesh:
    mm = mf.to_mesh()
    return trimesh.Trimesh(np.asarray(mm.vert_properties)[:, :3], np.asarray(mm.tri_verts), process=False)


# ------------------------------------------------------------------------------ voxel grid
@dataclass
class Grid:
    origin: np.ndarray      # world position of voxel (0,0,0)'s centre
    pitch: float
    shape: tuple[int, int, int]

    @classmethod
    def around(cls, lo, hi, pitch: float, margin: float) -> "Grid":
        lo = np.asarray(lo, float) - margin
        hi = np.asarray(hi, float) + margin
        shape = tuple(int(math.ceil(s / pitch)) + 1 for s in hi - lo)
        n = shape[0] * shape[1] * shape[2]
        if n > MAX_VOXELS:
            raise HollowError(f"accuracy {pitch} mm needs {n / 1e6:.0f} M voxels for this part; "
                              f"use a coarser accuracy (limit {MAX_VOXELS / 1e6:.0f} M)")
        return cls(lo, pitch, shape)

    def centers(self, axis: int) -> np.ndarray:
        return self.origin[axis] + np.arange(self.shape[axis]) * self.pitch


def occupancy(mesh: trimesh.Trimesh, g: Grid) -> np.ndarray:
    """Winding-number inside test at voxel centres: +Z rays per (x, y) column, every triangle
    crossing adds -sign(n_z) at its height, then a cumulative sum along Z. Overlapping shells
    union correctly. Columns are offset by an irrational fraction of the pitch so rays never hit
    edges/vertices exactly."""
    nx, ny, nz = g.shape
    jit = np.array([1e-4 * math.sqrt(2), 1e-4 * math.sqrt(3)]) * g.pitch
    tri = np.asarray(mesh.triangles, float)
    a, b, c = tri[:, 0], tri[:, 1], tri[:, 2]
    nzs = np.cross(b - a, c - a)[:, 2]
    keep = np.abs(nzs) > 1e-12
    a, b, c, nzs = a[keep], b[keep], c[keep], nzs[keep]
    xy_lo = np.minimum(np.minimum(a, b), c)[:, :2]
    xy_hi = np.maximum(np.maximum(a, b), c)[:, :2]
    o = g.origin[:2] + jit
    i0 = np.clip(np.ceil((xy_lo - o) / g.pitch), 0, None).astype(np.int64)
    i1 = np.minimum(np.floor((xy_hi - o) / g.pitch).astype(np.int64), np.array([nx - 1, ny - 1]))
    cnt = np.clip(i1[:, 0] - i0[:, 0] + 1, 0, None) * np.clip(i1[:, 1] - i0[:, 1] + 1, 0, None)
    toggles = np.zeros((nx, ny, nz + 1), np.int32)
    idx = np.nonzero(cnt)[0]
    # process in chunks to bound memory
    start = 0
    while start < len(idx):
        csum = np.cumsum(cnt[idx[start:]])
        stop = start + max(1, int(np.searchsorted(csum, 4_000_000)))
        sel = idx[start:stop]
        start = stop
        reps = cnt[sel]
        t = np.repeat(sel, reps)
        off = np.arange(len(t)) - np.repeat(np.cumsum(reps) - reps, reps)
        w = (i1[sel, 0] - i0[sel, 0] + 1)
        ix = i0[t, 0] + off % np.repeat(w, reps)
        iy = i0[t, 1] + off // np.repeat(w, reps)
        px, py = o[0] + ix * g.pitch, o[1] + iy * g.pitch
        A, B, C = a[t], b[t], c[t]
        d = (B[:, 0] - A[:, 0]) * (C[:, 1] - A[:, 1]) - (C[:, 0] - A[:, 0]) * (B[:, 1] - A[:, 1])
        u = ((B[:, 0] - px) * (C[:, 1] - py) - (C[:, 0] - px) * (B[:, 1] - py)) / d
        v = ((C[:, 0] - px) * (A[:, 1] - py) - (A[:, 0] - px) * (C[:, 1] - py)) / d
        wgt = 1.0 - u - v
        hit = (u >= 0) & (v >= 0) & (wgt >= 0)
        z = u * A[:, 2] + v * B[:, 2] + wgt * C[:, 2]
        k = np.ceil((z - g.origin[2]) / g.pitch).astype(np.int64)       # first centre above z
        k = np.clip(k, 0, nz)
        s = np.where(nzs[t] < 0, 1, -1).astype(np.int32)               # entering solid going up
        np.add.at(toggles, (ix[hit], iy[hit], k[hit]), s[hit])
    return np.cumsum(toggles, axis=2)[:, :, :nz] > 0


def signed_distance(occ: np.ndarray, pitch: float, mesh: trimesh.Trimesh | None = None,
                    g: Grid | None = None, band: float = 0.0) -> np.ndarray:
    """Positive inside. Far from the surface: Euclidean distance transforms of the occupancy
    (±p/2 quantisation). Within `band` of the surface (when `mesh` is given): distance to dense
    surface samples (spacing p/4, error ≤ ~p/6), so offsets inside the band are sub-voxel."""
    din = ndimage.distance_transform_edt(occ) * pitch
    dout = ndimage.distance_transform_edt(~occ) * pitch
    sd = np.where(occ, din - 0.5 * pitch, -(dout - 0.5 * pitch)).astype(np.float32)
    if mesh is not None and g is not None and band > 0:
        from scipy.spatial import cKDTree
        near = np.abs(sd) <= band + pitch
        idx = np.argwhere(near)
        pts = g.origin + idx * pitch
        tree = cKDTree(surface_samples(mesh, pitch / 4))
        d, _ = tree.query(pts, workers=-1)
        sd[near] = np.where(occ[near], d, -d)
    return sd


def surface_samples(mesh: trimesh.Trimesh, spacing: float) -> np.ndarray:
    """Points on every triangle on a barycentric lattice no coarser than `spacing`."""
    tri = np.asarray(mesh.triangles, float)
    L = np.max(np.linalg.norm(tri - np.roll(tri, 1, axis=1), axis=2), axis=1)
    n = np.maximum(1, np.ceil(L / spacing).astype(int))
    out = [tri.reshape(-1, 3)]
    for k in np.unique(n):
        if k == 1:
            continue
        T = tri[n == k]
        i, j = np.meshgrid(np.arange(k + 1), np.arange(k + 1), indexing="ij")
        m = (i + j) <= k
        u, v = i[m] / k, j[m] / k
        w = 1 - u - v
        out.append((u[None, :, None] * T[:, None, 0] + v[None, :, None] * T[:, None, 1]
                    + w[None, :, None] * T[:, None, 2]).reshape(-1, 3))
    return np.concatenate(out)


def level_surface(field: np.ndarray, g: Grid, level: float) -> m3.Manifold:
    """Manifold of {field > level} (trilinear field, body-centred tetrahedral extraction)."""
    f = np.pad(field, 1, constant_values=float(field.min()) - 1.0)   # closed at the grid edge
    ox, oy, oz = (g.origin - g.pitch).tolist()
    ip = 1.0 / g.pitch
    sx, sy, sz = (s - 2 for s in f.shape)
    flat = f.ravel().tolist()
    ny_, nz_ = f.shape[1], f.shape[2]

    def sdf(x, y, z):
        fx, fy, fz = (x - ox) * ip, (y - oy) * ip, (z - oz) * ip
        i = min(max(int(fx), 0), sx); j = min(max(int(fy), 0), sy); k = min(max(int(fz), 0), sz)
        tx, ty, tz = min(max(fx - i, 0.0), 1.0), min(max(fy - j, 0.0), 1.0), min(max(fz - k, 0.0), 1.0)
        b = (i * ny_ + j) * nz_ + k
        c000, c001 = flat[b], flat[b + 1]
        c010, c011 = flat[b + nz_], flat[b + nz_ + 1]
        b2 = b + ny_ * nz_
        c100, c101 = flat[b2], flat[b2 + 1]
        c110, c111 = flat[b2 + nz_], flat[b2 + nz_ + 1]
        c00 = c000 + (c001 - c000) * tz; c01 = c010 + (c011 - c010) * tz
        c10 = c100 + (c101 - c100) * tz; c11 = c110 + (c111 - c110) * tz
        c0 = c00 + (c01 - c00) * ty; c1 = c10 + (c11 - c10) * ty
        return c0 + (c1 - c0) * tx

    lo = g.origin - g.pitch
    hi = g.origin + (np.array(g.shape)) * g.pitch
    return m3.Manifold.level_set(sdf, [*lo.tolist(), *hi.tolist()], g.pitch, level)


# --------------------------------------------------------------------------------- hollow
@dataclass
class HollowResult:
    mesh: trimesh.Trimesh
    cavities: int
    removed_volume: float
    original_volume: float


def hollow(mesh: trimesh.Trimesh, s: HollowSettings | None = None) -> HollowResult:
    s = s or HollowSettings()
    if (errs := s.validate()):
        raise HollowError("; ".join(errs))
    body = to_manifold(mesh)
    t, p = float(s.thickness), float(s.accuracy)
    lo, hi = mesh.bounds
    margin = (t if s.hollow_type in ("outside", "center") else 0.0) + 3 * p
    g = Grid.around(lo, hi, p, margin)
    band = t if s.hollow_type in ("inside", "open_bottom") else t / 2 if s.hollow_type == "center" else t
    sd = signed_distance(occupancy(mesh, g), p, mesh, g, band + 2 * p)
    vol0 = body.volume()
    if s.hollow_type in ("inside", "open_bottom"):
        field = sd - t
        if s.hollow_type == "open_bottom":
            field = _open_bottom(field, p)
        cavity = _filter_cavities(level_surface(field, g, 0.0), s, p)
        result = body - cavity
    elif s.hollow_type == "outside":
        cavity = body
        result = level_surface(sd, g, -t) - body
    elif s.hollow_type == "center":
        cavity = _filter_cavities(level_surface(sd, g, t / 2), s, p)
        result = level_surface(sd, g, -t / 2) - cavity
    else:
        raise HollowError(f"unknown hollow type {s.hollow_type!r}")
    n = len(cavity.decompose()) if not cavity.is_empty() else 0
    if result.is_empty():
        raise HollowError("hollowing removed the whole part (wall thickness too large?)")
    return HollowResult(to_trimesh(result), n, vol0 - result.volume() if s.hollow_type != "outside"
                        else -(result.volume()), vol0)


def _open_bottom(field: np.ndarray, pitch: float) -> np.ndarray:
    """Extend each column's lowest cavity run down through the floor [INFERRED behaviour of
    "Open Bottom Hollow"; SatelLite's surface-angle rule is not reproduced, spec 07 §2]."""
    cav = field > 0
    has = cav.any(axis=2)
    first = np.where(has, cav.argmax(axis=2), field.shape[2])
    kk = np.arange(field.shape[2])[None, None, :]
    below = has[:, :, None] & (kk < first[:, :, None])
    out = field.copy()
    out[below] = pitch
    return out


def _filter_cavities(cavity: m3.Manifold, s: HollowSettings, pitch: float) -> m3.Manifold:
    """Drop slivers under 8 voxels (thin regions at fine accuracy; they would only trap resin)
    [GooSlicer choice], then apply SatelLite's optional filter (keep the N largest, or those
    above a volume)."""
    if cavity.is_empty():
        return cavity
    parts = sorted((c for c in cavity.decompose() if c.volume() >= 8 * pitch ** 3),
                   key=lambda c: -c.volume())
    if s.filter_small_shells:
        if s.filter_by == "cavity_number":
            parts = parts[:max(1, s.max_cavities)]
        else:
            parts = [c for c in parts if c.volume() >= s.small_shell_size]
    return m3.Manifold.batch_boolean(parts, m3.OpType.Add) if parts else m3.Manifold()


# ----------------------------------------------------------------------------- drain holes
@dataclass
class DrainHole:
    point: tuple[float, float, float]       # on the surface, same frame as the mesh
    axis: tuple[float, float, float]        # unit vector pointing INTO the part
    settings: DrainHoleSettings

    def solid(self, segments: int = 32) -> m3.Manifold:
        s = self.settings
        a = np.asarray(self.axis, float)
        a = a / np.linalg.norm(a)
        length = s.extend1 + s.extend2
        # cylinder/cone along +Z from z=0 (outer end, radius1) to z=length (inner end, radius2)
        c = m3.Manifold.cylinder(length, s.radius1, s.radius2, segments)
        R = _rotation_from_z(a)
        T = np.eye(4)
        T[:3, :3] = R
        T[:3, 3] = np.asarray(self.point, float) - a * s.extend1
        return c.transform(T[:3, :].tolist())


def _rotation_from_z(a: np.ndarray) -> np.ndarray:
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(z, a)
    c = float(np.dot(z, a))
    if np.linalg.norm(v) < 1e-12:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))


def drill(mesh: trimesh.Trimesh, holes: list[DrainHole]) -> trimesh.Trimesh:
    if not holes:
        return mesh
    body = to_manifold(mesh)
    cut = m3.Manifold.batch_boolean([h.solid() for h in holes], m3.OpType.Add)
    out = body - cut
    if out.is_empty():
        raise HollowError("the drain holes removed the whole part")
    return to_trimesh(out)


# --------------------------------------------------------------------------- sealed cavities
@dataclass
class Cavity:
    volume: float                     # mm³
    lowest: tuple[float, float, float]  # world point near the cavity's bottom (drain hole hint)


REFINE_FACTOR = 4              # a candidate cavity is re-checked at pitch / REFINE_FACTOR …
REFINE_MAX_VOXELS = 24_000_000  # … in a window around it of at most this many voxels
REFINE_MARGIN_MM = 4.0         # around the cavity in that window: thicker than its walls, so
                               # a gap through the wall reaches space outside the window's part


def _opens_out(meshes, g: Grid, lab: np.ndarray, i: int, outside: np.ndarray) -> bool:
    """Whether coarse cavity `i` reaches the outside at a finer pitch. A gap narrower than about
    two coarse voxels (a crease, or a slot between a part and its supports) is closed off at the
    coarse pitch and the space behind it looks sealed. Re-voxelise a window around the cavity
    finer, and if the cavity's empty space joins empty space the coarse pass already found
    open (`outside`: the labels that reach the grid border), resin drains from it."""
    idx = np.argwhere(lab == i)
    margin = max(3, int(math.ceil(REFINE_MARGIN_MM / g.pitch)))
    k0 = np.maximum(idx.min(0) - margin, 0)
    k1 = np.minimum(idx.max(0) + margin, np.array(g.shape) - 1)
    lo = g.origin + k0 * g.pitch - g.pitch / 2
    hi = g.origin + k1 * g.pitch + g.pitch / 2
    pf = max(g.pitch / REFINE_FACTOR, float(np.prod(hi - lo) / REFINE_MAX_VOXELS) ** (1 / 3), 0.02)
    if pf > g.pitch / 2:
        return False                                  # too big to refine: keep it (large cavities
                                                      # are hollows, not slivers)
    shape = tuple(int(math.ceil(x / pf)) for x in hi - lo)
    gf = Grid(lo + pf / 2, pf, shape)
    occ = np.zeros(shape, bool)
    for m in meshes:
        b = m.bounds
        if (b[1][:2] < lo[:2]).any() or (b[0][:2] > hi[:2]).any() or b[0][2] > hi[2]:
            continue                                  # can't cross the window's columns below its top
        occ |= occupancy(m, gf)
    fl, _ = ndimage.label(~occ)
    # coarse label at each fine voxel centre
    ii = [np.clip(np.rint((gf.origin[d] + np.arange(shape[d]) * pf - g.origin[d]) / g.pitch).astype(np.int64),
                  0, g.shape[d] - 1) for d in range(3)]
    coarse = lab[np.ix_(*ii)]
    empty = ~occ
    seeds = np.unique(fl[empty & (coarse == i)])
    seeds = seeds[seeds > 0]
    if not len(seeds):
        return False
    out = np.unique(fl[empty & np.isin(coarse, outside)])
    return bool(np.isin(seeds, out[out > 0]).any())


def _cavity_labels(meshes, pitch, min_volume):
    lo = np.min([m.bounds[0] for m in meshes], axis=0)
    hi = np.max([m.bounds[1] for m in meshes], axis=0)
    if pitch is None:
        pitch = float(min(0.5, max(0.1, (hi - lo).max() / 300)))
    g = Grid.around(lo, hi, pitch, 2 * pitch)
    occ = np.zeros(g.shape, bool)
    for m in meshes:
        occ |= occupancy(m, g)
    lab, n = ndimage.label(~occ)
    border = set(np.unique(np.concatenate([
        lab[0].ravel(), lab[-1].ravel(), lab[:, 0].ravel(), lab[:, -1].ravel(),
        lab[:, :, 0].ravel(), lab[:, :, -1].ravel()])))
    out = []
    v = pitch ** 3
    sizes = ndimage.sum_labels(np.ones_like(lab), lab, np.arange(1, n + 1))
    outside = np.array(sorted(x for x in border if x > 0))
    for i in range(1, n + 1):
        if i in border or sizes[i - 1] * v < min_volume:
            continue
        if _opens_out(meshes, g, lab, i, outside):
            continue                                  # a narrow gap the coarse grid closed off
        idx = np.argwhere(lab == i)
        bot = idx[idx[:, 2].argmin()]
        out.append((i, Cavity(float(sizes[i - 1] * v), tuple((g.origin + bot * pitch).tolist()))))
    out.sort(key=lambda x: -x[1].volume)
    return g, lab, out


def sealed_cavities(meshes: list[trimesh.Trimesh], pitch: float | None = None,
                    min_volume: float = 1.0) -> list[Cavity]:
    """Enclosed voids that resin cannot drain from: empty-space components of the voxelised
    union that do not reach the grid border (6-connectivity). Openings narrower than about
    two voxels close up at that pitch (≤ 0.5 mm by default), so each candidate is confirmed at
    a quarter of the pitch in a window around it (_opens_out)."""
    if not meshes:
        return []
    return [c for _, c in _cavity_labels(meshes, pitch, min_volume)[2]]


def cavity_faces(meshes: list[trimesh.Trimesh], pitch: float | None = None,
                 min_volume: float = 1.0) -> tuple[list[Cavity], list[np.ndarray]]:
    """sealed_cavities, plus for each mesh a mask of the faces that line a sealed cavity: a
    point just off the face, on its outward (empty) side, lies in the cavity's voxels."""
    cav, each = cavity_faces_each(meshes, pitch, min_volume)
    masks = [np.zeros(len(m.faces), bool) for m in meshes]
    for per_mesh in each:
        for mask, hit in zip(masks, per_mesh):
            mask |= hit
    return cav, masks


def cavity_faces_each(meshes: list[trimesh.Trimesh], pitch: float | None = None,
                      min_volume: float = 1.0) -> tuple[list[Cavity], list[list[np.ndarray]]]:
    """cavity_faces for each cavity on its own: [cavity][mesh] face masks."""
    if not meshes:
        return [], []
    g, lab, found = _cavity_labels(meshes, pitch, min_volume)
    cav = [c for _, c in found]
    shape = np.array(g.shape)
    got_all = []
    for m in meshes:
        c, nrm = np.asarray(m.triangles_center, float), np.asarray(m.face_normals, float)
        gots = []
        for k in (0.6, 1.2, 2.0):                 # thin walls: the nearest voxel may be solid
            ijk = np.rint((c + nrm * (k * g.pitch) - g.origin) / g.pitch).astype(np.int64)
            ok = np.all((ijk >= 0) & (ijk < shape), axis=1)
            got = np.zeros(len(c), np.int64)
            got[ok] = lab[ijk[ok, 0], ijk[ok, 1], ijk[ok, 2]]
            gots.append(got)
        got_all.append(gots)
    each = [[np.any([got == i for got in gots], axis=0) if len(gots[0]) else np.zeros(0, bool)
             for gots in got_all] for i, _ in found]
    return cav, each


def cavity_shells(meshes: list[trimesh.Trimesh], pitch: float | None = None,
                  min_volume: float = 1.0) -> tuple[list[Cavity], list[np.ndarray]]:
    """sealed_cavities, plus for each mesh a mask of the faces to delete to make its cavities
    solid: every connected shell that (by area, ≥ 80 %, as the face test can miss slivers) only
    lines a sealed cavity, such as the inner shell hollowing leaves. A cavity whose wall is also
    part of the outer surface (several parts closing it off, say) has no such shell and stays."""
    cav, masks = cavity_faces(meshes, pitch, min_volume)
    out = []
    for m, mask in zip(meshes, masks):
        rm = np.zeros(len(m.faces), bool)
        if mask.any():
            lab = trimesh.graph.connected_component_labels(m.face_adjacency, node_count=len(m.faces))
            area = np.asarray(m.area_faces, float)
            for c in np.unique(lab[mask]):
                comp = lab == c
                if area[comp & mask].sum() >= 0.8 * area[comp].sum():
                    rm |= comp
        out.append(rm)
    return cav, out


def without_faces(mesh: trimesh.Trimesh, remove: np.ndarray) -> trimesh.Trimesh:
    """`mesh` with the faces in `remove` deleted (and the vertices only they used)."""
    m = trimesh.Trimesh(np.asarray(mesh.vertices), np.asarray(mesh.faces)[~remove], process=False)
    m.remove_unreferenced_vertices()
    return m
