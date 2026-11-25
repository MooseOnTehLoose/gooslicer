"""Layer rasteriser: polygons (mm) -> GOO RLE, matching SatelLite's Skia fill.

Behaviour reproduced (05-slicing-spec.md):
  * mm -> pixel transform of FUN_1800e1bb0 [OBSERVED], float32 arithmetic:
        c = (x - ox) * (1000 / PixX_um)
        r = ResY - (y - oy) * (1000 / PixY_um)
        if mirror_x: c = ResX - c ;  if mirror_y: r = ResY - r
  * non-antialiased SkCanvas::drawPath, fill type cleared to kWinding (non-zero) [OBSERVED],
    scan conversion as in Skia's SkEdge / walk_edges (FDot6 vertices, 16.16 x at row centres,
    spans [round(L), round(R)) ) [INFERRED from Skia's open-source rasteriser].
Output is a run list, never a full bitmap (a 16K layer is 94 Mpx).
"""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from elegoo_goo import rle


@dataclass(frozen=True)
class RasterSpec:
    res_x: int
    res_y: int
    pixel_x_um: float
    pixel_y_um: float
    mirror_x: bool = False
    mirror_y: bool = True
    origin_x: float | None = None   # mm of image column 0 (default: plate centre = image centre)
    origin_y: float | None = None

    @classmethod
    def from_profile(cls, p) -> "RasterSpec":
        return cls(p.res_x, p.res_y, p.pixel_x_um, p.pixel_y_um, p.mirror_x, p.mirror_y)

    @property
    def ox(self) -> float:
        return -self.res_x * self.pixel_x_um / 2000 if self.origin_x is None else self.origin_x

    @property
    def oy(self) -> float:
        return -self.res_y * self.pixel_y_um / 2000 if self.origin_y is None else self.origin_y

    def to_pixels(self, pts: np.ndarray) -> np.ndarray:
        """(N,2) mm -> (N,2) float32 pixel coords, as FUN_1800e1bb0 computes them."""
        f = np.float32
        p = np.asarray(pts, dtype=f)
        kx, ky = f(1000.0) / f(self.pixel_x_um), f(1000.0) / f(self.pixel_y_um)
        c = (p[:, 0] - f(self.ox)) * kx
        r = f(self.res_y) - (p[:, 1] - f(self.oy)) * ky
        if self.mirror_x:
            c = f(self.res_x) - c
        if self.mirror_y:
            r = f(self.res_y) - r
        return np.stack([c, r], axis=1).astype(f)


def _fdot6(v: np.ndarray) -> np.ndarray:
    # float -> FDot6 truncates toward zero (matches Skia m144 bit-for-bit; rounding did not)
    return np.trunc(v.astype(np.float32) * np.float32(64.0)).astype(np.int64)


def _edges(polys_px: list[np.ndarray]):
    """All polygon edges as int64 FDot6 arrays (x0, y0, x1, y1). Done for every polygon at once:
    layers with hundreds of small outlines (support pillars) spent most of their time in
    per-polygon numpy calls."""
    polys = [p for p in polys_px if len(p) >= 3]
    if not polys:
        return None
    lengths = np.fromiter((len(p) for p in polys), np.int64, len(polys))
    q = _fdot6(np.concatenate(polys))
    nxt = np.arange(1, len(q) + 1)
    ends = np.cumsum(lengths)
    nxt[ends - 1] = ends - lengths              # each polygon's last point joins its first
    return np.hstack([q, q[nxt]])


def _tdiv(a: np.ndarray, b: np.ndarray) -> np.ndarray:
    """C-style integer division (truncate toward zero)."""
    q = np.abs(a) // np.abs(b)
    return np.where((a < 0) ^ (b < 0), -q, q)


def spans(polys_px: list[np.ndarray], width: int, height: int):
    """Non-zero-winding spans: arrays (row, left, right) with left < right, sorted."""
    e = _edges(polys_px)
    empty = (np.zeros(0, np.int64),) * 3
    if e is None:
        return empty
    x0, y0, x1, y1 = e.T
    winding = np.where(y1 > y0, 1, -1)
    swap = y0 > y1
    x0, x1 = np.where(swap, x1, x0), np.where(swap, x0, x1)
    y0, y1 = np.where(swap, y1, y0), np.where(swap, y0, y1)
    top = (y0 + 32) >> 6           # SkFDot6Round
    bot = (y1 + 32) >> 6
    keep = top < bot
    if not keep.any():
        return empty
    x0, y0, x1, y1, top, bot, winding = (a[keep] for a in (x0, y0, x1, y1, top, bot, winding))
    # SkFDot6Div -> 16.16 slope; dy from the top row centre; SkFixedMul; SkFDot6ToFixed
    slope = _tdiv((x1 - x0) << 16, y1 - y0)
    dy = ((top << 6) + 32) - y0
    fx0 = (x0 + ((slope * dy) >> 16)) << 10
    # clip rows to the canvas
    t = np.clip(top, 0, height)
    b = np.clip(bot, 0, height)
    n = b - t
    ok = n > 0
    if not ok.any():
        return empty
    fx0 = fx0[ok] + slope[ok] * (t[ok] - top[ok])
    slope, t, n, winding = slope[ok], t[ok], n[ok], winding[ok]
    total = int(n.sum())
    idx = np.repeat(np.arange(len(n)), n)
    k = np.arange(total) - np.repeat(np.cumsum(n) - n, n)
    rows = t[idx] + k
    xs = fx0[idx] + slope[idx] * k
    ws = winding[idx]
    order = np.lexsort((xs, rows))
    rows, xs, ws = rows[order], xs[order], ws[order]
    acc = np.cumsum(ws)
    before = acc - ws
    # every closed contour contributes zero net winding per row, so the running sum
    # returns to 0 at each row end; guard anyway by row-relative accumulation
    row_start = np.r_[True, rows[1:] != rows[:-1]]
    base = np.maximum.accumulate(np.where(row_start, np.arange(len(rows)), 0))
    offs = before[base]
    before, after = before - offs, acc - offs
    starts = np.nonzero((before == 0) & (after != 0))[0]
    ends = np.nonzero((before != 0) & (after == 0))[0]
    if len(starts) != len(ends):
        raise ValueError("open contour: unbalanced winding")
    L = (xs[starts] + 0x8000) >> 16     # SkFixedRoundToInt
    R = (xs[ends] + 0x8000) >> 16
    r = rows[starts]
    L = np.clip(L, 0, width)
    R = np.clip(R, 0, width)
    good = L < R
    return r[good], L[good], R[good]


def runs_from_spans(r, L, R, width: int, height: int, value: int = 255) -> list[tuple[int, int]]:
    total = width * height
    if len(r) == 0:
        return [(0, total)]
    a = r * width + L
    b = r * width + R
    # merge touching spans (end of one row == start of next row, or adjacent spans)
    merge = np.r_[False, a[1:] <= b[:-1]]
    grp = np.cumsum(~merge) - 1
    a = np.minimum.reduceat(a, np.nonzero(~merge)[0])
    b = np.maximum.reduceat(b, np.nonzero(~merge)[0])
    del grp
    runs = []
    pos = 0
    for s, e in zip(a.tolist(), b.tolist()):
        if s > pos:
            runs.append((0, s - pos))
        runs.append((value, e - s))
        pos = e
    if pos < total:
        runs.append((0, total - pos))
    return runs


def encode_spans(r, L, R, width: int, height: int) -> bytes:
    """Spans (sorted by row, then left) -> encoded GOO layer image, at numpy speed. The same
    bytes as rle.encode_runs(runs_from_spans(...))."""
    total = width * height
    if len(r) == 0:
        return rle.encode_lengths([0], [total])
    a = np.asarray(r, np.int64) * width + np.asarray(L, np.int64)
    b = np.asarray(r, np.int64) * width + np.asarray(R, np.int64)
    new = np.r_[True, a[1:] > b[:-1]]                # touching or overlapping spans merge
    idx = np.flatnonzero(new)
    a = np.minimum.reduceat(a, idx)
    b = np.maximum.reduceat(b, idx)
    gaps = a - np.r_[0, b[:-1]]                      # black before each lit run
    lengths = np.empty(2 * len(a) + 1, np.int64)
    lengths[0:-1:2] = gaps
    lengths[1::2] = b - a
    lengths[-1] = total - b[-1]
    values = np.zeros(len(lengths), np.int64)
    values[1::2] = 255
    return rle.encode_lengths(values, lengths)


def _to_pixels_all(polys_mm: list[np.ndarray], spec: RasterSpec) -> list[np.ndarray]:
    """spec.to_pixels for every polygon (of 3+ points) in one call; the same values, per point."""
    polys = [p for p in polys_mm if len(p) >= 3]
    if not polys:
        return []
    lengths = [len(p) for p in polys]
    return np.split(spec.to_pixels(np.concatenate(polys)), np.cumsum(lengths)[:-1])


def rasterize_rle(polys_mm: list[np.ndarray], spec: RasterSpec) -> bytes:
    """Polygons in plate mm -> encoded GOO layer image."""
    px = _to_pixels_all(polys_mm, spec)
    r, L, R = spans(px, spec.res_x, spec.res_y)
    return encode_spans(r, L, R, spec.res_x, spec.res_y)


def rasterize_bitmap(polys_mm: list[np.ndarray], spec: RasterSpec) -> np.ndarray:
    """Same fill as a (res_y, res_x) uint8 array — for previews/tests on small specs."""
    px = _to_pixels_all(polys_mm, spec)
    row, a, b = spans(px, spec.res_x, spec.res_y)
    d = np.zeros((spec.res_y, spec.res_x + 1), np.int32)     # +1 at each span start, -1 after
    np.add.at(d, (row, a), 1)
    np.add.at(d, (row, b), -1)
    img = np.where(np.cumsum(d[:, :-1], axis=1) > 0, 255, 0).astype(np.uint8)
    return img
