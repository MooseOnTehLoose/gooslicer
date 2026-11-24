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

from .hollow import to_manifold, to_trimesh

# Support kinds
OVERHANG, LOWEST, ISLAND, MANUAL = "overhang", "lowest", "island", "manual"


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
    # bottom connection (supports that land on the model)
    bot_top_width: float = 0.3          # botConnect.topWidth
    bot_in_part: float = 0.1            # botConnect.distanceInPart
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
    max_branch_angle: float = 45.0      # structure.tree.trunk.maxBranchAngle, ° from vertical
    max_branch_height: float = 5.0      # structure.tree.trunk.maxHeight: longest drop of one branch
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
        pts, fidx = _sample_faces(mesh, np.nonzero(need)[0], max(0.1, p.anchor_dis / 4))
        keep = _poisson(pts, p.anchor_dis, order=pts[:, 2])
        for i in keep:
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


def plan_pillars(model: trimesh.Trimesh, contacts: list[Contact], p: SupportParams) -> tuple[list[Pillar], list[Contact]]:
    """Tip direction = outward normal blended with straight down, at most 45° from vertical.
    The pillar drops vertically from the tip end to the raft, or onto the model below."""
    pillars, rejected = [], []
    if not contacts:
        return pillars, rejected
    down = np.array([0.0, 0.0, -1.0])
    P = np.array([c.point for c in contacts])
    N = np.array([c.normal / max(np.linalg.norm(c.normal), 1e-12) for c in contacts])
    D = N + down
    D[np.linalg.norm(D, axis=1) < 1e-6] = down
    D /= np.linalg.norm(D, axis=1)[:, None]
    lim = math.cos(math.radians(45))
    flat = -D[:, 2] < lim
    if flat.any():          # tilt toward vertical, keeping the horizontal direction
        h = D[flat, :2]
        hn = np.linalg.norm(h, axis=1)[:, None]
        h = np.where(hn > 1e-9, h / np.maximum(hn, 1e-9), 0)
        D[flat] = np.column_stack([h * math.sin(math.radians(45)), np.full(flat.sum(), -lim)])
    ray = model.ray
    o = P + N * 0.02
    # 1) the tip must not pass through the model: try the blended direction, then straight
    #    down, then the normal (clamped as above) [GOOSLICER]
    ok = np.zeros(len(P), bool)
    for cand in (D, np.tile(down, (len(P), 1)), _clamp_dir(N, lim)):
        todo = np.nonzero(~ok)[0]
        if not len(todo):
            break
        hit = np.full(len(todo), np.inf)
        loc, ri, _ = ray.intersects_location(o[todo], cand[todo], multiple_hits=False)
        if len(ri):
            hit[ri] = np.linalg.norm(loc - o[todo][ri], axis=1)
        good = hit >= p.tip_length
        D[todo[good]] = cand[todo[good]]
        ok[todo[good]] = True
    E = P + D * p.tip_length
    if ok.any():            # the tip end must be in free space (rays can slip through seams)
        inside = np.zeros(len(P), bool)
        inside[ok] = model.contains(E[ok])
        ok &= ~inside
    hit_tip = np.where(ok, np.inf, 0.0)
    # 2) vertical drop from the tip end
    base = p.base_z
    floor = np.full(len(P), -np.inf)
    floor_up = np.ones(len(P), bool)
    loc, ri, tri = ray.intersects_location(E, np.tile(down, (len(E), 1)), multiple_hits=False)
    if len(ri):
        floor[ri] = loc[:, 2]
        floor_up[ri] = model.face_normals[tri][:, 2] > 0.05    # landing on an upward surface
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
    loc, _, tri = model.ray.intersects_location(o[None], np.array([[0.0, 0, -1]]), multiple_hits=False)
    if not len(loc) or model.face_normals[tri[0]][2] <= 0.05:
        return None
    gap = float(o[2] - loc[0][2])
    if gap < 0.15 or gap > p.tip_length + p.min_on_model_length:
        return None
    return Pillar(c, np.array([c.point[0], c.point[1], c.point[2] - gap / 2]), loc[0].copy(), True)


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
    body = m3.Manifold.batch_boolean(solids, m3.OpType.Add)
    return SupportResult(to_trimesh(body), pillars, rejected, len(braces), holes)


def _tip_solids(pl: Pillar, p: SupportParams, n: int) -> list[m3.Manifold]:
    """Contact tip: frustum from the tip end into the model, optional ball, joint sphere."""
    c = pl.contact
    nrm = c.normal / max(np.linalg.norm(c.normal), 1e-12)
    start = c.point - nrm * p.tip_in_part
    out = [_frustum(pl.tip_end, start, p.tip_bottom_width / 2, p.tip_top_width / 2, n)]
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
        out.append(_frustum(pl.foot - np.array([0, 0, p.bot_in_part]), mid, p.bot_top_width / 2, rp_bot, n))
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


def _segment_clear(model, a: np.ndarray, b: np.ndarray, clearance: float) -> bool:
    """The segment a→b does not pass through the model and keeps `clearance` from its
    surface except right at a (a tip end may sit close to the surface)."""
    v = b - a
    L = float(np.linalg.norm(v))
    if L < 1e-6:
        return True
    loc, _, _ = model.ray.intersects_location(np.array([a, b]), np.array([v / L, -v / L]), multiple_hits=False)
    if len(loc) and (np.linalg.norm(loc - a, axis=1) < L).any() and (np.linalg.norm(loc - b, axis=1) < L).any():
        return False
    t = np.linspace(0.25, 1.0, max(2, int(L / 0.5)))
    pts = a + t[:, None] * v
    _, dist, _ = trimesh.proximity.closest_point(model, pts)
    return bool((dist >= clearance).all()) and not model.contains(pts[-1:])[0]


def plan_tree(model: trimesh.Trimesh, contacts: list[Contact], p: SupportParams):
    """Tree layout [GOOSLICER, 08 §8]. Tips and the straight-drop test are the EVO ones
    (`plan_pillars`). Tip ends that drop to the raft are merged pairwise, highest meeting point
    first: two branches meet where both can reach going down at most maxBranchAngle from
    vertical. A merge is taken only when neither branch drops more than maxHeight, both stay
    clear of the model, the joint stays at least 1 mm above the raft, and the joint can still
    drop straight to the raft with trunk clearance. Whatever is left drops as a vertical trunk.
    Returns (pillars, rejected, nodes, roots)."""
    import heapq
    pillars, rejected = plan_pillars(model, contacts, p)
    ground = [i for i, pl in enumerate(pillars) if not pl.on_model]
    nodes = [_Node(pillars[i].tip_end.copy()) for i in ground]
    tan_a = math.tan(math.radians(min(max(p.max_branch_angle, 1.0), 80.0)))
    base = p.base_z
    zmin_join = base + 1.0
    clearance = p.branch_width / 2 + 0.1
    reach = 2 * p.max_branch_height * tan_a
    active = set(range(len(nodes)))
    heap: list = []

    def push(i, j):
        m = _meet(nodes[i].pos, nodes[j].pos, tan_a)
        if m[2] < zmin_join:
            return
        if max(nodes[i].pos[2], nodes[j].pos[2]) - m[2] > p.max_branch_height + 1e-9:
            return
        heapq.heappush(heap, (-m[2], i, j, m))

    if len(nodes) >= 2:
        xy = np.array([nd.pos[:2] for nd in nodes])
        for i, j in cKDTree(xy).query_pairs(reach):
            push(i, j)
    down = np.array([[0.0, 0, -1]])
    while heap:
        _, i, j, m = heapq.heappop(heap)
        if i not in active or j not in active:
            continue
        a, b = nodes[i].pos, nodes[j].pos
        ok = all(_segment_clear(model, q, m, clearance) for q in (a, b) if np.linalg.norm(q - m) > 1e-6)
        if ok:              # the joint's trunk must reach the raft clear of the model
            loc, _, _ = model.ray.intersects_location(m[None] - [0, 0, 1e-3], down, multiple_hits=False)
            ok = not len(loc) and _segment_clear(model, m, np.array([m[0], m[1], base]),
                                                 p.pillar_bottom_width / 2 + 0.1)
        if not ok:
            continue
        k = len(nodes)
        nodes.append(_Node(m, [i, j], -1, nodes[i].weight + nodes[j].weight))
        nodes[i].parent = nodes[j].parent = k
        active -= {i, j}
        for o in active:
            if np.linalg.norm(nodes[o].pos[:2] - m[:2]) <= reach:
                push(k, o)
        active.add(k)
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
    body = m3.Manifold.batch_boolean(solids, m3.OpType.Add)
    return SupportResult(to_trimesh(body), pillars, rejected, len(braces), holes, trunks=len(roots))


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
                        max_area: float = ISLAND_MAX_AREA) -> list[Island]:
    """Regions that print in mid-air. The union of all meshes is rasterised at `pixel` mm per
    layer. The first layer rests on the plate and is grounded; a connected region of a later
    layer is grounded when it shares a pixel with grounded material of the layer below.
    Ungrounded regions are followed upward (they keep their identity while they overlap) and
    reported when they stay ungrounded for more than `max_hang` mm or reach `max_area` mm²."""
    from scipy import ndimage

    from .raster import RasterSpec, rasterize_bitmap
    mfs = []
    for m in meshes:
        try:
            mfs.append(to_manifold(m))
        except ValueError:
            continue
    if not mfs:
        return []
    body = m3.Manifold.batch_boolean(mfs, m3.OpType.Add)
    bb = body.bounding_box()
    x0, y0, zmin, x1, y1, zmax = (float(v) for v in bb)
    um = pixel * 1000
    spec = RasterSpec(int(math.ceil((x1 - x0) / pixel)) + 4, int(math.ceil((y1 - y0) / pixel)) + 4,
                      um, um, mirror_x=False, mirror_y=True, origin_x=x0 - 2 * pixel, origin_y=y0 - 2 * pixel)
    h = layer_height
    # layers are counted from the plate: the first layer rests on it, anything that first
    # appears higher up (a lifted part with nothing below) is an island
    z = first_layer if first_layer is not None else h / 2
    px2 = pixel ** 2
    min_px = max(1, int(min_area / px2))
    ground = None                   # grounded pixels of the previous layer
    ids = None                      # island id per ungrounded pixel of the previous layer (0 = none)
    found: list[Island] = []        # every ungrounded region, indexed by id - 1
    while z < zmax:
        polys = [np.asarray(q, float) for q in body.slice(z).to_polygons() if len(q) >= 3]
        cur = rasterize_bitmap(polys, spec) > 0 if polys else np.zeros((spec.res_y, spec.res_x), bool)
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
                        i = len(found)
                    else:
                        continue
                    isl = found[i - 1]
                    isl.area = max(isl.area, float(sz * px2))
                    isl.height = float(z - isl.z + h)
                    new_ids[m] = i
            ground, ids = np.isin(lab, g), new_ids
        z += h
        if z < zmin - h:            # skip empty air below the body (nothing to ground or track)
            n_skip = int((zmin - h - z) / h)
            z += n_skip * h
    return [i for i in found if i.height > max_hang + 1e-9 or i.area >= max_area]


# ------------------------------------------------------------------------- auto support
def auto_support(model: trimesh.Trimesh, p: SupportParams, extra: list[Contact] | None = None,
                 max_passes: int = 3) -> tuple[list[Contact], SupportResult, list[Island]]:
    """Detect contacts, build, then add a contact under every remaining floating island."""
    contacts = detect_contacts(model, p) + list(extra or [])
    res = build_supports(model, contacts, p)
    islands = []
    for _ in range(max_passes):
        meshes = [model] + ([res.mesh] if res.mesh is not None else [])
        islands = unsupported_islands(meshes, layer_height=0.1)
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
    loc, _, tri = model.ray.intersects_location(o, np.array([[0.0, 0, -1]]), multiple_hits=False)
    if not len(loc) or model.face_normals[tri[0]][2] >= 0:
        return None
    return Contact(loc[0], model.face_normals[tri[0]].copy(), ISLAND)


def contact_below(model: trimesh.Trimesh, point) -> Contact | None:
    """The model surface point straight below (or at) `point`, approached from underneath."""
    o = np.array([[point[0], point[1], model.bounds[0][2] - 1.0]])
    loc, _, tri = model.ray.intersects_location(o, np.array([[0.0, 0, 1]]), multiple_hits=False)
    if not len(loc):
        return None
    return Contact(loc[0], model.face_normals[tri[0]].copy(), MANUAL)


# ------------------------------------------------------------------------- per-part plan
@dataclass
class SupportPlan:
    """A part's supports: parameters plus contact points stored in the part's local mesh frame,
    so they follow the part when it is moved, rotated or scaled. Geometry is rebuilt from it."""
    params: SupportParams
    points: np.ndarray                  # (N, 3) local
    normals: np.ndarray                 # (N, 3) local, outward
    kinds: list[str]
    template: str = ""                  # name of the template the params came from

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


def build_for_part(part) -> SupportResult | None:
    """World-space support geometry for a part with a plan (None without one)."""
    plan = getattr(part, "supports", None)
    if plan is None:
        return None
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
