"""Surface selection by curvature and cutting a selection off as its own part (11)."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import scipy.sparse as sp
import trimesh

SEED_RADIUS = 2.0          # mm: faces this close to the click give the seed direction
SMOOTH_MM = 0.5            # mm: creases are measured on the surface smoothed over about this
MAX_SMOOTH_STEPS = 60      # neighbour-averaging passes at most (fine meshes)
MIN_CONTOUR = 4.0          # mm: a contour line spans at least this end to end; less is texture


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


def smoothed_normals(s: Surface, radius: float = SMOOTH_MM) -> np.ndarray:
    """Face normals averaged with their neighbours until they're smoothed over about `radius` mm
    (repeated averaging spreads like √steps × the face spacing), area-weighted, so creases are
    measured at that scale and not per triangle."""
    A = _face_graph(s)
    a = s.adjacency
    h = float(np.median(np.linalg.norm(s.centers[a[:, 0]] - s.centers[a[:, 1]], axis=1))) if len(a) else 1.0
    steps = int(np.clip(round((radius / max(h, 1e-6)) ** 2), 1, MAX_SMOOTH_STEPS))
    N = s.normals * s.areas[:, None]
    for _ in range(steps):
        N = N + A @ N
        N /= np.maximum(np.linalg.norm(N, axis=1), 1e-30)[:, None]
    return N


def _face_edges(s: Surface) -> np.ndarray:
    """(n, 3) the adjacency rows touching each face (-1 where a face has fewer)."""
    a = s.adjacency
    n = len(s.normals)
    rows = np.r_[np.arange(len(a)), np.arange(len(a))]
    faces = np.r_[a[:, 0], a[:, 1]]
    order = np.argsort(faces, kind="stable")
    faces, rows = faces[order], rows[order]
    start = np.searchsorted(faces, np.arange(n))
    slot = np.arange(len(faces)) - start[faces]
    out = np.full((n, 3), -1, np.int64)
    ok = slot < 3
    out[faces[ok], slot[ok]] = rows[ok]
    return out


def fold_angles(s: Surface, radius: float = SMOOTH_MM) -> np.ndarray:
    """Per adjacency row: how sharply the surface folds there, as the angle (degrees) it would
    turn across a 2·radius wide band — the turn between smoothed normals per mm of face spacing,
    times 2·radius. Signed: negative for a ridge, positive for a valley (by the faces' own
    normals; smoothed ones lean over a corner)."""
    N = smoothed_normals(s, radius)
    a = s.adjacency
    cos = np.clip(np.einsum("ij,ij->i", N[a[:, 0]], N[a[:, 1]]), -1.0, 1.0)
    d = np.linalg.norm(s.centers[a[:, 0]] - s.centers[a[:, 1]], axis=1)
    fold = np.degrees(np.arccos(cos)) / np.maximum(d, 1e-9) * 2 * radius
    valley = np.einsum("ij,ij->i", s.centers[a[:, 1]] - s.centers[a[:, 0]], s.normals[a[:, 0]]) > 0
    return np.where(valley, fold, -fold)


def contours(s: Surface, fold_deg: float, min_span: float = MIN_CONTOUR, radius: float = SMOOTH_MM,
             fold: np.ndarray | None = None) -> np.ndarray:
    """Bool per adjacency row: the edge lies on a contour, a valley (a groove, a strap's edge, a
    hairline) folding at least `fold_deg` within about 2·radius mm. Cleaned like an edge
    detector's output:
    - thinned: an edge is kept only where its fold is the largest of the edges around its two
      faces, so a crease is one line, not a band;
    - joined: edges folding half as much carry a line on from a strong edge (hysteresis), so
      lines don't break up;
    - only lines: a connected line must span at least `min_span` mm end to end; texture (hair,
      skin) makes tangles that are long but small, and those go.
    `fold`: `fold_angles(s, radius)` if already worked out (it's the slow part)."""
    if s.edges is None:
        raise ValueError("surface built without edges")
    if fold is None:
        fold = fold_angles(s, radius)
    fe = _face_edges(s)
    a = s.adjacency
    around = np.concatenate([fe[a[:, 0]], fe[a[:, 1]]], axis=1)
    nb = np.where(around >= 0, fold[np.maximum(around, 0)], -np.inf)
    peak = fold >= nb.max(axis=1) - 1e-9
    weak = peak & (fold >= fold_deg / 2)
    if not weak.any():
        return np.zeros(len(a), bool)
    strong = weak & (fold >= fold_deg)
    e = s.edges[weak]
    nv = len(s.vertices)
    g = sp.coo_matrix((np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(nv, nv))
    _, lab = sp.csgraph.connected_components(g, directed=False)
    line = lab[e[:, 0]]
    k = line.max() + 1
    has_strong = np.zeros(k, bool)
    has_strong[line[strong[weak]]] = True
    pts = s.vertices[e].reshape(-1, 3)
    lab2 = np.repeat(line, 2)
    lo = np.full((k, 3), np.inf)
    hi = np.full((k, 3), -np.inf)
    np.minimum.at(lo, lab2, pts)
    np.maximum.at(hi, lab2, pts)
    span = np.linalg.norm(hi - lo, axis=1)
    keep_line = has_strong & (span >= min_span)
    out = np.zeros(len(a), bool)
    out[np.nonzero(weak)[0][keep_line[line]]] = True
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


def _tidy(t: trimesh.Trimesh) -> trimesh.Trimesh:
    """A boolean result can hold vertices at exactly the same place under different indices
    (zero-length edges, zero-area triangles): weld them and drop the triangles that collapse,
    if the mesh stays closed."""
    w = trimesh.Trimesh(t.vertices.copy(), t.faces.copy(), process=False)
    w.merge_vertices(merge_tex=True, merge_norm=True)
    f = np.asarray(w.faces)
    ok = (f[:, 0] != f[:, 1]) & (f[:, 1] != f[:, 2]) & (f[:, 2] != f[:, 0])
    if ok.all():
        return t
    w.update_faces(ok)
    w.remove_unreferenced_vertices()
    return w if w.is_watertight else t


def fill_holes(s: Surface, mask: np.ndarray) -> np.ndarray:
    """`mask` plus the unselected patches it surrounds (bumps inside the selected area): every
    unselected connected patch bordering the selection except the largest (the rest of the
    model), and smaller than the selection (a ring round a cylinder doesn't make either end a
    hole). Shells not touching the selection (a hollow's inside) are left alone."""
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
    fill = fill[area[fill] < s.areas[mask].sum()]
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


HEIGHT_CELL = 0.25         # mm: the grid the selection's height is sampled on, for the cutter


def _height_map(tri: np.ndarray, bounds, cell: float):
    """Lowest height (frame z) of the selected surface over a grid covering `bounds` (frame x/y):
    (grid, x0, y0). Every triangle marks its corners, edge midpoints and centre; empty cells take
    their nearest neighbour's height. Each cell keeps its lowest point, so a steep slope's
    bottom is at most a cell's rise too low."""
    from scipy import ndimage
    x0, y0, x1, y1 = bounds
    x0, y0 = x0 - 2 * cell, y0 - 2 * cell
    nx = int(np.ceil((x1 - x0) / cell)) + 3
    ny = int(np.ceil((y1 - y0) / cell)) + 3
    pts = np.concatenate([tri.reshape(-1, 3), tri.mean(axis=1),
                          (tri[:, [0, 1, 2]] + tri[:, [1, 2, 0]]).reshape(-1, 3) / 2])
    ix = np.clip(((pts[:, 0] - x0) / cell).astype(int), 0, nx - 1)
    iy = np.clip(((pts[:, 1] - y0) / cell).astype(int), 0, ny - 1)
    h = np.full((nx, ny), np.inf)
    np.minimum.at(h, (ix, iy), pts[:, 2])
    empty = ~np.isfinite(h)
    if empty.all():
        raise CutError("The selection is edge-on to its cut direction")
    if empty.any():
        idx = ndimage.distance_transform_edt(empty, return_distances=False, return_indices=True)
        h = h[tuple(idx)]
    return h, x0, y0


def cutter(s: Surface, faces: np.ndarray, mask: np.ndarray, depth: float = 3.0, smooth: float = 0.5,
           grow: float = 0.0):
    """The cutting solid in world space (manifold), returned with the cut direction n.
    Seen along n it's the selection's outline (grown by `grow` mm); its bottom follows the
    selected surface `depth` mm beneath it, and it reaches up past the part. So it takes only the
    selected surface and what's under it to that depth (on a hollowed part: the wall, through to
    the cavity), never material elsewhere. Its walls run along n and follow the outline, so the
    piece lifts out along n and fits back only one way."""
    from scipy import ndimage
    n = cut_direction(s, mask)
    cs = outline(s, faces, mask, n, smooth, grow)
    R = _frame(n)
    tri = s.vertices[np.asarray(faces)[mask]] @ R.T
    h, gx, gy = _height_map(tri, cs.bounds(), HEIGHT_CELL)
    bottom = h - float(depth)
    b0 = float(bottom.min())
    top = float((s.vertices @ n).max()) + 2.0
    H = top - b0
    prism = cs.extrude(H).refine_to_length(max(4 * HEIGHT_CELL, 0.5))

    def warp(p):
        p = np.array(p, float, copy=True)
        b = ndimage.map_coordinates(bottom, [(p[:, 0] - gx) / HEIGHT_CELL, (p[:, 1] - gy) / HEIGHT_CELL],
                                    order=1, mode="nearest") - b0
        b = np.minimum(b, H - 1.0)
        p[:, 2] = b + p[:, 2] / H * (H - b)          # bottom onto the surface, top stays
        return p
    tool = prism.warp_batch(warp)
    T = np.zeros((3, 4))
    T[:, :3] = R.T
    T[:, 3] = n * b0
    return tool.transform(T), n


def joint(mesh_or_manifold, tool) -> trimesh.Trimesh | None:
    """The faces the cut makes inside the part (where piece and rest will meet), in the tool's
    coordinates: the tool's own faces that end up on the piece."""
    mf = mesh_or_manifold if not isinstance(mesh_or_manifold, trimesh.Trimesh) else _manifold(mesh_or_manifold)
    t = tool.as_original()
    tid = t.original_id()
    m = (mf ^ t).to_mesh()
    runs = np.asarray(m.run_original_id)
    idx = np.asarray(m.run_index) // 3
    keep = np.zeros(len(np.asarray(m.tri_verts)), bool)
    for i, rid in enumerate(runs):
        if rid == tid:
            keep[idx[i]:idx[i + 1]] = True
    if not keep.any():
        return None
    return trimesh.Trimesh(np.asarray(m.vert_properties)[:, :3], np.asarray(m.tri_verts)[keep], process=False)


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
    return _tidy(_trimesh(rest.simplify(SLIVER))), _tidy(_trimesh(piece.simplify(SLIVER))), dropped


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
