"""Slice scene parts into a .goo file (Qt-free; parallel across processes)."""
from __future__ import annotations

import math
import multiprocessing as mp
import os
import struct
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, Iterator

import manifold3d as m3
import numpy as np
import trimesh

from elegoo_goo import goo

from . import __version__
from .raster import RasterSpec, rasterize_rle
from .settings import PrintSettings

MIN_MOTION = 0.05  # SatelLite writes 0.05 where a motion value is 0 [OBSERVED in samples]


class SliceCancelled(Exception):
    pass


# ----------------------------------------------------------------- geometry
class _Body:
    """One part ready for slicing: a manifold if possible, else trimesh sections."""

    def __init__(self, vertices: np.ndarray, faces: np.ndarray):
        self.mf = None
        mesh = m3.Mesh(vert_properties=np.asarray(vertices, np.float32),
                       tri_verts=np.asarray(faces, np.uint32))
        mf = m3.Manifold(mesh)
        if mf.status() == m3.Error.NoError and not mf.is_empty():
            self.mf = mf
        else:
            self.tm = trimesh.Trimesh(vertices, faces, process=True)
        self.zmin = float(np.min(vertices[:, 2]))
        self.zmax = float(np.max(vertices[:, 2]))

    def section(self, z: float) -> list[np.ndarray]:
        if z < self.zmin or z > self.zmax:
            return []
        if self.mf is not None:
            return [np.asarray(p, np.float64) for p in self.mf.slice(z).to_polygons() if len(p) >= 3]
        sec = self.tm.section(plane_origin=(0, 0, z), plane_normal=(0, 0, 1))
        if sec is None:
            return []
        polys = []
        # `.discrete` drops open paths; open contours (holes in the mesh) are closed with a
        # straight segment so the gap is bridged rather than the whole outline lost
        for ent in sec.entities:
            pts = np.asarray(ent.discrete(sec.vertices))[:, :2]
            if len(pts) > 1 and np.allclose(pts[0], pts[-1]):
                pts = pts[:-1]
            if len(pts) >= 3:
                polys.append(pts)
        return _orient_even_odd(_join_open(polys))


def _join_open(polys: list[np.ndarray], tol: float = 1e-6) -> list[np.ndarray]:
    """Chain polylines whose end points touch (section output can be fragmented)."""
    polys = [p for p in polys]
    changed = True
    while changed and len(polys) > 1:
        changed = False
        for i in range(len(polys)):
            for j in range(len(polys)):
                if i == j:
                    continue
                a, b = polys[i], polys[j]
                if np.linalg.norm(a[-1] - b[0]) < tol:
                    polys[i] = np.vstack([a, b[1:]])
                elif np.linalg.norm(a[-1] - b[-1]) < tol:
                    polys[i] = np.vstack([a, b[::-1][1:]])
                else:
                    continue
                polys.pop(j)
                changed = True
                break
            if changed:
                break
    return polys


def _orient_even_odd(polys: list[np.ndarray]) -> list[np.ndarray]:
    """For non-manifold fallbacks: orient contours so non-zero fill equals even-odd nesting."""
    if not polys:
        return polys
    from shapely.geometry import Point, Polygon
    shapes = [Polygon(p) for p in polys]
    out = []
    for i, (p, s) in enumerate(zip(polys, shapes)):
        if not s.is_valid or s.area == 0:
            out.append(p)
            continue
        rep = s.representative_point()
        depth = sum(1 for j, o in enumerate(shapes) if j != i and o.is_valid and o.contains(Point(rep)))
        ccw = s.exterior.is_ccw
        want_ccw = depth % 2 == 0
        out.append(p if ccw == want_ccw else p[::-1])
    return out


_WORKER: dict = {}


def _init_worker(bodies, spec, zs):
    _WORKER["bodies"] = [_Body(v, f) for v, f in bodies]
    _WORKER["spec"] = spec
    _WORKER["zs"] = zs


def _layer_polys(bodies, z):
    polys = []
    for b in bodies:
        polys.extend(b.section(z))
    return polys


def _work(indices):
    bodies, spec, zs = _WORKER["bodies"], _WORKER["spec"], _WORKER["zs"]
    return [rasterize_rle(_layer_polys(bodies, zs[i]), spec) for i in indices]


# ------------------------------------------------------------------- job
@dataclass
class SliceResult:
    path: str
    layers: int
    print_time: int
    volume: float
    weight: float
    price: float
    header: goo.Header = field(repr=False)


@dataclass
class SliceJob:
    meshes: list[trimesh.Trimesh]          # world coordinates (plate mm)
    spec: RasterSpec
    settings: PrintSettings
    machine_name: str
    machine_z: float = 220.0
    preview_rgb: tuple[bytes, int, int] | None = None   # packed RGB, width, height

    def __post_init__(self):
        self._bodies = [_Body(np.asarray(m.vertices), np.asarray(m.faces)) for m in self.meshes]

    # -------------------------------------------------------------- layers
    @property
    def height(self) -> float:
        return max((b.zmax for b in self._bodies), default=0.0)

    @property
    def layer_count(self) -> int:
        """Layers whose cut height lies below the model top.

        SatelLite (EleTriElem FUN_18000ec40 / FUN_18000e850) [OBSERVED] creates
        int((zmax - base)/h + 1) layers at z_i = base + i*h + {0, h/2, h}[cutposition], then drops
        empty layers at both ends (VoxSlicer::StartSlicing). With base = 0 that equals
        ceil(zmax/h - f); e.g. a 10.01 mm part at 0.05 mm gives 200 layers, not 201.
        """
        h, f = self.settings.layer_height, self.settings.slice_z_fraction
        return max(0, math.ceil(self.height / h - f - 1e-6))

    def layer_z(self) -> list[float]:
        h, f = self.settings.layer_height, self.settings.slice_z_fraction
        return [(i + f) * h for i in range(self.layer_count)]

    def layer_polygons(self, i: int) -> list[np.ndarray]:
        return _layer_polys(self._bodies, self.layer_z()[i])

    def volume(self) -> float:
        mfs = [b.mf for b in self._bodies if b.mf is not None]
        vol = m3.Manifold.batch_boolean(mfs, m3.OpType.Add).volume() if mfs else 0.0
        return float(vol + sum(abs(b.tm.volume) for b in self._bodies if b.mf is None))

    # ----------------------------------------------------------- encoding
    def iter_rle(self, workers: int | None = None, chunk: int = 8,
                 progress: Callable[[int, int], None] | None = None,
                 cancelled: Callable[[], bool] | None = None) -> Iterator[bytes]:
        n = self.layer_count
        zs = self.layer_z()
        workers = workers if workers is not None else max(1, (os.cpu_count() or 2) - 1)
        chunks = [list(range(s, min(n, s + chunk))) for s in range(0, n, chunk)]
        done = 0
        if workers <= 1 or n < 2 * chunk:
            _init_worker([(np.asarray(m.vertices), np.asarray(m.faces)) for m in self.meshes], self.spec, zs)
            results = map(_work, chunks)
            ex = None
        else:
            ex = ProcessPoolExecutor(max_workers=workers, mp_context=mp.get_context("spawn"),
                                     initializer=_init_worker,
                                     initargs=([(np.asarray(m.vertices), np.asarray(m.faces)) for m in self.meshes],
                                               self.spec, zs))
            results = ex.map(_work, chunks)
        try:
            for batch in results:
                if cancelled and cancelled():
                    raise SliceCancelled()
                for data in batch:
                    yield data
                done += len(batch)
                if progress:
                    progress(done, n)
        finally:
            if ex is not None:
                ex.shutdown(wait=False, cancel_futures=True)

    # -------------------------------------------------------------- header
    def estimate_print_time(self) -> int:
        return estimate_print_time(self.settings, self.layer_count)

    def build_header(self, volume: float) -> goo.Header:
        s, sp = self.settings, self.spec
        f32 = np.float32  # SatelLite computes these in float (1.15 g × 30 / 1000 -> 0.034) [SAMPLE]
        weight = round(float(f32(volume) * f32(s.density) / f32(1000.0)), 3)
        price = round(float(f32(weight) * f32(s.cost) / f32(1000.0)), 3)
        m = lambda v: max(float(v), MIN_MOTION)  # noqa: E731
        h = goo.Header(
            version="V3.0", software_name="GooSlicer", software_version=__version__,
            file_time=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            machine_name=self.machine_name, machine_type=self.machine_name, resin_profile=s.resin_name,
            layer_count=self.layer_count, resolution_x=sp.res_x, resolution_y=sp.res_y,
            mirror_x=int(sp.mirror_x), mirror_y=int(sp.mirror_y),
            display_width=sp.res_x * sp.pixel_x_um / 1000, display_height=sp.res_y * sp.pixel_y_um / 1000,
            machine_z=self.machine_z, layer_height=s.layer_height, exposure_time=s.exposure,
            delay_mode=1, light_off_delay=s.light_off_delay,
            bottom_before_lift_time=s.bottom_wait_before_lift, bottom_after_lift_time=s.bottom_wait_after_lift,
            bottom_after_retract_time=s.bottom_wait_after_retract,
            before_lift_time=s.wait_after_cure, after_lift_time=0.0,
            after_retract_time=s.bottom_wait_after_retract,      # [INFERRED] see spec §3
            bottom_exposure_time=s.bottom_exposure,
            bottom_layers=min(s.bottom_layers, self.layer_count),
            bottom_lift_distance=m(s.bottom_lift_distance), bottom_lift_speed=m(s.bottom_lift_speed),
            lift_distance=m(s.lift_distance), lift_speed=m(s.lift_speed),
            bottom_retract_distance=m(s.bottom_lift_distance), bottom_retract_speed=m(s.bottom_retract_speed),
            retract_distance=m(s.lift_distance), retract_speed=m(s.retract_speed),
            bottom_second_lift_distance=s.slow_bottom_lift_distance,
            bottom_second_lift_speed=s.slow_bottom_lift_speed,
            second_lift_distance=s.slow_lift_distance, second_lift_speed=s.slow_lift_speed,
            bottom_second_retract_distance=s.slow_bottom_retract_distance,
            bottom_second_retract_speed=s.slow_bottom_retract_speed,
            second_retract_distance=s.slow_retract_distance, second_retract_speed=s.slow_retract_speed,
            bottom_light_pwm=s.bottom_light_pwm, light_pwm=s.light_pwm, advance_mode=0,
            print_time=self.estimate_print_time(), volume=round(volume, 3), weight=weight, price=price,
            price_unit=s.price_unit, grey_scale_level=0, transition_layers=s.transition_layers,
        )
        if self.preview_rgb:
            rgb, w, hgt = self.preview_rgb
            le = self.machine_name == "ELEGOO Jupiter 2"
            h.small_preview = goo.rgb565_preview(rgb, w, hgt, goo.SMALL_PREVIEW, le)
            h.big_preview = goo.rgb565_preview(rgb, w, hgt, goo.BIG_PREVIEW, le)
        return h

    def layer_template(self) -> goo.LayerDef:
        s = self.settings
        m = lambda v: max(float(v), MIN_MOTION)  # noqa: E731
        return goo.LayerDef(
            pause_flag=0, pause_z=200.0, light_off_time=s.light_off_delay,
            before_lift_time=s.wait_after_cure, after_lift_time=0.0,
            after_retract_time=s.bottom_wait_after_retract,
            lift_distance=m(s.lift_distance), lift_speed=m(s.lift_speed),
            second_lift_distance=m(s.slow_lift_distance), second_lift_speed=m(s.slow_lift_speed),
            retract_distance=m(s.lift_distance), retract_speed=m(s.retract_speed),
            second_retract_distance=m(s.slow_retract_distance), second_retract_speed=m(s.slow_retract_speed),
            light_pwm=s.light_pwm)

    # --------------------------------------------------------------- write
    def write(self, path: str, **kw) -> SliceResult:
        if self.layer_count == 0:
            raise ValueError("nothing to slice")
        vol = self.volume()
        header = self.build_header(vol)
        tmpl = self.layer_template()
        layers = (goo.Layer(goo.default_layer_def(header, i, tmpl), data)
                  for i, data in enumerate(self.iter_rle(**kw)))
        n = goo.write(path, header, layers)
        return SliceResult(path, n, header.print_time, vol, header.weight, header.price, header)


def estimate_print_time(s: PrintSettings, layers: int) -> int:
    """Print-time estimate in seconds.

    Structure from FUN_18000cf70 [OBSERVED]: 0.93 × (exposures + waits + per-layer motion).
    Per-layer motion for printers that manage motion themselves (all-zero profile values, e.g.
    the 16K) is calibrated on samples/10mmcube.goo (200 layers -> 1386 s) and samples/arc.goo
    (1140 layers -> 6556 s): 2.914 s/layer + 20.6 s [INFERRED]. With explicit lift/retract values
    the motion time is distance/speed (mm/min).
    """
    from elegoo_goo.goo import Header, layer_exposure
    hdr = Header(bottom_layers=min(s.bottom_layers, layers), transition_layers=s.transition_layers,
                 bottom_exposure_time=s.bottom_exposure, exposure_time=s.exposure)
    exposure = sum(layer_exposure(hdr, i) for i in range(layers))
    waits = layers * (s.bottom_wait_after_retract + s.wait_after_cure + s.light_off_delay)

    def motion(dist, up, down):
        return 60.0 * (dist / up + dist / down) if dist > 0 and up > 0 and down > 0 else None
    nb = min(s.bottom_layers, layers)
    mb = motion(s.bottom_lift_distance, s.bottom_lift_speed, s.bottom_retract_speed)
    mn = motion(s.lift_distance, s.lift_speed, s.retract_speed)
    move = (nb * mb + (layers - nb) * mn) if mb is not None and mn is not None else 2.914 * layers + 20.6
    return int(round(0.93 * (exposure + waits + move)))


def packed_rgb(img: np.ndarray) -> tuple[bytes, int, int]:
    h, w = img.shape[:2]
    return np.ascontiguousarray(img[:, :, :3], np.uint8).tobytes(), w, h
