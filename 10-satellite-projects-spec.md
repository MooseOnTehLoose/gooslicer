# 10 — SatelLite projects (`.goo.3mf`)

Tags as in 06: **[OBSERVED]** is read from a real file or SatelLite's binaries; **[INFERRED]** is a reading of observed data that fits but isn't proven; **[UNKNOWN]** is not decoded yet.

Sample: `samples/arc-1a-attitude-4.goo.3mf` (10.3 MB; not in git), saved by SatelLite 4.2.2.43 (`ProgramVersion`) on 2026-11-24. One part, `arc-1a-attitude-4` (the arc STL in `samples/`), EVO supports with a raft, resin "Space Grey 8K ABS-Like 3.0" on the Saturn 4 Ultra 16K.

Status: **decoded and opened by GooSlicer, 2026-11-24** (§1–6). Code: `gooslicer/satellite.py`; tests: `tests/test_satellite.py`.

## 1. Package [OBSERVED]
A standard 3MF zip written by lib3mf (01: `Lib3MF::CWriter`), plus SatelLite's attachments. The root `.rels` names only the model; `3D/_rels/3dmodel.model.rels` names the attachments, with relationship types under `https://satellite.elegoo.com/3mf/attachments/` (`testjson` for three of them, `support` for the supports).

| Entry | Size | Contents |
|---|---|---|
| `3D/3dmodel.model` | 3.1 MB | the part meshes (§2) |
| `Metadata/info.json` | 6 KB | everything else as JSON (§3) |
| `Metadata/Document.xml` | 59 KB | the same as info.json, as XML, plus a base64 PNG preview (`<Preview data=…>`) |
| `Metadata/MeshKernel.bms` | 100 B | per-part placement (§4) |
| `Metadata/SupportKernelEx.bms` | 29 MB (9.6 MB zipped) | supports: mesh, points, links, parameters (§5) |

The `.goo.3mf` extension is what tells SatelLite it's a project (`isGoo3mfProjectFileName`, 01).

## 2. `3D/3dmodel.model` [OBSERVED]
- One `<object type="model">` per part, with `p:UUID`, named as the part. The build item has **no transform**.
- Vertices are in **plate coordinates, with the part's placement already applied**. The arc's mesh equals the STL turned 30.9° about −X and shifted, with no mirroring (determinant +1, every vertex within 8 × 10⁻⁶ mm, same vertex and triangle order).
- The origin is a corner of the plate: every coordinate is positive and the part's x and y lie within 211.68 × 118.37. The frame is right-handed with Z up (no mirroring), so **[INFERRED]** X right, Y back, origin front-left. GooSlicer's plate coordinates are centred: subtract (X/2, Y/2, 0). Not checked against a print: a 180° turn about Z would also fit.
- The part's lowest point is at z = 5.0, the support lift height (`LiftHeight`, 06). The supports are not in this file (§5).

## 3. `Metadata/info.json` [OBSERVED]
JSON, one line. Keys:
- `ProjectFormat` "goo.3mf", `ProjectFormatVersion` 1, `Goo3mfMeshMapVersion` "0.0.0.2".
- `Goo3mfMeshMap`: `{"Storage": "3mf-plus-meshkernel-meta", "Meshes": [{"Model": "3D/3dmodel.model", "ObjectId": 1, "MeshKernel": "MeshKernel.bms"}]}` ties each 3MF object to its placement file.
- `Objects` (`ObjectCount` 1): `Name`, `SourceName`, `Uuid`, `Index`, `Role` "part", `Visible`, `ColorR/G/B/A`, `ClassName` "Support::DocumentObject", `HasSupport`, `SupportAttachment` "/Metadata/SupportKernelEx.bms".
- `ActiveScript`: the print settings in use, in `.cfgx` key names (06, 05): `Scirptname` (sic, the resin profile's name), `layerthickness`, `exposureTime`, `bottomExposureTime`, `bottomLayerCount`, `tanslate_layer_count` (sic), lift, retract, wait and PWM values, `resineDensity` (sic), `resinCost`, `resinType`, `resinCompany`, `ExtraResinPara_*` (resin colour, printer name, profile id). Floats are float32 values written as doubles (0.05000000074505806).
- `VDPRINTERPAMETERS`: `ScriptCnt` and `Scripts`, every resin profile the project carries (here "Normal" and the active one).
- `PlatformInfo.Platform`: the printer's build volume (`X` 211.68, `Y` 118.37, `Z` 220) and many SLA/laser fields that don't apply.
- `SatelLiteMachine`: `PrinterType`, `ResolutionX/Y` 15120 × 6230, `PixelX/Y` 14 × 19 µm, `MirrorX` false, `MirrorY` true, `isInclinePrinter`.
- `ExtraPlatformPara` (`Id` "23"), `SliceFixing` (`FillGap` 0.01 …), `MultipleField`, `NonPrintAreaInfo`: not used by GooSlicer.

## 4. `Metadata/MeshKernel.bms` [OBSERVED]
100 bytes, little-endian:

| Offset | Type | Value in the sample |
|---|---|---|
| 0 | char[4] | `MKMG` |
| 4 | u32 | 2 (version) |
| 8 | float32[6] | the part's bounding box min, max (equal to its mesh's) |
| 32 | float32[16] | a 4 × 4 matrix: identity here, since the mesh already holds the placement **[INFERRED]** |
| 96 | u32 | 0 **[UNKNOWN]** |

## 5. `Metadata/SupportKernelEx.bms` [OBSERVED layout, INFERRED meanings]
Little-endian throughout. Sections in order:

**Header (264 bytes):** u32 0xA0B0C0D0, u32 0x00010000, then `MESH-` repeated, ending in `\n` at byte 263.

**Support mesh (one indexed triangle mesh for all of the part's supports and its raft):**
- 8 zero bytes, u8 1, u32 vertex count (614,130), float32 × 3 per vertex in plate coordinates; u32 triangle count (1,205,902), int32 × 3 per triangle.
- Then float32[6] bounding box and float32[16] identity matrix, as in MeshKernel.bms.
- The mesh spans z 0 to 47.8 and reaches the model's underside.

**`Support::SmartSupportBlock`:** u8 1, u32 2 (two arrays), the name ending in `\n`, then two arrays, each a u32 count and 44-byte records:

| Offset | Type | Meaning |
|---|---|---|
| 0 | float32[3] | point (plate coordinates) |
| 12 | float32[3] | direction, unit length (0 for points on the plate) |
| 24 | int32 | index in the array, or −1 |
| 28 | float32 | 0.1 in every record **[UNKNOWN]**: tip depth into the part (`distanceInPart` 0.1)? |
| 32 | u32 | role: 1 = contact on the model (500), 2 = joint (998), 3 = on the plate (80 + all 165 of the second array), 5 (75), 0 (9) **[INFERRED]** |
| 36 | u16 | 0 |
| 38 | int32 | the model triangle the contact lies on, −1 for the others. **[OBSERVED]**: each contact lies on that triangle's plane to within 6 × 10⁻⁶ mm |
| 42 | u16 | contact type: 2 = overhang (430; median spacing 1.98 mm = `anchorDis` 2.0), 3 = lowest point (70; on faces pointing nearly straight down, tips near vertical) **[INFERRED]**; 2 for every non-contact |

- Array 1: 1,662 points, of which 500 contacts. Array 2: 165 points on the plate, on a grid with 3.5 mm steps.
- **Tip directions** (measured with the part's mesh, 2026-11-24):
  - No tip leans more than 30.00° from vertical, and 312 of 500 lean exactly 30°. So SatelLite caps tips at **30°**.
  - 286 of 500 (57 %) are exactly the outward normal capped at 30° (within 1°; median gap 0.01°). For normals under 28° from straight down, the tip angle equals the normal's. GooSlicer uses this rule since 2026-11-24 (08 §3). The old blend with straight down matched 4 %.
  - The other 214 keep the angle but turn the heading (median 37°). **[UNKNOWN]** why. Not to avoid the model: the capped normal would be blocked for 27 of the 286 and 27 of the 214, about as often in both groups. Not to share a pillar either: each tip runs 1 mm to its own joint (links of kind 0).
  - GooSlicer's model check would block 50 of SatelLite's own tips (a ray from 0.02 mm off the surface, 1 mm long), so it's stricter than SatelLite's.

**`Support::SupportSmartEntity`:** u32 1, the name ending in `\n`, u32 4 **[UNKNOWN]**, u32 1,935 pieces. Each piece: u32 triangle count, u32 vertex count, that many int32 triangle indices, then vertex indices into the support mesh. The ranges are consecutive and cover all but 10,116 triangles (**[INFERRED]** the raft). Piece sizes: 66/36 × 1,186, 2,050/1,030 × 379, 1,024/516 × 290, 545/276 × 80.

**Links:** 1,935 records of int32 × 4: from point, to point, kind (0 × 368, 1 × 366, 2 × 2, 3 × 1,190, 4 × 9), piece index or −1. For example 500 → 501 kind 1 piece 1, then 501 → contact 0 kind 0 piece 0: a pillar, then a tip **[INFERRED]**.

**Parameters and rest (78 KB) [UNKNOWN]:** starts with values that match the EVO template's support parameters (2.0, 2.0, 0.5, … 0.1, 0.32, 0.6, … 60, 7.0, 0.8, 06 §1), then## 6. Opening in GooSlicer [GOOSLICER]
Decided 2026-11-24: **both**. SatelLite's support mesh is sliced exactly until a change makes it wrong for the part; GooSlicer then builds its own supports from SatelLite's support points.

- **How it opens.** File → Open, Open recent, a drop onto an empty plate, or the command line. A `.goo.3mf` opens as an **untitled** project: Save asks for a new name and writes a GooSlicer project. The SatelLite file is never written to. It still goes on the recent list. Importing one into a project that's already open adds its parts with their supports, as for a GooSlicer project.
- **Parts.** Each 3MF object becomes a part, centred on its own mesh. Coordinates shift from the plate corner to GooSlicer's centred plate (§2). `elevation` is the part's lowest z (the lift).
- **Printer and resin.** `SatelLiteMachine.PrinterType` selects the printer. `ActiveScript` selects the resin by `Scirptname` and puts its values on top, as a GooSlicer project's saved settings do. The sample's lift and retract values are 0. They are 0 in SatelLite's own profile too, and both slicers raise them to 0.05 mm when writing the `.goo` (05 §4).
- **Support plan.** Points with role 1 and a model triangle become the plan's contacts. Normal: that triangle's outward normal. Kind: type 3 → lowest point, anything else → overhang. `params.lift` is the part's lift; the other values are GooSlicer's defaults, since the parameter section isn't decoded (§5). The template name is "SatelLite project".
- **Fixed geometry.** The plan also carries `FixedSupports`: SatelLite's mesh plus the part's mesh, turn, scale and height at import. A move on the plate shifts the mesh with the part. Any other change makes GooSlicer build supports from the plan's contacts, and the log says so once per plan: a new mesh (repair, hollow, mirror), turn, scale, height or lift, or added or removed points. Undo brings the exact mesh back.
- **Removing a point.** SatelLite's mesh has no pillars to pick. Ctrl+click picks the contact within 2.5 mm in plan view, at or above the click.
- **Saving.** A GooSlicer save writes the support mesh as a `type="support"` object, as always. A plan whose fixed mesh still fits also records `"fixed": {object, source, position, rotation, scale}` in project.json. Reopening keeps the exact supports. Without that key the plan is rebuilt as usual.
- **Sample results.** Opens in 0.2 s. With the fixed mesh, a save takes 1.3 s and a reopen 1.4 s (14 MB). GooSlicer's rebuild from the 500 points takes 0.7 s and gives 481 pillars. It turns down 19, mostly where its model check is stricter than SatelLite's (§5). With the old 45° blend it was 487 and 13.

## 7. Open
- The Y direction of SatelLite's frame (§2). Checkable with one test print, or with a project and its sliced `.goo` saved together.
- The 0.1 field, the role values 0 and 5, the turned tip headings, and the parameter section (§5). Decoding the parameters would let GooSlicer rebuild with SatelLite's tip and pillar sizes instead of its defaults.
- Several parts in one project: the sample has one. Parts are matched to `Objects` by order.
- Writing `.goo.3mf` files for SatelLite to open.
