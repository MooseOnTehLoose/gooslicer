"""SatelLite projects (.goo.3mf, 10): a small file written here in SatelLite's layout, and the real
sample when it is present (samples/ is not in git)."""
import json
import struct
import zipfile
from pathlib import Path

import numpy as np
import pytest
import trimesh

from gooslicer import project as P
from gooslicer import satellite as SAT
from gooslicer import supports as S

PLATE = (211.68, 118.37)
SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "arc-1a-attitude-4.goo.3mf"


# the sample's values by struct offset (10 §5); tests change a few
SMART_VALUES = dict(zip(SAT._SMART, [
    2.0, 2.0, 0.5, 2.0, 2, 0.1, 0.32, 0.6, 1, 1.0, 1.5, 5.0, 0, 0.1, 0.3, 1, 0, 2.0, 4.0, 90.0, 1, 0, 60.0, 7.0, 0.8,
    2, 16, 0.0, 0.0, 1, 1, 4, 1, 0, 0, 1, 1, 1, 8.0, 1, 0.6, 0, 1, 2.0, 1.2, 1, 0.35, 0.35, 50.0, 1, 99999.0, 0.0,
    0.0, 99999.0, 0, 10.0, 0.7, 0.7, 6.0, 0]))
RAFT_VALUES = [1, 1.0, 0.5, 60.0, 1, 1, 1.0, 3.0, 12]


def _pack(kinds, values) -> bytes:
    return b"".join(struct.pack("<B" if k == "b" else "<i" if k == "i" else "<f", v) for k, v in zip(kinds, values))


def _params(smart: dict, raft: list) -> bytes:
    out = _pack([k[0] for k in SAT._SMART], [smart[k] for k in SAT._SMART]) + b"\n"
    out += bytes(40) + b"Support::BasePlatBlock\n" + struct.pack("<5I", 1, 4, 1, 1, 1) + bytes(8)
    return out + _pack([t for t, _ in SAT._RAFT], raft) + bytes(16)


def _support_file(sup: trimesh.Trimesh, nodes: list[tuple], links: list[tuple] | None = None,
                  params: bytes = b"") -> bytes:
    out = bytearray(struct.pack("<II", SAT.SUPPORT_MAGIC, 0x00010000))
    out += (b"MESH-" * 51)[:255] + b"\n"
    out += bytes(8) + b"\x01" + struct.pack("<I", len(sup.vertices))
    out += np.asarray(sup.vertices, "<f4").tobytes()
    out += struct.pack("<I", len(sup.faces)) + np.asarray(sup.faces, "<i4").tobytes()
    out += np.zeros(6, "<f4").tobytes() + np.eye(4, dtype="<f4").tobytes()
    out += b"\x01" + struct.pack("<I", 2) + b"Support::SmartSupportBlock\n"
    arr = np.zeros(len(nodes), SAT._NODE)
    for i, (p, role, face, typ) in enumerate(nodes):
        arr[i] = (p, (0, 0, -1), i, 0.1, role, 0, face, typ)
    out += struct.pack("<I", len(arr)) + arr.tobytes() + struct.pack("<I", 0)
    if links is not None:                 # one empty piece per link, then the links (10 §5)
        out += struct.pack("<I", 1) + b"Support::SupportSmartEntity\n" + struct.pack("<II", 4, len(links))
        out += struct.pack("<II", 0, 0) * len(links)
        out += np.asarray(links, "<i4").tobytes()
        out += params or np.zeros(8, "<f4").tobytes()  # the parameter section
    return bytes(out)


def make_project(path, *, supports=True, unsupported=False, params: bytes = b"") -> trimesh.Trimesh:
    """A 10 mm cube at plate-corner (100, 50), lifted 5 mm, with two contacts on its underside;
    `unsupported` adds a third that SatelLite left without a support (no link names it)."""
    cube = trimesh.creation.box((10, 10, 10))
    cube.apply_translation([100, 50, 10])                     # z 5..15
    down = [i for i, n in enumerate(cube.face_normals) if n[2] < -0.9]
    sup = trimesh.creation.box((8, 8, 5))
    sup.apply_translation([100, 50, 2.5])
    verts = "".join(f'<vertex x="{x}" y="{y}" z="{z}"/>' for x, y, z in cube.vertices)
    tris = "".join(f'<triangle v1="{a}" v2="{b}" v3="{c}"/>' for a, b, c in cube.faces)
    model = (f'<?xml version="1.0"?><model unit="millimeter"><resources><object id="1" type="model" '
             f'name="cube"><mesh><vertices>{verts}</vertices><triangles>{tris}</triangles></mesh></object>'
             f'</resources><build><item objectid="1"/></build></model>')
    info = {"ProjectFormat": "goo.3mf", "ProjectFormatVersion": 1,
            "PlatformInfo": {"Platform": {"X": PLATE[0], "Y": PLATE[1], "Z": 220}},
            "SatelLiteMachine": {"PrinterType": "ELEGOO Saturn 4 Ultra 16K"},
            "Objects": [{"Name": "cube", "HasSupport": supports,
                         "SupportAttachment": "/Metadata/SupportKernelEx.bms"}],
            "ActiveScript": {"Scirptname": "Test resin", "layerthickness": 0.05000000074505806,
                             "exposureTime": 1.7999999523162842, "bottomLayerCount": 5, "resineDensity": 1.15}}
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("3D/3dmodel.model", model)
        z.writestr("Metadata/info.json", json.dumps(info))
        if supports:
            z.writestr("Metadata/SupportKernelEx.bms", _support_file(sup, [
                ((98, 48, 5), SAT.CONTACT, down[0], SAT.LOWEST_T),
                ((102, 52, 5), SAT.CONTACT, down[1], SAT.OVERHANG_T),
                ((100, 50, 0), 3, -1, 2)]                     # on the plate: not a contact
                + ([((101, 51, 5), SAT.CONTACT, down[0], SAT.OVERHANG_T)] if unsupported else []),
                [(2, 0, 0, 0), (2, 1, 0, 1)] if unsupported or params else None, params))
    return sup


def test_reads_parts_settings_and_supports(tmp_path):
    f = tmp_path / "cube.goo.3mf"
    sup = make_project(f)
    assert P.is_project(f) and not P.is_gooslicer_project(f)
    proj = P.load(f)
    assert proj.source == "SatelLite" and proj.printer == "ELEGOO Saturn 4 Ultra 16K"
    assert proj.print_settings == {"resin_name": "Test resin", "layer_height": 0.05, "exposure": 1.8,
                                   "bottom_layers": 5, "density": 1.15}
    (part,) = proj.parts
    # plate corner -> GooSlicer's centred plate
    assert np.allclose(part.bounds(), [[100 - 5 - PLATE[0] / 2, 50 - 5 - PLATE[1] / 2, 5],
                                       [100 + 5 - PLATE[0] / 2, 50 + 5 - PLATE[1] / 2, 15]])
    plan = part.supports
    assert part.elevation == 5 and plan.params.lift == 5 and len(plan) == 2
    assert plan.kinds == [S.LOWEST, S.OVERHANG]
    w = plan.world_contacts(part)
    assert np.allclose(w[0].point, [98 - PLATE[0] / 2, 48 - PLATE[1] / 2, 5])
    assert np.allclose(w[0].normal, [0, 0, -1])
    # SatelLite's own mesh is what gets sliced, shifted with the part
    r = S.build_for_part(part)
    assert r.pillars == [] and len(r.mesh.faces) == len(sup.faces)
    assert np.allclose(r.mesh.bounds[0], [96 - PLATE[0] / 2, 46 - PLATE[1] / 2, 0])
    part.position[:2] += [7, -3]
    assert np.allclose(S.build_for_part(part).mesh.bounds[0], [103 - PLATE[0] / 2, 43 - PLATE[1] / 2, 0])


@pytest.mark.parametrize("change", ["lift", "turn", "scale", "edit"])
def test_changes_hand_over_to_gooslicer(tmp_path, change):
    f = tmp_path / "cube.goo.3mf"
    make_project(f)
    part = P.load(f).parts[0]
    if change == "lift":
        part.position[2] += 1
    elif change == "turn":
        part.rotation = trimesh.transformations.rotation_matrix(0.3, [0, 0, 1])[:3, :3] @ part.rotation
    elif change == "scale":
        part.scale = part.scale * 1.1
    else:
        part.supports = part.supports.with_contacts(part, part.supports.world_contacts(part)[:1])
    assert S.fixed_result(part) is None
    r = S.build_for_part(part)
    assert r.pillars                                          # GooSlicer's own, from the same points


def test_gooslicer_save_keeps_the_exact_supports(tmp_path):
    f = tmp_path / "cube.goo.3mf"
    sup = make_project(f)
    proj = P.load(f)
    part = proj.parts[0]
    part.position[:2] += [4, 4]
    out = tmp_path / "saved.3mf"
    P.save(out, proj, support_meshes={part.id: S.build_for_part(part).mesh})
    back = P.load(out)
    assert back.source == "" and P.is_gooslicer_project(out)
    q = back.parts[0]
    assert q.supports.fixed is not None and q.supports.fixed.source == "SatelLite"
    r = S.build_for_part(q)
    assert r.pillars == [] and len(r.mesh.faces) == len(sup.faces)
    assert np.allclose(r.mesh.bounds[0], [100 - PLATE[0] / 2, 50 - PLATE[1] / 2, 0])
    # once GooSlicer builds them, a save stores only the points
    q.position[2] += 1
    P.save(out, back, support_meshes={q.id: S.build_for_part(q).mesh})
    assert P.load(out).parts[0].supports.fixed is None


def test_no_supports_and_damaged_files(tmp_path):
    f = tmp_path / "plain.goo.3mf"
    make_project(f, supports=False)
    assert P.load(f).parts[0].supports is None
    bad = tmp_path / "bad.goo.3mf"
    make_project(bad)
    with zipfile.ZipFile(bad) as z:
        entries = {n: z.read(n) for n in z.namelist()}
    entries["Metadata/SupportKernelEx.bms"] = entries["Metadata/SupportKernelEx.bms"][:300]
    with zipfile.ZipFile(bad, "w") as z:
        for n, b in entries.items():
            z.writestr(n, b)
    with pytest.raises(P.ProjectError, match="ends early"):
        P.load(bad)


@pytest.mark.skipif(not SAMPLE.exists(), reason="samples/ is not in git")
def test_real_sample():
    proj = P.load(SAMPLE)
    (part,) = proj.parts
    assert proj.printer == "ELEGOO Saturn 4 Ultra 16K"
    assert proj.print_settings["resin_name"] == "Space Grey 8K ABS-Like 3.0"
    assert len(part.mesh.faces) == 40074 and part.bounds()[0][2] == pytest.approx(5.0, abs=1e-4)
    assert len(part.supports) == 370 and part.supports.kinds.count(S.LOWEST) == 70   # 130 left unsupported
    assert len(S.build_for_part(part).mesh.faces) == 1205902


def test_points_satellite_left_unsupported_are_dropped(tmp_path):
    f = tmp_path / "cube.goo.3mf"
    make_project(f, unsupported=True)
    plan = P.load(f).parts[0].supports
    assert len(plan) == 2 and plan.kinds == [S.LOWEST, S.OVERHANG]


def test_support_settings_come_with_the_project(tmp_path):
    smart = {**SMART_VALUES, "f7c": 0.45, "f84": 2.0, "if0": 12, "f18": 37.0, "fd8": 1.1, "fdc": 1.3, "b8c": 0}
    raft = list(RAFT_VALUES)
    raft[2] = 0.3                                              # raft height
    f = tmp_path / "cube.goo.3mf"
    make_project(f, params=_params(smart, raft))
    p = P.load(f).parts[0].supports.params
    assert (p.tip_top_width, p.tip_length, p.poly_edges, p.critical_angle) == (0.45, 2.0, 12, 37.0)
    assert (p.pillar_top_width, p.pillar_bottom_width, p.tip_ball) == (1.1, 1.3, False)
    assert p.raft_height == 0.3 and p.raft_offset == 1.0 and p.hole_edges == 12
    assert p.lift == 5.0                                       # the part's own lift, not a setting
    # a section that doesn't check out leaves GooSlicer's defaults
    bad = tmp_path / "bad.goo.3mf"
    make_project(bad, params=_params({**smart, "if0": 999}, raft))
    assert P.load(bad).parts[0].supports.params.tip_top_width == S.SupportParams().tip_top_width
