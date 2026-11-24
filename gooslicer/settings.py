"""Print settings (exposure, layers, motion) from SatelLite printer/resin profiles.

Sources [OBSERVED]:
  * printer profile INI `Printer/*`, `Advanced/*`, `Resin/*` keys (profiles.py)
  * resin profiles `resources/resin/material/<n>.data` (JSON; `platitem.vd_printer.vd_active_printer`)
The resin JSON "Space Grey 8K ABS-Like 3.0" for the 16K reproduces the header SatelLite wrote
for samples/*.goo (exposure 1.8, bottom 32 ×5, transition 10, wait-after-retract 1.2, density 1.15).
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, fields, replace
from pathlib import Path


@dataclass
class PrintSettings:
    resin_name: str = "Normal"
    layer_height: float = 0.05
    bottom_layers: int = 5
    exposure: float = 2.5
    bottom_exposure: float = 32.0
    transition_layers: int = 10
    light_off_delay: float = 0.0
    bottom_light_off_delay: float = 0.0
    wait_after_cure: float = 0.0
    bottom_wait_before_lift: float = 0.0
    bottom_wait_after_lift: float = 0.0
    bottom_wait_after_retract: float = 0.0
    bottom_lift_distance: float = 0.0
    lift_distance: float = 0.0
    bottom_lift_speed: float = 0.0
    lift_speed: float = 0.0
    bottom_retract_speed: float = 0.0
    retract_speed: float = 0.0
    slow_bottom_lift_distance: float = 0.0
    slow_lift_distance: float = 0.0
    slow_bottom_retract_distance: float = 0.0
    slow_retract_distance: float = 0.0
    slow_bottom_lift_speed: float = 0.0
    slow_lift_speed: float = 0.0
    slow_bottom_retract_speed: float = 0.0
    slow_retract_speed: float = 0.0
    bottom_light_pwm: int = 255
    light_pwm: int = 255
    density: float = 1.15          # g/cm³
    cost: float = 30.0             # per litre / kg (SatelLite: per 1000 g)
    price_unit: str = "$"
    slice_z_fraction: float = 0.5  # cut height inside a layer; SatelLite `cutposition` 0/1/2 = 0, 0.5, 1 (default 1) [OBSERVED]

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "PrintSettings":
        names = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


def _dec(x) -> float:
    return float(f"{float(x):.7g}")   # 1.7999999523 -> 1.8


# INI key (Printer/…, Advanced/…, Resin/…)  ->  field
_INI = {
    "Printer/LayerThickness": "layer_height", "Printer/BottomLayerCount": "bottom_layers",
    "Printer/ExposureTime": "exposure", "Printer/BottomExposureTime": "bottom_exposure",
    "Printer/TranslateLayerCount": "transition_layers", "Printer/LightoffDelay": "light_off_delay",
    "Printer/BottomLightoffDelay": "bottom_light_off_delay", "Printer/WaitAfterCure": "wait_after_cure",
    "Printer/BottomWaitTimeBeforeLift": "bottom_wait_before_lift",
    "Printer/BottomWaitTimeAfterLift": "bottom_wait_after_lift",
    "Printer/BottomWaitTimeAfterRetract": "bottom_wait_after_retract",
    "Printer/BottomLiftDis": "bottom_lift_distance", "Printer/LiftingDis": "lift_distance",
    "Printer/BottomLiftSpeed": "bottom_lift_speed", "Printer/LiftingSpeed": "lift_speed",
    "Printer/BottomRetractSpeed": "bottom_retract_speed", "Printer/BackSpeed": "retract_speed",
    "Printer/SlowBottomLiftDis": "slow_bottom_lift_distance", "Printer/SlowLiftDis": "slow_lift_distance",
    "Printer/SlowBottomBackDis": "slow_bottom_retract_distance", "Printer/SlowBackDis": "slow_retract_distance",
    "Printer/SlowBottomLiftSpeed": "slow_bottom_lift_speed", "Printer/SlowLiftSpeed": "slow_lift_speed",
    "Printer/SlowBottomBackSpeed": "slow_bottom_retract_speed", "Printer/SlowBackSpeed": "slow_retract_speed",
    "Advanced/BottomLightPwm": "bottom_light_pwm", "Advanced/LightPwm": "light_pwm",
    "Resin/ResinDensity": "density", "Resin/ResinCost": "cost", "Resin/ProfileName": "resin_name",
}

# resin JSON key (vd_printer_* inside vd_active_printer.*) -> field
_JSON = {
    "vd_printer_printer_para_thickness": "layer_height", "vd_printer_printer_para_bottom_layer": "bottom_layers",
    "vd_printer_printer_para_exposure_time": "exposure",
    "vd_printer_printer_para_bottom_exposure_time": "bottom_exposure",
    "vd_printer_printer_para_tanslate_layer_count": "transition_layers",
    "vd_printer_printer_para_lightoff_delay": "light_off_delay",
    "vd_printer_printer_para_bottom_lightoff_delay": "bottom_light_off_delay",
    "vd_printer_printer_para_wait_after_cure": "wait_after_cure",
    "vd_printer_printer_bottom_wait_before_lift": "bottom_wait_before_lift",
    "vd_printer_printer_bottom_wait_after_lift": "bottom_wait_after_lift",
    "vd_printer_printer_bottom_wait_after_retract": "bottom_wait_after_retract",
    "vd_printer_printer_para_bootom_lift_dis": "bottom_lift_distance",
    "vd_printer_printer_para_lift_dis": "lift_distance",
    "vd_printer_printer_para_bottom_lift_speed": "bottom_lift_speed",
    "vd_printer_printer_para_lift_speed": "lift_speed",
    "vd_printer_printer_para_bottom_back_speed": "bottom_retract_speed",
    "vd_printer_printer_para_back_speed": "retract_speed",
    "vd_printer_printer_slow_bottom_lift_dis": "slow_bottom_lift_distance",
    "vd_printer_printer_slow_lift_dis": "slow_lift_distance",
    "vd_printer_printer_slow_bottom_back_dis": "slow_bottom_retract_distance",
    "vd_printer_printer_slow_back_dis": "slow_retract_distance",
    "vd_printer_printer_slow_bottom_lift_speed": "slow_bottom_lift_speed",
    "vd_printer_printer_slow_lift_speed": "slow_lift_speed",
    "vd_printer_printer_slow_bottom_back_speed": "slow_bottom_retract_speed",
    "vd_printer_printer_slow_back_speed": "slow_retract_speed",
    "vd_printer_advanced_para_bottom_led_power": "bottom_light_pwm",
    "vd_printer_advanced_para_led_power": "light_pwm",
    "vd_printer_resin_para_density": "density", "vd_printer_resin_para_cost": "cost",
}

_INT = {"bottom_layers", "transition_layers", "bottom_light_pwm", "light_pwm"}


def _coerce(field_name: str, v):
    if field_name == "resin_name":
        return str(v)
    return int(round(float(v))) if field_name in _INT else _dec(v)


def from_printer_profile(profile) -> PrintSettings:
    kw = {f: _coerce(f, profile.get(k)) for k, f in _INI.items() if profile.get(k) is not None}
    return PrintSettings(**kw)


@dataclass(frozen=True)
class ResinProfile:
    id: str
    name: str
    printer: str
    resin_type: str
    raw: dict

    def settings(self) -> PrintSettings:
        kw = {"resin_name": self.name}
        for group in ("vd_printer_printer_para", "vd_printer_advanced_para", "vd_printer_resin_para"):
            for k, v in self.raw.get(group, {}).items():
                if k in _JSON and not isinstance(v, (list, dict)):
                    kw[_JSON[k]] = _coerce(_JSON[k], v)
        return PrintSettings(**kw)


def load_resin_profile(path: str | os.PathLike) -> ResinProfile | None:
    try:
        j = json.loads(Path(path).read_bytes().decode("utf-8-sig"))
        ap = j["platitem"]["vd_printer"]["vd_active_printer"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    return ResinProfile(
        id=Path(path).stem, name=str(ap.get("vd_resine_profilename", "")),
        printer=str(ap.get("cloudResinExtraParam", {}).get("cloudResinPrinterName", "")),
        resin_type=str(ap.get("vd_printer_resin_para", {}).get("vd_printer_resin_para_type", "")),
        raw=ap)


def resin_dir_for(profile_ini: str | os.PathLike) -> Path | None:
    """resources/machines/printer/officialPlatform.ini -> resources/resin/material."""
    p = Path(profile_ini).resolve()
    for base in (p.parent.parent.parent, p.parent):
        d = base / "resin" / "material"
        if d.is_dir():
            return d
    return None


def load_resin_profiles(directory: str | os.PathLike, printer: str | None = None) -> list[ResinProfile]:
    out = []
    for f in sorted(Path(directory).glob("*.data")):
        r = load_resin_profile(f)
        if r and r.name and (printer is None or r.printer == printer):
            out.append(r)
    out.sort(key=lambda r: r.name.lower())
    return out


def with_overrides(s: PrintSettings, **kw) -> PrintSettings:
    return replace(s, **kw)
