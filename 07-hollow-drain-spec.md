# 07 — Hollowing, drain holes and trapped-resin check (GooSlicer P5)

Tags as in 06: **[OBSERVED]** is static RE of SatelLite; **[GOOSLICER]** marks a GooSlicer design choice where SatelLite's behaviour was not recovered.

Code: `gooslicer/hollow.py` (engine), `gooslicer_qt/hollowing.py` (dialogs, part-frame helpers), `gooslicer_qt/main.py` (Edit menu, undo, slice warning), `gooslicer_qt/viewport.py` (drain-hole pick mode).
Tests: `tests/test_hollow.py`, `tests/test_gui_smoke.py::test_hollow_drill_and_trapped_resin`.

## 1. Settings and defaults
- The Hollow dialog uses SatelLite's fields and defaults (06 §2) **[OBSERVED]**: wall 1 mm, accuracy 1 mm, type Inside / Outside / Center / Open bottom, surface angle 45° (5–85), cavity filter off, maximum cavities 1.
- The drain-hole dialog uses the Perforation defaults (06 §3) **[OBSERVED]**: radius1 = radius2 = 2 mm, extend1 = extend2 = 2 mm.
- The last-used values are kept in GooSlicer's own settings (`hollow/*`, `drain/*`), not SatelLite's keys.
- Not implemented: angle slot, notch, clearance, keep-subtracted plug, the "by cavity volume" filter in the dialog (the engine supports it).

## 2. Hollowing algorithm [GOOSLICER]
SatelLite uses OpenVDB / `HollowOctree`, which has no Linux wheel. GooSlicer's pipeline:
1. **Occupancy:** a winding-number inside test at voxel centres (pitch = accuracy), by casting +Z rays per column. Overlapping shells union correctly.
2. **Signed distance:** Euclidean distance transforms inside and outside. Within the wall band the values are replaced with the exact distance to dense surface samples, so the inner wall position is sub-voxel.
3. **Inner surface:** `manifold3d.Manifold.level_set` on the trilinear field (marching tetrahedra; watertight by construction).
4. **Boolean** with the original manifold. The outer surface is never resampled.

| Type | Result |
|---|---|
| Inside | part − {sd > t} |
| Outside | {sd > −t} − part (a shell grown outwards; the original becomes the cavity) |
| Center | {sd > −t/2} − {sd > t/2} |
| Open bottom | Inside, with each column's lowest cavity run extended down through the floor |

- **Open bottom:** SatelLite's use of the surface angle was not recovered; GooSlicer ignores it **[UNKNOWN for SatelLite]**.
- **Cavity filter:** pockets smaller than 8 voxels are dropped (they only trap resin). Then SatelLite's optional filter keeps the N largest.
- **Scaled parts:** hollowing and drilling work in the part's scaled frame, so the thickness is true millimetres even with non-uniform scale.
- **Limit:** grids above 60 M voxels are refused with a message asking for a coarser accuracy.

## 3. Drain holes
- **Placement:** Add drain holes… (Hollowing pane or Edit menu) opens the drain-hole settings in a panel floating over the top-left of the 3D view. The settings are outer and inner radius, extend outside, depth inside and direction. The panel isn't a dialog: the view stays live, so parts can be clicked, orbited and zoomed while it's open.
  - Under the cursor, the hole a click would cut is drawn as a translucent blue 3D frustum: the outer radius at extend outside, narrowing to the inner radius at depth inside. Its part inside the model shows fainter through the surface.
  - The preview follows every change to the settings, which are remembered (Preferences → Drain holes shows the same values).
  - A padlock right of each pair (outer/inner radius, extend outside/depth inside) links it: while closed, changing one value sets the other. Both are closed by default; clicking one opens it so the pair can differ, and closing it again copies the first value to the second. The padlock states are remembered; a saved pair that already differs loads unlocked.
  - Hover picking is exact ray/triangle tests over every face, culled first by each face's bounding sphere (a few ms for 300k faces, no index to build after the mesh changes). Mouse moves are coalesced so only the latest position is picked.
  - Each click drills one hole, one undo step, and the panel stays for the next. Drilling (the boolean and the mesh check) runs in the background with a status-bar message; clicks made meanwhile queue up, and a hole whose part was changed or undone meanwhile is dropped. Done or Esc ends the mode.
- **Geometry:** a cylinder, or a cone when the radii differ, from `extend1` outside the surface to `extend2` inside. radius1 is the outer end **[INFERRED]**.
- **Direction:** along the inward surface normal (default), or vertical (from below). SatelLite's `HoleDirID` presets were not mapped.

## 4. Trapped-resin check
- Voxelise all parts in world space (pitch ≤ 0.5 mm), label the empty space with 6-connectivity, and report every component that doesn't reach the grid border and is at least 1 mm³.
- Each cavity reports its volume and lowest point, as a hint for where to drill.
- It runs before "Slice…" and "Slice and send"; the user can still slice anyway. Edit → Check for voids runs it on demand.
- **Shown in red** [GOOSLICER]: `cavity_faces` also marks each mesh's faces that line a sealed cavity: a point just off the face on its outward side (0.6, 1.2 or 2 voxels, for thin walls) lies in the cavity's voxels. The 3D view draws them red, and since a cavity is hidden inside its part, also through the walls at 45 % opacity (back faces culled, so one even layer). Each cavity gets a white guide line from its lowest point to the edge of the build area, as islands do (08 §5), labelled "Sealed cavity" just past its outer end. The display stays until a part's mesh or placement changes (supports don't affect it).
- **Models tab → Hollowing** pane, above Supports: Hollow… (the selected part), Add drain holes…, Check for voids (in the background, with the result under the button) and Seal cavity.
- **Seal cavity** [GOOSLICER]: `cavity_shells` finds, per part, every connected shell whose area (≥ 80 %) lines a sealed cavity, such as the inner shell hollowing leaves, and deletes it, so the part is solid there again. All parts change in one undo step ("Seal cavities"), then the check runs again. A cavity whose wall is also the outside of a part (an open-bottom shell closed off by another part, say) has no such shell; it is reported as not sealed and stays red.
- **Fine confirmation** [GOOSLICER]: an opening narrower than about two voxels closes up at the coarse pitch, and the space behind it looks sealed. On a supported model (`samples/Lisa_torso.stl`, at the 0.5 mm pitch a wide plate gets), two flat pockets about 1.5 mm³ each, in creases under the model, were reported this way; at 0.4 mm or finer they are open. So each candidate is re-voxelised at a quarter of the pitch in a window around it, 4 mm (at least 3 voxels) wider than the cavity on every side, so a gap through a wall reaches space outside the part. If the cavity's empty space joins empty space the coarse pass found open, it is dropped. A window over 24 M fine voxels gets a coarser fine pitch; if that would be over half the coarse pitch, the candidate is kept unconfirmed (only very large cavities, which are hollows, not slivers).
- **Limits:** openings narrower than about a quarter voxel (≈ 0.1 mm), or longer than the 4 mm margin, aren't resolved, so a very small hole can still be reported as sealed, as can a narrow gap into a very large cavity. Open cups (resin held by suction against the film) are not detected.

## 5. Measured accuracy and speed
| Case | Result |
|---|---|
| 20 mm cube, 1 mm wall, accuracy 0.5 | wall volume within 0.2% of 2168 mm³; watertight |
| r = 10 sphere, 1 mm wall, accuracy 0.5 | within 0.5% |
| Outside / Center offsets | bounds within 0.05 mm |
| Drain hole Ø4 × 1.5 mm wall | removed volume within 1% of π·r²·t |
| arc (40k triangles), accuracy 1 / 0.5 / 0.25 | 0.6 / 2.4 / 14 s |
| Trapped-resin check, hollowed arc | 0.2 s |

These are fast enough that the plan's C++ fallback isn't needed.

## 6. Open items
- Optional: decompile `EleTriHole.dll` (hole presets, notch and slot geometry) and SatelLite's Open Bottom surface-angle rule.
- Optional internal infill grid.
- Cup detection.
