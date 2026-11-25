"""CPU-heavy jobs the GUI runs in child processes (gooslicer_qt.workers.submit_process): Qt-free,
and module-level so they pickle by name. A worker thread doing this work would hold Python's GIL
and stall the GUI; a child process doesn't."""
from __future__ import annotations

import numpy as np
import trimesh

from . import hollow as H
from . import supports as S


def _red(meshes, masks) -> list:
    return [m.submesh([np.flatnonzero(sel)], append=True) for m, sel in zip(meshes, masks) if sel.any()]


def islands_and_faces(meshes, layer_height):
    """The island check, plus each island's unsupported surface as red meshes for the 3D view:
    (islands, [[meshes] per island])."""
    isl = S.unsupported_islands(meshes, layer_height=layer_height)
    reds = [_red(meshes, masks) for masks in S.island_faces_each(meshes, isl, layer_height)] if isl else []
    return isl, reds


def cavities_and_faces(meshes):
    """The trapped-resin check, plus the surface lining each sealed cavity or suction cup as
    red meshes: (cavities, [[meshes] per cavity])."""
    cav, each = H.cavity_faces_each(meshes, cups=True)
    return cav, [_red(meshes, masks) for masks in each]


def cavity_shells(meshes):
    return H.cavity_shells(meshes)


def scaled(part) -> tuple[trimesh.Trimesh, bool]:
    """The part's mesh with its scale applied (hollowing and drilling work at true size)."""
    m = trimesh.Trimesh(np.asarray(part.mesh.vertices) * part.scale, part.mesh.faces, process=False)
    flipped = bool(np.prod(np.sign(part.scale)) < 0)
    if flipped:
        m.invert()
    return m, flipped


def unscaled(mesh: trimesh.Trimesh, scale, flipped: bool) -> trimesh.Trimesh:
    out = trimesh.Trimesh(np.asarray(mesh.vertices) / scale, mesh.faces, process=False)
    if flipped:
        out.invert()
    return out


def hollow_mesh(mesh: trimesh.Trimesh, scale, s) -> tuple[trimesh.Trimesh, H.HollowResult]:
    """Hollow a part's local mesh at its scale: (the hollowed local mesh, the result)."""
    from types import SimpleNamespace
    m, flipped = scaled(SimpleNamespace(mesh=mesh, scale=np.asarray(scale, float)))
    res = H.hollow(m, s)
    return unscaled(res.mesh, scale, flipped), res
