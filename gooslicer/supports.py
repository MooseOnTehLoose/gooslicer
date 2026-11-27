"""Automatic supports: contact detection, EVO-style structure (tips, pillars, diamond bracing),
raft, and an unsupported-island check (clean-room; spec 08-supports-spec.md).

All geometry here is in world coordinates (plate mm, Z up, plate at z = 0) with the model
already lifted to its print height. Parameters come from a SatelLite template (06 §1) or the
built-in defaults, which equal the EVO_30-60mm values documented in 06.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, fields, replace

import manifold3d as m3
import numpy as np
import trimesh
from scipy.sparse.csgraph import minimum_spanning_tree
from scipy.spatial import Delaunay, cKDTree

from .raycast import caster

# Support kinds
OVERHANG, LOWEST, ISLAND, MANUAL = "overhang", "lowest", "island", "manual"
# Tip shapes (08 §11) [GOOSLICER, after Lychee's list]; "cone" is SatelLite's only form
TIP_SHAPES = ("cone", "cylinder", "cube", "cross")


@dataclass
class SupportParams:
    """Names follow the .satsup keys (06 §1). Defaults = EVO_30-60mm [OBSERVED decode]."""
    # area (detection)
    critical_angle: float = 50.0        # area.criticalAngle, ° from horizontal
    anchor_dis: float = 2.0             # anchorDis: contact spacing inside overhang areas
    anchor_dis_border: float = 2.0      # anchorDisBorder
    border_offset: float = 0.5          # borderOffset
    lowest_interval: float = 2.0        # lowestPtsInterval
    reinforce_lowest: bool = True       # bReinforceLowestPts
    # top connection (tip)
    tip_top_width: float = 0.32         # topConnect.topWidth (diameter at the model)
    tip_bottom_width: float = 0.6       # topConnect.bottomWidth
    tip_length: float = 1.0             # topConnect.tipLength
    tip_ball: bool = True               # topConnect.topBall
    tip_ball_diameter: float = 0.35     # topConnect.topBallDiameter
    tip_in_part: float = 0.1            # topConnect.distanceInPart
    tip_shape: str = "cone"             # GooSlicer (08 §11): cone, cylinder, cube or cross
    # bottom connection (supports that land on the model)
    bot_top_width: float = 0.3          # botConnect.topWidth
    bot_in_part: float = 0.1            # botConnect.distanceInPart
    bot_tip_shape: str = "cone"         # GooSlicer (08 §11): base tip shape
    bot_ball: bool = False              # GooSlicer: break point ball on base tips
    bot_ball_diameter: float = 0.35
    # pillar
    pillar_top_width: float = 0.7       # structure.cluster.topWidth
    pillar_bottom_width: float = 0.7    # structure.cluster.bottomWidth
    poly_edges: int = 16                # structure.polyEdgeNum
    # bracing
    diamond_angle: float = 60.0         # diamond.diamondAngle, ° from horizontal
    diamond_width: float = 7.0          # diamond.diamondWidth (max pillar spacing braced)
    edge_width: float = 0.8             # diamond.edgeWidth
    bracing: bool = True
    # plate connection (pillar foot)
    plat_connect: bool = False          # platConnect.hasPlatConnect
    plat_connect_radius: float = 4.0    # platConnect.radius
    plat_connect_height: float = 2.0    # platConnect.height
    # filters
    inner_support: bool = True          # EnableInnerSupport (supports landing on the model)
    min_on_model_length: float = 1.0    # GooSlicer: shortest support allowed to land on the model
    max_tip_angle: float = 30.0         # steepest tip, ° from vertical (SatelLite's, 10 §5; name GooSlicer's)
    # raft (script 9)
    raft: bool = True                   # hasBasePlat
    raft_height: float = 0.5            # platHeight
    raft_offset: float = 1.0            # platOffset
    raft_slot_angle: float = 60.0       # slotAngle (edge chamfer, ° from horizontal)
    raft_holes: bool = True             # hasHole
    hole_radius: float = 1.0            # holeRadius
    hole_interval: float = 3.0          # holeInterval
    hole_edges: int = 12                # holeEdgeNum
    # placement
    lift: float = 5.0                   # LiftHeight: model bottom above the plate
    # tree structure (script type 0); names OBSERVED, roles GOOSLICER (08 §8)
    style: str = "evo"                  # "evo" (script 7) or "tree" (script 0)
    branch_width: float = 1.2           # structure.tree.brunch.topWidth / botWidth
    max_branch_angle: float = 45.0      # structure.tree.trunk.maxBranchAngle, ° from horizontal (height type 2)
    max_branch_height: float = 5.0      # structure.tree.trunk.maxHeight: highest joint above the raft (type 1)
    tree_height_type: int = 0           # structure.tree.trunk.heightType: how joints are placed (08 §8)
    bot_tip_length: float = 3.0         # botConnect.tipLength (tree supports landing on the model)
    cross: bool = True                  # structure.cross.hasCrossConnect
    cross_start: float = 3.0            # cross.startHeight, above the raft
    cross_interval: float = 5.0         # cross.corssInterval (sic)
    cross_width: float = 0.3            # cross.barWidth
    cross_angle: float = 45.0           # cross.barAngle, ° from horizontal
    cross_max_dist: float = 10.0        # GooSlicer: farthest trunks that get braced

    @classmethod
    def tree_defaults(cls) -> "SupportParams":
        """Built-in tree values = SatelLite's Tree_Medium template [OBSERVED decode, 08 §8]."""
        return replace(
            cls(), style="tree", critical_angle=50.0, anchor_dis=2.3, anchor_dis_border=2.0,
            lowest_interval=1.5, tip_top_width=0.45, tip_bottom_width=1.2, tip_length=2.0,
            tip_ball=False, tip_ball_diameter=0.5, tip_in_part=0.2, bot_top_width=0.5,
            pillar_top_width=1.2, pillar_bottom_width=1.2, poly_edges=12, bracing=False,
            raft_offset=3.0)

    @classmethod
    def from_template(cls, t) -> "SupportParams":
        """From a gooslicer.support_defaults.SupportTemplate (EVO or Tree family)."""
        s, r = t.structure, t.raft
        def g(d, k, dflt):
            v = type(dflt)(d.get(k, dflt))
            return round(v, 4) if isinstance(v, float) else v    # stored as float32 (0.32 → 0.31999…)
        if getattr(t, "family", "evo") == "tree":
            return cls._from_tree_template(t, g)
        d = cls()
        c = "structure.connect."
        st = "structure.structure."
        return replace(
            d,
            critical_angle=g(s, "area.criticalAngle", d.critical_angle),
            anchor_dis=g(s, "anchorDis", d.anchor_dis),
            anchor_dis_border=g(s, "anchorDisBorder", d.anchor_dis_border),
            border_offset=g(s, "borderOffset", d.border_offset),
            lowest_interval=g(s, "lowestPtsInterval", d.lowest_interval),
            reinforce_lowest=g(s, "bReinforceLowestPts", d.reinforce_lowest),
            tip_top_width=g(s, c + "topConnect.topWidth", d.tip_top_width),
            tip_bottom_width=g(s, c + "topConnect.bottomWidth", d.tip_bottom_width),
            tip_length=g(s, c + "topConnect.tipLength", d.tip_length),
            tip_ball=g(s, c + "topConnect.topBall", d.tip_ball),
            tip_ball_diameter=g(s, c + "topConnect.topBallDiameter", d.tip_ball_diameter),
            tip_in_part=g(s, c + "topConnect.distanceInPart", d.tip_in_part),
            bot_top_width=g(s, c + "botConnect.topWidth", d.bot_top_width),
            bot_in_part=g(s, c + "botConnect.distanceInPart", d.bot_in_part),
            pillar_top_width=g(s, st + "cluster.topWidth", d.pillar_top_width),
            pillar_bottom_width=g(s, st + "cluster.bottomWidth", d.pillar_bottom_width),
            poly_edges=g(s, st + "polyEdgeNum", d.poly_edges),
            diamond_angle=g(s, st + "diamond.diamondAngle", d.diamond_angle),
            diamond_width=g(s, st + "diamond.diamondWidth", d.diamond_width),
            edge_width=g(s, st + "diamond.edgeWidth", d.edge_width),
            plat_connect=g(s, c + "platConnect.hasPlatConnect", d.plat_connect),
            plat_connect_radius=g(s, c + "platConnect.radius", d.plat_connect_radius),
            plat_connect_height=g(s, c + "platConnect.height", d.plat_connect_height),
            inner_support=bool(t.inner_support),
            raft=g(r, "hasBasePlat", d.raft),
            raft_height=g(r, "platHeight", d.raft_height),
            raft_offset=g(r, "platOffset", d.raft_offset),
            raft_slot_angle=g(r, "slotAngle", d.raft_slot_angle),
            raft_holes=g(r, "hasHole", d.raft_holes),
            hole_radius=g(r, "holeRadius", d.hole_radius),
            hole_interval=g(r, "holeInterval", d.hole_interval),
            hole_edges=g(r, "holeEdgeNum", d.hole_edges),
            lift=float(t.lift_height),
        )

    @classmethod
    def _from_tree_template(cls, t, g) -> "SupportParams":
        s, r = t.structure, t.raft
        d = cls.tree_defaults()
        c, tr = "structure.connect.", "structure.tree."
        return replace(
            d,
            critical_angle=g(s, "area.criticalAngle", d.critical_angle),
            anchor_dis=g(s, "anchorDis", d.anchor_dis),
            anchor_dis_border=g(s, "anchorDisBorder", d.anchor_dis_border),
            border_offset=g(s, "borderOffset", d.border_offset),
            lowest_interval=g(s, "lowestPtsInterval", d.lowest_interval),
            reinforce_lowest=g(s, "bReinforceLowestPts", d.reinforce_lowest),
            tip_top_width=g(s, c + "topConnect.topWidth", d.tip_top_width),
            tip_bottom_width=g(s, tr + "brunch.topWidth", d.tip_bottom_width),   # tip widens to the branch
            tip_length=g(s, c + "topConnect.tipLength", d.tip_length),
            tip_ball=g(s, c + "topConnect.topBall", d.tip_ball),
            tip_ball_diameter=g(s, c + "topConnect.topBallDiameter", d.tip_ball_diameter),
            tip_in_part=g(s, c + "topConnect.distanceInPart", d.tip_in_part),
            bot_top_width=g(s, c + "botConnect.topWidth", d.bot_top_width),
            bot_in_part=g(s, c + "botConnect.distanceInPart", d.bot_in_part),
            bot_tip_length=g(s, c + "botConnect.tipLength", d.bot_tip_length),
            pillar_top_width=g(s, tr + "trunk.topWidth", d.pillar_top_width),
            pillar_bottom_width=g(s, tr + "trunk.botWidth", d.pillar_bottom_width),
            branch_width=g(s, tr + "brunch.botWidth", d.branch_width),
            poly_edges=g(s, tr + "polyEdgeNum", d.poly_edges),
            max_branch_angle=g(s, tr + "trunk.maxBranchAngle", d.max_branch_angle),
            max_branch_height=g(s, tr + "trunk.maxHeight", d.max_branch_height),
            tree_height_type=g(s, tr + "trunk.heightType", d.tree_height_type),
            cross=g(s, "structure.cross.hasCrossConnect", d.cross),
            cross_start=g(s, "structure.cross.startHeight", d.cross_start),
            cross_interval=g(s, "structure.cross.corssInterval", d.cross_interval),
            cross_width=g(s, "structure.cross.barWidth", d.cross_width),
            cross_angle=g(s, "structure.cross.barAngle", d.cross_angle),
            plat_connect=g(s, c + "platConnect.hasPlatConnect", d.plat_connect),
            plat_connect_radius=g(s, c + "platConnect.radius", d.plat_connect_radius),
            plat_connect_height=g(s, c + "platConnect.height", d.plat_connect_height),
            inner_support=bool(t.inner_support),
            raft=g(r, "hasBasePlat", d.raft),
            raft_height=g(r, "platHeight", d.raft_height),
            raft_offset=g(r, "platOffset", d.raft_offset),
            raft_slot_angle=g(r, "slotAngle", d.raft_slot_angle),
            raft_holes=g(r, "hasHole", d.raft_holes),
            hole_radius=g(r, "holeRadius", d.hole_radius),
            hole_interval=g(r, "holeInterval", d.hole_interval),
            hole_edges=g(r, "holeEdgeNum", d.hole_edges),
            lift=float(t.lift_height),
        )

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}

    @classmethod
    def from_dict(cls, d: dict) -> "SupportParams":
        names = {f.name: f for f in fields(cls)}
        base = cls()
        return replace(base, **{k: type(getattr(base, k))(v) for k, v in d.items() if k in names})

    @property
    def base_z(self) -> float:
        return self.raft_height if self.raft else 0.0


@dataclass
class Contact:
    point: np.ndarray       # on the model surface
    normal: np.ndarray      # outward surface normal at the point
    kind: str = OVERHANG


# --------------------------------------------------------------------------- detection
def detect_contacts(mesh: trimesh.Trimesh, p: SupportParams) -> list[Contact]:
    """Overhang areas sampled at anchor spacing, plus local lowest points [GOOSLICER rules,
    with SatelLite's parameter names]. A face needs support when its tilt from horizontal is
    below the critical angle: -n_z > cos(criticalAngle)."""
    out: list[Contact] = []
    n = mesh.face_normals
    need = -n[:, 2] > math.cos(math.radians(p.critical_angle))
    zmin = mesh.bounds[0][2]
    if need.any():
        spacing = max(0.1, p.anchor_dis / 4)
        pts, fidx = _sample_faces(mesh, np.nonzero(need)[0], spacing)
        for i in _border_pass(mesh, need, pts, fidx, p, spacing):
            out.append(Contact(pts[i], n[fidx[i]].copy(), OVERHANG))
    if p.reinforce_lowest:
        for v, nn in _lowest_points(mesh, p.lowest_interval):
            out.append(Contact(v, nn, LOWEST))
    out = _dedupe(out, min(p.anchor_dis, p.lowest_interval) * 0.5)
    # contacts on the very bottom face of a part resting at lift height are all needed
    return [c for c in out if c.point[2] >= zmin - 1e-6]


def _sample_faces(mesh, faces: np.ndarray, spacing: float):
    tri = mesh.triangles[faces]
    area = mesh.area_faces[faces]
    pts, idx = [tri.mean(axis=1)], [faces]
    k = np.ceil(area / (spacing * spacing)).astype(int)
    many = k > 1
    if many.any():
        rep = np.repeat(np.nonzero(many)[0], k[many])
        r = np.random.default_rng(12345).random((len(rep), 2))
        flip = r.sum(axis=1) > 1
        r[flip] = 1 - r[flip]
        T = tri[rep]
        pts.append(T[:, 0] + r[:, :1] * (T[:, 1] - T[:, 0]) + r[:, 1:] * (T[:, 2] - T[:, 0]))
        idx.append(faces[rep])
    return np.vstack(pts), np.concatenate(idx)


BORDER_DEPTH = 0.25     # inset as a share of a region's depth, fitted to SatelLite's arc project (08 §2)


def _border_distance(mesh, need: np.ndarray, pts: np.ndarray, fidx: np.ndarray, step: float):
    """For each sample: its distance to the edge of its overhang region, and the region's id.
    An edge is where an overhang face meets a face that is not one, or an open mesh edge."""
    m = mesh.copy()
    m.merge_vertices()
    inv = m.edges_unique_inverse                       # 3 per face, in face order
    need3 = np.repeat(need, 3)
    count = np.bincount(inv[need3], minlength=len(m.edges_unique))
    edges = m.vertices[m.edges_unique[count == 1]]
    region = np.full(len(m.faces), -1)
    adj = m.face_adjacency[need[m.face_adjacency[:, 0]] & need[m.face_adjacency[:, 1]]]
    for k, comp in enumerate(trimesh.graph.connected_components(adj, nodes=np.nonzero(need)[0], min_len=1)):
        region[comp] = k
    if not len(edges):
        return np.full(len(pts), np.inf), region[fidx]
    L = np.linalg.norm(edges[:, 1] - edges[:, 0], axis=1)
    k = np.maximum(1, np.ceil(L / step).astype(int))
    rep = np.repeat(np.arange(len(edges)), k + 1)
    t = np.concatenate([np.linspace(0, 1, j + 1) for j in k])
    border = edges[rep, 0] + t[:, None] * (edges[rep, 1] - edges[rep, 0])
    d, _ = cKDTree(border).query(pts)
    return d, region[fidx]


def _border_pass(mesh, need, pts, fidx, p: SupportParams, spacing: float) -> list[int]:
    """Overhang contacts kept back from their region's edge [GOOSLICER rule fitted to SatelLite,
    08 §2]: no sample nearer the edge than borderOffset, or a quarter of the region's depth
    when that is less (narrow strips), then Poisson selection at anchorDis, lowest first."""
    d, reg = _border_distance(mesh, need, pts, fidx, spacing / 2)
    deep = np.zeros(reg.max() + 2)
    np.maximum.at(deep, reg + 1, np.where(np.isfinite(d), d, 0.0))
    target = np.where(np.isfinite(d), np.minimum(p.border_offset, BORDER_DEPTH * deep[reg + 1]), 0.0)
    idx = np.nonzero(d >= target - 1e-9)[0]
    return idx[_poisson(pts[idx], p.anchor_dis, order=pts[idx, 2])].tolist()


def _poisson(pts: np.ndarray, r: float, order: np.ndarray) -> list[int]:
    """Greedy Poisson-disk selection, lowest points first."""
    tree = cKDTree(pts)
    taken = np.zeros(len(pts), bool)
    blocked = np.zeros(len(pts), bool)
    for i in np.argsort(order, kind="stable"):
        if blocked[i]:
            continue
        taken[i] = True
        blocked[tree.query_ball_point(pts[i], r * 0.999)] = True
    return list(np.nonzero(taken)[0])


def _lowest_points(mesh, interval: float):
    """Vertices with no lower neighbour (local minima, including flat-bottomed edges)."""
    v = mesh.vertices
    e = mesh.edges_unique
    dz = v[e[:, 1], 2] - v[e[:, 0], 2]
    has_lower = np.zeros(len(v), bool)
    has_lower[e[dz > 1e-9, 1]] = True
    has_lower[e[dz < -1e-9, 0]] = True
    cand = np.nonzero(~has_lower)[0]
    if len(cand) == 0:
        return []
    vn0 = mesh.vertex_normals[cand]
    cand = cand[vn0[:, 2] <= -0.3]
    if len(cand) == 0:
        return []
    keep = _poisson(v[cand], interval, order=v[cand, 2])
    vn = mesh.vertex_normals
    out = []
    for i in keep:
        k = cand[i]
        nrm = vn[k] if np.linalg.norm(vn[k]) > 0 else np.array([0.0, 0, -1])
        nrm = nrm / np.linalg.norm(nrm)
        if nrm[2] > -0.3:       # minima of upward-facing or vertical surface (bottom of a dip) need nothing
            continue
        out.append((v[k].copy(), nrm))
    return out


def _dedupe(cs: list[Contact], r: float) -> list[Contact]:
    if not cs:
        return cs
    pts = np.array([c.point for c in cs])
    pri = np.array([{LOWEST: 0, ISLAND: 0, MANUAL: -1}.get(c.kind, 1) for c in cs], float)
    keep = _poisson(pts, r, order=pri * 1e6 + pts[:, 2])
    return [cs[i] for i in sorted(keep)]


# --------------------------------------------------------------------------- structure
@dataclass
class Pillar:
    contact: Contact
    tip_end: np.ndarray             # where the tip meets the pillar
    foot: np.ndarray                # bottom of the pillar
    on_model: bool                  # lands on the model instead of the raft
    path: list | None = None        # tree: tip end → branch joints → foot (None = straight)


@dataclass
class SupportResult:
    mesh: trimesh.Trimesh | None
    pillars: list[Pillar]
    rejected: list[Contact] = field(default_factory=list)
    braces: int = 0
    raft_holes: int = 0
    trunks: int = 0                 # tree: vertical trunks to the raft (EVO: 0)


def join_solids(solids: list[m3.Manifold]) -> trimesh.Trimesh | None:
    """One mesh holding every solid as its own closed shell, where they overlap too. Slicing
    (manifold sections, non-zero fill), the island check and the 3D view all treat overlapping
    closed shells as their union, so the boolean union is not needed. Unioning a typical support
    set took 1–2 s each time (thousands of small brace rods); joining takes a few ms."""
    vs, fs, off = [], [], 0
    for s in solids:
        mm = s.to_mesh()
        v = np.asarray(mm.vert_properties)[:, :3]
        f = np.asarray(mm.tri_verts)
        if not len(f):
            continue
        vs.append(v)
        fs.append(f.astype(np.int64) + off)
        off += len(v)
    if not fs:
        return None
    return trimesh.Trimesh(np.vstack(vs).astype(np.float64), np.vstack(fs), process=False)


def _frustum(a, b, ra: float, rb: float, n: int) -> m3.Manifold:
    """Cone frustum from point a (radius ra) to point b (radius rb)."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    v = b - a
    L = float(np.linalg.norm(v))
    if L < 1e-6:
        return m3.Manifold()
    c = m3.Manifold.cylinder(L, ra, rb, n)
    R = _rot_z_to(v / L)
    T = np.zeros((3, 4))
    T[:, :3], T[:, 3] = R, a
    return c.transform(T.tolist())


def _rot_z_to(a: np.ndarray) -> np.ndarray:
    z = np.array([0.0, 0.0, 1.0])
    v = np.cross(z, a)
    c = float(np.dot(z, a))
    if np.linalg.norm(v) < 1e-12:
        return np.eye(3) if c > 0 else np.diag([1.0, -1.0, -1.0])
    vx = np.array([[0, -v[2], v[1]], [v[2], 0, -v[0]], [-v[1], v[0], 0]])
    return np.eye(3) + vx + vx @ vx / (1.0 + c)


def _sphere(c, r: float, n: int = 12) -> m3.Manifold:
    return m3.Manifold.sphere(r, n).translate(list(map(float, c)))


def _tip_frame(axis: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Two unit vectors across `axis`: u level and square to the way the tip leans (X for a
    vertical tip), v completing the frame. Cube sides and cross blades line up with them."""
    u = np.cross(axis, [0.0, 0.0, 1.0])
    if np.linalg.norm(u) < 1e-6:
        u = np.array([1.0, 0.0, 0.0])
    u = u / np.linalg.norm(u)
    v = np.cross(axis, u)
    return u, v / np.linalg.norm(v)


def _sections(shape: str, r: float) -> list[np.ndarray]:
    """Convex pieces of a tip's cross-section at half-width r (08 §11)."""
    if shape == "cube":
        return [np.array([[-r, -r], [r, -r], [r, r], [-r, r]])]
    t = r / 3                               # cross blades: a third of the diameter thick
    return [np.array([[-r, -t], [r, -t], [r, t], [-r, t]]),
            np.array([[-t, -r], [t, -r], [t, r], [-t, r]])]


def tip_piece(a, b, ra: float, rb: float, shape: str, n: int) -> list[m3.Manifold]:
    """One support tip from a (the support end, half-width ra) to b (the contact end, rb) in
    the given shape (08 §11). A cylinder keeps rb all the way."""
    a, b = np.asarray(a, float), np.asarray(b, float)
    if shape not in ("cube", "cross"):
        return [_frustum(a, b, rb if shape == "cylinder" else ra, rb, n)]
    d = b - a
    L = float(np.linalg.norm(d))
    if L < 1e-6:
        return []
    u, v = _tip_frame(d / L)
    out = []
    for qa, qb in zip(_sections(shape, ra), _sections(shape, rb)):
        pts = np.vstack([a + qa[:, :1] * u + qa[:, 1:] * v, b + qb[:, :1] * u + qb[:, 1:] * v])
        out.append(m3.Manifold.hull_points(pts.tolist()))
    return out


def plan_pillars(model: trimesh.Trimesh, contacts: list[Contact], p: SupportParams) -> tuple[list[Pillar], list[Contact]]:
    """Tip direction = the outward normal, tilted no further than max_tip_angle from vertical
    (SatelLite's rule, 10 §5). The pillar drops vertically from the tip end to the raft, or onto
    the model below."""
    pillars, rejected = [], []
    if not contacts:
        return pillars, rejected
    down = np.array([0.0, 0.0, -1.0])
    P = np.array([c.point for c in contacts])
    N = np.array([c.normal / max(np.linalg.norm(c.normal), 1e-12) for c in contacts])
    lim = math.cos(math.radians(min(max(p.max_tip_angle, 0.0), 89.0)))
    D = _clamp_dir(N, lim)
    D /= np.maximum(np.linalg.norm(D, axis=1), 1e-12)[:, None]
    half = N + down                     # halfway to vertical
    half[np.linalg.norm(half, axis=1) < 1e-6] = down
    half /= np.linalg.norm(half, axis=1)[:, None]
    half = _clamp_dir(half, lim)
    rc = caster(model)
    o = P + N * 0.02
    # 1) the tip must not pass through the model: try the capped normal, then halfway to
    #    vertical, then straight down [GOOSLICER fallbacks]
    ok = np.zeros(len(P), bool)
    for cand in (D, half, np.tile(down, (len(P), 1))):
        todo = np.nonzero(~ok)[0]
        if not len(todo):
            break
        hit, _ = rc.rays(o[todo], cand[todo], p.tip_length)
        good = hit >= p.tip_length
        D[todo[good]] = cand[todo[good]]
        ok[todo[good]] = True
    E = P + D * p.tip_length
    if ok.any():            # the tip end must be in free space (rays can slip through seams)
        inside = np.zeros(len(P), bool)
        inside[ok] = rc.contains(E[ok])
        ok &= ~inside
    hit_tip = np.where(ok, np.inf, 0.0)
    # 2) vertical drop from the tip end
    base = p.base_z
    floor = np.full(len(P), -np.inf)
    floor_up = np.ones(len(P), bool)
    dist, tri = rc.vertical(E)
    ri = np.flatnonzero(tri >= 0)
    if len(ri):
        floor[ri] = E[ri, 2] - dist[ri]
        floor_up[ri] = rc.normals[tri[ri], 2] > 0.05          # landing on an upward surface
    for i, c in enumerate(contacts):
        if hit_tip[i] < p.tip_length or (floor[i] > base and p.inner_support and floor_up[i]
                                         and E[i, 2] - floor[i] < p.min_on_model_length):
            short = _short_link(model, c, p) if p.inner_support else None
            (pillars.append(short) if short else rejected.append(c))
            continue
        if E[i, 2] <= base + 1e-3:
            rejected.append(c)
            continue
        if floor[i] > base:
            if not p.inner_support or not floor_up[i]:
                rejected.append(c)
                continue
            pillars.append(Pillar(c, E[i], np.array([E[i, 0], E[i, 1], floor[i]]), True))
        else:
            pillars.append(Pillar(c, E[i], np.array([E[i, 0], E[i, 1], base]), False))
    return pillars, rejected


def _short_link(model, c: Contact, p: SupportParams) -> Pillar | None:
    """Tight gap: a vertical double cone from the contact straight down onto an upward-facing
    model surface less than tipLength + minimum length below [GOOSLICER]."""
    o = np.asarray(c.point, float) - np.array([0, 0, 0.02])
    rc = caster(model)
    dist, tri = rc.vertical(o[None])
    if tri[0] < 0 or rc.normals[tri[0]][2] <= 0.05:
        return None
    gap = float(dist[0])
    if gap < 0.15 or gap > p.tip_length + p.min_on_model_length:
        return None
    return Pillar(c, np.array([c.point[0], c.point[1], c.point[2] - gap / 2]),
                  np.array([o[0], o[1], o[2] - gap]), True)


def _clamp_dir(N: np.ndarray, lim: float) -> np.ndarray:
    D = N.copy()
    bad = -D[:, 2] < lim
    h = D[bad, :2]
    hn = np.linalg.norm(h, axis=1)[:, None]
    h = np.where(hn > 1e-9, h / np.maximum(hn, 1e-9), 0)
    D[bad] = np.column_stack([h * math.sqrt(1 - lim * lim), np.full(bad.sum(), -lim)])
    return D


def build_supports(model: trimesh.Trimesh, contacts: list[Contact], p: SupportParams) -> SupportResult:
    if p.style == "tree":
        return build_tree_supports(model, contacts, p)
    pillars, rejected = plan_pillars(model, contacts, p)
    if not pillars:
        return SupportResult(None, [], rejected)
    n = max(6, int(p.poly_edges))
    solids: list[m3.Manifold] = []
    for pl in pillars:
        solids += _tip_solids(pl, p, n)
        solids += _pillar_solids(pl, p, n)
    ground = [pl for pl in pillars if not pl.on_model]
    braces = _bracing(ground, p) if p.bracing else []
    solids += braces
    holes = 0
    if p.raft and ground:
        raft, holes = _raft(ground, p)
        solids.append(raft)
    return SupportResult(join_solids(solids), pillars, rejected, len(braces), holes)


def _tip_solids(pl: Pillar, p: SupportParams, n: int) -> list[m3.Manifold]:
    """Contact tip from the tip end into the model in its shape (08 §11), the optional break
    point ball, and the joint sphere."""
    c = pl.contact
    nrm = c.normal / max(np.linalg.norm(c.normal), 1e-12)
    start = c.point - nrm * p.tip_in_part
    out = tip_piece(pl.tip_end, start, p.tip_bottom_width / 2, p.tip_top_width / 2, p.tip_shape, n)
    if p.tip_ball:
        out.append(_sphere(c.point, max(p.tip_ball_diameter, p.tip_top_width) / 2))
    out.append(_sphere(pl.tip_end, max(p.pillar_top_width / 2, p.tip_bottom_width / 2), n))
    return out


def _pillar_solids(pl: Pillar, p: SupportParams, n: int, cone_len: float | None = None) -> list[m3.Manifold]:
    """Straight pillar from the tip end down to the raft, or onto the model through a cone."""
    rp_top, rp_bot = p.pillar_top_width / 2, p.pillar_bottom_width / 2
    out = []
    if pl.on_model:
        L = p.tip_length if cone_len is None else cone_len
        cl = min(L, max(0.0, pl.tip_end[2] - pl.foot[2]) * 0.5)
        mid = pl.foot + np.array([0, 0, cl])
        out.append(_frustum(mid, pl.tip_end, rp_bot, rp_top, n))
        out += tip_piece(mid, pl.foot - np.array([0, 0, p.bot_in_part]), rp_bot, p.bot_top_width / 2,
                         p.bot_tip_shape, n)
        if p.bot_ball:
            out.append(_sphere(pl.foot, max(p.bot_ball_diameter, p.bot_top_width) / 2))
    else:
        out.append(_frustum(pl.foot - np.array([0, 0, 0.01]), pl.tip_end, rp_bot, rp_top, n))
        out += _plat_foot(pl.foot, pl.tip_end[2] - pl.foot[2], p, n)
    return out


def _plat_foot(foot, height: float, p: SupportParams, n: int) -> list[m3.Manifold]:
    if not p.plat_connect:
        return []
    h = min(p.plat_connect_height, height)
    return [_frustum(foot - np.array([0, 0, 0.01]), foot + np.array([0, 0, h]),
                     p.plat_connect_radius, p.pillar_bottom_width / 2, n)]


# --------------------------------------------------------------------------- tree
@dataclass
class _Node:
    pos: np.ndarray
    children: list = field(default_factory=list)   # node indices merged into this one
    parent: int = -1
    weight: int = 1                                 # contacts carried


def _meet(a: np.ndarray, b: np.ndarray, tan_a: float) -> np.ndarray:
    """Highest point that both a and b can reach going down at most `atan(tan_a)` from
    vertical (a straight drop is allowed too)."""
    if a[2] < b[2]:
        a, b = b, a
    d = float(np.linalg.norm(b[:2] - a[:2]))
    if d < 1e-9:
        return np.array([a[0], a[1], b[2]])
    da = (d + tan_a * (a[2] - b[2])) / 2           # horizontal run of a's branch
    if da >= d:                                     # a reaches b's column above b: join at b
        return b.copy()
    xy = a[:2] + (b[:2] - a[:2]) * (da / d)
    return np.array([xy[0], xy[1], a[2] - da / tan_a])


def _samples(A: np.ndarray, B: np.ndarray, t0: float, t1: float, counts: np.ndarray):
    """counts[i] points evenly from t0 to t1 along each segment A[i]→B[i]: (segment, point)."""
    seg = np.repeat(np.arange(len(A)), counts)
    first = np.cumsum(counts) - counts
    k = np.arange(int(counts.sum())) - first[seg]
    t = t0 + (t1 - t0) * k / np.maximum(counts[seg] - 1, 1)
    return seg, A[seg] + t[:, None] * (B[seg] - A[seg])


def _any_per(seg: np.ndarray, flags: np.ndarray, n: int) -> np.ndarray:
    out = np.zeros(n, bool)
    out[seg[flags]] = True
    return out


def _crosses_many(rc, A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """Per segment A[i]→B[i]: it passes through the surface (touching it at A[i] doesn't
    count). Each in pieces of at most 1 mm: one long query would gather every face in a ball
    around it. A piece the distance grid proves is in open air further than half its length
    from the surface can't touch a face, so only the others are tested."""
    n = len(A)
    if not n:
        return np.zeros(0, bool)
    L = np.linalg.norm(B - A, axis=1)
    k = np.where(L < 1e-9, 0, np.maximum(1, np.ceil(L / CROSS_PIECE))).astype(np.int64)
    seg = np.repeat(np.arange(n), k)
    first = np.cumsum(k) - k
    j = np.arange(int(k.sum())) - first[seg]
    s0 = A[seg] + (j / np.maximum(k[seg], 1))[:, None] * (B[seg] - A[seg])
    s1 = A[seg] + ((j + 1) / np.maximum(k[seg], 1))[:, None] * (B[seg] - A[seg])
    half = np.linalg.norm(s1 - s0, axis=1) / 2
    test = np.flatnonzero(~rc.far_outside((s0 + s1) / 2, half + 1e-6))
    fwd, back = np.zeros(len(seg), bool), np.zeros(len(seg), bool)
    fwd[test], back[test] = rc.hits_both_ways(s0[test], s1[test])
    return _any_per(seg, fwd, n) & _any_per(seg, back, n)


def _segments_clear(model, A, B, clearance) -> np.ndarray:
    """Per segment A[i]→B[i]: it does not pass through the model and keeps clearance[i] from
    its surface except right at A[i] (a tip end may sit close to the surface).

    The exact test: no crossing (`_crosses_many`), and points every 0.5 mm of the far three
    quarters clear of the surface, the end not inside. Two shortcuts with the same answers:
    - certainly clear: the far three quarters, every h (the distance grid's voxel), certainly
      in open air and at least clearance + h/2 from the surface (`Caster.far_outside`). Every
      point between is then clear and the surface can't be crossed there, so only the quarter
      at A[i] needs the exact crossing test;
    - certainly crossing: a point the grid hints is off the surface and out of the open air,
      which the exact inside test finds inside the model while A[i] is outside."""
    A = np.atleast_2d(np.asarray(A, np.float64))
    B = np.atleast_2d(np.asarray(B, np.float64))
    n = len(A)
    c = np.broadcast_to(np.asarray(clearance, np.float64), (n,))
    ok = np.ones(n, bool)
    L = np.linalg.norm(B - A, axis=1)
    todo = L >= 1e-6
    if not todo.any():
        return ok
    rc = caster(model)
    h = rc.voxel
    idx = np.flatnonzero(todo)
    a, b, cc, LL = A[idx], B[idx], c[idx], L[idx]
    seg, far = _samples(a, b, 0.25, 1.0, np.maximum(2, np.ceil(0.75 * LL / h).astype(np.int64) + 1))
    sure = ~_any_per(seg, ~rc.far_outside(far, np.maximum(cc[seg] + h / 2, h)), len(idx))
    # certainly clear: only the quarter at a for crossings
    q = np.flatnonzero(sure)
    ok[idx[q]] = ~_crosses_many(rc, a[q], a[q] + 0.25 * (b[q] - a[q]))
    rest = np.flatnonzero(~sure)
    if not len(rest):
        return ok
    # certainly crossing: the first, middle and last hinted point of each
    keep = np.zeros(len(idx), bool)
    keep[rest] = True
    hint = keep[seg] & rc.maybe_inside(far)
    hs = seg[hint]
    hp = far[hint]
    crossing = np.zeros(len(idx), bool)
    if len(hs):
        segs, start, cnt = np.unique(hs, return_index=True, return_counts=True)
        pick = np.r_[start, start + cnt // 2, start + cnt - 1]
        inside = rc.contains(np.vstack([a[segs], hp[pick]]))
        a_in, x_in = inside[:len(segs)], inside[len(segs):].reshape(3, -1).any(axis=0)
        crossing[segs[~a_in & x_in]] = True
    ok[idx[crossing]] = False
    rest = rest[~crossing[rest]]
    # the exact test for the others
    cross = _crosses_many(rc, a[rest], b[rest])
    ok[idx[rest[cross]]] = False
    rest = rest[~cross]
    if not len(rest):
        return ok
    pseg, pts = _samples(a[rest], b[rest], 0.25, 1.0, np.maximum(2, (LL[rest] / 0.5).astype(np.int64)))
    near = np.zeros(len(pts), bool)
    pc = cc[rest][pseg]
    check = ~rc.far_outside(pts, pc)              # the others are certainly clear
    for cv in np.unique(pc[check]):
        m = np.flatnonzero(check & (pc == cv))
        near[m] = ~rc.clear_of(pts[m], float(cv))
    bad = _any_per(pseg, near, len(rest))
    last = np.cumsum(np.maximum(2, (LL[rest] / 0.5).astype(np.int64))) - 1
    bad |= rc.contains(pts[last])
    ok[idx[rest[bad]]] = False
    return ok


def _segment_clear(model, a: np.ndarray, b: np.ndarray, clearance: float) -> bool:
    """`_segments_clear` for one segment."""
    return bool(_segments_clear(model, a, b, clearance)[0])


TREE_BATCH = 32                 # tree merges judged together
CROSS_PIECE = 1.0               # mm: crossing tests go in pieces this long (see _crosses_many)


def plan_tree(model: trimesh.Trimesh, contacts: list[Contact], p: SupportParams):
    """Tree layout [GOOSLICER, 08 §8]. Tips and the straight-drop test are the EVO ones
    (`plan_pillars`). Tip ends that drop to the raft are merged pairwise, highest meeting point
    first: two branches meet at the highest point both reach at the branch slope, which
    SatelLite's heightType sets (EleTriBuilder FUN_18001e680, 08 §8): 0 = 45°; 1 = 30° from
    horizontal, with joints no higher than maxHeight above the raft; 2 = maxBranchAngle from
    horizontal. A merge is taken only when both branches stay clear of the model, the joint stays
    at least 1 mm above the raft, and the joint can still drop straight to the raft with trunk
    clearance. Whatever is left drops as a vertical trunk.
    Returns (pillars, rejected, nodes, roots)."""
    import heapq
    pillars, rejected = plan_pillars(model, contacts, p)
    ground = [i for i, pl in enumerate(pillars) if not pl.on_model]
    nodes = [_Node(pillars[i].tip_end.copy()) for i in ground]
    slope = {1: 30.0, 2: p.max_branch_angle}.get(int(p.tree_height_type), 45.0)   # ° from horizontal
    tan_a = 1.0 / math.tan(math.radians(min(max(slope, 10.0), 89.0)))             # run per unit drop
    base = p.base_z
    zmin_join = base + 1.0
    cap = base + p.max_branch_height if int(p.tree_height_type) == 1 else math.inf
    clearance = p.branch_width / 2 + 0.1
    top = max((nd.pos[2] for nd in nodes), default=base)
    reach = 2 * (min(top, cap) - zmin_join) * tan_a + 1e-6
    active = set(range(len(nodes)))
    heap: list = []

    def push(i, j):
        m = _meet(nodes[i].pos, nodes[j].pos, tan_a)
        if m[2] > cap:                  # height type 1: joints no higher than maxHeight
            m = np.array([(nodes[i].pos[0] + nodes[j].pos[0]) / 2, (nodes[i].pos[1] + nodes[j].pos[1]) / 2, cap])
        if m[2] < zmin_join:
            return
        heapq.heappush(heap, (-m[2], i, j, m))

    if len(nodes) >= 2:
        xy = np.array([nd.pos[:2] for nd in nodes])
        for i, j in cKDTree(xy).query_pairs(reach):
            push(i, j)
    trunk_clear = p.pillar_bottom_width / 2 + 0.1
    good: dict = {}                     # (i, j) → the merge is allowed (depends only on i, j)

    def judge(batch):
        """Whether each merge is allowed: no model under the joint, both branches clear of the
        model, and the joint's trunk reaching the raft clear of it. All the batch's segments
        go to `_segments_clear` together (that and not per merge is what makes it fast)."""
        M = np.array([e[3] for e in batch])
        _, hit = caster(model).vertical(M - [0, 0, 1e-3])
        A, B, C, owner = [], [], [], []
        for k, (_, i, j, m) in enumerate(batch):
            for q in (nodes[i].pos, nodes[j].pos):
                if np.linalg.norm(q - m) > 1e-6:
                    A.append(q), B.append(m), C.append(clearance), owner.append(k)
            A.append(m), B.append(np.array([m[0], m[1], base])), C.append(trunk_clear), owner.append(k)
        clear = _segments_clear(model, np.array(A), np.array(B), np.array(C))
        bad = np.zeros(len(batch), bool)
        bad[np.array(owner)[~clear]] = True
        for k, e in enumerate(batch):
            good[(e[1], e[2])] = bool(hit[k] < 0 and not bad[k])

    # merges in order of joint height, highest first, as one at a time; judged in batches. A
    # merge adds candidates; if one outranks the rest of the batch they go back on the heap
    # (judged already), so the order and the result are those of judging one at a time
    while heap:
        batch = []
        while heap and len(batch) < TREE_BATCH:
            e = heapq.heappop(heap)
            if e[1] in active and e[2] in active:
                batch.append(e)
        judge([e for e in batch if (e[1], e[2]) not in good])
        for n_done, (_, i, j, m) in enumerate(batch):
            if i not in active or j not in active or not good[(i, j)]:
                continue
            k = len(nodes)
            nodes.append(_Node(m, [i, j], -1, nodes[i].weight + nodes[j].weight))
            nodes[i].parent = nodes[j].parent = k
            active -= {i, j}
            for o in active:
                if np.linalg.norm(nodes[o].pos[:2] - m[:2]) <= reach:
                    push(k, o)
            active.add(k)
            later = batch[n_done + 1:]
            if later and heap and heap[0] < later[0]:
                for e in later:
                    heapq.heappush(heap, e)
                break
    roots = sorted(active)
    # each contact's path down to its root's foot
    for leaf, pi in enumerate(ground):
        path, q = [nodes[leaf].pos], leaf
        while nodes[q].parent >= 0:
            q = nodes[q].parent
            path.append(nodes[q].pos)
        foot = np.array([path[-1][0], path[-1][1], base])
        path.append(foot)
        pl = pillars[pi]
        pl.foot, pl.path = foot, path
    return pillars, rejected, nodes, roots


def build_tree_supports(model: trimesh.Trimesh, contacts: list[Contact], p: SupportParams) -> SupportResult:
    pillars, rejected, nodes, roots = plan_tree(model, contacts, p)
    if not pillars:
        return SupportResult(None, [], rejected)
    n = max(6, int(p.poly_edges))
    rb = p.branch_width / 2
    solids: list[m3.Manifold] = []
    for pl in pillars:
        solids += _tip_solids(pl, p, n)
        if pl.on_model:
            solids += _pillar_solids(pl, p, n, cone_len=p.bot_tip_length)
    for nd in nodes:                     # branches: child → joint
        if nd.parent >= 0:
            solids.append(_frustum(nd.pos, nodes[nd.parent].pos, rb, rb, n))
        if nd.children:
            solids.append(_sphere(nd.pos, rb, n))
    feet = []
    for r in roots:                      # trunks to the raft
        top = nodes[r].pos
        foot = np.array([top[0], top[1], p.base_z])
        solids.append(_frustum(foot - np.array([0, 0, 0.01]), top, p.pillar_bottom_width / 2, p.pillar_top_width / 2, n))
        solids += _plat_foot(foot, top[2] - foot[2], p, n)
        feet.append(Pillar(Contact(top, np.array([0.0, 0, -1])), top, foot, False))
    braces = _cross_braces(feet, p) if p.cross else []
    solids += braces
    holes = 0
    if p.raft and feet:
        raft, holes = _raft(feet, p)
        solids.append(raft)
    return SupportResult(join_solids(solids), pillars, rejected, len(braces), holes, trunks=len(roots))


def _brace_pairs(feet: list[Pillar], max_dist: float, min_dist: float) -> list[tuple[int, int]]:
    """Minimum spanning tree over Delaunay neighbours no further apart than max_dist."""
    from scipy.sparse import coo_matrix
    if len(feet) < 2:
        return []
    xy = np.array([pl.foot[:2] for pl in feet])
    edges = set()
    if len(feet) >= 3:
        try:
            for s in Delaunay(xy).simplices:
                for a, b in ((s[0], s[1]), (s[1], s[2]), (s[0], s[2])):
                    edges.add((min(a, b), max(a, b)))
        except Exception:
            pass
    if not edges:
        edges = {(i, j) for i in range(len(feet)) for j in range(i + 1, len(feet))}
    e = np.array(sorted(edges))
    w = np.linalg.norm(xy[e[:, 0]] - xy[e[:, 1]], axis=1)
    ok = (w <= max_dist) & (w > min_dist)
    e, w = e[ok], w[ok]
    if len(e) == 0:
        return []
    mst = minimum_spanning_tree(coo_matrix((w, (e[:, 0], e[:, 1])), shape=(len(feet), len(feet)))).tocoo()
    return list(zip(mst.row.tolist(), mst.col.tolist()))


def _cross_braces(feet: list[Pillar], p: SupportParams) -> list[m3.Manifold]:
    """Tree cross braces: an X of barWidth rods at barAngle between neighbouring trunks, the
    first at startHeight above the raft, then every crossInterval [names OBSERVED; layout
    GOOSLICER]."""
    rods = []
    r = p.cross_width / 2
    tan = math.tan(math.radians(p.cross_angle))
    for a, b in _brace_pairs(feet, p.cross_max_dist, p.pillar_bottom_width):
        A, B = feet[a], feet[b]
        d = float(np.linalg.norm(A.foot[:2] - B.foot[:2]))
        rise = d * tan
        zmax = min(A.tip_end[2], B.tip_end[2]) - 0.3
        z = p.base_z + p.cross_start
        while z + rise <= zmax:
            rods.append(_frustum(np.r_[A.foot[:2], z], np.r_[B.foot[:2], z + rise], r, r, 8))
            rods.append(_frustum(np.r_[B.foot[:2], z], np.r_[A.foot[:2], z + rise], r, r, 8))
            z += max(p.cross_interval, rise)
    return rods


def _bracing(ground: list[Pillar], p: SupportParams) -> list[m3.Manifold]:
    """X-bracing at diamondAngle along a minimum spanning tree of neighbouring ground pillars
    no further apart than diamondWidth; rods start 0.5 mm above the raft [OBSERVED in arc.goo:
    0.8 mm rods at 60° from z ≈ 1.0 mm; topology INFERRED]."""
    xy = np.array([pl.foot[:2] for pl in ground]) if ground else np.zeros((0, 2))
    top = np.array([pl.tip_end[2] for pl in ground])
    rods = []
    r = p.edge_width / 2
    tan = math.tan(math.radians(p.diamond_angle))
    z0 = p.base_z + 0.5
    for a, b in _brace_pairs(ground, p.diamond_width, p.edge_width):
        d = float(np.linalg.norm(xy[a] - xy[b]))
        rise = d * tan
        zmax = min(top[a], top[b]) - 0.3
        z = z0
        while z + rise <= zmax:
            A0, B1 = np.r_[xy[a], z], np.r_[xy[b], z + rise]
            B0, A1 = np.r_[xy[b], z], np.r_[xy[a], z + rise]
            rods.append(_frustum(A0, B1, r, r, 8))
            rods.append(_frustum(B0, A1, r, r, 8))
            z += rise
    return rods


def raft_outline(ground: list[Pillar], p: SupportParams):
    """Convex hull of the pillar feet grown by platOffset + pillar radius [SAMPLE: arc.goo's raft
    top outline is within 1% of this]."""
    from shapely.geometry import MultiPoint
    pts = [pl.foot[:2] for pl in ground]
    grow = p.raft_offset + max(p.pillar_bottom_width / 2, p.plat_connect_radius if p.plat_connect else 0)
    return MultiPoint(pts).convex_hull.buffer(grow, quad_segs=8)


def _raft(ground: list[Pillar], p: SupportParams) -> tuple[m3.Manifold, int]:
    top = raft_outline(ground, p)
    inset = p.raft_height / max(math.tan(math.radians(p.raft_slot_angle)), 1e-3)
    bot = top.buffer(-inset, quad_segs=8)
    if bot.is_empty:
        bot = top
    pts = [(x, y, 0.0) for x, y in bot.exterior.coords] + [(x, y, p.raft_height) for x, y in top.exterior.coords]
    raft = m3.Manifold.hull_points(pts)
    holes = 0
    if p.raft_holes and p.hole_radius > 0 and p.hole_interval > 0:
        a = p.hole_interval
        minx, miny, maxx, maxy = top.bounds
        dx = a * math.sqrt(3) / 2
        cs = []
        for i in range(int((maxx - minx) / dx) + 2):
            x = minx + i * dx
            for j in range(int((maxy - miny) / a) + 2):
                cs.append((x, miny + j * a + (a / 2 if i % 2 else 0)))
        cs = np.array(cs)
        from shapely import contains_xy
        inner = bot.buffer(-(p.hole_radius + 0.5))
        cs = cs[contains_xy(inner, cs[:, 0], cs[:, 1])] if not inner.is_empty else cs[:0]
        feet = np.array([pl.foot[:2] for pl in ground])
        # pillar feet stay on raft material: in arc.goo no pillar overlaps a hole by more than
        # 4% of its section [SAMPLE]
        clear = p.hole_radius + p.pillar_bottom_width / 2 - 0.05
        if p.plat_connect:
            clear = p.hole_radius + p.plat_connect_radius - 0.05
        if len(cs):
            d, _ = cKDTree(feet).query(cs)
            cs = cs[d >= clear]
        cyl = [m3.Manifold.cylinder(p.raft_height + 0.2, p.hole_radius, p.hole_radius, p.hole_edges)
               .translate([float(x), float(y), -0.1]) for x, y in cs]
        if cyl:
            raft = raft - m3.Manifold.batch_boolean(cyl, m3.OpType.Add)
        holes = len(cyl)
    return raft, holes


# ------------------------------------------------------------------------- island check
@dataclass
class Island:
    z: float                # layer where it first appears
    point: np.ndarray       # a point inside it on that layer (x, y, z)
    area: float             # largest area (mm²) while still unconnected
    height: float = 0.0     # how far it hangs (mm) before joining grounded material
    # where it is, for showing it: (x0, y0, pixel, mask) — mask[r, c] covers the pixel centred
    # at (x0 + (c + 0.5) * pixel, y0 + (r + 0.5) * pixel), every layer it hangs on together
    footprint: tuple | None = field(default=None, repr=False, compare=False)


# An island is only reported when it hangs longer or grows larger than this. Below both, the
# rasteriser splits a nearly flat underside between support tips into specks that join the
# supported area one or two layers later (08 §5). Supported arc: 34 specks at 0.05 mm layers,
# all hanging ≤ 0.1 mm and ≤ 0.4 mm²; at 0.03 mm layers one strip of 1.05 mm² hangs one layer.
# Every real island of the unsupported arc hangs ≥ 0.3 mm.
ISLAND_MAX_HANG = 0.15      # mm
ISLAND_MAX_AREA = 5.0       # mm²


def unsupported_islands(meshes: list[trimesh.Trimesh], layer_height: float = 0.05,
                        first_layer: float | None = None, min_area: float = 0.01,
                        pixel: float = 0.1, max_hang: float = ISLAND_MAX_HANG,
                        max_area: float = ISLAND_MAX_AREA, gpu: bool = False) -> list[Island]:
    """Regions that print in mid-air. The union of all meshes is rasterised at `pixel` mm per
    layer. The first layer rests on the plate and is grounded; a connected region of a later
    layer is grounded when it shares a pixel with grounded material of the layer below.
    Ungrounded regions are followed upward (they keep their identity while they overlap) and
    reported when they stay ungrounded for more than `max_hang` mm or reach `max_area` mm².
    gpu: draw the layers on the GPU (gpu_raster, in a headless EGL context: worker processes
    only), several times faster; on the CPU if that can't start. A few edge pixels per layer can
    differ, as when slicing on the GPU."""
    if gpu:
        from .gl_headless import HeadlessUnavailable, egl_context
        try:
            with egl_context():
                from .gpu_raster import GpuRasterizer
                return _islands(meshes, layer_height, first_layer, min_area, pixel, max_hang,
                                max_area, lambda ms, spec: GpuRasterizer(ms, spec))
        except HeadlessUnavailable:
            pass
        except Exception:  # noqa: BLE001 (drivers fail in many ways: the CPU always works)
            pass
    return _islands(meshes, layer_height, first_layer, min_area, pixel, max_hang, max_area, None)


class _CpuLayers:
    """Layer bitmaps on the CPU: each mesh sectioned and the outlines filled together
    (non-zero winding), which is their union without a boolean; meshes that aren't closed are
    sectioned as the slicer does. Same interface as gpu_raster.GpuRasterizer."""

    def __init__(self, meshes, spec):
        from .slicer import _Body
        self.spec = spec
        self.bodies = [_Body(np.asarray(m.vertices), np.asarray(m.faces)) for m in meshes]

    def bitmap(self, z: float) -> np.ndarray:
        from .raster import rasterize_bitmap
        polys = [q for b in self.bodies for q in b.section(z)]
        if not polys:
            return np.zeros((self.spec.res_y, self.spec.res_x), np.uint8)
        return rasterize_bitmap(polys, self.spec)

    def close(self) -> None:
        pass


def _islands(meshes, layer_height, first_layer, min_area, pixel, max_hang, max_area, make_layers):
    from .raster import RasterSpec
    meshes = [m for m in meshes if len(m.faces)]
    if not meshes:
        return []
    lo = np.min([m.bounds[0] for m in meshes if len(m.faces)], axis=0)
    hi = np.max([m.bounds[1] for m in meshes if len(m.faces)], axis=0)
    x0, y0, zmin = (float(v) for v in lo)
    x1, y1, zmax = (float(v) for v in hi)
    um = pixel * 1000
    spec = RasterSpec(int(math.ceil((x1 - x0) / pixel)) + 4, int(math.ceil((y1 - y0) / pixel)) + 4,
                      um, um, mirror_x=False, mirror_y=True, origin_x=x0 - 2 * pixel, origin_y=y0 - 2 * pixel)
    h = layer_height
    # layers are counted from the plate: the first layer rests on it, anything that first
    # appears higher up (a lifted part with nothing below) is an island
    z = first_layer if first_layer is not None else h / 2
    px2 = pixel ** 2
    min_px = max(1, int(min_area / px2))
    zs = []
    while z < zmax:
        zs.append(z)
        z += h
        if z < zmin - h:            # skip empty air below the body (nothing to ground or track)
            n_skip = int((zmin - h - z) / h)
            z += n_skip * h
    layers = (make_layers or _CpuLayers)(meshes, spec)
    try:
        found, cells = _track_islands(layers, zs, spec, h, pixel, min_px)
    finally:
        layers.close()
    out = []
    for isl, cl in zip(found, cells):
        if not (isl.height > max_hang + 1e-9 or isl.area >= max_area):
            continue
        rr, cc = np.divmod(np.unique(np.concatenate(cl)), spec.res_x)
        mask = np.zeros((rr.max() - rr.min() + 1, cc.max() - cc.min() + 1), bool)
        mask[rr - rr.min(), cc - cc.min()] = True
        isl.footprint = (spec.ox + cc.min() * pixel, spec.oy + rr.min() * pixel, pixel, mask)
        out.append(isl)
    return out


def _track_islands(layers, zs, spec, h, pixel, min_px):
    """Follow ungrounded regions up the layers (see unsupported_islands): (islands, flat pixel
    indices of each, per layer)."""
    from scipy import ndimage
    px2 = pixel ** 2
    ground = None                   # grounded pixels of the previous layer
    ids = None                      # island id per ungrounded pixel of the previous layer (0 = none)
    found: list[Island] = []        # every ungrounded region, indexed by id - 1
    cells: list[list[np.ndarray]] = []   # flat pixel indices per island, every layer
    for z in zs:
        cur = layers.bitmap(z) > 0
        if ground is None:
            ground, ids = cur, np.zeros(cur.shape, np.int32)
        else:
            lab, n = ndimage.label(cur)
            g = np.unique(lab[cur & ground])
            g = g[g > 0]
            new_ids = np.zeros(cur.shape, np.int32)
            free = np.setdiff1d(np.arange(1, n + 1), g)
            if len(free):
                sizes = ndimage.sum_labels(cur, lab, free)
                for k, sz in zip(free, sizes):
                    m = lab == k
                    prev_ids = ids[m]
                    prev_ids = prev_ids[prev_ids > 0]
                    if len(prev_ids):           # continues (or merges) earlier islands: keep the oldest
                        i = int(prev_ids.min())
                    elif sz >= min_px:          # appears in mid-air on this layer
                        rr, cc = np.nonzero(m)
                        r, c = rr.mean(), cc.mean()
                        j = int(np.argmin((rr - r) ** 2 + (cc - c) ** 2))   # a pixel inside
                        x = spec.ox + (cc[j] + 0.5) * pixel
                        y = spec.oy + (rr[j] + 0.5) * pixel
                        found.append(Island(float(z), np.array([x, y, z]), 0.0))
                        cells.append([])
                        i = len(found)
                    else:
                        continue
                    isl = found[i - 1]
                    isl.area = max(isl.area, float(sz * px2))
                    isl.height = float(z - isl.z + h)
                    cells[i - 1].append(np.flatnonzero(m))
                    new_ids[m] = i
            ground, ids = np.isin(lab, g), new_ids
    return found, cells


def island_faces(meshes: list[trimesh.Trimesh], islands: list[Island],
                 layer_height: float = 0.05) -> list[np.ndarray]:
    """Per mesh (world coordinates), the faces that make up the islands' unsupported surface:
    downward-facing faces over each island's footprint (two pixels of slack) within the height
    it hangs. An island too small to catch such a face gets the faces nearest its point."""
    sels = [np.zeros(len(m.faces), bool) for m in meshes]
    for hits in island_faces_each(meshes, islands, layer_height):
        for sel, hit in zip(sels, hits):
            sel |= hit
    return sels


def island_faces_each(meshes: list[trimesh.Trimesh], islands: list[Island],
                      layer_height: float = 0.05) -> list[list[np.ndarray]]:
    """island_faces for each island on its own: [island][mesh] face masks."""
    from scipy import ndimage
    out = []
    cens = [m.triangles_center if len(m.faces) else np.zeros((0, 3)) for m in meshes]
    for isl in islands:
        hits = []
        for m, cen in zip(meshes, cens):
            hit = np.zeros(len(cen), bool)
            if isl.footprint is not None and len(cen):
                x0, y0, px, mask = isl.footprint
                grown = ndimage.binary_dilation(mask, iterations=2, border_value=0)
                grown = np.pad(grown, 2)
                c = np.floor((cen[:, 0] - x0) / px).astype(int) + 2
                r = np.floor((cen[:, 1] - y0) / px).astype(int) + 2
                ok = ((cen[:, 2] >= isl.z - 2 * layer_height)
                      & (cen[:, 2] <= isl.z + isl.height + layer_height)
                      & (m.face_normals[:, 2] < -0.05)
                      & (c >= 0) & (r >= 0) & (c < grown.shape[1]) & (r < grown.shape[0]))
                hit[ok] = grown[r[ok], c[ok]]
            hits.append(hit)
        if not any(h.any() for h in hits):
            ds = [np.linalg.norm(cen - isl.point, axis=1) for cen in cens]
            reach = max(np.sqrt(isl.area), 0.5) + 2 * layer_height
            hits = [d <= reach for d in ds]
            if not any(h.any() for h in hits) and any(len(d) for d in ds):
                best = min(float(d.min()) for d in ds if len(d))
                hits = [d == best for d in ds]
        out.append(hits)
    return out


# ------------------------------------------------------------------------- auto support
def auto_support(model: trimesh.Trimesh, p: SupportParams, extra: list[Contact] | None = None,
                 max_passes: int = 3, gpu: bool = False) -> tuple[list[Contact], SupportResult, list[Island]]:
    """Detect contacts, build, then add a contact under every remaining floating island (the
    island check on the GPU with gpu=True: see unsupported_islands)."""
    contacts = detect_contacts(model, p) + list(extra or [])
    res = build_supports(model, contacts, p)
    islands = []
    for _ in range(max_passes):
        meshes = [model] + ([res.mesh] if res.mesh is not None else [])
        islands = unsupported_islands(meshes, layer_height=0.1, gpu=gpu)
        if not islands:
            break
        added = []
        for isl in islands:
            c = island_contact(model, isl.point)
            if c is not None:
                c.kind = ISLAND
                added.append(c)
        if not added:
            break
        contacts = contacts + added
        res = build_supports(model, contacts, p)
    return contacts, res, islands


def island_contact(model: trimesh.Trimesh, point) -> Contact | None:
    """The underside of the island containing `point` (a point inside it): cast down."""
    o = np.array([[point[0], point[1], point[2] + 1e-3]])
    rc = caster(model)
    dist, tri = rc.vertical(o)
    if tri[0] < 0 or rc.normals[tri[0]][2] >= 0:
        return None
    return Contact(o[0] - [0, 0, dist[0]], rc.normals[tri[0]].copy(), ISLAND)


def contact_below(model: trimesh.Trimesh, point) -> Contact | None:
    """The model surface point straight below (or at) `point`, approached from underneath."""
    o = np.array([[point[0], point[1], model.bounds[0][2] - 1.0]])
    rc = caster(model)
    dist, tri = rc.vertical(o, up=True)
    if tri[0] < 0:
        return None
    return Contact(o[0] + [0, 0, dist[0]], rc.normals[tri[0]].copy(), MANUAL)


# ------------------------------------------------------------------------- per-part plan
@dataclass(eq=False)
class FixedSupports:
    """Support geometry made elsewhere (SatelLite, 10 §6), used as it is while the part keeps the
    mesh, turn, scale and height it was made for; a move on the plate shifts it. Otherwise the
    plan's contacts are built by GooSlicer instead."""
    mesh: trimesh.Trimesh               # world frame, for a part placed at `position`
    model: object                       # the part mesh it was made for (compared by identity)
    position: np.ndarray
    rotation: np.ndarray
    scale: np.ndarray
    source: str = "SatelLite"

    def offset(self, part) -> np.ndarray | None:
        """The XY shift that puts the geometry under `part`, or None when it no longer fits."""
        if (part.mesh is not self.model or not np.allclose(part.rotation, self.rotation, atol=1e-9)
                or not np.allclose(part.scale, self.scale, atol=1e-9)
                or abs(float(part.position[2] - self.position[2])) > 1e-6):
            return None
        return np.array([part.position[0] - self.position[0], part.position[1] - self.position[1], 0.0])


@dataclass
class SupportPlan:
    """A part's supports: parameters plus contact points stored in the part's local mesh frame,
    so they follow the part when it is moved, rotated or scaled. Geometry is rebuilt from it."""
    params: SupportParams
    points: np.ndarray                  # (N, 3) local
    normals: np.ndarray                 # (N, 3) local, outward
    kinds: list[str]
    template: str = ""                  # name of the template the params came from
    fixed: FixedSupports | None = None  # exact geometry from another program; edits drop it

    @classmethod
    def from_world(cls, part, contacts: list[Contact], params: SupportParams, template: str = "") -> "SupportPlan":
        if contacts:
            W = np.array([c.point for c in contacts], float)
            Nw = np.array([c.normal for c in contacts], float)
        else:
            W = Nw = np.zeros((0, 3))
        loc = ((W - part.position) @ part.rotation) / part.scale
        nl = (Nw @ part.rotation) * part.scale
        nl /= np.maximum(np.linalg.norm(nl, axis=1), 1e-12)[:, None]
        return cls(params, loc, nl, [c.kind for c in contacts], template)

    def world_contacts(self, part) -> list[Contact]:
        W = (self.points * part.scale) @ part.rotation.T + part.position
        Nw = (self.normals / part.scale) @ part.rotation.T
        Nw /= np.maximum(np.linalg.norm(Nw, axis=1), 1e-12)[:, None]
        return [Contact(W[i], Nw[i], self.kinds[i]) for i in range(len(self.kinds))]

    def with_contacts(self, part, contacts: list[Contact]) -> "SupportPlan":
        return SupportPlan.from_world(part, contacts, self.params, self.template)

    def __len__(self) -> int:
        return len(self.kinds)


def fixed_result(part) -> tuple[SupportResult, np.ndarray] | None:
    """(result, the part position it is placed for) when the part's plan carries fixed geometry
    that still fits the part; None otherwise."""
    plan = getattr(part, "supports", None)
    fx = getattr(plan, "fixed", None)
    if fx is None or fx.offset(part) is None:
        return None
    return SupportResult(fx.mesh, []), fx.position.copy()


def build_for_part(part) -> SupportResult | None:
    """World-space support geometry for a part with a plan (None without one)."""
    plan = getattr(part, "supports", None)
    if plan is None:
        return None
    fr = fixed_result(part)
    if fr is not None:
        m = fr[0].mesh.copy()
        m.apply_translation(part.supports.fixed.offset(part))
        return SupportResult(m, [])
    return build_supports(part.world_mesh(), plan.world_contacts(part), plan.params)


def nearest_pillar(pillars: list[Pillar], point, max_dist: float = 1.5) -> int | None:
    """Index of the pillar whose tip or shaft passes closest to `point` (support picking)."""
    best, bi = max_dist, None
    q = np.asarray(point, float)
    for i, pl in enumerate(pillars):
        pts = [pl.contact.point] + (list(pl.path) if pl.path else [pl.tip_end, pl.foot])
        for a, b in zip(pts[:-1], pts[1:]):
            ab = b - a
            t = 0.0 if not ab.any() else float(np.clip(np.dot(q - a, ab) / np.dot(ab, ab), 0, 1))
            d = float(np.linalg.norm(a + t * ab - q))
            if d < best:
                best, bi = d, i
    return bi
