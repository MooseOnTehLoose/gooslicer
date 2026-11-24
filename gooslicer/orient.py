"""Auto position: the rotation that needs the fewest supports [GOOSLICER].

Every candidate "up" direction is scored by an estimate of the support contacts the generator
(supports.detect_contacts) would place, plus small costs for print height and for large areas
lying flat against the plate:

- overhang contacts: faces tilted less than the critical angle from horizontal need support;
  contacts go in at anchor spacing over their area as seen from below (projected area / spacing²).
  Faces within FLAT_DEG of facing straight down count FLAT_COST times: big flat undersides peel
  hard off the vat film, which is why resin parts are usually printed tilted;
- lowest points: vertices with no lower neighbour, facing down (as supports._lowest_points), with
  no overhang face around them to cover them: one contact each;
- down-facing ridges: sharp convex edges pointing down and running less than the critical angle
  from horizontal, between faces that don't need support themselves (the bottom edges of a tilted
  plate): one contact per anchor spacing along them;
- height: HEIGHT_COST contacts per mm, so near-ties go to the lower (faster) print.

About 300 directions spread over the sphere are scored, then the best few are refined within
REFINE_DEG. The part is only turned if that beats where it is now by MIN_GAIN.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

COARSE = 300            # directions over the whole sphere (about 12° apart)
REFINE_TOP = 4          # best coarse directions refined
REFINE_N = 40           # directions in each refinement cap
REFINE_DEG = 12.0       # cap radius
CHUNK = 32              # directions scored at once
FLAT_DEG = 10.0
FLAT_COST = 1.3
HEIGHT_COST = 0.05      # contacts per mm of height
TIE = 0.01              # scores within 1 % are a tie
MIN_GAIN = 0.03         # turn only for at least 3 % fewer (estimated) supports
LOWEST_FACING = -0.3    # a lowest point needs support when its normal points down this much
RIDGE_DEG = 30.0        # edges sharper than this (angle between the face normals) are ridges


@dataclass
class Score:
    total: float
    overhang_area: float     # mm², needing support (true area, not projected)
    contacts: float          # estimated support contacts
    height: float            # mm


@dataclass
class OrientResult:
    up: np.ndarray           # the world direction to turn to +Z
    best: Score
    current: Score

    @property
    def improves(self) -> bool:
        return self.best.total < self.current.total * (1 - MIN_GAIN) - 1e-9

    @property
    def tilt_deg(self) -> float:
        return math.degrees(math.acos(float(np.clip(self.up[2], -1, 1))))


def fibonacci_sphere(n: int) -> np.ndarray:
    i = np.arange(n) + 0.5
    z = 1 - 2 * i / n
    r = np.sqrt(1 - z * z)
    phi = i * math.pi * (3 - math.sqrt(5))
    return np.c_[r * np.cos(phi), r * np.sin(phi), z]


def _cap(axis: np.ndarray, n: int, radius_deg: float) -> np.ndarray:
    """n directions spread over a spherical cap around axis."""
    c = math.cos(math.radians(radius_deg))
    i = np.arange(n) + 0.5
    z = 1 - (1 - c) * i / n
    r = np.sqrt(1 - z * z)
    phi = i * math.pi * (3 - math.sqrt(5))
    local = np.c_[r * np.cos(phi), r * np.sin(phi), z]
    a = axis / np.linalg.norm(axis)
    t = np.cross(a, [1.0, 0, 0] if abs(a[0]) < 0.9 else [0, 1.0, 0])
    t /= np.linalg.norm(t)
    b = np.cross(a, t)
    return local @ np.vstack([t, b, a])


class _Mesh:
    """What the score needs, computed once: face normals and areas, vertex normals, edges."""

    def __init__(self, vertices: np.ndarray, faces: np.ndarray):
        v = np.asarray(vertices, np.float64)
        f = np.asarray(faces, np.int64)
        cr = np.cross(v[f[:, 1]] - v[f[:, 0]], v[f[:, 2]] - v[f[:, 0]])
        dbl = np.linalg.norm(cr, axis=1)
        ok = dbl > 1e-15
        f, cr, dbl = f[ok], cr[ok], dbl[ok]
        self.area = (dbl / 2).astype(np.float32)
        self.normal = (cr / dbl[:, None]).astype(np.float32)
        vn = np.zeros_like(v)
        for k in range(3):
            np.add.at(vn, f[:, k], cr)
        ln = np.linalg.norm(vn, axis=1)
        vn[ln > 0] /= ln[ln > 0, None]
        self.vnormal = vn.astype(np.float32)
        used = np.unique(f)
        self.v = v.astype(np.float32)
        self.f = f
        self.used = used
        e = np.sort(f[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
        edges = np.unique(e, axis=0)
        # per used vertex: its neighbours, and the faces around it (rows for reduceat)
        a = np.r_[edges[:, 0], edges[:, 1]]
        b = np.r_[edges[:, 1], edges[:, 0]]
        o = np.argsort(a, kind="stable")
        self.nbr = b[o]
        self.nbr_start = np.searchsorted(a[o], used)
        fv = f.ravel()
        o = np.argsort(fv, kind="stable")
        self.vf = (o // 3)
        self.vf_start = np.searchsorted(fv[o], used)
        # ridges: two-face edges with a sharp convex bend
        fid = np.repeat(np.arange(len(f)), 3)
        order = np.lexsort((e[:, 1], e[:, 0]))
        es = e[order]
        same = (es[1:] == es[:-1]).all(axis=1)
        first = np.r_[True, ~same]
        cnt = np.diff(np.r_[np.flatnonzero(first), len(es)])
        st = np.flatnonzero(first)[cnt == 2]
        fa, fb = fid[order[st]], fid[order[st + 1]]
        na, nb = self.normal[fa].astype(np.float64), self.normal[fb].astype(np.float64)
        ev = v[es[st, 1]] - v[es[st, 0]]
        other = v[f[fb]].mean(axis=1) - v[f[fa]].mean(axis=1)
        convex = np.einsum("ij,ij->i", other, na) < 0          # b lies below a's plane
        sharp = np.einsum("ij,ij->i", na, nb) < math.cos(math.radians(RIDGE_DEG))
        r = convex & sharp
        rn = na[r] + nb[r]
        rl = np.linalg.norm(rn, axis=1)
        keep = rl > 1e-9
        self.ridge_faces = np.c_[fa[r][keep], fb[r][keep]]
        self.ridge_normal = (rn[keep] / rl[keep, None]).astype(np.float32)
        evr = ev[r][keep]
        self.ridge_len = np.linalg.norm(evr, axis=1).astype(np.float32)
        self.ridge_dir = (evr / np.maximum(self.ridge_len[:, None], 1e-12)).astype(np.float32)


def score_directions(m: _Mesh, ups: np.ndarray, critical_angle: float, anchor: float) -> list[Score]:
    """Score each unit 'up' direction (rows of ups)."""
    cos_crit = math.cos(math.radians(critical_angle))
    cos_flat = math.cos(math.radians(FLAT_DEG))
    per_contact = 1.0 / (anchor * anchor)
    out: list[Score] = []
    for s in range(0, len(ups), CHUNK):
        U = np.asarray(ups[s:s + CHUNK], np.float32).T                 # 3 × k
        k = U.shape[1]
        down = -(m.normal @ U)                                          # F × k: 1 = facing straight down
        need = down > cos_crit
        w = np.where(down > cos_flat, FLAT_COST, 1.0).astype(np.float32)
        proj = ((m.area[:, None] * down * w) * need).sum(axis=0)
        area = (m.area[:, None] * need).sum(axis=0)
        h = m.v @ U                                                     # V × k
        hu = h[m.used]
        lowest_nbr = np.minimum.reduceat(h[m.nbr], m.nbr_start, axis=0)
        is_min = lowest_nbr > hu + 1e-6                                  # no neighbour lower or level
        covered = np.maximum.reduceat(need[m.vf].view(np.uint8), m.vf_start, axis=0).astype(bool)
        vdown = -(m.vnormal[m.used] @ U)
        lowest = is_min & (vdown >= -LOWEST_FACING) & ~covered
        n_low = lowest.sum(axis=0)
        # down-facing ridges between faces that don't need support
        rdown = -(m.ridge_normal @ U)
        rslope = np.abs(m.ridge_dir @ U)                                # sin of the edge's rise
        ridge = (rdown > cos_crit) & (rslope < math.sin(math.radians(critical_angle)))
        ridge &= ~need[m.ridge_faces[:, 0]] & ~need[m.ridge_faces[:, 1]]
        ridge_contacts = (m.ridge_len[:, None] * ridge).sum(axis=0) / anchor
        height = hu.max(axis=0) - hu.min(axis=0)
        contacts = proj * per_contact + n_low + ridge_contacts
        total = contacts + HEIGHT_COST * height
        for j in range(k):
            out.append(Score(float(total[j]), float(area[j]), float(contacts[j]), float(height[j])))
    return out


def best_orientation(vertices: np.ndarray, faces: np.ndarray, critical_angle: float = 50.0,
                     anchor: float = 2.0, progress=None) -> OrientResult:
    """The up direction (in the frame of `vertices`, normally world) needing the fewest supports.
    progress(done, total) is called as directions are scored."""
    m = _Mesh(vertices, faces)
    current = score_directions(m, np.array([[0.0, 0, 1]]), critical_angle, anchor)[0]
    coarse = fibonacci_sphere(COARSE)
    total = COARSE + REFINE_TOP * REFINE_N
    scores: list[Score] = []
    for s in range(0, COARSE, CHUNK * 4):
        scores += score_directions(m, coarse[s:s + CHUNK * 4], critical_angle, anchor)
        if progress:
            progress(len(scores), total)
    order = np.argsort([x.total for x in scores])
    cands = [(scores[i], coarse[i]) for i in order[:1]]
    picked: list[np.ndarray] = []
    for i in order:                                  # the best few, not all next to each other
        if all(float(coarse[i] @ p) < math.cos(math.radians(2 * REFINE_DEG)) for p in picked):
            picked.append(coarse[i])
        if len(picked) == REFINE_TOP:
            break
    for n, axis in enumerate(picked):
        dirs = _cap(axis, REFINE_N, REFINE_DEG)
        for sc, d in zip(score_directions(m, dirs, critical_angle, anchor), dirs):
            cands.append((sc, d))
        if progress:
            progress(COARSE + (n + 1) * REFINE_N, total)
    low = min(c[0].total for c in cands)
    # near-ties (symmetric parts) go to the smallest turn from where the part is
    best, up = max((c for c in cands if c[0].total <= low * (1 + TIE) + 1e-9), key=lambda t: t[1][2])
    if best.total >= current.total:                  # nothing better than where it is
        best, up = current, np.array([0.0, 0, 1])
    return OrientResult(np.asarray(up, float) / np.linalg.norm(up), best, current)
