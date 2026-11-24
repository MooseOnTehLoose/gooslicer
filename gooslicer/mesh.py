"""Mesh import, analysis and repair (Qt-free)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

SUPPORTED = (".stl", ".obj", ".3mf")


class MeshError(ValueError):
    pass


def load_meshes(path: str | os.PathLike) -> list[tuple[str, trimesh.Trimesh]]:
    """Load a model file into one or more named triangle meshes (world units = mm).

    3MF/OBJ files may hold several objects; their build transforms are applied.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise MeshError(f"unsupported file type {ext!r} (supported: {', '.join(SUPPORTED)})")
    try:
        loaded = trimesh.load(str(p), force="scene", process=False)
    except Exception as e:  # trimesh raises a variety of errors on bad input
        raise MeshError(f"cannot read {p.name}: {e}") from e
    out: list[tuple[str, trimesh.Trimesh]] = []
    geoms = loaded.dump(concatenate=False) if isinstance(loaded, trimesh.Scene) else [loaded]
    for i, g in enumerate(geoms):
        if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
            continue
        g = trimesh.Trimesh(vertices=np.asarray(g.vertices, dtype=np.float64),
                            faces=np.asarray(g.faces, dtype=np.int64), process=True)
        name = p.stem if len(geoms) == 1 else f"{p.stem} [{g.metadata.get('name') or i + 1}]"
        out.append((name, g))
    if not out:
        raise MeshError(f"{p.name} contains no triangles")
    return out


@dataclass(frozen=True)
class MeshReport:
    triangles: int
    vertices: int
    components: int
    boundary_edges: int        # edges used by exactly one face (holes)
    nonmanifold_edges: int     # edges used by more than two faces
    degenerate_faces: int
    duplicate_faces: int
    winding_consistent: bool
    watertight: bool
    inverted: bool             # closed but normals point inwards
    volume: float | None       # mm³, only when watertight

    @property
    def ok(self) -> bool:
        return (self.watertight and self.winding_consistent and not self.inverted
                and self.degenerate_faces == 0 and self.duplicate_faces == 0
                and self.nonmanifold_edges == 0)

    def problems(self) -> list[str]:
        out = []
        if self.boundary_edges:
            out.append(f"{self.boundary_edges} open (hole) edges")
        if self.nonmanifold_edges:
            out.append(f"{self.nonmanifold_edges} non-manifold edges")
        if self.degenerate_faces:
            out.append(f"{self.degenerate_faces} degenerate triangles")
        if self.duplicate_faces:
            out.append(f"{self.duplicate_faces} duplicate triangles")
        if not self.winding_consistent:
            out.append("inconsistent triangle orientation")
        if self.inverted:
            out.append("normals point inwards")
        return out


def _edge_use_counts(faces: np.ndarray) -> np.ndarray:
    """How many faces use each edge (one count per distinct edge). Edges are packed into one
    int64 key each and sorted: several times faster than np.unique(axis=0)."""
    if not len(faces):
        return np.zeros(0, int)
    e = np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2).astype(np.int64), axis=1)
    key = np.sort(e[:, 0] * (int(e.max()) + 1) + e[:, 1])
    starts = np.flatnonzero(np.r_[True, key[1:] != key[:-1]])
    return np.diff(np.r_[starts, len(key)])


def _duplicate_faces(faces: np.ndarray) -> int:
    """Faces using the same three vertices as an earlier one (either orientation)."""
    if not len(faces):
        return 0
    f = np.sort(faces, axis=1)
    f = f[np.lexsort((f[:, 2], f[:, 1], f[:, 0]))]
    return int((f[1:] == f[:-1]).all(axis=1).sum())


def analyze(mesh: trimesh.Trimesh) -> MeshReport:
    faces = np.asarray(mesh.faces)
    counts = _edge_use_counts(faces) if len(faces) else np.zeros(0, int)
    area = mesh.area_faces if len(faces) else np.zeros(0)
    degenerate = int(np.count_nonzero(area <= 1e-12))
    dup = _duplicate_faces(faces)
    boundary = int(np.count_nonzero(counts == 1))
    nonmanifold = int(np.count_nonzero(counts > 2))
    watertight = len(faces) > 0 and boundary == 0 and nonmanifold == 0
    winding = bool(mesh.is_winding_consistent) if len(faces) else True
    vol = float(mesh.volume) if watertight and winding else None
    comps = int(trimesh.graph.connected_component_labels(mesh.face_adjacency, len(faces)).max() + 1) \
        if len(faces) else 0
    return MeshReport(len(faces), len(mesh.vertices), comps, boundary, nonmanifold, degenerate, dup,
                      winding, watertight, vol is not None and vol < 0, abs(vol) if vol is not None else None)


DEBRIS_THICKNESS_MM = 0.002     # a shell thinner than this on average (3 × volume / area) is debris
DEBRIS_VOLUME_MM3 = 0.001       # ... and so is one smaller than a 0.1 mm cube (far below a pixel column)


def _signed_volumes(v: np.ndarray, f: np.ndarray, labels: np.ndarray, n: int) -> np.ndarray:
    a, b, c = v[f[:, 0]], v[f[:, 1]], v[f[:, 2]]
    return np.bincount(labels, np.einsum("ij,ij->i", a, np.cross(b, c)) / 6.0, minlength=n)


def _face_labels(faces: np.ndarray) -> tuple[np.ndarray, int]:
    """Connected shells: faces sharing an edge (any number of users) are one shell."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    n = len(faces)
    if not n:
        return np.zeros(0, np.int64), 0
    pairs = _edge_pairs(faces, manifold_only=False)
    g = coo_matrix((np.ones(len(pairs)), (pairs[:, 0], pairs[:, 1])), shape=(n, n))
    k, lab = connected_components(g, directed=False)
    return lab, k


def _edge_groups(faces: np.ndarray):
    """Half-edges (u -> v) grouped by undirected edge: (half_edges, face of each, order, starts, counts)."""
    he = faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2)
    fid = np.repeat(np.arange(len(faces)), 3)
    key = np.sort(he, axis=1)
    order = np.lexsort((key[:, 1], key[:, 0]))
    k = key[order]
    new = np.r_[True, (k[1:] != k[:-1]).any(axis=1)]
    starts = np.flatnonzero(new)
    counts = np.diff(np.r_[starts, len(k)])
    return he, fid, order, starts, counts


def _edge_pairs(faces: np.ndarray, manifold_only: bool = True) -> np.ndarray:
    """(face, face) pairs that share an edge; with manifold_only, edges with exactly two users."""
    he, fid, order, starts, counts = _edge_groups(faces)
    if manifold_only:
        s = starts[counts == 2]
        return np.c_[fid[order[s]], fid[order[s + 1]]]
    same = np.ones(len(order), bool)
    same[starts] = False                                   # half-edge i shares its edge with i - 1
    i = np.flatnonzero(same)
    return np.c_[fid[order[i - 1]], fid[order[i]]]


def _orient(faces: np.ndarray) -> np.ndarray:
    """Faces flipped so that neighbours across every two-user edge agree: one breadth-first walk
    over all shells (joined through an extra root node), each face taking its parent's flip."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import breadth_first_order, connected_components
    n = len(faces)
    he, fid, order, starts, counts = _edge_groups(faces)
    s = starts[counts == 2]
    a, b = order[s], order[s + 1]
    flip = (he[a, 0] == he[b, 0]).astype(np.int8)            # same direction: one must turn over
    fa, fb = fid[a], fid[b]
    _, lab = connected_components(coo_matrix((np.ones(len(fa)), (fa, fb)), shape=(n, n)), directed=False)
    roots = np.unique(lab, return_index=True)[1]
    src = np.r_[fa, fb, np.full(len(roots), n)]
    dst = np.r_[fb, fa, roots]
    g = coo_matrix((np.ones(len(src)), (src, dst)), shape=(n + 1, n + 1)).tocsr()
    bfs, pred = breadth_first_order(g, n, directed=True, return_predecessors=True)
    bfs, pred = bfs.astype(np.int64), pred.astype(np.int64)        # keys below overflow int32
    # the flip across each tree edge (pred -> node)
    keys = np.r_[fa * (n + 1) + fb, fb * (n + 1) + fa]
    vals = np.r_[flip, flip]
    srt = np.argsort(keys)
    keys, vals = keys[srt], vals[srt]
    nodes = bfs[1:]
    prs = pred[nodes]
    rel = np.zeros(len(nodes), np.int8)
    inner = prs != n
    q = prs[inner] * (n + 1) + nodes[inner]
    rel[inner] = vals[np.searchsorted(keys, q)]
    parity = np.zeros(n + 1, np.int8)
    rel_of = np.zeros(n + 1, np.int8)
    rel_of[nodes] = rel
    par = parity.tolist()
    pl, rl = pred.tolist(), rel_of.tolist()
    for v in nodes.tolist():                                # parents come first in BFS order
        par[v] = par[pl[v]] ^ rl[v] if pl[v] != n else 0
    flipme = np.asarray(par[:n], bool)
    out = faces.copy()
    out[flipme] = out[flipme][:, ::-1]
    return out


def _boundary_loops(faces: np.ndarray) -> list[list[int]]:
    """Closed chains of one-user edges. Walked without regard to direction (orientation may still
    be inconsistent around them); the faces filling them are oriented afterwards."""
    he, fid, order, starts, counts = _edge_groups(faces)
    b = he[order[starts[counts == 1]]]
    nbr: dict[int, list[int]] = {}
    for u, v in b.tolist():
        nbr.setdefault(u, []).append(v)
        nbr.setdefault(v, []).append(u)

    def take(u, v):
        nbr[u].remove(v)
        nbr[v].remove(u)
    loops = []
    for u0 in list(nbr):
        while nbr.get(u0):
            u = nbr[u0][0]
            take(u0, u)
            loop = [u0]
            while u != u0 and nbr.get(u):
                if u in loop:                                 # pinched: split off the inner loop
                    i = loop.index(u)
                    if len(loop) - i >= 3:
                        loops.append(loop[i:])
                    loop = loop[:i]
                loop.append(u)
                w = nbr[u][0]
                take(u, w)
                u = w
            if u == u0 and len(loop) >= 3:
                loops.append(loop)
    return loops


def _drop_fins(v: np.ndarray, faces: np.ndarray) -> np.ndarray:
    """Edges with more than two triangles: keep the largest triangle and the largest one running
    the other way along the edge; drop the rest (slivers and fins). Holes left are closed later."""
    he, fid, order, starts, counts = _edge_groups(faces)
    if not (counts > 2).any():
        return faces
    tri = v[faces]
    area = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    drop = set()
    for s0, c in zip(starts[counts > 2].tolist(), counts[counts > 2].tolist()):
        hs = order[s0:s0 + c]
        fs = fid[hs]
        best = int(np.argmax(area[fs]))
        keep = {best}
        other = [i for i in range(c) if he[hs[i], 0] != he[hs[best], 0]]
        if other:
            keep.add(max(other, key=lambda i: area[fs[i]]))
        drop.update(int(fs[i]) for i in range(c) if i not in keep)
    mask = np.ones(len(faces), bool)
    mask[list(drop)] = False
    return faces[mask]


def _fill_loops(v: np.ndarray, faces: np.ndarray, loops) -> tuple[np.ndarray, np.ndarray]:
    """Close each hole: a triangle for three edges, else a fan from a new vertex at its centre."""
    new_v, new_f = [v], [faces]
    n = len(v)
    for loop in loops:
        L = np.asarray(loop)
        if len(L) == 3:
            new_f.append(L[::-1][None])
            continue
        new_v.append(v[L].mean(axis=0)[None])
        c = n
        n += 1
        new_f.append(np.c_[np.roll(L, -1), L, np.full(len(L), c)])
    return np.concatenate(new_v), np.concatenate(new_f).astype(np.int64)


def _inconsistent_faces(faces: np.ndarray) -> np.ndarray:
    """Faces on a two-user edge whose neighbour runs the same way along it."""
    he, fid, order, starts, counts = _edge_groups(faces)
    s = starts[counts == 2]
    same = he[order[s], 0] == he[order[s + 1], 0]
    out = np.zeros(len(faces), bool)
    out[fid[order[s[same]]]] = out[fid[order[s[same] + 1]]] = True
    return out


def _close(v: np.ndarray, f: np.ndarray):
    loops = _boundary_loops(f) if len(f) else []
    if loops:
        v, f = _fill_loops(v, f, loops)
    return v, (_orient(f) if len(f) else f)


def _dedupe(f: np.ndarray) -> np.ndarray:
    """One triangle per vertex triple, facing the way most copies face; copies facing opposite
    ways in equal numbers (a zero-thickness wall) all go."""
    if not len(f):
        return f
    key = np.sort(f, axis=1)
    _, inv = np.unique(key, axis=0, return_inverse=True)
    inv = inv.ravel()
    # orientation of each face relative to its sorted vertices: +1 or -1
    sign = np.where((f == key).all(1) | (f == key[:, [1, 2, 0]]).all(1) | (f == key[:, [2, 0, 1]]).all(1), 1, -1)
    net = np.bincount(inv, sign)
    first = np.full(len(net), -1)
    cand = np.flatnonzero(sign == np.sign(net)[inv])
    first[inv[cand[::-1]]] = cand[::-1]                        # the first face facing the net way
    keep = np.zeros(len(f), bool)
    keep[first[first >= 0]] = True
    return f[keep]


def _drop_debris(v: np.ndarray, f: np.ndarray) -> np.ndarray:
    lab, k = _face_labels(f)
    if k < 2:
        return f
    tri = v[f]
    area = np.bincount(lab, np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1) / 2,
                       minlength=k)
    vol = np.abs(_signed_volumes(v, f, lab, k))
    debris = (3 * vol < DEBRIS_THICKNESS_MM * np.maximum(area, 1e-12)) | (vol < DEBRIS_VOLUME_MM3)
    if debris.all():
        debris[np.argmax(area)] = False
    return f[~debris[lab]]


def _clean(f: np.ndarray) -> bool:
    """Every edge has two triangles running opposite ways, and no triangle is repeated."""
    if not len(f):
        return True
    counts = _edge_groups(f)[4]
    return bool((counts == 2).all() and not _inconsistent_faces(f).any()
                and len(np.unique(np.sort(f, axis=1), axis=0)) == len(f))


def repair(mesh: trimesh.Trimesh, progress=None) -> trimesh.Trimesh:
    """Return a repaired copy: weld vertices; drop degenerate triangles, duplicates (a pair facing
    opposite ways is a zero-thickness wall: both go), debris (shells with no thickness or smaller
    than a 0.1 mm cube) and fins (extra triangles on an edge); make triangle orientation
    consistent; close holes; cut out and re-close patches too crumpled to orient; turn each shell
    outwards. Closing a hole can expose another problem, so this runs up to three passes.
    progress(text) is told each step."""
    say = progress or (lambda _t: None)
    say("welding vertices")
    m = mesh.copy()
    m.merge_vertices()
    m.update_faces(m.nondegenerate_faces(height=1e-9))
    f = np.asarray(m.faces, np.int64)
    v = np.asarray(m.vertices, np.float64)
    for attempt in range(3):
        again = f" (pass {attempt + 1})" if attempt else ""
        say("removing duplicate triangles" + again)
        f = _dedupe(f)
        say("removing debris" + again)
        f = _drop_debris(v, f)
        say("fixing triangle orientation" + again)
        for _ in range(3):
            g = _drop_fins(v, f) if len(f) else f
            if len(g) == len(f):
                break
            f = g
        if len(f):
            f = _orient(f)
        say("closing holes" + again)
        v, f = _close(v, f)
        # a crumpled patch can leave the surface impossible to orient (a twist): cut it out,
        # wider each time, and close the hole again
        for ring in range(1, 5):
            bad = _inconsistent_faces(f)
            if not bad.any():
                break
            cut = bad
            for _ in range(ring):
                cut = np.isin(f, np.unique(f[cut])).any(axis=1)
            if cut.all():
                break
            v, f = _close(v, f[~cut])
        if _clean(f):
            break
    say("turning shells outwards")
    lab, k = _face_labels(f)
    if k:
        inward = _signed_volumes(v, f, lab, k) < 0
        f[inward[lab]] = f[inward[lab]][:, ::-1]
    out = trimesh.Trimesh(vertices=v, faces=f, process=False)
    out.remove_unreferenced_vertices()
    return out
