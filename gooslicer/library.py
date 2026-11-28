"""The project library (11 §4): library → projects → designs → versions, as plain folders. Qt-free.

  <root>/<Project>/project.json                 name, description, created, designs (status, notes)
  <root>/<Project>/<Design>.3mf                 an ordinary GooSlicer project (09 §2)
  <root>/<Project>/.versions/<Design>/v<N>.3mf  explicit checkpoints ("Save version…")
  <root>/<Project>/.versions/<Design>/versions.json
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import zipfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .project import PROJECT_PATH

FORMAT = "gooslicer-library-project"
VERSION = 1
META = "project.json"
VERSIONS = ".versions"
STATUSES = ("draft", "ready", "printed")
DEFAULT_ROOT = Path.home() / "GooSlicer Library"


class LibraryError(ValueError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def check_name(name: str) -> str:
    """A project or design name, used as a file name: no separators, NUL or leading dot."""
    n = (name or "").strip()
    if not n or n.startswith(".") or any(c in n for c in "/\\\0") or n in (".", ".."):
        raise LibraryError(f"{name!r} can't be used as a name")
    if len(n.encode()) > 200:
        raise LibraryError("name too long")
    return n


def _write_json(path: Path, doc) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(doc, indent=1))
    os.replace(tmp, path)


def _digest(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(1 << 20), b""):
            h.update(b)
    return h.hexdigest()


def design_info(path: Path) -> dict:
    """Printer, resin, saved time and thumbnail of a design, read from its 3MF's project.json
    without loading meshes. Missing values are left out."""
    out: dict = {}
    try:
        with zipfile.ZipFile(path) as z:
            names = set(z.namelist())
            if PROJECT_PATH in names and z.getinfo(PROJECT_PATH).file_size < (64 << 20):
                doc = json.loads(z.read(PROJECT_PATH))
                out["printer"] = doc.get("printer") or ""
                ps = doc.get("print_settings") or {}
                out["resin"] = ps.get("resin_name", "") if isinstance(ps, dict) else ""
                out["saved"] = doc.get("saved", "")
                out["parts"] = len(doc.get("parts") or [])
                out["plates"] = int(doc.get("plates", 1) or 1)
            if "Metadata/thumbnail.png" in names and z.getinfo("Metadata/thumbnail.png").file_size < (8 << 20):
                out["thumbnail"] = z.read("Metadata/thumbnail.png")
    except (OSError, zipfile.BadZipFile, ValueError, KeyError):
        out["error"] = "unreadable"
    return out


@dataclass
class Version:
    n: int
    note: str
    saved: str
    app: str
    path: Path


class Design:
    def __init__(self, project: "LibProject", file: str):
        self.project = project
        self.file = file

    @property
    def name(self) -> str:
        return Path(self.file).stem

    @property
    def path(self) -> Path:
        return self.project.path / self.file

    def _entry(self) -> dict:
        return self.project._entry(self.file)

    @property
    def status(self) -> str:
        return self._entry().get("status", "draft")

    @property
    def notes(self) -> str:
        return self._entry().get("notes", "")

    def set_status(self, status: str) -> None:
        if status not in STATUSES:
            raise LibraryError(f"unknown status {status!r}")
        self._entry()["status"] = status
        self.project.save()

    def set_notes(self, notes: str) -> None:
        self._entry()["notes"] = str(notes)
        self.project.save()

    # ---------------------------------------------------------------- versions
    @property
    def version_dir(self) -> Path:
        return self.project.path / VERSIONS / self.name

    def _vdoc(self) -> list:
        f = self.version_dir / "versions.json"
        try:
            doc = json.loads(f.read_text())
            return doc if isinstance(doc, list) else []
        except (OSError, ValueError):
            return []

    def versions(self) -> list[Version]:
        """Newest first; only those whose file exists."""
        out = []
        for e in self._vdoc():
            try:
                n = int(e["n"])
            except (KeyError, TypeError, ValueError):
                continue
            p = self.version_dir / f"v{n}.3mf"
            if p.is_file():
                out.append(Version(n, str(e.get("note", "")), str(e.get("saved", "")), str(e.get("app", "")), p))
        return sorted(out, key=lambda v: -v.n)

    def save_version(self, note: str = "", app: str = "") -> Version:
        """Copy the design's file as the next version (numbers are never reused)."""
        if not self.path.is_file():
            raise LibraryError(f"{self.name} has not been saved yet")
        doc = self._vdoc()
        n = max([int(e.get("n", 0)) for e in doc] + [0]) + 1
        self.version_dir.mkdir(parents=True, exist_ok=True)
        dst = self.version_dir / f"v{n}.3mf"
        shutil.copy2(self.path, dst)
        entry = {"n": n, "note": str(note), "saved": _now(), "app": app}
        doc.append(entry)
        _write_json(self.version_dir / "versions.json", doc)
        return Version(n, entry["note"], entry["saved"], app, dst)

    def delete_version(self, n: int) -> None:
        doc = self._vdoc()
        keep = [e for e in doc if int(e.get("n", -1)) != int(n)]
        (self.version_dir / f"v{int(n)}.3mf").unlink(missing_ok=True)
        # the highest number stays recorded so it isn't reused
        if keep != doc and not any(int(e.get("n", 0)) > int(n) for e in keep):
            keep.append({"n": int(n), "deleted": True})
        _write_json(self.version_dir / "versions.json", keep)

    def matches_a_version(self) -> bool:
        if not self.path.is_file():
            return True
        d = _digest(self.path)
        return any(_digest(v.path) == d for v in self.versions())

    def restore(self, n: int, app: str = "") -> Version | None:
        """Copy version n over the design. When the design differs from every version, it is
        first kept as a new version "Before restoring V<n>" (returned)."""
        src = self.version_dir / f"v{int(n)}.3mf"
        if not src.is_file():
            raise LibraryError(f"version {n} is missing")
        safety = None if self.matches_a_version() else self.save_version(f"Before restoring V{int(n)}", app)
        tmp = self.path.with_name(self.path.name + ".tmp")
        shutil.copy2(src, tmp)
        os.replace(tmp, self.path)
        return safety

    def size_on_disk(self) -> int:
        total = self.path.stat().st_size if self.path.is_file() else 0
        if self.version_dir.is_dir():
            total += sum(f.stat().st_size for f in self.version_dir.iterdir() if f.is_file())
        return total

    def info(self) -> dict:
        return design_info(self.path) if self.path.is_file() else {}


class LibProject:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.meta = self._load()

    def _load(self) -> dict:
        try:
            doc = json.loads((self.path / META).read_text())
            if not isinstance(doc, dict):
                raise ValueError
        except (OSError, ValueError):
            doc = {}
        doc.setdefault("format", FORMAT)
        doc.setdefault("version", VERSION)
        doc.setdefault("name", self.path.name)
        doc.setdefault("description", "")
        doc.setdefault("created", "")
        if not isinstance(doc.get("designs"), list):
            doc["designs"] = []
        doc["designs"] = [d for d in doc["designs"] if isinstance(d, dict) and isinstance(d.get("file"), str)]
        return doc

    def save(self) -> None:
        self.path.mkdir(parents=True, exist_ok=True)
        _write_json(self.path / META, self.meta)

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def description(self) -> str:
        return str(self.meta.get("description", ""))

    def set_description(self, text: str) -> None:
        self.meta["description"] = str(text)
        self.save()

    def _entry(self, file: str) -> dict:
        for d in self.meta["designs"]:
            if d["file"] == file:
                return d
        d = {"file": file, "status": "draft", "notes": ""}
        self.meta["designs"].append(d)
        return d

    def designs(self) -> list[Design]:
        """Listed designs in their order, then .3mf files found in the folder that aren't listed."""
        files = [d["file"] for d in self.meta["designs"]]
        on_disk = sorted(p.name for p in self.path.glob("*.3mf") if p.is_file())
        files = [f for f in files if f in on_disk] + [f for f in on_disk if f not in files]
        return [Design(self, f) for f in files]

    def design(self, name: str) -> Design:
        return Design(self, check_name(name) + ".3mf")

    def new_design_path(self, name: str) -> Path:
        d = self.design(name)
        if d.path.exists():
            raise LibraryError(f"there is already a design called {d.name}")
        self._entry(d.file)
        self.save()
        return d.path

    def rename_design(self, design: Design, new: str) -> Design:
        nd = self.design(new)
        if nd.path.exists():
            raise LibraryError(f"there is already a design called {nd.name}")
        if design.path.exists():
            os.replace(design.path, nd.path)
        if design.version_dir.exists():
            os.replace(design.version_dir, nd.version_dir)
        e = design._entry()
        e["file"] = nd.file
        self.save()
        return nd

    def duplicate_design(self, design: Design, new: str) -> Design:
        nd = self.design(new)
        if nd.path.exists():
            raise LibraryError(f"there is already a design called {nd.name}")
        shutil.copy2(design.path, nd.path)
        self._entry(nd.file).update(status="draft", notes=design.notes)
        self.save()
        return nd

    def forget_design(self, design: Design) -> None:
        """Drop a design from the list (after its files were moved to the trash)."""
        self.meta["designs"] = [d for d in self.meta["designs"] if d["file"] != design.file]
        self.save()


class Library:
    def __init__(self, root: str | os.PathLike = DEFAULT_ROOT):
        self.root = Path(root)

    def projects(self) -> list[LibProject]:
        if not self.root.is_dir():
            return []
        return [LibProject(p) for p in sorted(self.root.iterdir(), key=lambda p: p.name.lower())
                if p.is_dir() and not p.name.startswith(".")]

    def create_project(self, name: str, description: str = "") -> LibProject:
        path = self.root / check_name(name)
        if path.exists():
            raise LibraryError(f"there is already a project called {path.name}")
        path.mkdir(parents=True)
        p = LibProject(path)
        p.meta.update(name=path.name, description=description, created=_now())
        p.save()
        return p

    def rename_project(self, project: LibProject, new: str) -> LibProject:
        path = self.root / check_name(new)
        if path.exists():
            raise LibraryError(f"there is already a project called {path.name}")
        os.replace(project.path, path)
        p = LibProject(path)
        p.meta["name"] = path.name
        p.save()
        return p

    def find(self, file: str | os.PathLike) -> Design | None:
        """The library design a project file is, if it lies directly in a project folder."""
        try:
            f = Path(file).resolve()
            root = self.root.resolve()
        except OSError:
            return None
        if f.suffix.lower() != ".3mf" or f.parent.parent != root or f.parent.name.startswith("."):
            return None
        return Design(LibProject(f.parent), f.name)
