"""Select by Picture (11 §2): outlines to faces, candidates, and the real MobileSAM model when
its files are installed."""
import numpy as np
import pytest

from gooslicer import picture


def test_candidates_sorted_and_whole_dropped():
    part = np.zeros((10, 10), bool)
    part[2:8, 2:8] = True                                   # 36 px of part
    small = np.zeros_like(part)
    small[3:5, 3:5] = True
    mid = np.zeros_like(part)
    mid[2:6, 2:8] = True
    whole = np.ones_like(part)                              # cropped to the part: all of it
    c = picture.candidates([mid, whole, small, small.copy()], part)
    assert [int(m.sum()) for m in c] == [4, 24]             # sorted, duplicate and whole gone
    assert picture.pick(c, 0) is c[0] and picture.pick(c, 1) is c[1] and picture.pick(c, 2) is c[1]
    only = picture.candidates([whole], part)
    assert len(only) == 1                                   # kept when it's the only one
    assert picture.pick([], 1) is None


def test_faces_in_outline_at_twice_the_size():
    ids = np.full((8, 8), -1)
    ids[:, :4] = 0
    ids[:, 4:] = 1
    ids[0, 0] = 2
    outline = np.zeros((4, 4), bool)
    outline[1:, :2] = True                                  # the left half, not the top-left pixel
    got = picture.faces_in_outline(ids, outline, 3)
    assert got.tolist() == [True, False, False]


@pytest.mark.parametrize("key", list(picture.MODELS))
def test_model_outlines_a_block(key):
    """Each image model, where installed: a click on a block outlines it."""
    if not picture.available(key)[0]:
        pytest.skip(picture.available(key)[1])
    img = np.full((300, 400, 3), 40, np.uint8)
    img[100:200, 150:260] = (230, 140, 50)                  # an orange block on a dark ground
    seg = picture.Segmenter(key)
    seg.embed(img)
    outs = seg.outlines([(200, 150, True)])
    assert len(outs) in (3, 4) and outs[0].shape == (300, 400)
    sq = np.zeros((300, 400), bool)
    sq[100:200, 150:260] = True
    iou = [(m & sq).sum() / (m | sq).sum() for m in outs]
    assert max(iou) > 0.9


def test_download_checks_the_checksum(tmp_path, monkeypatch):
    import hashlib
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.onnx").write_bytes(b"encoder")
    (src / "b.onnx").write_bytes(b"decoder")
    sha = lambda b: hashlib.sha256(b).hexdigest()  # noqa: E731
    good = picture.Model("t", "Test", "sam2", "test", (
        picture.ModelFile("a.onnx", 7, sha(b"encoder"), (src / "a.onnx").as_uri()),
        picture.ModelFile("b.onnx", 7, sha(b"decoder"), (src / "b.onnx").as_uri())), "a.onnx", "b.onnx", "")
    monkeypatch.setitem(picture.MODELS, "t", good)
    seen = []
    out = picture.download("t", lambda d, t: seen.append((d, t)), dest=tmp_path / "models")
    assert (out / "a.onnx").read_bytes() == b"encoder" and seen[-1] == (14, 14)
    bad = picture.Model("u", "Bad", "sam2", "bad", (
        picture.ModelFile("a.onnx", 7, "0" * 64, (src / "a.onnx").as_uri()),), "a.onnx", "a.onnx", "")
    monkeypatch.setitem(picture.MODELS, "u", bad)
    with pytest.raises(OSError, match="checksum"):
        picture.download("u", dest=tmp_path / "models")
    assert not list((tmp_path / "models/bad").iterdir())                 # nothing half-there left


def test_drop_specks_keeps_big_pieces_and_clicked_ones():
    import trimesh
    from gooslicer import modify
    m = trimesh.creation.icosphere(subdivisions=4, radius=10)
    s = modify.surface(m, np.eye(4))
    c = s.centers
    big = c[:, 2] > 6                                       # a cap
    speck = np.zeros(len(c), bool)
    speck[np.argmin(c[:, 2])] = True                        # one face at the bottom
    other = np.zeros(len(c), bool)
    other[np.argmin(c[:, 0])] = True                        # one on the side, clicked
    got = picture.drop_specks(s, big | speck | other, keep_faces=[int(np.argmin(c[:, 0]))])
    assert got[big].all() and not got[speck].any() and got[other].all()
