"""Select by Picture (11 §2) [GOOSLICER]: an image model ("segment anything") outlines what was
clicked in a render of the part, and the outline is turned into the faces seen inside it.

Models (all run with onnxruntime):
- MobileSAM, ONNX export by Acly (MIT): small and fast; the Flatpak includes it.
- SAM 2.1 Hiera Small / Base+ (Meta, Apache-2.0), ONNX export by onnx-community: more precise on
  small features (a strap, a ring, a nose); downloaded on request from Preferences.
Optional: without onnxruntime or a model's files that model isn't available, and nothing else
here is needed.
"""
from __future__ import annotations

import hashlib
import os
import urllib.request
from dataclasses import dataclass
from pathlib import Path

import numpy as np

SIDE = 1024                     # the encoders' input: long side (SAM 1) / both sides (SAM 2), px
WHOLE = 0.95                    # an outline covering this much of the part is "all of it"
SIZES = ("Small", "Medium", "Large")
GPU_PROVIDERS = ("MIGraphXExecutionProvider", "ROCMExecutionProvider", "CUDAExecutionProvider")


@dataclass(frozen=True)
class ModelFile:
    name: str
    size: int
    sha256: str
    url: str = ""                  # empty: not downloadable from the app (installed with it)


@dataclass(frozen=True)
class Model:
    key: str
    title: str
    kind: str                      # "sam1" (MobileSAM's ONNX form) | "sam2" (onnx-community's)
    #                                | "sam2x" (samexporter's SAM 2 form)
    folder: str                    # under a model directory
    files: tuple
    encoder: str
    decoder: str
    note: str
    gpu: bool = True               # False: the encoder stays on the CPU (it fails on the GPU)

    @property
    def size(self) -> int:
        return sum(f.size for f in self.files)


_HF2S = "https://huggingface.co/onnx-community/sam2.1-hiera-small-ONNX/resolve/a7df49d8de14b9d2e4504d1687b0d568f905fd8d/onnx/"
_HF2B = "https://huggingface.co/onnx-community/sam2.1-hiera-base-plus-ONNX/resolve/bab18593f44e652f04cf18b60b3690f60e8996b0/onnx/"
MODELS = {m.key: m for m in (
    Model("mobilesam", "MobileSAM (fast, included)", "sam1", "", (
        ModelFile("mobile_sam_image_encoder.onnx", 28157093,
                  "580f5fb648ea1062c0aabc26217aed56921985f03f0cbbd852bba81d760cc749"),
        ModelFile("sam_mask_decoder_multi.onnx", 16496559,
                  "8976b90a87ba50a6a72217a5ff994f7d25ce16f2229fcc1ed259e1294c622ffe")),
        "mobile_sam_image_encoder.onnx", "sam_mask_decoder_multi.onnx",
        "MobileSAM by Chaoning Zhang et al.; ONNX export by Acly (MIT)"),
    Model("sam2-small", "SAM 2.1 Small (more precise)", "sam2", "sam2.1-hiera-small", (
        ModelFile("vision_encoder.onnx", 467440,
                  "aacf1f7137bb6fffcf6bf166abcfabe28f57a76059254f3fb611c4a64a208119", _HF2S + "vision_encoder.onnx"),
        ModelFile("vision_encoder.onnx_data", 162476288,
                  "260fd1f0a34e72a3dc79a739e563b4facc0ba75504818b433a1f808e66637456", _HF2S + "vision_encoder.onnx_data"),
        ModelFile("prompt_encoder_mask_decoder.onnx", 213114,
                  "079c59b261f723ff5c6a125e69b0170a957b21c58738c28d2b0394ecd0587d7f",
                  _HF2S + "prompt_encoder_mask_decoder.onnx"),
        ModelFile("prompt_encoder_mask_decoder.onnx_data", 20958208,
                  "f9e59a584ab8ced21fa812c211bc01084204db1c9e92a5ef4fb3a49972b4e864",
                  _HF2S + "prompt_encoder_mask_decoder.onnx_data")),
        "vision_encoder.onnx", "prompt_encoder_mask_decoder.onnx",
        "SAM 2.1 by Meta (Apache-2.0); ONNX export by onnx-community"),
    Model("sam2-base", "SAM 2.1 Base+ (most precise, slowest)", "sam2", "sam2.1-hiera-base-plus", (
        ModelFile("vision_encoder.onnx", 702836,
                  "6fd992cf796e6ad5a29f3b92a127f2ce33f7f320d63c8b3f02bf3775f5f692f4", _HF2B + "vision_encoder.onnx"),
        ModelFile("vision_encoder.onnx_data", 305746496,
                  "f8a5f1b14d02eb7ed3b3576ad6774b8ebcfef6cc20674d5f316d6144c60a08a5", _HF2B + "vision_encoder.onnx_data"),
        ModelFile("prompt_encoder_mask_decoder.onnx", 213114,
                  "f39eeec20243ed1c8f2cd013812e77813d937ddbc800fa4bc703761adc7e63cd",
                  _HF2B + "prompt_encoder_mask_decoder.onnx"),
        ModelFile("prompt_encoder_mask_decoder.onnx_data", 20958208,
                  "445cd3f72a218815db10e336f4f1c46a6eb2713a0160a85af5365134607f32a7",
                  _HF2B + "prompt_encoder_mask_decoder.onnx_data")),
        "vision_encoder.onnx", "prompt_encoder_mask_decoder.onnx",
        "SAM 2.1 by Meta (Apache-2.0); ONNX export by onnx-community"),
)}
# SAM 2.1 Large and HQ-SAM 2 share one image encoder (HQ-SAM 2 keeps SAM 2.1 Large's frozen), so
# they share a folder and the 852 MB file is downloaded once
_LL = "https://huggingface.co/latolukasz/sam2.1-hiera-large-onnx/resolve/ccbb4af967bbb9dcf2c40472f6d97c930e35910a/"
_LHQ = "https://huggingface.co/latolukasz/sam2.1-hq-hiera-large-onnx/resolve/5d5e61bc0f0b61247e2cf9839872d2920a000727/"
_LARGE_ENC = ModelFile("encoder.onnx", 852451097,
                       "f18538735868c076b715d5abbdf1a9d5fb4b0939c00be9db42d6b3808cdd3251", _LL + "encoder.onnx")
MODELS.update({m.key: m for m in (
    Model("sam2-large", "SAM 2.1 Large (most precise, slow)", "sam2x", "sam2.1-hiera-large", (
        _LARGE_ENC,
        ModelFile("decoder.onnx", 16519627,
                  "014ac31e9037238ee027d6399d932dafe80c15ae711aee564976b94383b4ffa1", _LL + "decoder.onnx")),
        "encoder.onnx", "decoder.onnx",
        "SAM 2.1 by Meta (Apache-2.0); ONNX export by vietanhdev (samexporter), as published by latolukasz",
        False),
    Model("sam2-hq", "HQ-SAM 2 Large (finest edges, slow)", "sam2x", "sam2.1-hiera-large", (
        _LARGE_ENC,
        ModelFile("decoder_hq.onnx", 17238498,
                  "4cace48837e9923863d7ee21f775c60b35b49ac3a2eb2afb6cb68aa6ae8ff7f9", _LHQ + "decoder_hq.onnx")),
        "encoder.onnx", "decoder_hq.onnx",
        "HQ-SAM 2 by SysCV (Apache-2.0), on SAM 2.1 Large by Meta; ONNX export by latolukasz", False),
)})
DEFAULT_MODEL = "mobilesam"


def user_dir() -> Path:
    """Where downloaded models go (in the Flatpak: its own data folder)."""
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
    return Path(base) / "gooslicer/models"


def model_dirs() -> list[Path]:
    out = []
    if os.environ.get("GOOSLICER_MODELS"):
        out.append(Path(os.environ["GOOSLICER_MODELS"]))
    out += [Path("/app/share/gooslicer/models"),
            Path(__file__).resolve().parents[1] / "models",
            user_dir()]
    return out


def model_dir(key="mobilesam") -> Path | None:
    """The folder holding all of a model's files, or None. `key`: a key of MODELS, or a Model."""
    m = key if isinstance(key, Model) else MODELS[key]
    for d in model_dirs():
        d = d / m.folder if m.folder else d
        if all((d / f.name).is_file() for f in m.files):
            return d
    return None


def runtime_ok() -> tuple[bool, str]:
    try:
        import onnxruntime  # noqa: F401
    except Exception:
        return False, "onnxruntime isn't installed"
    return True, ""


def available(key: str = DEFAULT_MODEL) -> tuple[bool, str]:
    """(usable, why not)."""
    ok, why = runtime_ok()
    if not ok:
        return ok, why
    if model_dir(key) is None:
        return False, (f"{MODELS[key].title} isn't downloaded (Preferences → Picture selection)"
                       if MODELS[key].files[0].url else
                       "the MobileSAM model files aren't installed (scripts/fetch-models.sh)")
    return True, ""


def any_available() -> tuple[bool, str]:
    ok, why = runtime_ok()
    if not ok:
        return ok, why
    if any(model_dir(k) for k in MODELS):
        return True, ""
    return available(DEFAULT_MODEL)


def download(key, progress=None, dest: Path | None = None) -> Path:
    """Download a model's files into the user model folder, each checked against its sha256
    (a bad or partial file is never left under its real name). progress(done, total) bytes."""
    m = key if isinstance(key, Model) else MODELS[key]
    if not all(f.url for f in m.files):
        raise ValueError(f"{m.title} can't be downloaded")
    d = (dest or user_dir()) / m.folder
    d.mkdir(parents=True, exist_ok=True)
    done = 0
    for f in m.files:
        out = d / f.name
        if out.is_file() and out.stat().st_size == f.size and _sha256(out) == f.sha256:
            done += f.size
            if progress:
                progress(done, m.size)
            continue
        part = d / (f.name + ".part")
        h = hashlib.sha256()
        req = urllib.request.Request(f.url, headers={"User-Agent": "GooSlicer"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as fo:
            while True:
                b = r.read(1 << 20)
                if not b:
                    break
                fo.write(b)
                h.update(b)
                done += len(b)
                if progress:
                    progress(done, m.size)
        if h.hexdigest() != f.sha256:
            part.unlink(missing_ok=True)
            raise OSError(f"{f.name}: the download doesn't match its checksum")
        part.replace(out)
    return d


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


GPU_LIBS = {"MIGraphXExecutionProvider": ("libamdhip64.so.7", "libmigraphx_c.so.3"),
            "ROCMExecutionProvider": ("libamdhip64.so.7", "libMIOpen.so.1"),
            "CUDAExecutionProvider": ("libcudart.so.12",)}


def gpu_status() -> tuple[str | None, str]:
    """(the GPU execution provider that can start here, or None; why not). The provider must
    be in this onnxruntime build (AMD: MIGraphX or ROCm) and the GPU's own libraries must load
    (ROCm, installed with the system)."""
    import ctypes
    try:
        import onnxruntime as ort
        have = ort.get_available_providers()
    except Exception:
        return None, "onnxruntime isn't installed"
    prov = next((p for p in GPU_PROVIDERS if p in have), None)
    if prov is None:
        return None, "the installed onnxruntime has no GPU support"
    from . import gpuhost
    if prov in ("MIGraphXExecutionProvider", "ROCMExecutionProvider") and gpuhost.in_flatpak():
        why = gpuhost.preload()                    # the host's ROCm, from inside the Flatpak
        if why:
            return None, why
    for lib in GPU_LIBS.get(prov, ()):
        try:
            ctypes.CDLL(lib)
        except OSError:
            return None, f"{lib} (ROCm) can't be loaded"
    return prov, ""


def gpu_provider() -> str | None:
    """The GPU execution provider that can start here, or None (see gpu_status)."""
    return gpu_status()[0]


def gpu_cache_dir() -> Path:
    base = os.environ.get("XDG_CACHE_HOME") or str(Path.home() / ".cache")
    return Path(base) / "gooslicer/gpu"


class Segmenter:
    """A model: `embed(image)` once per picture, then `outlines(points)` per click. `device` is
    "cpu" or "gpu"; `self.device` says what it actually runs on (a GPU that fails to start falls
    back to the CPU, and `self.gpu_error` says why)."""

    def __init__(self, key: str = DEFAULT_MODEL, device: str = "cpu", folder: Path | None = None):
        import onnxruntime as ort
        self.model = MODELS[key]
        d = folder or model_dir(key)
        if d is None:
            raise FileNotFoundError(available(key)[1])
        opt = ort.SessionOptions()
        opt.log_severity_level = 3
        self.device, self.gpu_error = "cpu", None
        gpu = gpu_provider() if device == "gpu" else None
        if device == "gpu" and gpu is None:
            self.gpu_error = "this onnxruntime has no GPU support"
        if gpu and not self.model.gpu:
            # MIGraphX segfaults compiling SAM 2.1 Large's encoder as exported, or (optimised
            # first) gives NaN no faster than the CPU
            gpu, self.gpu_error = None, f"{self.model.title} runs only on the CPU"
        self._enc = self._dec = None
        eopt = opt
        if self.model.kind == "sam2x":           # its encoder gives NaN once onnxruntime optimises it
            eopt = ort.SessionOptions()
            eopt.log_severity_level = 3
            eopt.graph_optimization_level = ort.GraphOptimizationLevel.ORT_DISABLE_ALL
        if gpu:
            po = {}
            if gpu == "MIGraphXExecutionProvider":     # compiling for the GPU takes minutes: keep it
                gpu_cache_dir().mkdir(parents=True, exist_ok=True)
                po = {"migraphx_model_cache_dir": str(gpu_cache_dir())}
            try:
                prov = [(gpu, po), "CPUExecutionProvider"]
                self._enc = ort.InferenceSession(str(d / self.model.encoder), eopt, providers=prov)
                if gpu not in self._enc.get_providers():
                    raise RuntimeError("the GPU didn't start (its libraries or device aren't reachable)")
                self.device = "gpu"
            except Exception as e:
                self.gpu_error = str(e).splitlines()[0] if str(e) else type(e).__name__
                self._enc = self._dec = None
        cpu = ["CPUExecutionProvider"]
        if self._enc is None:
            self._enc = ort.InferenceSession(str(d / self.model.encoder), eopt, providers=cpu)
        # the decoder stays on the CPU: it takes 0.03 s there, and MIGraphX can't compile SAM 2.1's
        # (its empty "boxes" input fails to parse)
        self._dec = ort.InferenceSession(str(d / self.model.decoder), opt, providers=cpu)
        self._enc_path, self._enc_opt = str(d / self.model.encoder), eopt
        self._emb = None
        self._size = None

    def embed(self, rgb: np.ndarray):
        """rgb: (H, W, 3) uint8. Returns the embedding (pass it to `outlines` to use one other
        than the latest)."""
        from PIL import Image
        h, w = rgb.shape[:2]
        img = Image.fromarray(np.ascontiguousarray(rgb[:, :, :3]))
        if self.model.kind == "sam1":
            sc = SIDE / max(h, w)
            im = img.resize((max(1, round(w * sc)), max(1, round(h * sc))), Image.BILINEAR)
            # padded to a fixed 1024², with the colour the model's own padding stands for (its
            # mean), so a GPU compiles it once rather than for every window size
            pad = np.empty((SIDE, SIDE, 3), np.float32)
            pad[:] = (123.675, 116.28, 103.53)
            pad[:im.height, :im.width] = np.asarray(im, np.float32)
            out = self._enc.run(None, {"input_image": pad})
        else:
            im = np.asarray(img.resize((SIDE, SIDE), Image.BILINEAR), np.float32) / 255.0
            im = ((im - (0.485, 0.456, 0.406)) / (0.229, 0.224, 0.225)).transpose(2, 0, 1)[None]
            name = "pixel_values" if self.model.kind == "sam2" else "image"
            out = self._enc.run(None, {name: im.astype(np.float32)})
            if self.model.kind == "sam2x":
                out = dict(zip((o.name for o in self._enc.get_outputs()), out))
        if self.device == "gpu" and any(np.isnan(a).any() for a in (out.values() if isinstance(out, dict) else out)):
            # a GPU that computes garbage: this model goes back to the CPU for good
            import onnxruntime as ort
            self.device, self.gpu_error = "cpu", "the GPU gave no numbers (NaN) for this model"
            self._enc = ort.InferenceSession(self._enc_path, self._enc_opt, providers=["CPUExecutionProvider"])
            return self.embed(rgb)
        self._emb, self._size = out, (h, w)
        return out, (h, w)

    def outlines(self, points, emb=None, size=None, box=None) -> list[np.ndarray]:
        """Candidate outlines (bool (H, W) each) for points [(x, y, include), …] in pixels of
        the embedded image: the first point and any more included ones mark the thing, excluded
        ones mark what isn't part of it. `emb`: an `embed` result (default: the latest).
        `size`: (h, w) to return them at instead of the picture's own. `box`: (x0, y0, x1, y1)
        in picture pixels round the thing (SAM's box prompt)."""
        if emb is None:
            if self._emb is None:
                raise RuntimeError("no picture embedded")
            emb = (self._emb, self._size)
        e, (h, w) = emb
        oh, ow = size or (h, w)
        pts = np.array([(x, y) for x, y, _ in points], np.float32).reshape(-1, 2)
        lab = np.array([1 if inc else 0 for _x, _y, inc in points])
        bx = None if box is None else np.array([[box[0], box[1]], [box[2], box[3]]], np.float32)
        if self.model.kind == "sam1":
            sc = SIDE / max(h, w)
            if bx is None:      # a padding point (SAM's ONNX form); a box is two corner points
                pc = np.r_[pts * sc, [[0.0, 0.0]]][None].astype(np.float32)
                pl = np.r_[lab, -1][None].astype(np.float32)
            else:
                pc = np.r_[pts * sc, bx * sc][None].astype(np.float32)
                pl = np.r_[lab, 2, 3][None].astype(np.float32)
            masks, _iou, _low = self._dec.run(None, {
                "image_embeddings": e[0], "point_coords": pc, "point_labels": pl,
                "mask_input": np.zeros((1, 1, 256, 256), np.float32),
                "has_mask_input": np.zeros(1, np.float32),
                "orig_im_size": np.array([oh, ow], np.float32)})
            return [m > 0 for m in masks[0]]
        from PIL import Image
        if self.model.kind == "sam2x":
            sc2 = np.array([SIDE / w, SIDE / h], np.float32)
            if bx is None:                              # a padding point, as for SAM 1
                pc = np.r_[pts * sc2, [[0.0, 0.0]]][None].astype(np.float32)
                pl = np.r_[lab, -1][None].astype(np.float32)
            else:
                pc = np.r_[pts * sc2, bx * sc2][None].astype(np.float32)
                pl = np.r_[lab, 2, 3][None].astype(np.float32)
            logits = self._dec.run(None, {
                "image_embed": e["image_embed"], "high_res_feats_0": e["high_res_feats_0"],
                "high_res_feats_1": e["high_res_feats_1"], "point_coords": pc, "point_labels": pl,
                "mask_input": np.zeros((1, 1, 256, 256), np.float32),
                "has_mask_input": np.zeros(1, np.float32)})[0][0]
        else:
            pc = (pts * (SIDE / w, SIDE / h))[None, None].astype(np.float32)
            out = self._dec.run(None, {
                "input_points": pc, "input_labels": lab[None, None].astype(np.int64),
                "input_boxes": (np.zeros((1, 0, 4), np.float32) if bx is None else
                                (bx * (SIDE / w, SIDE / h)).reshape(1, 1, 4).astype(np.float32)),
                "image_embeddings.0": e[0], "image_embeddings.1": e[1],
                "image_embeddings.2": e[2]})
            logits = out[1][0, 0]                  # (3, 256, 256), stretched over the picture
        return [np.asarray(Image.fromarray(m.astype(np.float32)).resize((ow, oh), Image.BILINEAR)) > 0
                for m in logits]


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


SPECK = 0.02                    # bits of an outline under this share of it (or 1 mm²) are specks


def drop_specks(s, mask: np.ndarray, keep_faces=()) -> np.ndarray:
    """The outline's faces without its small loose bits (pixels the model got wrong; a strap
    seen in two places stays, being big): pieces connected through the mesh, under SPECK of the
    whole or 1 mm², go unless they hold a clicked face. `s`: modify.Surface."""
    import scipy.sparse as sp
    from scipy.sparse import csgraph
    if not mask.any():
        return mask
    a = s.adjacency
    e = a[mask[a[:, 0]] & mask[a[:, 1]]]
    n = len(mask)
    g = sp.coo_matrix((np.ones(len(e), np.int8), (e[:, 0], e[:, 1])), shape=(n, n))
    _, lab = csgraph.connected_components(g, directed=False)
    area = np.bincount(lab[mask], s.areas[mask], minlength=lab.max() + 1)
    keep = area >= max(1.0, SPECK * area.sum())
    keep[lab[list(keep_faces)]] = True
    return mask & keep[lab]



# ------------------------------------------------------------------ more views (11, Picture)
VIEWS = 3                       # default for Preferences → Views (the clicked one included)
MAX_VIEWS = 9
# (turn round the vertical, tilt) in degrees from the clicked view, in the order they're used
VIEW_TURNS = ((0, 0), (45, 0), (-45, 0), (0, 35), (0, -35), (90, 0), (-90, 0), (45, 35), (-45, 35))
FILL = 0.5                      # the largest candidate's bounding sphere fills this much of a view
SEEN = 0.1                      # a view must see this share of a candidate's area to vote on it
AGREE = 0.3                     # …and outline something overlapping it at least this much
INSIDE_POINTS = 3               # prompt points deep inside the candidate, per view
VOTE_SIDE = 800                 # px: the vote works on face pictures cut down to about this (the
#                                 model's outlines are 256² scaled up, so nothing finer is there)


def seen_faces(ids: np.ndarray, n_faces: int) -> np.ndarray:
    """Bool per face: some pixel of it is in the picture."""
    v = ids[(ids >= 0) & (ids < n_faces)]
    out = np.zeros(n_faces, bool)
    out[v] = True
    return out


def face_points(ids: np.ndarray, faces) -> list[tuple[float, float]]:
    """The middle (mean pixel) of each of `faces` that the picture sees."""
    out = []
    for f in faces:
        ys, xs = np.nonzero(ids == f)
        if len(xs):
            out.append((float(xs.mean()), float(ys.mean())))
    return out


def inside_points(px: np.ndarray, k: int = INSIDE_POINTS) -> list[tuple[float, float]]:
    """Up to k points deep inside a pixel mask, spread apart: each the farthest from the mask's
    edge, outside a circle round the ones before (half the deepest depth, 4 px at least)."""
    from scipy import ndimage
    if not px.any():
        return []
    dist = ndimage.distance_transform_edt(px)
    r = max(0.5 * float(dist.max()), 4.0)
    R = int(np.ceil(r))
    yy, xx = np.mgrid[-R:R + 1, -R:R + 1]
    disc = yy ** 2 + xx ** 2 <= r * r
    H, W = px.shape
    out = []
    for _ in range(k):
        i = int(np.argmax(dist))
        y, x = divmod(i, W)
        if dist[y, x] <= 0:
            break
        out.append((float(x), float(y)))
        y0, y1, x0, x1 = max(y - R, 0), min(y + R + 1, H), max(x - R, 0), min(x + R + 1, W)
        win = dist[y0:y1, x0:x1]
        win[disc[y0 - y + R:y1 - y + R, x0 - x + R:x1 - x + R]] = 0
    return out


def refine(seg, views: list[dict], cands: list[np.ndarray], ids0: np.ndarray, areas: np.ndarray,
           faces_in=(), faces_out=()) -> list[np.ndarray]:
    """More views: each candidate (a face mask from the clicked view, whose face-index picture
    is `ids0`) voted on by the model's outlines in other views. `views`: [{"rgb", "ids"}, …];
    the first time each gets its "emb", and its "ids" are cut down to about VOTE_SIDE px
    ("small", every k-th pixel), so a later call only decodes. A face is kept where it's inside
    the outline in at least half the used views that see it."""
    n = len(areas)
    seen0 = seen_faces(ids0, n)
    for v in views:
        _prepare(seg, v, n)
    out = []
    for c in cands:
        votes = (c & seen0).astype(np.float32)
        count = seen0.astype(np.float32)
        total = areas[c].sum()
        for v in views:
            ids, sv, k = v["small"], v["seen"], v["k"]
            common = seen0 & sv
            if total <= 0 or areas[c & sv].sum() < SEEN * total:
                continue
            valid = (ids >= 0) & (ids < n)
            cpx = np.zeros(ids.shape, bool)
            cpx[valid] = c[ids[valid]]
            pts = [(x, y, True) for x, y in face_points(ids, faces_in)]
            pts += [(2 * x + 0.5, 2 * y + 0.5, True) for x, y in inside_points(cpx[::2, ::2])]
            pts += [(x, y, False) for x, y in face_points(ids, faces_out)]
            if not any(p[2] for p in pts):
                continue
            pts = [(k * x + (k - 1) / 2, k * y + (k - 1) / 2, inc) for x, y, inc in pts]
            outs = seg.outlines(pts, emb=_unpack(v["emb"]), size=ids.shape)
            best, score = None, AGREE
            for o in candidates(outs, valid):
                f = faces_in_outline(ids, o, n)
                union = areas[(f | c) & common].sum()
                iou = areas[f & c & common].sum() / union if union > 0 else 0.0
                if iou >= score:
                    best, score = f, iou
            if best is None:
                continue
            votes += best & sv
            count += sv
        out.append((count > 0) & (votes >= 0.5 * count))
    return out


# ------------------------------------------------------------------ full turn (11, Picture)
TURN_STEPS = 12                 # pictures per ring round the model (30° apart)
TURN_FILL = 0.8                 # the part's bounding sphere fills this much of each picture
TURN_PITCH = (20.0, 45.0)       # the rings are this far (degrees) above and below the middle
HEAD_ON = 0.5                   # a face seen at least this head-on (cos) can prompt later views


class Cancelled(Exception):
    """The user stopped a full turn."""


def turn_offsets(n: int = TURN_STEPS) -> list[float]:
    """Turns (degrees) from the clicked view, nearest first both ways: 30, -30, 60, … 180."""
    step = 360.0 / n
    out = []
    for i in range(1, n // 2 + 1):
        out.append(i * step)
        if i * step < 180.0:
            out.append(-i * step)
    return out[:n - 1]


def turn_layout(pitch: float, n: int = TURN_STEPS) -> list[tuple[float, float]]:
    """(turn, pitch) of each full-turn picture, nearest the clicked view first: two rings, above
    and below the middle (by the clicked view's own pitch, kept within TURN_PITCH), the lower
    one starting straight below the click."""
    p = float(np.clip(abs(pitch), *TURN_PITCH))
    up = p if pitch >= 0 else -p
    out = [(0.0, -up)]
    for o in turn_offsets(n):
        out += [(o, up), (o, -up)]
    return out


def facing(s, eye: np.ndarray) -> np.ndarray:
    """How directly a camera at `eye` sees each face (cos, 0 when edge-on or behind).
    `s`: modify.Surface."""
    d = eye - s.centers
    d /= np.maximum(np.linalg.norm(d, axis=1, keepdims=True), 1e-12)
    return np.clip(np.einsum("ij,ij->i", s.normals, d), 0.0, 1.0)


def _pack(emb):
    """An `embed` result at half precision (kept for later decoding); anything else as it is."""
    if not (isinstance(emb, tuple) and len(emb) == 2 and isinstance(emb[0], (list, dict))):
        return emb
    e, size = emb
    if isinstance(e, dict):
        return {k: v.astype(np.float16) for k, v in e.items()}, size
    return [a.astype(np.float16) for a in e], size


def _unpack(emb):
    if not (isinstance(emb, tuple) and len(emb) == 2 and isinstance(emb[0], (list, dict))):
        return emb
    e, size = emb
    if isinstance(e, dict):
        return {k: v.astype(np.float32) for k, v in e.items()}, size
    return [a.astype(np.float32) for a in e], size


def _prepare(seg, v: dict, n: int) -> None:
    """Embed a view the first time: its picture becomes "emb" (half precision) and its face
    picture "small" (every k-th pixel, about VOTE_SIDE px)."""
    if "emb" in v:
        return
    v["emb"] = _pack(seg.embed(v.pop("rgb")))
    k = max(1, int(round(max(v["ids"].shape) / VOTE_SIDE)))
    v["k"], v["small"] = k, np.ascontiguousarray(v.pop("ids")[::k, ::k])
    v["seen"] = seen_faces(v["small"], n)


def propagate(seg, views: list[dict], mask0: np.ndarray, ids0: np.ndarray, eye0: np.ndarray, s,
              faces_in=(), faces_out=(), progress=None, cancelled=None) -> np.ndarray:
    """Full turn: carry an outline (`mask0`, from the clicked view: face-index picture `ids0`,
    camera at `eye0`) round the model. `views`: [{"rgb", "ids", "eye"}, …] in `turn_layout`
    order; each is embedded once (kept for later calls). Each view is prompted by the selection
    so far (only faces some view saw fairly head-on), and its outline agreeing best with that
    selection votes, weighted by how directly it sees each face. A face is kept where its
    in-votes are at least half its votes. `s`: modify.Surface. progress(done, total) after each
    view; cancelled() → True stops it (Cancelled)."""
    n = len(s.areas)
    areas = s.areas
    w0 = np.where(seen_faces(ids0, n), facing(s, eye0), 0.0)
    vis = w0.copy()
    vin = w0 * mask0
    for j, v in enumerate(views):
        if cancelled is not None and cancelled():
            raise Cancelled()
        _prepare(seg, v, n)
        if progress is not None:
            progress(j + 1, len(views))
        ids, sv, k = v["small"], v["seen"], v["k"]
        sel = (vis > 0) & (vin >= 0.5 * vis)
        sure = sel & (vin >= HEAD_ON)
        valid = (ids >= 0) & (ids < n)
        cpx = np.zeros(ids.shape, bool)
        cpx[valid] = sure[ids[valid]]
        if cpx.sum() < 20:                       # nothing of it in sight
            continue
        pts = [(x, y, True) for x, y in face_points(ids, faces_in)]
        pts += [(2 * x + 0.5, 2 * y + 0.5, True) for x, y in inside_points(cpx[::2, ::2])]
        pts += [(x, y, False) for x, y in face_points(ids, faces_out)]
        pts = [(k * x + (k - 1) / 2, k * y + (k - 1) / 2, inc) for x, y, inc in pts]
        common = sv & (vis > 0)
        best, score = None, AGREE
        for o in candidates(seg.outlines(pts, emb=_unpack(v["emb"]), size=ids.shape), valid):
            f = faces_in_outline(ids, o, n)
            union = areas[(f | sel) & common].sum()
            iou = areas[f & sel & common].sum() / union if union > 0 else 0.0
            if iou >= score:
                best, score = f, iou
        if best is None:
            continue
        wv = np.where(sv, facing(s, v["eye"]), 0.0)
        vis += wv
        vin += wv * best
    return (vis > 0) & (vin >= 0.5 * vis)
