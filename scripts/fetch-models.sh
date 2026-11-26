#!/bin/sh
# Download the MobileSAM model files for Select by Picture (spec 11) into models/ (git-ignored),
# for running from the source tree. The Flatpak downloads its own copy at build time.
# MobileSAM ONNX export by Acly (MIT): https://huggingface.co/Acly/MobileSAM
set -eu
cd "$(dirname "$0")/.."
REV=0d3b403339b4674a82493d5e97964dd78089ddc8
mkdir -p models
fetch() {
    if [ -f "models/$1" ] && echo "$2  models/$1" | sha256sum -c --quiet 2>/dev/null; then
        echo "models/$1: already there"
        return
    fi
    curl -fL --retry 3 -o "models/$1.part" "https://huggingface.co/Acly/MobileSAM/resolve/$REV/$1"
    echo "$2  models/$1.part" | sha256sum -c --quiet
    mv "models/$1.part" "models/$1"
    echo "models/$1: downloaded"
}
fetch mobile_sam_image_encoder.onnx 580f5fb648ea1062c0aabc26217aed56921985f03f0cbbd852bba81d760cc749
fetch sam_mask_decoder_multi.onnx 8976b90a87ba50a6a72217a5ff994f7d25ce16f2229fcc1ed259e1294c622ffe
