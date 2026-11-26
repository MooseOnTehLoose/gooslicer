"""Select by Parts (11 §2): the minima-rule part split."""
import numpy as np
import trimesh

from gooslicer import modify, parts
from test_modify import _step


def test_post_on_a_slab_is_its_own_part():
    m = _step()
    s = modify.surface(m, np.eye(4))
    lab = parts.parts(s)
    top = (s.normals[:, 2] > 0.99)
    post = s.centers[:, 2] > 4.05                    # the post's sides and top
    slab_top = top & (s.centers[:, 2] < 4.01) & (s.centers[:, 2] > 3.99)
    post_lab = np.unique(lab[post])
    assert len(post_lab) == 1                        # all of the post: one part…
    assert post_lab[0] not in set(lab[slab_top])     # …not the slab's top
    assert len(np.unique(lab[slab_top])) == 1
    # its edges lie round the post's foot, where it meets the slab
    e = parts.edges(s, lab)
    mid = s.vertices[s.edges[e]].mean(axis=1)
    foot = (np.abs(mid[:, 2] - 4) < 1e-6) & (np.max(np.abs(mid[:, :2]), axis=1) <= 2 + 1e-6)
    assert foot.sum() > 0 and foot.all()


def test_ball_is_one_part():
    m = trimesh.creation.icosphere(subdivisions=5, radius=12)
    s = modify.surface(m, np.eye(4))
    assert parts.parts(s).max() == 0


def test_lower_edge_angle_never_gives_fewer_parts():
    s = modify.surface(_step(), np.eye(4))
    P = parts.Parts(s, modify.fold_angles(s))
    n = [P.labels(d).max() + 1 for d in (2, 10, 30)]
    assert n[0] >= n[1] >= n[2] >= 1
