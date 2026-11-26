"""Select by Picture (11 §2) [GOOSLICER]: MobileSAM outlines what was clicked in a render of
the part, and the outline is turned into the faces seen inside it.

MobileSAM ONNX export: huggingface.co/Acly/MobileSAM (MIT), run on the CPU with onnxruntime.
Optional: without onnxruntime or the model files `available()` is False and nothing else here
is needed.
"""
from __future__ import annotations

import os
from pathlib import Path

import numpy as np

ENCODER = "mobile_sam_image_encoder.onnx"
DECODER = "sam_mask_decoder_multi.onnx"
SIDE = 1024                     # the encoder's input: long side, pixels
WHOLE = 0.95                    # an outline covering this much of the part is "all of it"
SIZES = ("Small", "Medium", "Large")


def model_dirs() -> list[Path]:
    out = []
    if os.environ.get("GOOSLICER_MODELS"):
        out.append(Path(os.environ["GOOSLICER_MODELS"]))
    out += [Path("/app/share/gooslicer/models"),
            Path(__file__).resolve().parents[1] / "models",
            Path.home() / ".local/share/gooslicer/models"]
    return out


def model_dir() -> Path | None:
    for d in model_dirs():
        if (d / ENCODER).is_file() and (d / DECODER).is_file():
            return d
    return None


def available() -> tuple[bool, str]:
    """(usable, why not)."""
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False, "onnxruntime isn't installed"
    if model_dir() is None:
        return False, "the MobileSAM model files aren't installed (scripts/fetch-models.sh)"
    return True, ""


class Segmenter:
    """MobileSAM: `embed(image)` once per picture, then `outlines(x, y)` per click."""

    def __init__(self, folder: Path | None = None):
        import onnxruntime as ort
        d = folder or model_dir()
        if d is None:
            raise FileNotFoundError("MobileSAM model files not found")
        opt = ort.SessionOptions()
        opt.log_severity_level = 3
        cpu = ["CPUExecutionProvider"]
        self._enc = ort.InferenceSession(str(d / ENCODER), opt, providers=cpu)
        self._dec = ort.InferenceSession(str(d / DECODER), opt, providers=cpu)
        self._emb = None
        self._size = None
        self._scale = 1.0

    def embed(self, rgb: np.ndarray) -> None:
        """rgb: (H, W, 3) uint8."""
        from PIL import Image
        h, w = rgb.shape[:2]
        sc = SIDE / max(h, w)
        im = Image.fromarray(np.ascontiguousarray(rgb[:, :, :3])).resize(
            (max(1, round(w * sc)), max(1, round(h * sc))), Image.BILINEAR)
        self._emb = self._enc.run(None, {"input_image": np.asarray(im, np.float32)})[0]
        self._size, self._scale = (h, w), sc

    def outlines(self, x: float, y: float) -> list[np.ndarray]:
        """The candidate outlines (bool (H, W) each) for a click at pixel (x, y) of the
        embedded image."""
        if self._emb is None:
            raise RuntimeError("no picture embedded")
        h, w = self._size
        pc = np.array([[[x * self._scale, y * self._scale], [0.0, 0.0]]], np.float32)
        pl = np.array([[1, -1]], np.float32)            # one point; the second pads (SAM's ONNX form)
        masks, _iou, _low = self._dec.run(None, {
            "image_embeddings": self._emb, "point_coords": pc, "point_labels": pl,
            "mask_input": np.zeros((1, 1, 256, 256), np.float32),
            "has_mask_input": np.zeros(1, np.float32),
            "orig_im_size": np.array([h, w], np.float32)})
        return [m > 0 for m in masks[0]]


def candidates(outlines: list[np.ndarray], part_px: np.ndarray) -> list[np.ndarray]:
    """The outlines, cropped to the part's pixels, smallest first; empty and duplicate ones
    dropped, and one covering (nearly) all of the part dropped when others remain."""
    out, seen = [], set()
    total = max(int(part_px.sum()), 1)
    for m in outlines:
        m = m & part_px
        n = int(m.sum())
        if n == 0 or n in seen:
            continue
        seen.add(n)
        out.append(m)
    out.sort(key=lambda m: int(m.sum()))
    if len(out) > 1 and out[-1].sum() >= WHOLE * total:
        out.pop()
    return out


def pick(cands: list, size: int):
    """The candidate for an Outline position 0 (Small) .. 2 (Large)."""
    if not cands:
        return None
    return cands[(0, len(cands) // 2, len(cands) - 1)[size]]   # Medium: the middle, or the larger of two


def faces_in_outline(ids: np.ndarray, outline: np.ndarray, n_faces: int) -> np.ndarray:
    """Bool per face: some pixel of it is inside the outline. `ids`: face index per pixel (-1
    where there's no part), at an integer multiple of the outline's size."""
    fy, fx = ids.shape[0] // outline.shape[0], ids.shape[1] // outline.shape[1]
    if fy > 1 or fx > 1:
        outline = np.repeat(np.repeat(outline, fy, axis=0), fx, axis=1)
    hit = ids[:outline.shape[0], :outline.shape[1]][outline[:ids.shape[0], :ids.shape[1]]]
    hit = hit[(hit >= 0) & (hit < n_faces)]
    out = np.zeros(n_faces, bool)
    out[hit] = True
    return out
