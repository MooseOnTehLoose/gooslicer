# GooSlicer Flatpak

App ID `io.github.mooseontehloose.GooSlicer`, built on Flathub's `io.qt.PySide.BaseApp` 6.11 (KDE 6.11 runtime, Python 3.13, PySide6 6.11). x86_64 only.

## Build
```sh
flatpak install --user flathub org.flatpak.Builder     # once
scripts/build-flatpak.sh          # build, install for this user, write dist/GooSlicer.flatpak
scripts/build-flatpak.sh --deps   # same, after changing Python dependencies
flatpak run io.github.mooseontehloose.GooSlicer
```
- The script installs the KDE runtime, SDK and PySide BaseApp for the current user (about 1.7 GB, once). Build state goes to `build/flatpak/`.
- `python3-deps.json` pins every dependency wheel (URL + sha256) at the versions in `.venv`, which are the tested set. `gen-python-deps.py` regenerates it from `pyproject.toml`. The build replaces the BaseApp's own numpy with the pinned one.
- QtWebEngine/QtPdf (`BASEAPP_REMOVE_WEBENGINE`) and the shiboken generator's libclang/LLVM are removed, which takes the bundle from 215 MB to 84 MB.
- Two workarounds in the script, both caused by running `flatpak-builder` as the sandboxed `org.flatpak.Builder` app:
  - `FLATPAK_USER_DIR` is set so it finds `--user` runtimes.
  - `--disable-rofiles-fuse` is passed because FUSE can't mount in its sandbox.

## Install the bundle on another machine
```sh
flatpak install --user dist/GooSlicer.flatpak    # pulls the KDE runtime from Flathub if needed
```

## Sandbox permissions
| Permission | Why |
|---|---|
| `wayland`, `fallback-x11`, `ipc`, `dri` | window and OpenGL viewport |
| `network` | printer discovery (UDP broadcast), WebSocket control, HTTP upload |
| `~/.wine:ro` | SatelLite's printer, resin and support profiles in the default Wine install. They're copied into the app's data folder the first time, so this is only needed until then (and to refresh the copy) |

Models and `.goo` files are opened and saved through the desktop's file portal, so no other home access is needed.

If SatelLite is installed somewhere else (another Wine prefix, Bottles, a copied folder), grant read access to that folder once:
```sh
flatpak override --user --filesystem="/path/to/ELEGOO SatelLite:ro" io.github.mooseontehloose.GooSlicer
```
Then use File → Locate SatelLite profiles… and pick `resources/machines/printer/officialPlatform.ini` inside it.

Settings and data live in `~/.var/app/io.github.mooseontehloose.GooSlicer/`.
