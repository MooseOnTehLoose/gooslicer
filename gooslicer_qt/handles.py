"""Move arrows and scale handles in the 3D view, and the handle mode that picks between them and
the rotation rings (09 §8). A mixin for Viewport: it uses the view's scene, selection, camera,
_screen and _ray.

Move: X and Y arrows from the selection's bottom centre; a drag moves along that axis only
(free, or whole millimetres with Ctrl). There is no Z arrow: parts rest on the plate or at their
support lift.
Scale: with one part, a cube on each of the part's own axes (its scale is per local axis, so world
axes would shear a turned part) plus a uniform handle above the bounding box's top corner; with
several parts only the uniform handle, scaling them about the selection's bottom centre. Steps of
1 %, 10 % with Ctrl, never below 1 % of the size at the start.
"""
from __future__ import annotations

import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QFont, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QButtonGroup, QFrame, QHBoxLayout, QToolButton

from gooslicer.camera import ray_plane

MODES = ("move", "rotate", "scale")
MODE_LABELS = {"move": "Move", "rotate": "Rotate", "scale": "Scale"}
MODE_KEYS = {"move": "W", "rotate": "E", "scale": "R"}
HANDLE_GRAB_PX = 9.0
MIN_SCREEN_PX = 12.0          # a handle shorter than this on screen points at the camera: hidden
CUBE_PX = 5.0                 # half size of a scale cube
UNIFORM = 3                   # handle index of the uniform scale handle
AXIS_COLORS = ("#e5534b", "#7ac74f", "#4c9be8")
MIN_FACTOR = 0.01


def snap(value: float, step: float) -> float:
    return float(np.round(value / step) * step) + 0.0


def _point_segment_px(q, a, b) -> float:
    ab = b - a
    t = float(np.clip(np.dot(q - a, ab) / max(float(np.dot(ab, ab)), 1e-9), 0.0, 1.0))
    return float(np.linalg.norm(a + ab * t - q))


class HandlesMixin:
    """Needs, on the class it is mixed into: handleModeChanged(str), transformFinished(object,
    str) ({id: (before, after)}, undo text) and transformLive() (parts changed mid-drag)."""

    def _init_handles(self) -> None:
        self.handle_mode = "rotate"
        self.uniform_scale = lambda: True          # the Mesh panel's Uniform scale box (set by the window)
        self._hdrag = None                         # an active move or scale drag
        self._handle_hover = None                  # handle index under the mouse
        bar = QFrame(self)
        bar.setObjectName("handleModes")
        bar.setStyleSheet("#handleModes { background: rgba(21, 23, 26, 200); border-radius: 5px; }"
                          "#handleModes QToolButton { color: #e8e8e8; padding: 3px 8px; border: none;"
                          " border-radius: 4px; }"
                          "#handleModes QToolButton:checked { background: #3b6ea8; }"
                          "#handleModes QToolButton:hover:!checked { background: rgba(255,255,255,30); }")
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(3, 3, 3, 3)
        lay.setSpacing(2)
        self.mode_buttons: dict[str, QToolButton] = {}
        group = QButtonGroup(bar)
        for m in MODES:
            b = QToolButton(bar)
            b.setText(MODE_LABELS[m])
            b.setCheckable(True)
            b.setFocusPolicy(Qt.NoFocus)
            b.setToolTip(f"{MODE_LABELS[m]} handles ({MODE_KEYS[m]})")
            b.clicked.connect(lambda _=False, mode=m: self.set_handle_mode(mode))
            group.addButton(b)
            lay.addWidget(b)
            self.mode_buttons[m] = b
        self.mode_buttons[self.handle_mode].setChecked(True)
        bar.move(8, 8)
        bar.adjustSize()
        self.mode_bar = bar

    def set_handle_mode(self, mode: str) -> None:
        if mode not in MODES:
            return
        if self._hdrag is not None or self._rot is not None:
            self.cancel_handle_drag()
        changed = mode != self.handle_mode
        self.handle_mode = mode
        self._handle_hover = self._ring_hover = None
        self.mode_buttons[mode].setChecked(True)
        self.update()
        if changed:
            self.handleModeChanged.emit(mode)

    # ------------------------------------------------------------ geometry
    def _handles_allowed(self) -> bool:
        return not (self.support_mode or self.hole_mode or self.lay_flat_mode)

    def _selected_parts(self) -> list:
        return [p for p in (self.scene.get(i) for i in self.selected) if p]

    def _selection_box(self, parts):
        b = np.array([p.bounds() for p in parts])
        return b[:, 0].min(axis=0), b[:, 1].max(axis=0)

    def handles(self):
        """The current mode's move or scale handles: (kind, centre, [(index, world end)...]) or
        None. Move: indices 0 (X) and 1 (Y). Scale: 0–2 along the part's own axes (one part)
        and UNIFORM."""
        if self.handle_mode not in ("move", "scale") or not self._handles_allowed():
            return None
        if self._hdrag is not None:
            return self._hdrag["layout"]
        parts = self._selected_parts()
        if not parts:
            return None
        lo, hi = self._selection_box(parts)
        length = max(0.55 * float(np.linalg.norm(hi - lo)), 4.0)
        if self.handle_mode == "move":
            base = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2, lo[2]])
            return "move", base, [(i, base + np.eye(3)[i] * length) for i in (0, 1)]
        ends = []
        if len(parts) == 1:
            p = parts[0]
            centre = p.position.copy()                  # the local mesh is centred on its box
            half = p.scale * p.mesh.extents / 2
            gap = max(0.3 * length, 4.0)
            for i in range(3):
                ends.append((i, centre + p.rotation[:, i] * (half[i] + gap)))
        else:
            centre = (lo + hi) / 2
        diag = np.array([1.0, 1.0, 1.0]) / np.sqrt(3)
        ends.append((UNIFORM, hi + diag * max(0.3 * length, 4.0)))
        return "scale", centre, ends

    def _handle_start(self, kind, centre, i, end):
        """Where a handle's line starts: the centre, or the box corner for the uniform one."""
        if kind == "scale" and i == UNIFORM:
            lo, hi = self._selection_box(self._selected_parts()) if self._hdrag is None else self._hdrag["box"]
            return hi
        return centre

    def _visible_handles(self, vp=None):
        """[(index, start xy, end xy)] of handles long enough on screen to be used."""
        h = self.handles()
        if h is None:
            return []
        kind, centre, ends = h
        out = []
        for i, end in ends:
            start = self._handle_start(kind, centre, i, end)
            xy, ok = self._screen(np.array([start, end]), vp)
            if not ok.all():
                continue
            if i != UNIFORM and np.linalg.norm(xy[1] - xy[0]) < MIN_SCREEN_PX:
                continue
            out.append((i, xy[0], xy[1]))
        return out

    def handle_at(self, pos) -> int | None:
        """The handle under a screen position: the end (arrow tip or cube) first, then the line."""
        q = np.array([pos.x(), pos.y()])
        best = None
        for i, a, b in self._visible_handles():
            d = float(np.linalg.norm(q - b))
            if d > HANDLE_GRAB_PX + CUBE_PX:
                d = _point_segment_px(q, a, b) + 2.0       # the line counts, a little less
            if d <= HANDLE_GRAB_PX + CUBE_PX and (best is None or d < best[0]):
                best = (d, i)
        return None if best is None else best[1]

    # ------------------------------------------------------------ dragging
    def _start_handle(self, i: int, pos) -> None:
        parts = self._selected_parts()
        layout = self.handles()
        kind, centre, ends = layout
        lo, hi = self._selection_box(parts)
        d = {"kind": kind, "index": i, "parts": parts, "before": {p.id: p.state() for p in parts},
             "layout": layout, "box": (lo, hi), "pos": pos, "value": 0.0 if kind == "move" else 1.0}
        if kind == "move":
            axis = np.eye(3)[i]
            to_eye = self.camera.eye() - centre
            n = to_eye - np.dot(to_eye, axis) * axis
            if np.linalg.norm(n) < 1e-6:
                n = np.cross(axis, [0, 0, 1.0]) if i != 2 else np.array([1.0, 0, 0])
            d["axis"], d["normal"] = axis, n / np.linalg.norm(n)
            d["t0"] = self._axis_param(d, pos)
        else:
            end = dict(ends)[i]
            # the uniform handle measures from the box centre, so the factor is about its size
            origin = (lo + hi) / 2 if i == UNIFORM else centre
            xy, _ = self._screen(np.array([origin, end]))
            u = xy[1] - xy[0]
            d["origin_xy"], d["dir"] = xy[0], u / max(float(np.linalg.norm(u)), 1e-9)
            d["d0"] = max(float(np.dot(np.array([pos.x(), pos.y()]) - xy[0], d["dir"])), 4.0)
            d["pivot"] = np.array([(lo[0] + hi[0]) / 2, (lo[1] + hi[1]) / 2])
        self._hdrag = d
        self.setCursor(Qt.ClosedHandCursor)

    def _axis_param(self, d, pos) -> float | None:
        o, r = self._ray(pos)
        hit = ray_plane(o, r, d["layout"][1], d["normal"])
        return None if hit is None else float(np.dot(hit - d["layout"][1], d["axis"]))

    def _drag_handle(self, pos, ctrl: bool) -> None:
        d = self._hdrag
        d["pos"] = pos
        if d["kind"] == "move":
            t = self._axis_param(d, pos)
            if t is None or d["t0"] is None:
                return
            value = snap(t - d["t0"], 1.0 if ctrl else 0.01)
            if value == d["value"]:
                return
            d["value"] = value
            for p in d["parts"]:
                b = d["before"][p.id]
                p.position = b.position.copy()
                p.position[d["index"]] += value
        else:
            along = float(np.dot(np.array([pos.x(), pos.y()]) - d["origin_xy"], d["dir"]))
            f = max(snap(along / d["d0"], 0.1 if ctrl else 0.01), MIN_FACTOR)
            if f == d["value"]:
                return
            d["value"] = f
            one_axis = d["index"] != UNIFORM and not self.uniform_scale()
            for p in d["parts"]:
                b = d["before"][p.id]
                s = b.scale.copy()
                if one_axis:
                    s[d["index"]] *= f
                else:
                    s *= f
                p.scale = np.maximum(s, b.scale * MIN_FACTOR)
                p.rotation = b.rotation.copy()
                pos3 = b.position.copy()
                if len(d["parts"]) > 1:
                    pos3[:2] = d["pivot"] + f * (b.position[:2] - d["pivot"])
                p.position = pos3
                self.scene.drop_to_plate(p)
        self.transformLive.emit()
        self.update()

    def _drag_text(self, d) -> str:
        if d["kind"] == "move":
            return f"{'XY'[d['index']]} {d['value']:+.2f} mm"
        pct = f"{d['value'] * 100:.0f} %"
        if d["index"] == UNIFORM or self.uniform_scale():
            return pct
        return f"{'XYZ'[d['index']]} {pct}"

    def _end_handle(self) -> None:
        d, self._hdrag = self._hdrag, None
        self._restore_cursor()
        changes = {p.id: (d["before"][p.id], p.state()) for p in d["parts"] if p.state() != d["before"][p.id]}
        self.update()
        if changes:
            verb = "Move" if d["kind"] == "move" else "Scale"
            self.transformFinished.emit(changes, f"{verb} {self._drag_text(d)}")

    def cancel_handle_drag(self) -> bool:
        """Esc: put the parts back and end a handle or ring drag. True if one was running."""
        d = self._hdrag or self._rot
        if d is None:
            return False
        for p in d["parts"]:
            p.set_state(d["before"][p.id])
        self._hdrag = self._rot = None
        self._restore_cursor()
        self.transformLive.emit()
        self.update()
        return True

    # ------------------------------------------------------------ drawing
    def _draw_label(self, p: QPainter, pos: QPointF, text: str, color) -> None:
        font = QFont(self.font())
        font.setBold(True)
        p.setFont(font)
        at = pos + QPointF(16, -12)
        box = p.fontMetrics().boundingRect(text).adjusted(-6, -3, 6, 3)
        box.moveTopLeft(at.toPoint() - QPointF(6, box.height() - 3).toPoint())
        p.setPen(Qt.NoPen)
        p.setBrush(QColor(0x15, 0x17, 0x1a, 215))
        p.drawRoundedRect(box, 4, 4)
        p.setPen(QColor(color).lighter(130))
        p.drawText(box, Qt.AlignCenter, text)

    def _draw_handles(self, p: QPainter, vp) -> None:
        vis = self._visible_handles(vp)
        if not vis:
            return
        d = self._hdrag
        kind = self.handles()[0]
        p.save()
        p.setRenderHint(QPainter.Antialiasing)
        for i, a, b in vis:
            if d is not None and i != d["index"]:
                continue
            hot = (d is not None and i == d["index"]) or i == self._handle_hover
            col = QColor("#f2f2f2" if i == UNIFORM else AXIS_COLORS[i])
            if hot:
                col = col.lighter(125)
            p.setPen(QPen(col, 3.4 if hot else 2.4, Qt.SolidLine, Qt.RoundCap))
            A, B = QPointF(*a), QPointF(*b)
            p.drawLine(A, B)
            p.setPen(QPen(QColor(0x15, 0x17, 0x1a), 1.0))
            p.setBrush(col)
            if kind == "move":
                u = (b - a) / max(float(np.linalg.norm(b - a)), 1e-9)
                n = np.array([-u[1], u[0]])
                tip, back = b + u * 6, b - u * 9
                p.drawPolygon(QPolygonF([QPointF(*tip), QPointF(*(back + n * 6)), QPointF(*(back - n * 6))]))
            else:
                s = CUBE_PX + (1.5 if hot else 0)
                p.drawRect(B.x() - s, B.y() - s, 2 * s, 2 * s)
        if d is not None:
            self._draw_label(p, d["pos"], self._drag_text(d),
                             "#f2f2f2" if d["index"] == UNIFORM else AXIS_COLORS[min(d["index"], 2)])
        p.restore()
