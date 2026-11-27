#!/usr/bin/env bash
# Make and install PartField's ONNX file for Select by Parts (AI) (spec 11), in a throwaway
# Python environment with PyTorch (CPU). PartField is NVIDIA's, non-commercial only; see
# scripts/convert-partfield.py. Pass --flatpak to install it for the Flatpak build.
# Needs git, python3 (with venv) and about 5 GB free in ~/.cache while it runs.
set -euo pipefail
here="$(cd "$(dirname "$0")" && pwd)"
venv="${XDG_CACHE_HOME:-$HOME/.cache}/gooslicer/partfield-venv"
if [ ! -x "$venv/bin/python" ]; then
    python3 -m venv "$venv"
    "$venv/bin/pip" install -q --upgrade pip
    "$venv/bin/pip" install -q --index-url https://download.pytorch.org/whl/cpu torch
    "$venv/bin/pip" install -q numpy onnx onnxruntime yacs einops lightning boto3 loguru psutil \
        setuptools pytz h5py plyfile scikit-image trimesh
fi
"$venv/bin/python" "$here/convert-partfield.py" "$@"
echo "Done. The environment in $venv (about 1 GB) can be deleted."
