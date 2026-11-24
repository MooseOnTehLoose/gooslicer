"""GPU slicing (experimental) against the CPU rasteriser. Real GL: run via scripts/test-gui.sh."""
import os
import threading

import numpy as np
import pytest

if os.environ.get("GOOSLICER_GL_TESTS") != "1":
    pytest.skip("GPU slicing tests need scripts/test-gui.sh (Xvfb)", allow_module_level=True)

import trimesh  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from elegoo_goo import rle  # noqa: E402
from gooslicer import gpu_raster as G  # noqa: E402
from gooslicer.raster import RasterSpec, rasterize_bitmap  # noqa: E402
from gooslicer.settings import PrintSettings  # noqa: E402
from gooslicer.slicer import SliceJob  # noqa: E402

SPEC = RasterSpec(600, 400, 50.0, 60.0)              # 30 x 24 mm, non-square pixels like the 16K


@pytest.fixture(scope="module")
def gl():
    app = QApplication.instance() or QApplication([])
    from gooslicer_qt.gpuslice import ThreadGL
    t = ThreadGL()
    with t:
        yield app
    t.destroy()


def _cpu(meshes, z, spec=SPEC):
    from gooslicer.slicer import _layer_polys
    job = SliceJob(meshes, spec, PrintSettings(), "test")
    return rasterize_bitmap(_layer_polys(job._bodies, z), spec)


def _box(size, at=(0, 0, 0)):
    m = trimesh.creation.box(size)
    m.apply_translation(np.asarray(at, float) + [0, 0, size[2] / 2])
    return m


def test_box_matches_cpu_exactly(gl):
    m = _box((10.3, 7.7, 5), (1.13, -2.4, 0))
    g = G.GpuRasterizer([m], SPEC)
    img = g.bitmap(2.5)
    assert img.sum() > 0 and (img == _cpu([m], 2.5)).all()
    assert not g.bitmap(6.0).any()                     # above the top: nothing
    g.close()


@pytest.mark.parametrize("mx,my", [(False, True), (True, True), (False, False), (True, False)])
def test_mirroring_and_curves_match_cpu(gl, mx, my):
    spec = RasterSpec(600, 400, 50.0, 60.0, mx, my)
    sph = trimesh.creation.icosphere(4, 8)
    sph.apply_translation((3, 2, 8))
    g = G.GpuRasterizer([sph], spec)
    for z in (4.0, 8.0, 11.5):
        a, b = g.bitmap(z), _cpu([sph], z, spec)
        diff = int((a != b).sum())
        # only pixels whose centre is within a hair of the outline may differ
        assert diff <= max(4, 0.01 * (b > 0).sum()), (z, diff)
    g.close()


def test_winding_union_and_hollow(gl):
    a = _box((10, 10, 6), (-3, 0, 0))
    b = _box((10, 10, 6), (3, 0, 0))                   # overlaps a: union, not a hole
    outer = _box((12, 12, 6), (0, 0, 0))
    inner = _box((6, 6, 4), (0, 0, 1))
    inner.invert()                                     # a sealed void inside
    hollow = trimesh.util.concatenate([outer, inner])
    g = G.GpuRasterizer([a, b], SPEC)
    assert (g.bitmap(3.0) == _cpu([a, b], 3.0)).all()
    g.close()
    g = G.GpuRasterizer([hollow], SPEC)
    img = g.bitmap(3.0)
    assert (img == _cpu([hollow], 3.0)).all()
    c = SPEC.to_pixels(np.array([[0.0, 0.0]]))[0].astype(int)
    assert img[c[1], c[0]] == 0                        # the void stays empty
    g.close()


def test_tiles_join_seamlessly(gl, monkeypatch):
    sph = trimesh.creation.icosphere(4, 11)            # 22 mm: about 440 x 370 pixels
    sph.apply_translation((0, 0, 11))
    whole = G.GpuRasterizer([sph], SPEC)
    ref = whole.bitmap(11.0)
    whole.close()
    monkeypatch.setattr(G, "TILE", 64)                 # the floor is 256
    g = G.GpuRasterizer([sph], SPEC)
    assert g.tile == 256 and len(list(g.tiles())) == 4
    assert (g.bitmap(11.0) == ref).all()
    # and the encoded layer is the same as encoding the full bitmap
    data = next(g.iter_rle([11.0]))
    assert data == rle.encode(ref.tobytes())
    g.close()


def test_slicejob_gpu_path_and_fallback(gl):
    m = _box((10, 10, 2), (0, 0, 0))
    job = SliceJob([m], SPEC, PrintSettings(), "test")
    cpu = list(job.iter_rle(workers=1))
    assert job.backend == "cpu" and job.backend_note == ""
    seen = []
    gpu = list(job.iter_rle(gpu=True, progress=lambda d, n: seen.append((d, n))))
    assert job.backend.startswith("gpu (") and gpu == cpu and seen[-1] == (len(cpu), len(cpu))
    res = job.build(gpu=True)
    assert res.backend.startswith("gpu") and res.seconds > 0 and res.layers == len(cpu)

    # a thread without an OpenGL context falls back to the CPU and says why
    out = {}

    def other():
        out["layers"] = list(job.iter_rle(workers=1, gpu=True))
        out["backend"], out["note"] = job.backend, job.backend_note
    th = threading.Thread(target=other)
    th.start()
    th.join()
    assert out["layers"] == cpu and out["backend"] == "cpu" and "sliced on the CPU" in out["note"]


def test_cube_matches_satellite_on_the_gpu(gl):
    from pathlib import Path
    from elegoo_goo import goo
    sample = Path(__file__).resolve().parent.parent / "samples" / "10mmcube.goo"
    if not sample.exists():
        pytest.skip("sample missing")
    from test_slicer import ABS_LIKE_16K, SPEC16K
    cube = trimesh.creation.box((10, 10, 10))
    cube.apply_translation((-15.869, 1.51, 5))         # as test_cube_matches_satellite_byte_for_byte
    job = SliceJob([cube], SPEC16K, ABS_LIKE_16K, "ELEGOO Saturn 4 Ultra 16K")
    layers = list(job.iter_rle(gpu=True))
    assert job.backend.startswith("gpu")
    assert layers == [x.data for x in goo.read(sample).layers]
