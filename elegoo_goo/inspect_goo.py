"""Inspect a .goo file: detect byte order, dump header fields, probe the layer area.

Usage: python3 -m elegoo_goo.inspect_goo FILE.goo
Read-only; built to resolve the open items in 03-goo-format-spec.md §6.
"""
from __future__ import annotations

import struct
import sys

HEADER_END = 0x2FB95
FIELDS = [  # (offset, struct code, name)
    (0x00BC, "H", "AntiAliasingLevel"), (0x00BE, "H", "GreyLevel"), (0x00C0, "H", "BlurLevel"),
    (0x2FAEE, "I", "LayerCount"), (0x2FAF2, "H", "ResolutionX"), (0x2FAF4, "H", "ResolutionY"),
    (0x2FAF6, "B", "MirrorX"), (0x2FAF7, "B", "MirrorY"),
    (0x2FAF8, "f", "DisplayWidth"), (0x2FAFC, "f", "DisplayHeight"), (0x2FB00, "f", "MachineZ"),
    (0x2FB04, "f", "LayerHeight"), (0x2FB08, "f", "ExposureTime"), (0x2FB0C, "B", "DelayMode"),
    (0x2FB0D, "f", "LightOffDelay"),
    *[(0x2FB11 + 4 * k, "f", f"Wait[{k}]") for k in range(6)],
    (0x2FB29, "f", "BottomExposureTime"), (0x2FB2D, "I", "BottomLayers"),
    *[(0x2FB31 + 4 * k, "f", f"LiftRetract[{k}]") for k in range(16)],
    (0x2FB71, "H", "BottomLightPWM"), (0x2FB73, "H", "LightPWM"), (0x2FB75, "B", "AdvanceMode"),
    (0x2FB76, "I", "PrintTime"), (0x2FB7A, "f", "Volume"), (0x2FB7E, "f", "Weight"),
    (0x2FB82, "f", "Price"), (0x2FB8E, "I", "LayerDefAddress"), (0x2FB92, "B", "GrayScaleLevel"),
    (0x2FB93, "H", "TransitionLayers"),
]


def cstr(b: bytes) -> str:
    return b.split(b"\0", 1)[0].decode("utf-8", "replace")


def detect_endian(d: bytes) -> str:
    be = struct.unpack_from(">I", d, 0x2FB8E)[0]
    le = struct.unpack_from("<I", d, 0x2FB8E)[0]
    if be == HEADER_END and le != HEADER_END:
        return ">"
    if le == HEADER_END and be != HEADER_END:
        return "<"
    rx_be, rx_le = struct.unpack_from(">H", d, 0x2FAF2)[0], struct.unpack_from("<H", d, 0x2FAF2)[0]
    return ">" if 0 < rx_be <= 20000 and not (0 < rx_le <= 20000 and rx_le < rx_be) else "<"


def main(path: str) -> None:
    d = open(path, "rb").read()
    print(f"file: {path}  size: {len(d)}")
    print(f"version: {d[0:4]!r}  magic: {d[4:12].hex(' ')}")
    for off, ln, name in [(0x0C, 32, "SoftwareName"), (0x2C, 24, "SoftwareVersion"),
                          (0x44, 24, "FileCreateTime"), (0x5C, 32, "MachineName"),
                          (0x7C, 32, "MachineType"), (0x9C, 32, "ResinProfileName"),
                          (0x2FB86, 8, "PriceUnit")]:
        print(f"  {off:#08x} {name:18s} {cstr(d[off:off + ln])!r}")
    print(f"delimiters @0x69E2={d[0x69E2:0x69E4].hex()} @0x2FAEC={d[0x2FAEC:0x2FAEE].hex()}")
    e = detect_endian(d)
    print(f"detected byte order: {'BIG' if e == '>' else 'LITTLE'} endian")
    for off, code, name in FIELDS:
        v = struct.unpack_from(e + code, d, off)[0]
        print(f"  {off:#08x} {name:20s} {round(v, 5) if isinstance(v, float) else v}")
    lda = struct.unpack_from(e + "I", d, 0x2FB8E)[0]
    print(f"\nbytes after header ({HEADER_END:#x}..+96):")
    for k in range(0, 96, 16):
        print(f"  {HEADER_END + k:#08x}: {d[HEADER_END + k:HEADER_END + k + 16].hex(' ')}")
    idx = d.find(b"\x55", lda)
    print(f"first 0x55 at/after LayerDefAddress: {idx:#x} (offset +{idx - lda} from {lda:#x})")
    print(f"tail 48 bytes: {d[-48:].hex(' ')}")


if __name__ == "__main__":
    main(sys.argv[1])
