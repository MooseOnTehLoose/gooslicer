"""Mesh import, analysis and repair (Qt-free)."""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import numpy as np
import trimesh

SUPPORTED = (".stl", ".obj", ".3mf")


class MeshError(ValueError):
    pass


def load_meshes(path: str | os.PathLike) -> list[tuple[str, trimesh.Trimesh]]:
    """Load a model file into one or more named triangle meshes (world units = mm).

    3MF/OBJ files may hold several objects; their build transforms are applied.
    """
    p = Path(path)
    ext = p.suffix.lower()
    if ext not in SUPPORTED:
        raise MeshError(f"unsupported file type {ext!r} (supported: {', '.join(SUPPORTED)})")
    try:
        loaded = trimesh.load(str(p), force="scene", process=False)
    except Exception as e:  # trimesh raises a variety of errors on bad input
        raise MeshError(f"cannot read {p.name}: {e}") from e
    out: list[tuple[str, trimesh.Trimesh]] = []
    geoms = loaded.dump(concatenate=False) if isinstance(loaded, trimesh.Scene) else [loaded]
    for i, g in enumerate(geoms):
        if not isinstance(g, trimesh.Trimesh) or len(g.faces) == 0:
            continue
        g = trimesh.Trimesh(vertices=np.asarray(g.vertices, dtype=np.float64),
                            faces=np.asarray(g.faces, dtype=np.int64), process=True)
        name = p.stem if len(geoms) == 1 else f"{p.stem} [{g.metadata.get('name') or i + 1}]"
        out.append((name, g))
    if not out:
        raise MeshError(f"{p.name} contains no triangles")
    return out


@dataclass(frozen=True)
class MeshReport:
    triangles: int
    vertices: int
    components: int
    boundary_edges: int        # edges used by exactly one face (holes)
    nonmanifold_edges: int     # edges used by more than two faces
    degenerate_faces: int
    duplicate_faces: int
    winding_consistent: bool
    watertight: bool
    inverted: bool             # closed but normals point inwards
    volume: float | None       # mm³, only when watertight

    @property
    def ok(self) -> bool:
        return (self.watertight and self.winding_consistent and not self.inverted
                and self.degenerate_faces == 0 and self.duplicate_faces == 0
                and self.nonmanifold_edges == 0)

    def problems(self) -> list[str]:
        out = []
        if self.boundary_edges:
            out.append(f"{self.boundary_edges} open (hole) edges")
        if self.nonmanifold_edges:
            out.append(f"{self.nonmanifold_edges} non-manifold edges")
        if self.degenerate_faces:
            out.append(f"{self.degenerate_faces} degenerate triangles")
        if self.duplicate_faces:
            out.append(f"{self.duplicate_faces} duplicate triangles")
        if not self.winding_consistent:
            out.append("inconsistent triangle orientation")
        if self.inverted:
            out.append("normals point inwards")
        return out


def _edge_use_counts(faces: np.ndarray) -> np.ndarray:
    e = np.sort(faces[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    _, counts = np.unique(e, axis=0, return_counts=True)
    return counts


def analyze(mesh: trimesh.Trimesh) -> MeshReport:
    faces = np.asarray(mesh.faces)
    counts = _edge_use_counts(faces) if len(faces) else np.zeros(0, int)
    area = mesh.area_faces if len(faces) else np.zeros(0)
    degenerate = int(np.count_nonzero(area <= 1e-12))
    dup = len(faces) - len(np.unique(np.sort(faces, axis=1), axis=0)) if len(faces) else 0
    boundary = int(np.count_nonzero(counts == 1))
    nonmanifold = int(np.count_nonzero(counts > 2))
    watertight = len(faces) > 0 and boundary == 0 and nonmanifold == 0
    winding = bool(mesh.is_winding_consistent) if len(faces) else True
    vol = float(mesh.volume) if watertight and winding else None
    comps = int(trimesh.graph.connected_component_labels(mesh.face_adjacency, len(faces)).max() + 1) \
        if len(faces) else 0
    return MeshReport(len(faces), len(mesh.vertices), comps, boundary, nonmanifold, degenerate, dup,
                      winding, watertight, vol is not None and vol < 0, abs(vol) if vol is not None else None)


def repair(mesh: trimesh.Trimesh) -> trimesh.Trimesh:
    """Return a repaired copy: weld vertices, drop degenerate/duplicate faces,
    fix winding and outward normals, fill simple holes."""
    m = mesh.copy()
    m.merge_vertices()
    m.update_faces(m.nondegenerate_faces(height=1e-9))
    m.update_faces(m.unique_faces())
    m.remove_unreferenced_vertices()
    trimesh.repair.fix_winding(m)
    trimesh.repair.fill_holes(m)
    trimesh.repair.fix_normals(m, multibody=True)
    if m.is_watertight and m.volume < 0:
        m.invert()
    return m
