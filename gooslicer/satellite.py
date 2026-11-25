"""Read SatelLite projects (`.goo.3mf`, 10) into a GooSlicer Project (Qt-free).

What comes in, per part: its mesh where SatelLite placed it (plate corner → GooSlicer's centred
plate), and, when it has supports, a SupportPlan built from SatelLite's contacts that also carries
SatelLite's own support mesh as FixedSupports. That mesh is sliced as it is until the part is
turned, scaled, lifted, edited or its supports changed; GooSlicer then builds supports from the
same contacts. The printer and the active resin's settings come from info.json.

Like project.load, no XML parser runs on the package: the model is read with plain searches,
and every count in the binary support file is checked against the bytes that are there.
"""
from __future__ import annotations

import json
import os
import struct
import zipfile
from pathlib import Path

import numpy as np
import trimesh

from . import project as projmod
from .scene import Part

INFO = "Metadata/info.json"
MODEL = "3D/3dmodel.model"
SUPPORT_MAGIC = 0xA0B0C0D0
CONTACT, OVERHANG_T, LOWEST_T = 1, 2, 3
_NODE = np.dtype([("p", "<f4", 3), ("d", "<f4", 3), ("id", "<i4"), ("r", "<f4"), ("role", "<u4"),
                  ("pad", "<u2"), ("face", "<i4"), ("type", "<u2")])

# SmartSupportBlock parameters (10 §5): the order and types SatelLite writes them in
# (EleTriAddExpand FUN_180045cb0, "SUPPORTVARIABLESMART"), named by the struct offset they come from.
_SMART = ("f58 f5c f60 f64 i74 f78 f7c f80 b8c f84 f94 f98 b9c fa0 fa4 ba8 bb0 fb4 fb8 fbc bc0 ic4 fe0 fe4 fe8 iec if0 "
          "ff4 ff8 ifc i100 i104 i108 b10c b10d b10e bd0 b110 f114 b118 f11c id4 i88 f68 f6c b70 fac f90 f18 b2c "
          "f28 f24 f1c f20 b30 f34 fd8 fdc fc8 bcc").split()
# struct offset -> SupportParams field [INFERRED from EVO_30-60mm's values and the struct layout, 10 §5]
_SMART_FIELDS = {
    "f18": "critical_angle", "f58": "anchor_dis", "f5c": "anchor_dis_border", "f60": "border_offset",
    "f68": "lowest_interval", "b70": "reinforce_lowest", "f78": "tip_in_part", "f7c": "tip_top_width",
    "f80": "tip_bottom_width", "f84": "tip_length", "b8c": "tip_ball", "f90": "tip_ball_diameter",
    "fa0": "bot_in_part", "fa4": "bot_top_width", "bb0": "plat_connect", "fb4": "plat_connect_height",
    "fb8": "plat_connect_radius", "fd8": "pillar_top_width", "fdc": "pillar_bottom_width",
    "fe0": "diamond_angle", "fe4": "diamond_width", "fe8": "edge_width", "if0": "poly_edges",
}
# BasePlatBlock (raft) parameters at the end of the file [INFERRED from values]
_RAFT = (("b", "raft"), ("f", "raft_offset"), ("f", "raft_height"), ("f", "raft_slot_angle"), ("i", None),
         ("b", "raft_holes"), ("f", "hole_radius"), ("f", "hole_interval"), ("i", "hole_edges"))

# ActiveScript key -> PrintSettings field (the .cfgx names, 10 §3)
ACTIVE_SCRIPT = {
    "Scirptname": "resin_name", "layerthickness": "layer_height", "bottomLayerCount": "bottom_layers",
    "exposureTime": "exposure", "bottomExposureTime": "bottom_exposure",
    "tanslate_layer_count": "transition_layers", "lightoffDelay": "light_off_delay",
    "bottomLightoffDelay": "bottom_light_off_delay", "waitaftercure": "wait_after_cure",
    "bottomwaittimebeforelift": "bottom_wait_before_lift", "bottomwaittimeafterlift": "bottom_wait_after_lift",
    "bottomwaittimeafterretract": "bottom_wait_after_retract", "bottomLiftDis": "bottom_lift_distance",
    "liftingDis": "lift_distance", "bottomLiftSpeed": "bottom_lift_speed", "liftingSpeed": "lift_speed",
    "bottom_retract_speed": "bottom_retract_speed", "backSpeed": "retract_speed",
    "slowbottomliftdis": "slow_bottom_lift_distance", "slowliftdis": "slow_lift_distance",
    "slowbottombackdis": "slow_bottom_retract_distance", "slowbackdis": "slow_retract_distance",
    "slowbottomliftspeed": "slow_bottom_lift_speed", "slowliftspeed": "slow_lift_speed",
    "slowbottombackspeed": "slow_bottom_retract_speed", "slowbackspeed": "slow_retract_speed",
    "bottom_light_pwm": "bottom_light_pwm", "light_pwm": "light_pwm",
    "resineDensity": "density", "resinCost": "cost",
}


class SatelLiteError(projmod.ProjectError):
    pass


def is_satellite_project(path: str | os.PathLike) -> bool:
    """A SatelLite project: a 3MF whose info.json says ProjectFormat goo.3mf."""
    try:
        with zipfile.ZipFile(path) as z:
            if INFO not in z.namelist() or z.getinfo(INFO).file_size > 16 << 20:
                return False
            return json.loads(z.read(INFO)).get("ProjectFormat") == "goo.3mf"
    except (OSError, zipfile.BadZipFile, ValueError, AttributeError):
        return False


def print_settings(script: dict) -> dict:
    """ActiveScript -> PrintSettings fields (values cleaned as resin profiles are)."""
    from .settings import _coerce
    out = {}
    for k, f in ACTIVE_SCRIPT.items():
        if k in script and script[k] is not None:
            try:
                out[f] = _coerce(f, script[k])
            except (TypeError, ValueError):
                continue
    return out


# --------------------------------------------------------------------------- model
def _model_meshes(model: bytes) -> dict[int, tuple[str, trimesh.Trimesh]]:
    out = {}
    for a, start, end in projmod._objects(model):
        oid = int(a.get("id", -1))
        v = projmod._numbers(model, start, end, b"vertices", 3, np.float64)
        f = projmod._numbers(model, start, end, b"triangles", 3, np.int64)
        if not len(f):
            continue
        if f.min() < 0 or f.max() >= len(v):
            raise SatelLiteError(f"object {oid} refers to vertices it doesn't have")
        out[oid] = (a.get("name", f"part {oid}"), trimesh.Trimesh(v, f, process=False))
    return out


# --------------------------------------------------------------------------- supports
class _Reader:
    def __init__(self, data: bytes):
        self.b, self.o = data, 0

    def take(self, n: int) -> bytes:
        if n < 0 or self.o + n > len(self.b):
            raise SatelLiteError("the support file ends early")
        out = self.b[self.o:self.o + n]
        self.o += n
        return out

    def u8(self) -> int:
        return self.take(1)[0]

    def u32(self) -> int:
        return struct.unpack("<I", self.take(4))[0]

    def array(self, dtype, count: int) -> np.ndarray:
        dt = np.dtype(dtype)
        return np.frombuffer(self.take(dt.itemsize * count), dt).copy()

    def line(self) -> bytes:
        end = self.b.find(b"\n", self.o, self.o + 256)
        if end < 0:
            raise SatelLiteError("a block name in the support file isn't closed")
        return self.take(end + 1 - self.o)[:-1]


def _linked(r: _Reader, n_nodes: int) -> np.ndarray | None:  # leaves r after the links
    """Which points of the first array have a support built from them: the point indices named by
    the link records after Support::SupportSmartEntity (10 §5). None if that part can't be read."""
    try:
        r.array(_NODE, r.u32())                         # the second point array (on the plate)
        r.u32()
        if r.line() != b"Support::SupportSmartEntity":
            return None
        r.u32()
        pieces = r.u32()
        for _ in range(pieces):
            a, b = r.u32(), r.u32()
            r.take(4 * (a + b))
        links = r.array("<i4", 4 * pieces).reshape(-1, 4)
    except SatelLiteError:
        return None
    ends = links[:, :2]
    if len(ends) and (ends.min() < 0 or ends.max() >= n_nodes):
        return None
    used = np.zeros(n_nodes, bool)
    used[ends.ravel()] = True
    return used


def _values(r: _Reader, spec) -> list:
    out = []
    for t in spec:
        if t == "b":
            out.append(bool(r.u8()))
        elif t == "i":
            out.append(struct.unpack("<i", r.take(4))[0])
        else:
            out.append(round(struct.unpack("<f", r.take(4))[0], 4))   # float32: 0.32 -> 0.31999…
    return out


def _params(r: _Reader) -> dict:
    """SupportParams fields from the parameter section after the links; {} if it isn't the EVO
    (smart) layout or anything doesn't check out."""
    try:
        vals = _values(r, [k[0] for k in _SMART])
        if r.take(1) != b"\n":
            return {}
        out = {_SMART_FIELDS[k]: v for k, v in zip(_SMART, vals) if k in _SMART_FIELDS}
        at = r.b.find(b"Support::BasePlatBlock\n", r.o)
        if at >= 0:
            r.o = at + len(b"Support::BasePlatBlock\n")
            r.take(12)
            a, b = r.u32(), r.u32()
            r.take(4 * (a + b))
            for (_, name), v in zip(_RAFT, _values(r, [t for t, _ in _RAFT])):
                if name:
                    out[name] = v
    except SatelLiteError:
        return {}
    ok = (3 <= out.get("poly_edges", 0) <= 64 and 0 < out.get("critical_angle", 0) < 90
          and all(0 < out[k] < 20 for k in ("tip_top_width", "tip_bottom_width", "tip_length",
                                            "pillar_top_width", "pillar_bottom_width")))
    if "raft" in out and not (0 < out["raft_height"] < 10 and 3 <= out["hole_edges"] <= 64):
        for _, name in _RAFT:
            out.pop(name, None)
    return out if ok else {}


def read_supports(data: bytes) -> tuple[trimesh.Trimesh, np.ndarray, dict]:
    """SupportKernelEx.bms (10 §5): the support mesh (plate frame); the first point array, with a
    `linked` flag (False for points SatelLite left without a support); and the support settings
    as SupportParams fields ({} when they can't be read)."""
    r = _Reader(data)
    if r.u32() != SUPPORT_MAGIC:
        raise SatelLiteError("not a SatelLite support file")
    r.u32()                                             # 0x00010000
    if not r.line().startswith(b"MESH-"):
        raise SatelLiteError("unexpected support file header")
    r.take(8)
    r.u8()
    nv = r.u32()
    v = r.array("<f4", nv * 3).reshape(-1, 3).astype(np.float64)
    nf = r.u32()
    f = r.array("<i4", nf * 3).reshape(-1, 3).astype(np.int64)
    if len(f) and (f.min() < 0 or f.max() >= nv):
        raise SatelLiteError("the support mesh refers to vertices it doesn't have")
    r.take(4 * (6 + 16))                                # bounding box, matrix
    r.u8()
    blocks = r.u32()
    if r.line() != b"Support::SmartSupportBlock" or blocks < 1:
        raise SatelLiteError("the support points are missing")
    nodes = r.array(_NODE, r.u32())
    linked = _linked(r, len(nodes)) if blocks >= 2 else None
    out = np.zeros(len(nodes), _NODE.descr + [("linked", "?")])
    for k in _NODE.names:
        out[k] = nodes[k]
    out["linked"] = True if linked is None else linked
    params = _params(r) if linked is not None else {}
    return trimesh.Trimesh(v, f, process=False), out, params


def _plan_for(part: Part, model_world: trimesh.Trimesh, nodes: np.ndarray, support_mesh: trimesh.Trimesh,
              settings: dict | None = None):
    from .supports import LOWEST, OVERHANG, FixedSupports, SupportParams, SupportPlan
    # contacts SatelLite built a support from; the rest it left unsupported (10 §5)
    c = nodes[(nodes["role"] == CONTACT) & nodes["linked"] & (nodes["face"] >= 0)
              & (nodes["face"] < len(model_world.faces))]
    normals = model_world.face_normals[c["face"]]           # outward normals, as GooSlicer's plans hold
    kinds = [LOWEST if t == LOWEST_T else OVERHANG for t in c["type"]]
    pts = (c["p"].astype(np.float64) - part.position) @ part.rotation / part.scale
    nl = normals @ part.rotation * part.scale
    nl /= np.maximum(np.linalg.norm(nl, axis=1), 1e-12)[:, None]
    params = SupportParams(**{**(settings or {}), "lift": float(part.elevation)})
    fixed = FixedSupports(support_mesh, part.mesh, part.position.copy(), part.rotation.copy(), part.scale.copy())
    return SupportPlan(params, pts, nl, kinds, "SatelLite project", fixed=fixed)


# --------------------------------------------------------------------------- project
def load(path: str | os.PathLike, progress=None) -> projmod.Project:
    path = Path(path)
    try:
        with zipfile.ZipFile(path) as z:
            try:
                info = json.loads(projmod._entry(z, INFO))
            except (ValueError, UnicodeDecodeError) as e:
                raise SatelLiteError(f"{INFO} is not valid JSON: {e}") from e
            if not isinstance(info, dict) or info.get("ProjectFormat") != "goo.3mf":
                raise SatelLiteError(f"{path.name} is not a SatelLite project")
            model = projmod._entry(z, MODEL)
            names = set(z.namelist())
            support_data = {}
            for o in info.get("Objects", []):
                att = str(o.get("SupportAttachment") or "").lstrip("/")
                if o.get("HasSupport") and att in names:
                    support_data[att] = projmod._entry(z, att)
    except zipfile.BadZipFile as e:
        raise SatelLiteError(f"{path.name} is damaged or not a 3MF file: {e}") from e
    except OSError as e:
        raise SatelLiteError(f"cannot read {path.name}: {e.strerror or e}") from e
    try:
        return _build(info, model, support_data, progress)
    except projmod.ProjectError:
        raise
    except (KeyError, IndexError, TypeError, ValueError, struct.error) as e:
        raise SatelLiteError(f"{path.name} is damaged: {type(e).__name__}: {e}") from e


def _build(info: dict, model: bytes, support_data: dict, progress) -> projmod.Project:
    plat = info.get("PlatformInfo", {}).get("Platform", {})
    sx, sy = float(plat.get("X", 0) or 0), float(plat.get("Y", 0) or 0)
    shift = np.array([-sx / 2, -sy / 2, 0.0])               # corner origin -> GooSlicer's centre (10 §2)
    meshes = _model_meshes(model)
    objects = info.get("Objects", [])
    parts = []
    for i, (oid, (name, mesh)) in enumerate(sorted(meshes.items())):
        world = mesh.copy()
        world.apply_translation(shift)
        meta = objects[i] if i < len(objects) else {}
        part = Part.from_mesh(str(meta.get("Name") or name), world)
        att = str(meta.get("SupportAttachment") or "").lstrip("/")
        if meta.get("HasSupport") and att in support_data:
            sup, nodes, settings = read_supports(support_data[att])
            sup.apply_translation(shift)
            nodes = nodes.copy()
            nodes["p"] += shift.astype(np.float32)
            part.elevation = float(world.bounds[0][2])
            part.supports = _plan_for(part, world, nodes, sup, settings)
        parts.append(part)
        if progress:
            progress((i + 1) / max(len(meshes), 1))
    script = info.get("ActiveScript") or {}
    printer = (info.get("SatelLiteMachine", {}).get("PrinterType") or info.get("PlatformInfo", {}).get("PlatName")
               or script.get("ExtraResinPara_PrinterName"))
    return projmod.Project(parts, printer=printer, print_settings=print_settings(script) or None,
                           app=f"SatelLite ({info.get('ProjectFormat')} v{info.get('ProjectFormatVersion')})",
                           source="SatelLite")
