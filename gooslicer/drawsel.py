"""Select by Drawing (11 §2) [GOOSLICER]: a rough line painted on the model round a part; the
part inside it is selected, its edge cut along the creases under the paint.

1. Painting: faces within the brush radius (along the surface) of each picked face.
2. Inside: the unpainted surface falls apart along the paint; the smallest piece touching it is
   the inside, the others are outside (pieces under two brush dabs are gaps in the paint). Gaps in the paint are closed by widening it, a little at
   a time.
3. The edge: a minimum s-t cut through the drawn region (inside and paint) between its core
   (the faces farthest from the paint) and the outside, crossing cheap along valleys (the Parts
   rule), with a slight pull out toward the line; optionally pulled toward an image model's
   outline (`ai`).
"""
from __future__ import annotations

import numpy as np
import scipy.sparse as sp
from scipy.sparse import csgraph

from . import modify

VALLEY_SCALE = 20.0       # degrees: crossing cost falls by e per this much valley (as Parts)
RIDGE_SHARE = 0.15        # ridges lower it this much as valleys do
AI_PULL = 0.5             # per mm: an AI vote on a face, × its area, against the cut cost
BALLOON = 0.05            # per mm: what keeping an inside face earns, × its area
CORE = 0.5                # inside faces at least this share of the greatest distance from the paint
LEAK = 0.1                # an AI outline may reach outside the drawing by this share of its area
CORE_COVER = 0.8          # …and must cover this share of the core
CLOSE_STEPS = 3           # widen the paint up to this many times to close gaps
SCALE = 1e4               # capacities to integers (maximum_flow wants them)


def paint(s: modify.Surface, faces, radius: float) -> np.ndarray:
    """Bool per face: within `radius` (mm, along the surface: face centre to face centre) of
    one of `faces`."""
    faces = np.unique(np.asarray(faces, np.int64))
    n = len(s.areas)
    out = np.zeros(n, bool)
    if len(faces) == 0:
        return out
    out[faces] = True
    if radius <= 0:
        return out
    a = s.adjacency
    d = np.linalg.norm(s.centers[a[:, 0]] - s.centers[a[:, 1]], axis=1)
    g = modify._face_graph(s, d + 1e-9)
    dist = csgraph.dijkstra(g, indices=faces, min_only=True, limit=radius)
    return np.isfinite(dist) & (dist <= radius)


def _pieces(s: modify.Surface, painted: np.ndarray):
    """(label per face, -1 painted; the labels touching the paint)."""
    n = len(painted)
    a = s.adjacency
    un = ~painted
    e = a[un[a[:, 0]] & un[a[:, 1]]]
    g = sp.coo_matrix((np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(n, n))
    _, lab = csgraph.connected_components(g, directed=False)
    lab = np.where(painted, -1, lab)
    border = a[painted[a[:, 0]] ^ painted[a[:, 1]]]
    touch = np.unique(np.where(painted[border[:, 0]], lab[border[:, 1]], lab[border[:, 0]]))
    return lab, touch[touch >= 0]


def inside(s: modify.Surface, painted: np.ndarray, radius: float = 1.0):
    """(inside, outside, painted) face masks, or None when the drawing walls nothing off.
    Inside: the smallest unpainted piece touching the paint, at the first paint width (widened
    by half the brush, up to CLOSE_STEPS times) that walls off the most pieces."""
    speck = 2 * np.pi * max(radius, 0.5) ** 2
    best = None
    for step in range(CLOSE_STEPS + 1):
        if step:
            painted = paint(s, np.nonzero(painted)[0], radius * 0.5 * step)
        lab, touch = _pieces(s, painted)
        area = np.bincount(lab[lab >= 0], s.areas[lab >= 0], minlength=lab.max() + 1)
        small = touch[area[touch] < speck]            # gaps between brush dabs: paint them
        if len(small):
            painted = painted | np.isin(lab, small)
            touch = touch[area[touch] >= speck]
        # a gap can join the inside to an outside piece: the first width that walls off the
        # most pieces closes the gaps
        if len(touch) >= 2 and (best is None or len(touch) > best[0]):
            k = touch[np.argmin(area[touch])]
            inn = lab == k
            best = (len(touch), (inn, ~painted & ~inn, painted.copy()))
    return None if best is None else best[1]


def crossing_costs(s: modify.Surface, fold: np.ndarray) -> np.ndarray:
    """The cost of the edge running between each adjacent face pair: shared edge length ×
    exp(−valley / VALLEY_SCALE), ridges counting RIDGE_SHARE."""
    e = s.edges
    length = np.linalg.norm(s.vertices[e[:, 0]] - s.vertices[e[:, 1]], axis=1)
    cost = np.clip(fold, 0, None) + RIDGE_SHARE * np.clip(-fold, 0, None)
    return length * np.exp(-cost / VALLEY_SCALE)


def core(s: modify.Surface, inn: np.ndarray, painted: np.ndarray) -> np.ndarray:
    """The faces of the inside farthest from the paint (at least CORE of the greatest distance,
    along the surface): kept whatever the cut."""
    a = s.adjacency
    d = np.linalg.norm(s.centers[a[:, 0]] - s.centers[a[:, 1]], axis=1)
    keep = (inn | painted)[a[:, 0]] & (inn | painted)[a[:, 1]]
    n = len(inn)
    g = sp.coo_matrix((d[keep] + 1e-9, (a[keep, 0], a[keep, 1])), shape=(n, n)).tocsr()
    dist = csgraph.dijkstra(g, directed=False, indices=np.nonzero(painted)[0], min_only=True)
    dist = np.where(inn & np.isfinite(dist), dist, 0.0)
    return inn & (dist >= CORE * dist.max()) & (dist > 0)


def cut(s: modify.Surface, inn: np.ndarray, out: np.ndarray, painted: np.ndarray, fold: np.ndarray,
        ai: np.ndarray | None = None, ai_seen: np.ndarray | None = None) -> np.ndarray:
    """The selection: the cheapest cut through the drawn region (inside and paint) between its
    core and the outside. Crossing costs `crossing_costs`; each inside face kept earns
    BALLOON × its area (so on a smooth surface the edge moves out to the line). `ai`/`ai_seen`:
    bool per face, an image model's outline and the faces its picture sees; faces of the drawn
    region are pulled toward its answer by AI_PULL × their area."""
    a = s.adjacency
    w = crossing_costs(s, fold)
    dom = inn | painted
    idx = np.nonzero(dom)[0]
    m = len(idx)
    if m == 0:
        return inn.copy()
    hard = core(s, inn, painted)
    node = np.full(len(dom), -1)
    node[idx] = np.arange(m)
    S, T = m, m + 1
    rows, cols, caps = [], [], []

    def add(u, v, c):
        rows.append(np.asarray(u, np.int64).ravel())
        cols.append(np.asarray(v, np.int64).ravel())
        caps.append(np.broadcast_to(np.asarray(c, float), np.asarray(u).shape).ravel())
    both = dom[a[:, 0]] & dom[a[:, 1]]                         # within the region: both ways
    add(node[a[both, 0]], node[a[both, 1]], w[both])
    add(node[a[both, 1]], node[a[both, 0]], w[both])
    for x, y in ((0, 1), (1, 0)):                              # next to the outside
        sel = dom[a[:, x]] & out[a[:, y]]
        add(node[a[sel, x]], np.full(sel.sum(), T), w[sel])
    big = 1e6
    h = np.nonzero(hard[idx])[0]
    add(np.full(len(h), S), h, big)
    b = np.nonzero(inn[idx])[0]
    add(np.full(len(b), S), b, BALLOON * s.areas[idx[b]])
    if ai is not None:
        seen = np.ones(len(dom), bool) if ai_seen is None else ai_seen
        pull = AI_PULL * s.areas[idx]
        yes = np.nonzero(ai[idx] & seen[idx])[0]
        no = np.nonzero(~ai[idx] & seen[idx])[0]
        add(np.full(len(yes), S), yes, pull[yes])
        add(no, np.full(len(no), T), pull[no])
    r, c, cap = np.concatenate(rows), np.concatenate(cols), np.concatenate(caps)
    G = sp.coo_matrix((cap * SCALE, (r, c)), shape=(m + 2, m + 2)).tocsr()   # sums duplicates
    G.data = np.clip(np.round(G.data), 1, 2 ** 31 - 1).astype(np.int32)
    flow = csgraph.maximum_flow(G, S, T).flow
    resid = (G - flow.tocsr()).tocsr()
    resid.data = (resid.data > 0).astype(np.int8)
    resid.eliminate_zeros()
    reach = csgraph.breadth_first_order(resid, S, directed=True, return_predecessors=False)
    side = np.zeros(m + 2, bool)
    side[reach] = True
    sel = np.zeros(len(dom), bool)
    sel[idx[side[:m]]] = True
    return sel


def pick_outline(s: modify.Surface, inn, painted, core, outlines, seen, leak: float = LEAK):
    """(outline, seen) for the AI: the largest of an image model's outlines (bool per face) that
    covers the core (as seen) and leaks out of the drawing (inside and paint) by under `leak` of
    its area; or None. The line is rough, so the outline isn't matched to it: the model finds
    the thing, the drawing only bounds it."""
    A = s.areas
    dom = inn | painted
    cs = core & seen
    best = None
    for f in outlines:
        a = A[f].sum()
        if a <= 0 or A[f & ~dom].sum() > leak * a:
            continue
        if cs.any() and A[f & cs].sum() < CORE_COVER * A[cs].sum():
            continue
        if best is None or a > A[best].sum():
            best = f
    return None if best is None else (best, seen)


def select(s: modify.Surface, painted: np.ndarray, fold: np.ndarray, radius: float = 1.0,
           ai=None, ai_seen=None):
    """The part a drawing walls off, or None (nothing walled off)."""
    r = inside(s, painted, radius)
    if r is None:
        return None
    inn, out, painted = r
    return cut(s, inn, out, painted, fold, ai, ai_seen)
