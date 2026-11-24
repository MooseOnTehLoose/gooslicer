import os
from pathlib import Path

import pytest

from gooslicer import profiles as P

# Lines in the exact encoding SatelLite ships (values for the 16K and Mars 4 Ultra).
FIXTURE = r'''[General]
platformIds=23

[23]
Name=ELEGOO Saturn 4 Ultra 16K
XLength=@Variant(\0\0\0\x87\x43S\xae\x14)
YWidth=@Variant(\0\0\0\x87\x42\xec\xbdq)
ZHeight=@Variant(\0\0\0\x87\x43\\\0\0)
ResX=@Variant(\0\0\0\x87\x46l@\0)
ResY=@Variant(\0\0\0\x87\x45\xc2\xb0\0)
MirrorX=false
MirrorY=true
PixX=@Variant(\0\0\0\x87\x41`\0\0)
PixY=@Variant(\0\0\0\x87\x41\x98\0\0)
UVtoolsFormat=goo
Printer\LayerThickness="@Variant(\0\0\0\x87=L\xcc\xcd)"
Printer\BottomLayerCount=5

[10]
Name=ELEGOO Mars 4 Ultra
XLength=@Variant(\0\0\0\x87\x43\x19\\))
YWidth=@Variant(\0\0\0\x87\x42\x9b\x85\x1f)
ZHeight=@Variant(\0\0\0\x87\x43%\0\0)
ResX=@Variant(\0\0\0\x87\x46\x5 \0)
ResY=@Variant(\0\0\0\x87\x45\x87\0\0)
'''


@pytest.fixture
def ini(tmp_path):
    p = tmp_path / "officialPlatform.ini"
    p.write_bytes(FIXTURE.replace("\n", "\r\n").encode())
    return p


def test_16k_profile_matches_goo_sample(ini):
    ps = P.load_profiles(ini)
    assert list(ps) == ["ELEGOO Saturn 4 Ultra 16K"]
    p = ps["ELEGOO Saturn 4 Ultra 16K"]
    assert (p.size_x, p.size_y, p.size_z) == (211.68, 118.37, 220.0)
    assert (p.res_x, p.res_y, p.pixel_x_um, p.pixel_y_um) == (15120, 6230, 14.0, 19.0)
    assert p.mirror_y and not p.mirror_x and p.file_format == "goo"
    assert p.get("Printer/LayerThickness") == 0.05 and p.get("Printer/BottomLayerCount") == 5
    assert round(p.pixel_x_mm * 1000, 1) == 14.0 and round(p.pixel_y_mm * 1000, 1) == 19.0


def test_short_hex_escape_with_space_separator(ini):
    p = P.load_profiles(ini, names=None)["ELEGOO Mars 4 Ultra"]
    assert p.res_x == 8520 and p.size_x == 153.36  # "\x5 \0" -> 05 20 ; "\\)" -> 5C 29


def test_unescape_basics():
    assert P.unescape(r'"a\\b\"c"') == 'a\\b"c'
    assert P.unescape(r"\x41\101\t") == "AA\t"
    assert P.decode_value("true") is True and P.decode_value("12") == 12


def test_find_ini_from_env_and_dir(ini, tmp_path, monkeypatch):
    monkeypatch.setenv("GOOSLICER_PROFILE_INI", str(ini))
    assert P.find_official_ini() == ini
    monkeypatch.delenv("GOOSLICER_PROFILE_INI")
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "x"))
    monkeypatch.setenv("WINEPREFIX", str(tmp_path / "w"))
    inst = tmp_path / "SatelLite"
    (inst / "resources/machines/printer").mkdir(parents=True)
    (inst / "resources/machines/printer/officialPlatform.ini").write_bytes(ini.read_bytes())
    assert P.find_official_ini([inst]).name == "officialPlatform.ini"
    assert P.find_official_ini([tmp_path / "nope"]) is None


REAL = os.environ.get("GOOSLICER_REAL_INI")


@pytest.mark.skipif(not REAL, reason="set GOOSLICER_REAL_INI to SatelLite's officialPlatform.ini")
def test_real_ini_parity_with_qsettings():
    QSettings = pytest.importorskip("PySide6.QtCore").QSettings
    secs = P.parse_ini(Path(REAL).read_bytes().decode("utf-8", "replace"))
    q = QSettings(REAL, QSettings.IniFormat)
    for sid, sec in secs.items():
        if sid == "General":
            continue
        for k, v in sec.items():
            qv = q.value(f"{sid}/{k}")
            if isinstance(v, float):
                assert float(f"{qv:.7g}") == v, (sid, k)
            elif isinstance(v, bool):
                assert str(qv).lower() == str(v).lower(), (sid, k)
            else:
                assert str(qv) == str(v), (sid, k)
    assert set(P.load_profiles(REAL)) == set(P.SATURN4_FAMILY)
