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


def _support_file(sup: trimesh.Trimesh, nodes: list[tuple]) -> bytes:
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
    return bytes(out)


def make_project(path, *, supports=True) -> trimesh.Trimesh:
    """A 10 mm cube at plate-corner (100, 50), lifted 5 mm, with two contacts on its underside."""
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
                ((100, 50, 0), 3, -1, 2)]))                   # on the plate: not a contact
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
    assert len(part.supports) == 500 and part.supports.kinds.count(S.LOWEST) == 70
    assert len(S.build_for_part(part).mesh.faces) == 1205902
