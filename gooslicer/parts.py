"""Parts of a model's surface (11 §2, Select by Parts) [GOOSLICER].

The surface is split into the pieces a person would name (an ear, a strap, the mask round the
eyes) by the minima rule: one shape meets another along a valley.
1. Patches: seeds on a 1.2 mm grid; each face goes to the nearest seed along the surface, where
   crossing a contour costs extra, so patch borders lie on creases.
2. Normalized cuts on the patch graph (borders weighted down where they run along a valley)
   split off regions only weakly attached to the rest, even where a crease has gaps.
3. Neighbouring parts whose border isn't a valley are merged back (normalized cuts also split
   smooth surfaces where nothing marks a border).
"""
from __future__ import annotations

import heapq

import numpy as np
import scipy.sparse as sp
from scipy.sparse import csgraph
from scipy.sparse.linalg import eigsh

from . import modify

PATCH_CELL = 1.2          # mm: one patch seed per cell of this size, per shell
CREASE_FOLD = 20.0        # degrees: contours at this fold wall the patches in
CREASE_COST = 200.0       # crossing a crease costs this many times its length (a near-wall)
VALLEY_SCALE = 20.0       # degrees: a border's weight falls by e per this much valley
RIDGE_SHARE = 0.15        # ridges weaken a border this much as valleys do
NCUT_MAX = 0.1            # split while the normalized cut is under this
MIN_PART = 1.0            # mm²: smaller parts always merge
MIN_PATCHES = 3           # a split keeps at least this many patches each side


class Parts:
    """The slow, detail-independent stages (patches and normalized cuts) for one surface;
    `labels(edge_deg)` merges them back for a Part edges angle (fast)."""

    def __init__(self, s: modify.Surface, fold: np.ndarray):
        self.s, self.fold = s, fold
        crease = modify.contours(s, CREASE_FOLD, fold=fold)
        self.patch, k = _patches(s, crease)
        W = _patch_graph(s, fold, self.patch, k)
        self.cut = _ncut(W, k)[self.patch]
        self._edges = _border_stats(s, fold, self.cut)

    def labels(self, edge_deg: float) -> np.ndarray:
        """Part label per face (0..n-1) at this Part edges angle."""
        return _merge(self.s, self.cut, self._edges, float(edge_deg))


def parts(s: modify.Surface, edge_deg: float = 10.0, fold: np.ndarray | None = None) -> np.ndarray:
    """Part label per face (0..n-1). `fold`: modify.fold_angles(s) if already worked out."""
    return Parts(s, modify.fold_angles(s) if fold is None else fold).labels(edge_deg)


def edges(s: modify.Surface, labels: np.ndarray) -> np.ndarray:
    """Bool per adjacency row: the edge lies between two parts."""
    a = s.adjacency
    return labels[a[:, 0]] != labels[a[:, 1]]


def _patches(s: modify.Surface, crease: np.ndarray, cell: float = PATCH_CELL):
    c = s.centers
    n = len(c)
    _, shell = csgraph.connected_components(modify._face_graph(s), directed=False)
    key = np.c_[np.floor((c - c.min(axis=0)) / cell).astype(np.int64), shell]
    _, seeds = np.unique(key, axis=0, return_index=True)
    a = s.adjacency
    d = np.linalg.norm(c[a[:, 0]] - c[a[:, 1]], axis=1)
    g = modify._face_graph(s, d * np.where(crease, CREASE_COST, 1.0) + 1e-9)
    _, _, src = csgraph.dijkstra(g, indices=seeds, min_only=True, return_predecessors=True)
    remap = np.full(n, -1)
    remap[seeds] = np.arange(len(seeds))
    return remap[src], len(seeds)


def _edge_lengths(s: modify.Surface) -> np.ndarray:
    e = s.edges
    return np.linalg.norm(s.vertices[e[:, 0]] - s.vertices[e[:, 1]], axis=1)


def _patch_graph(s, fold, lab, k):
    a = s.adjacency
    la, lb = lab[a[:, 0]], lab[a[:, 1]]
    x = la != lb
    cost = np.clip(fold, 0, None) + RIDGE_SHARE * np.clip(-fold, 0, None)
    w = _edge_lengths(s)[x] * np.exp(-cost[x] / VALLEY_SCALE)
    W = sp.coo_matrix((np.r_[w, w], (np.r_[la[x], lb[x]], np.r_[lb[x], la[x]])), shape=(k, k)).tocsr()
    W.sum_duplicates()
    return W


def _best_split(W, idx):
    """(normalized cut, mask) of the best sweep split of subgraph `idx` along its Fiedler
    vector, or None."""
    sub = W[idx][:, idx]
    d = np.asarray(sub.sum(axis=1)).ravel()
    if len(idx) < 2 * MIN_PATCHES or (d <= 0).any():
        return None
    Dm = sp.diags(1 / np.sqrt(d))
    try:                               # the largest eigenvalue is 1 (constant); the next is Fiedler's
        vals, vecs = eigsh(Dm @ sub @ Dm, k=2, which="LA", tol=1e-4, maxiter=5000)
    except Exception:
        return None
    v = vecs[:, np.argsort(vals)[0]] / np.sqrt(d)
    order = np.argsort(v)
    pos = np.empty(len(idx), int)
    pos[order] = np.arange(len(idx))
    C = sub.tocoo()
    pi, pj = pos[C.row], pos[C.col]
    m = pi < pj                        # an edge is cut for prefixes ending in [pos_i, pos_j)
    delta = np.zeros(len(idx) + 1)
    np.add.at(delta, pi[m], C.data[m])
    np.add.at(delta, pj[m], -C.data[m])
    cut = np.cumsum(delta)[:-1]
    volS = np.cumsum(d[order])
    vol = d.sum()
    with np.errstate(divide="ignore", invalid="ignore"):
        nc = cut / volS + cut / (vol - volS)
    nc[:MIN_PATCHES - 1] = np.inf
    nc[len(idx) - MIN_PATCHES:] = np.inf
    if not np.isfinite(nc).any():
        return None
    i = int(np.nanargmin(nc))
    mask = np.zeros(len(idx), bool)
    mask[order[: i + 1]] = True
    return float(nc[i]), mask


def _ncut(W, k):
    """Patch → region label, by recursive normalized cuts."""
    _, comp = csgraph.connected_components(W, directed=False)
    out = np.full(k, -1)
    nxt = 0
    stack = [np.nonzero(comp == c)[0] for c in range(comp.max() + 1)]
    while stack:
        idx = stack.pop()
        r = _best_split(W, idx)
        if r is not None and r[0] < NCUT_MAX:
            for half in (idx[r[1]], idx[~r[1]]):          # a half may fall apart: each piece
                _, cc = csgraph.connected_components(W[half][:, half], directed=False)
                stack.extend(half[cc == c] for c in range(cc.max() + 1))
        else:
            out[idx] = nxt
            nxt += 1
    return out


def _border_stats(s, fold, lab):
    """{(i, j): [length, length × valley]} for each pair of neighbouring labels, i < j."""
    a = s.adjacency
    la, lb = lab[a[:, 0]], lab[a[:, 1]]
    x = la != lb
    p = np.sort(np.c_[la[x], lb[x]], axis=1)
    L = _edge_lengths(s)[x]
    k = int(lab.max()) + 1
    key, inv = np.unique(p[:, 0] * k + p[:, 1], return_inverse=True)
    blen = np.bincount(inv, L)
    bval = np.bincount(inv, L * np.clip(fold[x], 0, None))
    return {(int(q) // k, int(q) % k): (bl, bv) for q, bl, bv in zip(key, blen, bval)}


def _merge(s, lab, stats, edge_deg):
    """Merge neighbouring labels, weakest border first, while the length-weighted mean valley
    along it is under `edge_deg` (or a side is under MIN_PART)."""
    k = int(lab.max()) + 1
    area = np.bincount(lab, s.areas, minlength=k)
    nb = [dict() for _ in range(k)]
    for (i, j), (bl, bv) in stats.items():
        nb[i][j] = nb[j][i] = [bl, bv]
    parent = np.arange(k)
    alive = np.ones(k, bool)

    def cost(i, j):
        bl, bv = nb[i][j]
        return bv / bl - (1e6 if min(area[i], area[j]) < MIN_PART else 0.0)

    h = [(cost(i, j), i, j) for i in range(k) for j in nb[i] if i < j]
    heapq.heapify(h)
    while h:
        c, i, j = heapq.heappop(h)
        if not (alive[i] and alive[j]) or j not in nb[i]:
            continue
        cc = cost(i, j)
        if cc != c:                          # stale: its border changed since it was queued
            heapq.heappush(h, (cc, i, j))
            continue
        if c >= edge_deg:
            break
        if len(nb[j]) > len(nb[i]):
            i, j = j, i
        alive[j] = False
        parent[j] = i
        area[i] += area[j]
        del nb[i][j]
        for m, v in nb[j].items():
            if m == i:
                continue
            del nb[m][j]
            if m in nb[i]:
                nb[i][m][0] += v[0]
                nb[i][m][1] += v[1]
            else:
                nb[i][m] = v
            nb[m][i] = nb[i][m]
        nb[j] = {}
        for m in nb[i]:
            heapq.heappush(h, (cost(i, m), min(i, m), max(i, m)))
    root = parent
    while True:
        r2 = root[root]
        if np.array_equal(r2, root):
            break
        root = r2
    return np.unique(root[lab], return_inverse=True)[1]
