"""Background jobs on the global QThreadPool with Qt signals back to the GUI thread."""
from __future__ import annotations

import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_live: set = set()  # keep signal objects alive until delivered


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Job(QRunnable):
    def __init__(self, fn, *args, **kw):
        super().__init__()
        self.fn, self.args, self.kw = fn, args, kw
        self.signals = _Signals()

    def run(self):
        try:
            res = self.fn(*self.args, **self.kw)
        except Exception as e:  # report every failure to the UI
            self.signals.failed.emit(f"{e}\n{traceback.format_exc(limit=3)}")
        else:
            self.signals.done.emit(res)


def submit(fn, *args, on_done=None, on_failed=None, **kw) -> Job:
    job = Job(fn, *args, **kw)
    sig = job.signals
    _live.add(sig)

    def finish(*_):
        _live.discard(sig)
    if on_done:
        sig.done.connect(on_done)
    if on_failed:
        sig.failed.connect(on_failed)
    sig.done.connect(finish)
    sig.failed.connect(finish)
    QThreadPool.globalInstance().start(job)
    return job
