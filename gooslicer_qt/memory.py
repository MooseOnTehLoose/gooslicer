"""GooSlicer's memory use, shown at the left of the status bar.

Counted for GooSlicer and every process it started (the slicing and hollowing worker pools), as
each one's proportional set size (PSS) from /proc/<pid>/smaps_rollup: memory shared between them
(libraries, copy-on-write pages) is split between the sharers instead of counted once per process,
so the total is what GooSlicer really costs. RSS from /proc/<pid>/statm is the fallback where
smaps_rollup can't be read. The percentage is of the computer's total RAM (MemTotal).

Readings are taken on a background thread every INTERVAL_S seconds (well under a millisecond per
process), so the window never waits on them.
"""
from __future__ import annotations

import gc
import os
import threading
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRect, Qt, QTimer, Signal
from PySide6.QtGui import QPainter, QPalette
from PySide6.QtWidgets import QLabel, QStatusBar, QStyle, QStyleOption

INTERVAL_S = 2.0
TRIM_DELAY_MS = 1000          # after the last big job, hand freed memory back to the system
GAP = 10                      # pixels between the memory figure and a status message
_PAGE = os.sysconf("SC_PAGE_SIZE") if hasattr(os, "sysconf") else 4096


def _libc_trim():
    """glibc's malloc_trim, or None (musl, other systems)."""
    try:
        import ctypes
        import ctypes.util
        libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6")
        fn = libc.malloc_trim
        fn.argtypes, fn.restype = [ctypes.c_size_t], ctypes.c_int
        return fn
    except (OSError, AttributeError):
        return None


_trim = _libc_trim()
_trim_timer: QTimer | None = None


def trim_memory() -> None:
    """Return memory Python has freed to the system. glibc keeps freed blocks in its heaps (one
    per thread that allocated) for reuse, so after loading or repairing a large mesh the process
    can hold hundreds of MB it no longer uses; malloc_trim releases them (a few ms). A full
    garbage collection goes first: trimesh meshes sit in reference cycles (their caches point
    back at them), so a dropped mesh is only freed by the cycle collector, which Python runs by
    object count, not size, and so rarely after a few huge arrays."""
    gc.collect()
    if _trim is not None:
        _trim(0)


def trim_memory_soon() -> None:
    """trim_memory() once things have settled: TRIM_DELAY_MS after the last call (GUI thread)."""
    global _trim_timer
    if _trim_timer is None:
        _trim_timer = QTimer()
        _trim_timer.setSingleShot(True)
        _trim_timer.setInterval(TRIM_DELAY_MS)
        _trim_timer.timeout.connect(trim_memory)
    _trim_timer.start()


@dataclass
class MemoryUse:
    used: int                 # bytes
    total: int                # bytes of RAM in the computer (0 if unknown)
    processes: int

    @property
    def percent(self) -> float:
        return 100.0 * self.used / self.total if self.total else 0.0

    def text(self) -> str:
        return f"{self.used / 1e9:.2f} GB · {self.percent:.1f} %" if self.total else f"{self.used / 1e9:.2f} GB"

    def tooltip(self) -> str:
        who = "GooSlicer uses" if self.processes == 1 else \
            f"GooSlicer and its {self.processes - 1} worker process{'es' if self.processes > 2 else ''} use"
        tail = f", {self.percent:.1f} % of this computer's {self.total / 1e9:.1f} GB" if self.total else ""
        return f"Memory: {who} {self.used / 1e9:.2f} GB{tail}"


def descendants(pid: int) -> list[int]:
    """pid's child processes, their children and so on (from /proc/<pid>/task/*/children)."""
    out, todo = [], [pid]
    while todo:
        p = todo.pop()
        try:
            tasks = os.listdir(f"/proc/{p}/task")
        except OSError:
            continue
        for t in tasks:
            try:
                with open(f"/proc/{p}/task/{t}/children") as f:
                    kids = [int(x) for x in f.read().split()]
            except (OSError, ValueError):
                continue
            out += kids
            todo += kids
    return out


def process_bytes(pid: int) -> int:
    """PSS of one process, else its RSS; 0 if it has gone."""
    try:
        with open(f"/proc/{pid}/smaps_rollup") as f:
            for line in f:
                if line.startswith("Pss:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    try:
        with open(f"/proc/{pid}/statm") as f:
            return int(f.read().split()[1]) * _PAGE
    except (OSError, ValueError, IndexError):
        return 0


def total_ram() -> int:
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
    except (OSError, ValueError, IndexError):
        pass
    return 0


def memory_use(pid: int | None = None) -> MemoryUse:
    pid = os.getpid() if pid is None else pid
    procs = [pid] + descendants(pid)
    return MemoryUse(sum(process_bytes(p) for p in procs), total_ram(), len(procs))


class MemoryMonitor(QObject):
    """Reads memory_use() every INTERVAL_S seconds on its own thread; updated(MemoryUse)."""
    updated = Signal(object)

    def __init__(self, parent=None, interval: float = INTERVAL_S):
        super().__init__(parent)
        self.interval = interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is None or not self._thread.is_alive():
            self._stop.clear()
            self._thread = threading.Thread(target=self._run, name="memory", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        while True:
            try:
                self.updated.emit(memory_use())
            except RuntimeError:              # the receiver is gone (closing)
                return
            if self._stop.wait(self.interval):
                return


class StatusBar(QStatusBar):
    """A status bar with GooSlicer's memory use fixed at its left: status messages are drawn to its
    right instead of over it. (QStatusBar hides its own left-hand widgets while a message shows,
    so the figure is a child placed by hand, and the message is painted here.)"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._permanent: list = []                       # right-hand widgets (progress, Cancel)
        self.memory = QLabel(self)
        self.memory.setObjectName("memoryLabel")
        self.messageChanged.connect(lambda _m: self.update())
        self.set_usage(None)

    def set_usage(self, use: MemoryUse | None) -> None:
        if use is None:
            self.memory.setText("Memory …")
            self.memory.setToolTip("GooSlicer's memory use")
        else:
            self.memory.setText(f"Memory {use.text()}")
            self.memory.setToolTip(use.tooltip())
        self.memory.adjustSize()
        self._place()
        self.update()

    def _place(self) -> None:
        h = self.memory.sizeHint().height()
        self.memory.move(6, max(0, (self.height() - h) // 2))
        self.memory.raise_()

    def resizeEvent(self, e) -> None:
        super().resizeEvent(e)
        self._place()

    def addPermanentWidget(self, widget, stretch: int = 0) -> None:
        self._permanent.append(widget)
        super().addPermanentWidget(widget, stretch)

    def message_rect(self) -> QRect:
        """From after the memory figure to before the first visible right-hand widget."""
        left = self.memory.geometry().right() + 1 + GAP
        right = self.width() - 12
        for w in self._permanent:
            if w.isVisible():
                right = min(right, w.x() - 2)
        return QRect(left, 0, max(0, right - left), self.height())

    def paintEvent(self, e) -> None:
        # as QStatusBar::paintEvent (panel, item frames), with the message moved right
        p = QPainter(self)
        opt = QStyleOption()
        opt.initFrom(self)
        self.style().drawPrimitive(QStyle.PE_PanelStatusBar, opt, p, self)
        msg = self.currentMessage()
        x = self.memory.geometry().right() + 1 + GAP // 2
        p.setPen(self.palette().color(QPalette.Mid))
        p.drawLine(x, 4, x, self.height() - 5)                # a divider after the figure
        if msg:
            r = self.message_rect()
            p.setPen(self.palette().color(QPalette.WindowText))
            p.drawText(r, Qt.AlignLeading | Qt.AlignVCenter | Qt.TextSingleLine,
                       p.fontMetrics().elidedText(msg, Qt.ElideRight, r.width()))
        p.end()
