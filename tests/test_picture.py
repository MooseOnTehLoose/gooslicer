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


@pytest.mark.skipif(not picture.available()[0], reason="MobileSAM or onnxruntime not installed")
def test_mobilesam_outlines_a_square():
    img = np.full((300, 400, 3), 40, np.uint8)
    img[100:200, 150:260] = (230, 140, 50)                  # an orange block on a dark ground
    seg = picture.Segmenter()
    seg.embed(img)
    outs = seg.outlines(200, 150)
    assert len(outs) == 4 and outs[0].shape == (300, 400)
    sq = np.zeros((300, 400), bool)
    sq[100:200, 150:260] = True
    iou = [(m & sq).sum() / (m | sq).sum() for m in outs]
    assert max(iou) > 0.9
