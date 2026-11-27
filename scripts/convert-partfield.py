#!/usr/bin/env python3
"""Make PartField's ONNX file for Select by Parts (AI) (spec 11) from NVIDIA's own code and
checkpoint, and install it with NVIDIA's licence.

PartField is under the NVIDIA License (non-commercial research and education only), so
GooSlicer doesn't ship or host it: this fetches it, pinned by commit and sha256, and converts it
on your machine. Run it through scripts/convert-partfield.sh, which makes a throwaway Python
environment with PyTorch (CPU) for it.

The changes to PartField's code, all for the CPU and the ONNX export (the weights and the
network are unchanged; the export is checked against PyTorch before it's installed):
  - device 'cuda' → 'cpu';
  - torch_scatter.scatter_mean (a CUDA library) → sum ÷ count with scatter_add;
  - the voxelisation's scatter_(reduce='add') → scatter_add;
  - the 3D grid_sample in trilinear_devoxelize (not exportable) → eight gathers, the same
    arithmetic;
  - its data loader isn't imported (it needs cloud libraries and isn't used).
"""
from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
import types
import urllib.request
from pathlib import Path

REPO = "https://github.com/nv-tlabs/PartField.git"
COMMIT = "373025dbd283bb44cc4a6dc78c99994dbc91de32"
CKPT_URL = ("https://huggingface.co/mikaelaangel/partfield-ckpt/resolve/"
            "90b9b1e08b6a12fdcb6ee26b4854a26235e1765f/model_objaverse.ckpt")
CKPT_SHA = "463efc8a3afd3913142aa025e0125c00f16ef452b8de6a132ebe32bbe7877ee4"
N_POINTS = 100000                     # gooslicer/partfeat.py N_POINTS
APP_ID = "io.github.mooseontehloose.GooSlicer"

SCATTER = '''"""torch_scatter stand-in (GooSlicer): sum / count, exportable to ONNX."""
import torch


def scatter_mean(src, index, dim=-1, out=None, dim_size=None):
    index = index.expand_as(src) if index.shape != src.shape else index
    if out is None:
        shape = list(src.shape)
        shape[dim] = dim_size or int(index.max()) + 1
        out = src.new_zeros(shape)
    total = torch.zeros_like(out).scatter_add(dim, index, src)
    count = torch.zeros_like(out).scatter_add(dim, index, torch.ones_like(src))
    return total / count.clamp(min=1.0)


def scatter_max(*a, **k):
    raise NotImplementedError
'''

DEVOX = '''import torch

__all__ = ['trilinear_devoxelize']


def trilinear_devoxelize(c, coords, r, training=None):
    """(GooSlicer) grid_sample(c, ..., padding_mode='border', align_corners=False) at voxel
    coordinates in [0, r-1], with gathers so it exports to ONNX; coords[:, 0] indexes W,
    coords[:, 1] H, coords[:, 2] D, as the original."""
    B, C = c.shape[0], c.shape[1]
    flat = c.reshape(B, C, -1)
    x, y, z = coords[:, 0], coords[:, 1], coords[:, 2]
    x0, y0, z0 = x.floor(), y.floor(), z.floor()
    fx, fy, fz = x - x0, y - y0, z - z0
    x1, y1, z1 = (x0 + 1).clamp(max=r - 1), (y0 + 1).clamp(max=r - 1), (z0 + 1).clamp(max=r - 1)
    out = 0
    for zi, wz in ((z0, 1 - fz), (z1, fz)):
        for yi, wy in ((y0, 1 - fy), (y1, fy)):
            for xi, wx in ((x0, 1 - fx), (x1, fx)):
                idx = (zi * (r * r) + yi * r + xi).long()
                g = torch.gather(flat, 2, idx[:, None, :].expand(B, C, idx.shape[1]))
                out = out + g * (wx * wy * wz)[:, None, :]
    return out
'''


def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def fetch(work: Path) -> tuple[Path, Path]:
    src = work / "PartField"
    if not (src / ".git").is_dir():
        subprocess.run(["git", "clone", "-q", REPO, str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "checkout", "-q", COMMIT], check=True)
    ckpt = work / "model_objaverse.ckpt"
    if not ckpt.is_file() or sha256(ckpt) != CKPT_SHA:
        print("Downloading PartField's checkpoint (1.2 GB)…", flush=True)
        part = ckpt.with_suffix(".part")
        req = urllib.request.Request(CKPT_URL, headers={"User-Agent": "GooSlicer"})
        with urllib.request.urlopen(req, timeout=60) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f, 1 << 20)
        if sha256(part) != CKPT_SHA:
            part.unlink()
            raise SystemExit("The checkpoint doesn't match its checksum.")
        part.replace(ckpt)
    return src, ckpt


def patch(src: Path, work: Path) -> Path:
    """A copy of PartField's package with the CPU/ONNX changes (see the module docstring)."""
    out = work / "patched"
    shutil.rmtree(out, ignore_errors=True)
    shutil.copytree(src / "partfield", out / "partfield")
    for p in (out / "partfield").rglob("*.py"):
        s = p.read_text()
        t = s.replace("device='cuda'", "device='cpu'").replace('device="cuda"', 'device="cpu"').replace(".cuda()", "")
        if t != s:
            p.write_text(t)
    vox = out / "partfield/model/PVCNN/pv_module/voxelization.py"
    s = vox.read_text()
    old = "result.scatter_(index=indices.long(), src=features, dim=2, reduce='add')"
    if old not in s:
        raise SystemExit("PartField's voxelisation has changed; this script needs updating.")
    vox.write_text(s.replace(old, "result.scatter_add(2, indices.long(), features)"))
    (out / "partfield/model/PVCNN/pv_module/functional/devoxelization.py").write_text(DEVOX)
    (out / "torch_scatter").mkdir()
    (out / "torch_scatter/__init__.py").write_text(SCATTER)
    return out


def export(src: Path, patched: Path, ckpt: Path, onnx_path: Path) -> None:
    import numpy as np
    import torch
    sys.path[:0] = [str(patched)]
    stub = types.ModuleType("partfield.dataloader")
    stub.Demo_Dataset = stub.Demo_Remesh_Dataset = stub.Correspondence_Demo_Dataset = None
    sys.modules["partfield.dataloader"] = stub
    from partfield.config.defaults import _C
    from partfield.model_trainer_pvcnn_only_demo import Model
    cfg = _C.clone()
    cfg.merge_from_file(str(src / "configs/final/demo.yaml"))
    cfg.freeze()
    model = Model(cfg)
    sd = torch.load(ckpt, map_location="cpu", weights_only=False)["state_dict"]
    model.load_state_dict(sd, strict=True)
    model.eval()

    class Net(torch.nn.Module):
        def __init__(self, m):
            super().__init__()
            self.m = m

        def forward(self, pc):
            planes = self.m.triplane_transformer(self.m.pvcnn(pc, pc))
            return planes[:, :, 64:]                      # the part planes (the first 64: SDF)
    net = Net(model).eval()
    rng = np.random.default_rng(0)                         # points on a box and a ball
    p = rng.normal(size=(N_POINTS, 3))
    p /= np.linalg.norm(p, axis=1, keepdims=True)
    p[: N_POINTS // 2] = np.clip(p[: N_POINTS // 2] * 2, -1, 1)
    x = torch.tensor(p * 0.9, dtype=torch.float32)[None]
    print("Exporting to ONNX…", flush=True)
    with torch.no_grad():
        ref = net(x).numpy()
        tmp = onnx_path.with_suffix(".part")
        torch.onnx.export(net, (x,), str(tmp), input_names=["points"], output_names=["planes"],
                          opset_version=18, dynamo=False, do_constant_folding=True)
    import onnxruntime as ort
    y = ort.InferenceSession(str(tmp), providers=["CPUExecutionProvider"]).run(None, {"points": x.numpy()})[0]
    a = y.transpose(0, 1, 3, 4, 2).reshape(-1, y.shape[2])
    b = ref.transpose(0, 1, 3, 4, 2).reshape(-1, ref.shape[2])
    cos = (a * b).sum(1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1) + 1e-9)
    print(f"Checked against PyTorch: lowest cosine {cos.min():.6f}", flush=True)
    if not np.isfinite(y).all() or cos.min() < 0.9999:
        tmp.unlink()
        raise SystemExit("The ONNX file doesn't match PyTorch; not installed.")
    tmp.replace(onnx_path)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--flatpak", action="store_true", help="install for the Flatpak build of GooSlicer")
    ap.add_argument("--dest", type=Path, help="model folder (default: GooSlicer's user model folder)")
    ap.add_argument("--work", type=Path, default=Path.home() / ".cache/gooslicer/partfield-convert")
    a = ap.parse_args()
    if a.dest is None:
        if a.flatpak:
            a.dest = Path.home() / ".var/app" / APP_ID / "data/gooslicer/models"
        else:
            base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local/share")
            a.dest = Path(base) / "gooslicer/models"
    a.work.mkdir(parents=True, exist_ok=True)
    src, ckpt = fetch(a.work)
    patched = patch(src, a.work)
    dest = a.dest / "partfield"
    dest.mkdir(parents=True, exist_ok=True)
    export(src, patched, ckpt, dest / "partfield.onnx")
    shutil.copy(src / "LICENSE", dest / "LICENSE")
    (dest / "NOTICE").write_text(
        "PartField (https://github.com/nv-tlabs/PartField, commit " + COMMIT + ") by NVIDIA,\n"
        "converted to ONNX on this machine by GooSlicer's scripts/convert-partfield.py.\n"
        "Under the NVIDIA License in LICENSE: non-commercial research and education only.\n")
    print(f"Installed in {dest}. The work folder {a.work} (about 3 GB) can be deleted.")


if __name__ == "__main__":
    main()
