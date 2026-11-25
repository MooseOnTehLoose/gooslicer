"""SatelLite support templates (*.satsup), template auto-selection, hollow and drain-hole defaults.

Spec: 06-support-hollow-defaults-spec.md. Templates come from the user's own SatelLite install
(`resources/script/common/`), copied once into GooSlicer's data folder (gooslicer/profile_store.py);
GooSlicer never ships them.
"""
from __future__ import annotations

import math
import os
from dataclasses import dataclass, field
from pathlib import Path

from .qdatastream import read_variant_map

_PREFIX = "support/scriptmanager/scripcustomtnum0/scriptcontainername/"

# Script block types inside a template [OBSERVED in all 11 shipped templates]
SCRIPT_STRUCTURE_EVO = 7
SCRIPT_STRUCTURE_TREE = 0
SCRIPT_RAFT = 9

# Height buckets used by SatelLite's "recommended support" rule [OBSERVED EleTriAdd
# FUN_180103d10 / FUN_180101ae0]: the tag is matched as a substring of the template name.
BUCKETS = (("0-30", 0.0, 30.0), ("30-60", 30.0, 60.0), ("60-100", 60.0, 100.0),
           ("100+", 100.0, math.inf))


@dataclass
class SupportTemplate:
    name: str                       # container name, e.g. "EVO_30-60mm"
    path: Path
    printer: str | None             # sub-folder name (e.g. "ELEGOO Jupiter 2"), None = common
    script_types: list[int]
    structure: dict                 # structure script parameters (EVO type 7 or Tree type 0)
    raft: dict                      # script 9 parameters
    lift_height: float              # model lift above the plate, mm
    inner_support: bool
    raw: dict = field(repr=False, default_factory=dict)

    @property
    def family(self) -> str:
        if SCRIPT_STRUCTURE_EVO in self.script_types:
            return "evo"
        if SCRIPT_STRUCTURE_TREE in self.script_types:
            return "tree"
        return "unknown"

    @property
    def bucket(self) -> tuple[str, float, float] | None:
        return next((b for b in BUCKETS if b[0] in self.name), None)

    def fits_height(self, h: float) -> bool:
        """SatelLite warns when the tallest part is outside the template's range (strict
        comparisons, so the bucket edges fit both neighbours) [OBSERVED FUN_180101ae0]."""
        b = self.bucket
        return b is None or not (h > b[2] or h < b[1])


def parse_template(path: str | os.PathLike, printer: str | None = None) -> SupportTemplate:
    m = read_variant_map(str(path))
    name = m.get(_PREFIX.rstrip("/")) or Path(path).stem
    scripts: dict[int, dict] = {}
    top: dict = {}
    types = []
    # header keys live under _PREFIX; parameters are "<name>/<script type>/<param>" or
    # "<name>/<param>" [OBSERVED layout of the shipped files]
    for k, v in m.items():
        if k.endswith("/ConfidentState"):
            continue
        if k.startswith(_PREFIX):
            rest = k[len(_PREFIX):]
            if rest.startswith("scriptNum/") and rest.endswith("/type"):
                types.append(int(v))
            continue
        if not k.startswith(f"{name}/"):
            continue
        parts = k.split("/", 2)
        if len(parts) == 3 and parts[1].isdigit():
            scripts.setdefault(int(parts[1]), {})[parts[2]] = v
        elif len(parts) == 2:
            top[parts[1]] = v
    struct_type = SCRIPT_STRUCTURE_EVO if SCRIPT_STRUCTURE_EVO in scripts else SCRIPT_STRUCTURE_TREE
    return SupportTemplate(
        name=str(name), path=Path(path), printer=printer, script_types=sorted(set(types)),
        structure=scripts.get(struct_type, {}), raft=scripts.get(SCRIPT_RAFT, {}),
        lift_height=float(top.get("LiftHeight", 5.0)), inner_support=bool(top.get("EnableInnerSupport", True)),
        raw=m)


def script_dir_for(profile_ini: str | os.PathLike) -> Path | None:
    """resources/machines/printer/officialPlatform.ini -> resources/script/common."""
    p = Path(profile_ini).resolve()
    for base in (p.parent.parent.parent, p.parent):
        d = base / "script" / "common"
        if d.is_dir():
            return d
    return None


def load_templates(directory: str | os.PathLike, printer: str | None = None) -> list[SupportTemplate]:
    """Templates for `printer`: its own sub-folder when one exists (only "ELEGOO Jupiter 2" ships
    one), else the common ones [INFERRED from the folder layout]. Sorted by name, which is also
    the order SatelLite lists them in [UNKNOWN; affects which template wins inside a bucket]."""
    d = Path(directory)
    own = d / printer if printer else None
    if own and own.is_dir() and any(own.glob("*.satsup")):
        files, tag = sorted(own.glob("*.satsup")), printer
    else:
        files, tag = sorted(d.glob("*.satsup")), None
    out = []
    for f in files:
        try:
            out.append(parse_template(f, tag))
        except (ValueError, EOFError, OSError):
            continue
    return out


def recommended_template(templates: list[SupportTemplate], max_height: float,
                         family: str = "evo") -> SupportTemplate | None:
    """SatelLite's 'supports recommended by the system' [OBSERVED FUN_180103d10]: the first
    template whose name carries the bucket tag for the tallest part's Z height
    (h ≤ 30 → 0-30, ≤ 60 → 30-60, ≤ 100 → 60-100, else 100+)."""
    tag = "0-30" if max_height <= 30 else "30-60" if max_height <= 60 else \
        "60-100" if max_height <= 100 else "100+"
    cands = [t for t in templates if tag in t.name and (family is None or t.family == family)]
    return cands[0] if cands else None


# --------------------------------------------------------------------------- hollowing
HOLLOW_TYPES = ("inside", "outside", "center", "open_bottom")   # combo order [OBSERVED]
INFILL_TYPES = ("bcc", "fcc")


@dataclass
class HollowSettings:
    """Defaults of SatelLite's Hollow dialog [OBSERVED DialogHollow::loadParas, EleShapeModify]."""
    thickness: float = 1.0          # mm, /Hollowing/thickness
    accuracy: float = 1.0           # mm, /Hollowing/accuracy (no cross-check vs thickness found)
    hollow_type: str = "inside"     # /Hollowing/Type 0..3
    surface_angle: float = 45.0     # degrees, /Hollowing/surfaceAngle, range 5–85
    filter_small_shells: bool = False   # /Hollowing/isFilter
    small_shell_size: float = 0.1   # /Hollowing/filterSmallShell, range 0.001–5e9 (volume unit)
    filter_by: str = "cavity_number"    # /Hollowing/filter_type 0 = number, 1 = volume
    max_cavities: int = 1           # /Hollowing/cavity_number, range 1–9999
    # Infill lattice: SatelLite's Hollow dialog with "Infill structure" ticked [OBSERVED
    # DialogInfill::setUI, EleTriInfill.dll; no ini keys found]. Not offered for Open Bottom.
    infill: bool = False            # "Infill structure" (useHatch), off
    infill_type: str = "bcc"        # "Fill Structure Type": BCC or FCC
    infill_size: float = 4.0        # mm, "Fill Structure Dimensions": the lattice cell
    infill_radius: float = 1.0      # mm, "Support Radius": the strut radius

    def validate(self) -> list[str]:
        errs = []
        if self.thickness <= 0:
            errs.append("wall thickness must be positive")
        if self.accuracy <= 0:
            errs.append("accuracy must be positive")
        if self.hollow_type not in HOLLOW_TYPES:
            errs.append(f"unknown hollow type {self.hollow_type!r}")
        if not 5 <= self.surface_angle <= 85:
            errs.append("surface angle must be 5–85°")
        if not 1 <= self.max_cavities <= 9999:
            errs.append("maximum cavity number must be 1–9999")
        if self.infill:
            if self.infill_type not in INFILL_TYPES:
                errs.append(f"unknown infill type {self.infill_type!r}")
            if self.infill_radius <= 0 or self.infill_size <= 0:
                errs.append("infill cell size and strut radius must be positive")
            elif self.infill_radius * 4 > self.infill_size:
                errs.append("infill struts must be thinner than a quarter of the cell size")
        return errs


@dataclass
class DrainHoleSettings:
    """Defaults of SatelLite's Perforation (drain hole) page [OBSERVED EleTriStructure
    FUN_180009970]. radius1/radius2 are the two end radii (equal = cylinder), extend1/extend2
    the depth on each side of the picked surface point [INFERRED from names]."""
    radius1: float = 2.0
    radius2: float = 2.0
    extend1: float = 2.0
    extend2: float = 2.0
    use_angle_slot: bool = False
    angle_slot: float = 0.0
    use_notch: bool = False
    notch_width: float = 0.4
    notch_height: float = 0.4
    notch_angle: float = 0.0
    keep_subtracted: bool = False
    use_clearance: bool = False
    clearance: float = 0.0
    clearance_type: int = 1
    direction_id: int = 0           # /Perforation/HoleDirID
    direction: tuple[float, float, float] = (1.0, 0.0, 0.0)   # HoleDirX/Y/Z defaults


@dataclass
class StructureHollowDefaults:
    """The Structure page's hollow switch [OBSERVED EleTriStructure FUN_1800064a0]."""
    hollow: bool = True             # /Structure/Hollow/bHollow
    inside: bool = True             # /Hollowing/isInside
    thickness: float = 1.0
    accuracy: float = 1.0
