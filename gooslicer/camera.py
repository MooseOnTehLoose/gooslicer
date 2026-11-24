"""Orbit camera maths (Qt-free, column-vector convention, matrices returned row-major)."""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np


def perspective(fov_y_deg: float, aspect: float, near: float, far: float) -> np.ndarray:
    f = 1.0 / math.tan(math.radians(fov_y_deg) / 2)
    m = np.zeros((4, 4))
    m[0, 0], m[1, 1] = f / aspect, f
    m[2, 2], m[2, 3] = (far + near) / (near - far), 2 * far * near / (near - far)
    m[3, 2] = -1.0
    return m


def look_at(eye, target, up=(0, 0, 1)) -> np.ndarray:
    eye, target, up = (np.asarray(x, float) for x in (eye, target, up))
    f = target - eye
    f /= np.linalg.norm(f)
    s = np.cross(f, up)
    if np.linalg.norm(s) < 1e-9:
        s = np.cross(f, (0, 1, 0))
    s /= np.linalg.norm(s)
    u = np.cross(s, f)
    m = np.eye(4)
    m[0, :3], m[1, :3], m[2, :3] = s, u, -f
    m[:3, 3] = -m[:3, :3] @ eye
    return m


@dataclass
class OrbitCamera:
    target: np.ndarray = field(default_factory=lambda: np.zeros(3))
    distance: float = 300.0
    yaw: float = -60.0     # degrees around +Z, 0 = looking from +X... see eye()
    pitch: float = 30.0    # degrees above the plate
    fov: float = 35.0

    def eye(self) -> np.ndarray:
        y, p = math.radians(self.yaw), math.radians(self.pitch)
        d = np.array([math.cos(p) * math.cos(y), math.cos(p) * math.sin(y), math.sin(p)])
        return self.target + self.distance * d

    def view(self) -> np.ndarray:
        return look_at(self.eye(), self.target)

    def projection(self, aspect: float) -> np.ndarray:
        return perspective(self.fov, aspect, max(self.distance * 0.01, 0.1), self.distance * 20 + 2000)

    def orbit(self, d_yaw: float, d_pitch: float) -> None:
        self.yaw = (self.yaw + d_yaw) % 360.0
        self.pitch = float(np.clip(self.pitch + d_pitch, -89.0, 89.0))

    def zoom(self, factor: float) -> None:
        self.distance = float(np.clip(self.distance * factor, 5.0, 5000.0))

    def pan(self, dx_px: float, dy_px: float, viewport_h: float) -> None:
        """Move target in the view plane so the scene follows the mouse."""
        v = self.view()
        right, up = v[0, :3], v[1, :3]
        world_per_px = 2 * self.distance * math.tan(math.radians(self.fov) / 2) / max(viewport_h, 1)
        self.target = self.target - right * dx_px * world_per_px + up * dy_px * world_per_px

    def frame(self, lo, hi) -> None:
        lo, hi = np.asarray(lo, float), np.asarray(hi, float)
        self.target = (lo + hi) / 2
        radius = max(np.linalg.norm(hi - lo) / 2, 1.0)
        self.distance = radius / math.sin(math.radians(self.fov) / 2) * 1.1

    def ray(self, x: float, y: float, w: float, h: float) -> tuple[np.ndarray, np.ndarray]:
        """World-space ray through pixel (x, y) with y down from the top-left."""
        ndc = np.array([2 * x / w - 1, 1 - 2 * y / h])
        inv = np.linalg.inv(self.projection(w / h) @ self.view())
        p0 = inv @ np.array([ndc[0], ndc[1], -1, 1])
        p1 = inv @ np.array([ndc[0], ndc[1], 1, 1])
        p0, p1 = p0[:3] / p0[3], p1[:3] / p1[3]
        d = p1 - p0
        return p0, d / np.linalg.norm(d)


def ray_plane(origin, direction, point, normal) -> np.ndarray | None:
    denom = float(np.dot(direction, normal))
    if abs(denom) < 1e-12:
        return None
    t = float(np.dot(np.asarray(point) - origin, normal)) / denom
    return None if t < 0 else np.asarray(origin) + t * np.asarray(direction)


def axis_gizmo(view: np.ndarray) -> list[tuple[str, int, float, float, float]]:
    """The world axes as the camera sees them, for an orientation compass: one entry per axis
    end (name, sign ±1, x, y, depth). x/y are unit-length screen offsets (x right, y down),
    taken from the view rotation only (no pan, zoom or perspective). depth grows toward the
    viewer. Sorted far to near, the order to draw them in."""
    R = np.asarray(view, float)[:3, :3]
    out = []
    for i, name in enumerate("XYZ"):
        v = R[:, i]                         # camera-space direction of the world axis
        for sign in (1, -1):
            out.append((name, sign, float(sign * v[0]), float(-sign * v[1]), float(sign * v[2])))
    out.sort(key=lambda a: a[4])
    return out
