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
- **Spread:** a slider, 5°–90°, default 35°, with its value shown. Moving it recomputes every region from its seed, live (with contours and grows). Remembered in `modify/spread`.
- **Contours** (added 2026-11-25, cleaned up the same day): with **Stop at contours** on (default, `modify/contours`), a region never spreads across a contour, so it follows the model's own outlines (a strap's edge, a groove, a hairline) as well as the spread angle. While picking, the part's contours are drawn on it as thin dark lines in the Selected surface colour.
  - **Fold angle:** a contour is a valley where the surface folds at least the **contour fold** (slider 10°–90°, default 35°, `modify/contourFold`) within about a millimetre. Lower finds more, fainter contours.
  - **How the fold is measured:** normals are smoothed over 0.5 mm (neighbour averaging, the number of passes set from the face spacing, at most 60). The fold is then the turn between neighbouring smoothed normals per mm × 1 mm, so it doesn't depend on how fine the mesh is. Ridges never count: valley or ridge is decided by the faces' own normals.
  - **Cleaned like an edge detector:**
    - *Thinned:* an edge stays only if its fold is the largest round its two faces, so a crease is one line, not a band.
    - *Joined:* edges folding at least half the angle carry a line on from a strong one (hysteresis), so lines don't break up.
    - *Spanning:* a connected line must span 4 mm end to end. Hair and skin texture makes tangles that are long but small, and those go.
  - **Why not per triangle:** per-triangle creases on `Lisa_head_D.stl` gave 187k crease edges at 30°, all joined into one network. The first smoothed version still drew hair speckle. The cleaned one at 35° draws the ear, the hairline and the gag (about 5,500 edges); lower the fold to pick up the straps.
  - The smoothing is the slow part (about 1.1 s on the hollowed head, 618k triangles). It's cached per part placement, so moving the slider only redoes the rest. A pick takes about 0.2 s.
- **Select by** (added 2026-11-25; combobox above Spread, `modify/selectBy`) sets what a click selects. Ctrl+click adds to the selection in any mode, and a selection can mix modes. Grow selection, Clear and the cut work the same for all three. Each mode shows only its own controls.
  - **Surface** (default): the spread and contours above.
  - **Parts**: the click selects the whole part of the model it lands on (see *Parts* below). Part edges are drawn on the model while picking, in place of contours. **Part edges** slider (2°–30°, default 10°, `modify/partEdges`): how deep a valley must run along a border for it to stay a part edge. Lower gives more, smaller parts.
  - **Picture (AI)**: an image model outlines the thing you clicked in the 3D view, the way you see it (see *Picture* below). **Outline** slider, Small / Medium / Large (default Medium, `modify/outline`): which of the model's candidate outlines to use, from smallest to largest. Changing it re-applies the stored candidates; nothing is recomputed. Shown only when the model is installed; otherwise the entry is disabled, with a tooltip saying so.
- **Parts** (`gooslicer/parts.py`) [GOOSLICER]: splits the surface into the pieces a person would name (an ear, a strap, the mask round the eyes), by the *minima rule*: one shape meets another along a valley.
  1. *Patches*: one seed per 1.2 mm cell of each shell. Faces go to the nearest seed along the surface, where crossing a contour (the fold at 20°, cleaned as in *Contours*) costs 200× its length (a near-wall; a 10× wall let a seed on one side take a strip of the other). So patch borders lie on creases where there are creases.
  2. *Normalized cuts*: the patches form a graph. Two patches are joined by the length of their shared border, weighted by exp(−valley / 20°); ridges count 15 % as much as valleys. The graph is split in two along its Fiedler vector at the sweep point with the lowest normalized cut, and each half again, while the cut is under 0.1. This finds regions only weakly attached to the rest (an ear on its rim, a band between two strap edges) even where a crease has gaps.
  3. *Merge back*: normalized cuts also split smooth surfaces where nothing marks a border. So neighbouring parts are merged, the weakest first, while the length-weighted mean valley fold along their border is under the **Part edges** angle. Parts under 1 mm² always merge.
  - Measured on the hollowed `Lisa_head_D.stl` (618k triangles): about 1.2 s once per part placement, on top of the 1.1 s contour smoothing (cached and shared with contours). At 10° it gives 56 parts (70 at 6°, 38 at 15°); changing the angle only redoes the merge (0.02 s). The eye socket inside the mask, the ear and the hood edge come out as parts; some smooth skin is still split where the cut found no clear border, so Ctrl+click adds a neighbouring part.
- **Picture** (`gooslicer/picture.py`) [GOOSLICER]: MobileSAM ("segment anything" for small devices; ONNX export by Acly, MIT, `huggingface.co/Acly/MobileSAM` at commit `0d3b4033`) run on the CPU with onnxruntime.
  - On a click, the part alone is drawn as it's seen, opaque in the model colour on the view's background, with no overlays. That render goes to the image encoder (long side 1024 px, about 0.2–0.3 s). The embedding is cached until the view, the part or its placement changes. The clicked face's centre is the prompt point. The multi-mask decoder (about 0.03 s) returns 4 candidate outlines.
  - Candidates are sorted by area. One covering at least 95 % of the part's own pixels is dropped when others remain. The Outline slider picks Small (the smallest), Medium (the middle one, or the larger of two) or Large (the largest).
  - **From pixels to faces:** a second render of the part, at twice the size, writes each face's index into the colour (gl_PrimitiveID, 24 bits, no multisampling). A face is selected when any of its pixels is inside the outline. Faces smaller than a pixel leave holes, which *fill holes* closes as for every selection. Only visible faces can be selected, so the cavity wall and the far side are never taken.
  - **Measured** in the app on the hollowed `Lisa_head_D.stl`:
    - Clicked between the eyes, Small is the hood round the eyes, Medium the whole hood with both ears, and Large adds the face. Clicked on an ear, Medium is exactly that ear.
    - The first click took 1.1 s (loading the model and encoding the view). Later clicks in the same view took 0.3 s.
  - **Files:** `mobile_sam_image_encoder.onnx` (28,157,093 bytes, sha256 `580f5fb6…c749`) and `sam_mask_decoder_multi.onnx` (16,496,559 bytes, sha256 `8976b90a…2ffe`).
  - **Where the app looks:** `$GOOSLICER_MODELS`, `/app/share/gooslicer/models` (the Flatpak downloads them at build time, pinned by sha256), `models/` in the source tree (`scripts/fetch-models.sh`, git-ignored), then `~/.local/share/gooslicer/models`.
  - onnxruntime (MIT) is a dependency. Without it or the files, Picture is disabled and everything else works.
- **Grow selection** (button, with a distance −20…20 mm beside it, default 1 mm, `modify/grow`; placed just above Cut out selection): grows the selection by that distance along the surface, or shrinks it when negative.
  - Distances run along the mesh's edges (Dijkstra) from the selection's vertices. A face joins when all its corners are in reach. A shrink is the same grow applied to the rest, so +1 then −1 gives back about the same selection.
  - It doesn't stop at contours or the spread, and stays on the shells it's on.
  - Grows are kept, in order, through later spread, contour or placement changes, until a plain click starts a new selection or Clear. The info line shows the total, e.g. "(grown +1 mm)". About 0.3 s on the hollowed head.
- Normals are taken in world space (the part's rotation, scale and mirror applied), so the result matches what's on screen.
- **Shown** in the 3D view drawn over the part in the **Selected surface** colour (Preferences → 3D view colors, `colors/region`, default green). An info line under the slider reads e.g. "4,584 triangles · 20 mm²".
- **Clear** empties the selection. The selection belongs to one part. It's dropped when that part's mesh changes (repair, hollow, cut, undo) or the part is deleted, and kept through moves, rotations and scaling (stored as face indices, recomputed for the view).
- Speed: 0.04–0.12 s on `Lisa_head_D.stl` (604k triangles, 2026-11-25 prototype) plus a one-off face-adjacency build per mesh, cached.

## 3. Cut out selection
The cut follows the selection's outline, not a plane (changed 2026-11-25 after trying a plane on `Lisa_head_D.stl`: a plane cut didn't follow the model). The curved, irregular joint also indexes the piece: it seats back only one way, so no pins are needed.
- **Selection used:** the grown regions plus the holes they enclose (unselected patches surrounded by the selection, e.g. a bump in the hair). Shells not touching the selection, such as a hollow's inside wall, are never added.
- **Cut direction** n: the area-weighted mean normal of the selected faces. The piece lifts out along it.
- **Cutter:** a solid that, seen along n, is the selection's outline: the union of the selected triangles projected on the plane across n, rounded by **Smoothing** (a round offset out then back in, default 0.5 mm, 0–5), holes filled.
  - **Its bottom follows the selected surface** **Cut depth** beneath it (default 3 mm, 0.2–100). The height comes from the lowest selected point in each 0.25 mm cell of a grid along n; empty cells take their nearest neighbour's. The extruded outline is refined to 1 mm and its bottom warped down onto that height.
  - So it takes only the selected surface and what's under it to that depth, never material elsewhere. An earlier version with a flat bottom at the selection's lowest point could reach well into the part under a sloped selection.
  - Its walls run along n and follow the outline, so the piece lifts out along n.
  - On a 50° cap of a solid ball with a 2 mm depth, the piece is a skin at most 2.4 mm thick (one cell's rise on the slope).
- **Hollowed parts:** with a cut depth more than the wall thickness, the cutter goes through the wall into the cavity. The piece is the curved shell over the cavity with no floor, the rest gets a matching opening, and both drain through it, so there's no suction cup. On a solid part the piece gets a flat floor at the cut depth.
- **Fit gap** (default 0.1 mm, 0–1): the space left all round the joint, for glue. It's split evenly: the opening grows by half the gap and the piece shrinks by half. At 0 the cutter's walls still sit 0.002 mm off the outline, because walls exactly on mesh vertices leave zero-area slivers. Results are also run through manifold `simplify(1e-4)`, then vertices at exactly the same place are welded and the zero-area triangles that leaves are dropped (kept only if the mesh stays closed). On the hollowed head a cut without this left 20–48 such triangles.
- **Pieces:** the parts inside the cutter that have a vertex of a selected face become the **new part**. Other parts inside (e.g. an ear the cutter's footprint also covers) stay with the rest. Loose bits under 0.5 mm³ (hair strands the cutter wall cut through) are dropped and counted in the log.
- **Preview:** nothing is drawn outside the part. It shows the joint (the faces the cut makes inside the part, where piece and rest will meet), drawn solid in a darker Selected surface colour so a see-through part shows it inside, and the **cut line**, the joint's edges where it meets the part's surfaces, drawn as thick light lines. It's worked out in the background 250 ms after the selection or a setting last changed (manifold `as_original`: the tool's faces on the piece).
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
- `modify.cutter` on a solid ball (2 mm depth): the piece is a skin at most about 2 mm under the surface, and `modify.joint` lies inside the ball within that skin.
- `modify.contours` on a slab with a post standing in it: the contours are exactly the four valleys round the post's foot (no ridges). A region grown from the slab's top at 91° climbs the post without contours, and with them takes all of the slab's top and none of the post.
- `modify.grow_by` on a ball's cap: ±2 mm moves its edge 2 mm along the surface (±0.35); a grow keeps the cap and a shrink stays inside it.
- `parts.parts` on a slab with a post standing in it: the post's sides and top are one part and the slab's top another. A ball is a single part.
- `picture.faces_in_outline` maps a face-index image and an outline to faces; candidates are sorted, and the near-whole one is dropped.
- GUI: Select by Parts selects the post of a slab-and-post part; Select by Picture, with a stand-in segmenter outlining the left half of the view, selects only visible faces on that side. The face-index render matches the faces the view's own pick finds.
- GUI: the Mesh pane starts folded with the status in its button; Select surface + click selects a region; the spread slider changes its size; Cut out selection gives two parts, a single undo restores one; refuses on an open mesh.

## 5. Open items
- A cut whose walls follow the surface normal rather than one direction (a curved cutter). The straight cutter guarantees the piece can be lifted out.
