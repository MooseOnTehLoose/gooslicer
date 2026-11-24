"""Printer profiles from SatelLite's `officialPlatform.ini` (Qt QSettings IniFormat).

Clean-room reader (no Qt import). Value encoding [OBSERVED in the shipped file]:
  * QSettings escapes: \\xH… hex (1-4 digits, a following space is a separator
    only after a short escape), \\0.. octal, \\\\ \\" \\a\\b\\f\\n\\r\\t\\v, optional "…" quoting.
  * "@Variant(" + QDataStream bytes + ")": u32 BE type tag, then payload.
    Tag 0x87 (135) = float32 BE [OBSERVED: XLength 211.68, ResX 15120 for the 16K].
"""
from __future__ import annotations

import os
import struct
from dataclasses import dataclass, field
from pathlib import Path

SATURN4_FAMILY = ("ELEGOO Saturn 4", "ELEGOO Saturn 4 Ultra", "ELEGOO Saturn 4 Ultra 16K")

_SIMPLE = {"a": 7, "b": 8, "f": 12, "n": 10, "r": 13, "t": 9, "v": 11,
           "\\": 92, '"': 34, "'": 39, "?": 63, ";": 59, ",": 44, "=": 61}
_HEX = "0123456789abcdefABCDEF"


def unescape(raw: str) -> str:
    """Undo QSettings INI value escaping. Returns a str of code points (latin-1 for bytes)."""
    s = raw.strip()
    if len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    out, i, n = [], 0, len(s)
    while i < n:
        c = s[i]
        if c != "\\":
            out.append(c)
            i += 1
            continue
        i += 1
        if i >= n:
            break
        e = s[i]
        if e == "x":
            j = i + 1
            while j < n and j - (i + 1) < 4 and s[j] in _HEX:
                j += 1
            out.append(chr(int(s[i + 1:j], 16)) if j > i + 1 else "")
            i = j
            if i < n and s[i] == " " and i + 1 < n and s[i + 1] in _HEX:
                i += 1  # Qt writes "\x5 2" to stop a short escape eating a hex digit
        elif e in "01234567":
            j = i
            while j < n and j - i < 3 and s[j] in "01234567":
                j += 1
            out.append(chr(int(s[i:j], 8)))
            i = j
        else:
            out.append(chr(_SIMPLE.get(e, ord(e))))
            i += 1
    return "".join(out)


def decode_value(raw: str):
    """Decode one INI value: @Variant floats -> float, true/false -> bool, ints -> int, else str."""
    s = unescape(raw)
    if s.startswith("@Variant(") and s.endswith(")"):
        blob = s[len("@Variant("):-1].encode("latin-1")
        if len(blob) >= 4:
            tag = struct.unpack_from(">I", blob)[0]
            if tag == 0x87 and len(blob) == 8:
                # float32 -> the decimal the user typed (211.68, not 211.6799926…)
                return float(f"{struct.unpack_from('>f', blob, 4)[0]:.7g}")
        return blob  # unknown variant: keep raw bytes
    if s in ("true", "false"):
        return s == "true"
    try:
        return int(s)
    except ValueError:
        return s


def parse_ini(text: str) -> dict[str, dict[str, object]]:
    sections: dict[str, dict[str, object]] = {}
    cur = None
    for line in text.splitlines():
        line = line.rstrip("\r")
        if not line.strip() or line.lstrip().startswith(";"):
            continue
        if line.startswith("[") and line.rstrip().endswith("]"):
            cur = sections.setdefault(line.strip()[1:-1], {})
            continue
        if cur is None or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cur[k.strip().replace("\\", "/")] = decode_value(v)
    return sections


@dataclass
class PrinterProfile:
    id: str
    name: str
    size_x: float          # mm (XLength)
    size_y: float          # mm (YWidth)
    size_z: float          # mm (ZHeight)
    res_x: int
    res_y: int
    pixel_x_um: float
    pixel_y_um: float
    mirror_x: bool
    mirror_y: bool
    file_format: str = "goo"
    settings: dict = field(default_factory=dict, repr=False)  # everything else, '/'-keyed

    @property
    def pixel_x_mm(self) -> float:
        return self.size_x / self.res_x

    @property
    def pixel_y_mm(self) -> float:
        return self.size_y / self.res_y

    def get(self, key: str, default=None):
        return self.settings.get(key, default)

    @classmethod
    def from_section(cls, sec_id: str, d: dict) -> "PrinterProfile":
        return cls(
            id=sec_id, name=str(d.get("Name", sec_id)),
            size_x=float(d["XLength"]), size_y=float(d["YWidth"]), size_z=float(d["ZHeight"]),
            res_x=int(round(float(d["ResX"]))), res_y=int(round(float(d["ResY"]))),
            pixel_x_um=float(d.get("PixX", 0.0)), pixel_y_um=float(d.get("PixY", 0.0)),
            mirror_x=bool(d.get("MirrorX", False)), mirror_y=bool(d.get("MirrorY", False)),
            file_format=str(d.get("UVtoolsFormat", "goo")).lower(), settings=dict(d))


def load_profiles(path: str | os.PathLike, names: tuple[str, ...] | None = SATURN4_FAMILY
                  ) -> dict[str, PrinterProfile]:
    """Return {printer name: profile}; `names=None` returns every printer section."""
    text = Path(path).read_bytes().decode("utf-8", "replace")
    out = {}
    for sid, sec in parse_ini(text).items():
        if "XLength" not in sec or "ResX" not in sec:
            continue
        p = PrinterProfile.from_section(sid, sec)
        if names is None or p.name in names:
            out[p.name] = p
    return out


def find_official_ini(extra: list[str | os.PathLike] | None = None) -> Path | None:
    """Locate SatelLite's officialPlatform.ini (user-supplied; never redistributed).

    Search order: $GOOSLICER_PROFILE_INI, explicit `extra` paths, XDG data dir copy,
    then common Wine install locations.
    """
    cands: list[Path] = []
    if os.environ.get("GOOSLICER_PROFILE_INI"):
        cands.append(Path(os.environ["GOOSLICER_PROFILE_INI"]))
    cands += [Path(p) for p in (extra or [])]
    xdg = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share"))
    cands.append(xdg / "gooslicer" / "officialPlatform.ini")
    rel = Path("resources/machines/printer/officialPlatform.ini")
    wine = Path(os.environ.get("WINEPREFIX", Path.home() / ".wine")) / "drive_c"
    for base in ("Program Files/ELEGOO SatelLite", "Program Files/ELEGOO_Satellite",
                 "Program Files (x86)/ELEGOO SatelLite"):
        cands.append(wine / base / rel)
    for c in cands:
        c = c / rel if c.is_dir() else c
        if c.is_file():
            return c
    return None
