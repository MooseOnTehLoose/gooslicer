"""elegoo-goo CLI.

  python3 -m elegoo_goo info FILE.goo [--verify]
  python3 -m elegoo_goo extract FILE.goo OUTDIR [--layers 0,10,99]
  python3 -m elegoo_goo build OUT.goo LAYER.png... [--template REF.goo] [--preview IMG]
                               [--set field=value ...]

PNG/preview handling needs Pillow (optional); the .goo format code is stdlib-only.
"""
from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from pathlib import Path

from . import goo, rle


def _pil():
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover
        sys.exit("error: this command needs Pillow (pip install pillow)")
    return Image


# ELEGOO printers anti-alias at the LCD; layer images are binary (spec §6).
BINARY = bytes(0 if v < 128 else 255 for v in range(256))


def _natural(p: Path):
    return [int(t) if t.isdigit() else t for t in re.split(r"(\d+)", p.name)]


def cmd_info(a) -> int:
    g = goo.read(a.file)
    h = g.header
    skip = {"small_preview", "big_preview", "raw_strings"}
    for f in dataclasses.fields(h):
        if f.name not in skip:
            v = getattr(h, f.name)
            print(f"{f.name:32s} {round(v, 5) if isinstance(v, float) else v}")
    print(f"{'layers parsed':32s} {len(g.layers)}")
    if a.verify:
        total = h.resolution_x * h.resolution_y
        for i, layer in enumerate(g.layers):
            n = sum(length for _, length in rle.decode_runs(layer.data))
            if n != total:
                print(f"layer {i}: {n} pixels != {total}")
                return 1
        print("all layer images decode, checksums and pixel counts OK")
    return 0


def cmd_extract(a) -> int:
    Image = _pil()
    g = goo.read(a.file)
    w, h = g.header.resolution_x, g.header.resolution_y
    out = Path(a.outdir)
    out.mkdir(parents=True, exist_ok=True)
    idx = [int(x) for x in a.layers.split(",")] if a.layers else range(len(g.layers))
    for i in idx:
        px = rle.decode(g.layers[i].data, w, h)
        Image.frombytes("L", (w, h), bytes(px)).save(out / f"{i:05d}.png")
    print(f"wrote {len(idx)} layer(s) to {out}")
    return 0


def _coerce(h: goo.Header, key: str, val: str):
    if key not in {f.name for f in dataclasses.fields(h)} or key in ("raw_strings",):
        sys.exit(f"error: unknown header field {key!r}")
    cur = getattr(h, key)
    return type(cur)(val) if not isinstance(cur, bytes) else sys.exit(f"error: {key} not settable")


def cmd_build(a) -> int:
    Image = _pil()
    files = sorted((Path(p) for p in a.layers), key=_natural)
    if not files:
        sys.exit("error: no layer images")
    tmpl_def = None
    if a.template:
        ref = goo.read(a.template)
        h = dataclasses.replace(ref.header, raw_strings={})
        tmpl_def = ref.layers[-1].definition if ref.layers else None
    else:
        h = goo.Header()
    for kv in a.set or []:
        k, _, v = kv.partition("=")
        setattr(h, k, _coerce(h, k, v))
    from datetime import datetime
    h.file_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    h.software_name, h.software_version = "elegoo-goo (Linux)", "0.1"
    h.layer_count = len(files)
    h.bottom_layers = min(h.bottom_layers, len(files))
    if a.preview:
        im = Image.open(a.preview).convert("RGB")
        le = h.machine_type == "ELEGOO Jupiter 2"
        for attr, size in (("small_preview", goo.SMALL_PREVIEW), ("big_preview", goo.BIG_PREVIEW)):
            setattr(h, attr, goo.rgb565_preview(im.tobytes(), im.width, im.height, size, le))
    want = (h.resolution_x, h.resolution_y)
    # 16K layers (94.2 Mpx) exceed Pillow's default bomb limit; allow exactly one layer.
    Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, want[0] * want[1])

    def images():
        for i, f in enumerate(files):
            im = Image.open(f)
            if im.size != want:
                sys.exit(f"error: {f} is {im.size[0]}x{im.size[1]}, printer needs {want[0]}x{want[1]}")
            px = im.convert("L").tobytes()
            yield px if a.keep_grey else px.translate(BINARY)
            if a.verbose:
                print(f"\rlayer {i + 1}/{len(files)}", end="", file=sys.stderr)

    n = goo.write(a.out, h, goo.build_layers(h, images(), tmpl_def))
    if a.verbose:
        print(file=sys.stderr)
    print(f"wrote {a.out}: {n} layers, {want[0]}x{want[1]}")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="elegoo_goo", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("info")
    p.add_argument("file")
    p.add_argument("--verify", action="store_true")
    p.set_defaults(fn=cmd_info)
    p = sub.add_parser("extract")
    p.add_argument("file")
    p.add_argument("outdir")
    p.add_argument("--layers")
    p.set_defaults(fn=cmd_extract)
    p = sub.add_parser("build")
    p.add_argument("out")
    p.add_argument("layers", nargs="+")
    p.add_argument("--template", help="SatelLite .goo for the same printer/resin: header + motion")
    p.add_argument("--preview")
    p.add_argument("--set", action="append", metavar="FIELD=VALUE")
    p.add_argument("--keep-grey", action="store_true",
                   help="keep grey pixels instead of binarising at 128 (printer AAs at LCD)")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_build)
    a = ap.parse_args(argv)
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
