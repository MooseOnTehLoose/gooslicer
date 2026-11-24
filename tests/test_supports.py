import math
import os
from pathlib import Path

import numpy as np
import pytest
import trimesh

from gooslicer import supports as S
from gooslicer.scene import Part, rotation_matrix

ROOT = Path(__file__).parent.parent
INSTALL = Path(os.environ.get("GOOSLICER_SATELLITE_DIR", "/nonexistent"))


def _lifted(mesh, lift=5.0):
    m = mesh.copy()
    m.apply_translation([0, 0, lift - m.bounds[0][2]])
    return m


def _tee():
    return trimesh.boolean.union([trimesh.creation.box((4, 4, 6)),
                                  trimesh.creation.box((12, 4, 2), trimesh.transformations.translation_matrix((0, 0, 4)))],
                                 engine="manifold")


def test_defaults_equal_p4_spec():
    p = S.SupportParams()
    # 06-support-hollow-defaults-spec.md §1, EVO 30-60 column and raft row
    assert (p.critical_angle, p.anchor_dis, p.anchor_dis_border, p.border_offset) == (50, 2, 2, 0.5)
    assert (p.tip_top_width, p.tip_bottom_width, p.tip_length, p.tip_ball_diameter, p.tip_in_part) == (0.32, 0.6, 1, 0.35, 0.1)
    assert (p.pillar_top_width, p.pillar_bottom_width, p.poly_edges) == (0.7, 0.7, 16)
    assert (p.diamond_angle, p.diamond_width, p.edge_width) == (60, 7, 0.8)
    assert (p.raft_height, p.raft_offset, p.raft_slot_angle) == (0.5, 1, 60)
    assert (p.hole_radius, p.hole_interval, p.hole_edges, p.lift) == (1, 3, 12, 5)
    assert not p.plat_connect and p.inner_support
    assert S.SupportParams.from_dict(p.to_dict()) == p


@pytest.mark.skipif(not (INSTALL / "resources/script/common").is_dir(), reason="needs GOOSLICER_SATELLITE_DIR")
def test_params_from_real_templates():
    from gooslicer.support_defaults import load_templates
    ts = {t.name: t for t in load_templates(INSTALL / "resources/script/common")}
    assert S.SupportParams.from_template(ts["EVO_30-60mm"]) == S.SupportParams()
    p = S.SupportParams.from_template(ts["EVO_0-30mm"])
    assert (p.critical_angle, p.anchor_dis, p.tip_top_width, p.pillar_top_width) == (37, 1, 0.25, 0.8)
    assert p.plat_connect and (p.plat_connect_radius, p.plat_connect_height) == (pytest.approx(0.7), pytest.approx(0.3))


def test_tee_auto_support_no_islands_watertight():
    m = _lifted(_tee())
    p = S.SupportParams()
    assert len(S.unsupported_islands([m])) >= 1          # the lifted part itself floats
    contacts, res, islands = S.auto_support(m, p)
    assert islands == [] and res.mesh.is_watertight
    assert S.unsupported_islands([m, res.mesh]) == []
    # contacts under the post bottom and under both slab wings; spacing ≈ anchorDis
    pts = np.array([c.point for c in contacts])
    assert (pts[:, 0] < -2.5).any() and (pts[:, 0] > 2.5).any()
    z0 = m.bounds[0][2]                                  # post bottom; wing undersides 6 mm higher
    assert np.all(np.isclose(pts[:, 2], z0, atol=1e-6) | np.isclose(pts[:, 2], z0 + 6, atol=1e-6))
    # every tip end is below its contact; ground pillars stand on the raft top
    for pl in res.pillars:
        assert pl.tip_end[2] < pl.contact.point[2]
        if not pl.on_model:
            assert pl.foot[2] == pytest.approx(p.raft_height)
    lo, hi = res.mesh.bounds
    assert lo[2] == pytest.approx(0.0, abs=0.02)


def test_raft_outline_and_holes():
    p = S.SupportParams()
    big = _lifted(trimesh.creation.box((30, 30, 2)))
    _, res, islands = S.auto_support(big, p)
    assert islands == []
    ground = [pl for pl in res.pillars if not pl.on_model]
    outline = S.raft_outline(ground, p)
    feet = np.array([pl.foot[:2] for pl in ground])
    # convex hull of the feet grown by platOffset + pillar radius (arc.goo: within 1%)
    from shapely.geometry import MultiPoint
    assert outline.area == pytest.approx(MultiPoint(feet).convex_hull.buffer(1.35).area, rel=0.01)
    assert res.raft_holes > 0
    # raft is platHeight thick: the support body below z = 0.5 is just the raft
    import manifold3d as m3
    from gooslicer.hollow import to_manifold
    sec = to_manifold(res.mesh).slice(0.25)
    def signed_area(q):
        q = np.asarray(q)
        return 0.5 * float(np.sum(q[:, 0] * np.roll(q[:, 1], -1) - np.roll(q[:, 0], -1) * q[:, 1]))
    holes = [q for q in sec.to_polygons() if len(q) >= 3 and signed_area(q) < 0]
    assert len(holes) == res.raft_holes
    r = [np.linalg.norm(np.asarray(q) - (np.asarray(q).min(0) + np.asarray(q).max(0)) / 2, axis=1).max()
         for q in holes]
    assert np.allclose(r, p.hole_radius, atol=0.01)


def test_tight_gap_and_on_model_landing():
    # a shelf 0.6 mm above a lower step: tip (1 mm) cannot fit, a short link is used
    base = trimesh.creation.box((10, 10, 2))
    shelf = trimesh.creation.box((10, 10, 1), trimesh.transformations.translation_matrix((0, 0, 1.0 + 0.6 + 0.5)))
    post = trimesh.creation.box((2, 2, 3), trimesh.transformations.translation_matrix((-4, -4, 2.0)))
    m = _lifted(trimesh.boolean.union([base, shelf, post], engine="manifold"))
    _, res, islands = S.auto_support(m, S.SupportParams())
    assert islands == []
    assert any(pl.on_model and pl.tip_end[2] - pl.foot[2] < 1.0 for pl in res.pillars)


def test_plan_follows_part_transform():
    part = Part.from_mesh("t", _tee())
    part.position = np.array([3.0, -2.0, 7.0])
    part.rotation = rotation_matrix([0, 0, 1], 30)
    part.scale = np.array([1.0, 2.0, 1.0])
    cs = [S.Contact(np.array([1.0, 2.0, 3.0]), np.array([0.0, 0.6, -0.8]))]
    plan = S.SupportPlan.from_world(part, cs, S.SupportParams())
    back = plan.world_contacts(part)[0]
    assert np.allclose(back.point, cs[0].point) and np.allclose(back.normal, cs[0].normal)
    part.position = part.position + [5, 0, 0]
    assert np.allclose(plan.world_contacts(part)[0].point, cs[0].point + [5, 0, 0])


def test_unsupported_island_detection():
    a = trimesh.creation.box((4, 4, 4), trimesh.transformations.translation_matrix((0, 0, 2)))
    b = trimesh.creation.box((4, 4, 2), trimesh.transformations.translation_matrix((10, 0, 6)))
    isl = S.unsupported_islands([a, b])
    assert len(isl) == 1 and isl[0].z == pytest.approx(5.025, abs=0.06)
    assert abs(isl[0].point[0] - 10) < 2 and isl[0].area == pytest.approx(16, rel=0.1)


@pytest.mark.skipif(not (ROOT / "samples/arc-1a-attitude-4.stl").exists(), reason="reference sample missing")
def test_arc_reference_density():
    """arc.goo (SatelLite, EVO_30-60mm): 156 pillars on the raft, 0.7 mm pillars, 0.8 mm braces
    at 60°. GooSlicer's open algorithm should land in the same range on the same STL."""
    p = S.SupportParams()
    arc = trimesh.load(ROOT / "samples/arc-1a-attitude-4.stl")
    arc.apply_translation([-arc.bounds.mean(0)[0], -arc.bounds.mean(0)[1], p.lift - arc.bounds[0][2]])
    _, res, islands = S.auto_support(arc, p)
    assert islands == [] and res.mesh.is_watertight
    ground = sum(not pl.on_model for pl in res.pillars)
    assert 110 <= ground <= 200


def test_island_thresholds():
    """A speck that hangs ≤ 0.15 mm and stays under 5 mm² is ignored (raster artefact, 08 §5);
    a longer or larger one is reported with how far it hangs."""
    base = trimesh.creation.box((4, 4, 2), trimesh.transformations.translation_matrix((0, 0, 1)))
    def floater(sx, sy, sz):
        return trimesh.creation.box((sx, sy, sz), trimesh.transformations.translation_matrix((8, 0, 3 + sz / 2)))
    assert S.unsupported_islands([base, floater(0.5, 0.5, 0.1)]) == []
    isl = S.unsupported_islands([base, floater(0.5, 0.5, 0.5)])
    assert len(isl) == 1 and isl[0].height == pytest.approx(0.5, abs=0.06) and isl[0].area == pytest.approx(0.25, rel=0.3)
    isl = S.unsupported_islands([base, floater(3, 3, 0.1)])        # large: reported even when thin
    assert len(isl) == 1 and isl[0].area == pytest.approx(9, rel=0.15)


@pytest.mark.skipif(not (INSTALL / "resources/script/common").is_dir(), reason="needs GOOSLICER_SATELLITE_DIR")
def test_tree_params_from_real_templates():
    from gooslicer.support_defaults import load_templates
    ts = {t.name: t for t in load_templates(INSTALL / "resources/script/common")}
    assert S.SupportParams.from_template(ts["Tree_Medium"]) == S.SupportParams.tree_defaults()
    p = S.SupportParams.from_template(ts["Tree_Small"])
    assert p.style == "tree" and (p.critical_angle, p.anchor_dis, p.tip_top_width, p.tip_in_part) == (45, 2.5, 0.4, 0.1)
    assert (p.raft_offset, p.cross_start, p.cross_interval, p.cross_width) == (2, 3, 5, 0.3)


def _check_tree(model, p):
    contacts, res, islands = S.auto_support(model, p)
    assert islands == [] and res.mesh is not None and res.mesh.is_watertight
    pls, _, nodes, roots = S.plan_tree(model, contacts, p)
    tan = math.tan(math.radians(p.max_branch_angle))
    for nd in nodes:
        if nd.parent >= 0:
            a, b = nd.pos, nodes[nd.parent].pos
            drop = a[2] - b[2]
            assert -1e-9 <= drop <= p.max_branch_height + 1e-6
            assert np.linalg.norm(a[:2] - b[:2]) <= drop * tan + 1e-6
    for pl in pls:
        if not pl.on_model:
            assert pl.path[-1][2] == pytest.approx(p.base_z) and np.allclose(pl.path[0], pl.tip_end)
    return contacts, res


def test_tree_tee_merges_branches():
    p = S.SupportParams.tree_defaults()
    assert S.SupportParams.from_dict(p.to_dict()) == p
    contacts, res = _check_tree(_lifted(_tee()), p)
    ground = sum(not pl.on_model for pl in res.pillars)
    assert ground >= 8 and 1 <= res.trunks < ground / 2
    # picking follows the branch path
    pl = next(pl for pl in res.pillars if pl.path and len(pl.path) > 2)
    mid = (pl.path[0] + pl.path[1]) / 2
    assert S.nearest_pillar(res.pillars, mid, 0.05) is not None


@pytest.mark.skipif(not (ROOT / "samples/arc-1a-attitude-4.stl").exists(), reason="reference sample missing")
def test_tree_arc_reference():
    p = S.SupportParams.tree_defaults()
    arc = trimesh.load(ROOT / "samples/arc-1a-attitude-4.stl")
    arc.apply_translation([-arc.bounds.mean(0)[0], -arc.bounds.mean(0)[1], p.lift - arc.bounds[0][2]])
    _, res = _check_tree(arc, p)
    ground = sum(not pl.on_model for pl in res.pillars)
    assert res.trunks < ground / 2 and res.braces > 0
    assert S.unsupported_islands([arc, res.mesh], 0.05) == []


def test_island_faces_mark_only_the_floating_underside():
    import trimesh
    from gooslicer import supports as S
    ground = trimesh.creation.box((10, 10, 2))
    ground.apply_translation((0, 0, 1))
    floating = trimesh.creation.box((4, 4, 2))
    floating.apply_translation((12, 3, 4))
    isl = S.unsupported_islands([ground, floating])
    assert len(isl) == 1 and isl[0].footprint is not None
    x0, y0, px, mask = isl[0].footprint
    assert abs(mask.sum() * px * px - 16) < 1.5                 # the 4 x 4 mm footprint
    sel_ground, sel_float = S.island_faces([ground, floating], isl)
    assert not sel_ground.any()
    assert sel_float.sum() == 2                                   # the two underside triangles
    assert np.allclose(floating.triangles_center[sel_float][:, 2], 3.0)
    # a hanging ball: its whole underside hangs, the top half does not count
    ball = trimesh.creation.icosphere(3, radius=5)
    ball.apply_translation((0, 0, 8))
    (sel,) = S.island_faces([ball], S.unsupported_islands([ball]))
    assert sel.any() and (ball.face_normals[sel][:, 2] < 0).all()


def test_island_faces_each_keeps_islands_apart():
    import trimesh
    from gooslicer import supports as S
    a = trimesh.creation.box((4, 4, 2))
    a.apply_translation((-10, 0, 4))
    b = trimesh.creation.box((4, 4, 2))
    b.apply_translation((10, 0, 6))
    isl = S.unsupported_islands([a, b])
    assert len(isl) == 2
    each = S.island_faces_each([a, b], isl)
    assert len(each) == 2
    for per_mesh in each:                          # each island marks one box's underside only
        assert sum(int(m.any()) for m in per_mesh) == 1
    combined = S.island_faces([a, b], isl)
    for k in range(2):
        assert (combined[k] == (each[0][k] | each[1][k])).all()
