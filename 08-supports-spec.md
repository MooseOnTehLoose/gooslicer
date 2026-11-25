# 08 — Supports (GooSlicer P6)

Tags as in 06/07: **[OBSERVED]** is static RE of SatelLite, **[SAMPLE]** is measured from SatelLite output (`samples/arc.goo`), **[INFERRED]** is a guess backed by evidence, **[GOOSLICER]** is a GooSlicer design choice where SatelLite's behaviour was not recovered.

Code: `gooslicer/supports.py` (engine), `gooslicer/support_defaults.py` (template loader, 06 §1), `gooslicer_qt/supporting.py` (dialog, cache, undo), `gooslicer_qt/main.py` (Supports menu, edit mode, slice integration), `gooslicer_qt/viewport.py` (support display and picking).
Tests: `tests/test_supports.py`, `tests/test_gui_smoke.py`.

Status: the **EVO** structure (script type 7), the **Tree** structure (script type 0, a GooSlicer design over SatelLite's parameters, §8) and the raft (script type 9) are implemented.

## 1. Parameters
- `SupportParams` uses the `.satsup` key names (06 §1). Built-in defaults equal **EVO_30-60mm** **[OBSERVED decode]**; `test_defaults_equal_p4_spec` pins them. `SupportParams.tree_defaults()` equals **Tree_Medium** (§8).
- The dialog offers the two built-in sets (EVO, tree) and every EVO and Tree template found. The height recommendation only picks EVO templates, as Tree names carry no height bucket.
- With SatelLite installed, the dialog loads its templates and preselects the one SatelLite would recommend from the tallest part's height (06 §1, "Recommended template"), including the "may cause printing failure" note.
- Template floats are rounded to 4 places (they are stored as float32, so 0.32 reads as 0.31999…).
- `lift` (`LiftHeight`, 5 mm) raises the model; the part's `elevation` holds it.

## 2. Contact detection [GOOSLICER rules, SatelLite parameter names]
1. **Overhangs:** a face needs support when its tilt from horizontal is below `criticalAngle`: `-n_z > cos(criticalAngle)`. Those faces are sampled densely, then thinned by greedy Poisson-disk selection at `anchorDis`, lowest points first.
   - **Border inset** (2026-11-24): samples nearer the edge of their overhang region than `borderOffset` are dropped. A region is a connected set of overhang faces; its edge is where it meets other faces, or an open mesh edge. On a region narrower than that, the limit is a quarter of the region's depth (the largest edge distance of its samples), so narrow strips keep contacts near their middle.
   - The quarter is fitted to SatelLite's arc project **[SAMPLE, 10 §5]**, whose 430 overhang points (anchorDis 2, borderOffset 0.5) have edge distances binned at 0 / 0.1 / 0.3 / 0.45 / 0.55 / 0.7 / 1 mm as 124 / 164 / 66 / 21 / 23 / 22 / 10. With 0.1 mm sampling this rule gives 421 points binned 161 / 144 / 52 / 20 / 21 / 16 / 7. Without it, there are 474 points and 287 sit within 0.1 mm of an edge. A full 0.5 mm inset gives 317 and too few in the middle bins. The rule itself is **[GOOSLICER]**.
   - On arc (EVO defaults) this took auto-support from 315 pillars, 106 rejected to 304 pillars, 94 rejected, still with no islands.
2. **Lowest points** (`bReinforceLowestPts`): vertices with no lower neighbour and a downward vertex normal (`n_z ≤ −0.3`), thinned at `lowestPtsInterval`.
3. **Dedupe** at half the smaller spacing. Priority: manual, then lowest/island, then overhang.
4. **Islands** (§5) add contacts after the first build.

Not used yet: `anchorDisBorder`. A row of contacts along the inset line at that spacing was tried and dropped: on arc it left only 2 of 415 points more than 0.55 mm in, against SatelLite's 55 of 430. The arc project has `anchorDisBorder` equal to `anchorDis`, so it cannot show what the setting does.

## 3. Structure (EVO)
Per contact:
- **Tip:** a frustum from `topConnect.topWidth` at the model (sunk `distanceInPart` along the normal) to `bottomWidth` at the tip end, `tipLength` long. Optional ball of `topBallDiameter` at the contact, and a joint sphere at the tip end.
- **Tip direction:** the outward normal, tilted no further than `max_tip_angle` from vertical (default 30°), keeping the normal's heading. This is SatelLite's rule, measured on a SatelLite project **[SAMPLE, 10 §5]**; the parameter's name is GooSlicer's. Until 2026-11-24 GooSlicer blended the normal with straight down and capped at 45° **[GOOSLICER]**, which halved most tips' angle.
- **Collision avoidance:** the tip ray must stay clear of the model for `tipLength`. Candidates are tried in order: the capped normal, halfway to vertical (also capped), straight down **[GOOSLICER]**. A tip end found inside the model (rays can slip through seams) is rejected.
- **Ray queries** (`raycast.py`, 2026-11-24) [GOOSLICER]: trimesh's ray tester without Embree tests nearly every face for a long ray and held ~9 GB for hundreds of rays on a 300k-face model. A `Caster`, built once per mesh, bins faces on XY grids by size level for vertical rays and keeps k-d trees of face centres for short segments, querying in chunks so memory stays bounded.
- **Pillar:** vertical from the tip end, `cluster.topWidth` → `bottomWidth`, `polyEdgeNum` sides.
  - Nothing below → lands on the raft (or the plate with no raft).
  - Upward-facing model surface below and `EnableInnerSupport` → lands on the model through a cone of `botConnect.topWidth`, sunk `botConnect.distanceInPart`.
  - Downward- or side-facing surface below → contact rejected.
- **Tight gaps** (`_short_link`): when the gap to an upward surface below is under `tipLength + min_on_model_length` (1 mm, GooSlicer), a vertical double cone joins the two surfaces instead.
- **Plate connection** (`platConnect`): an optional cone foot of `radius` × `height`. Off in all shipped EVO templates.

## 4. Bracing and raft
- **Bracing:** X-braces between pillars that reach the raft, along a minimum spanning tree of their Delaunay neighbours no further apart than `diamondWidth`. Rods are `edgeWidth` thick at `diamondAngle`, stacked from 0.5 mm above the raft to 0.3 mm under the lower tip end. Rod size and angle **[SAMPLE]**: arc.goo shows 0.8 mm rods at 60° from z ≈ 1.0 mm. The MST topology is **[INFERRED]**.
- **Raft outline:** the convex hull of pillar feet grown by `platOffset` + pillar radius **[SAMPLE]**: with arc.goo's own feet this is within 1% of its raft.
- **Raft body:** `platHeight` thick, with the edge chamfered at `slotAngle`.
- **Raft holes:** a hex grid at `holeInterval`, radius `holeRadius`, `holeEdgeNum` sides. Holes are kept inside the chamfered bottom and clear of pillar feet **[SAMPLE]**: in arc.goo no pillar covers more than 4% of a hole.
- Each piece is a closed `manifold3d` solid. They are joined into one mesh as separate shells, not unioned (`join_solids`): slicing (manifold sections, non-zero fill), the island check and the 3D view all treat overlapping closed shells as their union. Unioning a typical support set took 1–2 s on every rebuild; joining takes a few ms (2026-11-24).

## 5. Unsupported-island check [GOOSLICER]
- The model plus supports are sliced at the layer height and rasterised at 0.1 mm.
- The first layer rests on the plate and is **grounded**. A connected region of a later layer is grounded when it shares a pixel with grounded material of the layer below.
- Ungrounded regions are followed upward (an island keeps its identity while it overlaps itself) and recorded with their first height, a point inside, their largest area and how far they hang.
- **Reported** only when an island hangs more than 0.15 mm **or** reaches 5 mm² (`ISLAND_MAX_HANG`, `ISLAND_MAX_AREA`).
  - Why: the rasteriser splits a nearly flat underside between support tips into specks that join the supported area one or two layers later. On the auto-supported arc at 0.05 mm layers the old "touches the layer below" rule found 34 such specks, all hanging ≤ 0.1 mm and ≤ 0.4 mm². At 0.03 mm layers one strip of 1.05 mm² hangs a single layer.
  - Every real island of the unsupported arc hangs ≥ 0.3 mm (up to 53 mm, 8–255 mm²), so it is still reported. `test_island_thresholds` pins both limits.
- `auto_support` checks at 0.1 mm and runs up to 3 passes: cast down from each island to find its underside, add an `island` contact, rebuild. The auto-supported arc is clean at 0.1, 0.05 and 0.03 mm layers.
- **Before slicing** ("Slice…" and "Slice and send"), after the trapped-resin check, the check runs in the background at the print layer height (≈ 5 s for arc; the slice actions are disabled meanwhile). Islands bring up a warning listing height, area and hang; the user can slice anyway. A failed check is logged and does not block slicing.
- Supports → Detect islands runs it on demand.
- **Shown in red** [GOOSLICER]: each island also records its footprint (the pixels it covers on every layer it hangs). `island_faces` marks the downward-facing faces whose centre lies over that footprint (two pixels of slack) between its first layer and where it joins grounded material; an island too small to catch one gets the faces nearest its point. The 3D view draws them red over the model until a part, its mesh or its supports change. The check from Models → Supports, the menu and before slicing all show it. Each island also gets a white guide line, level with its first layer, running from its point straight away from the plate centre to the edge of the build area (toward the front, −Y, for an island at the centre). It is drawn solid where visible and dimmed where the model hides it, so a small island can be found from any side. Just past the line's outer end, outside the build plate, a label names the flaw and its number, "Unsupported island 14" (sealed cavities get "Sealed cavity 1", 07). Flaws are numbered per kind, from 1, in the order the check reports them; a new check numbers afresh; lines of the same kind whose labels would overlap share one label, and an unselected label that would cover another is left out. Clicking a label selects its line(s): they turn red and are drawn over everything, so the model never hides them, and the label gets a red outline and is always shown. Clicking it again deselects. A new check clears the selection of its own kind only. Labels that share a spot list their numbers ("Unsupported islands 3, 4").
- **Flaws tab** [GOOSLICER], after Models: every detected flaw, title on the first line and details on the second (islands: height, area, how far it hangs; cavities: volume, lowest point). Each flaw has its own red surface (`island_faces_each`, `cavity_faces_each`). Clicking a row selects it exactly as clicking its label does, and the two stay in step. The red × at the row's end dismisses a false positive: its red surface, line and label go. Its point is remembered, so the next check, Fix islands, Seal cavity and the warnings before slicing leave out anything within 1 mm of it, until a part's mesh or placement changes. Supports changing doesn't reset it.
- **Fix islands** (Models → Supports) [GOOSLICER] runs the check, then supports every island: each is matched to the part whose underside lies straight below its point (`island_contact`, nearest such part). A part that already has supports gets an `island` contact there, all in one undo step. A part with none gets a full set of the selected support type (`auto_support`, which adds island contacts itself), because lifting a bare part onto one support would leave its base hanging. Islands not over any part (inside support geometry) are reported for manual editing. The check then runs again and reports what remains.
- In the 3D view, supports are drawn blue and half transparent (front faces only, no depth writes; blending leaves the framebuffer's alpha at 1 so the window behind never shows through) after the model, so the model and the island highlight stay visible through them. The build plate is drawn as its grid lines only.

## 6. Editing and storage
- `SupportPlan` stores params and contacts in the **part's local frame**, so supports follow moves, rotations and scaling. Geometry is rebuilt from the plan in a background cache.
- Supports → Edit (S): click on the model to add a manual contact (`contact_below` finds the underside); Ctrl+click on a support to remove it (`nearest_pillar`, 1.5 mm pick radius). Drag a support to move its contact: the press waits for the release, so a press that moves more than 4 px is a drag. An orange ring shows the drop point on the model, and the contact moves there, as a manual contact, only when it is dropped on the same part. A drag that doesn't start on a support does nothing. On imported SatelLite supports, the contact is found as for removal (the nearest contact above the press, within 2.5 mm). Every change is one undo step.
- Clear supports removes the plan and drops the part back to the plate.
- Slicing and "Slice and send" slice each part's support mesh together with the model; overlaps fill as one (non-zero rule).

## 7. Reference job (arc, EVO_30-60mm defaults)
| | SatelLite `arc.goo` [SAMPLE] | GooSlicer |
|---|---|---|
| Pillars to the raft | 156 | 151 |
| Pillars on the model | — | 189 |
| Contacts (overhang / lowest / island) | — | 295 / 122 / 19 |
| Rejected contacts | — | 96 |
| Raft top area (mm²) | ≈ 860 (layers 0–5) | 741 |
| Islands left | 0 | 0 |
| Watertight | — | yes |
| Time (16 cores) | — | 11.5 s |

The raft is smaller because GooSlicer's feet cluster more tightly; the outline rule itself matches (§4). `test_arc_reference_density` accepts 110–200 raft pillars.

## 8. Tree supports [names OBSERVED, layout GOOSLICER]
SatelLite's tree builder was not decompiled. GooSlicer uses its own layout driven by the Tree template values.

### Tree template values (`Tree_Small / Medium / Large`) [OBSERVED decode]
| Key | Small | Medium | Large | GooSlicer field |
|---|---|---|---|---|
| `area.criticalAngle` | 45 | 50 | 55 | `critical_angle` |
| `anchorDis` / `anchorDisBorder` | 2.5 / 2.5 | 2.3 / 2 | 2.25 / 2 | `anchor_dis` / `anchor_dis_border` |
| `lowestPtsInterval` | 1.5 | 1.5 | 2 | `lowest_interval` |
| `topConnect.topWidth` / `tipLength` / `distanceInPart` | 0.4 / 2 / 0.1 | 0.45 / 2 / 0.2 | 0.48 / 2 / 0.2 | `tip_top_width` / `tip_length` / `tip_in_part` |
| `topConnect.topBall` | no | no | no | `tip_ball` |
| `botConnect.topWidth` / `tipLength` / `distanceInPart` | 0.5 / 3 / 0.1 | same | same | `bot_top_width` / `bot_tip_length` / `bot_in_part` |
| `tree.trunk.topWidth` / `botWidth` | 1.2 / 1.2 | same | same | `pillar_top_width` / `pillar_bottom_width` |
| `tree.brunch.topWidth` / `botWidth` | 1.2 / 1.2 | same | same | `tip_bottom_width` / `branch_width` |
| `tree.trunk.maxBranchAngle` / `maxHeight` / `heightType` | 45 / 5 / 0 | same | same | `max_branch_angle` / `max_branch_height` / `tree_height_type` |
| `tree.polyEdgeNum` | 12 | 12 | 12 | `poly_edges` |
| `cross.hasCrossConnect` / `startHeight` / `corssInterval` / `barWidth` / `barAngle` | yes / 3 / 5 / 0.3 / 45 | same | same | `cross*` |
| raft `platOffset` (other raft keys as EVO) | 2 | 3 | 3 | `raft_offset` |

Unused, roles **[UNKNOWN]**: `tree.brunch.maxBrunchNum` (1), `tree.ribsNum` (2), `tree.thickness` (0), `cross.crossType` (1), `cross.connectType` (0), `locateSurfaceType` (0), the `filter.*` keys, and the `breakPoint.*` keys (`hasBreakPoint` is off).

### Layout
1. **Tips:** contacts, tip direction, collision tests and the straight-drop test are the EVO ones (§2–3). The tip widens from `topWidth` to the branch width (`tip_bottom_width` = `brunch.topWidth`; the Tree templates have no tip `bottomWidth`). Contacts whose drop lands on the model keep an EVO-style pillar, with a `botConnect.tipLength` cone.
2. **Merging:** tip ends that drop to the raft are merged pairwise, highest meeting point first (a priority queue).
   - Two nodes meet at the highest point both can reach going down at the branch slope. When the higher node can reach the lower one's column above it, they join at the lower node. Merged joints can merge again, so trees grow several levels.
   - The slope and height come from `trunk.heightType` **[DECOMPILED: EleTriBuilder.dll `FUN_18001e680`; the settings record is `SUPPORTVARIABLESBAR`, registered in EleTriBuilderBase `SUPPORTVARIABLESBAR::initialze`, where `heightType` is offset 0x104, `maxHeight` 0x100 and `maxBranchAngle` 0x108]**. The function places a group's joint below the mean XY of its points, at the lowest height that each point reaches with a cone:
     - type 0 (every template): 45° from horizontal (`z − r`), no height limit;
     - type 1: 30° from horizontal (`z − r·tan 30°`), then capped at `maxHeight` above the raft. Only here is `maxHeight` read;
     - type 2: `maxBranchAngle` from horizontal (`z − r·tan a`).
     A joint below the raft goes to the raft.
   - So with the templates' type 0, `maxHeight` and `maxBranchAngle` have no effect. GooSlicer's former reading (a 5 mm limit on each branch's drop) was wrong and has been removed. On arc with Tree_Medium the tree now has 28 trunks instead of 42; the longest branch drops 42.8 mm.
   - GooSlicer merges pairs, not whole groups, so its joints are not exactly SatelLite's **[GOOSLICER]**.
   - A merge is also refused when a branch passes through the model or comes closer than branch radius + 0.1 mm to it, when the joint is under 1 mm above the raft, or when the joint's trunk cannot drop to the raft with trunk radius + 0.1 mm clearance.
3. **Trunks:** every remaining node drops vertically to the raft as a `trunk` pillar, with an optional plate connection.
4. **Cross braces:** an X of `barWidth` rods at `barAngle` between neighbouring trunks (MST of Delaunay neighbours up to 10 mm apart, GooSlicer), the first at `startHeight` above the raft, then every `crossInterval`, up to 0.3 mm under the lower trunk top.
5. **Raft:** as §4, from the trunk feet, with the template's `platOffset`.
6. Each contact's `Pillar.path` (tip end → joints → foot) is kept for picking, so Ctrl+click works on branches.

### Results (Tree Medium values)
| | tee (12 × 4 mm slab on a post) | arc |
|---|---|---|
| Contacts to the raft → trunks | 16 → 5 | 139 → 51 |
| Contacts on the model | 0 | 159 |
| Joints per path | 0–4 | 0–11 |
| Braces (rods) | 0 (trunks too short) | 242 |
| Islands at 0.1 / 0.05 mm layers | 0 / 0 | 0 / 0 |
| Watertight | yes | yes |
| Time | 0.1 s | 16.5 s |
| Largest branch angle / drop | — | 45.0° / 4.9 mm |

Overlap with the model on arc is 68 mm³ (EVO: 19 mm³). Branches and joints account for 0.16 mm³. The rest is tips (48 mm³: 0.45 → 1.2 mm over 2 mm, sunk 0.2 mm), on-model cones (24 mm³) and trunks. These follow from the template values.

## 9. Auto Position [GOOSLICER]
The Auto Position button (Models → Properties, below Lay flat) turns the selected parts to the angle that needs the fewest supports. It runs in the background, shows progress in the status bar and is one undo step. The code is `gooslicer/orient.py`.
- **Score:** each candidate "up" direction is scored by the support contacts the generator would place (§2), using the chosen support type's critical angle and anchor spacing:
  - overhang faces: their area as seen from below ÷ anchor spacing². Faces within 10° of straight down count 1.3×, because large flat undersides peel hard off the film.
  - lowest points not covered by an overhang face: one contact each.
  - down-facing sharp ridges (convex edges sharper than 30°, pointing down, rising less than the critical angle) between faces that need nothing: one contact per anchor spacing. A tilted plate's bottom edges are an example.
  - height: 0.05 contacts per mm, so near-ties go to the faster print.
- **Search:** 300 directions spread over the sphere, then 40 within 12° of each of the 4 best. Scores within 1 % are a tie, and ties go to the smallest turn.
- **When it turns:** only for at least 3 % fewer contacts; otherwise the Log says the part is already at a good angle. The part keeps its centre on the plate and is dropped to it (or to its support lift height).
- **Examples:** a 40 × 40 × 2 mm plate tilts about 50° (520 → 15 contacts). A cylinder lying on its side stands up (77 → 15). A sphere or an upright cup stays as it is. The arc sample tilts 14° (214 → 200). Lisa_head_C turns about 100° (129 → 97) in about 6 s.
- **Not considered:** where support marks land on visible surfaces, and suction cups (hollow cups facing the film; the trapped-resin check finds them, 07 §4b). Check the result before supporting.

## 10. Open items
- Tree grouping. Read so far **[DECOMPILED: EleTriBuilder.dll]**:
  - `FUN_18001ce20` seeds a group with one point, then adds the nearest unused points (k-d tree) while the group has fewer than `maxBrunchNum` members.
  - `FUN_18001ecd0` tries to attach a point to existing groups, nearest centroid first.
  - Both go through `FUN_18001d750`, which accepts a point when:
    - the group, with the point added, still has its joint at least 1 mm up;
    - no member's branch to the joint, and not the new point's branch, hits the model;
    - the group is not already at `maxBrunchNum`. A full group can swap its farthest member for a closer point, but only in the seeding pass.
  - The joint is placed below the new centroid, as in §8. One difference: when a point is added, height type 1 uses 45°, not 30°.
  - No grouping radius applies, because every caller passes −1.
  - **[UNKNOWN]** All three Tree templates set `maxBrunchNum` to 1. Read literally, that would stop every group at one member, meaning no merging at all. Either the builder's record is not `SUPPORTVARIABLESBAR` as assumed (fields 0xec/0x104/0x100/0x108 copied to builder +0x30…+0x3c by the code at 0x1800e3342), or SatelLite's trees really never merge.
  - Settling this needs a SatelLite project sliced with a Tree template. GooSlicer keeps merging nearest pairs, highest joint first, until a sample decides it.
- `anchorDisBorder`: needs a SatelLite project where it differs from `anchorDis`.
- 106 rejected contacts on arc (EVO, 30° cap; 96 with the old 45° blend; 84 with tree values; no islands left in either case): mostly points with no clear tip path or a downward surface below. Worth checking that none leave an area unsupported (the island check passes today).
- On SatelLite's own supported points (10 §5), GooSlicer's checks reject 1 of 370. So the arc rejections above are mostly points SatelLite wouldn't support either; SatelLite left 130 of its 500 detected points unsupported.
