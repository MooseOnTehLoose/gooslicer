"""The project library's folders, designs and versions (11 §4)."""
import json
import zipfile

import pytest

from gooslicer import library as L
from gooslicer.project import PROJECT_PATH


def _design_file(path, printer="ELEGOO Saturn 4 Ultra 16K", resin="Clear", body=b""):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr(PROJECT_PATH, json.dumps({"printer": printer, "print_settings": {"resin_name": resin},
                                             "parts": [{}, {}], "saved": "2026-11-27T10:00:00Z"}))
        z.writestr("3D/3dmodel.model", body)


def test_projects_and_designs(tmp_path):
    lib = L.Library(tmp_path / "lib")
    assert lib.projects() == []
    p = lib.create_project("Lisa", "a bust in two resins")
    with pytest.raises(L.LibraryError):
        lib.create_project("Lisa")
    for bad in ("", ".hidden", "a/b", "a\\b", "x\0y"):
        with pytest.raises(L.LibraryError):
            lib.create_project(bad)
    _design_file(p.new_design_path("Flexible parts"), resin="Flexible")
    _design_file(p.new_design_path("Clear parts"))
    _design_file(p.path / "Made elsewhere.3mf")                 # not listed: still shown
    assert [d.name for d in p.designs()] == ["Flexible parts", "Clear parts", "Made elsewhere"]
    d = p.designs()[0]
    info = d.info()
    assert info["resin"] == "Flexible" and info["printer"].endswith("16K") and info["parts"] == 2
    d.set_status("ready")
    d.set_notes("print first")
    with pytest.raises(L.LibraryError):
        d.set_status("lost")
    again = L.Library(tmp_path / "lib").projects()[0]
    assert again.description == "a bust in two resins"
    assert (again.designs()[0].status, again.designs()[0].notes) == ("ready", "print first")
    r = again.rename_design(again.designs()[0], "Soft parts")
    assert r.path.is_file() and r.status == "ready"
    dup = again.duplicate_design(r, "Soft copy")
    assert dup.path.read_bytes() == r.path.read_bytes() and dup.status == "draft"
    moved = lib.rename_project(again, "Lisa bust")
    assert [d.name for d in moved.designs()][:2] == ["Soft parts", "Clear parts"]
    assert lib.find(moved.path / "Clear parts.3mf").name == "Clear parts"
    assert lib.find(tmp_path / "elsewhere.3mf") is None


def test_versions(tmp_path):
    lib = L.Library(tmp_path)
    p = lib.create_project("P")
    d = p.design("Plate")
    with pytest.raises(L.LibraryError):
        d.save_version("too early")
    _design_file(p.new_design_path("Plate"), body=b"one")
    v1 = d.save_version("first")
    _design_file(d.path, body=b"two")
    v2 = d.save_version("second")
    assert [(v.n, v.note) for v in d.versions()] == [(2, "second"), (1, "first")]
    # the design matches V2: restoring V1 needs no safety copy
    assert d.restore(1) is None and d.path.read_bytes() == v1.path.read_bytes()
    _design_file(d.path, body=b"three")                       # unsaved-as-version work
    safety = d.restore(2)
    assert safety.n == 3 and safety.note == "Before restoring V2"
    assert d.path.read_bytes() == v2.path.read_bytes()
    d.delete_version(3)
    assert [v.n for v in d.versions()] == [2, 1]
    _design_file(d.path, body=b"four")
    assert d.save_version().n == 4                            # numbers are never reused
    assert d.size_on_disk() > 0
    r = p.rename_design(d, "Renamed")
    assert [v.n for v in r.versions()] == [4, 2, 1]
