"""GooSlicer's own copy of the user's SatelLite profiles [GOOSLICER].

The first time profiles are found (or located by the user), the files GooSlicer reads are copied
into its data folder, laid out as in a SatelLite install so the usual "next to the printer
profiles" lookups work unchanged:

    <root>/resources/machines/printer/officialPlatform.ini     printer profiles
    <root>/resources/resin/material/*.data                     resin profiles
    <root>/resources/script/common/**/*.satsup                 support templates
    <root>/source.json                                         where and when they came from

From then on only the copy is read, so SatelLite can be uninstalled. Locating the profiles again
refreshes the copy. The copy stays on the user's machine: it is their own install's files, and
GooSlicer still never ships them.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .settings import resin_dir_for
from .support_defaults import script_dir_for

INI_REL = Path("resources/machines/printer/officialPlatform.ini")
RESIN_REL = Path("resources/resin/material")
SCRIPT_REL = Path("resources/script/common")
INFO = "source.json"


@dataclass
class StoreInfo:
    root: Path
    ini: Path
    source: str             # the officialPlatform.ini it was copied from
    copied: float           # epoch seconds
    resins: int             # resin .data files in the copy
    templates: int          # .satsup files in the copy
    resin_source: str = ""
    template_source: str = ""

    @property
    def resin_dir(self) -> Path | None:
        d = self.root / RESIN_REL
        return d if d.is_dir() else None

    @property
    def template_dir(self) -> Path | None:
        d = self.root / SCRIPT_REL
        return d if d.is_dir() else None


def stored_ini(root: str | os.PathLike) -> Path | None:
    p = Path(root) / INI_REL
    return p if p.is_file() else None


def is_inside(path: str | os.PathLike, root: str | os.PathLike) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except ValueError:
        return False


def info(root: str | os.PathLike) -> StoreInfo | None:
    root = Path(root)
    ini = stored_ini(root)
    if ini is None:
        return None
    try:
        d = json.loads((root / INFO).read_text())
    except (OSError, ValueError):
        d = {}
    return StoreInfo(root, ini, str(d.get("source", "")), float(d.get("copied", 0) or 0),
                     _count(root / RESIN_REL, "*.data"), _count(root / SCRIPT_REL, "**/*.satsup"),
                     str(d.get("resin_source", "")), str(d.get("template_source", "")))


def _count(d: Path, pattern: str) -> int:
    return sum(1 for _ in d.glob(pattern)) if d.is_dir() else 0


def _copy_files(src: Path, dest: Path, pattern: str) -> int:
    """Copy src's files matching pattern (keeping sub-folders) into a fresh dest."""
    n = 0
    for f in sorted(src.glob(pattern)):
        if f.is_file():
            out = dest / f.relative_to(src)
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, out)
            n += 1
    return n


def copy_profiles(ini: str | os.PathLike, root: str | os.PathLike, *,
                  resin_src: str | os.PathLike | None = None,
                  script_src: str | os.PathLike | None = None) -> StoreInfo:
    """Copy a SatelLite officialPlatform.ini, plus the resin profiles and support templates found
    next to it (or in resin_src / script_src), into root, replacing what was there. A folder that
    can't be found keeps the copy already stored (e.g. the Flatpak file portal exposes only the
    picked file, not the install around it). The new copy is built beside the old one and swapped
    in at the end, so a failure leaves the old one intact."""
    ini, root = Path(ini), Path(root)
    if not ini.is_file():
        raise FileNotFoundError(ini)
    if is_inside(ini, root):                       # already ours: nothing to copy from
        return info(root)
    resin_src = Path(resin_src) if resin_src and Path(resin_src).is_dir() else resin_dir_for(ini)
    script_src = Path(script_src) if script_src and Path(script_src).is_dir() else script_dir_for(ini)
    old = info(root)
    tmp = root.with_name(root.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    try:
        (tmp / INI_REL).parent.mkdir(parents=True)
        shutil.copy2(ini, tmp / INI_REL)
        meta = {"source": str(ini.resolve()), "copied": time.time()}
        for src, rel, pattern, key in ((resin_src, RESIN_REL, "*.data", "resin_source"),
                                       (script_src, SCRIPT_REL, "**/*.satsup", "template_source")):
            if src is not None and not is_inside(src, root):
                _copy_files(src, tmp / rel, pattern)
                meta[key] = str(src.resolve())
            elif (root / rel).is_dir():            # keep what we had
                shutil.copytree(root / rel, tmp / rel)
                meta[key] = getattr(old, key, "") if old else ""
        (tmp / INFO).write_text(json.dumps(meta, indent=1))
        if root.exists():
            gone = root.with_name(root.name + ".old")
            shutil.rmtree(gone, ignore_errors=True)
            root.rename(gone)
            tmp.rename(root)
            shutil.rmtree(gone, ignore_errors=True)
        else:
            root.parent.mkdir(parents=True, exist_ok=True)
            tmp.rename(root)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return info(root)


def replace_folder(root: str | os.PathLike, kind: str, src: str | os.PathLike) -> StoreInfo:
    """Replace the stored resin profiles (kind "resin") or support templates ("templates") with
    the contents of another folder."""
    root, src = Path(root), Path(src)
    rel, pattern, key = ((RESIN_REL, "*.data", "resin_source") if kind == "resin"
                         else (SCRIPT_REL, "**/*.satsup", "template_source"))
    if stored_ini(root) is None:
        raise FileNotFoundError("no stored printer profiles")
    if not src.is_dir():
        raise NotADirectoryError(src)
    if is_inside(src, root / rel):
        return info(root)
    tmp = root / (rel.name + ".new")
    shutil.rmtree(tmp, ignore_errors=True)
    if not _copy_files(src, tmp, pattern):
        shutil.rmtree(tmp, ignore_errors=True)
        what = "resin profiles (.data)" if kind == "resin" else "support templates (.satsup)"
        raise ValueError(f"no {what} in {src}")
    shutil.rmtree(root / rel, ignore_errors=True)
    (root / rel).parent.mkdir(parents=True, exist_ok=True)
    tmp.rename(root / rel)
    try:
        meta = json.loads((root / INFO).read_text())
    except (OSError, ValueError):
        meta = {}
    meta[key] = str(src.resolve())
    (root / INFO).write_text(json.dumps(meta, indent=1))
    return info(root)
