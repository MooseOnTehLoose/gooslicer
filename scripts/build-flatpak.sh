#!/bin/sh
# Build GooSlicer as a Flatpak, install it for the current user and export a bundle.
#   scripts/build-flatpak.sh            build + install + dist/GooSlicer.flatpak
#   scripts/build-flatpak.sh --deps     also regenerate flatpak/python3-deps.json
# Needs: flatpak install --user flathub org.flatpak.Builder (runtime and SDK are installed here).
set -eu
cd "$(dirname "$0")/.."
ID=io.github.mooseontehloose.GooSlicer
M=flatpak/$ID.yml
# the sandboxed builder would ask the host flatpak about its own private user dir and miss --user installs
B="flatpak run --env=FLATPAK_USER_DIR=$HOME/.local/share/flatpak --command=flatpak-builder org.flatpak.Builder"
STATE=build/flatpak

if [ "${1:-}" = "--deps" ] || [ ! -f flatpak/python3-deps.json ]; then
    PYV=$(flatpak run --user --command=python3 org.freedesktop.Sdk//26.08 -c 'import sys; print("%d.%d" % sys.version_info[:2])')
    .venv/bin/python flatpak/gen-python-deps.py --python "$PYV"
fi

# runtime and SDK (the sandboxed builder cannot install them itself)
flatpak install --user --noninteractive --or-update flathub \
    org.freedesktop.Platform//26.08 org.freedesktop.Sdk//26.08

mkdir -p "$STATE" dist
$B --user --force-clean --ccache --disable-rofiles-fuse \
   --state-dir="$STATE/state" --repo="$STATE/repo" "$STATE/app" "$M"
flatpak build-bundle --runtime-repo=https://dl.flathub.org/repo/flathub.flatpakrepo \
   "$STATE/repo" dist/GooSlicer.flatpak "$ID"
# Install with the host flatpak, not the builder's --install: an install made inside the
# org.flatpak.Builder sandbox exports a launcher with Exec=/app/bin/flatpak, which does not
# exist on the host, so desktops hide the app.
flatpak install --user --reinstall --noninteractive dist/GooSlicer.flatpak
echo "Installed $ID; bundle: dist/GooSlicer.flatpak (run: flatpak run $ID)"
