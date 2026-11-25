"""Autosave and crash recovery (09 §6).

Each running GooSlicer writes its own `autosave-<session>.3mf` (with `<session>.json` beside it:
the project's own path, if it has one, and when it was written) in `<data dir>/autosave`. It holds
an exclusive lock on `<session>.lock` for as long as it runs. At start, an autosave whose lock can
be taken belongs to a GooSlicer that is no longer running: it didn't quit cleanly, so the user is
offered to restore it. Locks, unlike process ids, work across Flatpak instances, which each have
their own pid namespace.
"""
from __future__ import annotations

import fcntl
import json
import os
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PREFIX = "autosave-"


@dataclass
class Leftover:
    session: str
    model: Path                 # the .3mf
    original: str | None        # the project's own file when it was autosaved
    written: datetime


class AutosaveStore:
    def __init__(self, directory: Path):
        self.dir = Path(directory)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.session = uuid.uuid4().hex[:12]
        self._lock = open(self.dir / f"{self.session}.lock", "w")      # held until close()
        fcntl.flock(self._lock, fcntl.LOCK_EX | fcntl.LOCK_NB)

    # ---------------------------------------------------------- this session
    @property
    def model(self) -> Path:
        return self.dir / f"{PREFIX}{self.session}.3mf"

    @property
    def info(self) -> Path:
        return self.dir / f"{self.session}.json"

    def record(self, original: str | None) -> None:
        """Written after the .3mf, so a .json always has a whole file next to it."""
        tmp = self.info.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"original": original, "written": datetime.now().isoformat(timespec="seconds")}))
        os.replace(tmp, self.info)

    def exists(self) -> bool:
        return self.model.exists()

    def remove(self) -> None:
        for p in (self.info, self.model):
            p.unlink(missing_ok=True)

    def close(self) -> None:
        """A clean quit: no autosave is left, and the lock goes."""
        self.remove()
        if not self._lock.closed:
            self._lock.close()
            (self.dir / f"{self.session}.lock").unlink(missing_ok=True)

    # ---------------------------------------------------------- other sessions
    def _orphaned(self, session: str) -> bool:
        if session == self.session:
            return False
        try:
            with open(self.dir / f"{session}.lock", "a") as fh:
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)      # released when closed
            return True
        except BlockingIOError:
            return False                                           # that GooSlicer is running

    def leftovers(self) -> list[Leftover]:
        """Autosaves of GooSlicers that are no longer running, newest first."""
        out = []
        for info in self.dir.glob("*.json"):
            session = info.stem
            model = self.dir / f"{PREFIX}{session}.3mf"
            if not self._orphaned(session):
                continue
            if not model.exists():
                self.discard(session)
                continue
            try:
                d = json.loads(info.read_text())
                written = datetime.fromisoformat(d["written"])
                original = d.get("original")
            except (OSError, ValueError, KeyError, TypeError):
                written, original = datetime.fromtimestamp(model.stat().st_mtime), None
            out.append(Leftover(session, model, original, written))
        return sorted(out, key=lambda x: x.written, reverse=True)

    def discard(self, session: str) -> None:
        for p in (self.dir / f"{session}.json", self.dir / f"{PREFIX}{session}.3mf",
                  self.dir / f"{session}.lock"):
            p.unlink(missing_ok=True)
