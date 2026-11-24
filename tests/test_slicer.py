import json
from dataclasses import fields
from pathlib import Path

import numpy as np
import pytest
import trimesh

from elegoo_goo import goo, rle
from gooslicer import preview, raster as R, settings as S, slicer as SL

ROOT = Path(__file__).resolve().parent.parent
CUBE = ROOT / "samples" / "10mmcube.goo"
SPEC16K = R.RasterSpec(15120, 6230, 14.0, 19.0, False, True)

# The values of SatelLite's "Space Grey 8K ABS-Like 3.0" profile for the 16K (resin 1544.data)
ABS_LIKE_16K = S.PrintSettings(
    resin_name="Space Grey 8K ABS-Like 3.0", layer_height=0.05, bottom_layers=5, exposure=1.8,
    bottom_exposure=32.0, transition_layers=10, bottom_light_off_delay=1.2,
    bottom_wait_after_retract=1.2, density=1.15, cost=30.0)


# ---------------------------------------------------------------- raster
def bitmap(polys, w=40, h=30):
    img = np.zeros((h, w), np.uint8)
    for r, a, b in zip(*R.spans([np.asarray(p, np.float32) for p in polys], w, h)):
        img[r, a:b] = 1
    return img


def test_square_pixel_centres():
    img = bitmap([[(2.4, 3.6), (7.6, 3.6), (7.6, 6.4), (2.4, 6.4)]])
    ys, xs = np.nonzero(img)
    assert (xs.min(), xs.max(), ys.min(), ys.max()) == (2, 7, 4, 5)


def test_nonzero_winding_hole_and_overlap():
    outer = [(1, 1), (20, 1), (20, 20), (1, 20)]
    hole_cw = outer and [(5, 5), (5, 15), (15, 15), (15, 5)]     # opposite direction -> hole
    same_dir = [(5, 5), (15, 5), (15, 15), (5, 15)]               # same direction -> stays filled
    assert bitmap([outer, hole_cw])[10, 10] == 0
    assert bitmap([outer, same_dir])[10, 10] == 1
    a, b = [(1, 1), (10, 1), (10, 10), (1, 10)], [(5, 5), (14, 5), (14, 14), (5, 14)]
    u = bitmap([a, b])
    assert u[7, 7] == 1 and u.sum() == bitmap([a]).sum() + bitmap([b]).sum() - bitmap([[(5, 5), (10, 5), (10, 10), (5, 10)]]).sum()


def test_self_intersecting_star_nonzero_fills_centre():
    ang = np.radians(np.arange(5) * 144 + 90)
    star = np.stack([20 + 12 * np.cos(ang), 15 + 12 * np.sin(ang)], 1)
    assert bitmap([star])[15, 20] == 1   # even-odd would leave the pentagon empty


def test_transform_and_mirrors():
    s = R.RasterSpec(100, 50, 10.0, 20.0, mirror_x=False, mirror_y=False)
    # origin at plate centre: (0,0) mm -> (50, 25) px; +y goes up the image (row decreases)
    assert np.allclose(s.to_pixels([[0, 0], [1, 1]]), [[50, 25], [150, -25]])
    my = R.RasterSpec(100, 50, 10.0, 20.0, mirror_y=True)
    assert np.allclose(my.to_pixels([[1, 1]]), [[150, 75]])
    mx = R.RasterSpec(100, 50, 10.0, 20.0, mirror_x=True, mirror_y=False)
    assert np.allclose(mx.to_pixels([[1, 0]]), [[-50, 25]])


def test_runs_and_rle_roundtrip():
    spec = R.RasterSpec(64, 32, 1000.0, 1000.0, mirror_y=False)
    polys = [np.array([[-10, -5], [12, -5], [12, 8], [-10, 8]], float)]
    data = R.rasterize_rle(polys, spec)
    img = np.frombuffer(bytes(rle.decode(data, 64, 32)), np.uint8).reshape(32, 64)
    assert np.array_equal(img, R.rasterize_bitmap(polys, spec))
    assert img.sum() // 255 == 22 * 13
    assert R.rasterize_rle([], spec) == rle.encode(bytes(64 * 32))


def test_matches_real_skia():
    skia = pytest.importorskip("skia")
    rng = np.random.default_rng(7)
    W, H = 240, 160
    for _ in range(150):
        polys = []
        for _ in range(rng.integers(1, 4)):
            n = rng.integers(3, 20)
            c = rng.uniform([50, 50], [W - 50, H - 50])
            ang = rng.uniform(0, 2 * np.pi, n)
            if rng.random() < 0.6:
                ang = np.sort(ang)
            pts = c + rng.uniform(1, 45, n)[:, None] * np.stack([np.cos(ang), np.sin(ang)], 1)
            if rng.random() < 0.3:
                pts = np.round(pts * 2) / 2
            polys.append(pts.astype(np.float32))
        arr = np.zeros((H, W), np.uint8)
        with skia.Surface(arr, colorType=skia.kGray_8_ColorType, alphaType=skia.kOpaque_AlphaType) as c:
            p = skia.Path()
            for poly in polys:
                p.moveTo(*map(float, poly[0]))
                for q in poly[1:]:
                    p.lineTo(*map(float, q))
                p.close()
            p.setFillType(skia.PathFillType.kWinding)
            c.drawPath(p, skia.Paint(Color=skia.ColorWHITE, AntiAlias=False))
        assert np.array_equal(arr > 0, bitmap(polys, W, H) > 0)


# ---------------------------------------------------------------- slicer
@pytest.mark.skipif(not CUBE.exists(), reason="sample missing")
def test_cube_matches_satellite_byte_for_byte(tmp_path):
    cube = trimesh.creation.box((10, 10, 10))
    cube.apply_translation((-15.869, 1.51, 5))   # placement recovered from the sample
    job = SL.SliceJob([cube], SPEC16K, ABS_LIKE_16K, "ELEGOO Saturn 4 Ultra 16K")
    res = job.write(str(tmp_path / "c.goo"), workers=1)
    a, b = goo.read(res.path), goo.read(CUBE)
    assert len(a.layers) == 200
    for x, y in zip(a.layers, b.layers):
        assert x.data == y.data and x.definition.pack() == y.definition.pack()
    skip = {"software_name", "software_version", "file_time", "small_preview", "big_preview", "raw_strings"}
    for f in fields(goo.Header):
        if f.name not in skip:
            assert getattr(a.header, f.name) == getattr(b.header, f.name), f.name


def test_print_time_estimate_matches_samples():
    assert SL.estimate_print_time(ABS_LIKE_16K, 200) == 1386     # 10mmcube.goo
    assert SL.estimate_print_time(ABS_LIKE_16K, 1140) == 6556    # arc.goo
    moving = S.with_overrides(ABS_LIKE_16K, lift_distance=5, lift_speed=60, retract_speed=120,
                              bottom_lift_distance=5, bottom_lift_speed=60, bottom_retract_speed=120)
    assert SL.estimate_print_time(moving, 100) > SL.estimate_print_time(ABS_LIKE_16K, 100) - 300


def test_parallel_equals_serial_and_cancel(tmp_path):
    spec = R.RasterSpec(800, 400, 100.0, 100.0)
    s = S.with_overrides(ABS_LIKE_16K, layer_height=0.5)
    sph = trimesh.creation.icosphere(subdivisions=3, radius=10)
    sph.apply_translation((0, 0, 10))
    job = SL.SliceJob([sph], spec, s, "ELEGOO Saturn 4")
    serial = list(job.iter_rle(workers=1))
    par = list(job.iter_rle(workers=3, chunk=4))
    assert serial == par and len(serial) == 40
    with pytest.raises(SL.SliceCancelled):
        for _ in job.iter_rle(workers=1, chunk=4, cancelled=lambda: True):
            pass


def test_layer_count_and_slice_heights():
    s = S.with_overrides(ABS_LIKE_16K, slice_z_fraction=0.5)
    box = trimesh.creation.box((5, 5, 1.0))
    box.apply_translation((0, 0, 0.5))
    job = SL.SliceJob([box], SPEC16K, s, "x")
    assert job.layer_count == 20 and job.layer_z()[0] == pytest.approx(0.025)
    top = SL.SliceJob([box], SPEC16K, S.with_overrides(s, slice_z_fraction=1.0), "x")
    assert top.layer_count == 19 and top.layer_z()[-1] == pytest.approx(0.95)   # cut at 1.0 would be empty
    tall = trimesh.creation.box((5, 5, 10.01))
    tall.apply_translation((0, 0, 5.005))
    assert SL.SliceJob([tall], SPEC16K, s, "x").layer_count == 200   # SatelLite: 201 created, empty top dropped


def test_non_manifold_mesh_falls_back(tmp_path):
    box = trimesh.creation.box((10, 10, 10))
    box.apply_translation((0, 0, 5))
    open_box = trimesh.Trimesh(box.vertices, box.faces[:-2], process=False)  # missing top
    job = SL.SliceJob([open_box], R.RasterSpec(200, 200, 100.0, 100.0), ABS_LIKE_16K, "x")
    assert job._bodies[0].mf is None
    img = R.rasterize_bitmap(job.layer_polygons(50), job.spec)
    assert (img > 0).sum() == 100 * 100


def test_two_overlapping_parts_union(tmp_path):
    a = trimesh.creation.box((10, 10, 2))
    b = a.copy()
    b.apply_translation((5, 0, 0))
    a.apply_translation((0, 0, 1))
    b.apply_translation((0, 0, 1))
    job = SL.SliceJob([a, b], R.RasterSpec(400, 400, 100.0, 100.0), ABS_LIKE_16K, "x")
    img = R.rasterize_bitmap(job.layer_polygons(5), job.spec)
    assert (img > 0).sum() == 150 * 100
    assert job.volume() == pytest.approx(300.0, rel=1e-4)


def test_preview_in_header(tmp_path):
    cube = trimesh.creation.box((10, 10, 10))
    cube.apply_translation((0, 0, 5))
    rgb = SL.packed_rgb(preview.render([cube]))
    job = SL.SliceJob([cube], R.RasterSpec(200, 100, 200.0, 200.0), ABS_LIKE_16K, "ELEGOO Saturn 4",
                      preview_rgb=rgb)
    h = goo.read(job.write(str(tmp_path / "p.goo"), workers=1).path).header
    assert any(h.small_preview) and any(h.big_preview)
    assert len(h.big_preview) == 290 * 290 * 2


# --------------------------------------------------------------- settings
def test_settings_from_resin_json(tmp_path):
    ap = {"vd_resine_profilename": "Test Resin",
          "cloudResinExtraParam": {"cloudResinPrinterName": "ELEGOO Saturn 4 Ultra 16K"},
          "vd_printer_printer_para": {"vd_printer_printer_para_exposure_time": 1.7999999523162842,
                                      "vd_printer_printer_para_bottom_layer": 5,
                                      "vd_printer_printer_bottom_wait_after_retract": 1.2000000476837158,
                                      "vd_printer_printer_para_thickness": 0.05000000074505806},
          "vd_printer_advanced_para": {"vd_printer_advanced_para_led_power": 200},
          "vd_printer_resin_para": {"vd_printer_resin_para_density": 1.149999976158142,
                                    "vd_printer_resin_para_type": "ABS-Like"}}
    d = tmp_path / "resources" / "resin" / "material"
    d.mkdir(parents=True)
    (d / "7.data").write_text(json.dumps({"platitem": {"vd_printer": {"vd_active_printer": ap}}}), "utf-8-sig")
    (d / "8.data").write_text("not json")
    ini = tmp_path / "resources" / "machines" / "printer" / "officialPlatform.ini"
    ini.parent.mkdir(parents=True)
    ini.write_text("")
    assert S.resin_dir_for(ini) == d.resolve()
    rs = S.load_resin_profiles(d, "ELEGOO Saturn 4 Ultra 16K")
    assert [r.name for r in rs] == ["Test Resin"] and rs[0].resin_type == "ABS-Like"
    s = rs[0].settings()
    assert (s.exposure, s.bottom_layers, s.bottom_wait_after_retract, s.layer_height, s.light_pwm, s.density) \
        == (1.8, 5, 1.2, 0.05, 200, 1.15)
    assert S.load_resin_profiles(d, "other printer") == []


def test_settings_from_printer_profile():
    from gooslicer import profiles as P
    from test_profiles import FIXTURE
    text = FIXTURE.replace("Printer\\BottomLayerCount=5",
                           "Printer\\BottomLayerCount=5\nPrinter\\ExposureTime=@Variant(\\0\\0\\0\\x87@\\x13\\x33\\x33)")
    secs = P.parse_ini(text)
    p = P.PrinterProfile.from_section("23", secs["23"])
    s = S.from_printer_profile(p)
    assert s.exposure == 2.3 and s.layer_height == 0.05 and s.bottom_layers == 5
    assert S.PrintSettings.from_dict(s.to_dict()) == s


SPHERE_GOO = ROOT / "samples" / "10mmsphere.goo"
SPHERE_3MF = ROOT / "samples" / "sphere.3mf"


@pytest.mark.skipif(not (SPHERE_GOO.exists() and SPHERE_3MF.exists()), reason="sample missing")
def test_sphere_sample_is_cut_mid_layer():
    """SatelLite's 10 mm sphere: per-layer lit area is matched best by a mid-layer cut."""
    from gooslicer import mesh as M
    g = goo.read(SPHERE_GOO)
    px = 0.014 * 0.019
    theirs = np.array([sum(n for v, n in rle.decode_runs(l.data) if v) * px for l in g.layers])
    (_, m), = M.load_meshes(SPHERE_3MF)
    m.apply_translation((0, 0, -m.bounds[0, 2]))
    rms = {}
    for f in (0.0, 0.5, 1.0):
        job = SL.SliceJob([m], SPEC16K, S.with_overrides(ABS_LIKE_16K, slice_z_fraction=f), "x")
        assert job.layer_count == len(g.layers) or f != 0.5
        ours = np.array([job._bodies[0].mf.slice(z).area() for z in job.layer_z()])
        n = min(len(ours), len(theirs))
        rms[f] = float(np.sqrt(np.mean((ours[:n] - theirs[:n]) ** 2)))
    assert rms[0.5] < 0.15 and rms[0.5] < rms[0.0] / 3 and rms[0.5] < rms[1.0] / 3
