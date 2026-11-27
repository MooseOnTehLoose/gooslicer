"""Models tab → Modifying (11): select part of a part's surface, cut it off as its own part
along the selection's outline. A mixin of MainWindow: it uses the window's scene, viewport,
undo stack, settings and panels (and its `cutFinished` signal)."""
from __future__ import annotations

import math
import time
import weakref

import numpy as np
import trimesh
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QMessageBox, QProgressDialog

from gooslicer import mesh as meshmod
from gooslicer import modify, picture
from gooslicer import drawsel, partfeat
from gooslicer import parts as partsmod
from gooslicer.camera import OrbitCamera
from gooslicer.scene import Part, prepare

from . import workers
from .commands import AddPartsCommand, MeshCommand
from .memory import trim_memory_soon
from .supporting import SupportsCommand

APP = "GooSlicer"
CUT_EPS = 0.002                 # mm: the cutter's walls sit this far off the outline at gap 0
PREVIEW_DELAY_MS = 250          # the cutter preview follows the settings this long after a change


def _tools(s, faces, mask, depth, smooth, gap):
    """(hole cutter, piece cutter or None, direction): the gap is split evenly, the hole grown
    by half and the piece shrunk by half; never exactly on the outline, where the cutter's walls
    would graze the mesh's own vertices."""
    tool, n = modify.cutter(s, faces, mask, depth, smooth, max(gap / 2, CUT_EPS))
    inner = modify.cutter(s, faces, mask, depth, smooth, -gap / 2)[0] if gap > 0 else None
    return tool, inner, n


def _joint_mesh(world_mesh, s, faces, mask, depth, smooth, gap):
    """Worker: the faces the cut would make inside the part (world), for the 3D view: where
    the piece and the rest will meet. Nothing outside the part is shown."""
    tool, _ = modify.cutter(s, faces, mask, depth, smooth, max(gap / 2, CUT_EPS))
    m = modify.joint(world_mesh, tool)
    if m is None:
        return None, None
    prepare(m)
    # its edges: where the cut meets the part's surfaces (the cut line), as segments
    e = np.sort(np.asarray(m.faces)[:, [0, 1, 1, 2, 2, 0]].reshape(-1, 2), axis=1)
    rim = e[trimesh.grouping.group_rows(e, require_count=1)]
    return m, np.asarray(m.vertices)[rim]


def _cut_job(mesh, matrix, s, faces, mask, keep, depth, smooth, gap):
    """Worker: (rest, piece, dropped, direction, reports) of cutting `mesh` (part coordinates)."""
    tool, inner, n = _tools(s, faces, mask, depth, smooth, gap)
    rest, piece, dropped = modify.cut(mesh, modify.to_local(tool, matrix), keep,
                                      None if inner is None else modify.to_local(inner, matrix))
    return rest, piece, dropped, n, (meshmod.analyze(rest), meshmod.analyze(piece))


class _Busy:
    """A wait cursor and a status message while something slow runs on the GUI thread."""

    def __init__(self, win, text):
        self.win, self.text = win, text

    def __enter__(self):
        self.win.statusBar().showMessage(self.text)
        QApplication.setOverrideCursor(Qt.WaitCursor)
        QApplication.processEvents()

    def __exit__(self, *exc):
        QApplication.restoreOverrideCursor()
        self.win.statusBar().clearMessage()
        return False


class ModifyingMixin:
    """State: `_region` = {"pid", "mesh": id(part.mesh), "seeds": [seed, …], "grows", "mask",
    "surface"} or None, where a seed is {"kind": "surface" | "part" | "picture", "face"} (a
    picture seed also keeps its candidate face masks, "cands"); `_adjacency` = (id(mesh), face
    adjacency) for the last mesh asked."""

    _region = None
    _adjacency = None
    _region_key = None
    _preview_gen = 0
    _cutting = False

    # ------------------------------------------------------------ mode
    def set_region_mode(self, on: bool) -> None:
        vp = self.viewport
        if on:
            if not self.scene.parts:
                self.props.select_btn.setChecked(False)
                return
            vp.stop_hole_mode()
            if vp.support_mode:
                vp.stop_support_mode()
            vp.lay_flat_mode = False
            vp.start_region_mode()
            self._show_contours()
            self.statusBar().showMessage(
                "Select by Picture: click a thing on a part; Ctrl+click adds to its outline, right-click "
                "leaves something out (Esc to finish)" if self.select_by() == "picture" else
                "Select surface: click a part's surface; Ctrl+click adds an area (Esc to finish)")
        else:
            vp.stop_region_mode()

    def _region_mode_ended(self) -> None:
        self._show_contours()
        self.props.select_btn.blockSignals(True)
        self.props.select_btn.setChecked(False)
        self.props.select_btn.blockSignals(False)
        self.statusBar().clearMessage()

    def cut_settings(self) -> dict:
        pp = self.props
        return {"depth": pp.cut_depth.value(), "smooth": pp.cut_smooth.value(), "gap": pp.cut_gap.value()}

    # ------------------------------------------------------------ picking
    def select_by(self) -> str:
        return self.props.select_by.currentData() or "surface"

    def select_by_changed(self) -> None:
        self.settings.setValue("modify/selectBy", self.select_by())
        self.viewport.region_exclude = self.select_by() == "picture"
        self.viewport.region_draw = self.select_by() == "draw"
        self.viewport.brush_ring = None
        self._show_contours()

    def brush_changed(self) -> None:
        mm = self.props.brush.value() / 2
        self.settings.setValue("modify/brush", float(mm))
        self.viewport.brush_mm = mm
        self.viewport.update()

    # ------------------------------------------------------------ Select by Parts (AI) (11)
    def _partsai_click(self, p: Part, face: int, add: bool) -> None:
        """A click in Select by Parts (AI): the clearest part round it, from PartField's features
        (worked out first, once per model, in the background behind a window)."""
        feat = self._partsai_cache.get(id(p.mesh)) if hasattr(self, "_partsai_cache") else None
        if feat is not None:
            self._partsai_apply(p, face, add, feat)
            return
        if getattr(self, "_partsai_busy", False):
            return
        self._partsai_busy = True                  # before the window shows (it lets events through)
        dlg = QProgressDialog(self)
        dlg.setWindowTitle("Select by Parts (AI)")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setCancelButtonText("Stop")
        dlg.setRange(0, 0)
        dlg.setLabelText(f"Finding the parts of {p.name} in 3D…\nPlease wait: this is done once per "
                         "model and takes about 10 to 30 seconds.")
        dlg.show()
        QApplication.processEvents()
        state = {"stopped": False}
        dlg.canceled.connect(lambda: state.update(stopped=True))
        pid, mesh = p.id, p.mesh
        verts, faces = np.asarray(mesh.vertices), np.asarray(mesh.faces)
        t0 = time.monotonic()

        def job():
            net = getattr(self, "_partsai_net", None) or partfeat.FieldNet()
            return net, partfeat.face_features(net, verts, faces)

        def finish():
            self._partsai_busy = False
            dlg.close()
            dlg.deleteLater()

        def done(res):
            stopped = state["stopped"]              # before closing: closing the window cancels it
            finish()
            self._partsai_net, feat = res
            self._partsai_cache = {id(mesh): feat}             # one model's features at a time
            if stopped:
                self.statusBar().showMessage("Stopped.", 4000)
                return
            self.log_msg(f"Select by Parts (AI): found the parts of {p.name} in "
                         f"{time.monotonic() - t0:.0f} s.")
            q = self.scene.get(pid)
            if q is not None and q.mesh is mesh:
                self._partsai_apply(q, face, add, feat)

        def failed(msg):
            finish()
            self.log_msg("Select by Parts (AI) failed: " + msg.splitlines()[0])
            self.statusBar().showMessage("Select by Parts (AI) failed (see the log).", 8000)
        workers.submit(job, on_done=done, on_failed=failed)

    def _partsai_apply(self, p: Part, face: int, add: bool, feat) -> None:
        s = self._surface(p)
        b = partfeat.reach(s.adjacency, feat, face)
        found, k = partfeat.parts(s.areas, b)
        seed = {"kind": "partsai", "face": face, "parts": found, "pick": k}
        r = self._region
        if add and r is not None and r["pid"] == p.id and r["mesh"] == id(p.mesh):
            r["seeds"].append(seed)
        else:
            self._region = {"pid": p.id, "mesh": id(p.mesh), "seeds": [seed], "grows": [], "mask": None}
        self._show_part_size(seed)
        self._update_region()

    def _show_part_size(self, seed) -> None:
        pp = self.props
        pp.part_size.blockSignals(True)
        pp.part_size.setRange(0, max(len(seed["parts"]) - 1, 0))
        pp.part_size.setValue(seed["pick"])
        pp.part_size.blockSignals(False)
        pp.part_size_label.setText(f"{seed['pick'] + 1} of {len(seed['parts'])}")

    def part_size_changed(self) -> None:
        """The Part size slider: a smaller or larger part round the last Parts (AI) click."""
        r = self._region
        seeds = [q for q in (r["seeds"] if r else []) if q["kind"] == "partsai"]
        if not seeds:
            return
        seeds[-1]["pick"] = int(self.props.part_size.value())
        self._show_part_size(seeds[-1])
        self._update_region()

    # ------------------------------------------------------------ Select by Drawing (11)
    def region_stroke(self, pid: int, faces, path) -> None:
        """A stroke drawn on a part: add it to the drawing (a new drawing on another part, or
        after another kind of selection) and select what the drawing walls off."""
        p = self.scene.get(pid)
        if p is None or self.select_by() != "draw":
            return
        if pid not in self.selected_ids():
            self.select([pid])
        r = self._region
        last = r["seeds"][-1] if r and r["pid"] == pid and r["mesh"] == id(p.mesh) and r["seeds"] else None
        stroke = {"faces": np.asarray(faces, np.int64), "radius": self.viewport.brush_mm,
                  "path": np.asarray(path, float)}
        if last is not None and last["kind"] == "draw":
            last["strokes"].append(stroke)
            seed = last
        else:
            seed = {"kind": "draw", "face": int(stroke["faces"][0]), "strokes": [stroke], "mask": None}
            self._region = {"pid": pid, "mesh": id(p.mesh), "seeds": [seed], "grows": [], "mask": None}
        self._draw_lines(seed)
        self._draw_select(p, seed)

    def _draw_lines(self, seed) -> None:
        segs = [np.stack([st["path"][:-1], st["path"][1:]], axis=1) for st in seed["strokes"] if len(st["path"]) > 1]
        self.viewport.draw_lines = np.concatenate(segs) if segs else None
        self.viewport.update()

    def _draw_select(self, p: Part, seed: dict) -> None:
        """Work out the drawing's selection: the paint, the inside, then the cut, with the AI's
        outline when "Use the AI for the edge" is on (its picture looked at in the background)."""
        s = self._surface(p)
        with _Busy(self, "Finding the part you drew round…"):
            for st in seed["strokes"]:
                if "painted" not in st:
                    st["painted"] = drawsel.paint(s, st["faces"], st["radius"])
            painted = np.logical_or.reduce([st["painted"] for st in seed["strokes"]])
            radius = max(st["radius"] for st in seed["strokes"])
            got = drawsel.inside(s, painted, radius)
        if got is None:
            seed["mask"] = np.zeros(len(s.areas), bool)
            seed.pop("parts", None)
            self.statusBar().showMessage("Draw all the way round the part: the drawing doesn't wall "
                                         "anything off yet.", 8000)
            self._update_region()
            return
        seed["parts"] = got
        use_ai = self.props.draw_ai.isChecked() and self.props.draw_ai.isEnabled()
        if not use_ai or not picture.available(self.picture_model_settings()[0])[0]:
            self._draw_cut(p, seed, None)
            return
        model = self.picture_model_settings()
        key = self._picture_view_key(p)
        if getattr(self, "_picture_key", None) == key and getattr(self, "_segmenter_model", None) == model:
            self._draw_cut(p, seed, self._draw_ai(p, seed))
            return
        if getattr(self, "_picture_busy", False):
            return
        cap = self.viewport.capture(p)
        seg = getattr(self, "_segmenter", None) if getattr(self, "_segmenter_model", None) == model else None
        self._picture_busy = True
        QApplication.setOverrideCursor(Qt.BusyCursor)
        self.statusBar().showMessage("Looking at the picture…")
        pid, mesh_id = p.id, id(p.mesh)

        def job():
            sg = seg or picture.Segmenter(*model)
            sg.embed(cap["rgb"])
            return sg

        def finish():
            self._picture_busy = False
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()

        def done(sg):
            finish()
            self._segmenter, self._segmenter_model = sg, model
            self._picture, self._picture_key = cap, key
            q = self.scene.get(pid)
            r = self._region
            if q is None or id(q.mesh) != mesh_id or r is None or not any(x is seed for x in r["seeds"]):
                return
            self._draw_cut(q, seed, self._draw_ai(q, seed) if self._picture_view_key(q) == key else None)

        def failed(msg):
            finish()
            self._picture_key = None
            self.log_msg("Select by Drawing: the AI didn't run: " + msg.splitlines()[0])
            self._draw_cut(p, seed, None)
        workers.submit(job, on_done=done, on_failed=failed)

    def _draw_ai(self, p: Part, seed: dict):
        """(the image model's outline of the drawn region, the faces its picture sees), or None."""
        inn, out, painted = seed["parts"]
        cap = self._picture
        ids = cap["ids"]
        n = len(inn)
        ok = (ids >= 0) & (ids < n)
        dom_px = np.zeros(ids.shape, bool)
        dom_px[ok] = (inn | painted)[ids[ok]]
        if dom_px.sum() < 50:
            return None
        ys, xs = np.nonzero(dom_px)
        box = (float(xs.min()), float(ys.min()), float(xs.max()), float(ys.max()))
        s = self._surface(p)
        core = drawsel.core(s, inn, painted)
        core_px = np.zeros(ids.shape, bool)
        core_px[ok] = core[ids[ok]]
        if not core_px.any():
            core_px[ok] = inn[ids[ok]]
        out_px = np.zeros(ids.shape, bool)
        out_px[ok] = out[ids[ok]]
        out_px[:int(box[1]), :] = out_px[int(box[3]) + 1:, :] = False
        out_px[:, :int(box[0])] = out_px[:, int(box[2]) + 1:] = False
        k = 4                                            # points found on a coarser grid
        pts = [(k * x + k / 2, k * y + k / 2, True) for x, y in picture.inside_points(core_px[::k, ::k], 3)]
        pts += [(k * x + k / 2, k * y + k / 2, False) for x, y in picture.inside_points(out_px[::k, ::k], 3)]
        if not any(q[2] for q in pts):
            return None
        return drawsel.pick_outline(s, inn, painted, core,
                                    [picture.faces_in_outline(ids, o, n) for o in
                                     picture.candidates(self._segmenter.outlines(pts, box=box), ok)],
                                    picture.seen_faces(ids, n))

    def _draw_cut(self, p: Part, seed: dict, ai) -> None:
        s = self._surface(p)
        inn, out, painted = seed["parts"]
        with _Busy(self, "Finding the part's edge…"):
            seed["mask"] = drawsel.cut(s, inn, out, painted, self._fold(p, s),
                                       None if ai is None else ai[0], None if ai is None else ai[1])
        seed["ai"] = ai is not None
        self._update_region()

    def region_pick(self, pid: int, face: int, add: bool) -> None:
        p = self.scene.get(pid)
        if p is None:
            return
        if pid not in self.selected_ids():
            self.select([pid])
        kind = {"surface": "surface", "parts": "part", "picture": "picture", "partsai": "partsai",
                "draw": None}[self.select_by()]
        if kind is None:
            return
        if kind == "picture":
            self._picture_click(p, int(face), "add" if add else "new")
            return
        if kind == "partsai":
            self._partsai_click(p, int(face), add)
            return
        seed = {"kind": kind, "face": int(face)}
        r = self._region
        if add and r is not None and r["pid"] == pid and r["mesh"] == id(p.mesh):
            if not any(q["kind"] == kind and q["face"] == seed["face"] for q in r["seeds"]):
                r["seeds"].append(seed)
        else:
            self._region = {"pid": pid, "mesh": id(p.mesh), "seeds": [seed], "grows": [], "mask": None}
        self._update_region()

    # ------------------------------------------------------------ parts and picture
    def _part_labels(self, p: Part, s: modify.Surface) -> np.ndarray:
        """Part label per face at the current Part edges angle (11, Select by Parts); the slow
        stages are cached per placement."""
        place = (id(p.mesh), p.matrix().tobytes())
        c = getattr(self, "_parts_cache", None)
        if c is None or c[0] != place:
            with _Busy(self, f"Finding the parts of {p.name}…"):
                c = self._parts_cache = (place, partsmod.Parts(s, self._fold(p, s)), {})
        deg = int(self.props.part_edges.value())
        if deg not in c[2]:
            c[2][deg] = c[1].labels(deg)
        return c[2][deg]

    def _snapped(self, p: Part, s: modify.Surface, seed: dict, key, raw) -> np.ndarray:
        """A part (Parts, Parts (AI)) with its edge moved onto the steps near it
        (drawsel.snap); cached on the seed per `key`, so moving a slider back is instant."""
        c = seed.setdefault("snapped", {})
        place = (id(p.mesh), p.matrix().tobytes())
        key = (place, key)
        if key not in c:
            f = getattr(self, "_snap_fold_cache", None)
            with _Busy(self, "Fitting the part's edge…"):
                if f is None or f[0] != place:
                    f = self._snap_fold_cache = (place, drawsel.snap_fold(s))
                c[key] = drawsel.snap(s, raw(), f[1], np.asarray(p.mesh.faces))
        return c[key]

    def region_exclude(self, pid: int, face: int) -> None:
        """Right-click in Select by Picture: leave that out of the last outline."""
        p = self.scene.get(pid)
        if p is not None and self.select_by() == "picture":
            self._picture_click(p, int(face), "exclude")

    def _picture_view_key(self, p: Part):
        vp = self.viewport
        return (p.id, id(p.mesh), p.matrix().tobytes(), vp.camera.view().tobytes(), vp.width(), vp.height(),
                vp.clip_z)

    def picture_model_settings(self) -> tuple[str, str]:
        """(model key, "cpu" | "gpu") from Preferences."""
        key = str(self.settings.value("picture/model", picture.DEFAULT_MODEL))
        if key not in picture.MODELS or picture.model_dir(key) is None:
            key = picture.DEFAULT_MODEL
        dev = str(self.settings.value("picture/device", "cpu"))
        return key, ("gpu" if dev == "gpu" else "cpu")

    def _picture_click(self, p: Part, face: int, how: str) -> None:
        """Select by Picture (11). how: "new" (a plain click: a new outline), "add" (Ctrl+click:
        another point on the last outline when the view hasn't changed, else another outline)
        or "exclude" (right-click: leave that out of the last outline). The picture is looked
        at once per view, in the background (the first time on a GPU compiles the model)."""
        model = self.picture_model_settings()
        ok, why = picture.available(model[0])
        if not ok:
            QMessageBox.warning(self, APP, "Select by Picture isn't available: " + why + ".")
            return
        if getattr(self, "_picture_busy", False):
            self.statusBar().showMessage("Still looking at the picture…", 3000)
            return
        key = self._picture_view_key(p)
        if getattr(self, "_picture_key", None) == key and getattr(self, "_segmenter_model", None) == model:
            self._picture_apply(p, face, how)
            return
        if how == "exclude":
            self.statusBar().showMessage("Right-click leaves something out of an outline: click the "
                                         "thing first", 5000)
            return
        cap = self.viewport.capture(p)
        seg = getattr(self, "_segmenter", None) if getattr(self, "_segmenter_model", None) == model else None
        self._picture_busy = True
        gpu_first = model[1] == "gpu" and seg is None
        self.statusBar().showMessage("Looking at the picture…" + (
            " (the first time on the GPU it compiles the model, which can take a few minutes)"
            if gpu_first else ""))
        QApplication.setOverrideCursor(Qt.BusyCursor)
        pid, mesh_id = p.id, id(p.mesh)

        def job():
            s = seg or picture.Segmenter(*model)
            s.embed(cap["rgb"])
            return s

        def finish():
            self._picture_busy = False
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()

        def done(s):
            finish()
            if s.gpu_error and model[1] == "gpu" and not getattr(self, "_gpu_warned", False):
                self._gpu_warned = True
                self.log_msg(f"Select by Picture runs on the CPU ({s.gpu_error}).")
            self._segmenter, self._segmenter_model = s, model
            self._picture, self._picture_key = cap, key
            q = self.scene.get(pid)
            if q is not None and id(q.mesh) == mesh_id and self._picture_view_key(q) == key:
                self._picture_apply(q, face, how)

        def failed(msg):
            finish()
            self._picture_key = None
            QMessageBox.warning(self, APP, "Select by Picture failed: " + msg.splitlines()[0])
        workers.submit(job, on_done=done, on_failed=failed)

    def _picture_apply(self, p: Part, face: int, how: str) -> None:
        """Add the click to the selection now that the picture has been looked at."""
        cap = self._picture
        c = p.world_mesh().triangles_center[face]
        xy, _ = self.viewport._screen(np.asarray([c]), cap["vp"])
        x, y = xy[0] * cap["dpr"] * cap["scale"]
        r = self._region
        same = r is not None and r["pid"] == p.id and r["mesh"] == id(p.mesh)
        last = r["seeds"][-1] if same and r["seeds"] else None
        refine = last is not None and last["kind"] == "picture" and last.get("view") == self._picture_key
        if how == "exclude" and not refine:
            self.statusBar().showMessage("Right-click leaves something out of an outline: click the "
                                         "thing first", 5000)
            return
        if how in ("add", "exclude") and refine:
            last["points"].append((float(x), float(y), how == "add"))
            (last["faces_in"] if how == "add" else last["faces_out"]).append(face)
            seed = last
        else:
            seed = {"kind": "picture", "face": face, "view": self._picture_key,
                    "points": [(float(x), float(y), True)], "faces_in": [face], "faces_out": []}
            if how == "add" and same:
                r["seeds"].append(seed)
            else:
                self._region = {"pid": p.id, "mesh": id(p.mesh), "seeds": [seed], "grows": [], "mask": None}
        try:
            seed["cands"] = seed["prim"] = self._picture_candidates(p, seed)
            seed["refined"] = {}
        except Exception as e:
            QMessageBox.warning(self, APP, f"Select by Picture failed: {e}")
            return
        self._update_region()
        self._picture_more(p, seed)

    def picture_turn(self) -> bool:
        """Preferences → Picture selection → All the way round (default on)."""
        return str(self.settings.value("picture/turn", "true")).lower() in ("true", "1")

    def _picture_more(self, p: Part, seed: dict) -> None:
        """After the clicked view's outline: the full turn, or More views."""
        if self.picture_turn():
            self._picture_turn(p, seed)
        else:
            self._picture_refine(p, seed)

    def _picture_turn(self, p: Part, seed: dict) -> None:
        """Full turn (11): carry the outline the Outline slider picks round the model, in two
        rings of pictures, in the background behind a modal progress dialog (it can take
        minutes). Stop keeps the clicked view's outline."""
        prim = seed.get("prim")
        if not prim or getattr(self, "_picture_busy", False):
            return
        i = (0, len(prim) // 2, len(prim) - 1)[int(self.props.outline_size.value())]
        if i in seed["refined"]:
            return
        r = self._region
        surf = self._surface(p)
        cam = self.viewport.camera
        self._picture_busy = True                  # before the window shows (it lets events through)
        QApplication.setOverrideCursor(Qt.BusyCursor)
        dlg = QProgressDialog(self)
        dlg.setWindowTitle("Select by Picture")
        dlg.setWindowModality(Qt.ApplicationModal)
        dlg.setMinimumDuration(0)
        dlg.setAutoClose(False)
        dlg.setAutoReset(False)
        dlg.setCancelButtonText("Stop")
        dlg.setLabelText(f"Drawing {p.name} from all round…")
        dlg.setRange(0, 0)
        dlg.show()
        QApplication.processEvents()
        if "turn" not in seed:
            for q in r["seeds"]:                       # only the latest outline keeps its pictures
                if q is not seed:
                    q.pop("turn", None)
                    q.pop("views", None)
            b = p.bounds()
            centre = (b[0] + b[1]) / 2
            rad = max(float(np.linalg.norm(b[1] - b[0])) / 2, 1.0)
            dist = rad / (picture.TURN_FILL * math.sin(math.radians(cam.fov) / 2))
            v = cam.eye() - centre
            yaw0 = math.degrees(math.atan2(v[1], v[0]))
            pitch0 = math.degrees(math.asin(max(-1.0, min(1.0, v[2] / max(float(np.linalg.norm(v)), 1e-9)))))
            cams = [OrbitCamera(target=centre.copy(), distance=dist, yaw=(yaw0 + o) % 360.0, pitch=pt, fov=cam.fov)
                    for o, pt in picture.turn_layout(pitch0)]
            try:
                caps = self.viewport.capture_views(p, cams)
            except Exception as e:
                self._picture_busy = False
                QApplication.restoreOverrideCursor()
                dlg.close()
                dlg.deleteLater()
                self.log_msg(f"Select by Picture: drawing the model from all round failed: {e}")
                return
            seed["turn"] = [{"rgb": c["rgb"], "ids": c["ids"].astype(np.int32), "eye": cm.eye()}
                            for c, cm in zip(caps, cams)]
            seed["eye0"] = cam.eye().copy()
        seed["gen"] = gen = seed.get("gen", 0) + 1
        views, seg, ids0 = seed["turn"], self._segmenter, self._picture["ids"]
        fin, fout, eye0 = list(seed["faces_in"]), list(seed["faces_out"]), seed["eye0"]
        first = any("emb" not in v for v in views)
        dlg.setRange(0, len(views))
        dlg.setValue(0)
        what = (f"Looking at {p.name} from all round, in {len(views)} pictures" if first else
                f"Following the outline round {p.name}")
        dlg.setLabelText(what + "…\nPlease wait; Stop keeps the outline from this view.")
        stop = [False]
        dlg.canceled.connect(lambda: stop.__setitem__(0, True))
        t0 = time.monotonic()

        def on_progress(text):
            j, total = (int(x) for x in text.split("/"))
            dlg.setValue(j)
            left = ""
            if j >= 2 and j < total:
                sec = (time.monotonic() - t0) / j * (total - j)
                left = f", about {max(1, round(sec / 60))} min left" if sec >= 90 else f", about {max(1, round(sec))} s left"
            dlg.setLabelText(f"{what}: {j} of {total}{left}.\nPlease wait; Stop keeps the outline from this view.")

        def job(progress):
            return picture.propagate(seg, views, prim[i], ids0, eye0, surf, fin, fout,
                                     progress=lambda a, b: progress(f"{a}/{b}"), cancelled=lambda: stop[0])

        def finish():
            self._picture_busy = False
            QApplication.restoreOverrideCursor()
            dlg.close()
            dlg.deleteLater()

        def done(res):
            finish()
            rr = self._region
            if seed.get("gen") != gen or rr is None or not any(q is seed for q in rr["seeds"]):
                return
            m = picture.drop_specks(surf, res, fin)
            m[fin] = True
            m[fout] = False
            seed["refined"][i] = m
            seed["cands"] = [seed["refined"].get(j, c) for j, c in enumerate(prim)]
            self._update_region()
            self.statusBar().showMessage(f"Took {time.monotonic() - t0:.0f} s to look all the way round.", 8000)
            self._picture_refine_last()       # the slider may have moved meanwhile

        def failed(msg):
            finish()
            if "Cancelled" in msg:
                self.statusBar().showMessage("Stopped: the outline is from this view only.", 8000)
                return
            seed.pop("turn", None)
            self.log_msg("Select by Picture: looking all the way round failed: " + msg.splitlines()[0])
        workers.submit(job, on_done=done, on_failed=failed, on_progress=on_progress)

    def picture_views(self) -> int:
        """Preferences → Picture selection → Views (the clicked one included)."""
        try:
            n = int(self.settings.value("picture/views", picture.VIEWS))
        except (TypeError, ValueError):
            n = picture.VIEWS
        return max(1, min(picture.MAX_VIEWS, n))

    def _picture_cameras(self, p: Part, mask: np.ndarray, k: int) -> list:
        """k cameras round the middle of a candidate's faces (11, More views)."""
        surf = self._surface(p)
        if k <= 0 or not mask.any():
            return []
        c, a = surf.centers[mask], surf.areas[mask]
        centre = (c * a[:, None]).sum(axis=0) / max(a.sum(), 1e-12)
        r = max(float(np.linalg.norm(c - centre, axis=1).max()), 1.0)
        cam = self.viewport.camera
        v = cam.eye() - centre
        d0 = float(np.linalg.norm(v))
        d = min(r / (picture.FILL * math.sin(math.radians(cam.fov) / 2)), d0)
        yaw = math.degrees(math.atan2(v[1], v[0]))
        pitch = math.degrees(math.asin(max(-1.0, min(1.0, v[2] / max(d0, 1e-9)))))
        return [OrbitCamera(target=centre.copy(), distance=d, yaw=(yaw + dy) % 360.0,
                            pitch=float(np.clip(pitch + dp, -85.0, 85.0)), fov=cam.fov)
                for dy, dp in picture.VIEW_TURNS[:k]]

    def _picture_refine(self, p: Part, seed: dict) -> None:
        """More views (11): vote on the outline the Outline slider picks from other angles, in
        the background; the clicked view's outline shows meanwhile. The others are refined when
        the slider moves to them (decoding only, by then)."""
        n = self.picture_views()
        if n <= 1 or not seed.get("prim") or getattr(self, "_picture_busy", False):
            return
        prim = seed["prim"]
        i = (0, len(prim) // 2, len(prim) - 1)[int(self.props.outline_size.value())]
        if i in seed["refined"]:
            return
        r = self._region
        if "views" not in seed:
            for q in r["seeds"]:                       # only the latest outline keeps its views
                if q is not seed:
                    q.pop("views", None)
            cams = self._picture_cameras(p, seed["prim"][-1], n - 1)
            caps = self.viewport.capture_views(p, cams)
            seed["views"] = [{"rgb": c["rgb"], "ids": c["ids"].astype(np.int32)} for c in caps]
        seed["gen"] = gen = seed.get("gen", 0) + 1
        seg, ids0, surf = self._segmenter, self._picture["ids"], self._surface(p)
        views = seed["views"]
        fin, fout = list(seed["faces_in"]), list(seed["faces_out"])
        first = any("emb" not in v for v in views)
        self._picture_busy = True
        QApplication.setOverrideCursor(Qt.BusyCursor)
        if first:
            self.statusBar().showMessage(f"Looking from {len(views)} more views…")

        def job():
            out = picture.refine(seg, views, [prim[i]], ids0, surf.areas, fin, fout)[0]
            for v in views:
                v.pop("rgb", None)                     # embedded: the picture isn't needed again
            return out

        def finish():
            self._picture_busy = False
            QApplication.restoreOverrideCursor()
            self.statusBar().clearMessage()

        def done(res):
            finish()
            rr = self._region
            if seed.get("gen") != gen or rr is None or not any(q is seed for q in rr["seeds"]):
                return
            m = picture.drop_specks(surf, res, fin)
            m[fin] = True
            m[fout] = False
            seed["refined"][i] = m
            seed["cands"] = [seed["refined"].get(j, c) for j, c in enumerate(prim)]
            self._update_region()
            self._picture_refine_last()       # the slider may have moved meanwhile

        def failed(msg):
            finish()
            seed.pop("views", None)
            self.log_msg("Select by Picture: looking from more views failed: " + msg.splitlines()[0])
        workers.submit(job, on_done=done, on_failed=failed)

    def _picture_candidates(self, p: Part, seed: dict) -> list:
        """The image model's outlines for a picture seed's points, as face masks, smallest
        first."""
        cap = self._picture
        outs = self._segmenter.outlines(seed["points"])
        ids = cap["ids"]
        n = len(p.mesh.faces)
        surf = self._surface(p)
        cands = []
        for m in picture.candidates(outs, ids >= 0):
            m = picture.drop_specks(surf, picture.faces_in_outline(ids, m, n), seed["faces_in"])
            m[seed["faces_in"]] = True
            m[seed["faces_out"]] = False
            cands.append(m)
        return cands or [np.isin(np.arange(n), seed["faces_in"])]

    def clear_region(self) -> None:
        self._region = None
        self.viewport.draw_lines = None
        self._region_key = None
        self._preview_gen += 1
        self.viewport.region_mesh = None
        self.viewport.cut_preview = self.viewport.cut_lines = None
        self._show_contours()
        self.update_region_panel()
        self.viewport.update()

    def grow_selection(self) -> None:
        """Grow (or shrink) the selection by the Grow selection distance along the surface; kept
        through later spread or contour changes until the next plain click or Clear."""
        r = self._region
        mm = float(self.props.grow_mm.value())
        if r is None or r.get("mask") is None or not mm:
            return
        r.setdefault("grows", []).append(mm)
        self._update_region()

    def _fold(self, p: Part, s: modify.Surface) -> np.ndarray:
        """modify.fold_angles of a part, cached per placement (the slow part of contours and
        parts)."""
        place = (id(p.mesh), p.matrix().tobytes())
        f = getattr(self, "_fold_cache", None)
        if f is None or f[0] != place:
            f = self._fold_cache = (place, modify.fold_angles(s))
        return f[1]

    def _contours(self, p: Part, s: modify.Surface):
        """Contour edges (per adjacency row) of a part at the current angle; cached."""
        fold = self._fold(p, s)
        key = (id(p.mesh), p.matrix().tobytes(), int(self.props.contour_angle.value()))
        c = getattr(self, "_contour_cache", None)
        if c is None or c[0] != key:
            c = self._contour_cache = (key, modify.contours(s, float(key[2]), fold=fold))
        return c[1]

    def _show_contours(self) -> None:
        """Draw the lines of the Select by mode on the part being selected on (or the selected
        one while picking): its contours (Surface, with Stop at contours on) or its part edges
        (Parts)."""
        vp = self.viewport
        r = self._region
        p = self.scene.get(r["pid"]) if r else (self.primary() if vp.region_mode else None)
        mode = self.select_by()
        if p is None or not (vp.region_mode or r) or mode == "picture" \
                or (mode == "surface" and not self.props.stop_contours.isChecked()):
            vp.contour_lines = None
        else:
            s = r["surface"] if r and r.get("surface") is not None else self._surface(p)
            e = self._contours(p, s) if mode == "surface" else partsmod.edges(s, self._part_labels(p, s))
            vp.contour_lines = s.vertices[s.edges[e]]
        vp.update()

    def region_settings_changed(self) -> None:
        st = self.settings
        st.setValue("modify/spread", int(self.props.spread.value()))
        st.setValue("modify/contours", bool(self.props.stop_contours.isChecked()))
        st.setValue("modify/contourFold", int(self.props.contour_angle.value()))
        st.setValue("modify/partEdges", int(self.props.part_edges.value()))
        st.setValue("modify/outline", int(self.props.outline_size.value()))
        st.setValue("modify/drawAI", bool(self.props.draw_ai.isChecked()))
        for k, v in self.cut_settings().items():
            st.setValue(f"modify/{k}", float(v))
        if self._region is not None:
            self._update_region()
            self._picture_refine_last()
        else:
            self._show_contours()

    def _picture_refine_last(self) -> None:
        """Refine the latest picture outline at the Outline slider's position, if it isn't."""
        r = self._region
        seed = r["seeds"][-1] if r and r["seeds"] else None
        if seed is not None and seed.get("kind") == "picture" and seed.get("prim") \
                and getattr(self, "_picture", None) is not None and seed.get("view") == self._picture_key:
            p = self.scene.get(r["pid"])
            if p is not None:
                self._picture_more(p, seed)

    def load_modify_settings(self) -> None:
        pp, st = self.props, self.settings
        for w, key, default in ((pp.cut_depth, "depth", 3.0), (pp.cut_smooth, "smooth", 0.5),
                                (pp.cut_gap, "gap", 0.1)):
            w.blockSignals(True)
            w.setValue(float(st.value(f"modify/{key}", default)))
            w.blockSignals(False)
        pp.spread.blockSignals(True)
        pp.spread.setValue(int(st.value("modify/spread", 35)))
        pp.spread_label.setText(f"{pp.spread.value()}°")
        pp.spread.blockSignals(False)
        pp.lay_cut.setChecked(str(st.value("modify/layFlat", True)).lower() not in ("false", "0"))
        pp.stop_contours.blockSignals(True)
        pp.stop_contours.setChecked(str(st.value("modify/contours", True)).lower() not in ("false", "0"))
        pp.stop_contours.blockSignals(False)
        pp.contour_angle.blockSignals(True)
        pp.contour_angle.setValue(int(st.value("modify/contourFold", 35)))
        pp.contour_label.setText(f"{pp.contour_angle.value()}°")
        pp.contour_angle.blockSignals(False)
        pp.grow_mm.blockSignals(True)
        pp.grow_mm.setValue(float(st.value("modify/grow", 1.0)))
        pp.grow_mm.blockSignals(False)
        for w, lab, key, default, text in (
                (pp.part_edges, pp.part_edges_label, "partEdges", 10, lambda v: f"{v}°"),
                (pp.outline_size, pp.outline_label, "outline", 1, lambda v: picture.SIZES[v])):
            w.blockSignals(True)
            w.setValue(int(st.value(f"modify/{key}", default)))
            lab.setText(text(w.value()))
            w.blockSignals(False)
        i = pp.select_by.findData(str(st.value("modify/selectBy", "surface")))
        if i < 0 or not pp.select_by.model().item(i).isEnabled():
            i = 0
        pp.select_by.blockSignals(True)
        pp.select_by.setCurrentIndex(i)
        pp.select_by.blockSignals(False)
        pp.brush.blockSignals(True)
        pp.brush.setValue(int(round(float(st.value("modify/brush", 2.0)) * 2)))
        pp.brush_label.setText(f"{pp.brush.value() / 2:g} mm")
        pp.brush.blockSignals(False)
        self.viewport.brush_mm = pp.brush.value() / 2
        pp.draw_ai.blockSignals(True)
        pp.draw_ai.setChecked(pp.draw_ai.isEnabled() and str(st.value("modify/drawAI", False)).lower() not in ("false", "0"))
        pp.draw_ai.blockSignals(False)
        pp.show_select_by()
        self.viewport.region_exclude = self.select_by() == "picture"
        self.viewport.region_draw = self.select_by() == "draw"

    def _surface(self, p: Part) -> modify.Surface:
        """The part's surface in world space; cached per placement."""
        if self._adjacency is None or self._adjacency[0] != id(p.mesh):
            self._adjacency = (id(p.mesh), modify.adjacency(p.mesh))
        M = p.matrix().tobytes()
        c = getattr(self, "_surface_cache", None)            # (weakref to the mesh, matrix, surface)
        if c is None or c[0]() is not p.mesh or c[1] != M:
            c = self._surface_cache = (weakref.ref(p.mesh), M, modify.surface(p.mesh, p.matrix(),
                                                                              self._adjacency[1]))
        return c[2]

    def _update_region(self) -> None:
        """Recompute the selection from its seeds (spread, placement); the cutter preview
        follows a moment later."""
        r = self._region
        p = self.scene.get(r["pid"]) if r else None
        if p is None or r["mesh"] != id(p.mesh):
            self.clear_region()
            return
        s = self._surface(p)
        mask = np.zeros(len(p.mesh.faces), bool)
        for q in r["seeds"]:
            f = q["face"]
            if q["kind"] == "part":
                lab = self._part_labels(p, s)
                key = (int(self.props.part_edges.value()), int(lab[f]))
                mask |= self._snapped(p, s, q, key, lambda: lab == lab[f])
            elif q["kind"] == "picture":
                mask |= picture.pick(q["cands"], int(self.props.outline_size.value()))
            elif q["kind"] == "partsai":
                k = min(q["pick"], len(q["parts"]) - 1)
                mask |= self._snapped(p, s, q, k, lambda: q["parts"][k])
            elif q["kind"] == "draw":
                if q.get("mask") is not None:
                    mask |= q["mask"]
            else:
                barrier = self._contours(p, s) if self.props.stop_contours.isChecked() else None
                mask |= modify.grow_region(s, f, float(self.props.spread.value()), barrier=barrier)
        mask = modify.fill_holes(s, mask)
        for mm in r.get("grows", []):
            mask = modify.grow_by(s, mask, mm, p.mesh.faces)
        r["mask"], r["surface"] = mask, s
        self._show_contours()
        self._region_key = (r["pid"], id(p.mesh), p.matrix().tobytes())
        self.viewport.region_mesh = trimesh.Trimesh(s.vertices, np.asarray(p.mesh.faces)[mask], process=False)
        self.viewport.cut_preview = self.viewport.cut_lines = None
        self._preview_gen += 1
        if not hasattr(self, "_preview_timer"):
            self._preview_timer = QTimer(self)
            self._preview_timer.setSingleShot(True)
            self._preview_timer.timeout.connect(self._start_preview)
        self._preview_timer.start(PREVIEW_DELAY_MS)
        self.update_region_panel()
        self.viewport.update()

    def _start_preview(self) -> None:
        r = self._region
        p = self.scene.get(r["pid"]) if r else None
        if p is None or r.get("mask") is None or not r["mask"].any():
            return
        gen = self._preview_gen
        c = self.cut_settings()

        def done(res):
            if gen == self._preview_gen:
                self.viewport.cut_preview, self.viewport.cut_lines = res
                self.viewport.update()

        def failed(msg):
            if gen == self._preview_gen:
                self.viewport.cut_preview = self.viewport.cut_lines = None
                self.props.region_info.setText(self.props.region_info.text().split("\n")[0]
                                               + "\n⚠ " + msg.splitlines()[0])
        workers.submit(_joint_mesh, p.world_mesh(), r["surface"], np.asarray(p.mesh.faces), r["mask"],
                       c["depth"], c["smooth"], c["gap"], on_done=done, on_failed=failed)

    def _region_scene_changed(self) -> None:
        """After any scene change: drop the selection if its part or mesh went, follow moves."""
        r = self._region
        if r is None:
            return
        p = self.scene.get(r["pid"])
        if p is None or r["mesh"] != id(p.mesh):
            self.clear_region()
        elif self._region_key != (r["pid"], id(p.mesh), p.matrix().tobytes()):
            self._update_region()

    def update_region_panel(self) -> None:
        pp = getattr(self, "props", None)
        if pp is None:
            return
        r = self._region
        has = r is not None and r.get("mask") is not None and r["mask"].any()
        if has:
            s = r["surface"]
            q = self.scene.get(r["pid"])
            pp.region_info.setText(f"{int(r['mask'].sum()):,} triangles · {s.areas[r['mask']].sum():,.0f} mm²"
                                   + ("" if q is self.primary() else f" on {q.name}"))
        else:
            pp.region_info.setText("Nothing selected")
        pp.clear_region_btn.setEnabled(r is not None)
        pp.grow_btn.setEnabled(bool(has))
        grows = sum(r.get("grows", [])) if r else 0
        if has and grows:
            pp.region_info.setText(pp.region_info.text() + f" (grown {grows:+g} mm)")
        pp.cut_btn.setEnabled(bool(has) and not self._cutting)
        pp.select_btn.setEnabled(bool(self.scene.parts))

    # ------------------------------------------------------------ cut
    def cut_selection(self) -> bool:
        """Cut the selection off along its outline as a new part, in the background; cutFinished
        tells how it went. False when it can't start."""
        r = self._region
        p = self.scene.get(r["pid"]) if r else None
        if p is None or r.get("mask") is None or not r["mask"].any() or self._cutting:
            return False
        rep = self.reports.get(id(p.mesh))
        if rep is not None and not rep.watertight:
            QMessageBox.warning(self, APP, "Cut out selection: the mesh isn't closed. Repair it first "
                                           "(Models tab → Mesh).")
            return False
        pid, mesh_id, M = p.id, id(p.mesh), p.matrix()
        faces = np.asarray(p.mesh.faces)
        keep = np.asarray(p.mesh.vertices)[np.unique(faces[r["mask"]])]
        c = self.cut_settings()
        self._cutting = True
        self.update_region_panel()
        self.statusBar().showMessage(f"Cutting {p.name}…")
        self.log_msg(f"Cutting the selection off {p.name} (depth {c['depth']:g} mm, smoothing "
                     f"{c['smooth']:g} mm, fit gap {c['gap']:g} mm)…")

        def failed(msg):
            self._cutting = False
            self.update_region_panel()
            self.statusBar().clearMessage()
            QMessageBox.warning(self, APP, "Cut out selection: " + msg.splitlines()[0])
            self.cutFinished.emit(False)

        def done(res):
            self._cutting = False
            q = self.scene.get(pid)
            if q is None or id(q.mesh) != mesh_id:
                self.update_region_panel()
                self.cutFinished.emit(False)
                return
            self._apply_cut(q, *res)
            self.cutFinished.emit(True)
        workers.submit(_cut_job, p.mesh, M, r["surface"], faces, r["mask"], keep, c["depth"], c["smooth"],
                       c["gap"], on_done=done, on_failed=failed)
        return True

    def _apply_cut(self, p: Part, rest, piece, dropped, direction, reports) -> None:
        name = self._piece_name(p.name)
        pid = p.id
        self.clear_region()
        self.undo.beginMacro("Cut out selection")
        if p.supports is not None:
            before = SupportsCommand.snapshot(p)
            st = p.state()
            st.position[2] -= p.bounds()[0][2]
            self.undo.push(SupportsCommand(self, pid, before, (None, 0.0, st), "Clear supports"))
            self.log_msg(f"Removed the supports of {p.name}: they don't fit the cut shape.")
        old_bottom = p.bounds()[0][2]
        self.reports.pop(id(p.mesh), None)
        self.undo.push(MeshCommand(self, pid, rest, "Cut"))
        if p.bounds()[0][2] > old_bottom + 1e-6 and p.supports is None:
            self.edit_parts([pid], "Drop to plate", lambda q: self.scene.drop_to_plate(q))
        new = self._make_piece(p, piece, name, direction)
        self.undo.push(AddPartsCommand(self, [new], "Add piece"))
        if self._overlaps([new]):
            self.arrange_all()
        self.undo.endMacro()
        for m, rep in ((p.mesh, reports[0]), (new.mesh, reports[1])):
            self.reports[id(m)] = rep
            meshmod.slim(m)
        trim_memory_soon()
        self.select([new.id])
        self._sync_panels()
        self.statusBar().showMessage(f"Cut {name} off {p.name}", 8000)
        self.log_msg(f"Cut {name} off {p.name}: {piece.volume:,.1f} mm³ piece, {rest.volume:,.1f} mm³ left"
                     + (f", {dropped} loose sliver{'s' if dropped != 1 else ''} dropped" if dropped else "")
                     + ". The cut follows the selection's outline, so the piece fits back only one way.")

    def _piece_name(self, base: str) -> str:
        names = {q.name for q in self.scene.parts}
        name, i = f"{base} piece", 2
        while name in names:
            name, i = f"{base} piece {i}", i + 1
        return name

    def _make_piece(self, p: Part, piece_local: trimesh.Trimesh, name: str, direction) -> Part:
        """The cut-off piece as a part: laid with its cut side down on the plate beside the
        original, or left in place lifted 5 mm out along the cut direction."""
        w = trimesh.Trimesh(piece_local.vertices, piece_local.faces, process=False)
        w.apply_transform(p.matrix())
        if np.linalg.det(p.matrix()[:3, :3]) < 0:
            w.invert()
        lay = self.props.lay_cut.isChecked()
        if lay:
            T = np.eye(4)
            T[:3, :3] = modify.lay_on_plane_rotation(direction)
            w.apply_transform(T)
        else:
            w.apply_translation(np.asarray(direction, float) * 5.0)
        new = Part.from_mesh(name, w)
        new.opacity, new.repaired = p.opacity, p.repaired
        if lay:
            self.scene.drop_to_plate(new)
            lo, hi = p.bounds()
            nlo, nhi = new.bounds()
            new.position[0] += (hi[0] - nlo[0]) + 5.0          # beside the original, +X
            new.position[1] += (lo[1] + hi[1]) / 2 - (nlo[1] + nhi[1]) / 2
        else:
            new.elevation = max(0.0, float(new.bounds()[0][2]))
        return new
