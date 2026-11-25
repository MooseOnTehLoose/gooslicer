# 06 — Support, hollowing and drain-hole defaults (GooSlicer P4)

Evidence tags:
- **[OBSERVED]**: static RE of the named DLL (Ghidra via REA), or decoded from SatelLite's shipped data files.
- **[SAMPLE]**: confirmed against `samples/arc.goo`.
- **[INFERRED]** and **[UNKNOWN]**: as before.

Evidence bundles:
- `evidence/eletriadd-template-selection.json`
- `evidence/eletristructure-hollow-perforation.json`
- `evidence/eleshapemodify-hollow-dialog.json`

Code: `gooslicer/qdatastream.py`, `gooslicer/support_defaults.py`. Tests: `tests/test_support_defaults.py`.

SatelLite's template files come from the user's own install: GooSlicer copies them once into its data folder on the user's machine (with the printer and resin profiles, `gooslicer/profile_store.py`) and reads that copy. It does not ship them. The values below are documented for interoperability.

## 1. Support templates (`resources/script/common/*.satsup`)

### File format [OBSERVED]
- A Qt 5 `QDataStream` `QVariantMap`, big-endian: `quint32 count`, then `count × (QString key, QVariant value)`.
- A `QVariant` is `quint32 type, quint8 isNull, payload`.
- Types seen: 1 bool, 2 int, 10 QString, and **38 float, which is written as an 8-byte double** (QDataStream's default floating-point precision).
- Header keys are under `support/scriptmanager/scripcustomtnum0/scriptcontainername/`: `scriptNum`, `scriptNum/script0/type`, `…/script1/type`, and the container name.
- Parameters are `<name>/<script type>/<param>` (for example `EVO_30-60mm/7/area.criticalAngle`) or `<name>/<param>`. Each parameter has a sibling `…/ConfidentState` (always 3).

### Templates shipped (1.0.2.43)
- **Common** (`common/`):
  - `EVO_0-30mm`, `EVO_30-60mm`, `EVO_60-100mm`, `EVO_100+mm`: script types 7 (EVO structure) + 9 (raft).
  - `Tree_Small`, `Tree_Medium`, `Tree_Large`: script types 0 (tree structure) + 9 (raft).
- **`common/ELEGOO Jupiter 2/`**: the four EVO templates with Jupiter-specific values.
  - Their platform connection is always on (radius 0.7, height 0.7, slot −45°).
  - `h2wRatio` is 12–30, and `maxExtend` is −1.
- **Folder choice:** templates are picked per printer from a sub-folder named after the printer, falling back to `common/` **[INFERRED from the layout; the loader was not decompiled]**. Every Saturn 4 model uses `common/`.
- **Other strings:** `Smart support.satsup` and `common/Raft.satsup` appear in EleTriAdd but are not shipped. They are presumably user-saved or legacy **[UNKNOWN]**.

### Key EVO values, common folder [OBSERVED decode]

| Parameter | 0-30 | 30-60 | 60-100 | 100+ |
|---|---|---|---|---|
| `area.criticalAngle` (°) | 37 | 50 | 45 | 50 |
| `anchorDis` / `anchorDisBorder` (mm) | 1 / 1 | 2 / 2 | 2.5 / 2 | 2 / 2 |
| `lowestPtsInterval` / `lowestPointinfluenceRadius` | 1 / 1.5 | 2 / 1.2 | 2 / 1.5 | 2.5 / 1.5 |
| `borderOffset` / `noSupOffset` | 0.6 / 1 | 0.5 / 2 | 0.5 / 2 | 0.5 / 2 |
| `topConnect.topWidth` / `bottomWidth` / `tipLength` | 0.25 / 0.8 / 1 | 0.32 / 0.6 / 1 | 0.4 / 0.8 / 1 | 0.45 / 1.0 / 1 |
| `topConnect.topBall` / `topBallDiameter` | yes / 0.5 | yes / 0.35 | yes / 0.5 | yes / 0.5 |
| `topConnect.distanceInPart` (penetration) | 0.05 | 0.1 | 0.3 | 0.3 |
| `cluster.topWidth` = `bottomWidth` (pillar) | 0.8 | 0.7 | 0.9 | 1.0 |
| `diamond.diamondWidth` / `diamondAngle` / `edgeWidth` | 9 / 45 / 0.8 | 7 / 60 / 0.8 | 9 / 45 / 0.9 | 10 / 45 / 1.0 |
| `polyEdgeNum` (pillar facets) | 8 | 16 | 16 | 12 |
| `platConnect.hasPlatConnect` (radius / height) | yes (0.7 / 0.3) | no (4 / 2) | no | no |
| `filter.shortSupLength` / `rationOnMesh` | 3 / 0.7 | 8 / 0.6 | 3 / 0.5 | 2 / 0.5 |
| `avoidNeightbor` / `locateOnNeightbor` | yes / no | no / no | no / yes | no / yes |

**Common to all four:**
- `h2wRatio` 4, `contourType` 2, `tipType` 1, `strictlyInsidePlat` yes, `bReinforceLowestPts` yes, `duplicateSupport` yes.
- Area limits: min 0 / max 99999.

**Raft (script 9), all templates:**
- `hasBasePlat` yes, `platType` 1, `slotAngle` 60°.
- Raft holes: `hasHole` yes, `holeRadius` 1, `holeInterval` 3, `holeEdgeNum` 12.
- `boundBoxBasePlat.hdist` 2.
- `platHeight` and `platOffset` by template: 0-30 → 0.5 / 1, 30-60 → 0.5 / 1, 60-100 → 0.4 / 2, 100+ → 0.3 / 2. Tree templates are 0.5 / 3, 3, 2.

**Every template:** `LiftHeight` 5 mm, `EnableInnerSupport` yes.

**Tree highlights:**
- Critical angle 55 / 50 / 45 (Large / Medium / Small), anchor spacing 2.25 / 2.3 / 2.5.
- Trunk and branch width 1.2, `maxBranchAngle` 45, trunk `maxHeight` 5.
- Cross braces every 5 mm from 3 mm, 0.3 wide at 45°.
- Top tip 0.48 / 0.45 / 0.4 wide, 2 mm long, no ball.

**Meanings:** enum values (`tipType`, `platType`, `contourType`, `direct2PlatType`, `labelPalteDirect`) and the exact roles of `h2wRatio`, `maxExtend`, `rationOnMesh` and `simplifyLevel` are **[UNKNOWN]**. They are needed for P6.

### Recommended template [OBSERVED EleTriAdd FUN_180103d10, FUN_180101ae0]
- Height `h` = the largest `DimZ` of the selected parts' mesh bounding boxes. This is the model's own height, before the 5 mm lift.
- Bucket: `h ≤ 30` → "0-30", `h ≤ 60` → "30-60", `h ≤ 100` → "60-100", otherwise "100+".
- The **first** list entry whose name contains that tag is chosen. A template already stored on the part takes precedence.
- **Warning:** when the active template's bucket doesn't fit, SatelLite shows "The max model height is %1mm. The currently selected "%2" may cause printing failure. Do you want to enable the supports recommended by the system?"
  - It shows when `h > hi` or `h < lo` (strict), so bucket edges fit both neighbouring templates.
  - Choosing "Ok" clears the per-part template and re-runs the recommendation.

### Check against arc.goo [SAMPLE]
`arc-1a-attitude-4.stl` is 52.9 mm tall, so the rule picks **EVO_30-60mm**. The slices agree:
- **Raft:** layers 0–9 are a single solid island, so the raft is 0.5 mm thick (`platHeight` 0.5 at 0.05 mm layers).
  - The island grows from 1164 to 1199 mm² going up. That fits a chamfered raft edge (`slotAngle` 60°) **[INFERRED]**.
- **Pillars:** from layer 10, 156 islands with a median of 0.376 mm². That is π·(0.7/2)² = 0.385 mm² for a 0.7 mm pillar (`cluster` width 0.7) drawn as a polygon **[INFERRED]**.
- **Model:** first model contact is near z ≈ 5 mm (`LiftHeight` 5).

## 2. Hollowing

### Hollow dialog defaults [OBSERVED EleShapeModify `DialogHollow::loadParas` 0x18002b060, UI built in 0x180028460]

| QSettings key | Default | UI |
|---|---|---|
| `/Hollowing/thickness` | 1 (mm) | "Wall thickness" |
| `/Hollowing/accuracy` | 1 (mm) | "Accuracy" |
| `/Hollowing/Type` | 0 | "Hollow type": 0 Inside, 1 Outside, 2 Center, 3 Open Bottom |
| `/Hollowing/surfaceAngle` | 45 (°) | "Surface Angle", range 5–85 (used by Open Bottom) |
| `/Hollowing/isFilter` | false | "Filter small shells" |
| `/Hollowing/filterSmallShell` | 0.1 | range 0.001–5e9 |
| `/Hollowing/filter_type` | 0 (clamped ≤ 1) | "By cavity number" or "By cavity volume" |
| `/Hollowing/cavity_number` | 1 | "Maximum cavity number", 1–9999 |

- The filter widgets are hidden unless the license tier is 3. On the free tier they are not offered.
- The Infill dialog (`DialogInfill`, EleTriInfill.dll) adds its own fields, with no ini keys: Infill structure off, BCC / FCC, cell 4 mm, strut radius 1 mm (07 §2b).
- **Correction to 04-desktop-app-plan.md:** no "accuracy ≤ thickness/2" rule was found. `set_AccuracyValue` is an empty stub (it is COMDAT-folded with the Shell and Surface2Solid dialogs). Whether the hollowing kernel (`HollowImp`, EleTriStructure) constrains accuracy is **[UNKNOWN]**.

### Structure page [OBSERVED EleTriStructure FUN_1800064a0]
- `/Structure/Hollow/bHollow` = true and `/Hollowing/isInside` = true by default.
- It reads the same thickness and accuracy keys with the same defaults (1, 1).

## 3. Drain holes (Perforation) [OBSERVED EleTriStructure FUN_180009970]

| Key | Default | Note |
|---|---|---|
| `/Perforation/radius1`, `radius2` | 2, 2 | end radii; equal values give a cylinder **[INFERRED]** |
| `/Perforation/extend1`, `extend2` | 2, 2 | depth either side of the surface **[INFERRED]** |
| `/Perforation/useAngleSlot`, `angleSlot` | false, 0 | |
| `/Perforation/useNotch`, `notchwidth`, `notchheight`, `notchangle` | false, 0.4, 0.4, 0 | |
| `/Perforation/keepSubstract` | false | keep the removed plug as a part **[INFERRED]** |
| `/Perforation/useClearance`, `clearanceValue`, `clearanceType` | false, 0, 1 | |
| `/Perforation/HoleDirID`, `HoleDirX/Y/Z` | 0, (1, 0, 0) | hole direction preset and custom vector |

The geometry of the hole operation itself (`EleTriHole.dll`, `Perforator::PerforationSelection`) is not decompiled yet. It is P5 work.

## 4. Open items
- **For P6:** enum meanings and the support generation algorithm (EleTriStructure/EleTriAdd structure builders). Template field semantics need confirming against arc.goo: pillar count and positions, tip geometry.
- **List order:** SatelLite's list order within a bucket (which template wins when several match). It is irrelevant for the shipped set, which has one EVO per bucket.
- **Runtime confirmation (optional, on request):** the user's own `QSettings` file from a SatelLite install, which shows any non-default values.
