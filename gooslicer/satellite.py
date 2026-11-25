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


def read_supports(data: bytes) -> tuple[trimesh.Trimesh, np.ndarray]:
    """SupportKernelEx.bms (10 §5): the support mesh (plate frame) and the first point array."""
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
    return trimesh.Trimesh(v, f, process=False), nodes


def _plan_for(part: Part, model_world: trimesh.Trimesh, nodes: np.ndarray, support_mesh: trimesh.Trimesh):
    from .supports import LOWEST, OVERHANG, FixedSupports, SupportParams, SupportPlan
    c = nodes[(nodes["role"] == CONTACT) & (nodes["face"] >= 0) & (nodes["face"] < len(model_world.faces))]
    normals = model_world.face_normals[c["face"]]           # outward normals, as GooSlicer's plans hold
    kinds = [LOWEST if t == LOWEST_T else OVERHANG for t in c["type"]]
    pts = (c["p"].astype(np.float64) - part.position) @ part.rotation / part.scale
    nl = normals @ part.rotation * part.scale
    nl /= np.maximum(np.linalg.norm(nl, axis=1), 1e-12)[:, None]
    params = SupportParams(lift=float(part.elevation))
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
            sup, nodes = read_supports(support_data[att])
            sup.apply_translation(shift)
            nodes = nodes.copy()
            nodes["p"] += shift.astype(np.float32)
            part.elevation = float(world.bounds[0][2])
            part.supports = _plan_for(part, world, nodes, sup)
        parts.append(part)
        if progress:
            progress((i + 1) / max(len(meshes), 1))
    script = info.get("ActiveScript") or {}
    printer = (info.get("SatelLiteMachine", {}).get("PrinterType") or info.get("PlatformInfo", {}).get("PlatName")
               or script.get("ExtraResinPara_PrinterName"))
    return projmod.Project(parts, printer=printer, print_settings=print_settings(script) or None,
                           app=f"SatelLite ({info.get('ProjectFormat')} v{info.get('ProjectFormatVersion')})",
                           source="SatelLite")
