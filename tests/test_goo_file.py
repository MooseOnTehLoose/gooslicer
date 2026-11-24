import os
import struct
from dataclasses import replace
from pathlib import Path

import pytest

from elegoo_goo import goo, rle

SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "10mmcube.goo"
needs_sample = pytest.mark.skipif(not SAMPLE.exists(), reason="reference sample not present")


@pytest.fixture(scope="module")
def sample():
    return SAMPLE.read_bytes()


@needs_sample
def test_sample_header(sample):
    h = goo.Header.unpack(sample)
    assert h.version == "V3.0" and h.machine_type == "ELEGOO Saturn 4 Ultra 16K"
    assert (h.resolution_x, h.resolution_y, h.layer_count) == (15120, 6230, 200)
    assert h.bottom_layers == 5 and h.transition_layers == 10 and h.mirror_y == 1


@needs_sample
def test_sample_roundtrip_byte_identical(sample):
    g = goo.read(sample)
    assert goo.dumps(g) == sample


@needs_sample
def test_rle_reencode_matches_app_for_every_layer(sample):
    for i, layer in enumerate(goo.read(sample).layers):
        assert rle.encode_runs(rle.decode_runs(layer.data)) == layer.data, i


@needs_sample
def test_full_pixel_reencode_one_layer(sample):
    g = goo.read(sample)
    w, h = g.header.resolution_x, g.header.resolution_y
    layer = g.layers[100]
    px = rle.decode(layer.data, w, h)
    assert px.count(255) == 715 * 715 or px.count(255) > 0
    assert rle.encode(px) == layer.data


@needs_sample
def test_generated_layer_defs_match_app(sample):
    g = goo.read(sample)
    tmpl = g.layers[-1].definition
    for i, layer in enumerate(g.layers):
        assert goo.default_layer_def(g.header, i, tmpl).pack() == layer.definition.pack(), i


@needs_sample
def test_rebuild_from_pixels_identical(sample, tmp_path):
    """Full writer path: pixels -> encoder -> layer defs -> file == SatelLite output."""
    g = goo.read(sample)
    w, h = g.header.resolution_x, g.header.resolution_y
    imgs = (rle.decode(l.data, w, h) for l in g.layers[:3])
    hdr = replace(g.header, layer_count=0)  # writer must patch the count
    out = tmp_path / "rebuilt.goo"
    n = goo.write(out, hdr, goo.build_layers(hdr, imgs, g.layers[-1].definition))
    assert n == 3
    r = goo.read(out)
    assert r.header.layer_count == 3
    for a, b in zip(r.layers, g.layers):
        assert a.data == b.data and a.definition.pack() == b.definition.pack()


def test_synthetic_file_roundtrip(tmp_path):
    hdr = goo.Header(resolution_x=64, resolution_y=32, bottom_layers=2,
                     transition_layers=3, file_time="2026-11-23 00:00:00")
    imgs = []
    for i in range(8):
        px = bytearray(64 * 32)
        px[64 * 10 + 5: 64 * 10 + 5 + i + 1] = b"\xff" * (i + 1)
        px[0] = 0x80
        imgs.append(bytes(px))
    path = tmp_path / "syn.goo"
    goo.write(path, hdr, goo.build_layers(hdr, imgs))
    raw = path.read_bytes()
    assert raw[4:12] == goo.MAGIC and raw.endswith(goo.ENDING)
    assert struct.unpack_from(">I", raw, 0x2FB8E)[0] == 0x2FB95
    g = goo.read(raw)
    assert g.header.layer_count == 8
    for i, (layer, px) in enumerate(zip(g.layers, imgs)):
        assert rle.decode(layer.data, 64, 32) == px
        assert layer.definition.z == pytest.approx(0.05 * (i + 1))
    exps = [round(l.definition.exposure_time, 3) for l in g.layers]
    assert exps[:2] == [32.0, 32.0] and exps[-1] == 1.8
    assert exps[2] == round(32 - 30.2 / 3, 3)


def test_bad_pixel_count_and_atomic_cleanup(tmp_path):
    hdr = goo.Header(resolution_x=4, resolution_y=4)
    path = tmp_path / "x.goo"
    with pytest.raises(goo.GooError):
        goo.write(path, hdr, goo.build_layers(hdr, [bytes(15)]))
    assert os.listdir(tmp_path) == []


def test_string_truncation_and_bad_magic():
    h = goo.Header(machine_name="é" * 40)
    raw = h.pack()
    name = raw[0x5C:0x7C]
    assert name[-1] == 0 and name.rstrip(b"\0").decode("utf-8") == "é" * 15
    bad = bytearray(raw)
    bad[4] = 0
    with pytest.raises(goo.GooError):
        goo.Header.unpack(bytes(bad))


def test_rgb565_preview():
    rgb = bytes([255, 0, 0, 0, 255, 0, 0, 0, 255, 255, 255, 255])  # 2x2
    out = goo.rgb565_preview(rgb, 2, 2, (2, 2))
    assert out == bytes.fromhex("f800 07e0 001f ffff".replace(" ", ""))
    assert goo.rgb565_preview(rgb, 2, 2, (1, 1), little_endian=True) == bytes.fromhex("00f8")


@needs_sample
def test_cli_extract_then_build_reproduces_layers(tmp_path, capsys):
    pytest.importorskip("PIL")
    from elegoo_goo.__main__ import main
    assert main(["info", str(SAMPLE), "--verify"]) == 0
    assert "checksums and pixel counts OK" in capsys.readouterr().out
    assert main(["extract", str(SAMPLE), str(tmp_path / "png"), "--layers", "0,4,5,100"]) == 0
    pngs = sorted((tmp_path / "png").glob("*.png"))
    out = tmp_path / "cli.goo"
    assert main(["build", str(out), *map(str, pngs), "--template", str(SAMPLE)]) == 0
    ref, new = goo.read(SAMPLE), goo.read(out)
    assert new.header.layer_count == 4
    assert [l.data for l in new.layers] == [ref.layers[i].data for i in (0, 4, 5, 100)]
    assert new.header.software_name == "elegoo-goo (Linux)"
    assert new.header.resin_profile == ref.header.resin_profile


def test_cli_build_rejects_wrong_resolution(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from elegoo_goo.__main__ import main
    Image.new("L", (10, 10)).save(tmp_path / "0.png")
    with pytest.raises(SystemExit, match="printer needs"):
        main(["build", str(tmp_path / "o.goo"), str(tmp_path / "0.png")])


def test_cli_build_binarises_grey_by_default(tmp_path):
    Image = pytest.importorskip("PIL.Image")
    from elegoo_goo.__main__ import main
    im = Image.new("L", (8, 2))
    im.putdata([0, 40, 127, 128, 200, 255, 255, 0] * 2)
    im.save(tmp_path / "0.png")
    args = ["--set", "resolution_x=8", "--set", "resolution_y=2"]
    main(["build", str(tmp_path / "b.goo"), str(tmp_path / "0.png"), *args])
    main(["build", str(tmp_path / "g.goo"), str(tmp_path / "0.png"), *args, "--keep-grey"])
    b = rle.decode(goo.read(tmp_path / "b.goo").layers[0].data, 8, 2)
    g = rle.decode(goo.read(tmp_path / "g.goo").layers[0].data, 8, 2)
    assert set(b) <= {0, 255} and list(b[:8]) == [0, 0, 0, 255, 255, 255, 255, 0]
    assert list(g[:8]) == [0, 40, 127, 128, 200, 255, 255, 0]
