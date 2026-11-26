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
- **Picture** (`gooslicer/picture.py`) [GOOSLICER]: a "segment anything" image model run with onnxruntime. Three models, chosen in Preferences → Picture selection (`picture/model`):
  - **MobileSAM** (default, included): ONNX export by Acly (MIT), `huggingface.co/Acly/MobileSAM` at commit `0d3b4033`. 45 MB, about 0.25 s a picture on the CPU.
  - **SAM 2.1 Small** and **SAM 2.1 Base+** (Meta, Apache-2.0; ONNX export by onnx-community, pinned to commits `a7df49d8` and `bab18593`): 184 MB and 327 MB, about 0.7 s and 1.3 s a picture on the CPU. They follow small features more closely.
  - The SAM 2.1 models are downloaded from Preferences into `$XDG_DATA_HOME/gooslicer/models/<name>/`. Each file is checked against its sha256, and a bad or partial download is never left under its real name. Until one is downloaded, MobileSAM is used.
- **Clicks** while picking in Picture mode:
  - A click outlines the thing under it.
  - Ctrl+click in the same view adds an include point to that outline. In a new view it adds another outline.
  - Right-click (instead of the part menu) adds an exclude point, so a strap can be separated from the buckle it runs into.
  - Points go to the model together (SAM's multi-point prompt). The clicked faces are always in and the excluded ones always out.
- **The picture:** on a click the part alone is drawn as it's seen, opaque in the model colour on the view's background, with no overlays, at twice the view's resolution.
  - It's **smooth-shaded** (per-vertex normals) even though the view is flat-shaded. Flat shading of a finely sculpted mesh sparkles triangle by triangle, and the models read that as texture: on `Lisa_head_D.stl` their outlines came out speckled, and a click on a strap selected half the face. With smooth shading every model outlines the strap as a clean band.
  - The picture is looked at in the background (busy cursor; clicks wait), once per view, part and placement.
  - MobileSAM's input is padded to a fixed 1024×1024 with its mean colour (what its own padding stands for), so a GPU compiles it once rather than for each window size. SAM 2.1 takes the picture stretched to 1024×1024.
  - The decoder takes the click points and returns 3–4 candidate outlines in about 0.03 s.
- **Candidates:**
  - Sorted by area. One covering at least 95 % of the part's own pixels is dropped when others remain.
  - The Outline slider picks Small (the smallest), Medium (the middle one, or the larger of two) or Large (the largest).
  - **Specks** are dropped: loose pieces of an outline (connected through the mesh) smaller than 2 % of it or 1 mm², unless they hold a clicked face. A strap seen in two places either side of a ring stays, being big.
- **From pixels to faces:** a second render of the part, at the same size, writes each face's index into the colour (gl_PrimitiveID, 24 bits, no multisampling). A face is selected when any of its pixels is inside the outline. Faces smaller than a pixel leave holes, which *fill holes* closes as for every selection. Only visible faces can be selected, so the cavity wall and the far side are never taken.
- **GPU** (Preferences → Picture selection → Run on, `picture/device`, default CPU):
  - Offered only when this onnxruntime build has a GPU provider (MIGraphX, ROCm or CUDA) and the GPU's own libraries load (`libamdhip64.so.7` and `libmigraphx_c.so.3` for MIGraphX). Otherwise the note says which is missing.
  - The dependency is `onnxruntime-migraphx` (x86-64). It's the same CPU code, with AMD's MIGraphX provider for ROCm 7 besides.
  - Only the encoder runs on the GPU. The decoder stays on the CPU: it takes 0.03 s there, and MIGraphX can't compile SAM 2.1's (its empty "boxes" input fails to parse).
  - MIGraphX compiles a model for the card the first time (SAM 2.1 Small: 70–150 s on an RX 9070 XT). The result is kept in `$XDG_CACHE_HOME/gooslicer/gpu` (about 290 MB for SAM 2.1 Small), and later sessions load it in about 3 s.
  - A GPU that fails to start falls back to the CPU, and the log says why (once).
  - **Measured** on an RX 9070 XT (gfx1201) with Ubuntu's ROCm 7 packages, per picture: SAM 2.1 Small 0.16 s (CPU 0.7 s); MobileSAM 0.28 s (CPU 0.25 s); SAM 2.1 Base+ 1.6 s (CPU 1.3 s; MIGraphX leaves most of it on the CPU). So the GPU pays off for SAM 2.1 Small.
  - **Flatpak** (`gooslicer/gpuhost.py`): the runtime has no ROCm. With `--device=all` (for `/dev/kfd`) and `--filesystem=host-os:ro` the host's libraries are under `/run/host/usr`. The app loads them itself, by full path and globally, before the provider starts, and only where the runtime has no library of that name (a missing dependency is looked up the same way). It doesn't set LD_LIBRARY_PATH, which would let the host's Qt and C++ libraries replace the runtime's.
    - The host's ROCm must be built for a glibc no newer than the runtime's. Ubuntu's ROCm 7 needs glibc 2.44, so the Flatpak moved from the KDE 6.11 runtime (freedesktop 25.08, glibc 2.42) to freedesktop 26.08 (2.44), with Qt from the PySide6 wheel.
    - **Measured** in the installed Flatpak (2026-11-25): HIP, HSA, comgr, hiprtc, MIGraphX and libnuma/libbz2 came from the host. SAM 2.1 Small took 0.17 s a picture on the GPU, and a click in a new view 1.0 s in the app, face mapping included.
- **Measured** in the app on the hollowed `Lisa_head_D.stl`, with the smooth picture and the Small outline:
  - A click on the cheek strap selects that strap: SAM 2.1 Base+ cleanest; MobileSAM also takes the buckle strap it joins; SAM 2.1 Small left specks, now dropped.
  - A click on the ring takes the ring's tab; a click on the hood edge takes the hood.
  - With MobileSAM, clicked between the eyes, Small was the hood round the eyes and Medium the whole hood with both ears. Clicked on an ear, Medium was exactly that ear.
- **Files:**
  - MobileSAM: `mobile_sam_image_encoder.onnx` (28,157,093 bytes, sha256 `580f5fb6…c749`) and `sam_mask_decoder_multi.onnx` (16,496,559 bytes, sha256 `8976b90a…2ffe`).
  - SAM 2.1: `vision_encoder.onnx(+_data)` and `prompt_encoder_mask_decoder.onnx(+_data)`, with sizes and hashes in `picture.MODELS`.
- **Where the app looks:** `$GOOSLICER_MODELS`, `/app/share/gooslicer/models` (the Flatpak downloads MobileSAM at build time, pinned by sha256), `models/` in the source tree (`scripts/fetch-models.sh`, git-ignored), then `$XDG_DATA_HOME/gooslicer/models` (downloads from Preferences). Each SAM 2.1 model sits in its own subfolder.
  - onnxruntime (MIT) is a dependency. Without it or any model's files, Picture is disabled and everything else works.
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
- `picture.drop_specks` keeps big pieces and clicked ones. `picture.download` keeps files that match their checksum and leaves nothing behind otherwise.
- Each installed model outlines a block on a dark ground (skipped for a model that isn't installed).
- Preferences → Picture selection lists the models with their download state, and offers the GPU only when it can start.
- GUI: Select by Parts selects the post of a slab-and-post part; Select by Picture, with a stand-in segmenter outlining the left half of the view, selects only visible faces on that side; a right-click leaves a patch out without looking at the picture again. The face-index render matches the faces the view's own pick finds.
- GUI: the Mesh pane starts folded with the status in its button; Select surface + click selects a region; the spread slider changes its size; Cut out selection gives two parts, a single undo restores one; refuses on an open mesh.

## 5. Open items
- A cut whose walls follow the surface normal rather than one direction (a curved cutter). The straight cutter guarantees the piece can be lifted out.
