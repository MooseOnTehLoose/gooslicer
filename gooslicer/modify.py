"""Surface selection by curvature and cutting a selection off as its own part (11)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import trimesh

SEED_RADIUS = 2.0          # mm: faces this close to the click give the seed direction
SMOOTH_STEPS = 8           # neighbour-averaging passes before creases are measured (~0.5–1 mm)
MIN_CONTOUR = 3.0          # mm: shorter crease lines are surface texture, not contours


class CutError(ValueError):
    pass


@dataclass
class Surface:
    """What growing regions on one mesh needs: face adjacency (built once per mesh) and,
    for a placement, world face normals, centres and areas."""
    adjacency: np.ndarray              # (k, 2) face pairs sharing an edge
    normals: np.ndarray                # (n, 3) world unit normals
    centers: np.ndarray                # (n, 3) world face centres
    areas: np.ndarray                  # (n,) world face areas
    vertices: np.ndarray               # (m, 3) world vertex positions
    edges: np.ndarray | None = None    # (k, 2) the vertex pair each adjacency row shares


def adjacency(mesh: trimesh.Trimesh) -> tuple[np.ndarray, np.ndarray]:
    """(face pairs sharing an edge, that edge's vertex pair)."""
    return np.asarray(mesh.face_adjacency, np.int64), np.asarray(mesh.face_adjacency_edges, np.int64)


def surface(mesh: trimesh.Trimesh, matrix: np.ndarray, adj=None) -> Surface:
    """`adj`: what `adjacency(mesh)` returned (computed when None)."""
    """World normals, centres and areas of `mesh` placed by the 4x4 `matrix`."""
    v = np.asarray(mesh.vertices, float) @ matrix[:3, :3].T + matrix[:3, 3]
    tri = v[np.asarray(mesh.faces)]
    cross = np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0])
    if np.linalg.det(matrix[:3, :3]) < 0:
        cross = -cross                 # a mirror turns faces inside out
    area2 = np.linalg.norm(cross, axis=1)
    normals = cross / np.maximum(area2, 1e-30)[:, None]
    pairs, edges = adjacency(mesh) if adj is None else adj
    return Surface(pairs, normals, tri.mean(axis=1), area2 / 2, v, edges)


def seed_normal(s: Surface, face: int, radius: float = SEED_RADIUS) -> np.ndarray:
    """Area-weighted mean normal of the faces within `radius` of `face` (by centre)."""
    near = np.linalg.norm(s.centers - s.centers[face], axis=1) <= radius
    n = (s.normals[near] * s.areas[near, None]).sum(axis=0)
    ln = np.linalg.norm(n)
    return n / ln if ln > 1e-12 else s.normals[face]


def grow_region(s: Surface, face: int, spread_deg: float, normal: np.ndarray | None = None,
                barrier: np.ndarray | None = None) -> np.ndarray:
    """Bool mask of the faces connected to `face` through faces whose normal is within
    `spread_deg` of the seed normal, never across an edge marked in `barrier` (per adjacency
    row: the contours)."""
    n = len(s.normals)
    nrm = seed_normal(s, face) if normal is None else normal
    ok = s.normals @ nrm >= np.cos(np.radians(spread_deg))
    ok[face] = True
    a = s.adjacency
    use = ok[a[:, 0]] & ok[a[:, 1]]
    if barrier is not None:
        use &= ~barrier
    e = a[use]
    g = sp.coo_matrix((np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(n, n))
    _, lab = sp.csgraph.connected_components(g, directed=False)
    return ok & (lab == lab[face])


def _face_graph(s: Surface, weights=None):
    n = len(s.normals)
    a = s.adjacency
    w = np.ones(len(a)) if weights is None else weights
    return sp.coo_matrix((np.r_[w, w], (np.r_[a[:, 0], a[:, 1]], np.r_[a[:, 1], a[:, 0]])), shape=(n, n)).tocsr()


def smoothed_normals(s: Surface, steps: int = SMOOTH_STEPS) -> np.ndarray:
    """Face normals averaged with their neighbours `steps` times (area-weighted start), so
    creases are measured at a fraction of a millimetre, not per triangle."""
    A = _face_graph(s)
    N = s.normals * s.areas[:, None]
    for _ in range(steps):
        N = N + A @ N
        N /= np.maximum(np.linalg.norm(N, axis=1), 1e-30)[:, None]
    return N


def contours(s: Surface, angle_deg: float, min_len: float = MIN_CONTOUR, steps: int = SMOOTH_STEPS) -> np.ndarray:
    """Bool per adjacency row: the edge lies on a contour, a valley-like crease where the
    smoothed surface turns more than `angle_deg` between the two faces (a groove, a strap's
    edge, a hairline). Crease lines shorter than `min_len` mm in all are left out: they're
    surface texture (hair, skin), not outlines."""
    if s.edges is None:
        raise ValueError("surface built without edges")
    N = smoothed_normals(s, steps)
    a = s.adjacency
    cos = np.einsum("ij,ij->i", N[a[:, 0]], N[a[:, 1]])
    # valley or ridge: by the faces' own normals (smoothed ones lean over a corner)
    concave = np.einsum("ij,ij->i", s.centers[a[:, 1]] - s.centers[a[:, 0]], s.normals[a[:, 0]]) > 0
    crease = (cos < np.cos(np.radians(angle_deg))) & concave
    if not crease.any():
        return crease
    e = s.edges[crease]
    nv = len(s.vertices)
    g = sp.coo_matrix((np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(nv, nv))
    _, lab = sp.csgraph.connected_components(g, directed=False)
    length = np.linalg.norm(s.vertices[e[:, 0]] - s.vertices[e[:, 1]], axis=1)
    total = np.bincount(lab[e[:, 0]], weights=length, minlength=lab.max() + 1)
    out = np.zeros(len(a), bool)
    out[np.nonzero(crease)[0][total[lab[e[:, 0]]] >= min_len]] = True
    return out


def grow_by(s: Surface, mask: np.ndarray, mm: float, faces: np.ndarray | None = None) -> np.ndarray:
    """The selection grown by `mm` along the surface, or shrunk when negative: distances run
    along the mesh's edges from the selection's (or, shrinking, the rest's) vertices; a face
    joins (or, shrinking, goes) when all its corners are within reach, so a shrink is a grow of
    the rest. It only spreads over the
    shells it's on. `faces`: the mesh's faces (vertex indices)."""
    if not mm or not mask.any() or faces is None:
        return mask.copy()
    e = s.edges
    nv = len(s.vertices)
    w = np.linalg.norm(s.vertices[e[:, 0]] - s.vertices[e[:, 1]], axis=1) + 1e-12
    G = sp.coo_matrix((np.r_[w, w], (np.r_[e[:, 0], e[:, 1]], np.r_[e[:, 1], e[:, 0]])), shape=(nv, nv)).tocsr()
    faces = np.asarray(faces)
    src = np.unique(faces[mask if mm > 0 else ~mask])
    if not len(src):
        return mask.copy()
    d = sp.csgraph.dijkstra(G, directed=False, indices=src, limit=abs(mm) + 1e-9, min_only=True)
    if mm > 0:
        return mask | (d[faces].max(axis=1) <= mm)
    return mask & ~(d[faces].max(axis=1) <= -mm)


def _manifold(mesh: trimesh.Trimesh):
    import manifold3d as m3
    mf = m3.Manifold(m3.Mesh(np.asarray(mesh.vertices, np.float32), np.asarray(mesh.faces, np.uint32)))
    if mf.status() != m3.Error.NoError or mf.is_empty():
        raise CutError("The mesh isn't a closed solid: repair it first")
    return mf


def _trimesh(mf) -> trimesh.Trimesh:
    m = mf.to_mesh()
    return trimesh.Trimesh(np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts), process=False)


def fill_holes(s: Surface, mask: np.ndarray) -> np.ndarray:
    """`mask` plus the unselected patches it surrounds (bumps inside the selected area): every
    unselected connected patch bordering the selection except the largest (the rest of the
    model). Shells not touching the selection (a hollow's inside) are left alone."""
    n = len(mask)
    a = s.adjacency
    un = ~mask
    e = a[un[a[:, 0]] & un[a[:, 1]]]
    g = sp.coo_matrix((np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(n, n))
    _, lab = sp.csgraph.connected_components(g, directed=False)
    border = a[mask[a[:, 0]] ^ mask[a[:, 1]]]
    touching = np.unique(np.where(mask[border[:, 0]], border[:, 1], border[:, 0]))
    comps = np.unique(lab[touching])
    if len(comps) <= 1:
        return mask.copy()
    area = np.bincount(lab, weights=s.areas, minlength=lab.max() + 1)
    fill = comps[comps != comps[np.argmax(area[comps])]]
    return mask | np.isin(lab, fill)


def cut_direction(s: Surface, mask: np.ndarray) -> np.ndarray:
    """Area-weighted mean normal of the selected faces: the piece comes out this way."""
    if not mask.any():
        raise CutError("Nothing is selected")
    n = (s.normals[mask] * s.areas[mask, None]).sum(axis=0)
    ln = np.linalg.norm(n)
    if ln < 1e-12:
        raise CutError("The selection faces every way: select less")
    return n / ln


def _frame(n: np.ndarray) -> np.ndarray:
    """Rows: two axes across `n`, then `n` (world -> frame)."""
    a = np.cross(n, [1.0, 0.0, 0.0] if abs(n[0]) < 0.9 else [0.0, 1.0, 0.0])
    a /= np.linalg.norm(a)
    return np.stack([a, np.cross(n, a), n])


def outline(s: Surface, faces: np.ndarray, mask: np.ndarray, n: np.ndarray, smooth: float = 0.5,
            grow: float = 0.0):
    """The selection seen along `n`, as a manifold CrossSection in the frame `_frame(n)`: the
    union of its projected triangles, rounded by `smooth` mm (closed then opened), holes
    filled, then grown by `grow` mm (negative shrinks)."""
    import manifold3d as m3
    R = _frame(n)
    tri = (s.vertices[np.asarray(faces)[mask]] @ R.T)[:, :, :2]
    cr = ((tri[:, 1, 0] - tri[:, 0, 0]) * (tri[:, 2, 1] - tri[:, 0, 1])
          - (tri[:, 1, 1] - tri[:, 0, 1]) * (tri[:, 2, 0] - tri[:, 0, 0]))
    tri = np.where((cr < 0)[:, None, None], tri[:, ::-1], tri)[np.abs(cr) > 1e-12]
    if not len(tri):
        raise CutError("The selection is edge-on to its cut direction")
    cs = m3.CrossSection(list(tri), m3.FillRule.Positive)
    if smooth > 0:
        cs = cs.offset(smooth, m3.JoinType.Round).offset(-smooth, m3.JoinType.Round)
    cs = cs.simplify(0.02)
    outer = [c for c in (np.asarray(c) for c in cs.to_polygons()) if _signed_area(c) > 0]
    cs = m3.CrossSection(outer, m3.FillRule.Positive)
    if grow:
        cs = cs.offset(grow, m3.JoinType.Round)
    if cs.is_empty():
        raise CutError("The outline vanished: select more or lower the fit gap")
    return cs


def _signed_area(c: np.ndarray) -> float:
    x, y = c[:, 0], c[:, 1]
    return 0.5 * float(np.sum(x * np.roll(y, -1) - np.roll(x, -1) * y))


def cutter(s: Surface, faces: np.ndarray, mask: np.ndarray, depth: float = 3.0, smooth: float = 0.5,
           grow: float = 0.0):
    """The cutting solid in world space (manifold): the selection's outline (grown by `grow` mm)
    pushed along its cut direction, from `depth` mm below the selection's lowest point to past
    the top of the part. Its walls follow the outline, so the piece lifts out along the
    direction and fits back only one way. Returns (cutter, direction)."""
    n = cut_direction(s, mask)
    cs = outline(s, faces, mask, n, smooth, grow)
    sel_v = s.vertices[np.unique(np.asarray(faces)[mask])]
    bottom = float((sel_v @ n).min()) - float(depth)
    top = float((s.vertices @ n).max()) + 2.0
    R = _frame(n)
    T = np.zeros((3, 4))
    T[:, :3] = R.T
    T[:, 3] = n * bottom
    return cs.extrude(top - bottom).transform(T), n


def to_local(solid, matrix: np.ndarray):
    """A world-space manifold in the coordinates of a part placed by `matrix`."""
    inv = np.linalg.inv(matrix)
    return solid.transform(inv[:3, :4])


def to_trimesh(mf) -> trimesh.Trimesh:
    return _trimesh(mf)


DEBRIS = 0.5               # mm³: smaller loose bits left by a cut are dropped
SLIVER = 1e-4              # mm: edges this short left by a cut are collapsed


def cut(mesh: trimesh.Trimesh, tool, keep_points, piece_tool=None) -> tuple[trimesh.Trimesh, trimesh.Trimesh, int]:
    """Cut `mesh` (closed) with the solid `tool` (a manifold), both in the mesh's coordinates.
    Returns (rest, piece, dropped): the rest is the mesh minus `tool`; the piece is every
    connected part inside `piece_tool` (default `tool`; a slightly smaller one leaves a fit gap)
    that has one of `keep_points` (vertices of the selected faces) as a vertex. Other parts
    inside go back to the rest. Loose bits under 0.5 mm³ (strands the tool's wall cut through)
    are dropped and counted."""
    import manifold3d as m3
    from scipy.spatial import cKDTree
    mf = _manifold(mesh)
    inside, outside = mf ^ (tool if piece_tool is None else piece_tool), mf - tool
    if inside.is_empty() or outside.is_empty():
        raise CutError("Nothing to cut: the cut misses the part or takes all of it")
    keep = np.asarray(keep_points, float).reshape(-1, 3)
    pieces, others = [], []
    for c in inside.decompose():
        v = np.asarray(c.to_mesh().vert_properties)[:, :3]
        hit = np.isfinite(cKDTree(v).query(keep, distance_upper_bound=1e-3)[0]).any() if len(keep) else False
        (pieces if hit else others).append(c)
    if not pieces:
        raise CutError("The cut keeps none of the selection")
    rest_parts = outside.decompose() + [c - tool if piece_tool is not None else c for c in others]
    rest_parts = [c for c in rest_parts if not c.is_empty()]
    dropped = 0
    big = max(rest_parts, key=lambda c: c.volume())
    kept = []
    for c in rest_parts:
        if c is not big and c.volume() < DEBRIS:
            dropped += 1
        else:
            kept.append(c)
    big_piece = max(pieces, key=lambda c: c.volume())
    piece_parts = []
    for c in pieces:
        if c is not big_piece and c.volume() < DEBRIS:
            dropped += 1
        else:
            piece_parts.append(c)
    rest = m3.Manifold.batch_boolean(kept, m3.OpType.Add) if len(kept) > 1 else kept[0]
    piece = m3.Manifold.batch_boolean(piece_parts, m3.OpType.Add) if len(piece_parts) > 1 else piece_parts[0]
    # the cut walls can graze mesh vertices: collapse the zero-area slivers that leaves
    return _trimesh(rest.simplify(SLIVER)), _trimesh(piece.simplify(SLIVER)), dropped


def lay_on_plane_rotation(normal) -> np.ndarray:
    """Rotation taking `normal` to +Z (the piece's cut side then faces the plate)."""
    n = np.asarray(normal, float) / np.linalg.norm(normal)
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(n, z)
    c = float(np.dot(n, z))
    if np.linalg.norm(v) < 1e-9:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx * (1 / (1 + c))
