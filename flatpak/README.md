# GooSlicer Flatpak

App ID `io.github.mooseontehloose.GooSlicer`, built on Flathub's `org.freedesktop.Platform` 26.08 (glibc 2.44, Python 3.14), with Qt from the PySide6-Essentials 6.12 wheel. x86_64 only.

It was built on the KDE 6.11 runtime and PySide base until 2026-11-25. Those are on freedesktop 25.08 (glibc 2.42), too old to load ROCm built for a newer system, which Select by Picture needs for the GPU.

## Build
```sh
flatpak install --user flathub org.flatpak.Builder     # once
scripts/build-flatpak.sh          # build, install for this user, write dist/GooSlicer.flatpak
scripts/build-flatpak.sh --deps   # same, after changing Python dependencies
flatpak run io.github.mooseontehloose.GooSlicer
```
- The script installs the freedesktop runtime and SDK for the current user (once). Build state goes to `build/flatpak/`.
- `python3-deps.json` pins every dependency wheel (URL + sha256) at the versions in `.venv`, which are the tested set. `gen-python-deps.py` regenerates it from `pyproject.toml`, PySide6-Essentials and onnxruntime-migraphx included. pip installs them with `--ignore-installed`, so the SDK's own `packaging` is left alone.
- Bundle size: 106 MB with PyAV (2026-11-24), 165 MB with MobileSAM and onnxruntime, 197 MB on freedesktop 26.08 with Qt bundled (2026-11-25).
- Two workarounds in the script, both caused by running `flatpak-builder` as the sandboxed `org.flatpak.Builder` app:
  - `FLATPAK_USER_DIR` is set so it finds `--user` runtimes.
  - `--disable-rofiles-fuse` is passed because FUSE can't mount in its sandbox.

## Install the bundle on another machine
```sh
flatpak install --user dist/GooSlicer.flatpak    # pulls the freedesktop 26.08 runtime from Flathub if needed
```

## Sandbox permissions
| Permission | Why |
|---|---|
| `wayland`, `fallback-x11`, `ipc` | window |
| `device=all` | OpenGL viewport (`/dev/dri`), and `/dev/kfd`, the AMD GPU compute device for Select by Picture on the GPU. Flatpak has no narrower option for `/dev/kfd` |
| `host-os:ro` | the host's ROCm libraries, read-only, for Select by Picture on the GPU. The app loads them itself by full path (`gooslicer/gpuhost.py`), and only those the runtime doesn't have, so the host's Qt and C++ libraries never replace the runtime's |
| `network` | printer discovery (UDP broadcast), WebSocket control, HTTP upload |
| `~/.wine:ro` | SatelLite's printer, resin and support profiles in the default Wine install. They're copied into the app's data folder the first time, so this is only needed until then (and to refresh the copy) |

Models and `.goo` files are opened and saved through the desktop's file portal, so no other home access is needed.

If SatelLite is installed somewhere else (another Wine prefix, Bottles, a copied folder), grant read access to that folder once:
```sh
flatpak override --user --filesystem="/path/to/ELEGOO SatelLite:ro" io.github.mooseontehloose.GooSlicer
```
Then use File → Locate SatelLite profiles… and pick `resources/machines/printer/officialPlatform.ini` inside it.

Settings and data live in `~/.var/app/io.github.mooseontehloose.GooSlicer/`.

## GPU for Select by Picture
Preferences → Picture selection → Run on → GPU (AMD ROCm) needs ROCm 7 with MIGraphX installed on the host (`libamdhip64.so.7`, `libmigraphx_c.so.3`). It works only while the host's glibc is no newer than the runtime's (2.44): ROCm built for a newer glibc won't load, and Preferences says so. Tested on Ubuntu's ROCm 7 packages with an RX 9070 XT (2026-11-25).

To take the GPU permissions away (the viewport still needs `/dev/dri`):
```sh
flatpak override --user --nodevice=all --device=dri --nofilesystem=host-os io.github.mooseontehloose.GooSlicer
```
