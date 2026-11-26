# 07 — Hollowing, drain holes and trapped-resin check (GooSlicer P5)

Tags as in 06: **[OBSERVED]** is static RE of SatelLite; **[GOOSLICER]** marks a GooSlicer design choice where SatelLite's behaviour was not recovered.

Code: `gooslicer/hollow.py` (engine), `gooslicer_qt/hollowing.py` (Hollow and drain-hole panels, part-frame helpers), `gooslicer_qt/main.py` (Edit menu, undo, slice warning), `gooslicer_qt/viewport.py` (drain-hole pick mode).
Tests: `tests/test_hollow.py`, `tests/test_gui_smoke.py::test_hollow_drill_and_trapped_resin`, `tests/test_gui_viewport.py` (drain-hole panel, pin, fields).

## 1. Settings and defaults
- The Hollow settings use SatelLite's fields and defaults (06 §2) **[OBSERVED]**: wall 1 mm, accuracy 1 mm, type Inside / Outside / Center / Open bottom, surface angle 45° (5–85, used by Center only), cavity filter off, maximum cavities 1.
- The drain-hole settings use the Perforation defaults (06 §3) **[OBSERVED]**: radius1 = radius2 = 2 mm, extend1 = extend2 = 2 mm, Hole form, slot depth 2 mm, 50 edges, notch 0.4 × 0.4 mm, clearance 0.
- **Hollow panel** [GOOSLICER]: Hollow… (Hollowing pane, Edit menu, Ctrl+Shift+H) is available whenever there are parts, selected or not, and opens the settings in a panel floating over the top-left of the 3D view, like the drain-hole panel (§3). It stays open until Done or Esc, so several parts can be hollowed in turn. Click a part to select it (Ctrl+click for more), then press Hollow. Each selected part is hollowed in the background as its own undo step, while the panel shows "Hollowing N parts…". Opening the drain-hole tool closes it. Settings changed in the panel are saved at once. (SatelLite uses a modal dialog.)
- **See-through** [GOOSLICER]: a hollowed part is drawn see-through (front faces only), so its cavity, pillars and infill show. The panel's Opacity field (5–100 %, default 50 %, `hollow/opacity`; also in Preferences) sets how solid it looks; it changes only the view, not the print. Undoing the hollow puts the part's previous opacity back. The part's right-click menu and View → See-through (X; the selected parts, or all) switch between solid and that opacity, as undoable steps. Projects store it as `opacity` on the part (09 §2).
- The last-used values are kept in GooSlicer's own settings (`hollow/*`, `drain/*`), not SatelLite's keys.
- Not implemented: the "by cavity volume" filter in the dialog (the engine supports it).

## 2. Hollowing algorithm [GOOSLICER pipeline, SatelLite's types]
SatelLite uses OpenVDB / `HollowOctree`, which has no Linux wheel. GooSlicer's pipeline:
1. **Occupancy:** a winding-number inside test at voxel centres (pitch = accuracy), by casting +Z rays per column. Overlapping shells union correctly.
2. **Signed distance:** Euclidean distance transforms inside and outside. Within the wall band the values are replaced with the exact distance to dense surface samples, so the inner wall position is sub-voxel.
3. **Inner surface:** `manifold3d.Manifold.level_set` on the trilinear field (marching tetrahedra; watertight by construction).
4. **Boolean** with the original manifold. The outer surface is never resampled.

| Type | Result |
|---|---|
| Inside | part − {sd > t} |
| Outside | {sd > −t} − part (a shell grown outwards; the original becomes the cavity) |
| Center (self-supporting) | part − ({sd > t} − pillars): Inside, with pillars under flat cavity ceilings |
| Open bottom | Inside, measured from every face but the bottom ones, and open through the floor there |

**What SatelLite does** [OBSERVED: EleShapeModify `DialogHollow::on_confirm_clicked` and `on_comboBoxChanged`; EleShapeEdge `SelfSupportHollow`] (2026-11-25):
- Inside (0) and Outside (1) offset by −t and +t.
- **Center (2)** is not a wall centred on the surface. The dialog labels it "Center Hollow" and calls `HollowImp::setSelfSupportHollow(true, surfaceAngle)` with −t: a self-supporting inside hollow. The surface-angle box is shown for this type only.
  - The model is scaled so its largest size is at most 100 (accuracy with it), and an inside-out copy is made.
  - SatelLite's support anchor finder (`ESupport::SmartAnchorProcessor`) runs on that copy with the surface angle as its critical angle. Seen from inside, the part's roofs face down, so the anchors land on the underside of every roof flatter than the angle. Anchors below the model's lowest point are dropped.
  - From each anchor, rays along ∓Z within 0.15 snap it to the surface. It is moved down 0.001 and a ray straight down finds the floor.
  - A 3-sided cone bar (`creatConeMeshOfBar`) joins them: radius 0.01 × accuracy at the roof, accuracy/√3 at the floor.
  - `selfSupportHollowKeepInnerOnly` fixes holes, then level-set offsets the mesh with the bars included (voxel = accuracy × 1.06, or 0.94 when the thickness is that small). So the bars are thickened by the wall too and become pillars. Then it flips normals, filters small shells, fairs (5 passes) and merges.
- **Open Bottom (3)** doesn't use the surface angle, and the dialog hides the cavity filter for it. `markOpenBottomFaces` marks the bottom faces, the marked faces are deleted and the rest is shelled inward by t ("Shell mesh").
  - band = clamp(¼ × min(thickness, accuracy), 0.02, 0.2) mm.
  - A face is marked when its lowest corner and its centre are both within the band of the part's lowest point, and |n_z| ≥ 0.7.
  - If none is: the faces whose centres are within the band of the lowest centre.

**GooSlicer:**
- **Center** (`self_support_pillars`): anchors from `supports.detect_contacts` on an inside-out copy, with the surface angle as the critical angle; floors by a ray straight down (`raycast.Caster.vertical`). The bars have SatelLite's radii. They join the field as `min(sd, distance to the bars) − t`, so each pillar is a bar thickened by the wall, as in SatelLite. An anchor whose floor is closer than 2t + accuracy is dropped (there is no cavity there).
  - **[GOOSLICER]**: anchor spacing 4 × (t + accuracy), and anchors kept 2 × (t + accuracy) back from a roof's edge, where a pillar would only merge into the wall. SatelLite's spacing comes from its anchor generator's parameters, which weren't mapped **[UNKNOWN]**. No scaling to 100 mm.
  - A 30 × 30 × 10 slab with a 1 mm wall gets 17 pillars; a 45° cone gets none at a 30° surface angle.
- **Open bottom** (`bottom_faces`, `_open_bottom`): SatelLite's face rule. Near the marked faces, the field is the distance to the *other* faces, so the wall is measured as from the shell of the open mesh. Where the cavity then reaches a marked face, it is carried on down out of the part, and the floor opens there.
  - A 20 mm cube opens 18 × 18 mm (1 mm wall). A box on a 6 × 6 × 2 mm foot opens only the foot's sole, about 4 × 4 mm.
  - A sphere's marked cap is narrower than the wall, so the shell closes over it and the cavity stays sealed; the trapped-resin check reports it. A mesh-offset shell like SatelLite's may leave a small hole there **[INFERRED]**.
- **Cavity filter:** pockets smaller than 8 voxels are dropped (they only trap resin). Then SatelLite's optional filter keeps the N largest.
- **Scaled parts:** hollowing and drilling work in the part's scaled frame, so the thickness is true millimetres even with non-uniform scale.
- **Limit:** grids above 60 M voxels are refused with a message asking for a coarser accuracy.

## 2b. Infill lattice (2026-11-25)
**SatelLite** [OBSERVED: EleTriInfill.dll `DialogInfill::setUI`, `handleResult`, `createLattice`]:
- `DialogInfill` extends the Hollow dialog with an "Infill Format" group:
  - "Infill structure" checkbox (`useHatch`);
  - "Fill Structure Type": BCC or FCC;
  - "Fill Structure Dimensions": 4 mm, 3 decimals, step 1;
  - "Support Radius": 1 mm.
- No ini keys were found, so these are the dialog's defaults each time.
- With the box ticked and a type other than Open Bottom (index 3), `createLattice` hands the parts and these values to `StructureImp` (EleTriStructure.dll) with hollowing on. Otherwise it is a plain hollow. A failure reports "Failed to add padding structure." The lattice geometry, in `StructureImp`, was not decompiled.

**SatelLite's lattice** [OBSERVED: EleTriInfill `DialogInfill` FUN_180002670, EleTriCreate `GenerateLattice::generateLattice`, EleTriStructure `StructureImp::dowork_file`] (2026-11-25):
- `StructureParas`: type = the combo index (BCC → 0, FCC → 2); cell x = y = z = size; the radius; 4 segments; hollowing on.
- One cell: the box [r, r + size]³. Type 0 (BCC) is the 4 body diagonals, corner to corner. Type 2 (FCC) is both diagonals of all 6 faces (12 bars). Every bar is a `creatConeMeshOfBar` with the strut radius and **4 segments** (square), no caps, and **no balls at the joints**. The bars are united, intersected with the box, merged and simplified.
- `dowork_file` scales the cell to exactly the size and tiles it from the **part's bounding-box low corner**, int(dimension / size + 2) cells per axis. A voxel classification and booleans with the cavity follow.

**GooSlicer**:
- `HollowSettings.infill`, `infill_type` ("bcc" / "fcc"), `infill_size` and `infill_radius`. They are saved under `hollow/infill*` in GooSlicer's settings.
- The Hollow panel (and Preferences) show them below the cavity filter. They are disabled for Open bottom, where `hollow()` also ignores them.
- **Lattice** (`lattice`, `_cell`): SatelLite's cell. Square bars from `Manifold.cylinder` with 4 segments, the radius as the square's corner radius **[INFERRED]**. The cell is clipped to its box, made 10⁻⁴ × size larger on each side so neighbours overlap rather than touch. Tiled from the part's low corner, int(size / cell + 2) per axis, by doubling (log₂ n unions per axis). More than 400,000 cells is refused.
- **Applied** as `cavity − lattice` before the cavity is cut out, for Inside and Center (Outside: the original part is the cavity). So struts exist only inside the cavity and run on into the walls, making one solid. The cavity's empty space stays connected, so it still counts as one cavity and still needs a drain hole.
- **Measured:** 20 mm cube, 1.5 mm wall, accuracy 0.5, defaults: watertight in about 0.6 s; the solid volume goes from 3,092 to 5,690 mm³ with BCC and 6,039 with FCC. A 1 mm radius in a 4 mm cell is dense, so a larger cell or thinner struts save resin.
- **Validation:** strut radius at most a quarter of the cell size.

## 3. Drain holes
**SatelLite's Perforation tool** [OBSERVED: EleTriHole `DockWidgetPerf` (UI, `loadParas`), `PerforationSelection::setParas`, `Perforator::findNextInterPoint`, `CreateMesh::Create`, `BooleanImp`] (2026-11-25):
- **Panel:**
  - "Hole Form": Hole or Slot (`/Perforation/holeType` 1 / 0), with "Depth of slot" (`depth`, 2).
  - "Edge number" 3–50 (`edgenumber`, 50).
  - "Radius outer circle" and "Radius inner circle" (radius1, radius2), and one "Protrusion Length" for the extensions.
  - "Use angle" and an angle; "Use notch" with width, height and angle.
  - "Keep Hole Pin" with a clearance, and a "Hole Direction" combo, Inside / Outside / Center. That combo is the side the clearance is taken from (clearanceType 1 / 2 / 3), not the drill direction.
  - The Structure page's version has "Perpendicular to surface" or "Custom" (HoleDirX/Y/Z, default 1, 0, 0), always the Hole form and 50 edges.
- **Direction:** the inward normal (−n) when perpendicular. Otherwise the custom vector, negated if it points out of the surface (n · v > 0).
- **Far point:** an Embree ray from the clicked point along the direction; the nearest hit is the far side of the wall. The hole's length L is that distance; for a slot, L is the slot depth.
- **`PerforatorParas`** (0x54 bytes): point, far point, r1, r2, ext1, ext2, angle, useAngle, useNotch, notch width / height / angle, edges, direction, isSlot, depth. useAngle only counts for holes; useNotch is width ≠ 0 and height ≠ 0. The notch width is clamped to 1.999 × r1.
- **`CreateMesh::Create(paras, c)`** (c = clearance), built along +Z and turned by the shortest rotation from +Z onto the direction, at the clicked point:
  - Profile: points on the circle from a0 to 2π − a0 in N steps. a0 = 0 without a notch. With one, a0 = asin(½ × max(w + 2c, 0.002) / (r1 + c)), or π/2 if wider. The notch adds two points on the +X side, the notch height past the chord, w + 2c wide. With clearance its tip moves out by c: tip x = r1 × cos(asin(w / 2r1)) + h + c.
  - Cylinder when r1 = r2: radius r1 + c from z = −ext1 to L + ext2. A slot has no ext2.
  - Narrowing inward (r1 > r2, or "Use angle"): θ = atan((r1 − r2) / L), capped at 0.8 rad, or the given angle. Radius r1 + c at the surface, + ext1 × tan θ at the outer end, and extrapolated to L + ext2 unless it reaches the axis first, where it ends in a point.
  - Widening inward (r2 > r1): the same, built from the far end with radii and extensions swapped.
  - The rings are closed with fan caps.
- **Keep Hole Pin:** `boolean_subtract_keep` keeps the plug as its own object. With the clearance on the pin (Inside): hole at c = 0, pin at −c. On the hole (Outside): hole at +c, pin at 0. Both (Center): ±c/2. Clearance only widens radially (and the notch).
- The angle that turns the notch about the axis is read but not used inside `Create` **[UNKNOWN]**.

**GooSlicer** (`hollow.DrainHole`, `hole_mesh`, `hole_length`, `drill_keep`; `gooslicer_qt/hollowing.py`):
- **Geometry:** SatelLite's, as above. radius1 is the radius at the clicked surface and radius2 at the far end. extend1 runs out past the surface, extend2 past the far wall. GooSlicer keeps both extensions (SatelLite shows one). The far wall is found by a ray in the part's mesh (`scene.ray_triangles`).
  - The notch angle turns the notch about the axis **[INFERRED use]**.
  - On a taper, the whole profile (notch included) is scaled with the ring radius **[INFERRED]**.
- **Keep hole pin:** the plug (part ∩ the pin cutter) becomes a new part named "‹part› pin", set on the plate in the nearest free spot. The hole and the pin are one undo step. Clearance only applies when the pin is kept.
- **Direction:** perpendicular to the surface (default), vertical (from below, GooSlicer's), or a custom world vector turned to point into the part.
- **Placement:** Add drain holes… (Hollowing pane or Edit menu) opens the drain-hole settings in a panel floating over the top-left of the 3D view. It shows form, slot depth, outer and inner radius, extend outside and inside, and direction (with the custom X Y Z). "More options" shows edges, taper by angle, notch, keep hole pin and clearance. The panel isn't a dialog: the view stays live, so parts can be clicked, orbited and zoomed while it's open.
  - Under the cursor, the hole a click would cut is drawn as a translucent blue solid: the cutter itself, through the wall to the next surface. Its part inside the model shows fainter through the surface.
  - The preview follows every change to the settings, which are remembered (Preferences → Drain holes shows the same values).
  - A padlock right of each pair (outer/inner radius, extend outside/inside) links it: while closed, changing one value sets the other. Both are closed by default; clicking one opens it so the pair can differ, and closing it again copies the first value to the second. The padlock states are remembered; a saved pair that already differs loads unlocked.
  - Hover picking is exact ray/triangle tests over every face, culled first by each face's bounding sphere (a few ms for 300k faces, no index to build after the mesh changes). Mouse moves are coalesced so only the latest position is picked.
  - Each click drills one hole, one undo step, and the panel stays for the next. Drilling (the boolean and the mesh check) runs in the background with a status-bar message; clicks made meanwhile queue up, and a hole whose part was changed or undone meanwhile is dropped. Done or Esc ends the mode.
- A Hole-form click on a solid part goes right through it, as in SatelLite. A slot is the tool for a blind hole.

## 4. Trapped-resin check
- Voxelise all parts in world space (pitch ≤ 0.5 mm), label the empty space with 6-connectivity, and report every component that doesn't reach the grid border and is at least 1 mm³.
- Each cavity reports its volume and lowest point, as a hint for where to drill.
- It runs before "Slice…" and "Slice and send"; the user can still slice anyway. Edit → Check for voids runs it on demand.
- **Shown in red** [GOOSLICER]: `cavity_faces` also marks each mesh's faces that line a sealed cavity: a point just off the face on its outward side (0.6, 1.2 or 2 voxels, for thin walls) lies in the cavity's voxels. The 3D view draws them red, and since a cavity is hidden inside its part, also through the walls at 45 % opacity (back faces culled, so one even layer). Each cavity gets a white guide line from its lowest point to the edge of the build area, as islands do (08 §5), labelled "Sealed cavity" just past its outer end. The display stays until a part's mesh or placement changes (supports don't affect it).
- **Models tab → Hollowing** pane, above Supports: Hollow… (the panel above), Add drain holes…, Check for voids (in the background, with the result under the button) and Seal cavity.
- **Seal cavity** [GOOSLICER]: `cavity_shells` finds, per part, every connected shell whose area (≥ 80 %) lines a sealed cavity, such as the inner shell hollowing leaves, and deletes it, so the part is solid there again. All parts change in one undo step ("Seal cavities"), then the check runs again. A cavity whose wall is also the outside of a part (an open-bottom shell closed off by another part, say) has no such shell; it is reported as not sealed and stays red.
- **Fine confirmation** [GOOSLICER]: an opening narrower than about two voxels closes up at the coarse pitch, and the space behind it looks sealed. On a supported model (`samples/Lisa_torso.stl`, at the 0.5 mm pitch a wide plate gets), two flat pockets about 1.5 mm³ each, in creases under the model, were reported this way; at 0.4 mm or finer they are open. So each candidate is re-voxelised at a quarter of the pitch in a window around it, 4 mm (at least 3 voxels) wider than the cavity on every side, so a gap through a wall reaches space outside the part. If the cavity's empty space joins empty space the coarse pass found open, it is dropped. A window over 24 M fine voxels gets a coarser fine pitch; if that would be over half the coarse pitch, the candidate is kept unconfirmed (only very large cavities, which are hollows, not slivers).
- **Limits:** openings narrower than about a quarter voxel (≈ 0.1 mm), or longer than the 4 mm margin, aren't resolved, so a very small hole can still be reported as sealed, as can a narrow gap into a very large cavity. Suction cups are found separately (§4b).

## 4b. Suction cups [GOOSLICER] (2026-11-24)
A cup is a pocket closed on every side except the top, the side the film is on. While its walls print, the film closes the mouth, so every lift pulls on the resin inside, which can tear the print off its supports. The trapped-resin check finds cups on the same voxel grid as sealed cavities (`hollow._cups`, `cups=True`):
- **Held space:** in each voxel layer, empty space that the part encloses on every side within that layer (4-connected, not reaching the grid's edge).
- **Cups:** 6-connected pieces of held space that belong to the outside (not a sealed cavity), and that no voxel of open space lies directly below. Open space is empty space that is not held. Layers within half a voxel of the plate, or below it, count as closed, so a cup standing rim-down on the plate is a cup too.
- So a drain hole anywhere in the floor clears it. A cup opening downward drains.
- **Size limit:** a cup's largest layer must be at least 10 mm² (`CUP_MIN_AREA`), and its volume at least 1 mm³. Smaller pockets, such as engraved text or pits, pull too little to matter.
- **In the app:**
  - Cups are listed with the sealed cavities and drawn the same way (red walls, numbered guide lines), labelled "Suction cup N". The detail gives the volume, the area across and the lowest point.
  - The panel, the Log and the slice warning name both kinds ("1 sealed cavity and 2 suction cups"). Their advice adds "or turn the part so its cups open toward the plate".
  - Seal cavity leaves cups as they are, since they have no shell to delete, and says so.
- **Checked on:** a 20 mm cup with 2 mm walls is found upright on the plate, upright on supports, and rim-down on the plate. It is not found with a Ø2 mm drain in its floor, or rim-down on supports. A Ø3 mm pit is not found. On the real models, arc has no cups. Lisa_torso has one, a 9 × 18 × 2 mm recess facing up halfway up the torso, found in 1.6 s.
- **Limits:** a gap narrower than about two voxels isn't seen, so a cup with a very thin slit still counts as a cup. Supports aren't part of the check.

## 5. Measured accuracy and speed
| Case | Result |
|---|---|
| 20 mm cube, 1 mm wall, accuracy 0.5 | wall volume within 0.2% of 2168 mm³; watertight |
| r = 10 sphere, 1 mm wall, accuracy 0.5 | within 0.5% |
| Outside offset | bounds within 0.05 mm |
| Drain hole Ø4 × 1.5 mm wall | removed volume within 1% of π·r²·t |
| arc (40k triangles), accuracy 1 / 0.5 / 0.25 | 0.6 / 2.4 / 14 s |
| Trapped-resin check, hollowed arc | 0.2 s |

These are fast enough that the plan's C++ fallback isn't needed.

## 6. Open items
- Center: the anchor spacing SatelLite's `g_ESupportPara` gives (its fields weren't mapped), and SatelLite's scaling to 100 mm, which changes how far apart anchors are on large parts.
- Infill: whether the radius is the square bar's corner (circumradius) or half its side; the voxel classification step in `StructureImp` was not followed.
- Drain holes: what SatelLite does with the notch angle.
- To check against SatelLite output, when samples are available: a Center hollow, an Open Bottom hollow on a part that doesn't stand on a flat face, a notched pin hole.
