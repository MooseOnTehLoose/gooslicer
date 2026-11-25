"""GooSlicer's own copy of the SatelLite profiles (gooslicer/profile_store.py)."""
import json
import shutil

import pytest

from gooslicer import profile_store as PS
from gooslicer import profiles, settings as S, support_defaults as SD
from test_profiles import FIXTURE
from test_support_defaults import template


def resin(path, name):
    ap = {"vd_resine_profilename": name,
          "cloudResinExtraParam": {"cloudResinPrinterName": "ELEGOO Saturn 4 Ultra 16K"},
          "vd_printer_printer_para": {"vd_printer_printer_para_exposure_time": 2.2,
                                      "vd_printer_printer_para_thickness": 0.05}}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"platitem": {"vd_printer": {"vd_active_printer": ap}}}))


def install(root, resins=("Grey",), templates=("EVO_0-30mm",)):
    """A SatelLite install layout; returns its officialPlatform.ini."""
    res = root / "resources"
    ini = res / "machines" / "printer" / "officialPlatform.ini"
    ini.parent.mkdir(parents=True)
    ini.write_bytes(FIXTURE.encode())
    for i, n in enumerate(resins):
        resin(res / "resin" / "material" / f"{i}.data", n)
    (res / "script" / "common" / "ELEGOO Jupiter 2").mkdir(parents=True)
    for n in templates:
        template(res / "script" / "common" / f"{n}.satsup", n)
    if templates:
        template(res / "script" / "common" / "ELEGOO Jupiter 2" / "EVO_0-30mm.satsup", "EVO_0-30mm")
    else:
        shutil.rmtree(res / "script")
    return ini


def test_copy_then_uninstall(tmp_path):
    ini = install(tmp_path / "SatelLite", ("Grey", "Clear"), ("EVO_0-30mm", "Tree_Small"))
    store = tmp_path / "data" / "satellite"
    st = PS.copy_profiles(ini, store)
    assert st.ini == PS.stored_ini(store) and st.resins == 2 and st.templates == 3
    assert st.source == str(ini.resolve()) and st.copied > 0
    shutil.rmtree(tmp_path / "SatelLite")                        # SatelLite uninstalled
    st = PS.info(store)
    assert profiles.load_profiles(st.ini)
    # the usual next-to-the-profiles lookups find the copy
    assert S.resin_dir_for(st.ini) == st.resin_dir and SD.script_dir_for(st.ini) == st.template_dir
    assert sorted(r.name for r in S.load_resin_profiles(st.resin_dir)) == ["Clear", "Grey"]
    assert (st.template_dir / "ELEGOO Jupiter 2" / "EVO_0-30mm.satsup").is_file()
    assert [t.name for t in SD.load_templates(st.template_dir)] == ["EVO_0-30mm", "Tree_Small"]
    # copying "from" the copy changes nothing
    assert PS.copy_profiles(st.ini, store).copied == st.copied


def test_recopy_keeps_folders_it_cannot_find(tmp_path):
    store = tmp_path / "store"
    PS.copy_profiles(install(tmp_path / "a"), store)
    lone = tmp_path / "picked" / "officialPlatform.ini"          # e.g. the Flatpak file portal
    lone.parent.mkdir()
    lone.write_bytes(FIXTURE.encode())
    st = PS.copy_profiles(lone, store)
    assert st.source == str(lone.resolve()) and st.resins == 1 and st.templates == 2
    # a full install replaces everything, including resins that are gone from it
    st = PS.copy_profiles(install(tmp_path / "b", ("New1", "New2", "New3"), ()), store)
    assert st.resins == 3 and st.templates == 2                  # no templates there: kept
    assert sorted(r.name for r in S.load_resin_profiles(st.resin_dir)) == ["New1", "New2", "New3"]
    assert not (tmp_path / "store.new").exists() and not (tmp_path / "store.old").exists()


def test_replace_folder(tmp_path):
    store = tmp_path / "store"
    PS.copy_profiles(install(tmp_path / "a"), store)
    other = tmp_path / "more"
    resin(other / "x.data", "Other")
    st = PS.replace_folder(store, "resin", other)
    assert st.resins == 1 and [r.name for r in S.load_resin_profiles(st.resin_dir)] == ["Other"]
    with pytest.raises(ValueError, match="no resin profiles"):
        PS.replace_folder(store, "resin", tmp_path / "a" / "resources" / "script")
    assert PS.info(store).resins == 1                            # untouched by the failure
    with pytest.raises(FileNotFoundError):
        PS.replace_folder(tmp_path / "nothing", "templates", other)
