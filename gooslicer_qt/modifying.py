"""Models tab → Modifying (11): select part of a part's surface, cut it off as its own part
along the selection's outline. A mixin of MainWindow: it uses the window's scene, viewport,
undo stack, settings and panels (and its `cutFinished` signal)."""
from __future__ import annotations

import numpy as np
import trimesh
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QMessageBox

from gooslicer import mesh as meshmod
from gooslicer import modify
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


class ModifyingMixin:
    """State: `_region` = {"pid", "mesh": id(part.mesh), "seeds": [face, …], "mask",
    "surface"} or None; `_adjacency` = (id(mesh), face adjacency) for the last mesh asked."""

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
            self.statusBar().showMessage("Select surface: click a part's surface; Ctrl+click adds an area "
                                         "(Esc to finish)")
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
    def region_pick(self, pid: int, face: int, add: bool) -> None:
        p = self.scene.get(pid)
        if p is None:
            return
        if pid not in self.selected_ids():
            self.select([pid])
        r = self._region
        if add and r is not None and r["pid"] == pid and r["mesh"] == id(p.mesh):
            if face not in r["seeds"]:
                r["seeds"].append(face)
        else:
            self._region = {"pid": pid, "mesh": id(p.mesh), "seeds": [face], "grows": [], "mask": None}
        self._update_region()

    def clear_region(self) -> None:
        self._region = None
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

    def _contours(self, p: Part, s: modify.Surface):
        """Contour edges (per adjacency row) of a part at the current angle; cached."""
        place = (id(p.mesh), p.matrix().tobytes())
        f = getattr(self, "_fold_cache", None)
        if f is None or f[0] != place:                       # the slow part: once per placement
            f = self._fold_cache = (place, modify.fold_angles(s))
        key = place + (int(self.props.contour_angle.value()),)
        c = getattr(self, "_contour_cache", None)
        if c is None or c[0] != key:
            c = self._contour_cache = (key, modify.contours(s, float(key[2]), fold=f[1]))
        return c[1]

    def _show_contours(self) -> None:
        """Draw the contours of the part being selected on (or the selected one while picking),
        when Stop at contours is on."""
        vp = self.viewport
        r = self._region
        p = self.scene.get(r["pid"]) if r else (self.primary() if vp.region_mode else None)
        if p is None or not self.props.stop_contours.isChecked() or not (vp.region_mode or r):
            vp.contour_lines = None
        else:
            s = r["surface"] if r and r.get("surface") is not None else self._surface(p)
            vp.contour_lines = s.vertices[s.edges[self._contours(p, s)]]
        vp.update()

    def region_settings_changed(self) -> None:
        st = self.settings
        st.setValue("modify/spread", int(self.props.spread.value()))
        st.setValue("modify/contours", bool(self.props.stop_contours.isChecked()))
        st.setValue("modify/contourFold", int(self.props.contour_angle.value()))
        for k, v in self.cut_settings().items():
            st.setValue(f"modify/{k}", float(v))
        if self._region is not None:
            self._update_region()
        else:
            self._show_contours()

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

    def _surface(self, p: Part) -> modify.Surface:
        if self._adjacency is None or self._adjacency[0] != id(p.mesh):
            self._adjacency = (id(p.mesh), modify.adjacency(p.mesh))
        return modify.surface(p.mesh, p.matrix(), self._adjacency[1])

    def _update_region(self) -> None:
        """Recompute the selection from its seeds (spread, placement); the cutter preview
        follows a moment later."""
        r = self._region
        p = self.scene.get(r["pid"]) if r else None
        if p is None or r["mesh"] != id(p.mesh):
            self.clear_region()
            return
        s = self._surface(p)
        barrier = self._contours(p, s) if self.props.stop_contours.isChecked() else None
        mask = np.zeros(len(p.mesh.faces), bool)
        for f in r["seeds"]:
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
