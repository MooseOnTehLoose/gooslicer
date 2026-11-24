# GooSlicer

A native Linux slicer and printer controller for ELEGOO Saturn 4 MSLA resin printers (Saturn 4, Saturn 4 Ultra, Saturn 4 Ultra 16K). It replaces the Windows-only ELEGOO SatelLite workflow:

**import → orient and arrange → hollow and drill drain holes → support → slice to `.goo` → send and monitor over the LAN**

GooSlicer is an independent project. It is not affiliated with or endorsed by ELEGOO.

## Features
- **Models:** STL, OBJ and 3MF import; mesh checks and one-click repair; move, rotate (rings around the selection in 5° steps, −45° buttons or any angle), scale, mirror, add instances (right-click a model), lay flat, auto-arrange; undo/redo.
- **Hollowing:** inside, outside, centre and open-bottom shells; drain holes placed by clicking; a trapped-resin check before slicing.
- **Supports:** automatic EVO-style supports (tips, pillars, bracing, raft with holes) and tree supports, using SatelLite's template parameters; manual add/remove; an unsupported-island check (islands shown in red) before slicing.
- **Slicing:** native-resolution rasterising (non-square pixels on the 16K), parallel across all cores, `.goo` output checked against SatelLite's (a 10 mm cube is byte-identical; curved models differ by a few edge pixels per layer), plus print time, volume and cost estimates. Slices are held in memory to export or send to the printer.
- **Printer control:** discovery, live status, upload, start/pause/stop and file management over SDCP V3 (WebSocket and HTTP).

## Printer profiles
GooSlicer reads printer, resin and support profiles from **your own SatelLite installation** (`officialPlatform.ini`, resin `.data` files, `.satsup` templates). It does not ship them. On first start, use File → Locate SatelLite profiles… if they are not found in the default Wine location (`~/.wine`).

Resin profiles exported from SatelLite (`.satres`) can be added with File → Import resin profile… or by dropping the file on the window. They are copied to GooSlicer's data folder and listed under the printer they were made for. Resin → Manage resins… shows every profile's values and lets you copy (and then edit), hide, delete or export them as `.satres`.

## Install (Flatpak)
```sh
flatpak install --user flathub org.flatpak.Builder
scripts/build-flatpak.sh
flatpak run io.github.mooseontehloose.GooSlicer
```
This builds the app, installs it for the current user and writes `dist/GooSlicer.flatpak`. See [flatpak/README.md](flatpak/README.md) for permissions and installing on another machine.

## Development
Requires Python ≥ 3.12.
```sh
python3 -m venv .venv
.venv/bin/pip install -e '.[test]'
.venv/bin/gooslicer                 # run the app
.venv/bin/python -m pytest          # core tests
scripts/test-gui.sh                 # everything, including OpenGL tests under Xvfb (needs xvfb-run)
```
Command-line tools: `elegoo-goo` (inspect, extract layers from and build `.goo` files) and `elegoo-lan` (discover, upload, print).

Some tests need data that is not in this repository, and skip without it:
- `samples/`: SatelLite-sliced reference files.
- `captures/`: runtime captures from a real printer.
- `GOOSLICER_SATELLITE_DIR`: a SatelLite install folder, for the template tests.

## Layout
| Path | Contents |
|---|---|
| `gooslicer/` | Qt-free core: profiles, mesh, scene, hollowing, supports, slicer, rasteriser, estimates |
| `gooslicer_qt/` | PySide6 desktop app |
| `elegoo_goo/` | `.goo` file reader/writer |
| `elegoo_lan/` | SDCP V3 LAN client (discovery, WebSocket control, HTTP upload) |
| `tests/` | unit and GUI tests, plus a printer simulator |
| `flatpak/` | Flatpak manifest, desktop entry, metainfo, icon |
| `01-…08-*.md` | specifications, written before the code for each part |

## Specifications
The file format, network protocol and SatelLite defaults were documented by interoperability analysis of SatelLite. Every fact is tagged with how it was established. The algorithms (slicing, hollowing, supports) are GooSlicer's own.

1. [Static inventory](01-static-inventory.md)
2. [LAN / SDCP protocol](02-lan-sdcp-spec.md)
3. [`.goo` format](03-goo-format-spec.md)
4. [Desktop app plan](04-desktop-app-plan.md)
5. [Slicing](05-slicing-spec.md)
6. [Support and hollow defaults](06-support-hollow-defaults-spec.md)
7. [Hollowing and drain holes](07-hollow-drain-spec.md)
8. [Supports](08-supports-spec.md)

The specs cite evidence bundles (`evidence/`) and captures (`captures/`) that are kept out of this repository.

## License
[MIT](LICENSE). Printer, resin and support profiles stay ELEGOO's and are read from your own SatelLite install. The exception is a small test fixture in `tests/test_profiles.py`: one printer's dimensions and settings.
