"""The user's resin library: export to .satres, copies ("custom" profiles), and deleting from
GooSlicer's own files (05-slicing-spec §10).

Built-in profiles come from the user's SatelLite install and are never written; hiding and
deleting those is bookkept by the app (QSettings), not here.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import fields, replace
from pathlib import Path

from . import qdatastream as Q
from .settings import (_JSON, _SATRES, _SATRES_ROOT, SATRES_EXT, PrintSettings, ResinProfile,
                       load_satres)

# Every key of one profile in a SatelLite .satres export, with its QVariant type and the value
# SatelLite writes when there is nothing better [SAMPLE: all 36 profiles of the sample agree].
SCHEMA: tuple[tuple[str, int, object], ...] = (
    ("ExtraResinParameters/AverageScore", Q.FLOAT, 0.0),
    ("ExtraResinParameters/Id", Q.STRING, ""),
    ("ExtraResinParameters/IsFrequentlyUsed", Q.BOOL, False),
    ("ExtraResinParameters/MarkedCount", Q.INT, 0),
    ("ExtraResinParameters/Notes", Q.STRING, ""),
    ("ExtraResinParameters/PrintResultImageSize", Q.ULONG, 0),
    ("ExtraResinParameters/PrintSuccessRate", Q.DOUBLE, 0.0),
    ("ExtraResinParameters/PrinterBrand", Q.STRING, "ELEGOO"),
    ("ExtraResinParameters/PrinterBrandId", Q.STRING, ""),
    ("ExtraResinParameters/PrinterId", Q.STRING, ""),
    ("ExtraResinParameters/PrinterName", Q.STRING, ""),
    ("ExtraResinParameters/Reserved", Q.INT, 1),
    ("ExtraResinParameters/ResinColor", Q.STRING, ""),
    ("ExtraResinParameters/Score", Q.FLOAT, 0.0),
    ("ExtraResinParameters/ScoreUserCount", Q.INT, 0),
    ("ExtraResinParameters/UsedCount", Q.INT, 0),
    ("ExtraResinParameters/Version", Q.ULONG, 0),
    ("ExtraResinParameters/isMark", Q.BOOL, False),
    ("ExtraResinParameters/officialId", Q.STRING, ""),
    ("advanced/antilevel", Q.INT, 0),
    ("advanced/beamCompensation", Q.DOUBLE, 0.0),
    ("advanced/bottom_ledpower", Q.DOUBLE, 255.0),
    ("advanced/doubleblur", Q.BOOL, False),
    ("advanced/greylevel", Q.INT, 0),
    ("advanced/imageblur", Q.BOOL, False),
    ("advanced/imageblurpixel", Q.INT, 0),
    ("advanced/innertolerancecomp", Q.DOUBLE, 0.0),
    ("advanced/isanti", Q.BOOL, False),
    ("advanced/ledpower", Q.DOUBLE, 255.0),
    ("advanced/notes", Q.STRING, ""),
    ("advanced/reducepoint", Q.DOUBLE, 0.0),
    ("advanced/scale_x", Q.DOUBLE, 1.0),
    ("advanced/scale_y", Q.DOUBLE, 1.0),
    ("advanced/scale_z", Q.DOUBLE, 1.0),
    ("advanced/supportAA", Q.BOOL, False),
    ("company", Q.STRING, ""),
    ("print/backspeed", Q.FLOAT, 0.0),
    ("print/bottom_backspeed", Q.FLOAT, 0.0),
    ("print/bottom_wait_after_lift", Q.FLOAT, 0.0),
    ("print/bottom_wait_after_retract", Q.FLOAT, 0.0),
    ("print/bottom_wait_before_lift", Q.FLOAT, 0.0),
    ("print/bottomexposuretime", Q.FLOAT, 0.0),
    ("print/bottomlayer", Q.INT, 0),
    ("print/bottomliftdis", Q.FLOAT, 0.0),
    ("print/bottomliftspeed", Q.FLOAT, 0.0),
    ("print/bottomlightoffdelay", Q.FLOAT, 0.0),
    ("print/exposuretime", Q.FLOAT, 0.0),
    ("print/first_layer_exposure_time", Q.FLOAT, 50.0),
    ("print/liftingdis", Q.FLOAT, 0.0),
    ("print/liftingspeed", Q.FLOAT, 0.0),
    ("print/lightoffdelay", Q.FLOAT, 0.0),
    ("print/parameter_mode", Q.INT, 0),
    ("print/slow_back_dis", Q.FLOAT, 0.0),
    ("print/slow_back_speed", Q.FLOAT, 0.0),
    ("print/slow_bottom_back_dis", Q.FLOAT, 0.0),
    ("print/slow_bottom_back_speed", Q.FLOAT, 0.0),
    ("print/slow_bottom_lift_dis", Q.FLOAT, 0.0),
    ("print/slow_bottom_lift_speed", Q.FLOAT, 0.0),
    ("print/slow_lift_dis", Q.FLOAT, 0.0),
    ("print/slow_lift_speed", Q.FLOAT, 0.0),
    ("print/thickness", Q.FLOAT, 0.05),
    ("print/translate_layer_count", Q.INT, 0),
    ("print/wait_after_cure", Q.FLOAT, 0.0),
    ("resin/cost", Q.FLOAT, 30.0),
    ("resin/density", Q.FLOAT, 1.1),
    ("resin/suffixindex", Q.INT, 1),
    ("resin/suffixindexsuffix", Q.INT, 1),
    ("resin/type", Q.STRING, ""),
)

_FIELD_TO_JSON = {f: k for k, f in _JSON.items()}
_JSON_GROUP = {jk: group for group, jk in _SATRES.values()}


def _coerce(t: int, v):
    if t == Q.BOOL:
        return v if isinstance(v, bool) else str(v).strip().lower() in ("1", "true", "yes")
    if t in (Q.INT, Q.UINT, Q.LONG, Q.ULONG):
        return int(round(float(v)))
    if t in (Q.DOUBLE, Q.FLOAT):
        return float(v)
    return "" if v is None else str(v)


def profile_entries(r: ResinProfile, index: int) -> dict:
    """{key: (type, value)} for profile `index` of a .satres."""
    pre = f"{_SATRES_ROOT}{index}/scriptcontainername"
    out = {pre: (Q.STRING, r.name)}
    effective = r.settings()            # what GooSlicer slices with, for values the profile lacks
    for key, t, default in SCHEMA:
        v = default
        if key in _SATRES:
            group, jk = _SATRES[key]
            field = _JSON.get(jk)
            if field is not None:
                v = _coerce(t, getattr(effective, field))
            got = r.raw.get(group, {}).get(jk)
            if got is not None and not isinstance(got, (list, dict)):
                try:
                    v = _coerce(t, got)
                except (TypeError, ValueError):
                    v = default
        elif key == "company":
            v = r.company
        elif key == "ExtraResinParameters/PrinterName":
            v = r.printer
        out[f"{pre}/{key}"] = (t, v)
    return out


def write_satres(path: str | Path, profiles: list[ResinProfile]) -> None:
    """Write profiles as a SatelLite .satres file."""
    typed: dict = {_SATRES_ROOT: (Q.INT, len(profiles))}
    for i, r in enumerate(profiles):
        typed.update(profile_entries(r, i))
    Q.write_variant_map(str(path), typed)


# ---------------------------------------------------------------- names
def name_taken(name: str, profiles: list[ResinProfile]) -> bool:
    n = name.strip().casefold()
    return any(r.name.strip().casefold() == n for r in profiles)


def copy_name(name: str, profiles: list[ResinProfile]) -> str:
    """A free name to suggest for a copy: 'X copy', 'X copy 2', …"""
    base = f"{name} copy"
    cand, k = base, 2
    while name_taken(cand, profiles):
        cand, k = f"{base} {k}", k + 1
    return cand


def check_new_name(name: str, profiles: list[ResinProfile]) -> str | None:
    """Why `name` can't be used for a new profile, or None if it can."""
    if not name.strip():
        return "Enter a name."
    if name_taken(name, profiles):
        return f"A resin profile named “{name.strip()}” already exists. Choose another name."
    return None


# ---------------------------------------------------------------- custom profiles
def _file_for(name: str, directory: Path) -> Path:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("_")[:60] or "resin"
    return directory / f"{slug}-{hashlib.sha256(name.encode()).hexdigest()[:8]}{SATRES_EXT}"


def create_copy(src: ResinProfile, name: str, directory: str | Path) -> ResinProfile:
    """A new custom profile: `src`'s values under a new name, saved as its own .satres."""
    d = Path(directory)
    d.mkdir(parents=True, exist_ok=True)
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in src.raw.items()}
    raw["vd_resine_profilename"] = name.strip()
    path = _file_for(name.strip(), d)
    r = ResinProfile(id=f"satres:{path.stem}:0", name=name.strip(), printer=src.printer,
                     resin_type=src.resin_type, raw=raw, source="custom", path=str(path))
    write_satres(path, [r])
    return load_satres(path, "custom")[0]


def with_settings(r: ResinProfile, s: PrintSettings) -> ResinProfile:
    """`r` with its print values replaced by `s` (everything else kept)."""
    raw = {k: (dict(v) if isinstance(v, dict) else v) for k, v in r.raw.items()}
    for f in fields(PrintSettings):
        jk = _FIELD_TO_JSON.get(f.name)
        if jk and jk in _JSON_GROUP:
            raw.setdefault(_JSON_GROUP[jk], {})[jk] = getattr(s, f.name)
    return replace(r, raw=raw)


def save_custom(r: ResinProfile) -> ResinProfile:
    if r.source != "custom" or not r.path:
        raise ValueError("only copies made in GooSlicer can be changed")
    write_satres(r.path, [r])
    return load_satres(r.path, "custom")[0]


def delete_from_file(r: ResinProfile) -> None:
    """Remove an imported or custom profile from GooSlicer's copy of its .satres: the file goes
    when it was the last profile in it, otherwise it is rewritten without it."""
    if r.source not in ("imported", "custom") or not r.path:
        raise ValueError("only imported profiles and copies have a file in GooSlicer's library")
    p = Path(r.path)
    rest = [x for x in load_satres(p, r.source) if (x.printer, x.name) != (r.printer, r.name)]
    if rest:
        write_satres(p, rest)
    else:
        p.unlink()
