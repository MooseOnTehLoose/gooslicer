"""GooSlicer projects: a standard 3MF package plus GooSlicer's state as JSON (Qt-free). See 09 §2–3.

Layout of the zip:
  [Content_Types].xml, _rels/.rels
  3D/3dmodel.model                  each distinct part mesh as an <object type="model"> in its local
                                    frame, one <build><item> per part with its T·R·S transform; built
                                    support meshes as <object type="support"> (world frame)
  Metadata/gooslicer/project.json   printer, print settings, parts' exact placement, support plans,
                                    dismissed flaws, camera view
  Metadata/thumbnail.png            optional preview

Any 3MF reader shows the models (and supports) where GooSlicer had them. GooSlicer reads its own
files with a fast path that only understands what `save` writes; it never runs an XML parser on
the package, so entities and external references are never expanded or followed. Files without
project.json are plain models (`mesh.load_meshes`).
"""
from __future__ import annotations

import json
import os
import re
import zipfile
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from xml.sax.saxutils import quoteattr, unescape

import numpy as np
import trimesh

from .scene import Part

FORMAT = "gooslicer-project"
VERSION = 1
MODEL_PATH = "3D/3dmodel.model"
PROJECT_PATH = "Metadata/gooslicer/project.json"
THUMB_PATH = "Metadata/thumbnail.png"
REL_MODEL = "http://schemas.microsoft.com/3dmanufacturing/2013/01/3dmodel"
REL_THUMB = "http://schemas.openxmlformats.org/package/2006/relationships/metadata/thumbnail"
REL_PROJECT = "https://github.com/MooseOnTehLoose/gooslicer/project"
NS_CORE = "http://schemas.microsoft.com/3dmanufacturing/core/2015/02"
CHUNK = 65536                    # rows formatted per batch
MAX_ENTRY = 8 << 30              # refuse zip entries that would unpack beyond 8 GB

_CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
 <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
 <Default Extension="model" ContentType="application/vnd.ms-package.3dmanufacturing-3dmodel+xml"/>
 <Default Extension="png" ContentType="image/png"/>
 <Default Extension="json" ContentType="application/json"/>
</Types>
"""


class ProjectError(ValueError):
    pass


@dataclass
class Project:
    parts: list[Part] = field(default_factory=list)
    printer: str | None = None
    print_settings: dict | None = None
    support_type: str | None = None
    dismissed: dict = field(default_factory=dict)       # kind -> [[x, y, z], ...] (world)
    view: dict | None = None
    app: str = ""
    saved: str = ""
    newer: bool = False          # written by a newer GooSlicer (loaded anyway, with a warning)
    source: str = ""             # "SatelLite" for a project read from another program's format


# --------------------------------------------------------------------------- writing
def _transform(part: Part) -> str:
    """3MF's 3×4 transform (row vectors: p' = p·M) for world = R·diag(S)·local + position."""
    a = part.rotation @ np.diag(part.scale)
    return " ".join(f"{v:.17g}" for v in [*a.T.ravel(), *part.position])


def _write_rows(out, fmt: str, arr: np.ndarray) -> None:
    for i in range(0, len(arr), CHUNK):
        block = arr[i:i + CHUNK]
        out.write(((fmt * len(block)) % tuple(block.ravel().tolist())).encode())


def _write_mesh(out, oid: int, mesh: trimesh.Trimesh, name: str, kind: str) -> None:
    out.write(f' <object id="{oid}" type="{kind}" name={quoteattr(name)}>\n  <mesh>\n   <vertices>\n'.encode())
    _write_rows(out, '    <vertex x="%.9g" y="%.9g" z="%.9g"/>\n', np.asarray(mesh.vertices, np.float64))
    out.write(b"   </vertices>\n   <triangles>\n")
    _write_rows(out, '    <triangle v1="%d" v2="%d" v3="%d"/>\n', np.asarray(mesh.faces, np.int64))
    out.write(b"   </triangles>\n  </mesh>\n </object>\n")


def _plan_dict(plan) -> dict:
    return {"template": plan.template, "params": asdict(plan.params),
            "points": np.asarray(plan.points, float).tolist(),
            "normals": np.asarray(plan.normals, float).tolist(), "kinds": list(plan.kinds)}


def save(path: str | os.PathLike, project: Project, *, support_meshes: dict | None = None,
         thumbnail: bytes | None = None, progress=None) -> None:
    """Write the project to `path`, through a temporary file beside it, so a failure never
    damages an existing file. support_meshes: {part id: world-frame support mesh} for other
    programs to show; GooSlicer rebuilds supports from the plans."""
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    support_meshes = support_meshes or {}
    objects: dict[int, int] = {}            # id(mesh) -> object id
    plans: dict[int, int] = {}              # id(plan) -> index in "plans"
    plan_list, parts_json = [], []
    fixed_parts: dict[int, Part] = {}       # plan index -> the part whose support mesh is the plan's
    try:
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=5) as z:
            z.writestr("[Content_Types].xml", _CONTENT_TYPES)
            rels = [f'<Relationship Target="/{MODEL_PATH}" Id="rel0" Type="{REL_MODEL}"/>',
                    f'<Relationship Target="/{PROJECT_PATH}" Id="rel1" Type="{REL_PROJECT}"/>']
            if thumbnail:
                rels.append(f'<Relationship Target="/{THUMB_PATH}" Id="rel2" Type="{REL_THUMB}"/>')
            z.writestr("_rels/.rels", '<?xml version="1.0" encoding="UTF-8"?>\n<Relationships xmlns='
                       '"http://schemas.openxmlformats.org/package/2006/relationships">\n '
                       + "\n ".join(rels) + "\n</Relationships>\n")
            items = []
            with z.open(MODEL_PATH, "w", force_zip64=True) as out:
                out.write(f'<?xml version="1.0" encoding="UTF-8"?>\n<model unit="millimeter" '
                          f'xml:lang="en-US" xmlns="{NS_CORE}">\n'
                          f' <metadata name="Application">GooSlicer</metadata>\n<resources>\n'.encode())
                n = len(project.parts) + len(support_meshes)
                for i, part in enumerate(project.parts):
                    if id(part.mesh) not in objects:
                        objects[id(part.mesh)] = len(objects) + 1
                        _write_mesh(out, objects[id(part.mesh)], part.mesh, part.name, "model")
                    items.append((objects[id(part.mesh)], _transform(part)))
                    entry = {"object": objects[id(part.mesh)], "item": i, "name": part.name,
                             "position": part.position.tolist(), "rotation": part.rotation.tolist(),
                             "scale": part.scale.tolist(), "elevation": float(part.elevation)}
                    if part.supports is not None:
                        if id(part.supports) not in plans:
                            plans[id(part.supports)] = len(plan_list)
                            plan_list.append(_plan_dict(part.supports))
                            fx = getattr(part.supports, "fixed", None)
                            if fx is not None and fx.offset(part) is not None and part.id in support_meshes:
                                fixed_parts[plans[id(part.supports)]] = part
                        entry["plan"] = plans[id(part.supports)]
                    parts_json.append(entry)
                    if progress:
                        progress((i + 1) / n)
                oid = len(objects)
                fixed_oid: dict[int, int] = {}      # part id -> its support object
                for k, part in enumerate(project.parts):
                    m = support_meshes.get(part.id)
                    if m is None or not len(m.faces):
                        continue
                    oid += 1
                    fixed_oid[part.id] = oid
                    _write_mesh(out, oid, m, f"{part.name} supports", "support")
                    items.append((oid, "1 0 0 0 1 0 0 0 1 0 0 0"))
                    if progress:
                        progress((len(project.parts) + k + 1) / n)
                out.write(b"</resources>\n<build>\n")
                for oid, tr in items:
                    out.write(f' <item objectid="{oid}" transform="{tr}"/>\n'.encode())
                out.write(b"</build>\n</model>\n")
            for i, part in fixed_parts.items():
                if part.id in fixed_oid:            # SatelLite's mesh, kept exact (10 §6)
                    plan_list[i]["fixed"] = {"object": fixed_oid[part.id], "source": part.supports.fixed.source,
                                             "position": part.position.tolist(),
                                             "rotation": part.rotation.tolist(), "scale": part.scale.tolist()}
            doc = {"format": FORMAT, "version": VERSION, "app": project.app,
                   "saved": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
                   "printer": project.printer, "print_settings": project.print_settings,
                   "support_type": project.support_type, "parts": parts_json, "plans": plan_list,
                   "dismissed_flaws": {k: [list(map(float, p)) for p in v] for k, v in project.dismissed.items()},
                   "view": project.view}
            z.writestr(PROJECT_PATH, json.dumps(doc, indent=1))
            if thumbnail:
                z.writestr(THUMB_PATH, thumbnail)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


# --------------------------------------------------------------------------- reading
_OBJECT_TAG = re.compile(rb"<object\b([^>]*)>")
_ITEM = re.compile(rb"<item\b([^>]*?)/?>")
_ATTR = re.compile(rb'([\w:]+)\s*=\s*"([^"]*)"')
_VALUES = re.compile(rb'"([^"]*)"')


def _attrs(raw: bytes) -> dict[str, str]:
    return {k.decode(): unescape(v.decode(), {"&quot;": '"', "&apos;": "'"}) for k, v in _ATTR.findall(raw)}


def _objects(model: bytes):
    """(attributes, start, end) of each <object>…</object>, found with plain searches: lazy regexes
    over a file of hundreds of MB were most of the load time."""
    pos = 0
    while (m := _OBJECT_TAG.search(model, pos)) is not None:
        end = model.find(b"</object>", m.end())
        if end < 0:
            raise ProjectError(f"an object in {MODEL_PATH} is not closed")
        yield _attrs(m.group(1)), m.end(), end
        pos = end


def _numbers(model: bytes, start: int, end: int, tag: bytes, width: int, dtype) -> np.ndarray:
    a = model.find(b"<%s>" % tag, start, end)
    b = model.find(b"</%s>" % tag, a, end) if a >= 0 else -1
    vals = _VALUES.findall(model, a, b) if b >= 0 else []
    if len(vals) % width:
        raise ProjectError(f"unexpected {tag.decode()} layout in {MODEL_PATH}")
    return np.array(vals).astype(dtype).reshape(-1, width) if vals else np.zeros((0, width), dtype)


def _entry(z: zipfile.ZipFile, name: str) -> bytes:
    try:
        info = z.getinfo(name)
    except KeyError:
        raise ProjectError(f"{name} is missing") from None
    if info.file_size > MAX_ENTRY:
        raise ProjectError(f"{name} is too large ({info.file_size / 2**30:.1f} GB)")
    return z.read(info)


def is_gooslicer_project(path: str | os.PathLike) -> bool:
    """True for a 3MF that holds GooSlicer's project.json."""
    try:
        with zipfile.ZipFile(path) as z:
            return PROJECT_PATH in z.namelist()
    except (OSError, zipfile.BadZipFile):
        return False


def is_project(path: str | os.PathLike) -> bool:
    """A GooSlicer project, or a SatelLite one (.goo.3mf, 10), which opens as an untitled project."""
    from . import satellite
    return is_gooslicer_project(path) or satellite.is_satellite_project(path)


def thumbnail(path: str | os.PathLike) -> bytes | None:
    try:
        with zipfile.ZipFile(path) as z:
            return z.read(THUMB_PATH) if THUMB_PATH in z.namelist() else None
    except (OSError, zipfile.BadZipFile, KeyError):
        return None


def _plan(d: dict):
    from .supports import SupportParams, SupportPlan
    names = {f.name for f in fields(SupportParams)}
    params = SupportParams(**{k: v for k, v in d.get("params", {}).items() if k in names})
    pts = np.asarray(d.get("points", []), float).reshape(-1, 3)
    nrm = np.asarray(d.get("normals", []), float).reshape(-1, 3)
    kinds = list(d.get("kinds", []))
    if not (len(pts) == len(nrm) == len(kinds)):
        raise ProjectError("a support plan's points, normals and kinds differ in length")
    return SupportPlan(params, pts, nrm, kinds, d.get("template", ""))


def load(path: str | os.PathLike, progress=None) -> Project:
    """Read a GooSlicer or SatelLite project. Raises ProjectError for anything else or anything
    damaged."""
    path = Path(path)
    if not is_gooslicer_project(path):
        from . import satellite
        if satellite.is_satellite_project(path):
            return satellite.load(path, progress)
    try:
        with zipfile.ZipFile(path) as z:
            try:
                doc = json.loads(_entry(z, PROJECT_PATH))
            except (ValueError, UnicodeDecodeError) as e:
                raise ProjectError(f"{PROJECT_PATH} is not valid JSON: {e}") from e
            if not isinstance(doc, dict) or doc.get("format") != FORMAT:
                raise ProjectError(f"{path.name} is not a GooSlicer project")
            model = _entry(z, MODEL_PATH)
    except zipfile.BadZipFile as e:
        raise ProjectError(f"{path.name} is damaged or not a 3MF file: {e}") from e
    except OSError as e:
        raise ProjectError(f"cannot read {path.name}: {e.strerror or e}") from e
    try:
        return _build(doc, model, progress)
    except ProjectError:
        raise
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise ProjectError(f"{path.name} is damaged: {type(e).__name__}: {e}") from e


def _build(doc: dict, model: bytes, progress) -> Project:
    wanted = {int(p["object"]) for p in doc.get("parts", [])}
    fixed_ids = {int(d["fixed"]["object"]) for d in doc.get("plans", []) if isinstance(d.get("fixed"), dict)}
    wanted |= fixed_ids
    meshes: dict[int, trimesh.Trimesh] = {}
    for a, start, end in _objects(model):
        oid = int(a.get("id", -1))
        if oid not in wanted:
            continue                                     # support objects: rebuilt from plans
        v = _numbers(model, start, end, b"vertices", 3, np.float64)
        f = _numbers(model, start, end, b"triangles", 3, np.int64)
        if len(f) and (f.min() < 0 or f.max() >= len(v)):
            raise ProjectError(f"object {oid} refers to vertices it doesn't have")
        meshes[oid] = trimesh.Trimesh(v, f, process=False)
        if progress:
            progress(len(meshes) / max(len(wanted), 1))
    missing = wanted - meshes.keys()
    if missing:
        raise ProjectError(f"objects {sorted(missing)} are missing from {MODEL_PATH}")
    plan_docs = doc.get("plans", [])
    plans = [_plan(d) for d in plan_docs]
    parts = []
    for d in doc.get("parts", []):
        part = Part(str(d.get("name", "part")), meshes[int(d["object"])],
                    np.asarray(d["position"], float).reshape(3).copy(),
                    np.asarray(d["rotation"], float).reshape(3, 3).copy(),
                    np.asarray(d["scale"], float).reshape(3).copy(),
                    elevation=float(d.get("elevation", 0.0)))
        if np.any(part.scale <= 0):
            raise ProjectError(f"part {part.name!r} has a scale that isn't positive")
        if "plan" in d:
            i = int(d["plan"])
            part.supports = plans[i]
            fd = plan_docs[i].get("fixed")
            if isinstance(fd, dict) and part.supports.fixed is None:
                from .supports import FixedSupports
                part.supports.fixed = FixedSupports(
                    meshes[int(fd["object"])], part.mesh, np.asarray(fd["position"], float).reshape(3).copy(),
                    np.asarray(fd["rotation"], float).reshape(3, 3).copy(),
                    np.asarray(fd["scale"], float).reshape(3).copy(), str(fd.get("source", "SatelLite")))
        parts.append(part)
    dismissed = {str(k): [np.asarray(p, float).reshape(3) for p in v]
                 for k, v in (doc.get("dismissed_flaws") or {}).items()}
    ver = doc.get("version", 0)
    return Project(parts, doc.get("printer"), doc.get("print_settings"), doc.get("support_type"),
                   dismissed, doc.get("view"), str(doc.get("app", "")), str(doc.get("saved", "")),
                   newer=isinstance(ver, int) and ver > VERSION)
