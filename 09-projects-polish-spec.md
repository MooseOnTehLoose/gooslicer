# 09 — Projects, recent files, shortcuts and drag handles (GooSlicer P7)

Tags as in 06: **[OBSERVED]** is static RE of SatelLite; **[GOOSLICER]** marks a GooSlicer design choice where SatelLite's behaviour was not recovered or is not copied.

Status: **draft 2026-11-24**, not yet implemented.

Code (planned): `gooslicer/project.py` (Qt-free read and write), `gooslicer_qt/projectio.py` (File menu actions, unsaved-changes prompts, recent files, autosave), `gooslicer_qt/shortcuts.py` (action registry, Keyboard preferences page), `gooslicer_qt/viewport.py` (move and scale handles beside the existing rotation rings).
Tests (planned): `tests/test_project.py`, `tests/test_gui_project.py`, `tests/test_shortcuts.py`, `tests/test_gui_handles.py`.

## 1. What SatelLite does [OBSERVED strings, no sample]
- Projects are `.goo.3mf` files (`isGoo3mfProjectFileName`, "ELEGOOSatelLite.goo.3mf"). It also accepts plain `.3mf`.
- The package is written with lib3mf (`Lib3MF::CWriter`, `3D/3dmodel.model`). SatelLite's own state goes in `/Metadata/Document.xml` (a FreeCAD-style document: `PropertyMesh`, `readGoo3mfMesh`) and `/Metadata/info.json`. A `3mf-plus-meshkernel-meta` blob and a versioned "mesh map" (`getGoo3mfMeshMapVersion`) tie the 3MF meshes back to its document objects.
- Autosave goes to `AUTOSAVE-%2.goo.3mf`, and a crash leaves `crash.goo.3mf`.
- Recent projects are kept in SQLite (`projectrecent`: file path, path key, last used in ms, a PNG preview), trimmed to a limit. Recent sliced files are a separate list (`filerecent` in ElePrintHistory).
- **Not recovered:** the contents of `Document.xml` and `info.json`. No SatelLite project file exists in `samples/`. GooSlicer does not read or write SatelLite's project state (§10).

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
- **Writing:** to `<name>.3mf.tmp` beside the target, then renamed over it, so a failed or cancelled save never damages the old file. Done in a worker with progress. XML is built with vectorised formatting, aiming at ≤ 3 s for 2 M triangles.
- **Reading:** with the same code path as model import where possible. The XML is parsed with `xml.etree.ElementTree.iterparse` and nothing else from the package is executed or followed (no external references, no entities).

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
      "supports": { "template": "EVO_30-60mm", "params": { "...": "SupportParams fields" },
                    "points": [[x, y, z], ...], "normals": [[x, y, z], ...], "kinds": ["overhang", ...] } }
  ],
  "dismissed_flaws": { "island": [[x, y, z], ...], "cavity": [] },
  "view": { "...": "the 3D view's camera (yaw, pitch, distance, target)" }
}
```
- `parts[i]` matches build item `i` (and names its object id, as a check). Parts are restored in this order.
- `supports` is `SupportPlan` as stored: points and normals in the part's local frame. Parts that share a plan (Duplicate) share it again on load.
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
- **Flatpak:** paths from the file portal (`/run/user/<uid>/doc/…`) stay valid across sessions while the portal keeps the grant. To be checked in the sandbox (§9).

## 6. Autosave and crash recovery [GOOSLICER, after SatelLite's `AUTOSAVE` / `crash` files]
- Every N minutes while there are unsaved changes, the project is written in the background to `$XDG_DATA_HOME/GooSlicer/autosave/autosave.3mf` (the Flatpak's own data folder in the sandbox). N is in Preferences → Files and folders (default 5, 0 = off). It never writes over the user's own file.
- A clean quit, New, or a successful Save deletes the autosave.
- **At start**, an autosave left over means GooSlicer did not quit cleanly. A dialog offers Restore / Discard, with the time it was written. Restore opens it as an unsaved, untitled project (or titled with the original path, if the autosave recorded one, and still marked unsaved).
- An autosave never starts while a slice or another save is writing, and is skipped (not queued) when one is due during a job.

## 7. Keyboard shortcuts
- **Registry:** every main-window action gets a stable id (`file.save`, `edit.hollow`, `view.top`…) and a default shortcut, in one table in `shortcuts.py`. Menus are built from it, so shortcuts and ids can't drift apart.
- **Preferences → Keyboard** (new page): every action with its menu path, its shortcut and a `QKeySequenceEdit`. Typing a shortcut that another action uses shows which one and offers to move it. Restore defaults resets all. Stored as `shortcuts/<id>`; only changes from the default are stored.
- **Help → Keyboard shortcuts** (F1): a read-only list of the current shortcuts plus the mouse controls now in About.
- New defaults: the File items in §4; F1; W, E, R for the handle modes (§8). Existing shortcuts stay as they are.
- Single-key shortcuts (A, L, S, F, W, E, R, 1, 3, 7, Home) are window shortcuts and must not fire while a text field or spin box has focus. Checked by a test.

## 8. Drag handles in the 3D view [GOOSLICER]
Moving was dragging on the plate plus number fields, and rotation already has rings (04, P4 deviation). P7 adds handles for moving along one axis and for scaling, and a way to choose which handles are shown.

**Handle mode**
- Three modes: **Move** (W), **Rotate** (E), **Scale** (R). Only the current mode's handles are drawn. Rotate is the default, so the view looks as it does now until another mode is picked.
- Picked from Edit → Handles (radio items) and from three small buttons at the top left of the 3D view. The mode is remembered between sessions (`view/handleMode`).
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
- **No Z arrow.** Parts always rest on the plate or at their support lift height (`Part.elevation`), which Auto-support sets. A Z handle would be undone by the drop.

**Scale handles**
- **One part selected:** three cube handles on lines from the part's bounding-box centre, along the part's **own** X, Y and Z axes (turned with it), just past its surface. `Part.scale` is per local axis, so scaling along world axes would shear a rotated part.
- **Uniform:** a white cube handle at the end of a short diagonal line from the top of the bounding box. It scales all three axes by the same factor.
- When the Mesh panel's **Uniform scale** box is ticked (the default), the axis cubes also scale uniformly, so the handles and the number fields agree.
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
- **Flatpak:** in the sandbox, Save as, reopen through Open recent after a restart, and autosave restore.

## 10. Open items
- **SatelLite projects:** reading `.goo.3mf` files' own state (supports, settings) needs a sample made in SatelLite (Wine) and a decode of `Document.xml`, `info.json` and the mesh map. Until then GooSlicer opens them as plain 3MF (models only), if lib3mf's output is standard, which is likely but unverified.
- **Translations:** dropped from P7 (decided 2026-11-24). Strings stay plain English; adding `tr()` later is a mechanical pass.
- **Z placement:** a way to lift a bare part off the plate, if ever needed, would come with a Z arrow.
