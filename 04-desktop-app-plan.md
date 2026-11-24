# Stage 4: Native Linux desktop app — architecture and phased plan

Status: **approved 2026-11-23**. P0 done, P1 done, P2 done (2026-11-23; see 05-slicing-spec.md), P3 done (2026-11-23; simulator plus hardware tests on the user's printer, see 02-lan-sdcp-spec.md §6–7), P4 done (2026-11-23; see 06-support-hollow-defaults-spec.md), P5 done (2026-11-23; see 07-hollow-drain-spec.md), P6 done (2026-11-23; EVO and tree supports, raft, editing, island check with pre-slice warning; see 08-supports-spec.md), P7 next.
Decisions so far (2026-11-23):
- Toolkit: Qt6 / PySide6.
- Algorithms: hybrid. Open, clean-room algorithms, with SatelLite's *defaults and parameters* recovered by reverse engineering.
- Dependencies: project venv plus PyPI packages.

Name: **GooSlicer**.
- Printers: all Saturn 4 models in `officialPlatform.ini`: Saturn 4, Saturn 4 Ultra and Saturn 4 Ultra 16K.
- Project files: 3MF with GooSlicer extra data (supports, hollow and drain settings, profile), stored as its own 3MF part so other slicers ignore it.

## 1. Goals
1. A native Linux replacement for the SatelLite workflow on ELEGOO MSLA printers that speak SDCP V3.
   - The workflow is: import → orient/arrange → hollow and drain → support → slice → `.goo` → send and monitor.
   - Targets: the Saturn 4 family (3 models). Other printers are out of scope for now.
2. Supports and hollowing should produce results that *feel* like SatelLite's: same parameter names, defaults and tip geometry. They don't have to match byte for byte.
3. Every RE-derived fact goes into a spec with evidence tags first. Code is written only from those specs (same rules as Stages 1–3).

**Not in scope** (could be added later):
- the AI agent (`EleAIAgent`, embedded Python/ONNX)
- cloud and account features
- MQTT-only printers (decided earlier)
- lattice infill (`EleTriInfill`, `EleTriCreate`)
- labels/emboss, the wash-score/NFC features, and V5.x `.goo` extensions

**Clean-room rule for assets:** SatelLite's icons, images, translations and resources (`EleResources.dll`, `resources/`) are not copied into the app. The app may *read* `officialPlatform.ini` from the user's own SatelLite install as data. It does not redistribute that file.

## 2. What SatelLite has, and where it lives [OBSERVED by strings/imports]
| Feature | SatelLite module(s) | Evidence | Our phase |
|---|---|---|---|
| Printer and resin profiles | `resources/machines/printer/officialPlatform.ini` (QSettings INI, about 490 KB, sections keyed by printer ID, values are `@Variant` big-endian floats) | `Printer\LiftingDis`, `Resin\ResinDensity`, … | P0 |
| Mesh import and repair | `EleTriIO`, `EleTriFix` (add/invert/remove triangles, "Add thickness") | strings | P1 |
| Orient and arrange | `EleTriOrient`, `EleTriArrange`, `EleArrange`, `EleTriCollision` | module names | P1 |
| Hollowing | UI and command in `EleShapeModify` (`CmdHollow`, `DialogHollow`: Direction, Parameters, "Center Hollow"). The engine is in `EleShapeEdge` (voxel `HollowOctree`). Settings `/Hollowing/thickness`, `/accuracy`, `/isInside` | "Accuracy must be less than or equal to half of the wall thickness." | P4 → P5 |
| Drain holes | `EleTriHole` ("Perforate your part to drain the trapped material.") | strings | P5 |
| Supports | `EleTriAdd`, `EleTriAddExpand`, `EleTriBuilder`, `EleTriBuilderBase`, `EleTriTip`. Parameter tree, for example `area.criticalAngle`, `structure.connect.topConnect.tipLength`, `structure.connect.botConnect.tipLength`, `structure.connect.platConnect.slotAngle`, `structure.bar.thickness`, `structure.cross.barAngle`, `structure.contour.thickness`, `structure.gusset.angle`, `structure.structure.diamond.diamondAngle`, "Ball contact", "Add tree branch", "Angled volume supports" | strings | P4 → P6 |
| Slicing and raster | `EleImageApp`, `EleLyHatch` | Stage 3 | P2 |
| `.goo` output | `EleImageApp` | **done** (Stage 3, `elegoo_goo`) | — |
| Printer LAN control | `EleNetManager` | **done** (Stage 2, `elegoo_lan`; UI in P3) | P3 |
| Print-time estimate | `FUN_18000cf70` in `EleImageApp` | formula recovered in Stage 3 | P2 |

## 3. Architecture
```
gooslicer/                     (Qt-free core; headless and unit-tested)
  profiles.py                  officialPlatform.ini reader (QSettings @Variant decoding)
  mesh.py                      load/save STL/OBJ/3MF, repair, analysis (trimesh)
  scene.py                     parts, transforms, build-volume checks, undo stack model
  hollow.py                    voxel SDF shell, drain holes, trapped-volume detection
  supports/                    overhang/island detection, point placement, geometry builders
  slicer.py                    manifold slices → polygons → rasteriser (pluggable)
  estimate.py                  time/volume/weight/price (formula from Stage 3)
  project.py                   project files (3MF + our metadata)
gooslicer_qt/                  (PySide6 UI)
  main.py, mainwindow.py       docks: parts tree, properties, printer panel, layer preview
  viewport/                    QOpenGLWidget plus GLSL shaders, camera, picking, gizmos
  workers.py                   QThreadPool jobs (slice/hollow/support/upload) with cancel & progress
  printer_panel.py             wraps elegoo_lan: discovery, status feed, upload, controls
elegoo_lan/  elegoo_goo/       (existing; reused unchanged)
```
- **Core/UI split.** All geometry is in `gooslicer/` with no Qt import, so it can be tested and scripted from the CLI. The one exception is that the rasteriser *may* use `QPainter` on a `QImage(Format_Grayscale8)` for speed. A numpy scanline rasteriser serves as the reference and fallback.
- **Threading.** Long jobs run in worker threads or processes. Slicing runs layers in parallel across a process pool, since there are 16 cores. The UI never blocks, and every job can be cancelled. This replaces SatelLite's `SequencerLauncher`/`AbortException` pattern seen in Stage 3.
- **Win32 → Linux.**
  - Settings and user data go to `QStandardPaths` / XDG (`~/.config/gooslicer`, `~/.local/share/gooslicer`).
  - Output files are written to a temp file and then `os.replace`d.
  - A `.desktop` file and icons follow the XDG spec.
  - Packaging is **Flatpak** (decided 2026-11-23, replacing the earlier AppImage-first plan). See `flatpak/README.md`.

### Dependencies (Python 3.14 wheels verified with `pip --dry-run`, 2026-11-23)
| Package | Use |
|---|---|
| PySide6 6.12 | UI, OpenGL widget, QPainter rasteriser |
| numpy 2.5, scipy 1.18 | arrays; `ndimage.distance_transform_edt` for the hollowing SDF; KD-trees |
| trimesh 5.2 (+ rtree 1.4, networkx 3.7) | mesh I/O, repair, ray casts, connectivity |
| manifold3d 3.5.5 | robust booleans (drain holes, support union), plane slicing, `level_set` meshing |
| shapely 2.2 | 2D polygon operations (island detection, support bases) |
| PyOpenGL 3.1 | GL calls beyond Qt's wrappers |
| *optional* numba 0.68 | JIT for hot loops (point sampling, rasteriser fallback) |

**OpenVDB has no wheel**, and SatelLite uses it. Hollowing will use a voxel SDF instead: an EDT on a narrow band at the chosen accuracy, meshed with `manifold3d` level-set or marching cubes. That matches SatelLite's own voxel approach (`HollowOctree`). If this proves too slow or memory-hungry for large models, a small C++ extension is the fallback (decision point in P5).

## 4. Phases, deliverables and acceptance tests
Each phase ends with passing tests and a short demo. Specs are written before the code in each phase.

**P0: Foundation (small)**
- Set up venv/pyproject, the app skeleton (main window, docks, settings) and logging.
- `profiles.py`: parse `officialPlatform.ini`. The printer-ID → model mapping comes from `ProtocolVersion`/`MachineName`.
- ✔ Tests: decoding the `@Variant` floats, and the profile for "Saturn 4 Ultra 16K" yields 15120×6230 and 211.68×118.37 mm, as in the sample `.goo`.

**P1: Scene and 3D viewer**
- Import STL (binary/ASCII), OBJ and 3MF.
- An OpenGL viewport with orbit/pan/zoom, build plate and volume, and picking.
- Move/rotate/scale gizmos, drop to plate, auto-arrange, duplicate.
- Mesh checks (non-manifold edges, holes, flipped normals) with one-click repair.
- Undo/redo.
- ✔ Tests: transform maths, out-of-volume detection, repair on known-bad meshes.

*P1 status (done).*
- Built:
  - STL/OBJ/3MF import, including multi-object 3MF, run as background jobs
  - GL 3.3 viewport: plate grid, volume box, orbit/pan/zoom, view presets
  - picking (multi-select with Ctrl) and drag-move on the plate
  - transform panel (position, size, scale %, uniform lock), rotate −45° buttons or by any angle, lay flat on a clicked face, drop to plate, centre, reset
  - shelf auto-arrange, duplicate and delete
  - mesh report (holes, non-manifold edges, degenerate and duplicate triangles, winding, inverted, volume) with one-click repair
  - undo/redo for every edit
  - out-of-volume parts shown in red and logged
- **Deviation:** there are no 3D gizmo handles. Moving is done by dragging on the plate plus numeric fields; rotation uses buttons, angle input and lay-flat. Gizmo handles move to P7 if wanted.
- Tests: core tests, plus GL viewport tests run under Xvfb with `scripts/test-gui.sh`.

**P2: Slice → `.goo`**
- Slice with `manifold3d` at each layer Z.
- Rasterise at the printer's native resolution, respecting the mirror flags from Stage 3.
- Layer-preview slider and an estimate panel (time, volume, weight, price).
- Write the file through `elegoo_goo`.
- Note: pixels are **not square**. On the 16K the pitch is 14.0 µm in X and 19.0 µm in Y (211.68/15120 and 118.37/6230) [SAMPLE].
- ✔ Acceptance: slice a 10 mm cube STL. Every layer must match SatelLite's `samples/10mmcube.goo` to within 1 px on the edges, and the header fields must match apart from software name and time.

**P3: Printer integration (UI over `elegoo_lan`)**
- Discovery, a compatibility banner for old printers, and live status (state, layer progress, temperatures).
- Upload with progress, then start/pause/stop. Each of these asks for confirmation.
- File manager.
- ✔ Tests: run against the fake printer from Stage 2. A real-printer test only happens with your explicit go-ahead.

**P4: RE of support and hollow defaults (runs alongside P1–P3)**
- Decompile the parameter-tree defaults and validation rules in `EleTriBuilderBase`/`EleTriAddExpand`.
- Recover tip geometry (`EleTriTip`, "Ball contact"), the critical angle and density rules, the hollow defaults (`EleShapeModify`/`EleShapeEdge`) and drain-hole defaults (`EleTriHole`).
- Output: `05-support-hollow-defaults-spec.md` with evidence tags, plus `evidence/*.json`.
- Optional and only on your request: runtime captures of SatelLite's settings files from your Windows machine, to confirm the defaults.

**P5: Hollowing and drain holes** — done; the OpenVDB decision point resolved as pure Python (no C++ extension needed, 07 §5).
- Hollow types Inside / Outside / Center / Open Bottom (surface angle 5–85°). Defaults: wall 1 mm, accuracy 1 mm (06 §2). SatelLite's dialog enforces no accuracy ≤ thickness/2 rule. GooSlicer may warn about it but does not claim SatelLite parity for it.
- Optional internal infill grid can come later.
- Drain holes: click on the surface to place one, with diameter and depth, applied as a manifold boolean.
- Trapped-volume detection: a 3D flood fill of empty space on a voxel grid (replaces the per-layer plan; it also finds cavities that are open only sideways), with a warning before slicing. Open "cups" that hold resin without being sealed are not flagged yet.
- ✔ Tests: hollowing a 20 mm cube gives the expected wall volume within tolerance; the output mesh is watertight; a part with no drain hole gets the trapped-resin warning.

**Reference job for P4–P6 [SAMPLE].** The pair `samples/arc-1a-attitude-4.stl` (binary STL, 40 074 triangles, 32.5 × 23.4 × 52.9 mm) and `samples/arc.goo` was sliced by SatelLite with auto supports on the 16K. It has 1140 layers and is 57.0 mm tall.
- Layers 0–5 are a raft of about 860 mm².
- Thin supports run from about layer 20 (about 90 mm²); the model comes after.
- The reader round-trips it byte-identically.

GooSlicer's output for the same STL will be compared against it layer by layer: raft footprint, lift height, contact count and density.

**P6: Supports**
- Detection: overhang faces (critical angle), islands (new regions per layer from slice diffs), and local minima.
- Point placement, density-driven, with RE'd defaults.
- Structures:
  - contact tip (cone or ball), shaft and base
  - cross-bracing (bar angle)
  - tree branching (merge shafts)
  - raft/base plate
- Manual editing in the viewport: add, delete and drag points.
- Collision avoidance with the model, using ray casts.
- Union with the model for slicing.
- ✔ Tests: no unsupported islands remain after auto-support on reference meshes; every support is watertight; the parameter defaults equal the P4 spec.

**P7: Polish and packaging**
- Project save/load (3MF with metadata), recent files, preferences, keyboard shortcuts.
- Translations scaffold.
- **Flatpak build: done (2026-11-23).** `io.github.mooseontehloose.GooSlicer` on `io.qt.PySide.BaseApp` 6.11 / KDE 6.11 (Python 3.13), with the `.desktop` file, AppStream metainfo and an original icon. `scripts/build-flatpak.sh` builds, installs and writes `dist/GooSlicer.flatpak` (84 MB). Checked in the sandbox: the support → slice pipeline, the GL window, and discovery, status and upload against the printer simulator.

Rough order of effort: P0 S · P1 L · P2 M · P3 S–M · P4 M · P5 M–L · P6 XL · P7 M.

## 5. Risks
| Risk | Mitigation |
|---|---|
| Rasterising 94 Mpx layers in Python is slow | QPainter (C++) plus a process pool; only changed layers are re-rasterised; numba fallback |
| No OpenVDB; voxel memory at fine accuracy | narrow-band/blocked EDT; clamp accuracy; C++ extension if needed |
| Support generation quality is open-ended | phase P6 is scoped to auto plus manual editing; iterate on test models you supply |
| Python 3.14 wheel churn | pin versions in `pyproject.toml`; lock file |
| Printer rejects Linux-built files | P2 acceptance compares against SatelLite output; first real print only with your go-ahead |

## 6. Answers (2026-11-23)
1. Name: GooSlicer.
2. Printers: Saturn 4, Saturn 4 Ultra, Saturn 4 Ultra 16K.
3. Test model: `samples/arc-1a-attitude-4.stl` with SatelLite's `samples/arc.goo`.
4. Project file: 3MF plus GooSlicer extra data.
