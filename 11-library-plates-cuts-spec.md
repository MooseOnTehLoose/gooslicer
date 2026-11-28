# 11. Project library, build plates, planar cuts, magnet pockets [GOOSLICER]

Requested 2026-11-27. None of this comes from SatelLite (it has one plate and no library), so every rule here is GooSlicer's own; tags are left off below except where SatelLite behaviour is referred to. Decisions the user made (2026-11-27):
- versions are **explicit checkpoints** ("Save version…"); Save keeps overwriting the file;
- Arrange **adds plates** when the parts don't fit;
- deleting a plate **moves its parts** to the other plates;
- cut connectors are a **peg on one half and a socket in the other**.

## 1. Magnet pockets
A blind round pocket cut into a part's surface so a disc magnet can be glued in.
- **Panel:** Models → Magnet pocket (and Edit → Magnet pocket…) opens a floating panel like the drain-hole one (07 §3). While it is open, clicking a part cuts a pocket there, along the surface normal into the part. Done (or Esc) closes it.
- **Fields:**
  - **Magnet**: common neodymium disc sizes, diameter × thickness in mm: 2×1, 3×1, 3×2, 4×2, 4×3, 5×1, 5×2, 5×3, 6×2, 6×3, 8×2, 8×3, 10×2, 10×3, 10×5, 12×3, 12×5, 15×3, 20×3, 20×5, and **Custom**. Choosing one fills the two pocket fields.
  - **Clearance** (default 0.15 mm, 0–1): added to the radius, and twice it to the depth, when a size is chosen.
  - **Pocket diameter** (0.5–100 mm) and **Pocket depth** (0.2–50 mm). Editing either selects Custom. Defaults are the 6×3 magnet: 6.3 × 3.3 mm.
  - **Sides**: the pocket is round (50 sides, as a cylinder drain hole). The clearance is measured across the flats, as for polygon drain holes (07 §3).
- **Geometry:** a cylinder of the pocket diameter, from 0.5 mm outside the clicked surface to the pocket depth inside, along the negative face normal (the same cutter as a drain hole of the Slot form: `hollow.DrainHole` with `hole_form = "slot"`, `extend1 = 0.5`).
- **Wall check:** a ray from the clicked point along the pocket axis finds the next surface. If it is less than depth + 0.8 mm away, the pocket would break through or leave a skin under 0.8 mm. The pocket is still cut, and the Log says: "Magnet pocket: only 0.6 mm of wall left behind it (break-through risk)."
- One undo step per pocket ("Magnet pocket"). A part with supports has them rebuilt in the background, as after a drain hole. Repair after edits applies (04 P1).
- Settings are kept in QSettings `magnet/size`, `magnet/clearance`, `magnet/diameter`, `magnet/depth`.

## 2. Planar cuts
Split a part in two along a plane, optionally with a locating peg and socket.
- **Panel:** Edit → Cut… (and the part's right-click menu → Cut…) opens a floating Cut panel for the selected part. The plane is drawn in the 3D view as a translucent square across the part, with its outline, and moves as the fields change.
- **Plane:**
  - **Normal**: X, Y, Z (world axes, default Z) or **Face**. With Face, the next click on the part sets the plane through the clicked point with that face's normal.
  - **Position** (mm, along the normal): starts at the part's centre. Its range is the part's extent along the normal.
- **Connectors:**
  - **Type**: None (default), Round peg, Square peg.
  - **Size** (default 5 mm): the peg's diameter, or the side of a square peg.
  - **Height** (default 4 mm): how far the peg stands out of the cut face. The socket is that deep plus the depth clearance.
  - **Clearance** (default 0.15 mm): added to the socket's radius (half-width); the socket is 2 × clearance deeper than the peg.
  - **Count** (1–4, default 1).
  - **Peg on**: Lower half (default; the half on the negative side of the normal) or Upper half.
- **Placing the connectors:** the cut cross-section (manifold3d `slice`/`project` of the part at the plane, in the plane's own 2D frame) is inset by size/2 + clearance + 1.5 mm of wall. The first connector goes at the inset region's point farthest from its edge, each further one at the point farthest from both the edge and the connectors already placed. A connector that has no room (the inset region is empty, or its best point is closer than one size to another connector) is skipped, and the Log says how many were placed. Square pegs are aligned with the plane's 2D frame (its u axis is the world axis nearest the plane, or world X for a horizontal plane).
- **Result:** the part is replaced by two parts, "<name> A" (lower) and "<name> B" (upper). Each is a closed manifold (`Manifold.split_by_plane`). The peg is added to its half (union) and the socket cut from the other (difference), both before the halves are separated. The halves are moved apart along the normal by **Gap** (default 5 mm; each half half of it) so they can be seen, then dropped to the plate and kept on the plate they started on.
  - Each half's mesh is stored in the world frame recentred on its bounding-box centre, with rotation identity and scale 1 (the part's rotation and scale are baked in).
  - Supports: the original part's support plan doesn't apply to either half, so both halves start without supports (the Log says so when the part had them).
  - Hollowed parts: cutting through the cavity opens it, which is fine (both halves drain). Opacity carries over.
  - Undo restores the original part (one step, "Cut").
- **Errors:** a plane that misses the part (one side empty) is refused: "The plane doesn't cut <name>." A mesh that isn't a closed manifold is repaired first if Repair on import/after edits is on, otherwise refused with "Repair the mesh first".
- `gooslicer/cut.py` is Qt-free: `cut(mesh, origin, normal, CutSettings) -> (lower, upper, placed)`.

## 3. Build plates
Several plates in one project, each sliced on its own.
- **Layout:** plates sit side by side along +X. Plate *i* (from 0, shown as "Plate *i*+1") has its centre at x = *i* · pitch, y = 0, where pitch = plate width + 25 mm. Plate 1 stays where the single plate was, so older projects and every single-plate rule are unchanged.
- **Which plate a part is on:** the plate nearest the centre of its footprint (rounding x / pitch, clamped to the plates that exist). A part is **in volume** only when its whole box lies inside its plate's volume; a part straddling two plates or the gap is out of volume (drawn red, listed with ⚠, blocks slicing as today).
- **Count:** `Scene.plates` ≥ 1. Plate menu (new, between Edit and Supports):
  - **Add plate**: one more on the right.
  - **Delete plate**: the current plate (§ below); disabled with one plate. Plates to its right move left one pitch, their parts with them. Its own parts are placed on the remaining plates, first plate first, where they fit (`place_free`); no plates are added. Parts that fit nowhere are left beside the last plate, out of volume, and the Log lists them. One undo step.
  - **Current plate** ▸ Plate 1 … Plate N (radio items, also Ctrl+PgUp / Ctrl+PgDown).
  - **Arrange all plates** (the Arrange command, Ctrl+A in the toolbar sense, now does this).
- **Current plate:** the plate the slice commands, Reset view and new parts use. Selecting a part makes its plate current; the job panel shows a "Plate" selector when there is more than one. The current plate's outline is drawn brighter, and each plate has its label ("Plate 2") on the floor in front of it. The FRONT label stays in front of plate 1.
- **Spillover:**
  - **Arrange** shelf-packs every part (largest first, as today) onto plate 1, then 2, and so on, adding plates as needed. A part too big for an empty plate gets a plate to itself and is out of volume (logged). Plates left empty at the end are kept (the user can delete them).
  - **New parts** (import, Add instance, Duplicate, cut halves) go to the free spot nearest where they would land on the current plate (`place_free`); if none, the first later plate with room; if none, a new plate is added and the part placed there. The Log says "<name>: no room on plate 2, placed on plate 3 (added)".
- **Slicing:** "Slice", "Slice and save .goo…" and "Slice and send…" slice the **current plate**: only its parts and their supports, moved by −*i* · pitch in x so the plate's centre is the printer's centre. The held slice remembers its plate (a different current plate makes it stale). File → **Slice all plates…** asks for a base name and writes `<name>-plate1.goo`, `<name>-plate2.goo`, … one after another, skipping empty plates. Island and cavity checks run over all plates, as before.
- **View:** Reset view and Home frame the current plate; View → **All plates** frames them all.
- **Projects:** `project.json` gains `"plates": N` and `"current_plate": i` (left out when 1 and 0). Older files load with one plate. Other 3MF readers see all parts in their world positions, beside each other.
- **Undo:** adding, deleting and arranging are undo steps; the plate count is part of them.

## 4. Project library
A Fusion 360–style place to keep work: **library → projects → designs → versions**.
- **Where:** a folder, `~/GooSlicer Library` by default (Preferences → Files and folders → "Library", setting `library/root`). The Flatpak gets `--filesystem=~/GooSlicer Library:create` so it can use the default; another root needs `flatpak override --filesystem=…`.
- **On disk:**
  ```
  <root>/<Project>/project.json          {"format": "gooslicer-library-project", "version": 1,
                                           "name", "description", "created",
                                           "designs": [{"file": "Flexible parts.3mf", "status": "draft", "notes": ""}, …]}
  <root>/<Project>/<Design>.3mf          an ordinary GooSlicer project (09 §2), its own printer, resin and settings
  <root>/<Project>/.versions/<Design>/v<N>.3mf
  <root>/<Project>/.versions/<Design>/versions.json
                                         [{"n": 1, "note": "before cutting the base", "saved": "…Z", "app": "…"}, …]
  ```
  A design is a plain `.3mf`, so it can be opened, copied or shared without the library. Designs on disk that `project.json` doesn't list are shown too (added to it on the next change). Names are used as file names: `/`, `\`, NUL and leading dots are refused.
- **Why several designs:** one project can need several resins (flexible parts in one design, clear parts in another), each a separate plate set with its own resin and settings, as the user asked.
- **Library window** (File → Project library…, Ctrl+L; non-modal):
  - **Projects** (left): New project, Rename, Delete. Delete moves the folder to the desktop trash (`QFile.moveToTrash`), after a confirmation naming how many designs and versions it holds.
  - **Designs** (middle, the selected project's): each with its thumbnail (the 3MF's), name, printer and resin (read from its `project.json` entry only, without loading meshes), status, last saved and version count. Buttons: New design (an empty project saved into the project, then opened), Add current (save the open project into this project as a new design), Open, Rename, Duplicate, Delete (to trash, with its versions). Status: Draft, Ready, Printed (a combo), and Notes (a text field), saved in `project.json`.
  - **Versions** (right, the selected design's): newest first, "V3 · 27 Nov 2026 14:02 · note". **Open** opens the version as an unsaved copy titled "<Design> (V3)" (Save writes it over the design, after the usual prompt); **Restore** first saves the design's current state as a new version "Before restoring V3" when it differs from every version, then copies the version over the design and opens it; **Delete version** removes it (numbers are never reused).
  - Double-clicking a design opens it, through the unsaved-changes prompt (09 §4).
- **Save version…** (File, Ctrl+Alt+S): saves the open project to its file, then copies the file to the next `v<N>.3mf` with a note asked for in a small dialog. For a project outside the library it offers to add it first: pick (or create) a library project and a design name; the file is then saved there and the library copy becomes the open project. Versions are full copies of the 3MF (no deltas), so the window shows each project's size on disk.
- **Title bar** for a library design: `Design — Project — GooSlicer` (with `*` when modified).
- Recent files and autosave work as for any project (09 §5–6).
- `gooslicer/library.py` is Qt-free (folder layout, JSON, versions); `gooslicer_qt/library.py` is the window.

## 5. Tests
- Magnets: pocket volume ≈ π r² d on a box; the wall warning on a thin plate; settings round-trip; the panel cuts at a click (GUI).
- Cuts: a cube cut at z = 0 gives two closed halves whose volumes add up; round and square peg/socket volumes and fit (peg inside socket with the clearance); connectors skipped when the cross-section is too small; Face normal; undo restores the part (GUI).
- Plates: plate membership and in-volume across plates; Arrange spills and adds plates; delete moves parts and shifts plates; the slice job of plate 2 is centred; projects round-trip `plates`; import spillover adds a plate (GUI).
- Library: create project and designs, list designs written outside the library, versions (save, open, restore with the safety version, delete without number reuse), names refused, trash on delete (GUI).

## 6. Open items
- Versions are whole copies; a 3MF is mostly mesh, so 10 versions of a 20 MB design take 200 MB. Deltas or shared meshes would need a different store.
- Cut connectors are placed automatically; dragging them on the cut face is not implemented.
- Multi-plate slicing writes one .goo per plate; there is no queue to the printer.
