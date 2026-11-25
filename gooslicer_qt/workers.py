"""Background jobs on the global QThreadPool with Qt signals back to the GUI thread, and
CPU-heavy jobs in separate processes (submit_process)."""
from __future__ import annotations

import os
import threading
import traceback

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

_live: set = set()  # keep signal objects alive until delivered


class _Signals(QObject):
    done = Signal(object)
    failed = Signal(str)
    progress = Signal(str)


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


def submit(fn, *args, on_done=None, on_failed=None, on_progress=None, **kw) -> Job:
    """Run fn(*args, **kw) on the thread pool. With on_progress, fn is also given
    progress=callable(text), delivered to on_progress on the GUI thread."""
    job = Job(fn, *args, **kw)
    sig = job.signals
    if on_progress:
        sig.progress.connect(on_progress)
        job.kw["progress"] = sig.progress.emit
    _live.add(sig)

    def finish(*_):
        _live.discard(sig)
        from .memory import trim_memory_soon
        trim_memory_soon()                   # jobs (loading, repair, hollowing…) leave freed memory
    if on_done:
        sig.done.connect(on_done)
    if on_failed:
        sig.failed.connect(on_failed)
    sig.done.connect(finish)
    sig.failed.connect(finish)
    QThreadPool.globalInstance().start(job)
    return job


# ------------------------------------------------------------------ processes
# A worker *thread* running long Python or C code holds the GIL, and the GUI thread waits for it:
# the 3D view stalls for up to a second at a time. Support building, the island and trapped-resin
# checks and hollowing (gooslicer.jobs) run in child processes instead; a thread of the pool only
# waits for the result. Children fork from a forkserver that
# has the support code imported already (milliseconds per job instead of ~0.4 s), and each child
# ends after one job, so its memory goes back to the system.
PROCESS_PRELOAD = ["__main__", "gooslicer.supports", "gooslicer.jobs"]   # __main__: children then skip re-importing it
_pool = None
_pool_lock = threading.Lock()


def process_workers() -> int:
    return max(1, min(4, (os.cpu_count() or 2) // 2))


def _process_pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            import multiprocessing as mp
            from concurrent.futures import ProcessPoolExecutor
            ctx = mp.get_context("forkserver")
            ctx.set_forkserver_preload(PROCESS_PRELOAD)
            _pool = ProcessPoolExecutor(max_workers=process_workers(), mp_context=ctx, max_tasks_per_child=1)
        return _pool


def run_in_process(fn, *args):
    """fn(*args) in a child process (fn and its arguments must pickle); in this process if
    children can't be started at all."""
    global _pool
    from concurrent.futures.process import BrokenProcessPool
    try:
        fut = _process_pool().submit(fn, *args)
    except (OSError, RuntimeError, BrokenProcessPool):
        with _pool_lock:
            _pool = None
        return fn(*args)
    try:
        return fut.result()
    except BrokenProcessPool:
        with _pool_lock:
            _pool = None                    # a child was killed (out of memory?): start afresh
        raise RuntimeError("the worker process stopped unexpectedly (out of memory?)") from None


def submit_process(fn, *args, then=None, on_done=None, on_failed=None) -> Job:
    """Like submit(), but fn(*args) runs in a child process. then(result), if given, runs
    afterwards on the waiting worker thread (for work on the result that needn't block the GUI)."""
    def work():
        res = run_in_process(fn, *args)
        return then(res) if then else res
    return submit(work, on_done=on_done, on_failed=on_failed)


def shutdown_processes() -> None:
    """Stop the child processes (at exit), without waiting for running jobs."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    if pool is not None:
        pool.shutdown(wait=False, cancel_futures=True)
        kill = getattr(pool, "kill_workers", None)       # Python 3.14+
        if kill:
            try:
                kill()
            except Exception:  # noqa: BLE001 (already gone)
                pass
