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
- **Select by** (added 2026-11-25; combobox above Spread, `modify/selectBy`) sets what a click selects. Ctrl+click adds to the selection in any mode, and a selection can mix modes. Grow selection, Clear and the cut work the same for all of them. Each mode shows only its own controls.
  - **Surface** (default): the spread and contours above.
  - **Parts**: the click selects the whole part of the model it lands on (see *Parts* below). Part edges are drawn on the model while picking, in place of contours. **Part edges** slider (2°–30°, default 10°, `modify/partEdges`): how deep a valley must run along a border for it to stay a part edge. Lower gives more, smaller parts.
  - **Picture (AI)**: an image model outlines the thing you clicked in the 3D view, the way you see it (see *Picture* below). **Outline** slider, Small / Medium / Large (default Medium, `modify/outline`): which of the model's candidate outlines to use, from smallest to largest. Changing it re-applies the stored candidates; nothing is recomputed. Shown only when the model is installed; otherwise the entry is disabled, with a tooltip saying so.
  - **Parts (AI)** (added 2026-11-26): a click takes the part round it as found in 3D by PartField (see *Parts (AI)* below). The first click on a model works out its part features behind a modal window with Stop (about 10 s for 600k faces; kept for the model while it's the last one used). **Part size** slider: the nested parts round the last click, from the smallest to most of the model; the click picks the clearest, and the label says which of how many. Disabled, with a tooltip, unless PartField is installed (Preferences → Picture selection says how).
  - **Drawing**: drag on the model to paint a rough line round a part; the part inside is selected, with its edge found along the creases near the line (see *Drawing* below). **Brush** slider (0.5–10 mm, default 2 mm, `modify/brush`; Ctrl+scroll over the model also changes it), shown on the model as a ring round the cursor. **Use the AI for the edge** (`modify/drawAI`, default off; disabled without an image model): the image model's outline of the drawn region helps place the edge (see *Drawing: does the AI help?*). Each drag adds to the drawing until Clear; strokes needn't join up perfectly.
- **Drawing** (`gooslicer/drawsel.py`) [GOOSLICER]:
  - **Painting:** while dragging, the view picks the model every few pixels along the mouse's path, so a fast stroke stays continuous. Each picked face paints the faces within the brush radius of it along the surface (Dijkstra over face centres), so a brush never reaches through a thin part to its far side. The painted line shows on the model as it's drawn.
  - **Inside:** the unpainted surface falls apart along the drawing into pieces. The piece the drawing walls off is the smallest of those touching the paint (an ear inside a loop round its base; a band between two loops round a cylinder); the others are outside. Gaps in the drawing are closed by widening the paint, up to 3 times by half the brush more each time; the first width that walls off the most pieces is used (a gap in one of two loops round a band joins the band to the top; closing it gives three pieces, and the band is the smallest). Unpainted islands smaller than two brush dabs (gaps between dabs) count as paint. If nothing is walled off even then, the status bar says to draw all the way round, and nothing is selected.
  - **The edge** is the cheapest cut through the drawn region (the inside and the paint) between its core and the outside: a minimum s–t cut over faces (`scipy.sparse.csgraph.maximum_flow`). The line is drawn roughly round the part, so the true edge can be anywhere between the line and the core.
    - *Core:* the inside faces farthest from the paint along the surface (at least half the greatest distance) are always kept. The outside is always left out.
    - *Crossing* between two faces costs their shared edge's length × exp(−valley / 20°), with ridges counting 15 % as much (the Parts rule).
    - *Toward the line:* each inside face kept earns 0.05 /mm × its area. On a smooth surface that moves the edge out to the line; a crease is so cheap to cut along that it wins where there is one.
  - **With the AI:** the clicked view is drawn as for Picture and looked at once per view (in the background). The model is prompted with a box round the inside and the paint, as seen, include points deep inside the inside, and exclude points deep in the outside within that box. Of its outlines, the largest that covers 80 % of the core (as seen) and reaches outside the drawing by under 10 % of its area is used; none, and the AI is left out. (Matching the outline to the drawing instead just gave the rough line back.) Each face of the drawn region it covers is pulled to the inside, each it leaves out (and that the view sees) to the outside, by its area × 0.5 /mm, against the cut cost. So the AI helps place the edge where the geometry is ambiguous, but can't take anything outside the line or drop the core.
  - **Drawing: does the AI help?** Measured on four test shapes, seen from 60° above. A rough loop was dragged round each: *tight* is 8.5–10.5 mm out, *loose* 9.5–14.5 mm. IoU is against the true part (area-weighted). The times are for the whole stroke under the test harness; the AI's picture is looked at once per view.

    | Shape | Loop | Geometry only | MobileSAM | SAM 2.1 Small | SAM 2.1 Base+ | HQ-SAM 2 |
    |---|---|---|---|---|---|---|
    | Boss, sharp foot | tight / loose | 0.999 / 0.999 | 0.999 / 0.999 | 0.999 / 0.999 | 0.999 / 0.999 | 0.999 / 0.999 |
    | Boss, rough surface (0.12 mm noise) | tight / loose | 0.856 / 0.545 | 0.761 / 0.545 | 0.480 / 0.512 | 0.856 / 0.545 | 0.487 / 0.604 |
    | Smooth dome, no crease | tight / loose | 0.893 / 0.771 | 0.960 / 0.804 | 0.885 / 0.768 | 0.865 / 0.772 | 0.961 / 0.771 |
    | Boss inside a groove | tight / loose | 0.518 / 0.518 | 0.496 / 0.498 | 0.497 / 0.518 | 0.484 / 0.518 | 0.484 / 0.484 |
    | Time (s) | | 0.6–1.2 | 1.2–1.9 | 1.7–2.6 | 2.4–3.6 | 6.7–8.2 |

    - Where there's a crease at the part's foot, the geometry finds it exactly and the AI adds nothing.
    - Where the part blends in smoothly, with no crease, the AI helps: 0.89 → 0.96 with MobileSAM or HQ-SAM 2 on a tight loop. On a loose loop the gain is small.
    - On a rough or textured surface it can hurt: the noise breaks the outline, and the outlines that fit the drawing take in surface round the part (HQ-SAM 2: 0.86 → 0.49).
    - Boss inside a groove: both stop at the groove, the outermost crease inside the line, rather than at the boss's foot. That is what was drawn round. To get the boss alone, draw inside the groove.
    - Stronger pulls (2 /mm) and taking the smallest outline instead of the largest were also tried. Neither was better overall.
    - So the AI is an option, off by default. Its tooltip says when it helps.
- **Parts** (`gooslicer/parts.py`) [GOOSLICER]: splits the surface into the pieces a person would name (an ear, a strap, the mask round the eyes), by the *minima rule*: one shape meets another along a valley.
  1. *Patches*: one seed per 1.2 mm cell of each shell. Faces go to the nearest seed along the surface, where crossing a contour (the fold at 20°, cleaned as in *Contours*) costs 200× its length (a near-wall; a 10× wall let a seed on one side take a strip of the other). So patch borders lie on creases where there are creases.
  2. *Normalized cuts*: the patches form a graph. Two patches are joined by the length of their shared border, weighted by exp(−valley / 20°); ridges count 15 % as much as valleys. The graph is split in two along its Fiedler vector (`eigsh` from a fixed start vector, to 10⁻⁶, so the same model always splits the same way; from a random start, to 10⁻⁴, the band round a cylinder came out anywhere between IoU 0.56 and 0.98 from run to run) at the sweep point with the lowest normalized cut, and each half again, while the cut is under 0.1. This finds regions only weakly attached to the rest (an ear on its rim, a band between two strap edges) even where a crease has gaps.
  3. *Merge back*: normalized cuts also split smooth surfaces where nothing marks a border. So neighbouring parts are merged, the weakest first, while the length-weighted mean valley fold along their border is under the **Part edges** angle. Parts under 1 mm² always merge.
  - Measured on the hollowed `Lisa_head_D.stl` (618k triangles): about 1.4 s once per part placement, on top of the 1.1 s contour smoothing (cached and shared with contours). At 10° it gives 42 parts (56 at 6°, 29 at 15°); changing the angle only redoes the merge (0.02 s). The eye socket inside the mask, the ear and the hood edge come out as parts; some smooth skin is still split where the cut found no clear border, so Ctrl+click adds a neighbouring part.
- **Snapping a part's edge** (`drawsel.snap`, added 2026-11-27) [GOOSLICER]: Parts and Parts (AI) both find parts at a coarse scale, so their edges can miss a strap's or a mask's edge by a few millimetres. On `Lisa_head_D.stl` [OBSERVED]:
  - The mask's edges are low steps (about 0.3 mm). Folds measured over 0.5 mm, as contours and Parts use, barely see them; even at 0.1 mm they come out broken.
  - A click on the mask gave a blob round the eye that ignored the mask's outline, and a click on the ear bowl ran onto the hair. With Parts (AI), the click's part ran over the mask's outline.
  - So every part a click takes is snapped before it's shown:
    - a band 3 mm either side of its edge (narrowed for small parts, so at least 20 % of the part stays inside it) is cut again with Select by Drawing's min cut (`cut`), with crossing costs from folds measured at 0.2 mm, or the raw angle between the two faces where that's sharper (`snap_fold`: fold is a turn per mm of face spacing, so a boss's sharp foot next to large floor triangles read 24° against 51° one row up the wall, and the edge slipped a row up);
    - the part inside the band is always kept, so the edge moves at most 3 mm either way, and a larger size on the Part size slider never falls back to a smaller part (it did, before this rule: a boss with the ring round it snapped back to the boss's foot);
    - each face of the band is pulled toward its side of the old edge at 0.1/mm × its area, more than the cut's outward push (0.05/mm). So on a smooth surface the edge stays where it was, and it moves only where a step makes the cut cheaper.
  - Cached per click, per part size and per placement; the Part edges and Part size sliders don't redo a size already seen.
  - Measured on the test shapes (IoU of the click's part, before → after):

    | Shape | Parts | Parts (AI) |
    |---|---|---|
    | Band round a cylinder | 0.983 → 0.983 | 0.934 → 0.983 |
    | Boss, sharp foot | 1.000 → 1.000 | 0.991 → 1.000 |
    | Boss, rough surface | 0.082 → 0.069 | 0.948 → 0.945 |
    | Smooth dome | 0.917 → 0.944 | 0.901 → 0.936 |
    | Boss inside a groove | 1.000 → 1.000 | 0.941 → 1.000 |

  - On Lisa [OBSERVED]: a click on the mask gives the mask's eye opening, cut along its moulded edge; the ear bowl and the ear's point follow the ear's rim; Parts (AI)'s larger parts end on the mask's outline. 0.4–0.6 s per Parts click, up to 5 s for a Parts (AI) part covering a third of the head (the min cut over a long edge), behind a busy cursor.
- **Picture** (`gooslicer/picture.py`) [GOOSLICER]: a "segment anything" image model run with onnxruntime. Five models, chosen in Preferences → Picture selection (`picture/model`):
  - **MobileSAM** (default, included): ONNX export by Acly (MIT), `huggingface.co/Acly/MobileSAM` at commit `0d3b4033`. 45 MB, about 0.25 s a picture on the CPU.
  - **SAM 2.1 Small** and **SAM 2.1 Base+** (Meta, Apache-2.0; ONNX export by onnx-community, pinned to commits `a7df49d8` and `bab18593`): 184 MB and 327 MB, about 0.7 s and 1.3 s a picture on the CPU. They follow small features more closely.
  - **SAM 2.1 Large** and **HQ-SAM 2 Large** (Apache-2.0): SAM 2.1's largest model, and SysCV's HQ-SAM 2, which adds a high-quality output to it for fine structures (thin edges, straps, rims). HQ-SAM 2 keeps SAM 2.1 Large's image encoder frozen, so both use one 852 MB encoder, downloaded once into a shared folder. Their decoders are 17 MB each.
    - ONNX: the encoder and the SAM 2.1 decoder are vietanhdev's samexporter export, as republished unchanged by latolukasz (`huggingface.co/latolukasz/sam2.1-hiera-large-onnx`, commit `ccbb4af9`). The HQ decoder is latolukasz's export of SysCV's checkpoint (`…/sam2.1-hq-hiera-large-onnx`, commit `5d5e61bc`). [INFERRED from their model cards] that they're faithful exports: a single person's uploads, pinned by sha256 and checked here on test pictures, not against the original PyTorch models.
    - samexporter's form: the picture stretched to 1024×1024, ImageNet-normalised (`image`), giving `image_embed` and `high_res_feats_0/1`. The decoder takes points in 1024² pixels with float labels (a padding point, label −1, as for SAM 1), and returns 3 candidate outlines as 256² logits, which are scaled up to the picture.
  - Not added: EfficientSAM and EdgeSAM (faster, not more precise; EdgeSAM's licence is non-commercial) and Semantic-SAM (no ONNX export).
  - Tried, not added (2026-11-26): **Point-SAM** (zyc00/Point-SAM@25f4fd96, MIT; ViT-L weights yuchen0187/Point-SAM@fb2cd5cd, Apache-2.0, 1.24 GB, 311 M parameters), a SAM for point clouds that works on the model itself rather than pictures.
    - *Running it:* its CUDA add-ons were replaced by plain PyTorch (point gather, farthest-point sampling, LayerNorm). On the CPU it loads in 3 s, encodes in 2 s (at 10k–60k points sampled from the surface), and answers a click in 0.1–0.5 s.
    - *On the test shapes:* points coloured by surface direction (better than plain grey). The IoU of its best outline from one click:

      | Shape | Point-SAM | GooSlicer's best |
      |---|---|---|
      | Band round a cylinder | 0.84–0.87 | about 0.88 (all the way round, 9 s or more) |
      | Boss, sharp foot | ≤ 0.46 | 0.999 (Drawing or Parts) |
      | Smooth dome | ≤ 0.84 | 0.96 (Drawing + AI) |

      With 4–5 corrective clicks the boss reaches 0.94.
    - *On `Lisa_head_D.stl`:* a click on the ear cup selected a patch of it, or scattered bits over the head; one on a spike, bits of many spikes. Its 1,024 patches are too coarse for a miniature's details. It was trained on PartNet and ScanNet objects.
    - So it isn't added. It could only help with large wrap-round parts, and *All the way round* already handles those.
  - **PartField**: see *Parts (AI)*, the next entry. Point-SAM and PartField were both tried (2026-11-26); PartField was the better.
- **Parts (AI)** (`gooslicer/partfeat.py`, `scripts/convert-partfield.py`) [GOOSLICER]:
  - **The model:** PartField (nv-tlabs/PartField@373025db), with the Objaverse weights (mikaelaangel/partfield-ckpt@90b9b1e0, sha256 463efc8a…7ee4). It is under the NVIDIA License (non-commercial research and education only), so GooSlicer neither ships nor downloads it. `scripts/convert-partfield.sh [--flatpak]` makes it on the user's machine and installs it in the user model folder as `partfield/partfield.onnx` (505 MB), with NVIDIA's LICENSE and a NOTICE:
    - it fetches NVIDIA's code and checkpoint, pinned by commit and sha256;
    - it makes a throwaway Python environment with PyTorch (CPU);
    - it exports the network and checks the export against PyTorch before installing it.
  - **Changes made for the CPU and ONNX** (the weights and network are unchanged):
    - `device='cuda'` → `'cpu'`;
    - `torch_scatter.scatter_mean` → sum ÷ count with `scatter_add`;
    - the voxelisation's `scatter_(reduce='add')` → `scatter_add`;
    - the 3D `grid_sample` in `trilinear_devoxelize` → eight gathers, the same arithmetic (within 1e-6);
    - the exported planes match PyTorch's to cosine 1.000000.
  - **Features:**
    - The model's own vertices are turned from Z-up to Y-up (PartField was trained on Y-up Objaverse; Y-up was better on every test shape), centred, and scaled to a longest side of 1.8.
    - 100k points are sampled by area and run through the network: 4.4 s on the CPU, whatever the model's size.
    - Each face's feature is the mean, over 3 random points in it, of the bilinear lookups in the three part planes (`read_planes`, as `grid_sample(align_corners=True, padding_mode='border')`). It is cut to 64 numbers (the top singular directions of 20k faces' features, uncentred, so cosines are kept) and stored at half precision, unit length.
    - Lisa: 618k faces in 10.6 s, 75 MB, peak memory 2.8 GB. Kept per mesh: moving or turning the part doesn't redo them.
  - **The parts round a click:**
    - At similarity τ, the part is the faces reachable from the clicked one through faces whose feature has cosine ≥ τ with its feature.
    - For all τ at once (`reach`): each face's τ is the best, over paths from the click, of the lowest similarity along the path. That is the path through a maximum spanning tree, worked out by pointer jumping.
    - The parts offered are the τ (0.10–0.95, steps of 0.01) where the part's area changes least within ±0.06 (local minima of that change), between 0.05 % and 60 % of the surface, one per distinct size, smallest first.
    - The click takes the one that changes least; of equals, the one that keeps its size over the widest range of τ.
    - 0.03–0.24 s a click.
  - **Measured** (IoU of the click's own choice; the IoU of the best of its parts in brackets):

    | Shape | Parts (AI) | Before |
    |---|---|---|
    | Band round a cylinder | 0.93 (0.95) | about 0.88 (all the way round) |
    | Boss, sharp foot | 0.99 | 0.999 (Drawing or Parts) |
    | Boss, rough surface | 0.95 | 0.86 at best |
    | Smooth dome | 0.90 (0.92) | 0.96 (Drawing + AI) |
    | Boss inside a groove | 0.94 | 0.52 |

  - **On `Lisa_head_D.stl`:**
    - Good: a spike of the crown gives that spike (the row of spikes is a size up); the side flap of the hood comes out cleanly.
    - Not good: the ear cup gives only part of the ring round it; a click on top of the head picks the whole hood shell (smaller parts are on the slider).
  - The downloadable models are downloaded from Preferences into `$XDG_DATA_HOME/gooslicer/models/<name>/`. Each file is checked against its sha256, and a bad or partial download is never left under its real name. Until one is downloaded, MobileSAM is used.
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
- **All the way round** (Preferences → Picture selection → Look all the way round, `picture/turn`, default on; Views is then unused) [GOOSLICER]: for parts that wrap round the model (a strap, a band, a collar). Accuracy comes before speed: a click can take minutes, behind a progress window.
  - **Where from:** two rings of pictures round the middle of the part's bounding box, 30° apart, one above and one below the middle. Each ring sits at the clicked view's own pitch, kept between 20° and 45°, mirrored for the other ring. The part's bounding sphere fills 80 % of each picture. Order: straight below the click, then ±30°, ±60°, … 180°, each above then below. That's 23 pictures besides the clicked one.
  - **Carried round:** each picture is prompted by the selection so far, using only faces that some picture already saw fairly head-on (votes ≥ 0.5). Its outline that agrees best with that selection (area-weighted overlap ≥ 0.3, on faces seen before) votes. A picture with almost none of the selection in sight is skipped.
  - **The vote** is weighted by how directly each picture sees each face (the cosine between the face's normal and the direction to the camera), so faces seen edge-on count for little. A face is kept when its in-votes are at least half its votes. Specks, clicked and excluded faces as for one view.
  - **While it runs:** a modal window ("Looking at … from all round: 7 of 23, about 1 min left"; the first time it says how many pictures) blocks the app, with a busy cursor. **Stop** ends it and keeps the clicked view's outline. Ctrl+click, right-click and the Outline slider reuse the pictures (kept at half precision, about 200 MB for a SAM 2.1 model; only the latest outline keeps them), so they only decode again: seconds.
  - **Measured** on a test cylinder (radius 15 mm) with a raised band round its middle, clicked on the band, Small outline:

    | Model | One view | All the way round (24 pictures) |
    |---|---|---|
    | MobileSAM (GPU) | 35 % of the band | 100 %, plus 13 % of the band's area outside it, 9 s |
    | SAM 2.1 Base+ (CPU) | 36 % | 100 %, 10 % outside, 38 s |
    | HQ-SAM 2 (CPU) | 37 % | 98 %, 6 % outside, 108 s |
    | SAM 2.1 Small (GPU) | 18 % | 49 %, 2 % outside, 8 s: it outlines short pieces of a thin band |

    - One ring at the clicked pitch reached only 79 % (MobileSAM): the band's underside faces no picture from above. 5 More views reached 52 %.
    - On `Lisa_head_D.stl`'s cheek strap, which ends under the hood rather than wrapping round, it changed little. It also carries a wrong start round: a Medium outline that already took half the cylinder spread further. Pick the outline size that's right in the clicked view first.
    - Filling holes used to fill the smaller side of a ring selected all the way round (the whole top of the cylinder); now only patches smaller than the selection are holes (*Selection used*, below).
- **From pixels to faces:** a second render of the part, at the same size, writes each face's index into the colour (gl_PrimitiveID, 24 bits, no multisampling). A face is selected when any of its pixels is inside the outline. Faces smaller than a pixel leave holes, which *fill holes* closes as for every selection. Only faces seen in a view can be selected, so the cavity wall is never taken, and the far side only with More views.
- **More views** (Preferences → Picture selection → Views, `picture/views`, 1–9, default 3) [GOOSLICER]: the outline is checked against, and extended by, the model's outlines from other angles. The far side of a strap or an ear can then be selected, and one view's mistakes are outvoted.
  - **Where from:** after the clicked view's candidates are known, up to 8 more pictures of the part are drawn, all aimed at the centre of the largest candidate. The distance is where that candidate's bounding sphere fills about half the picture, but no farther than the view's own distance. Directions, in this order: the view's own (nearer), turned 45° either way round the vertical, tilted 35° up and down, turned 90° either way, then the 45° turns tilted up. Same size, shading, clipping and background as the clicked view.
  - **Prompts in each view, per candidate:** include points at the clicked faces it sees, plus up to 3 points deep inside the candidate's pixels there (by distance to its edge, spread apart); exclude points at the excluded faces it sees.
  - **Which outline:** of the outlines the model returns, the one that agrees best with the candidate on faces both views see (area-weighted overlap). A view is skipped for a candidate when it sees under 10 % of the candidate's area, or when the best overlap is under 0.3 (it outlined something else).
  - **The vote:** a face is selected when it's inside the outline in at least half the used views that see it, the clicked one included. Faces seen in no view aren't selected; fill holes closes small gaps as before. Specks are then dropped, and clicked and excluded faces forced, as for one view.
  - Only the candidate the Outline slider is on is refined; moving the slider refines the new one when it's needed (decoding only by then, so well under a second). The clicked view's outline shows straight away, and the refined one replaces it when ready (busy cursor meanwhile).
  - The vote works on the views' face pictures cut to about 800 px on the long side (every k-th pixel), and the model's outlines are scaled straight to that size: they're 256² before scaling, so nothing finer is lost. Points deep inside are found at half that again.
  - Ctrl+click and right-click on the same outline reuse its views' pictures: only the decoder runs again. Only the latest outline keeps its views (to save memory).
  - Cost: one picture per view.
  - **Measured** on the hollowed `Lisa_head_D.stl` (cheek strap, Small outline; under Xvfb, so the pictures are drawn in software):
    - Per click: MobileSAM on the GPU 0.7 s with 1 view and 3.7 s with 5; SAM 2.1 Small on the GPU 0.6 s and 3.7 s; HQ-SAM 2 on the CPU 6 s and 25 s. Moving the Outline slider to an unrefined candidate: 1–2.5 s.
    - For the strap, 5 views changed little (it's nearly all in sight from the clicked view): the band's ends came out a little cleaner. HQ-SAM 2 gave the cleanest band, from the ring down to the jaw, with 1 view. The gain from more views is on things that wrap round: on a hollow box (GUI test), faces round the side joined, and the cavity wall stayed out.
    - Hence the default of 3 views: the clicked one, nearer, and turned 45°.
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
  - SAM 2.1 Small and Base+: `vision_encoder.onnx(+_data)` and `prompt_encoder_mask_decoder.onnx(+_data)`, with sizes and hashes in `picture.MODELS`.
  - SAM 2.1 Large and HQ-SAM 2: `encoder.onnx`, with `decoder.onnx` or `decoder_hq.onnx`, in `sam2.1-hiera-large/`.
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
- **Selection used:** the grown regions plus the holes they enclose (unselected patches surrounded by the selection, e.g. a bump in the hair). Shells not touching the selection, such as a hollow's inside wall, are never added. A hole is smaller than the selection round it: a band selected all the way round a cylinder leaves two patches, its ends, and neither is a hole (filling the smaller, as before, took the whole top).
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
- `modify.fill_holes` fills holes punched in a cap and nothing else (not the hollow's inside), and leaves the ends of a ring round a cylinder alone.
- `modify.cutter` and `modify.cut` on a hollow ball, with a 40° cap: two watertight solids adding up to the ball. The piece is a curved cap of wall with no floor, as wide as the outline, its lowest points where the cutter's walls meet the inside of the wall. A fit gap shrinks the piece by the gap and leaves the hole as it was.
- On a U shape cut across both arms with one arm selected, only that arm becomes the piece.
- `mesh.repair` keeps a cavity facing in (and turns in one facing out), with a solid core inside it facing out.
- `modify.cutter` on a solid ball (2 mm depth): the piece is a skin at most about 2 mm under the surface, and `modify.joint` lies inside the ball within that skin.
- `modify.contours` on a slab with a post standing in it: the contours are exactly the four valleys round the post's foot (no ridges). A region grown from the slab's top at 91° climbs the post without contours, and with them takes all of the slab's top and none of the post.
- `modify.grow_by` on a ball's cap: ±2 mm moves its edge 2 mm along the surface (±0.35); a grow keeps the cap and a shrink stays inside it.
- `parts.parts` on a slab with a post standing in it: the post's sides and top are one part and the slab's top another. A ball is a single part.
- `picture.faces_in_outline` maps a face-index image and an outline to faces; candidates are sorted, and the near-whole one is dropped.
- `picture.propagate` (all the way round): an outline is carried from view to view, faces it never reaches stay out, and Stop raises `Cancelled`. `turn_layout` gives two rings, nearest first.
- `picture.refine` (More views): a face only another view sees, inside its outline, is added; a view that outlined something else is ignored; two views outvote the clicked one on a face it got wrong.
- `picture.drop_specks` keeps big pieces and clicked ones. `picture.download` keeps files that match their checksum and leaves nothing behind otherwise.
- Each installed model outlines a block on a dark ground (skipped for a model that isn't installed).
- Preferences → Picture selection lists the models with their download state, and offers the GPU only when it can start.
- GUI: all the way round, with a stand-in that outlines everything, reaches all four sides of a hollow box and never its cavity wall, behind a modal window that closes after; Stop keeps the clicked view's outline.
- GUI: Select by Parts selects the post of a slab-and-post part; Select by Picture, with a stand-in segmenter outlining the left half of the view, selects only visible faces on that side; a right-click leaves a patch out without looking at the picture again. The face-index render matches the faces the view's own pick finds.
- Drawing: two rough loops round a band on a cylinder select the band (≥ 97 %, under 10 % else); a gap in one loop is closed; an open line selects nothing; the brush paints along the surface, never through a thin plate; the AI's answer moves the edge only within the drawing and never drops the core. `Segmenter.outlines` takes a box prompt.
- Snapping: a part whose edge wanders 0–1.5 mm off a raised band's foot is snapped onto it (IoU 0.64 → 0.98); a part's edge on a smooth surface doesn't move. GUI: a larger Part size keeps the boss and the ring round it; a boss's snapped edge stays on its foot.
- Parts (AI): reading the planes is the bilinear, border-clamped lookup; Z-up becomes Y-up; `reach` ≥ τ is exactly the τ-component holding the click (against a brute-force check); the parts are nested, smallest first, each step offered. With PartField installed, a boss on a block is found (IoU > 0.95).
- GUI: Select by Parts (AI), with a stand-in network: the first click works out the features behind a modal window, then selects the boss; Part size steps to a larger part; a second click needs no window; Ctrl+click adds.
- GUI: Select by Drawing: a rough loop dragged round a boss on a block selects the boss, cut at its foot; the brush ring follows the cursor; the Brush slider and Ctrl+scroll change it; an open line selects nothing and says so; Clear clears the drawing.
- GUI: the Mesh pane starts folded with the status in its button; Select surface + click selects a region; the spread slider changes its size; Cut out selection gives two parts, a single undo restores one; refuses on an open mesh.

## 5. Open items
- A cut whose walls follow the surface normal rather than one direction (a curved cutter). The straight cutter guarantees the piece can be lifted out.
