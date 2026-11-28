"""3D viewport: QOpenGLWidget + GLSL 3.3 core (PyOpenGL calls)."""
from __future__ import annotations

import ctypes
import math

import numpy as np
from OpenGL import GL
# Qt owns the GL context (EGL or GLX). PyOpenGL's wrapped glVertexAttribPointer records the
# pointer per context and fails when PyOpenGL's platform differs from Qt's; we only pass
# VBO offsets, so use the raw entry point.
from OpenGL.raw.GL.VERSION.GL_2_0 import glVertexAttribPointer as _raw_attrib_pointer
from typing import NamedTuple

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF, QSurfaceFormat, QTransform
from PySide6.QtOpenGL import QOpenGLFramebufferObject, QOpenGLFramebufferObjectFormat, QOpenGLPaintDevice
from PySide6.QtOpenGLWidgets import QOpenGLWidget
from PySide6.QtWidgets import QToolTip

from gooslicer.camera import OrbitCamera, axis_gizmo, ray_plane
from gooslicer.scene import Part, PartState, Scene, rotation_matrix

from . import colors as colormod
from .handles import HandlesMixin
from .layerslider import LayerSlider

# Meshes are drawn indexed (shared vertices, 12 bytes each, plus 12 bytes a face). Flat shading
# needs a face normal: the fragment shader takes it from the screen-space derivatives of the
# world position, which lie in the triangle's plane. It faces the viewer, which for a front face
# is the outward normal; back faces are painted a fixed colour anyway.
# Zoomed out, a detailed model's triangles get smaller than a pixel and each pixel shows one
# random facet: a speckle of light and dark. Meshes uploaded with smooth_attributes carry three
# ever-smoother normals (locations 1–3) and the local edge length (location 4); as a pixel grows
# past the edge length the shading steps through them, like mipmaps. Up close it stays
# flat-shaded. Buffers without them read the default (0, 0, 0, 1): edge 0, face normals only.
MESH_VS = """#version 330 core
layout(location=0) in vec3 pos;
layout(location=1) in vec3 n1; layout(location=2) in vec3 n2; layout(location=3) in vec3 n3;
layout(location=4) in float edge;
uniform mat4 mvp; uniform mat4 model; uniform float clipZ;
out vec3 vW; out vec3 nA; out vec3 nB; out vec3 nC; out float edgeW;
void main(){ vW = (model * vec4(pos, 1.0)).xyz; gl_Position = mvp * vec4(pos, 1.0);
             mat3 m = mat3(model);
             mat3 nm = transpose(inverse(m));                // outward, mirrored parts too
             nA = nm * n1; nB = nm * n2; nC = nm * n3;
             edgeW = edge * pow(abs(determinant(m)), 1.0 / 3.0);
             gl_ClipDistance[0] = clipZ - vW.z; }  // layer slider: nothing above clipZ
"""
MESH_FS = """#version 330 core
in vec3 vW; in vec3 nA; in vec3 nB; in vec3 nC; in float edgeW; out vec4 frag;
uniform vec3 color; uniform vec3 lightA; uniform vec3 lightB; uniform float alpha;
uniform vec3 backColor;
void main(){
  if (!gl_FrontFacing) { frag = vec4(backColor, alpha); return; }  // inside/back faces
  vec3 n = normalize(cross(dFdx(vW), dFdy(vW)));
  if (edgeW > 0.0) {
    float r = max(length(dFdx(vW)), length(dFdy(vW))) / edgeW; // a pixel, in edge lengths
    vec3 f = n;
    vec3 s = n;
    if (dot(nA, nA) > 0.25) s = normalize(mix(s, normalize(nA), smoothstep(0.35, 1.5, r)));
    if (dot(nB, nB) > 0.25) s = normalize(mix(s, normalize(nB), smoothstep(1.5, 3.5, r)));
    if (dot(nC, nC) > 0.25) s = normalize(mix(s, normalize(nC), smoothstep(3.5, 8.0, r)));
    n = normalize(mix(f, s, clamp(dot(s, f) * 4.0, 0.0, 1.0)));  // never turn away from the eye
  }
  float d = 0.30 + 0.55 * max(dot(n, lightA), 0.0) + 0.25 * max(dot(n, lightB), 0.0);
  frag = vec4(color * d, alpha);
}
"""
LINE_VS = """#version 330 core
layout(location=0) in vec3 pos; uniform mat4 mvp; uniform float clipZ;
uniform vec2 pxOff;   // a shift in NDC: lines drawn twice, a pixel apart, are 2 pixels wide
void main(){ gl_Position = mvp * vec4(pos, 1.0); gl_Position.xy += pxOff * gl_Position.w;
             gl_ClipDistance[0] = clipZ - pos.z; }  // world pos
"""
LINE_FS = """#version 330 core
out vec4 frag; uniform vec4 color;
void main(){ frag = color; }
"""

# models, selected models, supports and flaw lines/labels take their colours from
# Viewport.colors (colors.py: Edit → Preferences → 3D view colors)
COL_OUTSIDE = (0.90, 0.25, 0.22)
COL_ISLAND = (0.92, 0.16, 0.14)       # unsupported islands, after the island check
BACK_FACE = (0.85, 0.15, 0.55)         # inside / back faces (no layer cut)
OPAQUE = 0.999                         # an opacity at least this is drawn solid
GUIDE_DIM = 0.35                       # island guide lines where something is in front of them
COL_HOLE = (0.15, 0.45, 1.0)           # drain-hole preview (blue)
HOLE_ALPHA = 0.75                      # its visible part (less and orange parts turn it grey)
HOLE_ALPHA_HIDDEN = 0.4                # its part inside the model, through the surface
CAVITY_XRAY = 0.45                     # sealed cavities are inside the model: shown through it
LABEL_ISLAND = "Unsupported island"    # text at the outer end of each guide line
LABEL_CAVITY = "Sealed cavity"
LABEL_CUP = "Suction cup"
LABEL_GAP_PX = 6                       # between a guide line's end and its label


class GuideLabel(NamedTuple):
    rect: QRectF                       # widget coordinates
    text: str
    keys: tuple                        # the flaws it stands for: ("island" | "cavity", number)
    selected: bool


def label_text(kind_text: str, numbers: list[int]) -> str:
    """'Unsupported island 14', or for lines that end together 'Unsupported islands 3, 4'."""
    numbers = sorted(numbers)
    if len(numbers) == 1:
        return f"{kind_text} {numbers[0]}"
    shown = ", ".join(str(n) for n in numbers[:3])
    more = f" +{len(numbers) - 3}" if len(numbers) > 3 else ""
    return f"{kind_text}s {shown}{more}"


def island_guides(points, size) -> np.ndarray:
    """Line segments (2 rows each) from each island point outward to the edge of the build
    area, level with the island: away from the plate centre (an island at the centre points to
    the front, −Y), so a small island can be found from any side."""
    hx, hy = float(size[0]) / 2, float(size[1]) / 2
    out = []
    for p in points:
        p = np.asarray(p, float)
        d = p[:2].copy()
        n = float(np.hypot(*d))
        d = d / n if n > 1e-6 else np.array([0.0, -1.0])
        t = min(hx / abs(d[0]) if abs(d[0]) > 1e-9 else np.inf, hy / abs(d[1]) if abs(d[1]) > 1e-9 else np.inf)
        end = np.array([d[0] * t, d[1] * t, p[2]])
        if t > n:                       # inside the build area: draw it
            out += [p, end]
    return np.array(out, float).reshape(-1, 3)


def default_format() -> QSurfaceFormat:
    f = QSurfaceFormat()
    f.setVersion(3, 3)
    f.setProfile(QSurfaceFormat.CoreProfile)
    f.setDepthBufferSize(24)
    f.setStencilBufferSize(8)            # see-through parts: where their own surface is nearest
    # no multisampling: the view supersamples instead (Viewport.paintGL). Multisampled, an AMD
    # RX 9070 XT (RDNA 4, Mesa 26.2) drops triangles much smaller than a pixel, so zoomed-out
    # detailed models broke up and vanished; a closed 15 mm sphere disappeared entirely
    f.setSamples(0)
    return f


SUPERSAMPLE = 2          # the 3D view draws at 2× width and height, then scales down 2:1


def _compile(vs: str, fs: str) -> int:
    def sh(src, kind):
        s = GL.glCreateShader(kind)
        GL.glShaderSource(s, src)
        GL.glCompileShader(s)
        if not GL.glGetShaderiv(s, GL.GL_COMPILE_STATUS):
            raise RuntimeError(GL.glGetShaderInfoLog(s).decode())
        return s
    p = GL.glCreateProgram()
    a, b = sh(vs, GL.GL_VERTEX_SHADER), sh(fs, GL.GL_FRAGMENT_SHADER)
    GL.glAttachShader(p, a)
    GL.glAttachShader(p, b)
    GL.glLinkProgram(p)
    if not GL.glGetProgramiv(p, GL.GL_LINK_STATUS):
        raise RuntimeError(GL.glGetProgramInfoLog(p).decode())
    GL.glDeleteShader(a)
    GL.glDeleteShader(b)
    return p


class _Buffer:
    """VAO + VBO of float32 xyz points, with an optional uint32 index buffer (then `count` is
    the number of indices and draw() uses glDrawElements)."""

    def __init__(self, data: np.ndarray, with_normals: bool = False, indices: np.ndarray | None = None,
                 smooth: np.ndarray | None = None):
        data = np.ascontiguousarray(data, dtype=np.float32)
        self.count = len(data)
        self.vao = GL.glGenVertexArrays(1)
        self.vbo = GL.glGenBuffers(1)
        self.ebo = None
        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
        GL.glEnableVertexAttribArray(0)
        _raw_attrib_pointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, data.shape[1] * 4, ctypes.c_void_p(0))
        self.sbo = None
        if smooth is not None and len(smooth) == len(data):          # locations 1–4 (MESH_VS)
            smooth = np.ascontiguousarray(smooth, dtype=np.uint8)
            stride = smooth.shape[1]
            self.sbo = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.sbo)
            GL.glBufferData(GL.GL_ARRAY_BUFFER, smooth.nbytes, smooth, GL.GL_STATIC_DRAW)
            for loc in (1, 2, 3):                                     # signed normalized bytes
                GL.glEnableVertexAttribArray(loc)
                _raw_attrib_pointer(loc, 3, GL.GL_BYTE, GL.GL_TRUE, stride, ctypes.c_void_p(4 * (loc - 1)))
            GL.glEnableVertexAttribArray(4)
            _raw_attrib_pointer(4, 1, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(12))
        if indices is not None:
            indices = np.ascontiguousarray(indices, dtype=np.uint32)
            self.ebo = GL.glGenBuffers(1)
            GL.glBindBuffer(GL.GL_ELEMENT_ARRAY_BUFFER, self.ebo)       # recorded in the VAO
            GL.glBufferData(GL.GL_ELEMENT_ARRAY_BUFFER, indices.nbytes, indices, GL.GL_STATIC_DRAW)
            self.count = indices.size
        GL.glBindVertexArray(0)

    def draw(self, mode=GL.GL_TRIANGLES):
        GL.glBindVertexArray(self.vao)
        if self.ebo is not None:
            GL.glDrawElements(mode, self.count, GL.GL_UNSIGNED_INT, ctypes.c_void_p(0))
        else:
            GL.glDrawArrays(mode, 0, self.count)

    def delete(self):
        GL.glDeleteBuffers(1, [self.vbo])
        if self.sbo is not None:
            GL.glDeleteBuffers(1, [self.sbo])
        if self.ebo is not None:
            GL.glDeleteBuffers(1, [self.ebo])
        GL.glDeleteVertexArrays(1, [self.vao])


def mesh_buffer(mesh) -> _Buffer:
    """A mesh's indexed GPU buffer, from the float32/uint32 arrays gpu_arrays() keeps on it
    (made on a worker thread where possible)."""
    from gooslicer.scene import GPU_KEY, gpu_arrays
    v, f, sm = gpu_arrays(mesh)
    buf = _Buffer(v, indices=f, smooth=sm)
    mesh._cache.cache.pop(GPU_KEY, None)          # on the GPU now: free the copy
    return buf


def plate_lines(size, step: float = 10.0) -> tuple[np.ndarray, np.ndarray]:
    sx, sy, sz = size
    hx, hy = sx / 2, sy / 2
    grid = []
    for x in np.arange(0, hx + 1e-9, step):
        for s in ((x,) if x == 0 else (x, -x)):
            grid += [(s, -hy, 0), (s, hy, 0)]
    for y in np.arange(0, hy + 1e-9, step):
        for s in ((y,) if y == 0 else (y, -y)):
            grid += [(-hx, s, 0), (hx, s, 0)]
    c = [(-hx, -hy), (hx, -hy), (hx, hy), (-hx, hy)]
    box = []
    for i in range(4):
        (x0, y0), (x1, y1) = c[i], c[(i + 1) % 4]
        box += [(x0, y0, 0), (x1, y1, 0), (x0, y0, sz), (x1, y1, sz), (x0, y0, 0), (x0, y0, sz)]
    return np.array(grid, float), np.array(box, float)


# FRONT label on the floor in front of the plate. GooSlicer's front is −Y: the Front view and
# the default camera look from there, and slices match SatelLite's (05-slicing-spec §11).
FRONT_TEXT = "FRONT"
FRONT_ASPECT = 4.6       # label width / height
PLATE_BTN_R = 9.0        # the − and + beside a plate label: radius in px
FRONT_FONT_PX = 64       # drawing resolution of the label before it is projected


def _area(q) -> float:
    return 0.5 * sum(q[i].x() * q[i - 1].y() - q[i - 1].x() * q[i].y() for i in range(len(q)))


COMPASS_RADIUS = 42      # px, logical
COMPASS_MARGIN = 10
COMPASS_COLORS = {"X": "#e5534b", "Y": "#7ac74f", "Z": "#4c9be8"}   # the usual red/green/blue

# Rotation rings around the selection: drag one to turn about that axis in ROTATE_STEP steps.
ROTATE_STEP = 5.0         # degrees
RING_GRAB_PX = 8.0        # how close to a ring a press must be to grab it
RING_SEGMENTS = 120
RING_EDGE_ON = 0.17       # |cos| between view and axis below which a ring (seen edge-on) is hidden
# per axis: the plane the ring lies in (u, w), with u × w = the axis (positive turn takes u to w)
RING_BASIS = {0: (np.array([0.0, 1, 0]), np.array([0.0, 0, 1])),
              1: (np.array([0.0, 0, 1]), np.array([1.0, 0, 0])),
              2: (np.array([1.0, 0, 0]), np.array([0.0, 1, 0]))}


def snap_angle(deg: float, step: float = ROTATE_STEP) -> float:
    return float(np.round(deg / step) * step) + 0.0


class Viewport(HandlesMixin, QOpenGLWidget):
    selectionChanged = Signal(list)                # [part ids]
    moveFinished = Signal(object)                  # {id: (before PartState, after PartState)}
    layFlatPicked = Signal(int, int)               # part id, local face index
    holePicked = Signal(int, int, object)          # part id, local face index, world hit point
    holeModeEnded = Signal()
    escapePressed = Signal()                       # Esc with no mode or drag to cancel
    supportClicked = Signal(object, object, bool)  # ray origin, ray direction, remove (Ctrl)
    supportDragged = Signal(object, object, object, object)  # start ray (o, d), drop ray (o, d)
    supportModeEnded = Signal()
    filesDropped = Signal(list)
    rotateFinished = Signal(object, str)           # {id: (before, after)}, undo text
    partContextMenu = Signal(int, object)          # part id, global QPoint (right click on a part)
    guideSelectionChanged = Signal(object)         # set of flaw keys, after a label click
    handleModeChanged = Signal(str)                # "move", "rotate" or "scale" (handles.py)
    transformFinished = Signal(object, str)        # a handle drag: {id: (before, after)}, undo text
    transformLive = Signal()                       # parts changed during a handle drag
    plateAddRequested = Signal()                   # the + beside a plate's label (11 §3)
    plateDeleteRequested = Signal(int)             # the − beside plate i's label
    plateLabelClicked = Signal(int)                # a click on "Plate N": make it current

    def __init__(self, scene: Scene, parent=None):
        super().__init__(parent)
        self.setFormat(default_format())
        self.setFocusPolicy(Qt.StrongFocus)
        self.setAcceptDrops(True)
        self.setMinimumSize(320, 240)
        self.scene = scene
        self.camera = OrbitCamera()
        self.selected: list[int] = []
        self.lay_flat_mode = False
        self.hole_mode = False
        self.support_mode = False
        self._spress = None             # support mode: a left press waiting to become a click or a drag
        self.support_drag_marker = None  # world point a dragged contact would move to
        # drain-hole preview: hole_preview_maker(part, face, hit) -> world trimesh or None; the
        # last hover is kept so the preview can follow settings changes
        self.hole_preview_maker = None
        self.hole_preview = None
        self.cut_preview = None                    # the Cut panel's plane (11 §2), world trimesh
        self._hole_hover = None
        self._hole_pos = None
        self._hole_pending = None       # the latest cursor spot, picked once the queued moves are in
        self._hole_timer = QTimer(self)
        self._hole_timer.setSingleShot(True)
        self._hole_timer.setInterval(0)
        self._hole_timer.timeout.connect(self._hover_pending)
        self.colors = colormod.default_colors()     # key -> QColor with alpha (colors.VIEW_COLORS)
        self.show_compass = True
        self.show_front = True
        self.show_rings = True                     # rotation rings around the selection
        self._rot = None                           # an active ring drag
        self._ring_hover = None                    # axis index under the mouse
        self._rpress = None                        # right-button press: a click opens the part menu
        self.setMouseTracking(True)
        self.extra_meshes = lambda: []             # [(trimesh, 4x4 world matrix)] drawn as supports
        self.island_meshes: list = []              # world-space trimeshes drawn red over the model
        self.island_lines = np.zeros((0, 3))       # segments from each island to the build area's edge
        self.cavity_meshes: list = []              # world-space trimeshes lining sealed cavities (red)
        self.cavity_lines = np.zeros((0, 3))       # segments from each cavity to the build area's edge
        self.island_numbers: list | None = None    # flaw number of each island segment (None: 1, 2, …)
        self.cavity_numbers: list | None = None
        self.cavity_cups: set = set()             # cavity numbers that are suction cups
        self._guide_buf = None
        self._guide_src = None
        self.selected_guides: set = set()          # guide keys picked by clicking their label
        self._sel_buf = None
        self._sel_src = None
        self._label_hover = False
        self._buffers: dict[int, tuple[object, _Buffer]] = {}  # id(mesh) -> (mesh, buffer)
        self._plate: tuple | None = None
        self._plate_size = None
        self._gl_ready = False
        self._drag = None
        self._last = QPointF()
        self._init_handles()
        self.clip_z: float | None = None           # layer slider: hide everything above (mm)
        self.current_plate = 0                     # drawn brighter; Reset view frames it (11 §3)
        self.layer_slider = LayerSlider(self)
        self.layer_slider.cutChanged.connect(self._set_clip)
        self.layer_slider.hide()
        self.reset_view()

    # ------------------------------------------------------------- public
    def reset_view(self):
        """Frame the current plate from the front, a little to the right and above."""
        sx, sy, sz = self.scene.size
        cx = float(self.scene.plate_offset(min(self.current_plate, self.scene.plates - 1))[0])
        self.camera.frame([cx - sx / 2, -sy / 2, 0], [cx + sx / 2, sy / 2, sz * 0.4])
        self.camera.yaw, self.camera.pitch = -90.0 + 25.0, 28.0
        self.update()

    def frame_all_plates(self):
        sx, sy, sz = self.scene.size
        last = float(self.scene.plate_offset(self.scene.plates - 1)[0])
        self.camera.frame([-sx / 2, -sy / 2, 0], [last + sx / 2, sy / 2, sz * 0.4])
        self.update()

    def set_view(self, yaw: float, pitch: float):
        self.camera.yaw, self.camera.pitch = yaw, pitch
        self.update()

    def frame_selection(self):
        parts = [self.scene.get(i) for i in self.selected] or self.scene.parts
        parts = [p for p in parts if p]
        if not parts:
            return self.reset_view()
        b = np.array([p.bounds() for p in parts])
        self.camera.frame(b[:, 0].min(axis=0), b[:, 1].max(axis=0))
        self.update()

    def set_selection(self, ids: list[int], emit: bool = True):
        ids = [i for i in ids if self.scene.get(i)]
        if ids != self.selected:
            self.selected = ids
            if emit:
                self.selectionChanged.emit(list(ids))
        self.update()

    # ------------------------------------------------------------ layer slider
    def set_layer_range(self, top: float | None, step: float) -> None:
        """The layer slider runs from 0 to `top` mm (the highest part) in `step` mm; no top
        (an empty plate) hides it and shows everything."""
        if top is None or top <= 0:
            self.layer_slider.set_cut(None)
            self.layer_slider.hide()
            return
        self.layer_slider.set_range(top, step)
        self._place_layer_slider()
        self.layer_slider.show()

    def _place_layer_slider(self) -> None:
        r = self.compass_rect()
        y = int(r.bottom()) + 14
        w = self.layer_slider.width()
        self.layer_slider.setGeometry(int(r.right()) - w + 4, y, w, max(self.height() - y - 40, 60))

    def _set_clip(self, z) -> None:
        self.clip_z = z
        self.update()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._place_layer_slider()

    # --------------------------------------------------------------- GL
    def initializeGL(self):
        self._mesh_prog = _compile(MESH_VS, MESH_FS)
        self._line_prog = _compile(LINE_VS, LINE_FS)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_MULTISAMPLE)
        self._gl_ready = True
        self.context().aboutToBeDestroyed.connect(self._cleanup)

    def _cleanup(self):
        self.makeCurrent()
        self._ss_fbo = None                 # its GL objects go with the context
        for _, b in self._buffers.values():
            b.delete()
        self._buffers.clear()
        if self._guide_buf is not None:
            self._guide_buf.delete()
            self._guide_buf, self._guide_src = None, None
        if self._sel_buf is not None:
            self._sel_buf.delete()
            self._sel_buf, self._sel_src = None, None
        if self._plate:
            for b in self._plate:
                b.delete()
            self._plate = None
        self.doneCurrent()

    def _sync_buffers(self):
        live = {id(p.mesh): p.mesh for p in self.scene.parts}
        self._extras = list(self.extra_meshes())
        live.update({id(m): m for m, _ in self._extras})
        live.update({id(m): m for m in self.island_meshes})
        live.update({id(m): m for m in self.cavity_meshes})
        if self.hole_preview is not None:
            live[id(self.hole_preview)] = self.hole_preview
        if self.cut_preview is not None:
            live[id(self.cut_preview)] = self.cut_preview
        for k in [k for k in self._buffers if k not in live]:
            self._buffers.pop(k)[1].delete()
        for k, m in live.items():
            if k not in self._buffers:
                self._buffers[k] = (m, mesh_buffer(m))
        if self._plate_size is None or not np.allclose(self._plate_size, self.scene.size):
            if self._plate:
                for b in self._plate:
                    b.delete()
            grid, box = plate_lines(self.scene.size)
            sx, sy = self.scene.size[:2] / 2
            quad = np.array([(-sx, -sy, -0.01), (sx, -sy, -0.01), (sx, sy, -0.01),
                             (-sx, -sy, -0.01), (sx, sy, -0.01), (-sx, sy, -0.01)])
            self._plate = (_Buffer(quad, False), _Buffer(grid, False), _Buffer(box, False))
            self._plate_size = self.scene.size.copy()

    def _ss_target(self):
        """The offscreen buffer a frame is drawn into, SUPERSAMPLE× the widget's pixels each way
        (no multisampling), and a paint device for QPainter on it; (None, self) to draw on the
        widget directly when the buffer can't be made. Scaled down 2:1 with linear filtering,
        each pixel is the average of 4: smooth edges, and the lighting of a model's sub-pixel
        triangles averaged rather than one facet picked at random (zoomed out, a speckle)."""
        if getattr(self, "_ss_failed", False):
            return None, self
        dpr = self.devicePixelRatioF()
        W = max(1, int(round(self.width() * dpr))) * SUPERSAMPLE
        H = max(1, int(round(self.height() * dpr))) * SUPERSAMPLE
        fbo = getattr(self, "_ss_fbo", None)
        if fbo is None or fbo.width() != W or fbo.height() != H:
            fmt = QOpenGLFramebufferObjectFormat()
            fmt.setAttachment(QOpenGLFramebufferObject.CombinedDepthStencil)
            fmt.setSamples(0)
            fbo = QOpenGLFramebufferObject(W, H, fmt)
            if not fbo.isValid():
                self._ss_failed, self._ss_fbo = True, None
                return None, self
            self._ss_fbo = fbo
            self._ss_dev = QOpenGLPaintDevice(W, H)
        self._ss_dev.setDevicePixelRatio(dpr * SUPERSAMPLE)
        return fbo, self._ss_dev

    def _draw_lines(self, count: int) -> None:
        """GL_LINES with the line program bound. Supersampled, a 1-pixel line would come out
        half a pixel wide and faint (core profile: no glLineWidth above 1), so it's drawn twice,
        one buffer pixel apart diagonally: a pixel wide once scaled down, as before."""
        loc = GL.glGetUniformLocation(self._line_prog, "pxOff")
        GL.glUniform2f(loc, 0.0, 0.0)
        GL.glDrawArrays(GL.GL_LINES, 0, count)
        fbo = getattr(self, "_ss_fbo", None)
        if fbo is not None and not getattr(self, "_ss_failed", False):
            GL.glUniform2f(loc, 2.0 / fbo.width(), 2.0 / fbo.height())
            GL.glDrawArrays(GL.GL_LINES, 0, count)
            GL.glUniform2f(loc, 0.0, 0.0)

    def _ss_resolve(self, fbo) -> None:
        """Scale the supersampled frame down onto the widget."""
        dpr = self.devicePixelRatioF()
        w, h = max(1, int(round(self.width() * dpr))), max(1, int(round(self.height() * dpr)))
        GL.glBindFramebuffer(GL.GL_READ_FRAMEBUFFER, fbo.handle())
        GL.glBindFramebuffer(GL.GL_DRAW_FRAMEBUFFER, self.defaultFramebufferObject())
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glBlitFramebuffer(0, 0, fbo.width(), fbo.height(), 0, 0, w, h, GL.GL_COLOR_BUFFER_BIT, GL.GL_LINEAR)
        GL.glBindFramebuffer(GL.GL_FRAMEBUFFER, self.defaultFramebufferObject())

    def paintGL(self):
        self._sync_buffers()
        fbo, target = self._ss_target()
        if fbo is not None:
            fbo.bind()
        GL.glClearColor(0.16, 0.17, 0.19, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        w, h = max(self.width(), 1), max(self.height(), 1)
        vp = self.camera.projection(w / h) @ self.camera.view()
        if self.show_front:
            # the FRONT label lies on the floor: painted first, into colour only, so the models
            # drawn after it hide it (painted last it showed through them)
            painter = QPainter(target)
            try:
                self._draw_front(painter, vp, w, h)
            finally:
                painter.end()
        # QPainter (the label, and the compass drawn last) leaves its own GL state behind:
        # reset ours
        if fbo is not None:
            fbo.bind()
            GL.glViewport(0, 0, fbo.width(), fbo.height())
        else:
            dpr = self.devicePixelRatioF()
            GL.glViewport(0, 0, int(round(w * dpr)), int(round(h * dpr)))
        GL.glDisable(GL.GL_SCISSOR_TEST)
        GL.glDisable(GL.GL_STENCIL_TEST)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glColorMask(GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glDepthFunc(GL.GL_LESS)
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_BLEND)
        clip = 1e9 if self.clip_z is None else float(self.clip_z) + 1e-4
        for prog in (self._mesh_prog, self._line_prog):
            GL.glUseProgram(prog)
            GL.glUniform1f(GL.glGetUniformLocation(prog, "clipZ"), clip)
        GL.glEnable(GL.GL_CLIP_DISTANCE0)
        # inside faces: the cut colour while the layer slider cuts, else the usual magenta
        back = BACK_FACE if self.clip_z is None else colormod.rgba(self.colors["cut"])[:3]
        GL.glUseProgram(self._mesh_prog)
        GL.glUniform3f(GL.glGetUniformLocation(self._mesh_prog, "backColor"), *back)

        GL.glUseProgram(self._line_prog)
        GL.glUniformMatrix4fv(GL.glGetUniformLocation(self._line_prog, "mvp"), 1, GL.GL_TRUE,
                              vp.astype(np.float32))
        loc = GL.glGetUniformLocation(self._line_prog, "color")
        _quad, grid, box = self._plate                # the plate itself is see-through: grid only
        u_line_mvp = GL.glGetUniformLocation(self._line_prog, "mvp")
        many = self.scene.plates > 1
        for i in range(self.scene.plates):            # side by side along +X (11 §3)
            T = np.eye(4)
            T[:3, 3] = self.scene.plate_offset(i)
            GL.glUniformMatrix4fv(u_line_mvp, 1, GL.GL_TRUE, (vp @ T).astype(np.float32))
            GL.glUniform4f(loc, 0.33, 0.35, 0.39, 1.0)
            GL.glBindVertexArray(grid.vao)
            self._draw_lines(grid.count)
            if many and i == self.current_plate:
                GL.glUniform4f(loc, 0.82, 0.86, 0.95, 1.0)
            else:
                GL.glUniform4f(loc, 0.55, 0.58, 0.64, 1.0) if not many else GL.glUniform4f(loc, 0.45, 0.47, 0.52, 1.0)
            GL.glBindVertexArray(box.vao)
            self._draw_lines(box.count)
        GL.glUniformMatrix4fv(u_line_mvp, 1, GL.GL_TRUE, vp.astype(np.float32))

        GL.glUseProgram(self._mesh_prog)
        eye_dir = self.camera.eye() - self.camera.target
        eye_dir /= np.linalg.norm(eye_dir)
        GL.glUniform3f(GL.glGetUniformLocation(self._mesh_prog, "lightA"), *eye_dir)
        GL.glUniform3f(GL.glGetUniformLocation(self._mesh_prog, "lightB"), 0.0, 0.0, 1.0)
        u_mvp = GL.glGetUniformLocation(self._mesh_prog, "mvp")
        u_n = GL.glGetUniformLocation(self._mesh_prog, "model")
        u_c = GL.glGetUniformLocation(self._mesh_prog, "color")
        u_alpha = GL.glGetUniformLocation(self._mesh_prog, "alpha")
        GL.glUniform1f(u_alpha, 1.0)
        solid, clear = [], []                       # see-through parts are drawn after the rest
        for p in self.scene.parts:
            *col, a = colormod.rgba(self.colors["selected" if p.id in self.selected else "model"])
            if not self.scene.in_volume(p):
                col = COL_OUTSIDE
            if p.opacity < 1.0:                   # see-through (hollowed parts start so)
                a = min(a, max(p.opacity, 0.05))
            (solid if a >= OPAQUE else clear).append((self._buffers[id(p.mesh)][1], p.matrix(), col, a))
        self._draw_meshes(solid, vp, u_mvp, u_n, u_c, u_alpha)
        if self.island_meshes or self.cavity_meshes:
            # the unsupported / cavity surface, drawn over the part's own faces (same depth):
            # pulled forward
            GL.glUniformMatrix4fv(u_mvp, 1, GL.GL_TRUE, vp.astype(np.float32))
            GL.glUniformMatrix4fv(u_n, 1, GL.GL_TRUE, np.eye(4, dtype=np.float32))
            GL.glUniform3f(u_c, *COL_ISLAND)
            GL.glEnable(GL.GL_POLYGON_OFFSET_FILL)
            GL.glPolygonOffset(-1.0, -2.0)
            GL.glDepthFunc(GL.GL_LEQUAL)
            for m in self.island_meshes + self.cavity_meshes:
                b = self._buffers[id(m)][1]
                b.draw()
            GL.glDisable(GL.GL_POLYGON_OFFSET_FILL)
            if self.cavity_meshes:
                # a sealed cavity is hidden inside its part: show it through the walls. Its
                # faces point into the void, so culling back faces leaves one layer (the far
                # side) and the colour stays even
                GL.glEnable(GL.GL_CULL_FACE)
                GL.glCullFace(GL.GL_BACK)
                GL.glDepthFunc(GL.GL_GREATER)
                GL.glDepthMask(GL.GL_FALSE)
                GL.glEnable(GL.GL_BLEND)
                GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ZERO, GL.GL_ONE)
                GL.glUniform1f(u_alpha, CAVITY_XRAY)
                for m in self.cavity_meshes:
                    b = self._buffers[id(m)][1]
                    b.draw()
                GL.glUniform1f(u_alpha, 1.0)
                GL.glDisable(GL.GL_BLEND)
                GL.glDisable(GL.GL_CULL_FACE)
                GL.glDepthMask(GL.GL_TRUE)
            GL.glDepthFunc(GL.GL_LESS)
        self._draw_guides(vp)
        GL.glUseProgram(self._mesh_prog)
        *scol, sa = colormod.rgba(self.colors["support"])
        sup = [(self._buffers[id(m)][1], M, scol, sa) for m, M in self._extras]
        # supports first when solid (they hide what's behind them), last when see-through
        self._draw_meshes(sup if sa >= OPAQUE else [], vp, u_mvp, u_n, u_c, u_alpha, cull=True)
        self._draw_meshes(clear, vp, u_mvp, u_n, u_c, u_alpha, layered=True)
        self._draw_meshes(sup if sa < OPAQUE else [], vp, u_mvp, u_n, u_c, u_alpha)
        self._draw_selected_guides(vp)
        if self.hole_preview is not None:
            self._draw_hole_preview(vp)
        if self.cut_preview is not None:
            self._draw_hole_preview(vp, self.cut_preview, cull=False)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        GL.glDisable(GL.GL_CLIP_DISTANCE0)          # QPainter draws next
        if fbo is not None:
            fbo.bind()
        painter = QPainter(target)
        try:
            if self.show_compass:
                self._draw_compass(painter)
            self._draw_guide_labels(painter, vp, w, h)
            if self.show_front:
                self._draw_plate_buttons(painter, vp, w, h)
            self._draw_rings(painter, vp, w, h)
            self._draw_handles(painter, vp)
            if self.support_drag_marker is not None:
                self._draw_drag_marker(painter, vp)
        finally:
            painter.end()
        if fbo is not None:
            self._ss_resolve(fbo)

    def _draw_meshes(self, items, vp, u_mvp, u_n, u_c, u_alpha, cull: bool = False,
                     layered: bool = False) -> None:
        """Draw (buffer, 4x4 matrix, rgb, opacity) items with the mesh program. See-through ones
        (opacity < 1) show front faces only and blend. Unlayered (supports) they write no depth,
        so everything behind them stays visible. `layered` (see-through parts) draws each part
        back to front so its nearest surface is always on top at exactly its opacity: a depth
        pre-pass finds that surface, the faces behind it (a hollow's inside walls) go first,
        then the nearest surface over them. Without it, unsorted faces paint inner walls over
        the outer skin and patches of the model look fully transparent. The pre-pass also marks
        (stencil) where the part's surface is in front of everything drawn so far; the faces
        behind are drawn only there, or they'd paint over a solid part in front of it (a kept
        hole pin looked as if it were behind its model)."""
        if not items:
            return
        see_through = items[0][3] < OPAQUE
        if see_through:
            GL.glEnable(GL.GL_BLEND)
            GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ZERO, GL.GL_ONE)  # stays opaque
            GL.glDepthMask(GL.GL_FALSE)
        if see_through or cull:
            GL.glEnable(GL.GL_CULL_FACE)
            GL.glCullFace(GL.GL_BACK)
        layered = layered and see_through
        if layered:                                 # farthest part first
            items = sorted(items, key=lambda it: -float((vp @ it[1][:, 3])[2] / max(abs((vp @ it[1][:, 3])[3]), 1e-9)))
        for buf, M, col, a in items:
            GL.glUniformMatrix4fv(u_mvp, 1, GL.GL_TRUE, (vp @ M).astype(np.float32))
            GL.glUniformMatrix4fv(u_n, 1, GL.GL_TRUE, M.astype(np.float32))
            GL.glUniform3f(u_c, *col)
            GL.glUniform1f(u_alpha, min(a, 1.0) if see_through else 1.0)
            # mirrored transforms flip winding; keep front faces correct
            GL.glFrontFace(GL.GL_CW if np.linalg.det(M[:3, :3]) < 0 else GL.GL_CCW)
            if not layered:
                buf.draw()
                continue
            GL.glClearStencil(0)
            GL.glClear(GL.GL_STENCIL_BUFFER_BIT)
            GL.glEnable(GL.GL_STENCIL_TEST)
            GL.glStencilMask(0xFF)
            GL.glStencilFunc(GL.GL_ALWAYS, 1, 0xFF)
            GL.glStencilOp(GL.GL_KEEP, GL.GL_KEEP, GL.GL_REPLACE)   # 1 where its surface is nearest
            GL.glColorMask(GL.GL_FALSE, GL.GL_FALSE, GL.GL_FALSE, GL.GL_FALSE)
            GL.glDepthMask(GL.GL_TRUE)
            buf.draw()                              # depth of the nearest surface
            GL.glColorMask(GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE, GL.GL_TRUE)
            GL.glDepthMask(GL.GL_FALSE)
            GL.glStencilFunc(GL.GL_EQUAL, 1, 0xFF)
            GL.glStencilOp(GL.GL_KEEP, GL.GL_KEEP, GL.GL_KEEP)
            GL.glDepthFunc(GL.GL_GREATER)
            buf.draw()                              # what lies behind it (inside walls)
            GL.glDisable(GL.GL_STENCIL_TEST)
            GL.glDepthFunc(GL.GL_LEQUAL)
            buf.draw()                              # the nearest surface, on top
            GL.glDepthFunc(GL.GL_LESS)
        GL.glFrontFace(GL.GL_CCW)
        GL.glUniform1f(u_alpha, 1.0)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDisable(GL.GL_BLEND)

    def _draw_drag_marker(self, p: QPainter, vp) -> None:
        """Where a dragged support contact will go."""
        xy, ok = self._screen(self.support_drag_marker[None], vp)
        if not ok[0]:
            return
        p.setRenderHint(QPainter.Antialiasing)
        c = QPointF(*xy[0])
        p.setBrush(Qt.NoBrush)
        p.setPen(QPen(QColor("#15171a"), 3.0))
        p.drawEllipse(c, 6.0, 6.0)
        p.setPen(QPen(QColor("#ffb347"), 1.8))
        p.drawEllipse(c, 6.0, 6.0)
        p.drawPoint(c)

    def set_cut_preview(self, mesh) -> None:
        self.cut_preview = mesh
        self.update()

    def _draw_hole_preview(self, vp, mesh=None, cull: bool = True) -> None:
        """The drain hole a click would drill (or another preview solid): translucent blue,
        its part inside the model shown fainter through the surface."""
        mesh = self.hole_preview if mesh is None else mesh
        GL.glUseProgram(self._mesh_prog)
        GL.glUniformMatrix4fv(GL.glGetUniformLocation(self._mesh_prog, "mvp"), 1, GL.GL_TRUE, vp.astype(np.float32))
        GL.glUniformMatrix4fv(GL.glGetUniformLocation(self._mesh_prog, "model"), 1, GL.GL_TRUE,
                              np.eye(4, dtype=np.float32))
        GL.glUniform3f(GL.glGetUniformLocation(self._mesh_prog, "color"), *COL_HOLE)
        u_alpha = GL.glGetUniformLocation(self._mesh_prog, "alpha")
        b = self._buffers[id(mesh)][1]
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ZERO, GL.GL_ONE)
        GL.glEnable(GL.GL_CULL_FACE) if cull else GL.glDisable(GL.GL_CULL_FACE)
        GL.glCullFace(GL.GL_BACK)
        GL.glDepthMask(GL.GL_FALSE)
        for func, alpha in ((GL.GL_GREATER, HOLE_ALPHA_HIDDEN), (GL.GL_LEQUAL, HOLE_ALPHA)):
            GL.glDepthFunc(func)
            GL.glUniform1f(u_alpha, alpha)
            b.draw()
        GL.glDepthFunc(GL.GL_LESS)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glDisable(GL.GL_CULL_FACE)
        GL.glDisable(GL.GL_BLEND)
        GL.glUniform1f(u_alpha, 1.0)

    def _hover_hole(self, pos) -> None:
        """Pick under the cursor and rebuild the preview there (none off the parts)."""
        self._hole_pos = QPointF(pos)
        part, face, hit = self.scene.pick(*self._ray(pos))
        self._hole_hover = (part.id, face, hit) if part is not None else None
        self.refresh_hole_preview()

    def _hover_pending(self) -> None:
        pos, self._hole_pending = self._hole_pending, None
        if pos is not None and self.hole_mode:
            self._hover_hole(pos)

    def repick_hole(self) -> None:
        """Pick again at the cursor's last spot (the part's mesh changed under it)."""
        if self.hole_mode and self._hole_pos is not None:
            self._hover_hole(self._hole_pos)

    def clear_hole_hover(self) -> None:
        """The cursor left the model view (or went over the settings panel): no preview."""
        self._hole_hover = self._hole_pos = self._hole_pending = None
        self._hole_timer.stop()
        self.refresh_hole_preview()

    def refresh_hole_preview(self) -> None:
        """Rebuild the preview at the last hover (after a settings change or a drilled hole)."""
        prev = self.hole_preview
        self.hole_preview = None
        if self.hole_mode and self._hole_hover and self.hole_preview_maker:
            pid, face, hit = self._hole_hover
            part = self.scene.get(pid)
            if part is not None and 0 <= face < len(part.mesh.faces):
                self.hole_preview = self.hole_preview_maker(part, face, hit)
        if prev is not None or self.hole_preview is not None:
            self.update()

    def _draw_guides(self, vp) -> None:
        """White lines from each unsupported island and sealed cavity to the edge of the build
        area: solid where visible, dimmer where the model is in front, so they can always be
        followed."""
        src = (self.island_lines, self.cavity_lines)
        if self._guide_src is None or any(a is not b for a, b in zip(self._guide_src, src)):
            if self._guide_buf is not None:
                self._guide_buf.delete()
            lines = np.concatenate([self.island_lines, self.cavity_lines]).reshape(-1, 3)
            self._guide_buf = _Buffer(lines, False) if len(lines) else None
            self._guide_src = src
        if self._guide_buf is None:
            return
        GL.glUseProgram(self._line_prog)
        u_mvp = GL.glGetUniformLocation(self._line_prog, "mvp")
        loc = GL.glGetUniformLocation(self._line_prog, "color")
        GL.glBindVertexArray(self._guide_buf.vao)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ZERO, GL.GL_ONE)  # stays opaque
        GL.glDepthMask(GL.GL_FALSE)
        # core GL draws 1 px lines only: thicken by drawing again shifted a pixel on screen
        px, py = 2.0 / max(self.width(), 1), 2.0 / max(self.height(), 1)
        shifts = [np.eye(4) for _ in range(5)]
        for T, (dx, dy) in zip(shifts, ((0, 0), (px, 0), (-px, 0), (0, py), (0, -py))):
            T[0, 3], T[1, 3] = dx, dy        # clip-space shift by w·d = a constant NDC shift
        *rgb, a = colormod.rgba(self.colors["flaw"])
        for func, alpha in ((GL.GL_GREATER, GUIDE_DIM), (GL.GL_LEQUAL, 1.0)):   # hidden part dimmed
            GL.glDepthFunc(func)
            GL.glUniform4f(loc, *rgb, alpha * a)
            for T in (shifts if alpha == 1.0 else shifts[:1]):
                GL.glUniformMatrix4fv(u_mvp, 1, GL.GL_TRUE, (T @ vp).astype(np.float32))
                self._draw_lines(self._guide_buf.count)
        GL.glDepthFunc(GL.GL_LESS)
        GL.glDepthMask(GL.GL_TRUE)
        GL.glDisable(GL.GL_BLEND)
        GL.glUseProgram(self._mesh_prog)

    # ------------------------------------------------------------ guide selection
    def _numbers(self, kind: str) -> list[int]:
        lines = self.island_lines if kind == "island" else self.cavity_lines
        nums = self.island_numbers if kind == "island" else self.cavity_numbers
        n = len(lines) // 2
        return list(nums) if nums is not None and len(nums) == n else list(range(1, n + 1))

    def guide_selection(self) -> set:
        """The selected flaw keys ((kind, number)). Keys whose line is gone are dropped."""
        live = {(k, n) for k in ("island", "cavity") for n in self._numbers(k)}
        self.selected_guides &= live
        return self.selected_guides

    def set_guide_selection(self, keys) -> None:
        self.selected_guides = set(keys)
        self.update()

    def clear_guide_selection(self, kind: str | None = None) -> None:
        """A new check renumbers its flaws: forget that kind's selection."""
        self.selected_guides = {k for k in self.selected_guides if kind is not None and k[0] != kind}
        self.update()

    def _guide_segments(self):
        """[(key, start, end, kind text)] for every guide line."""
        out = []
        for kind, lines, text in (("island", self.island_lines, LABEL_ISLAND),
                                  ("cavity", self.cavity_lines, LABEL_CAVITY)):
            for num, a, b in zip(self._numbers(kind), lines[0::2], lines[1::2]):
                out.append(((kind, num), a, b, LABEL_CUP if kind == "cavity" and num in self.cavity_cups else text))
        return out

    def guide_labels(self, vp=None, w=None, h=None) -> list[GuideLabel]:
        """Where the text at the outer end of each guide line goes: just past the end, on the
        line's continuation, outside the build plate. Lines of the same kind whose labels would
        overlap share one label (they end together). Selected labels are placed first and always
        shown; an unselected label that would cover another kind's label is left out."""
        w = w or max(self.width(), 1)
        h = h or max(self.height(), 1)
        if vp is None:
            vp = self.camera.projection(w / h) @ self.camera.view()
        sel = self.guide_selection()
        fm = self.fontMetrics()
        cands = []
        for key, a, b, text in self._guide_segments():
            out_dir = (b - a) / max(float(np.linalg.norm(b - a)), 1e-9)
            (e, f), ok = self._screen(np.array([b, b + out_dir]), vp)
            if not ok.all():
                continue
            d = f - e
            n = float(np.hypot(*d))
            d = d / n if n > 1e-6 else np.array([0.0, 1.0])
            cands.append((e, d, text, key))

        def rect(e, d, text):
            tw, th = fm.horizontalAdvance(text) + 10, fm.height() + 2
            reach = LABEL_GAP_PX + abs(d[0]) * tw / 2 + abs(d[1]) * th / 2
            return QRectF(e[0] + d[0] * reach - tw / 2, e[1] + d[1] * reach - th / 2, tw, th)

        cands.sort(key=lambda c: c[3] not in sel)                # selected first
        placed: list[list] = []                                    # [e, d, kind text, keys, selected, rect]
        for e, d, text, key in cands:
            is_sel = key in sel
            r = rect(e, d, label_text(text, [key[1]]))
            same = next((L for L in placed if L[2] == text and L[5].intersects(r)), None)
            if same is not None:
                same[3].append(key)
                same[4] = same[4] or is_sel
                same[5] = rect(same[0], same[1], label_text(text, [k[1] for k in same[3]]))
                continue
            if is_sel or not any(L[5].intersects(r) for L in placed):
                placed.append([e, d, text, [key], is_sel, r])
        return [GuideLabel(L[5], label_text(L[2], [k[1] for k in L[3]]), tuple(L[3]), L[4]) for L in placed]

    def guide_label_at(self, pos) -> GuideLabel | None:
        for lab in reversed(self.guide_labels()):                # topmost first
            if lab.rect.contains(QPointF(pos)):
                return lab
        return None

    def toggle_guide(self, lab: GuideLabel) -> None:
        """Clicking a label selects its line(s) (red, never hidden); clicking again deselects."""
        sel = self.guide_selection()
        if lab.selected:
            sel.difference_update(lab.keys)
        else:
            sel.update(lab.keys)
        self.update()
        self.guideSelectionChanged.emit(set(sel))

    def _draw_selected_guides(self, vp) -> None:
        """Selected guide lines in red over everything, so nothing hides them."""
        sel = self.guide_selection()
        segs = [(a, b) for key, a, b, _ in self._guide_segments() if key in sel]
        src = (self.island_lines, self.cavity_lines, frozenset(sel))          # numbers follow the lines
        if (self._sel_src is None or self._sel_src[0] is not src[0] or self._sel_src[1] is not src[1]
                or self._sel_src[2] != src[2]):
            if self._sel_buf is not None:
                self._sel_buf.delete()
            self._sel_buf = _Buffer(np.array(segs, float).reshape(-1, 3), False) if segs else None
            self._sel_src = src
        if self._sel_buf is None:
            return
        GL.glUseProgram(self._line_prog)
        u_mvp = GL.glGetUniformLocation(self._line_prog, "mvp")
        GL.glUniform4f(GL.glGetUniformLocation(self._line_prog, "color"), *colormod.rgba(self.colors["flaw_selected"]))
        GL.glBindVertexArray(self._sel_buf.vao)
        GL.glDisable(GL.GL_DEPTH_TEST)
        GL.glEnable(GL.GL_BLEND)
        GL.glBlendFuncSeparate(GL.GL_SRC_ALPHA, GL.GL_ONE_MINUS_SRC_ALPHA, GL.GL_ZERO, GL.GL_ONE)
        px, py = 2.0 / max(self.width(), 1), 2.0 / max(self.height(), 1)
        for dx, dy in ((0, 0), (px, 0), (-px, 0), (0, py), (0, -py)):
            T = np.eye(4)
            T[0, 3], T[1, 3] = dx, dy
            GL.glUniformMatrix4fv(u_mvp, 1, GL.GL_TRUE, (T @ vp).astype(np.float32))
            self._draw_lines(self._sel_buf.count)
        GL.glDisable(GL.GL_BLEND)
        GL.glEnable(GL.GL_DEPTH_TEST)

    def _draw_guide_labels(self, p: QPainter, vp, w, h) -> None:
        labels = self.guide_labels(vp, w, h)
        if not labels:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        font = QFont(self.font())
        font.setBold(True)
        p.setFont(font)
        text, sel = self.colors["flaw"], self.colors["flaw_selected"]
        for lab in labels:
            p.setOpacity((sel if lab.selected else text).alphaF())     # the label fades with its line
            p.setPen(QPen(QColor(sel.rgb()), 1.5) if lab.selected else Qt.NoPen)
            p.setBrush(QColor(20, 20, 24, 170))              # readable over the grid and models
            p.drawRoundedRect(lab.rect, 4, 4)
            p.setPen(QColor((sel if lab.selected else text).rgb()))
            p.drawText(lab.rect, Qt.AlignCenter, lab.text)
        p.restore()

    def front_label_quad(self, vp=None, w=None, h=None) -> list[QPointF] | None:
        """Screen corners (top-left, top-right, bottom-right, bottom-left) of the FRONT label,
        which lies flat on the floor just in front of the plate's front (−Y) edge, reading from
        the front. None when part of it is behind the camera."""
        w = w or max(self.width(), 1)
        h = h or max(self.height(), 1)
        if vp is None:
            vp = self.camera.projection(w / h) @ self.camera.view()
        sx, sy = float(self.scene.size[0]), float(self.scene.size[1])
        th = float(np.clip(sy * 0.16, 10.0, 28.0))                # text height in mm
        tw = th * FRONT_ASPECT
        far = -sy / 2 - th * 0.35
        near = far - th
        pts = []
        for x, y in ((-tw / 2, far), (tw / 2, far), (tw / 2, near), (-tw / 2, near)):
            c = vp @ np.array([x, y, 0.0, 1.0])
            if c[3] <= 1e-6:
                return None
            pts.append(QPointF((c[0] / c[3] + 1) * w / 2, (1 - c[1] / c[3]) * h / 2))
        return pts

    def plate_label_quad(self, i: int, vp=None, w=None, h=None) -> list[QPointF] | None:
        """Screen corners of plate i's label ("Plate 2"), flat on the floor just behind the
        plate's back (+Y) edge, reading from the front; None when behind the camera."""
        w = w or max(self.width(), 1)
        h = h or max(self.height(), 1)
        if vp is None:
            vp = self.camera.projection(w / h) @ self.camera.view()
        sx, sy = float(self.scene.size[0]), float(self.scene.size[1])
        cx = float(self.scene.plate_offset(i)[0])
        th = float(np.clip(sy * 0.12, 8.0, 22.0))
        tw = th * FRONT_ASPECT
        y0 = sy / 2 + th * 0.35
        y1 = y0 + th
        pts = []
        for x, y in ((cx - tw / 2, y1), (cx + tw / 2, y1), (cx + tw / 2, y0), (cx - tw / 2, y0)):
            c = vp @ np.array([x, y, 0.0, 1.0])
            if c[3] <= 1e-6:
                return None
            pts.append(QPointF((c[0] / c[3] + 1) * w / 2, (1 - c[1] / c[3]) * h / 2))
        return pts

    def plate_buttons(self, vp=None, w=None, h=None) -> list[tuple[str, int, QPointF]]:
        """The − and + beside each plate's label, in screen space: (kind, plate, centre).
        − deletes that plate (not offered with one plate left); + adds a plate at the end."""
        out = []
        for i in range(self.scene.plates):
            q = self.plate_label_quad(i, vp, w, h)
            if q is None or abs(_area(q)) < 40:               # label not drawn: no buttons
                continue
            m = (q[1] + q[2]) / 2                              # the label's right end
            d = q[1] - q[0]
            n = math.hypot(d.x(), d.y())
            d = QPointF(1.0, 0.0) if n < 1e-6 else d / n
            k = 1
            if self.scene.plates > 1:
                out.append(("delete", i, m + d * (PLATE_BTN_R + 6)))
                k = 2
            out.append(("add", i, m + d * (PLATE_BTN_R * (2 * k - 1) + 6 + 4 * (k - 1))))
        return out

    def plate_button_at(self, pos) -> tuple[str, int] | None:
        pos = QPointF(pos)
        for kind, i, c in self.plate_buttons():
            if (pos - c).manhattanLength() <= PLATE_BTN_R * 1.5 and \
                    math.hypot(pos.x() - c.x(), pos.y() - c.y()) <= PLATE_BTN_R + 2:
                return kind, i
        return None

    def plate_label_at(self, pos) -> int | None:
        """The plate whose "Plate N" label is under `pos` (only while it's readable), unless a
        model is in front of it there."""
        pt = QPointF(pos)
        for i in range(self.scene.plates):
            q = self.plate_label_quad(i)
            if q is None or abs(_area(q)) < 40:
                continue
            if QPolygonF(q).containsPoint(pt, Qt.OddEvenFill):
                part, _, _ = self.scene.pick(*self._ray(pt))
                return None if part is not None else i
        return None

    def _draw_plate_buttons(self, p: QPainter, vp, w, h) -> None:
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        hover = getattr(self, "_plate_btn_hover", None)
        for kind, i, c in self.plate_buttons(vp, w, h):
            on = hover == (kind, i)
            accent = QColor(230, 90, 80) if kind == "delete" else QColor(90, 170, 240)
            p.setPen(QPen(accent if on else QColor(255, 255, 255, 120), 1.2))
            p.setBrush(accent.darker(160) if on else QColor(0x22, 0x24, 0x28, 220))
            p.drawEllipse(c, PLATE_BTN_R, PLATE_BTN_R)
            p.setPen(QPen(QColor(255, 255, 255) if on else QColor(220, 224, 230), 1.8, Qt.SolidLine, Qt.RoundCap))
            a = PLATE_BTN_R * 0.5
            p.drawLine(QPointF(c.x() - a, c.y()), QPointF(c.x() + a, c.y()))
            if kind == "add":
                p.drawLine(QPointF(c.x(), c.y() - a), QPointF(c.x(), c.y() + a))
        p.restore()

    def _draw_front(self, p: QPainter, vp, w, h) -> None:
        for i in range(self.scene.plates):         # "Plate N" even with one plate: its + adds one
            q = self.plate_label_quad(i, vp, w, h)
            if q is not None:
                hot = i == self.current_plate or getattr(self, "_plate_btn_hover", None) == ("label", i)
                bright = QColor(255, 255, 255) if hot else QColor(175, 180, 190)
                self._draw_floor_text(p, q, f"Plate {i + 1}", bright)
        quad = self.front_label_quad(vp, w, h)
        if quad is None:
            return
        self._draw_floor_text(p, quad, FRONT_TEXT, QColor(255, 255, 255))

    def _draw_floor_text(self, p: QPainter, quad, text: str, color: QColor) -> None:
        poly = QPolygonF(quad)
        if abs(_area(quad)) < 40:                                 # edge-on: nothing readable
            return
        src = QRectF(0, 0, FRONT_FONT_PX * FRONT_ASPECT, FRONT_FONT_PX)
        t = QTransform.quadToQuad(QPolygonF([src.topLeft(), src.topRight(), src.bottomRight(), src.bottomLeft()]), poly)
        if t is None:
            return
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        p.setRenderHint(QPainter.TextAntialiasing)
        p.setTransform(t)
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(int(FRONT_FONT_PX * 0.86))
        font.setLetterSpacing(QFont.PercentageSpacing, 112)
        p.setFont(font)
        p.setPen(QColor(0, 0, 0, 110))                           # soft shadow for contrast
        p.drawText(src.translated(1.5, 1.5), Qt.AlignCenter, text)
        p.setPen(color)
        p.drawText(src, Qt.AlignCenter, text)
        p.restore()

    # ------------------------------------------------------------ rotation rings
    def rings(self):
        """(centre, radius) of the rotation rings around the selection, or None."""
        if (not self.show_rings or self.handle_mode != "rotate" or self.support_mode or self.hole_mode
                or self.lay_flat_mode):
            return None
        parts = [p for p in (self.scene.get(i) for i in self.selected) if p]
        if not parts:
            return None
        if self._rot is not None:
            return self._rot["centre"], self._rot["radius"]
        b = np.array([p.bounds() for p in parts])
        lo, hi = b[:, 0].min(axis=0), b[:, 1].max(axis=0)
        return (lo + hi) / 2, max(0.55 * float(np.linalg.norm(hi - lo)), 4.0)

    def ring_visible(self, axis: int, centre) -> bool:
        """Seen nearly edge-on a ring is a line across the part: hidden, so it neither hides the
        part nor takes the clicks meant for moving it (turn about that axis from another view)."""
        to_eye = self.camera.eye() - centre
        n = float(np.linalg.norm(to_eye))
        return n < 1e-9 or abs(float(to_eye[axis])) / n >= RING_EDGE_ON

    def ring_points(self, axis: int, centre, radius, n: int = RING_SEGMENTS) -> np.ndarray:
        t = np.linspace(0, 2 * np.pi, n + 1)
        u, w = RING_BASIS[axis]
        return centre + radius * (np.cos(t)[:, None] * u + np.sin(t)[:, None] * w)

    def _screen(self, pts, vp=None):
        """Screen positions (N, 2) of world points, and which are in front of the camera."""
        w, h = max(self.width(), 1), max(self.height(), 1)
        if vp is None:
            vp = self.camera.projection(w / h) @ self.camera.view()
        c = np.c_[pts, np.ones(len(pts))] @ vp.T
        ok = c[:, 3] > 1e-6
        ww = np.where(ok, c[:, 3], 1.0)
        xy = np.c_[(c[:, 0] / ww + 1) * w / 2, (1 - c[:, 1] / ww) * h / 2]
        return xy, ok

    def ring_at(self, pos) -> int | None:
        """The ring (axis index) within RING_GRAB_PX of a screen position, nearest first."""
        g = self.rings()
        if g is None:
            return None
        q = np.array([pos.x(), pos.y()])
        best = None
        for axis in range(3):
            if not self.ring_visible(axis, g[0]):
                continue
            xy, ok = self._screen(self.ring_points(axis, *g))
            a, b = xy[:-1], xy[1:]
            keep = ok[:-1] & ok[1:]
            if not keep.any():
                continue
            a, b = a[keep], b[keep]
            ab = b - a
            t = np.clip(np.einsum("ij,ij->i", q - a, ab) / np.maximum(np.einsum("ij,ij->i", ab, ab), 1e-9), 0, 1)
            d = float(np.min(np.linalg.norm(a + ab * t[:, None] - q, axis=1)))
            if d <= RING_GRAB_PX and (best is None or d < best[0]):
                best = (d, axis)
        return None if best is None else best[1]

    def _ring_angle(self, axis: int, pos):
        """Angle (deg) of the mouse around the ring's axis, in the ring's plane; None when the
        ring is seen edge-on (then dragging along it is used instead)."""
        centre = self._rot["centre"]
        o, d = self._ray(pos)
        n = np.eye(3)[axis]
        if abs(float(np.dot(d, n))) < 0.12:
            return None
        hit = ray_plane(o, d, centre, n)
        if hit is None:
            return None
        u, w = RING_BASIS[axis]
        v = hit - centre
        return float(np.degrees(np.arctan2(np.dot(v, w), np.dot(v, u))))

    def _start_rotate(self, axis: int, pos) -> None:
        centre, radius = self.rings()
        parts = [p for p in (self.scene.get(i) for i in self.selected) if p]
        self._rot = {"axis": axis, "centre": centre, "radius": radius, "parts": parts,
                     "before": {p.id: p.state() for p in parts}, "press": np.array([pos.x(), pos.y()]),
                     "raw": 0.0, "deg": 0.0, "pos": pos}
        a = self._ring_angle(axis, pos)
        self._rot["last"] = a
        if a is None:
            # edge-on: degrees from dragging along the ring's on-screen direction at the grab point
            pts = self.ring_points(axis, centre, radius, 720)
            xy, _ = self._screen(pts)
            i = int(np.argmin(np.linalg.norm(xy - self._rot["press"], axis=1)))
            tan = (xy[(i + 1) % 720] - xy[i]) * 2.0                    # px per degree (0.5° steps)
            self._rot["tangent"] = tan if np.dot(tan, tan) > 1e-6 else np.array([1.0, 0.0])
        self.setCursor(Qt.ClosedHandCursor)

    def _drag_rotate(self, pos) -> None:
        r = self._rot
        r["pos"] = pos
        if r["last"] is None:
            t = r["tangent"]
            r["raw"] = float(np.dot(np.array([pos.x(), pos.y()]) - r["press"], t) / np.dot(t, t))
        else:
            a = self._ring_angle(r["axis"], pos)
            if a is not None:
                r["raw"] += (a - r["last"] + 180.0) % 360.0 - 180.0      # unwrapped
                r["last"] = a
        deg = snap_angle(r["raw"])
        if deg == r["deg"]:
            return
        r["deg"] = deg
        R = rotation_matrix(np.eye(3)[r["axis"]], deg)
        for p in r["parts"]:
            b = r["before"][p.id]
            p.rotation = R @ b.rotation
            p.position = r["centre"] + R @ (b.position - r["centre"])
            self.scene.drop_to_plate(p)
        self.update()

    def _end_rotate(self) -> None:
        r, self._rot = self._rot, None
        self.unsetCursor()
        changes = {p.id: (r["before"][p.id], p.state()) for p in r["parts"]
                   if p.state() != r["before"][p.id]}
        self.update()
        if changes:
            self.rotateFinished.emit(changes, f"Rotate {'XYZ'[r['axis']]} {r['deg']:+g}°")

    def _draw_rings(self, p: QPainter, vp, w, h) -> None:
        g = self.rings()
        if g is None:
            return
        centre, radius = g
        eye = self.camera.eye()
        to_eye = (eye - centre) / max(np.linalg.norm(eye - centre), 1e-9)
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        active = self._rot["axis"] if self._rot else None
        for back in (True, False):                      # the far half first, dimmer
            for axis in range(3):
                if active is not None and axis != active:
                    continue
                if active is None and not self.ring_visible(axis, centre):
                    continue
                pts = self.ring_points(axis, centre, radius)
                xy, ok = self._screen(pts, vp)
                facing = (pts - centre) @ to_eye >= 0
                col = QColor(COMPASS_COLORS["XYZ"[axis]])
                hot = axis == active or axis == self._ring_hover
                if back:
                    col.setAlpha(110)
                width = (4.0 if hot else 2.6) if not back else (2.4 if hot else 1.6)
                p.setPen(QPen(col, width, Qt.SolidLine, Qt.RoundCap))
                for i in range(len(pts) - 1):
                    if ok[i] and ok[i + 1] and bool(facing[i]) != back:
                        p.drawLine(QPointF(*xy[i]), QPointF(*xy[i + 1]))
        if self._rot is not None:                       # the angle so far, by the cursor
            r = self._rot
            font = QFont(self.font())
            font.setBold(True)
            p.setFont(font)
            text = f"{'XYZ'[r['axis']]} {r['deg']:+g}°"
            at = r["pos"] + QPointF(16, -12)
            box = p.fontMetrics().boundingRect(text).adjusted(-6, -3, 6, 3)
            box.moveTopLeft(at.toPoint() - QPointF(6, box.height() - 3).toPoint())
            p.setPen(Qt.NoPen)
            p.setBrush(QColor(0x15, 0x17, 0x1a, 215))
            p.drawRoundedRect(box, 4, 4)
            p.setPen(QColor(COMPASS_COLORS["XYZ"[r["axis"]]]).lighter(130))
            p.drawText(box, Qt.AlignCenter, text)
        p.restore()

    def compass_rect(self) -> QRectF:
        """Where the orientation compass sits: the top-right corner."""
        d = 2 * COMPASS_RADIUS
        return QRectF(self.width() - d - COMPASS_MARGIN, COMPASS_MARGIN, d, d)

    def _draw_compass(self, p: QPainter) -> None:
        """X/Y/Z axes as the camera sees them (rotation only), far ends first so nearer
        axes overlap. Positive ends carry a labelled disc, negative ends a small ring."""
        p.setRenderHint(QPainter.Antialiasing)
        r = self.compass_rect()
        c = r.center()
        p.setPen(QPen(QColor(255, 255, 255, 40), 1.0))
        p.setBrush(QColor(0x22, 0x24, 0x28, 225))        # hides scene lines behind it
        p.drawEllipse(r)
        arm = COMPASS_RADIUS - 11
        font = QFont(self.font())
        font.setBold(True)
        font.setPixelSize(11)
        p.setFont(font)
        for name, sign, x, y, depth in axis_gizmo(self.camera.view()):
            col = QColor(COMPASS_COLORS[name])
            if depth < 0:                                   # pointing away: dim it
                col = col.darker(135)
            end = QPointF(c.x() + x * arm, c.y() + y * arm)
            if sign > 0:
                p.setPen(QPen(col, 2.5, Qt.SolidLine, Qt.RoundCap))
                p.drawLine(c, end)
                p.setPen(Qt.NoPen)
                p.setBrush(col)
                p.drawEllipse(end, 8.5, 8.5)
                p.setPen(QColor("#15171a"))
                p.drawText(QRectF(end.x() - 8.5, end.y() - 8.5, 17, 17), Qt.AlignCenter, name)
            else:
                p.setPen(QPen(col, 1.5))
                p.setBrush(QColor(0x29, 0x2b, 0x30))
                p.drawEllipse(end, 5.0, 5.0)

    # ------------------------------------------------------------ input
    def _ray(self, pos: QPointF):
        return self.camera.ray(pos.x(), pos.y(), max(self.width(), 1), max(self.height(), 1))

    def mousePressEvent(self, e):
        self._last = e.position()
        if e.button() == Qt.RightButton and not (e.modifiers() & Qt.ShiftModifier):
            self._rpress = e.position()
        if e.button() == Qt.LeftButton and not e.modifiers() and self._rot is None:
            btn = self.plate_button_at(e.position()) if self.show_front else None
            if btn is not None:
                self._plate_btn_hover = None
                QToolTip.hideText()
                if btn[0] == "add":
                    self.plateAddRequested.emit()
                else:
                    self.plateDeleteRequested.emit(btn[1])
                self.update()
                return
            plate = self.plate_label_at(e.position()) if self.show_front else None
            if plate is not None:
                self.plateLabelClicked.emit(plate)
                self.update()
                return
            lab = self.guide_label_at(e.position())
            if lab is not None:
                self.toggle_guide(lab)
                return
            axis = self.ring_at(e.position())
            if axis is not None:
                self._start_rotate(axis, e.position())
                return
            hd = self.handle_at(e.position())
            if hd is not None:
                self._start_handle(hd, e.position())
                return
        if e.button() == Qt.LeftButton and not (e.modifiers() & Qt.ShiftModifier):
            o, d = self._ray(e.position())
            part, face, hit = self.scene.pick(o, d)
            if self.support_mode:
                if e.modifiers() & Qt.ControlModifier:
                    self.supportClicked.emit(o, d, True)
                else:                   # a click adds on release; a drag moves a contact
                    self._spress = {"pos": QPointF(e.position()), "ray": (o, d), "moved": False}
                return
            if self.hole_mode:
                if part is not None:
                    self.holePicked.emit(part.id, face, hit)
                return
            if self.lay_flat_mode:
                if part is not None:
                    self.lay_flat_mode = False
                    self.unsetCursor()
                    self.layFlatPicked.emit(part.id, face)
                return
            if part is None:
                if not (e.modifiers() & Qt.ControlModifier):
                    self.set_selection([])
                return
            if e.modifiers() & Qt.ControlModifier:
                ids = [i for i in self.selected if i != part.id] if part.id in self.selected \
                    else self.selected + [part.id]
                self.set_selection(ids)
                return
            if part.id not in self.selected:
                self.set_selection([part.id])
            moving = [self.scene.get(i) for i in self.selected]
            self._drag = {"z": float(hit[2]), "start": hit, "parts": moving,
                          "before": {p.id: p.state() for p in moving}}

    def _restore_cursor(self) -> None:
        if self.support_mode or self.hole_mode or self.lay_flat_mode:
            self.setCursor(Qt.CrossCursor)
        elif self._ring_hover is not None or self._handle_hover is not None:
            self.setCursor(Qt.OpenHandCursor)
        else:
            self.unsetCursor()

    def mouseMoveEvent(self, e):
        pos = e.position()
        dx, dy = pos.x() - self._last.x(), pos.y() - self._last.y()
        self._last = pos
        if self._rot is not None and e.buttons() & Qt.LeftButton:
            self._drag_rotate(pos)
            return
        if self._hdrag is not None and e.buttons() & Qt.LeftButton:
            self._drag_handle(pos, bool(e.modifiers() & Qt.ControlModifier))
            return
        if self._spress is not None and e.buttons() & Qt.LeftButton:
            if (pos - self._spress["pos"]).manhattanLength() > 4:
                self._spress["moved"] = True
            if self._spress["moved"]:
                _, _, hit = self.scene.pick(*self._ray(pos))
                self.support_drag_marker = None if hit is None else np.asarray(hit, float)
                self.update()
            return
        if self.hole_mode and not e.buttons():
            # a fast mouse sends many moves: pick only where the cursor ends up once they're in
            self._hole_pending = QPointF(pos)
            if not self._hole_timer.isActive():
                self._hole_timer.start()
            return
        if not e.buttons() and self.show_front:
            btn = self.plate_button_at(pos)
            if btn is None:
                k = self.plate_label_at(pos)
                btn = None if k is None else ("label", k)
            if btn != getattr(self, "_plate_btn_hover", None):
                self._plate_btn_hover = btn
                if btn is None:
                    QToolTip.hideText()
                    self._restore_cursor()
                else:
                    self.setCursor(Qt.PointingHandCursor)
                    n = len(self.scene.on_plate(btn[1]))
                    QToolTip.showText(e.globalPosition().toPoint(),
                                      "Add a build plate" if btn[0] == "add" else
                                      f"Make plate {btn[1] + 1} the current plate" if btn[0] == "label" else
                                      f"Delete plate {btn[1] + 1}"
                                      + (f" (its {n} part{'' if n == 1 else 's'} move to the other plates)"
                                         if n else ""), self)
                self.update()
            if btn is not None:
                return
        if not e.buttons():
            on_label = self.guide_label_at(pos) is not None
            if on_label != self._label_hover:
                self._label_hover = on_label
                if on_label:
                    self.setCursor(Qt.PointingHandCursor)
                else:
                    self._restore_cursor()
            if on_label:
                return
            hover = self.ring_at(pos)
            handle = None if hover is not None else self.handle_at(pos)
            if hover != self._ring_hover or handle != self._handle_hover:
                self._ring_hover, self._handle_hover = hover, handle
                if hover is None and handle is None:
                    self.unsetCursor()
                else:
                    self.setCursor(Qt.OpenHandCursor)
                self.update()
            return
        if self._rpress is not None and (pos - self._rpress).manhattanLength() > 4:
            self._rpress = None                     # a drag: orbit, not a click
        if self._drag and e.buttons() & Qt.LeftButton:
            o, d = self._ray(pos)
            p = ray_plane(o, d, [0, 0, self._drag["z"]], [0, 0, 1])
            if p is not None:
                delta = p - self._drag["start"]
                delta[2] = 0.0
                for part in self._drag["parts"]:
                    part.position = self._drag["before"][part.id].position + delta
            self.update()
        elif e.buttons() & Qt.MiddleButton or (e.buttons() & Qt.RightButton and e.modifiers() & Qt.ShiftModifier) \
                or (e.buttons() & Qt.LeftButton and e.modifiers() & Qt.ShiftModifier):
            self.camera.pan(dx, dy, self.height())
            self.update()
        elif e.buttons() & Qt.RightButton:
            self.camera.orbit(-dx * 0.4, dy * 0.4)
            self.update()

    def mouseReleaseEvent(self, e):
        if self._rot is not None and e.button() == Qt.LeftButton:
            self._end_rotate()
            return
        if self._hdrag is not None and e.button() == Qt.LeftButton:
            self._end_handle()
            return
        if self._spress is not None and e.button() == Qt.LeftButton:
            sp, self._spress = self._spress, None
            self.support_drag_marker = None
            self.update()
            if sp["moved"]:
                self.supportDragged.emit(*sp["ray"], *self._ray(e.position()))
            else:
                self.supportClicked.emit(*sp["ray"], False)
            return
        if e.button() == Qt.RightButton and self._rpress is not None:
            pos, self._rpress = self._rpress, None
            part, _, _ = self.scene.pick(*self._ray(pos))
            if part is not None:
                self.set_selection([part.id])
                self.partContextMenu.emit(part.id, self.mapToGlobal(pos.toPoint()))
            return
        if self._drag and e.button() == Qt.LeftButton:
            changes = {p.id: (self._drag["before"][p.id], p.state()) for p in self._drag["parts"]
                       if p.state() != self._drag["before"][p.id]}
            self._drag = None
            if changes:
                self.moveFinished.emit(changes)

    def wheelEvent(self, e):
        steps = e.angleDelta().y() / 120.0
        self.camera.zoom(0.88 ** steps)
        self.update()

    def keyPressEvent(self, e):
        if e.key() == Qt.Key_Escape and self.cancel_handle_drag():
            return
        if e.key() == Qt.Key_Escape and (self.lay_flat_mode or self.hole_mode or self.support_mode):
            if self.hole_mode:
                self.stop_hole_mode()
            if self.support_mode:
                self.stop_support_mode()
            self.lay_flat_mode = False
            self.unsetCursor()
            return
        if e.key() == Qt.Key_Escape:
            self.escapePressed.emit()
        super().keyPressEvent(e)

    def start_support_mode(self):
        self.support_mode = True
        self.setCursor(Qt.CrossCursor)

    def stop_support_mode(self):
        self._spress = None
        self.support_drag_marker = None
        if self.support_mode:
            self.support_mode = False
            self.unsetCursor()
            self.supportModeEnded.emit()

    def start_hole_mode(self):
        self.hole_mode = True
        self.setCursor(Qt.CrossCursor)

    def stop_hole_mode(self):
        if self.hole_mode:
            self.hole_mode = False
            self.clear_hole_hover()
            self.unsetCursor()
            self.holeModeEnded.emit()

    def leaveEvent(self, e):
        if self.hole_mode and self._hole_hover is not None:
            self.clear_hole_hover()
        super().leaveEvent(e)

    def start_lay_flat(self):
        self.lay_flat_mode = True
        self.setCursor(Qt.CrossCursor)

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            e.acceptProposedAction()

    def dropEvent(self, e):
        files = [u.toLocalFile() for u in e.mimeData().urls() if u.isLocalFile()]
        if files:
            self.filesDropped.emit(files)
