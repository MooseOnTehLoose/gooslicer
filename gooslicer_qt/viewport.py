"""3D viewport: QOpenGLWidget + GLSL 3.3 core (PyOpenGL calls)."""
from __future__ import annotations

import ctypes

import numpy as np
from OpenGL import GL
# Qt owns the GL context (EGL or GLX). PyOpenGL's wrapped glVertexAttribPointer records the
# pointer per context and fails when PyOpenGL's platform differs from Qt's; we only pass
# VBO offsets, so use the raw entry point.
from OpenGL.raw.GL.VERSION.GL_2_0 import glVertexAttribPointer as _raw_attrib_pointer
from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF, QSurfaceFormat, QTransform
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from gooslicer.camera import OrbitCamera, axis_gizmo, ray_plane
from gooslicer.scene import Part, PartState, Scene

MESH_VS = """#version 330 core
layout(location=0) in vec3 pos;
layout(location=1) in vec3 nrm;
uniform mat4 mvp; uniform mat3 nmat;
out vec3 vN;
void main(){ vN = normalize(nmat * nrm); gl_Position = mvp * vec4(pos, 1.0); }
"""
MESH_FS = """#version 330 core
in vec3 vN; out vec4 frag;
uniform vec3 color; uniform vec3 lightA; uniform vec3 lightB;
void main(){
  vec3 n = normalize(vN);
  if (!gl_FrontFacing) { frag = vec4(0.85, 0.15, 0.55, 1.0); return; }  // inside/back faces
  float d = 0.30 + 0.55 * max(dot(n, lightA), 0.0) + 0.25 * max(dot(n, lightB), 0.0);
  frag = vec4(color * d, 1.0);
}
"""
LINE_VS = """#version 330 core
layout(location=0) in vec3 pos; uniform mat4 mvp;
void main(){ gl_Position = mvp * vec4(pos, 1.0); }
"""
LINE_FS = """#version 330 core
out vec4 frag; uniform vec4 color;
void main(){ frag = color; }
"""

COL_PART = (0.55, 0.70, 0.78)
COL_SELECTED = (0.98, 0.62, 0.20)
COL_OUTSIDE = (0.90, 0.25, 0.22)
COL_SUPPORT = (0.78, 0.78, 0.70)


def default_format() -> QSurfaceFormat:
    f = QSurfaceFormat()
    f.setVersion(3, 3)
    f.setProfile(QSurfaceFormat.CoreProfile)
    f.setDepthBufferSize(24)
    f.setSamples(4)
    return f


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
    """VAO + VBO with float32 attributes (3 or 3+3 floats per vertex)."""

    def __init__(self, data: np.ndarray, with_normals: bool):
        data = np.ascontiguousarray(data, dtype=np.float32)
        self.count = len(data)
        self.vao = GL.glGenVertexArrays(1)
        self.vbo = GL.glGenBuffers(1)
        GL.glBindVertexArray(self.vao)
        GL.glBindBuffer(GL.GL_ARRAY_BUFFER, self.vbo)
        GL.glBufferData(GL.GL_ARRAY_BUFFER, data.nbytes, data, GL.GL_STATIC_DRAW)
        stride = data.shape[1] * 4
        GL.glEnableVertexAttribArray(0)
        _raw_attrib_pointer(0, 3, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(0))
        if with_normals:
            GL.glEnableVertexAttribArray(1)
            _raw_attrib_pointer(1, 3, GL.GL_FLOAT, GL.GL_FALSE, stride, ctypes.c_void_p(12))
        GL.glBindVertexArray(0)

    def delete(self):
        GL.glDeleteBuffers(1, [self.vbo])
        GL.glDeleteVertexArrays(1, [self.vao])


def mesh_vertex_data(mesh) -> np.ndarray:
    """Flat-shaded triangle soup: per-corner position + face normal."""
    pos = np.asarray(mesh.vertices)[np.asarray(mesh.faces)].reshape(-1, 3)
    nrm = np.repeat(np.asarray(mesh.face_normals), 3, axis=0)
    return np.hstack([pos, nrm])


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
FRONT_FONT_PX = 64       # drawing resolution of the label before it is projected


def _area(q) -> float:
    return 0.5 * sum(q[i].x() * q[i - 1].y() - q[i - 1].x() * q[i].y() for i in range(len(q)))


COMPASS_RADIUS = 42      # px, logical
COMPASS_MARGIN = 10
COMPASS_COLORS = {"X": "#e5534b", "Y": "#7ac74f", "Z": "#4c9be8"}   # the usual red/green/blue


class Viewport(QOpenGLWidget):
    selectionChanged = Signal(list)                # [part ids]
    moveFinished = Signal(object)                  # {id: (before PartState, after PartState)}
    layFlatPicked = Signal(int, int)               # part id, local face index
    holePicked = Signal(int, int, object)          # part id, local face index, world hit point
    holeModeEnded = Signal()
    supportClicked = Signal(object, object, bool)  # ray origin, ray direction, remove (Ctrl)
    supportModeEnded = Signal()
    filesDropped = Signal(list)

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
        self.show_compass = True
        self.show_front = True
        self.extra_meshes = lambda: []             # [(trimesh, 4x4 world matrix)] drawn as supports
        self._buffers: dict[int, tuple[object, _Buffer]] = {}  # id(mesh) -> (mesh, buffer)
        self._plate: tuple | None = None
        self._plate_size = None
        self._gl_ready = False
        self._drag = None
        self._last = QPointF()
        self.reset_view()

    # ------------------------------------------------------------- public
    def reset_view(self):
        sx, sy, sz = self.scene.size
        self.camera.frame([-sx / 2, -sy / 2, 0], [sx / 2, sy / 2, sz * 0.4])
        self.camera.yaw, self.camera.pitch = -90.0 + 25.0, 28.0
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
        for _, b in self._buffers.values():
            b.delete()
        self._buffers.clear()
        if self._plate:
            for b in self._plate:
                b.delete()
            self._plate = None
        self.doneCurrent()

    def _sync_buffers(self):
        live = {id(p.mesh): p.mesh for p in self.scene.parts}
        self._extras = list(self.extra_meshes())
        live.update({id(m): m for m, _ in self._extras})
        for k in [k for k in self._buffers if k not in live]:
            self._buffers.pop(k)[1].delete()
        for k, m in live.items():
            if k not in self._buffers:
                self._buffers[k] = (m, _Buffer(mesh_vertex_data(m), True))
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

    def paintGL(self):
        self._sync_buffers()
        # QPainter (the compass, drawn last) leaves its own GL state behind: reset ours
        GL.glEnable(GL.GL_DEPTH_TEST)
        GL.glDisable(GL.GL_BLEND)
        GL.glClearColor(0.16, 0.17, 0.19, 1.0)
        GL.glClear(GL.GL_COLOR_BUFFER_BIT | GL.GL_DEPTH_BUFFER_BIT)
        w, h = max(self.width(), 1), max(self.height(), 1)
        vp = self.camera.projection(w / h) @ self.camera.view()

        GL.glUseProgram(self._line_prog)
        GL.glUniformMatrix4fv(GL.glGetUniformLocation(self._line_prog, "mvp"), 1, GL.GL_TRUE,
                              vp.astype(np.float32))
        loc = GL.glGetUniformLocation(self._line_prog, "color")
        quad, grid, box = self._plate
        GL.glUniform4f(loc, 0.24, 0.25, 0.28, 1.0)
        GL.glBindVertexArray(quad.vao)
        GL.glDrawArrays(GL.GL_TRIANGLES, 0, quad.count)
        GL.glUniform4f(loc, 0.33, 0.35, 0.39, 1.0)
        GL.glBindVertexArray(grid.vao)
        GL.glDrawArrays(GL.GL_LINES, 0, grid.count)
        GL.glUniform4f(loc, 0.55, 0.58, 0.64, 1.0)
        GL.glBindVertexArray(box.vao)
        GL.glDrawArrays(GL.GL_LINES, 0, box.count)

        GL.glUseProgram(self._mesh_prog)
        eye_dir = self.camera.eye() - self.camera.target
        eye_dir /= np.linalg.norm(eye_dir)
        GL.glUniform3f(GL.glGetUniformLocation(self._mesh_prog, "lightA"), *eye_dir)
        GL.glUniform3f(GL.glGetUniformLocation(self._mesh_prog, "lightB"), 0.0, 0.0, 1.0)
        u_mvp = GL.glGetUniformLocation(self._mesh_prog, "mvp")
        u_n = GL.glGetUniformLocation(self._mesh_prog, "nmat")
        u_c = GL.glGetUniformLocation(self._mesh_prog, "color")
        for p in self.scene.parts:
            M = p.matrix()
            GL.glUniformMatrix4fv(u_mvp, 1, GL.GL_TRUE, (vp @ M).astype(np.float32))
            GL.glUniformMatrix3fv(u_n, 1, GL.GL_TRUE, np.linalg.inv(M[:3, :3]).T.astype(np.float32))
            col = COL_SELECTED if p.id in self.selected else COL_PART
            if not self.scene.in_volume(p):
                col = COL_OUTSIDE
            GL.glUniform3f(u_c, *col)
            # mirrored transforms flip winding; keep front faces correct
            GL.glFrontFace(GL.GL_CW if np.linalg.det(M[:3, :3]) < 0 else GL.GL_CCW)
            b = self._buffers[id(p.mesh)][1]
            GL.glBindVertexArray(b.vao)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, b.count)
        GL.glFrontFace(GL.GL_CCW)
        GL.glUniform3f(u_c, *COL_SUPPORT)
        for m, M in self._extras:
            GL.glUniformMatrix4fv(u_mvp, 1, GL.GL_TRUE, (vp @ M).astype(np.float32))
            GL.glUniformMatrix3fv(u_n, 1, GL.GL_TRUE, np.eye(3, dtype=np.float32))
            b = self._buffers[id(m)][1]
            GL.glBindVertexArray(b.vao)
            GL.glDrawArrays(GL.GL_TRIANGLES, 0, b.count)
        GL.glBindVertexArray(0)
        GL.glUseProgram(0)
        painter = QPainter(self)
        try:
            if self.show_front:
                self._draw_front(painter, vp, w, h)
            if self.show_compass:
                self._draw_compass(painter)
        finally:
            painter.end()

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

    def _draw_front(self, p: QPainter, vp, w, h) -> None:
        quad = self.front_label_quad(vp, w, h)
        if quad is None:
            return
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
        p.drawText(src.translated(1.5, 1.5), Qt.AlignCenter, FRONT_TEXT)
        p.setPen(QColor(255, 255, 255))
        p.drawText(src, Qt.AlignCenter, FRONT_TEXT)
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
        if e.button() == Qt.LeftButton and not (e.modifiers() & Qt.ShiftModifier):
            o, d = self._ray(e.position())
            part, face, hit = self.scene.pick(o, d)
            if self.support_mode:
                self.supportClicked.emit(o, d, bool(e.modifiers() & Qt.ControlModifier))
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

    def mouseMoveEvent(self, e):
        pos = e.position()
        dx, dy = pos.x() - self._last.x(), pos.y() - self._last.y()
        self._last = pos
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
        if e.key() == Qt.Key_Escape and (self.lay_flat_mode or self.hole_mode or self.support_mode):
            if self.hole_mode:
                self.stop_hole_mode()
            if self.support_mode:
                self.stop_support_mode()
            self.lay_flat_mode = False
            self.unsetCursor()
            return
        super().keyPressEvent(e)

    def start_support_mode(self):
        self.support_mode = True
        self.setCursor(Qt.CrossCursor)

    def stop_support_mode(self):
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
            self.unsetCursor()
            self.holeModeEnded.emit()

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
