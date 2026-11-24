# 05 — Slicing to .goo (GooSlicer P2)

Evidence tags: **[OBSERVED]** static RE of EleImageApp.dll (Ghidra; `evidence/eleimageapp-raster.json`,
`evidence/eleimageapp-goo.json`), **[SAMPLE]** confirmed against `samples/10mmcube.goo` /
`samples/arc.goo`, **[INFERRED]** fitted or reasoned, **[UNKNOWN]** not settled.

## 1. Pipeline

```
scene parts (world mm) ──► per-layer section (manifold3d; trimesh fallback)
        ──► polygons (mm) ──► plate→pixel transform ──► Skia-compatible scanline fill (non-zero)
        ──► spans ──► GOO RLE ──► goo.write (streamed, atomic)
```

Code: `gooslicer/slicer.py`, `gooslicer/raster.py`, `gooslicer/settings.py`, `gooslicer/preview.py`.

## 2. Raster transform [OBSERVED FUN_1800e1bb0]

All float32:

```
col = (x − ox) · 1000 / PixelX_µm          if mirror_x: col = ResX − col
row = ResY − (y − oy) · 1000 / PixelY_µm   if mirror_y: row = ResY − row
```

- `ox`, `oy` are doubles at `param_3+0xf8` / `+0x100`. GooSlicer uses the plate centre at the image
  centre (`ox = −ResX·PixX/2000`) **[INFERRED]**. With this, the cube sample is reproduced byte-for-byte
  at the recovered position (−15.869, 1.51) **[SAMPLE]**.
- Pixels are not square on the 16K (14 × 19 µm), so X and Y scales differ.

## 3. Fill [OBSERVED FUN_1800e2510]

- SkPaint is white, fill type is forced to `kWinding` (non-zero). Overlapping parts union naturally;
  self-intersections fill.
- The model path uses AA only if the AA flag (`+0xb4`) is set. The support path is always non-AA. The AA
  LUT (`+0xec`) and blur (`+0xf0`, FUN_1800e1990) only run with AA on. Elegoo printers anti-alias
  on the LCD, so GooSlicer writes binary layers **[USER-REPORTED]**.
- `raster.spans` reproduces Skia's non-AA scan converter: FDot6 = trunc(x·64); top/bot =
  (y+32)>>6; 16.16 slope by truncating division; spans `[(L+0x8000)>>16, (R+0x8000)>>16)`.
  It is pixel-identical to skia-python m144 for in-canvas geometry (`test_matches_real_skia`). Geometry
  crossing the canvas edge can differ (Skia clips first). Parts outside the volume are refused anyway.
- Which Skia version skia.dll is: **[UNKNOWN]**.

## 4. Z sampling and layer count [OBSERVED]

Slicer: `Slicer::VoxSlicer` in EleTriElem.dll (CuraEngine-derived; evidence
`evidence/eletrielem-slicer.json`, `evidence/eleslicer-zcut-simplify.json`).

- Layer height is snapped to 1e-6 mm (`DAT_180061000` = 1 000 000 int units per mm) (FUN_18000ec40).
- `base = (int)(minZ / h) · h`; `count = (int)((maxZ − base)/h + 1)` (FUN_18000ec40).
- `z_i = base + i·h + offset`, where `offset` comes from `cutposition` (FUN_18000e850):
  0 → 0, **1 → h/2 (default, set in `SlicingParas::initialze`, EleLyHatch.dll)**, 2 → h,
  3 → `minZ + i·h`.
- Empty layers at both ends are dropped (`VoxSlicer::StartSlicing`). For a model on the plate, this gives
  `ceil(maxZ/h − f)` layers. For example, a 10.01 mm part at 0.05 mm gives 200 layers.

GooSlicer uses the same rule (`slice_z_fraction`, default 0.5). The UI offers bottom / middle / top,
matching `cutposition` 0/1/2.

**Sphere check** (`samples/10mmsphere.goo` from `samples/sphere.3mf`) [SAMPLE]:
- A mid-layer cut matches SatelLite's per-layer area with RMS 0.10 mm², against 0.55 / 0.36 mm² for bottom /
  top cuts (`test_sphere_sample_is_cut_mid_layer`).
- It isn't pixel-exact. About 100–250 edge pixels differ per layer, out of 50k–295k lit.
- The residual looks like the model sitting about 5 µm lower in SatelLite. That cuts the pixel mismatch by
  about 3×, but the best offset varies by layer and nothing in the decoded code explains it **[UNKNOWN]**.
  GooSlicer does not apply it; it is a tenth of a layer.

### Polygon post-processing [OBSERVED], not reproduced

`SlicerLayer::makePolygons` (0x18001dd30), model path (`isSupport = false`):
1. `POLYGON::PolygonRef::simplify(5000, 10000)` (ElePolySet.dll 0x180032e20, CuraEngine's simplify).
   It compares squared int distances, so it only removes points closer than about 0.1 µm. That's a
   no-op at LCD resolution.
2. If `SLICINGFIX_PARA.TOLERANCE_REDUCEPOINT` (default 0.005 mm) is above 0:
   `SlicerLayer::simplifyPolygon(tol, 999)` (0x180022e50). This is a greedy pass. A vertex is dropped when the
   Heron-formula deviation is below 1e-5, or when the turn cosine is above 0.9, the accumulated deviation
   is below `tol`, and both segments are under 999 mm.
   - The support path uses `simplifyPolygon(0.1, 999)` instead.

The greedy result depends on each contour's start point and direction, which come from SatelLite's own
segment chaining. A port applied to manifold3d contours made parity slightly worse on the sphere, so
GooSlicer leaves contours unsimplified. The effect is at most 5 µm, a third of a pixel.

Other defaults (`SlicingParas::initialze`):
- `FILLGAP` 0.01
- `CLOSEPOLY_FILTER` 0, `OPENPOLY_FILTER` 0
- `Z_COMPENSATION` 0, `BEAM_COMPENSATION` 0
- `THICKNESS` defaults 0.1 (overridden by the profile)

## 5. Header values

| Field | Rule | Tag |
|---|---|---|
| exposure, bottom exposure/layers, transition layers, PWM | resin profile (`resources/resin/material/*.data`), else printer INI | [SAMPLE] |
| transition exposure | linear over `transition_layers` (existing `goo.layer_exposure`) | [SAMPLE] |
| volume | union mesh volume (manifold), rounded to 3 dp | [SAMPLE] |
| weight | float32 `volume × density / 1000`, rounded 3 dp | [SAMPLE] |
| price | float32 `weight × cost / 1000`, rounded 3 dp (cube: 0.034) | [SAMPLE] |
| lift/retract distance & speed (8 floats) | `max(value, 0.05)` | [INFERRED] |
| second-stage lift/retract | raw value (0 stays 0) | [SAMPLE] |
| layer-definition motion floats | `max(value, 0.05)` | [SAMPLE] |
| normal after-retract wait | = bottom wait after retract (1.2) — source ambiguous with bottom light-off delay | [INFERRED] |
| pause Z | 200.0 | [SAMPLE] |
| previews | 116² and 290² RGB565, BE (Jupiter 2: LE); GooSlicer renders its own three-quarter view | [OBSERVED] format |

## 6. Print-time estimate

Structure `0.93 × (Σexposure + Σwaits + motion)` (FUN_18000cf70) **[OBSERVED]**. For profiles with all-zero
motion (the 16K manages motion itself), motion = `2.914·N + 20.6` s, fitted on both samples. With
rounding this gives exactly 1386 s (cube) and 6556 s (arc) **[INFERRED]**. With explicit motion values it uses
distance / speed (mm/min). That branch is not validated against SatelLite.

## 7. Performance

Arc (40k triangles, 1058 layers, 15120×6230): **6.2 s**, about 170 layers/s, on a process pool (spawn), with
progress reported per 8-layer batch. Cancel is checked between batches. The output is written atomically,
so a cancelled slice leaves no partial file.

Caveat: the spawn start method re-imports `__main__` in workers. Scripts that use `SliceJob` with
workers > 1 need an `if __name__ == "__main__":` guard (the GUI entry point has one).

## 8. Verification

- `tests/test_slicer.py`:
  - cube byte-identical to SatelLite: all 200 layer images, layer definitions and header fields
    except name/time/preview
  - Skia parity on 150 random scenes
  - non-zero fill, transform and mirrors
  - parallel = serial
  - cancel
  - non-manifold fallback
  - overlapping-part union
  - previews
  - resin JSON and INI parsing
  - print-time parity
- `tests/test_gui_smoke.py::test_slice_from_window`: profile import → resin selection → import → slice →
  layer preview, run through the main window.
- Arc: per-layer lit area matches the exact mesh cross-section area within 0.1 mm².

## 9. Imported resin profiles (`.satres`)

SatelLite exports resin presets as `.satres` files. GooSlicer imports them with File → Import resin profile… or by dropping the file on the window.

- **Container:** a big-endian Qt `QDataStream` `QVariantMap` (same container as `.satsup`, read by `gooslicer/qdatastream.py`). Not a ZIP or JSON. [SAMPLE]
- **Layout:** one file holds several profiles. `printer/scriptmanager/scripcustomtnum` = count N; profile *i* lives under `printer/scriptmanager/scripcustomtnum<i>/scriptcontainername`, whose own value is the profile name. [SAMPLE]
- **Profile keys** (relative to the profile prefix): [SAMPLE]

| Key | Setting | Same field in resin `.data` JSON |
|---|---|---|
| `print/thickness` | layer height | `vd_printer_printer_para_thickness` |
| `print/bottomlayer`, `print/translate_layer_count` | bottom / transition layers | `…_bottom_layer`, `…_tanslate_layer_count` |
| `print/exposuretime`, `print/bottomexposuretime` | exposure / bottom exposure | `…_exposure_time`, `…_bottom_exposure_time` |
| `print/lightoffdelay`, `print/bottomlightoffdelay` | light-off delays | `…_lightoff_delay`, `…_bottom_lightoff_delay` |
| `print/wait_after_cure`, `print/bottom_wait_{before_lift,after_lift,after_retract}` | waits | same names |
| `print/{bottomliftdis,liftingdis,bottomliftspeed,liftingspeed,bottom_backspeed,backspeed}` | lift / retract | `…_bootom_lift_dis`, `…_lift_dis`, … `…_back_speed` |
| `print/slow_{bottom_,}{lift,back}_{dis,speed}` | second-stage motion | `vd_printer_printer_slow_…` |
| `advanced/ledpower`, `advanced/bottom_ledpower` | light PWM | `vd_printer_advanced_para_{led_power,bottom_led_power}` |
| `resin/density`, `resin/cost`, `resin/type` | density, cost, type | `vd_printer_resin_para_{density,cost,type}` |
| `ExtraResinParameters/PrinterName` | printer the profile is for | `cloudResinPrinterName` |
| `company` | resin maker | — |

- The mapping is by matching key names and values against the `.data` fields; it was not traced in SatelLite's code. [INFERRED]
- Kept (so they survive an export) but not used when slicing: `print/first_layer_exposure_time` ↔ `vd_printer_printer_first_layer_exposure_time` (no `.goo` field found), `print/parameter_mode` ↔ `…_parameter_mode`, and the other `advanced/*` keys ↔ `vd_printer_advanced_*` (anti-aliasing, grey level, blur, support AA, beam/inner compensation, reduce point, scale, notes), matched by name. [INFERRED] [UNKNOWN] what SatelLite does with `first_layer_exposure_time`.
- Dropped: the rest of `ExtraResinParameters` (scores, counters, ids) and `resin/suffixindex*`.
- **Storage:** an imported file is copied to `<data dir>/resins/` (named `<stem>-<hash>.satres`, so re-importing the same file is a no-op and two exports with the same file name both stay). Its profiles appear under the printer they name, alongside SatelLite's own; an imported profile with the same printer and name replaces SatelLite's.

## 10. Resin library (Resin → Manage resins…)

- **Sources:** SatelLite's install (`*.data`, read-only, never written), imported `.satres` (`<data dir>/resins/`), and copies made in GooSlicer (`<data dir>/resins/custom/`, one single-profile `.satres` each). A profile's identity is printer + name.
- **Listed:** profiles for the printers GooSlicer supports, plus every imported profile and copy. SatelLite ships profiles for every ELEGOO printer (about 2,000).
- **Copy:** asks for a name, which must differ (ignoring case and surrounding spaces) from every existing profile on any printer. A copy keeps the source's printer and values. Only copies can be edited; changes are written to the copy's file.
- **Hide:** removes a profile from the Print settings Resin profile list only. Stored in settings as `resinHidden` (a list of printer|name).
- **Delete:** a copy's file is removed; an imported profile is removed from GooSlicer's copy of its `.satres` (the file is rewritten without it, or removed when it was the last one); a SatelLite profile is only listed as deleted (`resinDeleted`) and the install is untouched. Importing a profile again clears its deleted mark.
- **Export** writes a SatelLite `.satres`: a Qt 5 `QDataStream` `QVariantMap` with keys in reverse UTF-16 order (as Qt 5 writes a `QMap`), `printer/scriptmanager/scripcustomtnum` = count, and per profile the name plus the 69 keys of `resin_library.SCHEMA` with SatelLite's types (Qt5 Float 38 for `print/*`, double for `advanced/*` floats, int, bool, ulong, string). [SAMPLE] Values come from the profile; ones it lacks are GooSlicer's slicing defaults, and keys GooSlicer does not track get the values all 36 sample profiles share.
- **Verification:** re-exporting the sample's 36 profiles reproduces the file byte for byte (`tests/test_satres.py`). Whether SatelLite imports a GooSlicer export has not been tried. [UNKNOWN]

## 11. Front of the plate, and the slice held in memory

- **Front is −Y** in GooSlicer's plate coordinates; the 3D view marks it with FRONT on the floor just in front of the −Y edge. [INFERRED] GooSlicer's Front view and default camera look from −Y, and slices match SatelLite's byte for byte (§8), so this is SatelLite's −Y as well. SatelLite's platform model (`resources/PlatformModel/*/…stl`, the build plate with its centred mounting bracket) does not settle which physical side that is. To be confirmed on the printer: slice a part placed near the FRONT edge and check that it prints at the door side of the plate. [UNKNOWN]
- **Models → Slicing → Slice** builds the whole `.goo` in memory (`SliceJob.build`): the same layers and header as a sliced file (tested against `write`). The estimates shown are the header's: print time (§6), volume (mm³ / 1000 = ml) and weight (g, §5), plus layer count and layer height.
- The held slice stays valid while the printer, the print settings and every part (mesh and support plan by identity, since edits replace them; position, rotation and scale by value) are unchanged. While valid, the panel's Export and Send to printer buttons (and File → Slice and save, and Slice and send) write it out instead of slicing again; once anything changes, the panel says so and those commands slice afresh.
