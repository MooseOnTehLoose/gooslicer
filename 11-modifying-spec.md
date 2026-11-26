# 11 — Modifying: surface selection and cut-out [GOOSLICER]

**Problem.** A hollowed model needs drain holes, and sometimes there's nowhere acceptable to drill them. `samples/Lisa_head_C.stl` has flat spots top and bottom; `Lisa_head_D.stl` only has one at the bottom, so its top would print as a suction cup with no place to drill. The fix is to cut part of the model off and print it as a separate piece, then glue the pieces together. On a hollowed model the cut opens the cavity, so resin drains and there's no suction.

Two tools, in a new **Modifying** pane of the Models tab:
1. **Select surface:** pick part of a model's outer surface by its curvature, with a threshold for how far the selection spreads.
2. **Cut out selection:** cut the selected part off along its outline, as a new model whose curved joint fits back only one way.

Nothing here is from SatelLite (it has no such tool in the strings we have); everything is [GOOSLICER].

## 1. Models tab layout
- **Mesh pane:** collapsed by default behind a disclosure button, like Position, size and scale (09 §8 / Transform). The button reads "Mesh: OK" or "Mesh: ⚠ N problems", so a problem still shows while it's folded. Opening it shows the report and Repair as now.
- **Modifying pane:** a new group under Mesh, open by default, with the two tools below. Disabled with no part selected.

## 2. Select surface
- **Select surface** (checkable button) starts picking: a cross cursor; a click on a part's surface selects the region around the click. That part becomes the selected part if it wasn't. **Ctrl+click** adds another region (same part). Esc or unchecking the button stops picking; the selection stays.
- **How a region grows.** The seed normal is the area-weighted average normal of the faces within 2 mm of the clicked face, so a bumpy or noisy surface still has a sensible direction. A face belongs to the region when its normal is within the **Spread** angle of the seed normal and it's connected to the clicked face through such faces (shared edges). On a sphere it's a cap; on a head clicked on the crown, the top of the head, stopping where the surface turns away (the "outer curves").
- **Spread:** a slider, 5°–90°, default 35°, with its value shown. Moving it recomputes every region from its seed, live. Remembered in `modify/spread`.
- Normals are taken in world space (the part's rotation, scale and mirror applied), so the result matches what's on screen.
- **Shown** in the 3D view drawn over the part in the **Selected surface** colour (Preferences → 3D view colors, `colors/region`, default green). An info line under the slider reads e.g. "4,584 triangles · 20 mm²".
- **Clear** empties the selection. The selection belongs to one part. It's dropped when that part's mesh changes (repair, hollow, cut, undo) or the part is deleted, and kept through moves, rotations and scaling (stored as face indices, recomputed for the view).
- Speed: 0.04–0.12 s on `Lisa_head_D.stl` (604k triangles, 2026-11-25 prototype) plus a one-off face-adjacency build per mesh, cached.

## 3. Cut out selection
The cut follows the selection's outline, not a plane (changed 2026-11-25 after trying a plane on `Lisa_head_D.stl`: a plane cut didn't follow the model). The curved, irregular joint also indexes the piece: it seats back only one way, so no pins are needed.
- **Selection used:** the grown regions plus the holes they enclose (unselected patches surrounded by the selection, e.g. a bump in the hair). Shells not touching the selection, such as a hollow's inside wall, are never added.
- **Cut direction** n: the area-weighted mean normal of the selected faces. The piece lifts out along it.
- **Cutter:** a solid made from the selection's outline seen along n. It's the union of the selected triangles projected on the plane across n, rounded by **Smoothing** (a round offset out then back in, default 0.5 mm, 0–5), with holes filled, then pushed along n. It runs from **Cut depth** below the selection's lowest point (default 3 mm, 0.2–100) to past the top of the part. Its walls run along n and follow the outline, so the cut goes straight into the part along the outline's curves.
- **Hollowed parts:** with a cut depth more than the wall thickness, the cutter goes through the wall into the cavity. The piece is the curved shell over the cavity with no floor, the rest gets a matching opening, and both drain through it, so there's no suction cup. On a solid part the piece gets a flat floor at the cut depth.
- **Fit gap** (default 0.1 mm, 0–1): the space left all round the joint, for glue. It's split evenly: the opening grows by half the gap and the piece shrinks by half. At 0 the cutter's walls still sit 0.002 mm off the outline, because walls exactly on mesh vertices leave zero-area slivers. Results are also run through manifold `simplify(1e-4)` to collapse any slivers left.
- **Pieces:** the parts inside the cutter that have a vertex of a selected face become the **new part**. Other parts inside (e.g. an ear the cutter's footprint also covers) stay with the rest. Loose bits under 0.5 mm³ (hair strands the cutter wall cut through) are dropped and counted in the log.
- **Preview:** the cutter is drawn translucent in the Selected surface colour. It's worked out in the background 250 ms after the selection or a setting last changed.
- **Cut out selection** runs in the background (`cutFinished`), then:
  - The new part is named "<name> piece" (then "piece 2" …). It keeps the opacity and the "(repaired)" flag.
  - **Lay cut side on the plate** (default on, `modify/layFlat`): the piece is turned so n points up (its cut side down) and placed beside the original (Arrange All if it overlaps). Off: it stays where it was, lifted 5 mm out along n.
  - The rest keeps its placement, and drops to the plate if the cut took its bottom.
  - It's one undo step, "Cut out selection". Supports on the part are removed (the log says so), the mesh is replaced, and the piece is added.
- **Refused** with a message when the mesh isn't closed ("Repair it first"), the cutter misses or takes everything, or the outline vanishes.
- Settings are remembered: `modify/spread`, `modify/depth`, `modify/smooth`, `modify/gap`, `modify/layFlat`.
- **Measured** on `Lisa_head_D.stl`, repaired and hollowed (1 mm wall), with the crown selected at a 50° spread (94k triangles, 405 mm²) and default settings. The cut took 4.2 s. Both results are closed single shells: the rest 2,940 mm³, with its cavity open through the crown, and the piece 521 mm³.

**Repair keeps hollows (fixed 2026-11-25).** Repair used to turn every shell outwards, which turned a hollow's cavity wall inside out and filled the cavity. With repair on import that hit hollowed models. Now a shell inside an odd number of other shells (tested by winding number) is kept facing into its cavity (04 P1).

## 4. Tests
- `modify.grow_region` on an icosphere: the region is a connected cap around the seed, and it grows with the spread angle (5° < 35° < 90°).
- `modify.fill_holes` fills holes punched in a cap and nothing else (not the hollow's inside).
- `modify.cutter` and `modify.cut` on a hollow ball, with a 40° cap: two watertight solids adding up to the ball. The piece is a curved cap of wall with no floor, as wide as the outline, its lowest points where the cutter's walls meet the inside of the wall. A fit gap shrinks the piece by the gap and leaves the hole as it was.
- On a U shape cut across both arms with one arm selected, only that arm becomes the piece.
- `mesh.repair` keeps a cavity facing in (and turns in one facing out), with a solid core inside it facing out.
- GUI: the Mesh pane starts folded with the status in its button; Select surface + click selects a region; the spread slider changes its size; Cut out selection gives two parts, a single undo restores one; refuses on an open mesh.

## 5. Open items
- A cut whose walls follow the surface normal rather than one direction (a curved cutter). The straight cutter guarantees the piece can be lifted out.
