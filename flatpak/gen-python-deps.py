#!/usr/bin/env python3
"""Write python3-deps.json: a flatpak-builder module that installs GooSlicer's Python
dependencies offline from pinned PyPI wheels (url + sha256).

Versions are those installed in the project venv (the tested set), PySide6-Essentials (Qt
itself) included: the runtime is plain freedesktop.

Usage: gen-python-deps.py --python 3.14 [--arch x86_64] [--venv .venv] [-o python3-deps.json]
"""
from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKIP = {"pyside6", "pyside6-addons", "gooslicer", "pytest", "pluggy", "iniconfig", "pygments", "pip",
        "setuptools", "wheel"}


def norm(name: str) -> str:
    return name.lower().replace("_", "-").replace(".", "-")


def runtime_requirements(venv: Path) -> list[str]:
    """Pinned name==version for pyproject's dependencies and theirs (transitively), at the
    versions installed in the venv. pyproject.toml is the source of truth for the list (the
    venv's own gooslicer metadata may be stale)."""
    import tomllib
    top = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["dependencies"]
    code = r"""
import importlib.metadata as md, re, json, sys
from packaging.requirements import Requirement
top = [Requirement(r) for r in json.loads(sys.argv[1])]
seen, todo = {}, [r.name for r in top if not r.marker or r.marker.evaluate({"extra": ""})]
while todo:
    n = todo.pop()
    k = re.sub(r"[-_.]+", "-", n).lower()
    if k in seen:
        continue
    try:
        d = md.distribution(n)
    except md.PackageNotFoundError:
        sys.exit("not installed in the venv: " + n)
    seen[k] = d.version
    for r in d.requires or []:
        req = Requirement(r)
        if req.marker and not req.marker.evaluate({"extra": ""}):
            continue
        todo.append(req.name)
print(json.dumps(seen))
"""
    out = subprocess.check_output([str(venv / "bin/python"), "-c", code, json.dumps(top)], text=True)
    pins = json.loads(out)
    return [f"{n}=={v}" for n, v in sorted(pins.items()) if norm(n) not in SKIP]


def pypi_file(name: str, version: str, filename: str) -> dict:
    with urllib.request.urlopen(f"https://pypi.org/pypi/{name}/{version}/json", timeout=60) as r:
        data = json.load(r)
    for f in data["urls"]:
        if f["filename"] == filename:
            return f
    raise SystemExit(f"{filename} not found on PyPI for {name} {version}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--python", required=True, help="runtime Python version, e.g. 3.13")
    ap.add_argument("--arch", default="x86_64")
    ap.add_argument("--venv", default=str(ROOT / ".venv"))
    ap.add_argument("-o", "--output", default=str(Path(__file__).with_name("python3-deps.json")))
    a = ap.parse_args()

    reqs = runtime_requirements(Path(a.venv))
    plats = [f"manylinux_2_{v}_{a.arch}" for v in (44, 43, 42, 41, 40, 39, 34, 31, 28, 27, 24, 17)] + \
            [f"manylinux2014_{a.arch}", f"manylinux1_{a.arch}"]
    with tempfile.TemporaryDirectory() as tmp:
        cmd = [sys.executable, "-m", "pip", "download", "--no-deps", "--only-binary=:all:",
               "--python-version", a.python, "--implementation", "cp", "--dest", tmp]
        for p in plats:
            cmd += ["--platform", p]
        subprocess.check_call(cmd + reqs)
        sources = []
        for whl in sorted(Path(tmp).glob("*.whl")):
            name, version = whl.name.split("-")[:2]
            info = pypi_file(name, version, whl.name)
            sha = hashlib.sha256(whl.read_bytes()).hexdigest()
            if sha != info["digests"]["sha256"]:
                raise SystemExit(f"checksum mismatch for {whl.name}")
            sources.append({"type": "file", "url": info["url"], "sha256": sha})
    names = " ".join(reqs)          # pinned: the tested set
    module = {
        "name": "python3-deps",
        "buildsystem": "simple",
        "build-commands": [
            "pip3 install --verbose --no-index --find-links=\"file://${PWD}\" "
            f"--prefix=${{FLATPAK_DEST}} --no-build-isolation --ignore-installed {names}"
        ],
        "sources": sources,
    }
    Path(a.output).write_text(json.dumps(module, indent=2) + "\n")
    print(f"{len(sources)} wheels -> {a.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
