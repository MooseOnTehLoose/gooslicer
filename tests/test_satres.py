"""SatelLite .satres resin profile import (05-slicing-spec §9)."""
from pathlib import Path

import pytest

from gooslicer import settings as S
from test_support_defaults import write_map

ROOT = "printer/scriptmanager/scripcustomtnum"
SAMPLE = Path(__file__).resolve().parent.parent / "samples" / "ELEGOO Saturn 4 Ultra 16K.satres"


def satres(path, profiles):
    """profiles: [(name, printer, {key: value})] -> a .satres like SatelLite's export."""
    m = {ROOT: len(profiles)}
    for i, (name, printer, keys) in enumerate(profiles):
        pre = f"{ROOT}{i}/scriptcontainername"
        m[pre] = name
        m[f"{pre}/ExtraResinParameters/PrinterName"] = printer
        m[f"{pre}/company"] = "Maker"
        for k, v in keys.items():
            m[f"{pre}/{k}"] = v
    write_map(path, m)
    return path


FULL = {
    "print/thickness": 0.05000000074505806, "print/bottomlayer": 4, "print/exposuretime": 1.600000023841858,
    "print/bottomexposuretime": 20.0, "print/translate_layer_count": 6, "print/lightoffdelay": 0.5,
    "print/bottomlightoffdelay": 3.0, "print/wait_after_cure": 0.25, "print/bottom_wait_before_lift": 0.1,
    "print/bottom_wait_after_lift": 0.2, "print/bottom_wait_after_retract": 3.0, "print/bottomliftdis": 6.0,
    "print/liftingdis": 5.0, "print/bottomliftspeed": 60.0, "print/liftingspeed": 65.0,
    "print/bottom_backspeed": 140.0, "print/backspeed": 150.0, "print/slow_bottom_lift_dis": 1.0,
    "print/slow_lift_dis": 1.5, "print/slow_bottom_back_dis": 2.0, "print/slow_back_dis": 2.5,
    "print/slow_bottom_lift_speed": 10.0, "print/slow_lift_speed": 11.0, "print/slow_bottom_back_speed": 12.0,
    "print/slow_back_speed": 13.0, "advanced/bottom_ledpower": 200.0, "advanced/ledpower": 180.0,
    "resin/density": 1.149999976158142, "resin/cost": 42.0, "resin/type": "Normal",
    "print/first_layer_exposure_time": 50.0, "advanced/imageblur": False,
}


def test_load_satres_maps_every_setting(tmp_path):
    f = satres(tmp_path / "x.satres", [("Blu Clear", "ELEGOO Saturn 4 Ultra 16K", FULL),
                                       ("Bare", "ELEGOO Saturn 4", {})])
    a, b = S.load_satres(f)
    assert (a.name, a.printer, a.resin_type) == ("Blu Clear", "ELEGOO Saturn 4 Ultra 16K", "Normal")
    assert a.settings() == S.PrintSettings(
        resin_name="Blu Clear", layer_height=0.05, bottom_layers=4, exposure=1.6, bottom_exposure=20.0,
        transition_layers=6, light_off_delay=0.5, bottom_light_off_delay=3.0, wait_after_cure=0.25,
        bottom_wait_before_lift=0.1, bottom_wait_after_lift=0.2, bottom_wait_after_retract=3.0,
        bottom_lift_distance=6.0, lift_distance=5.0, bottom_lift_speed=60.0, lift_speed=65.0,
        bottom_retract_speed=140.0, retract_speed=150.0, slow_bottom_lift_distance=1.0,
        slow_lift_distance=1.5, slow_bottom_retract_distance=2.0, slow_retract_distance=2.5,
        slow_bottom_lift_speed=10.0, slow_lift_speed=11.0, slow_bottom_retract_speed=12.0,
        slow_retract_speed=13.0, bottom_light_pwm=200, light_pwm=180, density=1.15, cost=42.0)
    # a profile without values falls back to the defaults
    assert b.settings() == S.PrintSettings(resin_name="Bare") and b.printer == "ELEGOO Saturn 4"


@pytest.mark.parametrize("content", [b"", b"not a qt stream", b"\x00\x00\x00\x05\x00"])
def test_load_satres_rejects_other_files(tmp_path, content):
    f = tmp_path / "bad.satres"
    f.write_bytes(content)
    with pytest.raises(ValueError, match="not a SatelLite resin profile"):
        S.load_satres(f)


def test_load_satres_rejects_other_qt_maps(tmp_path):
    f = tmp_path / "sup.satres"
    write_map(f, {"support/scriptmanager/scripcustomtnum": 1})          # a support template
    with pytest.raises(ValueError, match="no resin profiles"):
        S.load_satres(f)
    write_map(f, {ROOT: 0})
    with pytest.raises(ValueError, match="no resin profiles"):
        S.load_satres(f)


def test_store_and_merge(tmp_path):
    import os
    store = tmp_path / "resins"
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = satres(tmp_path / "a" / "Saturn.satres", [("Shared", "P", {"print/exposuretime": 9.0}), ("Only", "P", {})])
    d1, profs = S.store_satres(a, store)
    assert [r.name for r in profs] == ["Shared", "Only"]
    assert S.store_satres(a, store)[0] == d1 and len(list(store.iterdir())) == 1     # re-import: no-op
    b = satres(tmp_path / "b" / "Saturn.satres", [("Shared", "P", {"print/exposuretime": 7.0})])
    d2, _ = S.store_satres(b, store)                                    # same file name, other content: both kept
    assert d2 != d1 and len(list(store.iterdir())) == 2
    os.utime(d1, (1_000, 1_000))
    os.utime(d2, (2_000, 2_000))                                         # b imported later
    imported = S.load_imported_resins(store)
    official = [S.ResinProfile("1", "Shared", "P", "", {"vd_printer_printer_para": {
        "vd_printer_printer_para_exposure_time": 2.0}}), S.ResinProfile("2", "Aaa", "P", "", {}),
        S.ResinProfile("3", "Shared", "Q", "", {})]
    merged = S.merge_resins(official, imported)
    assert [(r.name, r.printer) for r in merged] == [("Aaa", "P"), ("Only", "P"), ("Shared", "P"), ("Shared", "Q")]
    assert next(r for r in merged if r.name == "Shared" and r.printer == "P").settings().exposure == 7.0  # newest wins
    assert S.load_imported_resins(tmp_path / "missing") == []
    (store / "junk.satres").write_bytes(b"junk")                                     # a broken file is skipped
    assert len(S.load_imported_resins(store)) == 3


@pytest.mark.skipif(not SAMPLE.exists(), reason="samples/ not present")
def test_sample_satres():
    ps = S.load_satres(SAMPLE)
    assert len(ps) == 36 and {r.printer for r in ps} == {"ELEGOO Saturn 4 Ultra 16K"}
    blu = next(r for r in ps if r.name == "SirayaTech - Blu (Clear V2 / Emerald Blue)").settings()
    assert (blu.exposure, blu.bottom_exposure, blu.bottom_layers, blu.bottom_light_off_delay) == (1.6, 20.0, 5, 3.0)
    assert (blu.layer_height, blu.transition_layers, blu.density) == (0.05, 10, 1.15)


# ---------------------------------------------------------------- export and library (§10)
from gooslicer import qdatastream as Q  # noqa: E402
from gooslicer import resin_library as RL  # noqa: E402


def _typed(path):
    class TR(Q.Reader):
        def variant(self):
            t = self.u32()
            self.take(1)
            return (t, self.value(t))
    r = TR(Path(path).read_bytes())
    m = r.variant_map()
    assert r.o == len(r.d)
    return m


def test_writer_round_trip_and_key_order(tmp_path):
    typed = {"b": (Q.INT, -3), "a": (Q.STRING, "é ✓"), "c/x": (Q.BOOL, True), "d": (Q.FLOAT, 1.5),
             "e": (Q.DOUBLE, 2.25), "f": (Q.ULONG, 7)}
    Q.write_variant_map(tmp_path / "m", typed)
    back = _typed(tmp_path / "m")
    assert back == typed
    assert list(back) == sorted(typed, reverse=True)             # Qt 5 writes a QMap back to front


def test_export_round_trips_imported_profiles(tmp_path):
    src = satres(tmp_path / "in.satres", [("Blu Clear", "ELEGOO Saturn 4 Ultra 16K", FULL), ("Bare", "P", {})])
    profs = S.load_satres(src)
    RL.write_satres(tmp_path / "out.satres", profs)
    again = S.load_satres(tmp_path / "out.satres")
    assert [(r.name, r.printer, r.settings(), r.company) for r in again] == \
           [(r.name, r.printer, r.settings(), r.company) for r in profs]
    m = _typed(tmp_path / "out.satres")
    pre = f"{ROOT}0/scriptcontainername"
    assert m[ROOT] == (Q.INT, 2)
    assert m[f"{pre}/print/first_layer_exposure_time"] == (Q.FLOAT, 50.0)          # unused value kept
    assert m[f"{pre}/advanced/imageblur"] == (Q.BOOL, False)
    assert m[f"{pre}/print/bottomlayer"] == (Q.INT, 4) and m[f"{pre}/advanced/ledpower"] == (Q.DOUBLE, 180.0)
    # every profile carries SatelLite's full key set
    assert {k[len(pre) + 1:] for k in m if k.startswith(pre + "/")} == {k for k, _, _ in RL.SCHEMA}


def test_export_from_satellite_data_profile(tmp_path):
    raw = {"vd_resine_profilename": "Grey", "vd_resine_company": "ELEGOO",
           "vd_printer_printer_para": {"vd_printer_printer_para_exposure_time": "2.5",
                                       "vd_printer_printer_para_bottom_layer": 6.0,
                                       "vd_printer_printer_first_layer_exposure_time": 40},
           "vd_printer_advanced_para": {"vd_printer_advanced_para_anti": "true",
                                        "vd_printer_advanced_para_led_power": 230},
           "vd_printer_resin_para": {"vd_printer_resin_para_type": "Standard", "vd_printer_resin_para_density": 1.1}}
    r = S.ResinProfile("12", "Grey", "ELEGOO Saturn 4", "Standard", raw)
    RL.write_satres(tmp_path / "g.satres", [r])
    (back,) = S.load_satres(tmp_path / "g.satres")
    assert back.settings() == r.settings() and back.company == "ELEGOO" and back.resin_type == "Standard"
    m = _typed(tmp_path / "g.satres")
    pre = f"{ROOT}0/scriptcontainername"
    assert m[f"{pre}/advanced/isanti"] == (Q.BOOL, True) and m[f"{pre}/print/first_layer_exposure_time"] == (Q.FLOAT, 40.0)
    assert m[f"{pre}/ExtraResinParameters/PrinterName"] == (Q.STRING, "ELEGOO Saturn 4")


@pytest.mark.skipif(not SAMPLE.exists(), reason="samples/ not present")
def test_sample_reexport_is_byte_identical(tmp_path):
    RL.write_satres(tmp_path / "x.satres", S.load_satres(SAMPLE))
    assert (tmp_path / "x.satres").read_bytes() == SAMPLE.read_bytes()


def test_names():
    ps = [S.ResinProfile("1", "Grey", "P", "", {}), S.ResinProfile("2", "Grey copy", "Q", "", {})]
    assert RL.check_new_name("  grey ", ps) and RL.check_new_name("GREY COPY", ps)   # any printer, any case
    assert RL.check_new_name("", ps) == "Enter a name."
    assert RL.check_new_name("Grey 2", ps) is None
    assert RL.copy_name("Grey", ps) == "Grey copy 2"


def test_copy_edit_delete(tmp_path):
    lib = tmp_path / "resins"
    src = satres(tmp_path / "in.satres", [("A", "P", FULL), ("B", "P", {})])
    stored, (a, b) = S.store_satres(src, lib)
    a, b = S.load_imported_resins(lib)
    assert a.source == "imported" and Path(a.path) == stored

    c = RL.create_copy(a, " Mine ", lib / "custom")
    assert (c.name, c.printer, c.source) == ("Mine", "P", "custom") and c.settings().exposure == 1.6
    assert S.load_imported_resins(lib / "custom", "custom") == [c]
    c2 = RL.save_custom(RL.with_settings(c, S.with_overrides(c.settings(), exposure=2.2, light_pwm=200)))
    assert c2.settings().exposure == 2.2 and c2.settings().light_pwm == 200
    assert c2.settings().bottom_exposure == 20.0 and c2.company == "Maker"      # the rest is kept
    with pytest.raises(ValueError):
        RL.save_custom(a)                                                       # imports are read-only

    RL.delete_from_file(a)                                     # one of two: the file is rewritten
    assert [r.name for r in S.load_imported_resins(lib)] == ["B"]
    RL.delete_from_file(S.load_imported_resins(lib)[0])        # the last one: the file goes
    assert not stored.exists()
    RL.delete_from_file(c2)
    assert list((lib / "custom").iterdir()) == []
    with pytest.raises(ValueError):
        RL.delete_from_file(S.ResinProfile("1", "X", "P", "", {}))             # SatelLite's own: never
