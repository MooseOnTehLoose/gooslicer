"""Batched ray queries against one mesh, for support planning [GOOSLICER].

trimesh's ray tester (without Embree, which isn't installed) finds candidate faces from each
ray's bounding box, so a long ray, such as a support's straight drop to the raft, tests nearly
every face. It also holds arrays for all those pairs: hundreds of rays against a 300k-face model
took ~40 s and ~9 GB. Supports only ask two kinds of question, and each has a cheap index:

- vertical rays (drops to the raft, inside tests): faces binned by their XY bounding box on a
  grid, so a ray tests only the faces in its cell;
- short segments (contact tips, tree branches, clearance): k-d trees of face centres, so a
  segment tests only faces whose bounding sphere can reach it.

Meshes mix face sizes (repairs leave long slivers; a flat base is a few huge faces), and one
grid or search radius sized for the largest would test everything. So faces are split into
size levels: grids whose cells are LEVEL_STEP times wider than the level below (a face goes in
the level where it spans at most 2 x 2 cells), and one k-d tree per doubling of face radius.
Queries run in chunks of CHUNK so memory stays bounded however many rays there are.

A Caster is built once per mesh (under a second for 650k faces) and cached while the mesh lives.
"""
from __future__ import annotations

import weakref
from dataclasses import dataclass

import numpy as np
import trimesh
from scipy.spatial import cKDTree

EPS = 1e-9
MAX_CELLS = 1 << 20           # finest grid size limit
LEVEL_STEP = 4                # grid cell growth per level
CHUNK = 512                   # queries per batch
_CACHE: dict = {}


def caster(mesh: trimesh.Trimesh) -> "Caster":
    """The Caster for this mesh object, built on first use (meshes are replaced, not edited)."""
    k = id(mesh)
    c = _CACHE.get(k)
    if c is None or c[0]() is not mesh or c[1].n != len(mesh.faces):
        c = (weakref.ref(mesh), Caster(mesh))
        _CACHE[k] = c
        weakref.finalize(mesh, _CACHE.pop, k, None)
    return c[1]


def _pairs(counts: np.ndarray, starts: np.ndarray):
    """For queries q with counts[q] candidates beginning at starts[q] (in some flat list):
    (query index, flat position) of every candidate."""
    total = int(counts.sum())
    q = np.repeat(np.arange(len(counts)), counts)
    first = np.cumsum(counts) - counts
    pos = starts[q] + (np.arange(total) - first[q])
    return q, pos


def _first_per_query(q: np.ndarray, t: np.ndarray, face: np.ndarray, n: int):
    """Smallest t per query: (t, face) arrays of length n, inf / -1 where none."""
    out_t = np.full(n, np.inf)
    out_f = np.full(n, -1, np.int64)
    if len(q):
        order = np.lexsort((t, q))
        q, t, face = q[order], t[order], face[order]
        first = np.r_[True, q[1:] != q[:-1]]
        out_t[q[first]] = t[first]
        out_f[q[first]] = face[first]
    return out_t, out_f


@dataclass
class _Grid:
    cell: float
    nx: int
    ny: int
    faces: np.ndarray         # face ids sorted by cell
    start: np.ndarray         # (nx*ny + 1) offsets into faces


@dataclass
class _Tree:
    tree: cKDTree
    faces: np.ndarray         # face id per tree point
    radius: float             # largest bounding-sphere radius of these faces


class Caster:
    def __init__(self, mesh: trimesh.Trimesh):
        V = np.asarray(mesh.vertices, np.float64)
        F = np.asarray(mesh.faces, np.int64)
        self.n = len(F)
        self.tri = V[F] if self.n else np.zeros((0, 3, 3))
        self.normals = np.asarray(mesh.face_normals) if self.n else np.zeros((0, 3))
        self.grids: list[_Grid] = []
        self.trees: list[_Tree] = []
        if not self.n:
            return
        lo, hi = self.tri.min(axis=1), self.tri.max(axis=1)
        # ---- XY grids for vertical rays
        self.g0 = lo.min(axis=0)[:2]
        span = np.maximum(hi.max(axis=0)[:2] - self.g0, 1e-6)
        ext = (hi - lo)[:, :2].max(axis=1)
        base = max(float(np.median(ext)) * 2.0, float(np.sqrt(span[0] * span[1] / MAX_CELLS)), 1e-4)
        with np.errstate(divide="ignore"):
            lev = np.ceil(np.log(np.maximum(ext, 1e-12) / base) / np.log(LEVEL_STEP))
        lev = np.maximum(lev, 0).astype(np.int64)            # ext <= cell: at most 2 x 2 cells
        for k in range(int(lev.max()) + 1):
            ids = np.flatnonzero(lev == k)
            if len(ids):
                self.grids.append(self._grid(ids, lo, hi, base * LEVEL_STEP ** k, span))
        # ---- k-d trees of face centres for segments and clearance
        centre = self.tri.mean(axis=1)
        r = np.sqrt(((self.tri - centre[:, None]) ** 2).sum(axis=2).max(axis=1)) * 1.0001 + 1e-9
        r0 = max(float(np.median(r)), 1e-6)
        rl = np.maximum(np.ceil(np.log2(np.maximum(r / r0, 1.0))), 0).astype(np.int64)
        for k in range(int(rl.max()) + 1):
            ids = np.flatnonzero(rl == k)
            if len(ids):
                self.trees.append(_Tree(cKDTree(centre[ids]), ids, float(r[ids].max())))

    def _grid(self, ids, lo, hi, cell, span) -> _Grid:
        nx, ny = (np.floor(span / cell).astype(np.int64) + 1)
        i0 = np.floor((lo[ids, :2] - self.g0) / cell).astype(np.int64)
        i1 = np.minimum(np.floor((hi[ids, :2] - self.g0) / cell).astype(np.int64), [nx - 1, ny - 1])
        w = i1 - i0 + 1
        rep, k = _pairs(w[:, 0] * w[:, 1], np.zeros(len(ids), np.int64))
        ix = i0[rep, 0] + k % w[rep, 0]
        iy = i0[rep, 1] + k // w[rep, 0]
        cid = iy * nx + ix
        order = np.argsort(cid, kind="stable")
        start = np.searchsorted(cid[order], np.arange(nx * ny + 1)).astype(np.int64)
        return _Grid(cell, int(nx), int(ny), ids[rep[order]].astype(np.int32), start)

    # ------------------------------------------------------------------ vertical rays
    def vertical(self, origins, up: bool = False):
        """First face hit straight down (or up) from each origin: (distance, face), inf / -1
        for a miss. Hits closer than EPS are ignored."""
        o = np.atleast_2d(np.asarray(origins, np.float64))
        n = len(o)
        out_t, out_f = np.full(n, np.inf), np.full(n, -1, np.int64)
        if not self.n:
            return out_t, out_f
        for s in range(0, n, CHUNK):
            out_t[s:s + CHUNK], out_f[s:s + CHUNK] = self._vertical(o[s:s + CHUNK], up)
        return out_t, out_f

    def _vertical(self, o, up):
        n = len(o)
        qs, fs = [], []
        for g in self.grids:
            c = np.floor((o[:, :2] - self.g0) / g.cell).astype(np.int64)
            ok = (c[:, 0] >= 0) & (c[:, 1] >= 0) & (c[:, 0] < g.nx) & (c[:, 1] < g.ny)
            cid = np.where(ok, c[:, 1] * g.nx + c[:, 0], 0)
            counts = np.where(ok, g.start[cid + 1] - g.start[cid], 0)
            q, pos = _pairs(counts, g.start[cid])
            qs.append(q)
            fs.append(g.faces[pos].astype(np.int64))
        q, face = np.concatenate(qs), np.concatenate(fs)
        T = self.tri[face]
        a, b, cc = T[:, 0], T[:, 1], T[:, 2]
        p = o[q]
        ab, ac, ap = b - a, cc - a, p - a
        den = ab[:, 0] * ac[:, 1] - ab[:, 1] * ac[:, 0]
        with np.errstate(divide="ignore", invalid="ignore"):
            u = (ap[:, 0] * ac[:, 1] - ap[:, 1] * ac[:, 0]) / den
            v = (ab[:, 0] * ap[:, 1] - ab[:, 1] * ap[:, 0]) / den
            z = a[:, 2] + u * ab[:, 2] + v * ac[:, 2]
            t = (z - p[:, 2]) if up else (p[:, 2] - z)
            hit = (np.abs(den) > 1e-14) & (u >= -1e-7) & (v >= -1e-7) & (u + v <= 1 + 1e-7) & (t > EPS)
        return _first_per_query(q[hit], t[hit], face[hit], n)

    def contains(self, points) -> np.ndarray:
        """Points inside the (closed) mesh: the first surface straight below faces down and the
        first straight above faces up, i.e. both are seen from the inside."""
        P = np.atleast_2d(np.asarray(points, np.float64))
        if not self.n or not len(P):
            return np.zeros(len(P), bool)
        _, fd = self.vertical(P, up=False)
        _, fu = self.vertical(P, up=True)
        nz = self.normals[:, 2]
        return (fd >= 0) & (fu >= 0) & (nz[np.maximum(fd, 0)] < 0) & (nz[np.maximum(fu, 0)] > 0)

    # ------------------------------------------------------------------ segments
    def _near(self, centres: np.ndarray, radii: np.ndarray):
        """(query, face) for faces whose bounding sphere may come within radii of centres."""
        qs, fs = [], []
        for tr in self.trees:
            lists = tr.tree.query_ball_point(centres, radii + tr.radius)
            counts = np.fromiter((len(x) for x in lists), np.int64, len(lists))
            qs.append(np.repeat(np.arange(len(lists)), counts))
            fs.append(tr.faces[np.fromiter((i for x in lists for i in x), np.int64, int(counts.sum()))])
        return np.concatenate(qs), np.concatenate(fs)

    def segments(self, a, b):
        """First face hit going from each a to its b: (fraction of the way in (0, 1], face),
        inf / -1 where the segment is clear."""
        A = np.atleast_2d(np.asarray(a, np.float64))
        B = np.atleast_2d(np.asarray(b, np.float64))
        n = len(A)
        out_t, out_f = np.full(n, np.inf), np.full(n, -1, np.int64)
        if not self.n:
            return out_t, out_f
        for s in range(0, n, CHUNK):
            out_t[s:s + CHUNK], out_f[s:s + CHUNK] = self._segments(A[s:s + CHUNK], B[s:s + CHUNK])
        return out_t, out_f

    def _segments(self, A, B):
        d = B - A
        q, face = self._near((A + B) / 2, np.linalg.norm(d, axis=1) / 2)
        T = self.tri[face]
        o, dd = A[q], d[q]
        e1, e2 = T[:, 1] - T[:, 0], T[:, 2] - T[:, 0]
        p = np.cross(dd, e2)
        det = np.einsum("ij,ij->i", e1, p)
        with np.errstate(divide="ignore", invalid="ignore"):
            inv = 1.0 / det
            s = o - T[:, 0]
            u = np.einsum("ij,ij->i", s, p) * inv
            qv = np.cross(s, e1)
            v = np.einsum("ij,ij->i", dd, qv) * inv
            t = np.einsum("ij,ij->i", e2, qv) * inv
            ok = (np.abs(det) > 1e-14) & (u >= -1e-7) & (v >= -1e-7) & (u + v <= 1 + 1e-7) \
                & (t > EPS) & (t <= 1.0)
        return _first_per_query(q[ok], t[ok], face[ok], len(A))

    def rays(self, origins, directions, max_dist: float):
        """First hit within max_dist along unit directions: (distance, face), inf / -1 if none.
        Vertical directions use the grids; others are tested as segments."""
        O = np.atleast_2d(np.asarray(origins, np.float64))
        D = np.atleast_2d(np.asarray(directions, np.float64))
        D = D / np.maximum(np.linalg.norm(D, axis=1), 1e-12)[:, None]
        t = np.full(len(O), np.inf)
        f = np.full(len(O), -1, np.int64)
        vert = np.abs(D[:, 2]) > 1 - 1e-12
        for up in (False, True):
            m = vert & ((D[:, 2] > 0) == up)
            if m.any():
                t[m], f[m] = self.vertical(O[m], up=up)
        m = ~vert
        if m.any():
            frac, f[m] = self.segments(O[m], O[m] + D[m] * max_dist)
            t[m] = frac * max_dist
        far = t > max_dist
        t[far], f[far] = np.inf, -1
        return t, f

    def clear_of(self, points, clearance: float) -> np.ndarray:
        """Points at least `clearance` from every face."""
        P = np.atleast_2d(np.asarray(points, np.float64))
        ok = np.ones(len(P), bool)
        if not self.n:
            return ok
        for s in range(0, len(P), CHUNK):
            c = P[s:s + CHUNK]
            q, face = self._near(c, np.full(len(c), clearance))
            if len(q):
                cp = trimesh.triangles.closest_point(self.tri[face], c[q])
                close = np.linalg.norm(cp - c[q], axis=1) < clearance
                ok[s + q[close]] = False
        return ok
