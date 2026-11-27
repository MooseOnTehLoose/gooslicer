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
    pillars: int = 0                  # Center: pillars added under flat cavity ceilings


def hollow(mesh: trimesh.Trimesh, s: HollowSettings | None = None) -> HollowResult:
    s = s or HollowSettings()
    if (errs := s.validate()):
        raise HollowError("; ".join(errs))
    body = to_manifold(mesh)
    t, p = float(s.thickness), float(s.accuracy)
    lo, hi = mesh.bounds
    margin = (t if s.hollow_type == "outside" else 0.0) + 3 * p
    g = Grid.around(lo, hi, p, margin)
    sd = signed_distance(occupancy(mesh, g), p, mesh, g, t + 2 * p)
    vol0 = body.volume()
    pillars = 0
    if s.hollow_type in ("inside", "open_bottom", "center"):
        field = sd - t
        if s.hollow_type == "center":
            bars = self_support_pillars(mesh, s)
            pillars = len(bars)
            if bars:
                field = np.minimum(field, _bars_distance(bars, g, t + 2 * p) - t)
        if s.hollow_type == "open_bottom":
            field = _open_bottom(field, mesh, g, s)
        cavity = _filter_cavities(level_surface(field, g, 0.0), s, p)
        if s.infill and s.hollow_type != "open_bottom" and not cavity.is_empty():
            cavity = cavity - lattice((lo, hi), s)
        result = body - cavity
    elif s.hollow_type == "outside":
        cavity = body
        if s.infill:
            cavity = cavity - lattice((lo, hi), s)
        result = level_surface(sd, g, -t) - cavity
    else:
        raise HollowError(f"unknown hollow type {s.hollow_type!r}")
    n = len(cavity.decompose()) if not cavity.is_empty() else 0
    if result.is_empty():
        raise HollowError("hollowing removed the whole part (wall thickness too large?)")
    return HollowResult(to_trimesh(result), n, vol0 - result.volume() if s.hollow_type != "outside"
                        else -(result.volume()), vol0, pillars)


# ------------------------------------------------------------------- self-supporting (Center)
def self_support_pillars(mesh: trimesh.Trimesh, s: HollowSettings) -> list[tuple]:
    """SatelLite's "Center Hollow" pillars (07 §2) [OBSERVED EleShapeEdge SelfSupportHollow]:
    support anchors are found on an inside-out copy of the part, so they land on the underside
    of its roofs, the faces flatter than the surface angle as seen from inside. From each, a ray
    straight down finds the floor, and a thin cone bar joins them: radius 0.01·accuracy at the
    roof, accuracy/√3 at the floor. The hollow's inward offset then runs around the bars too,
    which leaves pillars of wall thickness inside the cavity. The anchor spacing is GooSlicer's
    (4 × (thickness + accuracy)), as is keeping them two walls back from a roof's edge, where a
    pillar would only merge into the wall; SatelLite's spacing comes from its support anchor generator [UNKNOWN].
    Returns [(top, bottom, top radius, bottom radius)]."""
    from .raycast import caster
    from .supports import SupportParams, detect_contacts
    t, acc = float(s.thickness), float(s.accuracy)
    inv = mesh.copy()
    inv.invert()
    spacing = 4.0 * (t + acc)
    prm = SupportParams(critical_angle=float(s.surface_angle), anchor_dis=spacing, anchor_dis_border=spacing,
                        border_offset=2 * (t + acc), reinforce_lowest=False)
    tops = np.array([c.point for c in detect_contacts(inv, prm)]).reshape(-1, 3)
    if not len(tops):
        return []
    zmin = mesh.bounds[0][2]
    tops = tops[tops[:, 2] >= zmin]
    dist, _ = caster(mesh).vertical(tops - [0.0, 0.0, 0.001])
    out = []
    for top, d in zip(tops, dist):
        # a floor further than the wall on both ends leaves room for a pillar in the cavity
        if np.isfinite(d) and d > 2 * t + acc:
            out.append((top, top - [0.0, 0.0, d + 0.001], 0.01 * acc, acc / math.sqrt(3)))
    return out


def _bars_distance(bars: list[tuple], g: Grid, reach: float) -> np.ndarray:
    """Distance from each voxel centre to the nearest cone bar's surface (inf beyond `reach`)."""
    out = np.full(g.shape, np.inf, np.float32)
    for a, b, ra, rb in bars:
        a, b = np.asarray(a, float), np.asarray(b, float)
        lo = np.minimum(a, b) - (reach + max(ra, rb))
        hi = np.maximum(a, b) + (reach + max(ra, rb))
        k0 = np.clip(np.floor((lo - g.origin) / g.pitch).astype(int), 0, np.array(g.shape) - 1)
        k1 = np.clip(np.ceil((hi - g.origin) / g.pitch).astype(int), 0, np.array(g.shape) - 1)
        if np.any(k1 < k0):
            continue
        ax = [g.origin[i] + np.arange(k0[i], k1[i] + 1) * g.pitch for i in range(3)]
        P = np.stack(np.meshgrid(*ax, indexing="ij"), axis=-1)
        d = b - a
        L2 = float(d @ d)
        u = np.clip(((P - a) @ d) / L2, 0.0, 1.0) if L2 > 0 else np.zeros(P.shape[:3])
        dist = np.linalg.norm(P - (a + u[..., None] * d), axis=-1) - (ra + u * (rb - ra))
        sl = tuple(slice(k0[i], k1[i] + 1) for i in range(3))
        out[sl] = np.minimum(out[sl], dist)
    return out


# ------------------------------------------------------------------------------ infill
def _bar(p, q, r: float, segs: int = 4) -> m3.Manifold:
    p, q = np.asarray(p, float), np.asarray(q, float)
    d = q - p
    L = float(np.linalg.norm(d))
    R = _rotation_from_z(d / L)
    return m3.Manifold.cylinder(L, r, r, segs).transform(np.c_[R, p].tolist())


def _cell(kind: str, a: float, r: float) -> m3.Manifold:
    """One lattice cell as SatelLite builds it [OBSERVED EleTriCreate GenerateLattice]: square
    bars (4 segments, `r` the corner radius [INFERRED]) with no balls at the joints, clipped to
    the cell's box. BCC: the four body diagonals. FCC: both diagonals of all six faces. The
    clip box is a hair larger than the cell so neighbouring cells overlap instead of touching."""
    c = [np.array((i, j, k), float) * a for i in (0, 1) for j in (0, 1) for k in (0, 1)]
    if kind == "bcc":
        bars = [(c[i], c[7 - i]) for i in range(4)]
    else:
        bars = []
        for axis in range(3):
            u, v = (i for i in range(3) if i != axis)
            for side in (0.0, a):
                def pt(x, y, axis=axis, u=u, v=v, side=side):
                    q = np.zeros(3)
                    q[axis], q[u], q[v] = side, x, y
                    return q
                bars += [(pt(0, 0), pt(a, a)), (pt(a, 0), pt(0, a))]
    cell = m3.Manifold.batch_boolean([_bar(p, q, r) for p, q in bars], m3.OpType.Add)
    e = 1e-4 * a
    return cell ^ m3.Manifold.cube((a + 2 * e,) * 3).translate((-e, -e, -e))


def _tile(cell: m3.Manifold, a: float, counts) -> m3.Manifold:
    """`counts` copies of a cell along x, y, z, built by doubling: log2(n) unions per axis."""
    out = cell
    for axis, n in enumerate(counts):
        block, size, acc, off = out, 1, None, 0
        while n:
            if n & 1:
                piece = block.translate(tuple(off * a if i == axis else 0.0 for i in range(3)))
                acc = piece if acc is None else acc + piece
                off += size
            n >>= 1
            if n:
                block = block + block.translate(tuple(size * a if i == axis else 0.0 for i in range(3)))
                size *= 2
        out = acc
    return out


def lattice(bounds, s: HollowSettings) -> m3.Manifold:
    """SatelLite's infill lattice (07 §2b) over a part's bounding box ((x0, y0, z0, x1, y1, z1)
    or lo, hi): cells of `infill_size` from the box's low corner, int(size/cell + 2) per axis
    [OBSERVED StructureImp::dowork_file], bars of `infill_radius`."""
    b = np.asarray(bounds, float).reshape(2, 3)
    a = float(s.infill_size)
    counts = [int((b[1][i] - b[0][i]) / a + 2) for i in range(3)]
    if np.prod(counts) > 400_000:
        raise HollowError(f"the infill would need {np.prod(counts):,} cells; use a larger cell size")
    return _tile(_cell(s.infill_type, a, float(s.infill_radius)), a, counts).translate(tuple(b[0].tolist()))


# ---------------------------------------------------------------------------- open bottom
def bottom_faces(mesh: trimesh.Trimesh, s: HollowSettings) -> np.ndarray:
    """The faces SatelLite's Open Bottom removes before shelling [OBSERVED EleShapeModify
    markOpenBottomFaces]: within a band of the lowest point (a quarter of the smaller of wall
    thickness and accuracy, kept to 0.02–0.2 mm) by both their lowest corner and their centre,
    and facing within about 45° of straight down or up (|n_z| ≥ 0.7). If none qualify: the
    faces whose centres are within the band of the lowest centre."""
    band = min(max(0.25 * min(abs(s.thickness), abs(s.accuracy)), 0.02), 0.2)
    tri = np.asarray(mesh.triangles, float)
    zmin = float(tri[:, :, 2].min())
    cz = tri[:, :, 2].mean(axis=1)
    keep = (tri[:, :, 2].min(axis=1) <= zmin + band) & (cz <= zmin + band) & \
        (np.abs(np.asarray(mesh.face_normals)[:, 2]) >= 0.7)
    if not keep.any():
        keep = cz <= cz.min() + band
    return keep


def _open_bottom(field: np.ndarray, mesh: trimesh.Trimesh, g: Grid, s: HollowSettings) -> np.ndarray:
    """SatelLite removes the bottom faces and shells what is left inward by the wall thickness
    (07 §2), so the wall is measured from the remaining surface only: near the removed faces,
    the field is recomputed as the distance to the other faces. Where the cavity then reaches
    the removed faces, it is carried on down out of the part, so the floor opens there."""
    from scipy.spatial import cKDTree
    t, p = float(s.thickness), g.pitch
    bottom = bottom_faces(mesh, s)
    faces = np.asarray(mesh.faces)
    rest = trimesh.Trimesh(mesh.vertices, faces[~bottom], process=False)
    if not len(rest.faces):
        return field
    low = surface_samples(trimesh.Trimesh(mesh.vertices, faces[bottom], process=False), p / 2)
    inside = field > -t - 1e-6                     # field = signed distance - t
    idx = np.argwhere(inside)
    pts = g.origin + idx * p
    d_low, _ = cKDTree(low).query(pts, distance_upper_bound=t + 2 * p, workers=-1)
    near = np.isfinite(d_low)
    out = field.copy()
    if near.any():
        d_rest, _ = cKDTree(surface_samples(rest, p / 4)).query(pts[near], workers=-1)
        i = idx[near]
        out[i[:, 0], i[:, 1], i[:, 2]] = np.maximum(field[i[:, 0], i[:, 1], i[:, 2]], d_rest - t)
    # columns under the removed faces where the cavity comes down to the part's floor
    ij = np.rint((low[:, :2] - g.origin[:2]) / p).astype(np.int64)
    ok = np.all((ij >= 0) & (ij < np.array(g.shape[:2])), axis=1)
    cols = np.zeros(g.shape[:2], bool)
    cols[ij[ok, 0], ij[ok, 1]] = True
    cav = out > 0
    has = cav.any(axis=2) & cols & inside.any(axis=2)
    first = np.where(has, cav.argmax(axis=2), field.shape[2])
    floor = np.where(has, inside.argmax(axis=2), 0)
    has &= first <= floor + 1
    kk = np.arange(field.shape[2])[None, None, :]
    below = has[:, :, None] & (kk < first[:, :, None])
    out[below] = p
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
    """A drain hole as SatelLite's Perforator makes it (07 §3) [OBSERVED EleTriHole
    CreateMesh::Create]. Hole form: through the wall, from the clicked point to the next surface
    along the axis (`length`, found by drill() when None). Slot form: settings.depth deep."""
    point: tuple[float, float, float]       # on the surface, same frame as the mesh
    axis: tuple[float, float, float]        # unit vector pointing INTO the part
    settings: DrainHoleSettings
    length: float | None = None             # surface to far wall; None: cast a ray in drill()

    def depth(self) -> float:
        s = self.settings
        return float(s.depth) if s.hole_form == "slot" else float(self.length or 0.0)

    def solid(self, clearance: float = 0.0) -> m3.Manifold:
        return to_manifold(self.mesh(clearance))

    def mesh(self, clearance: float = 0.0) -> trimesh.Trimesh:
        a = np.asarray(self.axis, float)
        a = a / np.linalg.norm(a)
        m = hole_mesh(self.settings, self.depth(), clearance)
        T = np.eye(4)
        T[:3, :3] = _rotation_from_z(a)
        T[:3, 3] = np.asarray(self.point, float)
        m.apply_transform(T)
        return m


def _profile(s: DrainHoleSettings, R: float, clearance: float) -> np.ndarray:
    """The hole's cross-section at corner radius R as SatelLite draws it: `s.sides()` steps
    around the circle (the first corner on +X), and with a notch, a key tab on the +X side, the notch width (plus
    twice the clearance) wide, its tip the notch height (plus the clearance) past the circle,
    turned by the notch angle [angle INFERRED]."""
    n = s.sides()
    if not s.use_notch:
        ang = np.linspace(0.0, 2 * math.pi, n, endpoint=False)
        return np.c_[np.cos(ang), np.sin(ang)] * R
    w = min(float(s.notch_width), 1.999 * float(s.radius1))
    half = 0.5 * max(w + 2 * clearance, 0.002)
    a0 = math.asin(half / R) if half < R else math.pi / 2
    ang = np.linspace(a0, 2 * math.pi - a0, n + 1)
    arc = np.c_[np.cos(ang), np.sin(ang)] * R
    r = R - clearance                                       # radius1, or radius2 when built from the far end
    tip = math.sqrt(max(r * r - (w / 2) ** 2, 0.0)) + float(s.notch_height) + clearance
    pts = np.vstack([arc, [[tip, -half], [tip, half]]])
    c, sn = math.cos(math.radians(s.notch_angle)), math.sin(math.radians(s.notch_angle))
    return pts @ np.array([[c, sn], [-sn, c]])


def hole_mesh(s: DrainHoleSettings, depth: float, clearance: float = 0.0) -> trimesh.Trimesh:
    """The cutter in the hole's own frame: +Z into the part, the clicked surface at z = 0, the
    far end at z = depth (a slot's own depth for the Slot form). Radius r1 (+ clearance) at the
    surface; tapering to r2 at the far end (the taper capped at 0.8 rad from the axis), or by
    the taper angle when that is on (Hole form only). It runs extend1 out past the surface and extend2 past the far end (not for a
    slot), keeping its taper, and closes in a point if the taper meets the axis first."""
    slot = s.hole_form == "slot"
    L = max(float(s.depth if slot else depth), 0.0)
    clearance = clearance / math.cos(math.pi / s.sides())    # measured across a polygon's flats
    e1, e2 = float(s.extend1), 0.0 if slot else float(s.extend2)
    r1, r2 = float(s.radius1), float(s.radius2)
    use_angle = s.use_angle_slot and not slot
    z0, z1 = -e1, L + e2
    if use_angle or r1 > r2:
        tan = math.tan(math.radians(s.angle_slot)) if use_angle else \
            math.tan(min(math.atan((r1 - r2) / L), 0.8)) if L > 1e-9 else 0.0
        R0 = r1 + clearance
        rad = lambda z: R0 - z * tan  # noqa: E731   narrows going in
    elif r2 > r1:
        tan = math.tan(min(math.atan((r2 - r1) / L), 0.8)) if L > 1e-9 else 0.0
        R0 = r2 + clearance
        rad = lambda z: R0 - (L - z) * tan  # noqa: E731   narrows coming out
    else:
        tan, R0 = 0.0, r1 + clearance
        rad = lambda z: R0  # noqa: E731
    base = _profile(s, R0, clearance) / R0                  # unit-radius shape, scaled per ring
    rings, apex = [], None
    for z in (z0, z1):
        r = rad(z)
        if r <= 1e-6:                                         # the taper closes before this end
            apex = (R0 / tan if z == z1 else L - R0 / tan) if tan > 0 else z
            r = 0.0
        rings.append((z, r))
    n = len(base)
    v, f = [], []
    pts = []
    for z, r in rings:
        if r == 0.0:
            pts.append(None)
        else:
            pts.append(np.c_[base * r, np.full(n, z)])
    i = np.arange(n)
    j = (i + 1) % n
    if pts[0] is not None and pts[1] is not None:
        v = np.vstack([pts[0], pts[1], [[0, 0, z0]], [[0, 0, z1]]])
        f = np.vstack([np.c_[i, j, n + j], np.c_[i, n + j, n + i],
                       np.c_[np.full(n, 2 * n), j, i], np.c_[np.full(n, 2 * n + 1), n + i, n + j]])
    else:                                                     # a cone ending in a point
        k = 0 if pts[0] is not None else 1
        zb = rings[k][0]
        v = np.vstack([pts[k], [[0, 0, apex]], [[0, 0, zb]]])
        f = np.vstack([np.c_[i, j, np.full(n, n)], np.c_[np.full(n, n + 1), j, i]])
    m = trimesh.Trimesh(v, f, process=False)
    if m.volume < 0:
        m.invert()
    return m


def _rotation_from_z(a: np.ndarray) -> np.ndarray:
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(z, a)
    c = float(np.dot(z, a))
    if np.linalg.norm(v) < 1e-12:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1.0 / (1.0 + c))


def hole_length(mesh: trimesh.Trimesh, point, axis) -> float:
    """From the clicked point along the axis to the next surface: the wall the hole goes
    through [OBSERVED Perforator::findNextInterPoint]. 0 if nothing is hit."""
    from .scene import ray_triangles
    a = np.asarray(axis, float)
    a = a / np.linalg.norm(a)
    p = np.asarray(point, float)
    hit = ray_triangles(mesh, p + a * 1e-3, a)
    return float(np.linalg.norm(hit[1] - p)) if hit is not None else 0.0


def _clearances(s: DrainHoleSettings) -> tuple[float, float]:
    """(hole, pin) radial offsets with "Keep Hole Pin" [OBSERVED BooleanImp]: the pin shrinks by
    the clearance (1), the hole grows (2), or each by half (3)."""
    c = float(s.clearance) if s.keep_subtracted else 0.0
    return {1: (0.0, -c), 2: (c, 0.0), 3: (c / 2, -c / 2)}.get(int(s.clearance_type), (0.0, -c))


def drill(mesh: trimesh.Trimesh, holes: list[DrainHole]) -> trimesh.Trimesh:
    return drill_keep(mesh, holes)[0]


def drill_keep(mesh: trimesh.Trimesh, holes: list[DrainHole]) -> tuple[trimesh.Trimesh, list[trimesh.Trimesh]]:
    """The mesh with the holes cut, and the plugs cut out of it for holes that keep their pin."""
    if not holes:
        return mesh, []
    body = to_manifold(mesh)
    for h in holes:
        if h.length is None and h.settings.hole_form != "slot":
            h.length = hole_length(mesh, h.point, h.axis)
    cut = m3.Manifold.batch_boolean([h.solid(_clearances(h.settings)[0]) for h in holes], m3.OpType.Add)
    out = body - cut
    if out.is_empty():
        raise HollowError("the drain holes removed the whole part")
    pins = []
    for h in holes:
        if h.settings.keep_subtracted:
            pin = body ^ h.solid(_clearances(h.settings)[1])
            if not pin.is_empty():
                pins.append(to_trimesh(pin))
    return to_trimesh(out), pins


# --------------------------------------------------------------------------- sealed cavities
SEALED, CUP = "sealed", "cup"
CUP_MIN_AREA = 10.0            # mm²: smaller pockets (engraved text, pits) pull too little to matter


@dataclass
class Cavity:
    volume: float                     # mm³
    lowest: tuple[float, float, float]  # world point near the cavity's bottom (drain hole hint)
    kind: str = SEALED                # SEALED, or CUP: open only upward, toward the film
    area: float = 0.0                 # cups: largest cross-section, mm² (what the film pulls on)


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


def _cups(occ: np.ndarray, lab: np.ndarray, outside: np.ndarray, g: Grid, min_volume: float,
          min_area: float):
    """Suction cups (07 §5): empty space that each layer encloses on every side, that is closed
    below (by the part or the plate) and reaches the outside only upward. Printing it, the film
    closes the cup's mouth, so each lift pulls on the resin inside. Sealed cavities are left to
    _cavity_labels. Returns (labels of the held voxels, [(label, Cavity)])."""
    empty = ~occ
    held = np.zeros_like(occ)
    for k in range(occ.shape[2]):
        l2, n2 = ndimage.label(empty[:, :, k])
        if n2:
            edge = np.unique(np.concatenate([l2[0], l2[-1], l2[:, 0], l2[:, -1]]))
            held[:, :, k] = (l2 > 0) & ~np.isin(l2, edge)
    held &= np.isin(lab, outside)
    if not held.any():
        return None, []
    cl, n = ndimage.label(held)
    free = empty & ~held                                 # empty space open to the side
    free[:, :, g.origin[2] + np.arange(occ.shape[2]) * g.pitch < g.pitch / 2] = False    # the plate
    below = np.zeros_like(held)
    below[:, :, 1:] = free[:, :, :-1]
    drains = np.unique(cl[held & below])
    v, a = g.pitch ** 3, g.pitch ** 2
    sizes = ndimage.sum_labels(held, cl, np.arange(1, n + 1))
    out = []
    for i in range(1, n + 1):
        if i in drains or sizes[i - 1] * v < min_volume:
            continue
        idx = np.argwhere(cl == i)
        area = float(np.bincount(idx[:, 2]).max() * a)
        if area < min_area:
            continue
        bot = idx[idx[:, 2].argmin()]
        out.append((i, Cavity(float(sizes[i - 1] * v), tuple((g.origin + bot * g.pitch).tolist()), CUP, area)))
    return cl, out


def _cavity_labels(meshes, pitch, min_volume, cups: bool = False):
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
    if cups:
        cl, found = _cups(occ, lab, outside, g, min_volume, CUP_MIN_AREA)
        if found:                     # cup voxels get labels after every empty-space label
            lab = lab.copy()
            lab[cl > 0] = cl[cl > 0] + n
            found.sort(key=lambda x: -x[1].area)
            out += [(i + n, c) for i, c in found]
    return g, lab, out


def sealed_cavities(meshes: list[trimesh.Trimesh], pitch: float | None = None,
                    min_volume: float = 1.0, cups: bool = False) -> list[Cavity]:
    """Enclosed voids that resin cannot drain from: empty-space components of the voxelised
    union that do not reach the grid border (6-connectivity). Openings narrower than about
    two voxels close up at that pitch (≤ 0.5 mm by default), so each candidate is confirmed at
    a quarter of the pitch in a window around it (_opens_out)."""
    if not meshes:
        return []
    return [c for _, c in _cavity_labels(meshes, pitch, min_volume, cups)[2]]


def cavity_faces(meshes: list[trimesh.Trimesh], pitch: float | None = None,
                 min_volume: float = 1.0, cups: bool = False) -> tuple[list[Cavity], list[np.ndarray]]:
    """sealed_cavities, plus for each mesh a mask of the faces that line a sealed cavity: a
    point just off the face, on its outward (empty) side, lies in the cavity's voxels."""
    cav, each = cavity_faces_each(meshes, pitch, min_volume, cups)
    masks = [np.zeros(len(m.faces), bool) for m in meshes]
    for per_mesh in each:
        for mask, hit in zip(masks, per_mesh):
            mask |= hit
    return cav, masks


def cavity_faces_each(meshes: list[trimesh.Trimesh], pitch: float | None = None,
                      min_volume: float = 1.0, cups: bool = False) -> tuple[list[Cavity], list[list[np.ndarray]]]:
    """cavity_faces for each cavity on its own: [cavity][mesh] face masks."""
    if not meshes:
        return [], []
    g, lab, found = _cavity_labels(meshes, pitch, min_volume, cups)
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
