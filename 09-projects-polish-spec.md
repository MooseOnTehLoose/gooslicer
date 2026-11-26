# 09 — Projects, recent files, shortcuts and drag handles (GooSlicer P7)

Tags as in 06: **[OBSERVED]** is static RE of SatelLite; **[GOOSLICER]** marks a GooSlicer design choice where SatelLite's behaviour was not recovered or is not copied.

Status: **approved 2026-11-24**. Done: the format (§2–3, `gooslicer/project.py`, `tests/test_project.py`); the File menu, title, prompts, Open recent and drops (§4–5, `gooslicer_qt/projectio.py`, `tests/test_gui_project.py`). Autosave and the Files and folders options (§6, `gooslicer_qt/autosave.py`) shortcuts (§7, `gooslicer_qt/shortcuts.py`, `tests/test_shortcuts.py`) and drag handles (§8, `gooslicer_qt/handles.py`, `tests/test_gui_handles.py`) are done too, and checked in the Flatpak (§9). **P7 done.**

Code: `gooslicer/project.py` (Qt-free read and write), `gooslicer_qt/projectio.py` (File menu actions, unsaved-changes prompts, recent files, autosave), `gooslicer_qt/shortcuts.py` (action registry, Keyboard preferences page, Help → Keyboard shortcuts), `gooslicer_qt/handles.py` (move and scale handles, handle mode; mixed into `Viewport`).
Tests (planned): `tests/test_project.py`, `tests/test_gui_project.py`, `tests/test_shortcuts.py`, `tests/test_gui_handles.py`.

## 1. What SatelLite does [OBSERVED strings, no sample]
- Projects are `.goo.3mf` files (`isGoo3mfProjectFileName`, "ELEGOOSatelLite.goo.3mf"). It also accepts plain `.3mf`.
- The package is written with lib3mf (`Lib3MF::CWriter`, `3D/3dmodel.model`). SatelLite's own state goes in `/Metadata/Document.xml` (a FreeCAD-style document: `PropertyMesh`, `readGoo3mfMesh`) and `/Metadata/info.json`. A `3mf-plus-meshkernel-meta` blob and a versioned "mesh map" (`getGoo3mfMeshMapVersion`) tie the 3MF meshes back to its document objects.
- Autosave goes to `AUTOSAVE-%2.goo.3mf`, and a crash leaves `crash.goo.3mf`.
- Recent projects are kept in SQLite (`projectrecent`: file path, path key, last used in ms, a PNG preview), trimmed to a limit. Recent sliced files are a separate list (`filerecent` in ElePrintHistory).
- The contents of `Document.xml`, `info.json` and the support file are decoded in 10, from a sample saved by SatelLite. GooSlicer opens SatelLite projects with their supports and settings (10 §6) but does not write them.

## 2. File format [GOOSLICER]
A GooSlicer project is a standard 3MF package (3MF Core 1.3, unit millimetre) with GooSlicer's own state in one JSON file next to it.
- **Extension `.3mf`.** The Save dialog suggests `<name>.3mf`. Any 3MF reader (PrusaSlicer, SatelLite, Windows 3D Viewer) opens the models in place. Only GooSlicer restores supports and settings.
- **Parts:** each distinct mesh is one `<object type="model">` in its **local** frame (centred, as `Part.mesh` holds it). Each part is a `<build><item>` whose `transform` is the part's `T·R·S` matrix (3MF's 3×4 row form). Parts that share a mesh (Add instance, Duplicate) share one object, so the mesh is stored once.
  - Object `name` = part name.
  - Vertices are written as `%.9g` (exact for float32; ≤ 1e-6 mm of change for float64 meshes up to 1 m).
  - Hollowing and drain holes are already in `Part.mesh`, so they are saved with it.
- **Supports:** each part with a built support mesh also gets an `<object type="support">` (world frame, identity transform) so other programs show the supports. GooSlicer ignores these objects when it reads its own projects and rebuilds supports from the saved plan (§3), in the background as it does after any edit.
- **`/Metadata/gooslicer/project.json`**, linked from the root `.rels` with a GooSlicer relationship type (`https://github.com/MooseOnTehLoose/gooslicer/project`). Contents in §3.
- **Thumbnail:** `/Metadata/thumbnail.png` (256 × 256, the 3D view as shown, standard 3MF thumbnail relationship). File managers and the recent-files list use it.
- **Writing:** to `<name>.3mf.tmp` beside the target, then renamed over it, so a failed or cancelled save never damages the old file. Done in a worker with progress. XML is built with batched `%` formatting.
- **Reading** (`project.load`): a fast path that understands only what `save` writes: object and item tags found with plain searches, vertex and triangle values with one regex per block. No XML parser runs on the package, so entities and external references are never expanded or followed. Zip entries over 8 GB unpacked are refused. A 3MF without `project.json` goes through the model importer (`mesh.load_meshes`). Measured (2026-11-24): 1.31 M triangles save in 1.8 s and load in 1.6 s (20 MB file), about 3.1 s and 2.7 s at 2.24 M.

## 3. `project.json`
```json
{
  "format": "gooslicer-project", "version": 1,
  "app": "GooSlicer 0.x.y", "saved": "2026-11-24T10:15:00Z",
  "printer": "ELEGOO Saturn 4 Ultra 16K",
  "print_settings": { "...": "PrintSettings.to_dict()" },
  "support_type": "EVO_30-60mm",
  "parts": [
    { "object": 1, "item": 0, "name": "arc", "elevation": 5.0,
      "position": [x, y, z], "rotation": [[...], [...], [...]], "scale": [sx, sy, sz], "plan": 0 }
  ],
  "plans": [
    { "template": "EVO_30-60mm", "params": { "...": "SupportParams fields" },
      "points": [[x, y, z], ...], "normals": [[x, y, z], ...], "kinds": ["overhang", ...] }
  ],
  "dismissed_flaws": { "island": [[x, y, z], ...], "cavity": [] },
  "view": { "...": "the 3D view's camera (yaw, pitch, distance, target)" }
}
```
- `parts[i]` may also carry `"opacity"` (0.05–1) for a part drawn see-through (07 §1); it is left out for solid parts. An older `"seeThrough": true` reads as 0.5.
- `parts[i]` may carry `"repaired": true` when its mesh was repaired (on import or with Repair mesh; 04 P1); the Models list shows it as "(repaired)". Left out otherwise.
- `parts[i]` matches build item `i` and names its object. Parts are restored in this order. Position, rotation and scale are stored exactly here; the build item's transform is for other programs, so GooSlicer never has to split a matrix back into them.
- `plans` holds each `SupportPlan` once, as stored: points and normals in the part's local frame. A part's `plan` is its index; parts that share a plan (Duplicate) share it again on load.
- `print_settings` is the full set, including values the user changed from the resin profile. On load the printer and resin are selected by name and then these values are applied on top. A printer or resin that no longer exists is logged and the saved values are still applied to the closest match (the same printer family, the first resin).
- `dismissed_flaws`: world points, kept because the parts come back exactly as they were (08 §5, Flaws tab: a change to a part's mesh or placement clears them).
- Unknown keys are ignored. A `version` newer than GooSlicer knows opens with a warning ("saved by a newer GooSlicer; some settings may be lost").
- **Not saved:** undo history, slice results, the island and cavity highlights (a new check redoes them), window layout and other preferences.

## 4. File menu and commands
New order of the File menu:

| Item | Shortcut | Notes |
|---|---|---|
| New project | Ctrl+N | Clears the plate after the unsaved-changes prompt. Keeps the printer, resin and settings. |
| Open project… | Ctrl+O | `.3mf` filter. A 3MF without `project.json` opens as a new project holding its models, placed as the file places them. |
| Open recent ▸ | | §5 |
| Save project | Ctrl+S | Save as… when the project has no file yet. |
| Save project as… | Ctrl+Shift+S | |
| — | | |
| Import model… | Ctrl+I | Unchanged: adds to the current project. A GooSlicer project imported this way adds its parts with their supports, but not its settings. |
| Import resin profile… | | |
| — | | |
| Slice and save .goo… | Ctrl+R | |
| Slice and send to printer… | Ctrl+Shift+R | |
| Open .goo for inspection… | Ctrl+Shift+O | |
| Locate SatelLite profiles… | | |
| — | | |
| Quit | Ctrl+Q | |

- **Title bar:** `name.3mf — GooSlicer`, with `*` when there are unsaved changes (Qt's `[*]` and `setWindowModified`). "Untitled" before the first save.
- **Unsaved changes** = the undo stack is not at its clean index, or the printer, resin, print settings or support type differ from the last save. Saving marks the undo stack clean and records the settings.
- **Prompt** before New, Open, Open recent and Quit when there are unsaved changes: Save / Don't save / Cancel. Save that fails or is cancelled cancels the action.
- **Command line and drag-and-drop:** a single `.3mf` with `project.json`, onto an empty, unchanged plate, opens as the project. Otherwise every model file is imported, as today (`open_files`).
- **Loading** clears the undo stack (it starts clean), clears flaw highlights and the slice result, then restores parts, settings, dismissals and view. Supports rebuild in the background; the slice actions wait for them as they do now.

## 5. Recent files [GOOSLICER, after SatelLite's list]
- **Open recent** lists up to 10 projects, newest first, with "Clear list" at the end. Kept in GooSlicer's settings (`recent/projects`, a list of absolute paths). SatelLite's SQLite store with previews is not copied; the 3MF thumbnail serves as the preview.
- A project enters the list when it is opened or saved. Imported models do not.
- Items show the file name; the tooltip shows the full path. A missing file is shown disabled with "(missing)" and stays until the list is cleared or the file is opened from elsewhere.
- Preferences → Files and folders: list length (1–30, default 10).
- **Flatpak:** paths from the file portal (`/run/user/<uid>/doc/…`) stay valid across sessions while the portal keeps the grant (checked, §9).

## 6. Autosave and crash recovery [GOOSLICER, after SatelLite's `AUTOSAVE` / `crash` files]
Code: `gooslicer_qt/autosave.py` (files and locks), `ProjectController.autosave_now` / `check_recovery`.
- Every N minutes while there are unsaved changes, the project is written in the background to `<data dir>/autosave/autosave-<session>.3mf`, then `<session>.json` beside it records the project's own path (if any) and the time. The data dir is `$XDG_DATA_HOME/GooSlicer/GooSlicer` (Qt's organisation/application folder); in the Flatpak, `~/.var/app/io.github.mooseontehloose.GooSlicer/data/GooSlicer/GooSlicer`. It never writes over the user's own file. N is in Preferences → Files and folders, "Autosave every" (default 5 min, Off = 0). The same page sets the length of Open recent (1–30, default 10) and the Projects folder.
- **One autosave per running GooSlicer.** Each holds an exclusive `flock` on `<session>.lock` while it runs. Locks work across Flatpak instances, which each have their own pid namespace, where process ids could not be compared.
- Becoming clean deletes the autosave: a successful Save, New, Open, and a clean quit (which also removes the lock). An autosave that finishes after the project was saved deletes itself.
- Skipped (not queued) while a slice, a save or another autosave is running.
- **At start** (`main`, before files from the command line), autosaves whose lock can be taken belong to a GooSlicer that didn't quit cleanly. The newest is offered (Restore / Discard, with its project's name and time); older ones are discarded. Restore opens it unsaved, under the project's own path if it had one, and deletes the leftover files. Discard deletes them.

## 7. Keyboard shortcuts
- **Registry:** every main-window action gets a stable id (`file.save`, `edit.hollow`, `view.top`…) and a default shortcut, in one table (`shortcuts.DEFAULTS`). `MainWindow._act` takes the id and the registry sets the shortcut; a test checks that the table and the menus hold the same ids. Defaults are written out (Ctrl+Z / Ctrl+Shift+Z, Ctrl+Q, Del, Ctrl+, for Preferences…) rather than taken from Qt's standard keys, which differ by desktop.
- **Preferences → Keyboard** (new page, last): every action with its menu path, its shortcut (a one-chord `QKeySequenceEdit`) and Clear, with a filter field. The Preferences search also matches action names. Typing a shortcut that another action uses shows which one and offers to move it. Restore defaults resets all. Stored as `shortcuts/<id>`; only changes from the default are stored.
- **Help → Keyboard shortcuts** (F1): a read-only list of the current shortcuts plus the mouse controls now in About.
- New defaults: the File items in §4; F1; W, E, R for the handle modes (§8). Existing shortcuts stay as they are.
- Single-key shortcuts (A, L, S, F, W, E, R, 1, 3, 7, Home) are window shortcuts and must not fire while a text field or spin box has focus. Qt's line edits already claim plain keys (ShortcutOverride); `test_single_keys_stay_out_of_fields` checks it.

## 8. Drag handles in the 3D view [GOOSLICER]
Moving was dragging on the plate plus number fields, and rotation already has rings (04, P4 deviation). P7 adds handles for moving along one axis and for scaling, and a way to choose which handles are shown.

**Handle mode**
- Three modes: **Move** (W), **Rotate** (E), **Scale** (R). Only the current mode's handles are drawn. Rotate is the default, so the view looks as it does now until another mode is picked.
- Picked from Edit → Handles (radio items, ids `edit.handles.*`) and from three small buttons at the top left of the 3D view. The mode is remembered between sessions (`view/handleMode`).
- Dragging a part's body still moves it on the plate in every mode.
- Handles are hidden while support editing, drain-hole placing or lay-flat picking is on, as the rings are now.

**Common rules** (shared with the rings)
- Drawn in the overlay over everything, so the model never hides them. X red, Y green, Z blue (`COMPASS_COLORS`). The handle under the mouse is drawn brighter, with an open-hand cursor.
- Click priority: flaw label, then handle, then part.
- A handle that points almost straight at the camera (shorter than 12 px on screen) is hidden, like an edge-on ring.
- While dragging, the amount is shown next to the cursor ("X +12.35 mm", "Z 120 %").
- **Esc** during a drag puts the parts back and ends it. Releasing makes one undo step ("Move X +12.35 mm", "Scale 120 %").
- Every selected part takes part. Parts with supports keep them: the plan is in the part's frame, so supports are rebuilt in the background afterwards, as for the rings.
- Parts are dropped back to the plate (or their support lift) after each step, as everywhere else.

**Move arrows**
- X and Y arrows start at the selection's bottom centre, on the plate, and point along world +X and +Y. Each is 0.55 × the selection's bounding-box diagonal long (the ring radius; at least 4 mm), with a cone tip.
- Dragging an arrow moves along that axis only. The amount is where the mouse ray meets the vertical plane through the axis that faces the camera most, projected onto the axis.
- Steps: free (0.01 mm). With Ctrl held, whole millimetres from where the drag started.
- **Z arrow** (2026-11-25): shown only when none of the selected parts has supports, since supports set their height. Dragging it sets `Part.elevation`, the bottom's height above the plate, so the lift lasts through the drop that follows every rotate, scale or lay-flat. It never goes below the plate: the drag stops when the lowest selected part reaches it. The label reads "Z +8.00 mm (lift 8.00)".
  - `PartState` carries the elevation, so undo, Esc and the transform commands restore it with the placement.
  - The Mesh panel's Z position field does the same for a bare part: the new centre height becomes a lift (at least 0), then the part is dropped to it. For a part with supports it still sets the position directly.
  - Auto-support or a support edit replaces the lift with the template's lift height; Clear supports sets it to 0.

**Scale handles**
- **One part selected:** three cube handles on lines from the part's bounding-box centre, along the part's **own** X, Y and Z axes (turned with it), just past its surface. `Part.scale` is per local axis, so scaling along world axes would shear a rotated part.
- **Uniform:** a white cube handle at the end of a short diagonal line from the top of the bounding box. It scales all three axes by the same factor.
- When the Mesh panel's **Uniform scale** box is ticked (the default), the axis cubes also scale uniformly, so the handles and the number fields agree.
- Axis cubes sit 0.3 × the handle length (at least 4 mm) past the part's surface; the uniform cube as far beyond the box's top corner.
- **Several parts selected:** only the uniform handle. Each part scales by the factor about the selection's bottom centre, so the spacing between parts grows with them.
- Factor = the mouse's distance from the centre along the handle's on-screen direction, divided by the distance where the drag started. Steps of 1 %; with Ctrl held, 10 %. Never below 1 % of the original size.
- The Mesh panel's size and scale fields follow the drag live.

## 9. Tests (planned)
- **Round trip** (`test_project.py`, Qt-free): a scene with three parts (one rotated and scaled non-uniformly, two sharing a mesh, one with an EVO plan and one hollowed with a drain hole) saves and loads with identical transforms (≤ 1e-6), mesh vertices (≤ 1e-6 mm), face counts, names, elevations, support plans and dismissals. The shared mesh is one 3MF object.
- **Other readers:** trimesh loads the saved file and places the models where GooSlicer had them (world bounds equal to ≤ 1e-5). The `type="support"` objects are present.
- **Plain 3MF:** `samples/sphere.3mf` opens as a project with one part, no supports.
- **Robustness:** a truncated zip, a missing `project.json` entry that the `.rels` names, and a `version: 99` file each give a clear message and leave the open project untouched. A save that fails part-way leaves the old file intact.
- **GUI** (`test_gui_project.py`, Xvfb): title and `*` follow edits and undo back to clean; the prompt appears for New and Open when changed and not when clean; Open recent lists, opens and marks missing files; autosave writes after its interval (shortened for the test) and the restore dialog appears on the next start; drag-and-drop of a project onto an empty plate opens it.
- **Shortcuts:** no two default actions share a shortcut; a changed shortcut persists and Restore defaults clears it; single-key shortcuts do nothing while a spin box has focus.
- **Handles** (`test_gui_handles.py`, Xvfb, scripted mouse events as in `test_gui_viewport.py`):
  - the mode buttons, menu and W/E/R agree, and the mode survives a restart;
  - dragging the X arrow changes only X, by the amount the ray maths gives (≤ 0.01 mm); with Ctrl, a whole number of millimetres;
  - Esc mid-drag restores every selected part exactly; a finished drag is one undo step and undo restores it;
  - on a part turned 30° about Z with Uniform scale off, the Y cube changes only `scale[1]`;
  - with two parts selected only the uniform handle is hit-testable, and both parts and their spacing scale;
  - handles are not drawn or clickable in support, drain-hole or lay-flat mode;
  - a supported part keeps its plan through a handle move and gets its supports rebuilt.
- **Speed:** saving and loading the six-model scene from 04 (2.24 M triangles) each take ≤ 5 s.
- **Flatpak** (done 2026-11-24, `dist/GooSlicer.flatpak` built from f51542b, Xvfb with xcb): a script run by the sandbox's python3, in two processes:
  - A file exported with `flatpak document-export` (as the file dialog's portal grants one) gives the app a `/run/user/<uid>/doc/<id>/` path. Saving there works, including the temporary file and rename, with no `.tmp` left; the host file is the project, preview included.
  - After a restart, Open recent lists the portal path and opens it.
  - An autosave followed by a crash (`os._exit`): the next start finds it, restores the unsaved state under the project's name, removes the leftover, and a clean quit leaves the autosave folder empty.
  - Not checked: the desktop's own file dialog (interactive). Whether the portal keeps the grant across a logout is the portal's behaviour; `document-export` grants persist in its store.

## 10. Open items
- **SatelLite projects:** reading is done (10). Writing `.goo.3mf` for SatelLite to open is still open (10 §7).
- **Translations:** dropped from P7 (decided 2026-11-24). Strings stay plain English; adding `tr()` later is a mechanical pass.
- ~~Z placement~~: done (2026-11-25), the Z arrow (§8).

## 11. Layer slider in the 3D view [GOOSLICER]
For looking inside a print at a given height (internal flaws, hollow walls, infill), the 3D view has a vertical slider down its right side, under the compass.
- **What it does:** everything above the slider's height is not drawn: parts, supports, island and cavity overlays, drain-hole preview and flaw guide lines. It cuts with a GPU clip plane (`gl_ClipDistance`, world Z), so it costs nothing per move and never changes the scene, the slice or the undo history. A solid part cut open shows its inside faces in the cut colour (Preferences → 3D view colors → "Layer slider cut", default magenta, always solid; `colors/cut`), so the cut reads as a cross-section. With no cut, inside faces keep the usual magenta.
- **Range and steps:** 0 mm to the top of the highest part, in steps of the current layer height (0.05 mm with no printer chosen). Top of the slider = nothing hidden; the slider starts there. When parts change, a slider at the top stays at the top, otherwise it keeps its height, clamped to the new range.
- **Labels:** a scale of mm ticks down the left of the groove at a round interval (1, 2, 5, 10, 20 or 50 mm, so the labels are at least ~28 px apart), and the current height next to the handle, e.g. "12.35 mm" (hidden at the top position, which reads "All").
- **Hidden** when the plate is empty. Not saved between sessions.
- **Clicks** still pick parts by their full geometry; the cut is a view aid only.
- **Tests:** at a cut halfway up a box, a pixel on the part's upper half shows background and one below still shows the part; the label reads the cut height; moving the slider back to the top draws everything.
