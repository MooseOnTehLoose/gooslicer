"""Select by Parts (AI) (11 §2) [GOOSLICER]: PartField's part features, straight from the 3D
model, and the nested parts round a click.

PartField (NVIDIA; non-commercial research or education only) turns 100k points sampled from
the surface into three feature planes; a face's feature is read from them where it lies.
Faces of one part have similar features. From a click, the part at similarity τ is the faces
reachable from it through faces at least τ similar to it; as τ falls the part grows, in steps.
The steps where it stays the same size over a range of τ are the parts offered.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import numpy as np
import scipy.sparse as sp
from scipy.sparse import csgraph

from . import picture

N_POINTS = 100000              # points sampled from the surface for the network (fixed in the ONNX)
PER_FACE = 3                   # points averaged per face to read its feature
DIM = 64                       # features compressed to this many numbers (by SVD), unit length
BASIS_FACES = 20000            # faces sampled to find that compression
TAU_STEP = 0.01                # similarity steps for the parts
TAU_RANGE = (0.10, 0.95)
STABLE_SPAN = 0.06             # a part's size is compared this far either side of its τ
PART_MIN, PART_MAX = 0.0005, 0.6   # parts offered: this share of the surface at least / most

MODEL = picture.Model(
    "partfield", "PartField (parts from the 3D model)", "partfield", "partfield",
    (picture.ModelFile("partfield.onnx", 529756947,
                       "", ""),),
    "partfield.onnx", "",
    "PartField by NVIDIA (NVIDIA License: non-commercial research and education only); "
    "ONNX export made by GooSlicer's scripts/convert-partfield.py", True)


def model_path() -> Path | None:
    d = picture.model_dir(MODEL)
    return None if d is None else d / MODEL.encoder


def available() -> tuple[bool, str]:
    ok, why = picture.runtime_ok()
    if not ok:
        return ok, why
    if model_path() is None:
        return False, "PartField isn't installed (Preferences → Picture selection)"
    return True, ""


class FieldNet:
    """The PartField network (ONNX) on the CPU."""

    def __init__(self, path: Path | None = None):
        import onnxruntime as ort
        path = path or model_path()
        if path is None:
            raise FileNotFoundError(available()[1])
        so = ort.SessionOptions()
        so.log_severity_level = 3
        self.session = ort.InferenceSession(str(path), so, providers=["CPUExecutionProvider"])

    def planes(self, points: np.ndarray) -> np.ndarray:
        """(3, C, H, W) feature planes for N_POINTS points in [-0.9, 0.9]³."""
        x = np.asarray(points, np.float32).reshape(1, N_POINTS, 3)
        return self.session.run(None, {"points": x})[0][0]


def normalise(vertices: np.ndarray) -> tuple[np.ndarray, np.ndarray, float]:
    """Vertices in the network's frame: Z-up to its Y-up (it was trained on Objaverse, which is
    Y-up), centred, the longest side 1.8 (as PartField's loader). (vertices, centre, scale)."""
    v = np.asarray(vertices, float)
    v = np.c_[v[:, 0], v[:, 2], -v[:, 1]]
    lo, hi = v.min(0), v.max(0)
    c = (lo + hi) / 2
    sc = 1.8 / max(float((hi - lo).max()), 1e-9)
    return (v - c) * sc, c, sc


def sample_surface(v: np.ndarray, f: np.ndarray, n: int, rng) -> np.ndarray:
    """n points spread over the surface by area."""
    tri = v[f]
    a = np.linalg.norm(np.cross(tri[:, 1] - tri[:, 0], tri[:, 2] - tri[:, 0]), axis=1)
    pick = rng.choice(len(f), n, p=a / a.sum())
    return _in_triangles(tri[pick], rng)


def _in_triangles(tri: np.ndarray, rng) -> np.ndarray:
    u = np.sqrt(rng.random((len(tri), 1)))
    w = rng.random((len(tri), 1))
    return (1 - u) * tri[:, 0] + u * (1 - w) * tri[:, 1] + u * w * tri[:, 2]


def read_planes(planes: np.ndarray, pts: np.ndarray) -> np.ndarray:
    """Features at points (P, 3) in [-1, 1]: the sum over the three planes (xy, yz, xz) of a
    bilinear lookup, as grid_sample(align_corners=True, padding_mode='border')."""
    _, C, H, W = planes.shape
    out = np.zeros((len(pts), C), np.float32)
    for k, (a, b) in enumerate(((0, 1), (1, 2), (0, 2))):
        tex = planes[k].reshape(C, H * W).T                           # (H*W, C)
        x = np.clip((pts[:, a] + 1) / 2 * (W - 1), 0, W - 1)
        y = np.clip((pts[:, b] + 1) / 2 * (H - 1), 0, H - 1)
        x0, y0 = np.floor(x).astype(int), np.floor(y).astype(int)
        x1, y1 = np.minimum(x0 + 1, W - 1), np.minimum(y0 + 1, H - 1)
        fx, fy = (x - x0)[:, None], (y - y0)[:, None]
        out += (tex[y0 * W + x0] * ((1 - fx) * (1 - fy)) + tex[y0 * W + x1] * (fx * (1 - fy)) +
                tex[y1 * W + x0] * ((1 - fx) * fy) + tex[y1 * W + x1] * (fx * fy))
    return out


def face_features(net: FieldNet, vertices, faces, seed: int = 0, progress=None) -> np.ndarray:
    """(F, DIM) float16, unit length: each face's part feature. progress(text) as it goes."""
    t = time.time()
    rng = np.random.default_rng(seed)
    v, _, _ = normalise(vertices)
    f = np.asarray(faces, np.int64)
    if progress:
        progress("Finding the model's parts…")
    planes = net.planes(sample_surface(v, f, N_POINTS, rng))
    tri = v[f]
    # the compression: the top DIM directions of a sample of faces' features (uncentred, so
    # cosines between compressed features match the originals')
    pick = rng.choice(len(f), min(BASIS_FACES, len(f)), replace=False)
    full = read_planes(planes, _in_triangles(tri[pick], rng))
    _, _, vt = np.linalg.svd(full, full_matrices=False)
    basis = vt[:DIM].T.astype(np.float32)                              # (C, DIM)
    small = np.einsum("kchw,cd->kdhw", planes, basis, optimize=True)  # project the planes first
    out = np.zeros((len(f), DIM), np.float32)
    step = 100000
    for i in range(0, len(f), step):
        tr = tri[i:i + step]
        acc = np.zeros((len(tr), DIM), np.float32)
        for _ in range(PER_FACE):
            acc += read_planes(small, _in_triangles(tr, rng))
        out[i:i + step] = acc
    out /= np.maximum(np.linalg.norm(out, axis=1, keepdims=True), 1e-9)
    if os.environ.get("GOOSLICER_DEBUG_TIMES"):
        print(f"partfeat: {len(f)} faces in {time.time() - t:.1f} s")
    return out.astype(np.float16)


def reach(adjacency: np.ndarray, feat: np.ndarray, seed: int) -> np.ndarray:
    """Per face, the highest τ at which it is in the clicked part: the best, over paths from the
    seed, of the lowest similarity to the seed along the path (the path through a maximum
    spanning tree)."""
    n = len(feat)
    sim = feat.astype(np.float32) @ feat[seed].astype(np.float32)
    a = adjacency
    w = np.minimum(sim[a[:, 0]], sim[a[:, 1]])
    g = sp.coo_matrix((3.0 - w, (a[:, 0], a[:, 1])), shape=(n, n)).tocsr()   # > 0: kept as edges
    tree = csgraph.minimum_spanning_tree(g)
    order, pred = csgraph.breadth_first_order(tree, seed, directed=False, return_predecessors=True)
    b = np.full(n, -2.0, np.float32)
    b[order] = sim[order]
    p = pred.copy()
    p[seed] = seed
    reached = np.zeros(n, bool)
    reached[order] = True
    p[~reached] = np.nonzero(~reached)[0]                       # unreached faces point at themselves
    for _ in range(int(np.ceil(np.log2(max(len(order), 2)))) + 1):   # pointer jumping: min to the root
        b = np.minimum(b, b[p])
        p = p[p]
    b[~reached] = -2.0
    return b


def parts(areas: np.ndarray, b: np.ndarray) -> tuple[list[np.ndarray], int]:
    """The parts round a click, smallest first, and the index of the most stable. A part is the
    faces with reach ≥ τ at a τ where the part's area changes least for STABLE_SPAN either side
    (a local minimum of that change), between PART_MIN and PART_MAX of the surface. The most
    stable changes least; of equals, the one that keeps its size over the widest range of τ."""
    taus = np.round(np.arange(TAU_RANGE[0], TAU_RANGE[1] + 1e-9, TAU_STEP), 4)
    order = np.argsort(-b)
    cum = np.cumsum(areas[order])
    total = float(areas.sum())
    cnt = np.searchsorted(-b[order], -taus, side="right")        # faces with b ≥ τ
    area = np.where(cnt > 0, cum[np.maximum(cnt - 1, 0)], 0.0)
    la = np.log(area + 1e-12)
    w = int(round(STABLE_SPAN / TAU_STEP))
    score = np.full(len(taus), np.inf)
    for i in range(len(taus)):
        if PART_MIN < area[i] / total < PART_MAX:
            j0, j1 = max(0, i - w), min(len(taus) - 1, i + w)
            score[i] = abs(la[j0] - la[j1]) / (taus[j1] - taus[j0])
    fin = np.isfinite(score)
    if not fin.any():
        return [b >= 0.999], 0
    mins = [i for i in range(len(taus)) if fin[i] and score[i] <= score[max(0, i - 1)]
            and score[i] <= score[min(len(taus) - 1, i + 1)]]
    width = np.zeros(len(taus), int)          # ties: the part that keeps its size over more τ
    for i in np.nonzero(fin)[0]:
        same = np.abs(la - la[i]) < np.log(1.02)
        j0 = i
        while j0 > 0 and same[j0 - 1]:
            j0 -= 1
        j1 = i
        while j1 < len(taus) - 1 and same[j1 + 1]:
            j1 += 1
        width[i] = j1 - j0
    best = min(np.nonzero(fin)[0], key=lambda i: (round(float(score[i]), 6), -width[i]))
    # one part per distinct size (plateaus can repeat a size), smallest first
    out, sizes, best_i = [], [], 0
    for i in sorted(set(mins) | {best}, key=lambda i: area[i]):
        if sizes and abs(area[i] - sizes[-1]) <= 0.02 * sizes[-1]:
            if i == best:
                best_i = len(out) - 1
            continue
        if i == best:
            best_i = len(out)
        out.append(b >= taus[i])
        sizes.append(area[i])
    return out, best_i
