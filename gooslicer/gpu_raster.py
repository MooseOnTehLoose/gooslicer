"""GPU layer rasteriser (experimental): the meshes themselves are drawn, not their sections.

For each layer the plate is drawn from above in the printer's pixel grid, clipped at the layer
height (gl_ClipDistance keeps what lies above it). Faces seen from above add one to the stencil,
faces seen from below take one off, so a pixel's stencil is the winding number of the solid at
the layer height under its centre: non-zero means resin, the same non-zero rule as the CPU fill
(raster.py), sampled at pixel centres like it. A second pass packs eight pixels into a byte, so
only an eighth of the image comes back; only the pixels under the models are drawn, in tiles.
Runs are found from the packed bytes (bit tricks, unpacking only bytes where a run starts or
ends) and encoded on worker threads while the GPU draws the next layers.

Edge pixels can differ from the CPU path where a contour passes within ~1/64 pixel of a pixel
centre (Skia snaps vertices to 1/64 px; the GPU doesn't) and on GL's tie rule. Open
(non-watertight) meshes have no consistent winding here; the CPU path closes their sections.

Needs a current OpenGL ≥ 3.3 core context on the calling thread (gooslicer_qt makes one).
"""
from __future__ import annotations

import ctypes
import os
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Iterator, NamedTuple

import numpy as np
from OpenGL import GL
# the wrapped glVertexAttribPointer keeps the array in per-context data, which PyOpenGL can't find
# for a context it didn't make (Qt's, EGL or GLX): the raw entry point skips that
from OpenGL.raw.GL.VERSION.GL_1_0 import glReadPixels as _raw_read_pixels
from OpenGL.raw.GL.VERSION.GL_1_5 import glUnmapBuffer as _raw_unmap
from OpenGL.raw.GL.VERSION.GL_3_0 import glMapBufferRange as _raw_map_range
from OpenGL.raw.GL.VERSION.GL_2_0 import glVertexAttribPointer as _raw_attrib_pointer

from .raster import RasterSpec, encode_spans

TILE = 4096                 # pixels per tile side (stencil + mask + packed ≈ 86 MB at the most)
MARGIN_PX = 2               # around the models' pixel bounds

_VS = """#version 330 core
layout(location = 0) in vec3 pos;
uniform vec2 k;            // pixels per mm (x, y)
uniform vec2 o;            // mm of pixel 0 (x, y)
uniform vec2 res;          // image size in pixels
uniform bvec2 mirror;
uniform vec4 tile;         // x0, y0, width, height in pixels
uniform float zcut;
void main() {
    float c = (pos.x - o.x) * k.x;
    float r = res.y - (pos.y - o.y) * k.y;
    if (mirror.x) c = res.x - c;
    if (mirror.y) r = res.y - r;
    gl_Position = vec4((c - tile.x) / tile.z * 2.0 - 1.0, (r - tile.y) / tile.w * 2.0 - 1.0, 0.0, 1.0);
    gl_ClipDistance[0] = pos.z - zcut;
}
"""
_FS = """#version 330 core
out vec4 frag;
void main() { frag = vec4(1.0); }
"""
_QUAD_VS = """#version 330 core
layout(location = 0) in vec2 pos;
void main() { gl_Position = vec4(pos, 0.0, 1.0); }
"""
_PACK_FS = """#version 330 core
uniform sampler2D mask;    // the tile: resin where > 0.5
uniform int w;             // its width: pixels from w on are 0 (each row ends in black)
out uint bits8;
void main() {
    ivec2 p = ivec2(gl_FragCoord.xy);
    uint v = 0u;
    for (int i = 0; i < 8; i++) {
        int x = p.x * 8 + i;
        if (x < w && texelFetch(mask, ivec2(x, p.y), 0).r > 0.5) v |= 1u << uint(7 - i);
    }
    bits8 = v;
}
"""


class GpuUnavailable(RuntimeError):
    """No usable OpenGL (no context, too old, or the image doesn't fit). Slice on the CPU."""


def _program(vs: str, fs: str) -> int:
    def sh(kind, src):
        s = GL.glCreateShader(kind)
        GL.glShaderSource(s, src)
        GL.glCompileShader(s)
        if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
            raise GpuUnavailable(GL.glGetShaderInfoLog(s).decode(errors="replace"))
        return s
    p = GL.glCreateProgram()
    for s in (sh(GL.GL_VERTEX_SHADER, vs), sh(GL.GL_FRAGMENT_SHADER, fs)):
        GL.glAttachShader(p, s)
    GL.glLinkProgram(p)
    if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
        raise GpuUnavailable(GL.glGetProgramInfoLog(p).decode(errors="replace"))
    return p


def _vao(data: np.ndarray, comps: int) -> tuple[int, int]:
    vao = GL.glGenVertexArrays(1)
    vbo = GL.glGenBuffers(1)
    GL.glBindVertexArray(vao)
    GL.glBindBuffer(GL.GL_ARRAY_BUFFER, vbo)
    GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
    GL.glEnableVertexAttribArray(0)
    _raw_attrib_pointer(0, comps, GL.GL_FLOAT, GL.GL_FALSE, 0, ctypes.c_void_p(0))
    GL.glBindVertexArray(0)
    return vao, vbo


def packed_width(w: int) -> int:
    """Bytes per packed row of w pixels: at least one black bit after the last pixel."""
    return w // 8 + 1


class Tile(NamedTuple):
    """One read-back tile, eight pixels a byte (first pixel in the high bit): `buf` holds a
    leading zero byte, then h rows of packed_width(w) bytes, each row ending in black, so every
    run of resin is bounded by black in the flat bit stream. The tile's pixel (col, row) is
    image pixel (x0 + col, y0 + row)."""
    buf: np.ndarray
    w: int
    h: int
    x0: int
    y0: int

    def image(self) -> np.ndarray:
        bits = np.unpackbits(self.buf[1:]).reshape(self.h, packed_width(self.w) * 8)[:, :self.w]
        return bits * np.uint8(255)


def _nonzero_bytes(a: np.ndarray) -> np.ndarray:
    """np.flatnonzero for a mostly-zero uint8 array, eight bytes at a time."""
    n8 = len(a) // 8 * 8
    words = np.flatnonzero(a[:n8].view(np.uint64))
    sub = a[:n8].reshape(-1, 8)[words]
    wi, bi = np.nonzero(sub)
    head = words[wi] * 8 + bi
    tail = np.flatnonzero(a[n8:]) + n8
    return np.concatenate([head, tail]) if len(tail) else head


def spans_from_tile(t: Tile):
    """(row, left, right) spans of a tile, row-major order. Bit k of a byte, from the top,
    differs from the pixel before it (bit k-1, or the previous byte's lowest bit) where the
    byte XOR itself shifted right one bit (carrying that lowest bit in) has a 1: only bytes
    with such a change are unpacked."""
    b = t.buf
    cur, prev = b[1:], b[:-1]
    change = cur ^ ((cur >> 1) | (prev << 7))
    idx = _nonzero_bytes(change)
    bi, bk = np.nonzero(np.unpackbits(change[idx]).reshape(-1, 8))
    pos = idx[bi].astype(np.int64) * 8 + bk                 # pixel index in the bit stream
    starts, ends = pos[0::2], pos[1::2]                     # runs alternate on, off
    s = packed_width(t.w) * 8
    return starts // s + t.y0, starts % s + t.x0, ends % s + t.x0


def runs_from_tiles(parts, spec: RasterSpec) -> bytes:
    """Encode one layer from its tiles' spans (any order)."""
    if parts:
        r = np.concatenate([p[0] for p in parts])
        L = np.concatenate([p[1] for p in parts])
        R = np.concatenate([p[2] for p in parts])
        order = np.lexsort((L, r))
        r, L, R = r[order], L[order], R[order]
    else:
        r = L = R = np.zeros(0, np.int64)
    return encode_spans(r, L, R, spec.res_x, spec.res_y)


class GpuRasterizer:
    """Rasterises layers of fixed meshes. Create, use and close() on one thread, with a current
    GL context."""

    def __init__(self, meshes, spec: RasterSpec):
        try:
            ver = GL.glGetString(GL.GL_VERSION)
        except Exception as e:  # noqa: BLE001 (no context: PyOpenGL raises various errors)
            raise GpuUnavailable(f"no OpenGL context: {e}") from e
        if not ver:
            raise GpuUnavailable("no OpenGL context")
        self.renderer = (GL.glGetString(GL.GL_RENDERER) or b"").decode(errors="replace")
        self.spec = spec
        limit = min(int(GL.glGetIntegerv(GL.GL_MAX_RENDERBUFFER_SIZE)),
                    int(np.min(GL.glGetIntegerv(GL.GL_MAX_VIEWPORT_DIMS))))
        self.tile = max(256, min(TILE, limit))
        tris = [np.asarray(m.vertices, np.float32)[np.asarray(m.faces)].reshape(-1, 3) for m in meshes if len(m.faces)]
        self.count = sum(len(t) for t in tris)
        self.region = self._region(meshes)
        self._objs = []
        self._pbo: dict[int, list[int]] = {}
        if not self.count or self.region is None:
            return
        self.prog = _program(_VS, _FS)
        self.fill = _program(_QUAD_VS, _FS)
        self.pack = _program(_QUAD_VS, _PACK_FS)
        self.vao, vbo = _vao(np.ascontiguousarray(np.concatenate(tris)), 3)
        self.quad, qbo = _vao(np.array([-1, -1, 1, -1, -1, 1, 1, 1], np.float32), 2)
        self.fbo, self.pfbo = GL.glGenFramebuffers(2)
        self.stencil = GL.glGenRenderbuffers(1)
        self.mask, self.packed = GL.glGenTextures(2)
        pw = packed_width(self.tile)
        for tex, fmt, w, kind in ((self.mask, GL.GL_R8, self.tile, GL.GL_RED),
                                  (self.packed, GL.GL_R8UI, pw, GL.GL_RED_INTEGER)):
            GL.glBindTexture(GL.GL_TEXTURE_2D, tex)
            GL.glTexImage2D(GL.GL_TEXTURE_2D, 0, fmt, w, self.tile, 0, kind, GL.GL_UNSIGNED_BYTE, None)
            for k in (GL.GL_TEXTURE_MIN_FILTER, GL.GL_TEXTURE_MAG_FILTER):
                GL.glTexParameteri(GL.GL_TEXTURE_2D, k, GL.GL_NEAREST)
        GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
        GL.glBindRenderbuffer(GL.GL_RENDERBUFFER, self.stencil)
        GL.glRenderbufferStorage(GL.GL_RENDERBUFFER, GL.GL_DEPTH24_STENCIL8, self.tile, self.tile)
        for fbo, tex in ((self.fbo, self.mask), (self.pfbo, self.packed)):
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, fbo)
            GL.glFramebufferTexture2D(GL.GL_FRAMEBUFFER, GL.GL_COLOR_ATTACHMENT0, GL.GL_TEXTURE_2D, tex, 0)
            if fbo == self.fbo:
                GL.glFramebufferRenderbuffer(GL.GL_FRAMEBUFFER, GL.GL_DEPTH_STENCIL_ATTACHMENT,
                                             GL.GL_RENDERBUFFER, self.stencil)
            if GL.glCheckFramebufferStatus(GL.GL_FRAMEBUFFER) != GL.GL_FRAMEBUFFER_COMPLETE:
                raise GpuUnavailable("the GPU can't make the slicing framebuffers")
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        self._objs = [vbo, qbo]
        GL.glUseProgram(self.pack)
        GL.glUniform1i(GL.glGetUniformLocation(self.pack, "mask"), 0)
        self._u_w = GL.glGetUniformLocation(self.pack, "w")
        s = spec
        GL.glUseProgram(self.prog)
        u = lambda n: GL.glGetUniformLocation(self.prog, n)   # noqa: E731
        GL.glUniform2f(u("k"), np.float32(1000.0) / np.float32(s.pixel_x_um), np.float32(1000.0) / np.float32(s.pixel_y_um))
        GL.glUniform2f(u("o"), s.ox, s.oy)
        GL.glUniform2f(u("res"), s.res_x, s.res_y)
        GL.glUniform2i(u("mirror"), int(s.mirror_x), int(s.mirror_y))
        self._u_tile, self._u_z = u("tile"), u("zcut")
        GL.glUseProgram(0)

    def _region(self, meshes):
        """Pixel bounds (x0, y0, x1, y1) the models can cover, or None if off the image."""
        pts = [np.asarray(m.bounds)[:, :2] for m in meshes if len(m.faces)]
        if not pts:
            return None
        b = np.concatenate(pts)
        corners = np.array([[x, y] for x in (b[:, 0].min(), b[:, 0].max()) for y in (b[:, 1].min(), b[:, 1].max())])
        px = self.spec.to_pixels(corners)
        x0 = max(0, int(np.floor(px[:, 0].min())) - MARGIN_PX)
        y0 = max(0, int(np.floor(px[:, 1].min())) - MARGIN_PX)
        x1 = min(self.spec.res_x, int(np.ceil(px[:, 0].max())) + MARGIN_PX)
        y1 = min(self.spec.res_y, int(np.ceil(px[:, 1].max())) + MARGIN_PX)
        return (x0, y0, x1, y1) if x0 < x1 and y0 < y1 else None

    def tiles(self):
        x0, y0, x1, y1 = self.region
        for ty in range(y0, y1, self.tile):
            for tx in range(x0, x1, self.tile):
                yield tx, ty, min(self.tile, x1 - tx), min(self.tile, y1 - ty)

    def layer_tiles(self, z: float) -> list[Tile]:
        """The layer at height z, as read-back tiles covering the models."""
        return self._collect(self._submit(z, 0))

    def _pbos(self, slot: int) -> list[int]:
        """Pixel-pack buffers (one per tile) for in-flight layer `slot`."""
        if slot not in self._pbo:
            tiles = list(self.tiles())
            ids = list(np.atleast_1d(GL.glGenBuffers(len(tiles))))
            for pbo, (_, _, tw, th) in zip(ids, tiles):
                GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, pbo)
                GL.glBufferData(GL.GL_PIXEL_PACK_BUFFER, packed_width(tw) * th, None, GL.GL_STREAM_READ)
            GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)
            self._pbo[slot] = [int(x) for x in ids]
        return self._pbo[slot]

    def _collect(self, pending) -> list[Tile]:
        """Wait for a submitted layer's tiles and copy them out of their buffers."""
        out = []
        for pbo, (tx, ty, tw, th) in pending:
            size = packed_width(tw) * th
            buf = np.empty(1 + size, np.uint8)
            buf[0] = 0
            GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, pbo)
            ptr = _raw_map_range(GL.GL_PIXEL_PACK_BUFFER, 0, size, GL.GL_MAP_READ_BIT)
            if not ptr:
                GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)
                raise GpuUnavailable("couldn't read the layer back from the GPU")
            ctypes.memmove(buf.ctypes.data + 1, ptr, size)
            _raw_unmap(GL.GL_PIXEL_PACK_BUFFER)
            out.append(Tile(buf, tw, th, tx, ty))
        GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)
        return out

    def _submit(self, z: float, slot: int):
        """Draw the layer at height z and start reading it back into slot's buffers, without
        waiting: [(pbo, tile)] for _collect."""
        if not self.count or self.region is None:
            return []
        out = []
        pbos = self._pbos(slot)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDisable(GL.GL_BLEND)
        GL.glDisable(GL.GL_MULTISAMPLE)
        GL.glEnable(GL.GL_CLIP_DISTANCE0)
        GL.glPixelStorei(GL.GL_PACK_ALIGNMENT, 1)
        GL.glPixelStorei(GL.GL_PACK_ROW_LENGTH, 0)
        GL.glPixelStorei(GL.GL_PACK_SKIP_PIXELS, 0)
        for pbo, (tx, ty, tw, th) in zip(pbos, self.tiles()):
            GL.glViewport(0, 0, tw, th)
            GL.glClearColor(0, 0, 0, 0)
            GL.glClearStencil(0)
            GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_STENCIL_BUFFER_BIT)
            # winding number into the stencil
            GL.glEnable(GL.GL_STENCIL_TEST)
            GL.glColorMask(GL.GL_FALSE, GL.GL_FALSE, GL.GL_FALSE, GL.GL_FALSE)
            GL.glStencilFunc(GL.GL_ALWAYS, 0, 0xFF)
            GL.glStencilOpSeparate(GL.GL_FRONT, GL.GL_KEEP, GL.GL_KEEP, GL.GL_INCR_WRAP)
            GL.glStencilOpSeparate(GL.GL_BACK, GL.GL_KEEP, GL.GL_KEEP, GL.GL_DECR_WRAP)
            GL.glUseProgram(self.prog)
            GL.glUniform4f(self._u_tile, tx, ty, tw, th)
            GL.glUniform1f(self._u_z, z)
            GL.glBindVertexArray(self.vao)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, self.count)
            # non-zero winding -> resin
            GL.glDisable(GL.GL_CLIP_DISTANCE0)
            GL.glColorMask(GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE)
            GL.glStencilFunc(GL.GL_NOTEQUAL, 0, 0xFF)
            GL.glStencilOp(GL.GL_KEEP, GL.GL_KEEP, GL.GL_KEEP)
            GL.glUseProgram(self.fill)
            GL.glBindVertexArray(self.quad)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glDisable(GL.GL_STENCIL_TEST)
            # eight pixels a byte, then read back
            pw = packed_width(tw)
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.pfbo)
            GL.glViewport(0, 0, pw, th)
            GL.glUseProgram(self.pack)
            GL.glUniform1i(self._u_w, tw)
            GL.glActiveTexture(GL.GL_TEXTURE0)
            GL.glBindTexture(GL.GL_TEXTURE_2D, self.mask)
            GL.glDrawArrays(GL.GL_TRIANGLE_STRIP, 0, 4)
            GL.glBindTexture(GL.GL_TEXTURE_2D, 0)
            GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, pbo)
            _raw_read_pixels(0, 0, pw, th, GL.GL_RED_INTEGER, GL.GL_UNSIGNED_BYTE, ctypes.c_void_p(0))
            GL.glBindBuffer(GL.GL_PIXEL_PACK_BUFFER, 0)
            out.append((pbo, (tx, ty, tw, th)))
            GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.fbo)
            GL.glEnable(GL.GL_CLIP_DISTANCE0)
        GL.glDisable(GL.GL_CLIP_DISTANCE0)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, 0)
        return out

    def bitmap(self, z: float) -> np.ndarray:
        """The whole layer as a (res_y, res_x) uint8 image (tests, small specs)."""
        img = np.zeros((self.spec.res_y, self.spec.res_x), np.uint8)
        for t in self.layer_tiles(z):
            img[t.y0:t.y0 + t.h, t.x0:t.x0 + t.w] = t.image()
        return img

    def iter_rle(self, zs, workers: int | None = None, ahead: int = 6,
                 progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None, cancel_exc=Exception) -> Iterator[bytes]:
        """Encoded layers in order. The GPU draws on this thread; turning a layer's pixels into
        runs happens on `workers` threads (numpy releases the GIL) up to `ahead` layers behind."""
        n = len(zs)
        workers = workers if workers is not None else max(1, min(8, (os.cpu_count() or 2) - 1))
        pending: deque = deque()

        def encode(tiles):
            return runs_from_tiles([spans_from_tile(t) for t in tiles], self.spec)
        done = 0
        with ThreadPoolExecutor(max_workers=workers) as ex:
            inflight = None                          # the GPU draws layer i while layer i-1 is copied out
            for i, z in enumerate(list(zs) + [None]):
                if cancelled and cancelled():
                    raise cancel_exc()
                nxt = self._submit(float(z), i % 2) if z is not None else None
                if inflight is not None:
                    pending.append(ex.submit(encode, self._collect(inflight)))
                inflight = nxt
                while len(pending) > ahead:
                    yield pending.popleft().result()
                    done += 1
                    if progress and done % 8 == 0:
                        progress(done, n)
            while pending:
                if cancelled and cancelled():
                    raise cancel_exc()
                yield pending.popleft().result()
                done += 1
                if progress and (done % 8 == 0 or done == n):
                    progress(done, n)

    def close(self) -> None:
        if not self._objs:
            return
        for ids in self._pbo.values():
            GL.glDeleteBuffers(len(ids), ids)
        self._pbo = {}
        GL.glDeleteFramebuffers(2, [self.fbo, self.pfbo])
        GL.glDeleteRenderbuffers(1, [self.stencil])
        GL.glDeleteTextures(2, [self.mask, self.packed])
        GL.glDeleteBuffers(len(self._objs), self._objs)
        GL.glDeleteVertexArrays(2, [self.vao, self.quad])
        GL.glDeleteProgram(self.prog)
        GL.glDeleteProgram(self.fill)
        GL.glDeleteProgram(self.pack)
        self._objs = []
