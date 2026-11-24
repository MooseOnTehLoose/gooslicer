"""Clean-room .goo reader/writer (V3.0 layout), from 03-goo-format-spec.md.

All multi-byte fields are big-endian [OBSERVED in samples/10mmcube.goo].
"""
from __future__ import annotations

import os
import struct
import tempfile
from dataclasses import dataclass, field, fields, replace
from typing import BinaryIO, Iterable, Iterator

from . import rle

MAGIC = b"\x07\x00\x00\x00DLP\x00"
DELIM = b"\r\n"
ENDING = b"\x00\x00\x00" + MAGIC          # 11-byte file trailer [OBSERVED]
SMALL_PREVIEW = (116, 116)
BIG_PREVIEW = (290, 290)

# (name, struct code) in file order. Names follow ELEGOO's public GOO doc where the
# decompiled writer gives no name; see spec §2 for the evidence tag of each row.
_HEADER = [
    ("version", "4s"), ("magic", "8s"),
    ("software_name", "32s"), ("software_version", "24s"), ("file_time", "24s"),
    ("machine_name", "32s"), ("machine_type", "32s"), ("resin_profile", "32s"),
    ("anti_aliasing_level", "H"), ("grey_level", "H"), ("blur_level", "H"),
    ("small_preview", f"{SMALL_PREVIEW[0] * SMALL_PREVIEW[1] * 2}s"), ("_d1", "2s"),
    ("big_preview", f"{BIG_PREVIEW[0] * BIG_PREVIEW[1] * 2}s"), ("_d2", "2s"),
    ("layer_count", "I"), ("resolution_x", "H"), ("resolution_y", "H"),
    ("mirror_x", "B"), ("mirror_y", "B"),
    ("display_width", "f"), ("display_height", "f"), ("machine_z", "f"),
    ("layer_height", "f"), ("exposure_time", "f"), ("delay_mode", "B"),
    ("light_off_delay", "f"),
    ("bottom_before_lift_time", "f"), ("bottom_after_lift_time", "f"),
    ("bottom_after_retract_time", "f"),
    ("before_lift_time", "f"), ("after_lift_time", "f"), ("after_retract_time", "f"),
    ("bottom_exposure_time", "f"), ("bottom_layers", "I"),
    ("bottom_lift_distance", "f"), ("bottom_lift_speed", "f"),
    ("lift_distance", "f"), ("lift_speed", "f"),
    ("bottom_retract_distance", "f"), ("bottom_retract_speed", "f"),
    ("retract_distance", "f"), ("retract_speed", "f"),
    ("bottom_second_lift_distance", "f"), ("bottom_second_lift_speed", "f"),
    ("second_lift_distance", "f"), ("second_lift_speed", "f"),
    ("bottom_second_retract_distance", "f"), ("bottom_second_retract_speed", "f"),
    ("second_retract_distance", "f"), ("second_retract_speed", "f"),
    ("bottom_light_pwm", "H"), ("light_pwm", "H"), ("advance_mode", "B"),
    ("print_time", "I"), ("volume", "f"), ("weight", "f"), ("price", "f"),
    ("price_unit", "8s"), ("layer_def_address", "I"), ("grey_scale_level", "B"),
    ("transition_layers", "H"),
]
HEADER_STRUCT = struct.Struct(">" + "".join(c for _, c in _HEADER))
HEADER_SIZE = HEADER_STRUCT.size
assert HEADER_SIZE == 0x2FB95

_LAYER = [
    ("pause_flag", "H"), ("pause_z", "f"), ("z", "f"), ("exposure_time", "f"),
    ("light_off_time", "f"), ("before_lift_time", "f"), ("after_lift_time", "f"),
    ("after_retract_time", "f"),
    ("lift_distance", "f"), ("lift_speed", "f"),
    ("second_lift_distance", "f"), ("second_lift_speed", "f"),
    ("retract_distance", "f"), ("retract_speed", "f"),
    ("second_retract_distance", "f"), ("second_retract_speed", "f"),
    ("light_pwm", "H"),
]
LAYER_STRUCT = struct.Struct(">" + "".join(c for _, c in _LAYER))
assert LAYER_STRUCT.size == 64

_STR_FIELDS = {n: int(c[:-1]) for n, c in _HEADER if c.endswith("s") and "preview" not in n
               and not n.startswith("_") and n not in ("version", "magic")}


class GooError(ValueError):
    pass


def _cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


def _fixed(s: str, size: int) -> bytes:
    """UTF-8, truncated to size-1 bytes, NUL padded (app: memcpy min(len, n-1) + NUL)."""
    raw = s.encode("utf-8")[: size - 1]
    while raw:  # do not leave a split multi-byte sequence
        try:
            raw.decode("utf-8")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    return raw.ljust(size, b"\0")


@dataclass
class Header:
    version: str = "V3.0"
    software_name: str = "elegoo-goo (Linux)"
    software_version: str = "0.1"
    file_time: str = ""
    machine_name: str = "ELEGOO Saturn 4 Ultra 16K"
    machine_type: str = "ELEGOO Saturn 4 Ultra 16K"
    resin_profile: str = ""
    anti_aliasing_level: int = 0
    grey_level: int = 0
    blur_level: int = 0
    small_preview: bytes = bytes(SMALL_PREVIEW[0] * SMALL_PREVIEW[1] * 2)
    big_preview: bytes = bytes(BIG_PREVIEW[0] * BIG_PREVIEW[1] * 2)
    layer_count: int = 0
    resolution_x: int = 15120
    resolution_y: int = 6230
    mirror_x: int = 0
    mirror_y: int = 1
    display_width: float = 211.68
    display_height: float = 118.37
    machine_z: float = 220.0
    layer_height: float = 0.05
    exposure_time: float = 1.8
    delay_mode: int = 1
    light_off_delay: float = 0.0
    bottom_before_lift_time: float = 0.0
    bottom_after_lift_time: float = 0.0
    bottom_after_retract_time: float = 1.2
    before_lift_time: float = 0.0
    after_lift_time: float = 0.0
    after_retract_time: float = 1.2
    bottom_exposure_time: float = 32.0
    bottom_layers: int = 5
    bottom_lift_distance: float = 0.05
    bottom_lift_speed: float = 0.05
    lift_distance: float = 0.05
    lift_speed: float = 0.05
    bottom_retract_distance: float = 0.05
    bottom_retract_speed: float = 0.05
    retract_distance: float = 0.05
    retract_speed: float = 0.05
    bottom_second_lift_distance: float = 0.0
    bottom_second_lift_speed: float = 0.0
    second_lift_distance: float = 0.0
    second_lift_speed: float = 0.0
    bottom_second_retract_distance: float = 0.0
    bottom_second_retract_speed: float = 0.0
    second_retract_distance: float = 0.0
    second_retract_speed: float = 0.0
    bottom_light_pwm: int = 255
    light_pwm: int = 255
    advance_mode: int = 0
    print_time: int = 0
    volume: float = 0.0
    weight: float = 0.0
    price: float = 0.0
    price_unit: str = "$"
    grey_scale_level: int = 0
    transition_layers: int = 0
    # Raw fixed-size string slots as read. SatelLite leaves stale bytes after the
    # NUL [OBSERVED]; reused only while the decoded string is unchanged.
    raw_strings: dict = field(default_factory=dict, repr=False, compare=False)

    def pack(self) -> bytes:
        v = []
        for name, code in _HEADER:
            if name == "version":
                ver = self.version.encode("ascii")
                v.append(ver if len(ver) == 4 else b"V3.0")
            elif name == "magic":
                v.append(MAGIC)
            elif name.startswith("_d"):
                v.append(DELIM)
            elif name == "layer_def_address":
                v.append(HEADER_SIZE)
            elif name in _STR_FIELDS:
                raw = self.raw_strings.get(name)
                cur = getattr(self, name)
                ok = raw is not None and len(raw) == _STR_FIELDS[name] and _cstr(raw) == cur
                v.append(raw if ok else _fixed(cur, _STR_FIELDS[name]))
            elif name.endswith("preview"):
                data = getattr(self, name)
                want = int(code[:-1])
                if len(data) != want:
                    raise GooError(f"{name} must be {want} bytes of RGB565, got {len(data)}")
                v.append(data)
            else:
                v.append(getattr(self, name))
        return HEADER_STRUCT.pack(*v)

    @classmethod
    def unpack(cls, data: bytes) -> "Header":
        if len(data) < HEADER_SIZE:
            raise GooError("file shorter than GOO header")
        vals = dict(zip((n for n, _ in _HEADER), HEADER_STRUCT.unpack_from(data)))
        if vals["magic"] != MAGIC:
            raise GooError(f"bad magic {vals['magic'].hex()}")
        if vals["_d1"] != DELIM or vals["_d2"] != DELIM:
            raise GooError("bad preview delimiters")
        if vals["layer_def_address"] != HEADER_SIZE:
            raise GooError(f"unexpected layer_def_address {vals['layer_def_address']:#x}")
        kw = {"raw_strings": {n: vals[n] for n in _STR_FIELDS}}
        for f in fields(cls):
            if f.name == "raw_strings":
                continue
            x = vals[f.name]
            if f.name == "version":
                x = x.decode("ascii", "replace")
            elif f.name in _STR_FIELDS:
                x = _cstr(x)
            kw[f.name] = x
        return cls(**kw)


@dataclass
class LayerDef:
    pause_flag: int = 0
    pause_z: float = 200.0
    z: float = 0.0
    exposure_time: float = 0.0
    light_off_time: float = 0.0
    before_lift_time: float = 0.0
    after_lift_time: float = 0.0
    after_retract_time: float = 0.0
    lift_distance: float = 0.0
    lift_speed: float = 0.0
    second_lift_distance: float = 0.0
    second_lift_speed: float = 0.0
    retract_distance: float = 0.0
    retract_speed: float = 0.0
    second_retract_distance: float = 0.0
    second_retract_speed: float = 0.0
    light_pwm: int = 255

    def pack(self) -> bytes:
        return LAYER_STRUCT.pack(*(getattr(self, n) for n, _ in _LAYER))

    @classmethod
    def unpack(cls, data: bytes, offset: int = 0) -> "LayerDef":
        return cls(*LAYER_STRUCT.unpack_from(data, offset))


@dataclass
class Layer:
    definition: LayerDef
    data: bytes  # encoded RLE (0x55 … checksum)


@dataclass
class GooFile:
    header: Header
    layers: list[Layer] = field(default_factory=list)


# --------------------------------------------------------------------------- read
def iter_layers(buf: bytes, count: int) -> Iterator[Layer]:
    p = HEADER_SIZE
    for i in range(count):
        if p + 70 > len(buf):
            raise GooError(f"truncated at layer {i}")
        ld = LayerDef.unpack(buf, p)
        if buf[p + 64:p + 66] != DELIM:
            raise GooError(f"layer {i}: missing delimiter after definition")
        size = struct.unpack_from(">I", buf, p + 66)[0]
        data = buf[p + 70:p + 70 + size]
        if len(data) != size or buf[p + 70 + size:p + 72 + size] != DELIM:
            raise GooError(f"layer {i}: bad image length/delimiter")
        yield Layer(ld, data)
        p += 72 + size
    if buf[p:] != ENDING:
        raise GooError(f"unexpected trailer {buf[p:p + 16].hex()}")


def read(path_or_bytes) -> GooFile:
    buf = path_or_bytes if isinstance(path_or_bytes, (bytes, bytearray)) else open(path_or_bytes, "rb").read()
    h = Header.unpack(buf)
    return GooFile(h, list(iter_layers(buf, h.layer_count)))


# -------------------------------------------------------------------------- write
def _write_layer(fh: BinaryIO, layer: Layer) -> None:
    fh.write(layer.definition.pack())
    fh.write(DELIM)
    fh.write(struct.pack(">I", len(layer.data)))
    fh.write(layer.data)
    fh.write(DELIM)


def dumps(goo: GooFile) -> bytes:
    import io
    out = io.BytesIO()
    _dump(out, goo.header, goo.layers)
    return out.getvalue()


def _dump(fh: BinaryIO, header: Header, layers: Iterable[Layer]) -> int:
    start = fh.tell()
    fh.write(header.pack())
    n = 0
    for layer in layers:
        _write_layer(fh, layer)
        n += 1
    fh.write(ENDING)
    if n != header.layer_count:  # patch the count (streamed input)
        end = fh.tell()
        fh.seek(start + 0x2FAEE)
        fh.write(struct.pack(">I", n))
        fh.seek(end)
    return n


def write(path: str | os.PathLike, header: Header, layers: Iterable[Layer]) -> int:
    """Stream to `path` atomically (temp file + rename, POSIX for the app's
    temp-file/MapViewOfFile/MoveFile flow). Returns layer count."""
    path = os.fspath(path)
    fd, tmp = tempfile.mkstemp(prefix=".goo-", dir=os.path.dirname(os.path.abspath(path)))
    try:
        with os.fdopen(fd, "wb") as fh:
            n = _dump(fh, header, layers)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        return n
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ------------------------------------------------------------------ layer helpers
def _dec(x: float) -> float:
    """float32 value -> the decimal setting it came from (app computes in decimal/double)."""
    return float(f"{x:.7g}")


def layer_exposure(h: Header, index: int) -> float:
    """Bottom → transition (linear) → normal [OBSERVED in sample, all 200 layers]."""
    bottom, normal = _dec(h.bottom_exposure_time), _dec(h.exposure_time)
    if index < h.bottom_layers:
        return bottom
    k = index - h.bottom_layers + 1
    if h.transition_layers and k <= h.transition_layers:
        return bottom - (bottom - normal) * k / h.transition_layers
    return normal


def default_layer_def(h: Header, index: int, template: LayerDef | None = None) -> LayerDef:
    """Per-layer record: Z = (i+1)·layer_height, exposure from layer_exposure().

    Motion fields come from `template` (e.g. a layer of a SatelLite-sliced file for
    the same printer/resin) — their header→layer mapping is not yet proven.
    """
    base = template or LayerDef(
        pause_z=h.machine_z if h.machine_z < 200 else 200.0,
        after_retract_time=h.after_retract_time,
        lift_distance=h.lift_distance, lift_speed=h.lift_speed,
        second_lift_distance=h.second_lift_distance, second_lift_speed=h.second_lift_speed,
        retract_distance=h.retract_distance, retract_speed=h.retract_speed,
        second_retract_distance=h.second_retract_distance,
        second_retract_speed=h.second_retract_speed, light_pwm=h.light_pwm)
    return replace(base, z=(index + 1) * _dec(h.layer_height), exposure_time=layer_exposure(h, index))


def build_layers(h: Header, images: Iterable[bytes], template: LayerDef | None = None) -> Iterator[Layer]:
    """Encode raw 8-bit row-major images (resolution_x × resolution_y each)."""
    want = h.resolution_x * h.resolution_y
    for i, px in enumerate(images):
        if len(px) != want:
            raise GooError(f"layer {i}: {len(px)} pixels, expected {want}")
        yield Layer(default_layer_def(h, i, template), rle.encode(px))


# ------------------------------------------------------------------------ preview
def rgb565_preview(rgb: bytes, src_w: int, src_h: int, size: tuple[int, int],
                   little_endian: bool = False) -> bytes:
    """Nearest-neighbour scale packed RGB24 to `size` and encode RGB565 (spec §3).

    The app uses Qt smooth scaling, so pixels differ slightly; the encoding is identical.
    """
    w, h = size
    out = bytearray(w * h * 2)
    o = 0
    for y in range(h):
        sy = y * src_h // h
        for x in range(w):
            p = (sy * src_w + x * src_w // w) * 3
            r, g, b = rgb[p], rgb[p + 1], rgb[p + 2]
            v = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
            out[o:o + 2] = v.to_bytes(2, "little" if little_endian else "big")
            o += 2
    return bytes(out)
