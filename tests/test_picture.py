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


# ------------------------------------------------------------------ more views
def _view(left, right, key):
    """A 10×10 face-index picture: face `left` on the left half, `right` on the right."""
    ids = np.full((10, 10), left, np.int64)
    ids[:, 5:] = right
    return {"rgb": np.full((10, 10, 3), key, np.uint8), "ids": ids}


class _Fake:
    """An image model whose outline for the picture numbered k is masks[k]."""

    def __init__(self, masks):
        self.masks = masks
        self.embeds = 0

    def embed(self, rgb):
        self.embeds += 1
        return int(rgb[0, 0, 0])

    def outlines(self, points, emb=None, size=None):
        assert any(inc for _x, _y, inc in points)
        assert size in (None, (10, 10))
        return [self.masks[emb]]


LEFT = np.zeros((10, 10), bool)
LEFT[:, :5] = True
RIGHT = ~LEFT
ALL = np.ones((10, 10), bool)


def test_refine_adds_what_only_another_view_sees():
    ids0 = _view(0, 1, 0)["ids"]
    areas = np.ones(4)
    c = np.array([True, False, False, False])
    seg = _Fake({1: ALL})
    views = [_view(0, 2, 1)]                  # sees face 0 and face 2, which the clicked view doesn't
    out = picture.refine(seg, views, [c], ids0, areas, faces_in=[0])
    assert out[0].tolist() == [True, False, True, False]
    picture.refine(seg, views, [c], ids0, areas, faces_in=[0])
    assert seg.embeds == 1                    # the second time only decodes


def test_refine_ignores_a_view_that_outlined_something_else():
    ids0 = _view(0, 1, 0)["ids"]
    c = np.array([True, False, False])
    out = picture.refine(_Fake({1: RIGHT}), [_view(0, 1, 1)], [c], ids0, np.ones(3), faces_in=[0])
    assert out[0].tolist() == [True, False, False]


def test_refine_outvotes_the_clicked_view():
    ids0 = _view(0, 1, 0)["ids"]
    c = np.array([True, True])                # the clicked view took face 1 by mistake
    seg = _Fake({1: LEFT, 2: LEFT})
    out = picture.refine(seg, [_view(0, 1, 1), _view(0, 1, 2)], [c], ids0, np.ones(2), faces_in=[0])
    assert out[0].tolist() == [True, False]


def test_refine_skips_a_view_that_barely_sees_the_candidate():
    ids0 = _view(0, 1, 0)["ids"]
    areas = np.array([1.0, 1.0, 1.0, 50.0])
    c = np.array([True, False, False, True])  # face 3 is most of it, and view 1 doesn't see it
    ids = np.full((10, 10), 2)
    ids[0, 0] = 0
    out = picture.refine(_Fake({1: ALL}), [{"rgb": np.full((10, 10, 3), 1, np.uint8), "ids": ids}],
                         [c], np.where(ids0 == 1, 3, ids0), areas, faces_in=[0])
    assert not out[0][2]


def test_inside_points_are_deep_and_apart():
    m = np.zeros((40, 100), bool)
    m[5:35, 5:45] = True
    m[5:35, 55:95] = True
    pts = picture.inside_points(m, 3)
    assert len(pts) == 3
    assert all(m[int(y), int(x)] for x, y in pts)
    d = [np.hypot(a[0] - b[0], a[1] - b[1]) for i, a in enumerate(pts) for b in pts[i + 1:]]
    assert min(d) > 7                          # half the deepest depth (15 px) apart
    x0, y0 = pts[0]
    assert 14 <= y0 <= 25                      # the first is the deepest: mid-height


# ------------------------------------------------------------------ full turn
def _flat(n):
    """A stand-in surface: n unit faces all facing a camera on +Z."""
    from types import SimpleNamespace
    return SimpleNamespace(normals=np.tile((0.0, 0.0, 1.0), (n, 1)), centers=np.zeros((n, 3)),
                           areas=np.ones(n))


def test_propagate_follows_the_outline_from_view_to_view():
    """The clicked view sees faces 0 and 1 (selected: 0); view 1 sees 0 and 2, view 2 sees 2 and
    3: the outline is carried 0 → 2 → 3, and 1 never joins."""
    eye = np.array([0.0, 0.0, 10.0])
    views = [dict(_view(0, 2, 1), eye=eye), dict(_view(2, 3, 2), eye=eye)]
    out = picture.propagate(_Fake({1: ALL, 2: ALL}), views, np.array([True, False, False, False]),
                            _view(0, 1, 0)["ids"], eye, _flat(4), faces_in=[0])
    assert out.tolist() == [True, False, True, True]


def test_propagate_stops_when_asked():
    eye = np.array([0.0, 0.0, 10.0])
    done = []
    with pytest.raises(picture.Cancelled):
        picture.propagate(_Fake({1: ALL, 2: ALL}), [dict(_view(0, 2, 1), eye=eye), dict(_view(2, 3, 2), eye=eye)],
                          np.array([True, False, False, False]), _view(0, 1, 0)["ids"], eye, _flat(4),
                          faces_in=[0], progress=lambda a, b: done.append(a), cancelled=lambda: bool(done))
    assert done == [1]


def test_turn_layout_is_two_rings_nearest_first():
    lay = picture.turn_layout(30.0)
    assert len(lay) == 2 * picture.TURN_STEPS - 1 and lay[0] == (0.0, -30.0)
    assert lay[1:5] == [(30.0, 30.0), (30.0, -30.0), (-30.0, 30.0), (-30.0, -30.0)]
    assert picture.turn_layout(5.0)[1][1] == 20.0 and picture.turn_layout(-80.0)[1][1] == -45.0
