"""gooslicer.gpuhost: loading the host's ROCm from inside the Flatpak (11, Picture → GPU)."""
import os

from gooslicer import gpuhost


def test_outside_the_flatpak_nothing_is_loaded(monkeypatch):
    monkeypatch.setattr(gpuhost, "in_flatpak", lambda: False)
    monkeypatch.setattr(gpuhost, "_loaded", {})
    assert gpuhost.preload() is None
    assert gpuhost.loaded() == {}


def test_the_runtime_s_own_library_wins(monkeypatch):
    monkeypatch.setattr(gpuhost, "_loaded", {})
    gpuhost._load("libm.so.6")
    assert gpuhost.loaded()["libm.so.6"] == "runtime"


def test_a_library_nowhere_says_which(monkeypatch, tmp_path):
    monkeypatch.setattr(gpuhost, "_loaded", {})
    monkeypatch.setattr(gpuhost, "HOST_LIBS", (str(tmp_path),))
    try:
        gpuhost._load("libnot-a-real-library.so.9")
    except OSError as e:
        assert "libnot-a-real-library.so.9" in str(e)
    else:
        raise AssertionError("loaded a library that doesn't exist")


def test_a_missing_dependency_is_named(monkeypatch, tmp_path):
    """A host library is found by full path; the dlopen error names what it needs."""
    monkeypatch.setattr(gpuhost, "_loaded", {})
    monkeypatch.setattr(gpuhost, "HOST_LIBS", (str(tmp_path),))
    (tmp_path / "libfake.so.1").write_bytes(b"not an ELF file")
    try:
        gpuhost._load("libfake.so.1")
    except OSError as e:
        assert "libfake.so.1" in str(e)
    assert "libfake.so.1" not in gpuhost.loaded()
    assert not os.environ.get("LD_LIBRARY_PATH", "").count("/run/host")
