"""Thumbnail renderer for .goo previews (Qt-free: numpy + Pillow).

Orthographic three-quarter view, flat shading; meshes up to ~150k faces are drawn as
depth-sorted polygons, larger ones as z-buffered face splats (supersampled).
"""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageDraw

BG = (32, 34, 38)
BASE = np.array([120, 170, 200], float)


def _view(yaw=-60.0, pitch=28.0):
    y, p = np.radians(yaw), np.radians(pitch)
    fwd = -np.array([np.cos(p) * np.cos(y), np.cos(p) * np.sin(y), np.sin(p)])
    right = np.cross(fwd, [0, 0, 1])
    right /= np.linalg.norm(right)
    up = np.cross(right, fwd)
    return right, up, fwd


def render(meshes, size: int = 290, yaw: float = -60.0, pitch: float = 28.0) -> np.ndarray:
    """Return an (size, size, 3) uint8 RGB image of the given world-space meshes."""
    tris = [np.asarray(m.vertices)[np.asarray(m.faces)] for m in meshes if len(m.faces)]
    img = Image.new("RGB", (size, size), BG)
    if not tris:
        return np.asarray(img)
    t = np.concatenate(tris)                         # (F, 3, 3)
    right, up, fwd = _view(yaw, pitch)
    n = np.cross(t[:, 1] - t[:, 0], t[:, 2] - t[:, 0])
    ln = np.linalg.norm(n, axis=1)
    ok = ln > 0
    t, n = t[ok], n[ok] / ln[ok, None]
    facing = n @ -fwd > 0
    t, n = t[facing], n[facing]
    light = 0.35 + 0.65 * np.clip(n @ (-fwd * 0.7 + up * 0.5 + right * 0.2) / 0.88, 0, 1)
    sx, sy, sz = t @ right, t @ up, t @ fwd
    lo = np.array([sx.min(), sy.min()])
    span = max(sx.max() - lo[0], sy.max() - lo[1], 1e-9)
    ss = 2 if len(t) > 150_000 else 1
    scale = (size * ss * 0.86) / span
    off = (size * ss - np.array([sx.max() - lo[0], sy.max() - lo[1]]) * scale) / 2
    px = (sx - lo[0]) * scale + off[0]
    py = size * ss - ((sy - lo[1]) * scale + off[1])
    colour = np.clip(BASE[None] * light[:, None], 0, 255).astype(np.uint8)
    depth = sz.mean(axis=1)
    if len(t) <= 150_000:
        draw = ImageDraw.Draw(img)
        for i in np.argsort(-depth):
            draw.polygon([(px[i, 0], py[i, 0]), (px[i, 1], py[i, 1]), (px[i, 2], py[i, 2])],
                         fill=tuple(int(c) for c in colour[i]))
        return np.asarray(img)
    big = size * ss
    cx = np.clip(px.mean(axis=1).astype(int), 0, big - 1)
    cy = np.clip(py.mean(axis=1).astype(int), 0, big - 1)
    idx = cy * big + cx
    zbuf = np.full(big * big, np.inf)
    np.minimum.at(zbuf, idx, depth)
    win = depth <= zbuf[idx]
    out = np.tile(np.array(BG, np.uint8), (big * big, 1))
    out[idx[win]] = colour[win]
    out = out.reshape(big, big, 3)
    z = zbuf.reshape(big, big)
    # fill pinholes (pixels no face centre landed on) from the nearest-in-depth neighbour
    for _ in range(2):
        hole = ~np.isfinite(z)
        best_z, best_c = np.full(z.shape, np.inf), out.copy()
        for dy, dx in ((0, 1), (0, -1), (1, 0), (-1, 0), (1, 1), (-1, -1), (1, -1), (-1, 1)):
            zz = np.roll(z, (dy, dx), (0, 1))
            cc = np.roll(out, (dy, dx), (0, 1))
            take = hole & (zz < best_z)
            best_z[take], best_c[take] = zz[take], cc[take]
        inner = hole & np.isfinite(best_z)
        neighbours = sum(np.isfinite(np.roll(z, s, (0, 1))) for s in ((0, 1), (0, -1), (1, 0), (-1, 0)))
        inner &= neighbours >= 3   # only true pinholes, keep silhouettes crisp
        out[inner], z[inner] = best_c[inner], best_z[inner]
    return np.asarray(Image.fromarray(out).resize((size, size), Image.BOX))
