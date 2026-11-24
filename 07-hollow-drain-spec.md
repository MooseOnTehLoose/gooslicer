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
- **Placement:** Edit → Add drain holes, then click on parts; Esc ends the mode. Each hole is one undo step.
- **Geometry:** a cylinder, or a cone when the radii differ, from `extend1` outside the surface to `extend2` inside. radius1 is the outer end **[INFERRED]**.
- **Direction:** along the inward surface normal (default), or vertical (from below). SatelLite's `HoleDirID` presets were not mapped.

## 4. Trapped-resin check
- Voxelise all parts in world space (pitch ≤ 0.5 mm), label the empty space with 6-connectivity, and report every component that doesn't reach the grid border and is at least 1 mm³.
- Each cavity reports its volume and lowest point, as a hint for where to drill.
- It runs before "Slice…" and "Slice and send"; the user can still slice anyway. Edit → Check for trapped resin runs it on demand.
- **Limits:** openings narrower than about two voxels (≈ 1 mm) aren't resolved, so a very small hole can still be reported as sealed. Open cups (resin held by suction against the film) are not detected.

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
