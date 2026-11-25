"""The printer's camera feed, in the Printer settings tab below Advanced settings.

The Saturn 4 Ultra (16K) answers Cmd 386 with an RTSP URL (rtsp://<ip>:554/video). Its server
("ireader/media-server") sends H.264, 1280x720 at 30 fps, over RTP/UDP only: it answers a
request for TCP with a UDP transport. Its SDP gives the clock as 9000 Hz instead of 90000, so
timestamps are useless; frames are shown as they're decoded instead [RUNTIME, 02 §6].

Decoding uses PyAV (FFmpeg) on a background thread. Only the newest frame is handed to the GUI,
scaled to the view, so a slow GUI never builds a backlog.
"""
from __future__ import annotations

import threading
import time

import numpy as np
from PySide6.QtCore import QObject, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPalette
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QSizePolicy, QVBoxLayout, QWidget

try:
    import av
except ImportError:  # pragma: no cover - PyAV is a dependency, but keep the app running without it
    av = None

OPEN_TIMEOUT_S = 8.0          # connecting, and waiting for data once connected
RETRIES = 3                   # automatic reconnects after the feed drops
RETRY_DELAY_MS = 2000
ASPECT = 16 / 9
SHOWN_CHECK_MS = 500          # how often to check whether the picture is on screen
FIT_RECHECK_MS = 250          # re-apply the camera window's size once it has appeared


def rtsp_options(url: str) -> dict:
    o = {"fflags": "nobuffer", "flags": "low_delay", "max_delay": "200000", "reorder_queue_size": "0",
         "probesize": "500000", "analyzeduration": "1000000"}
    if url.startswith("rtsp"):
        o["rtsp_transport"] = "udp"          # the 16K's server only does UDP
        o["timeout"] = str(int(OPEN_TIMEOUT_S * 1e6))
    return o


class FrameReader(QObject):
    """Decodes one stream on its own thread. frame(QImage) carries the newest frame (scaled to fit
    `target`), failed(str) a reason the feed ended, stopped() a clean stop."""
    frame = Signal(QImage)
    failed = Signal(str)
    stopped = Signal()

    def __init__(self, url: str, parent=None):
        super().__init__(parent)
        self.url = url
        self.target = QSize(1280, 720)     # set by the view (device pixels)
        self.frames = 0
        self.source = (0, 0)               # the stream's own resolution, once a frame is decoded
        self._stop = threading.Event()
        self._busy = threading.Event()     # a frame is on its way to the GUI
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="camera", daemon=True)
        self._thread.start()

    def stop(self, wait: bool = False) -> None:
        self._stop.set()
        if wait and self._thread and self._thread is not threading.current_thread():
            self._thread.join(OPEN_TIMEOUT_S + 2)

    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def taken(self) -> None:
        """The GUI has the last frame: the next one may be sent."""
        self._busy.clear()

    def _size(self, w: int, h: int) -> tuple[int, int]:
        t = self.target
        s = min(1.0, t.width() / max(w, 1), t.height() / max(h, 1))
        return max(2, int(w * s) & ~1), max(2, int(h * s) & ~1)

    def _run(self) -> None:
        if av is None:
            self.failed.emit("Camera view needs PyAV (the Python package “av”)")
            return
        container = None
        try:
            # an interrupt callback isn't exposed by PyAV: the read timeout bounds every wait
            container = av.open(self.url, options=rtsp_options(self.url),
                                timeout=(OPEN_TIMEOUT_S, OPEN_TIMEOUT_S))
            stream = container.streams.video[0]
            stream.thread_type = "AUTO"
            # a live stream arrives in real time; a file (tests, recordings) is paced to its rate
            pace = 0.0 if "://" in self.url and not self.url.startswith("file:") else \
                1.0 / float(stream.average_rate or 30)
            t0 = time.monotonic()
            for f in container.decode(stream):
                if self._stop.is_set():
                    break
                if pace:
                    wait = t0 + self.frames * pace - time.monotonic()
                    if wait > 0 and self._stop.wait(wait):
                        break
                self.frames += 1
                self.source = (f.width, f.height)
                if self._busy.is_set():
                    continue                        # the GUI hasn't drawn the last one: drop this
                w, h = self._size(f.width, f.height)
                rgb = np.ascontiguousarray(f.reformat(width=w, height=h, format="rgb24").to_ndarray())
                img = QImage(rgb.data, w, h, 3 * w, QImage.Format_RGB888).copy()
                self._busy.set()
                self.frame.emit(img)
            else:
                if not self._stop.is_set():
                    self.failed.emit("The camera feed ended")
                    return
        except Exception as e:  # noqa: BLE001 (FFmpeg errors are many and all mean "no feed")
            if not self._stop.is_set():
                self.failed.emit(_reason(e))
                return
        finally:
            if container is not None:
                try:
                    container.close()
                except Exception:  # noqa: BLE001
                    pass
        self.stopped.emit()


def _reason(e: Exception) -> str:
    s = str(e) or type(e).__name__
    if "timed out" in s.lower() or isinstance(e, TimeoutError):
        return "No picture from the camera (timed out)"
    return "Camera feed error: " + s.split("\n")[0][:160]


class CameraView(QWidget):
    """The latest frame, letterboxed at 16:9; a message when there's no picture."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.image: QImage | None = None
        self.message = "Camera off"
        sp = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        sp.setHeightForWidth(True)
        self.setSizePolicy(sp)
        self.setMinimumSize(160, 90)

    def hasHeightForWidth(self) -> bool:
        return True

    def heightForWidth(self, w: int) -> int:
        return int(round(w / ASPECT))

    def sizeHint(self) -> QSize:
        return QSize(320, 180)

    def device_size(self) -> QSize:
        r = self.devicePixelRatioF()
        return QSize(max(2, int(self.width() * r)), max(2, int(self.height() * r)))

    def set_image(self, img: QImage | None) -> None:
        self.image = img
        self.update()

    def set_message(self, text: str) -> None:
        self.message = text
        self.update()

    def paintEvent(self, _e) -> None:
        p = QPainter(self)
        p.fillRect(self.rect(), QColor(16, 16, 18))
        if self.image is not None and not self.image.isNull():
            iw, ih = self.image.width(), self.image.height()
            s = min(self.width() / iw, self.height() / ih)
            w, h = iw * s, ih * s
            p.setRenderHint(QPainter.SmoothPixmapTransform)
            p.drawImage(QRectF((self.width() - w) / 2, (self.height() - h) / 2, w, h), self.image)
        if self.message:
            p.setPen(QColor(220, 220, 220) if self.image is None else QColor(255, 255, 255))
            r = self.rect().adjusted(8, 8, -8, -8)
            if self.image is not None:          # a caption over the picture
                fm = p.fontMetrics()
                band = QRectF(0, self.height() - fm.height() - 8, self.width(), fm.height() + 8)
                p.fillRect(band, QColor(0, 0, 0, 150))
                p.drawText(band, Qt.AlignCenter, self.message)
            else:
                p.drawText(r, Qt.AlignCenter | Qt.TextWordWrap, self.message)
        p.end()


class CameraPanel(QWidget):
    """"Camera" fold-out: the feed of the connected printer's camera. Opening the fold starts the
    feed; closing it (or disconnecting) stops it and frees the printer's viewer slot. While the tab
    is hidden decoding pauses, and it picks up again when the tab is shown.

    Pop out moves the picture into its own window (like View → Logs) that stays live whatever
    tab is in front; closing that window brings the picture back here."""
    log = Signal(str)

    def __init__(self, lan, settings=None, parent=None):
        super().__init__(parent)
        from .panels import disclosure_button
        self.lan = lan
        self.settings = settings
        self.window_view = CameraView()          # the picture when popped out
        self.window_view.setMinimumSize(320, 180)
        self.popout: QWidget | None = None       # the camera's own window (made on first use)
        self.reader: FrameReader | None = None
        self.stream = None                       # elegoo_lan.client.VideoStream while on
        self.wanted = False                      # the user wants the feed on
        self._session = None                     # the session the stream was switched on with
        self._retries = 0
        self._requesting = False
        self.stream_size = QSize()               # the feed's resolution (pixels), once known
        self._fit_pending = False                # size the popped-out window to it when known
        self._fit_size: QSize | None = None
        self.toggle = disclosure_button("Camera", "The connected printer's camera, live.")
        self.toggle.toggled.connect(self.set_open)
        self.body = QWidget()
        self.view = CameraView()
        self.status = QLabel()
        self.status.setWordWrap(True)
        pal = self.status.palette()
        pal.setColor(QPalette.WindowText, pal.color(QPalette.PlaceholderText))
        self.status.setPalette(pal)
        self.button = QPushButton("Start camera")
        self.button.clicked.connect(self._button)
        self.popout_btn = QPushButton("Pop out")
        self.popout_btn.setToolTip("Show the camera in its own window, which you can move and resize")
        self.popout_btn.clicked.connect(self._popout_button)
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        row.addWidget(self.status, 1)
        row.addWidget(self.popout_btn, 0, Qt.AlignTop)
        row.addWidget(self.button, 0, Qt.AlignTop)
        b = QVBoxLayout(self.body)
        b.setContentsMargins(0, 0, 0, 0)
        b.addWidget(self.view)
        b.addLayout(row)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.toggle)
        lay.addWidget(self.body)
        self._retry_timer = QTimer(self)
        self._retry_timer.setSingleShot(True)
        self._retry_timer.timeout.connect(self._open_stream)
        self._shown_timer = QTimer(self)          # pauses decoding behind other tabs (see _on_screen)
        self._shown_timer.setInterval(SHOWN_CHECK_MS)
        self._shown_timer.timeout.connect(self._check_shown)
        lan.connectionStateChanged.connect(self._connection_changed)
        lan.attributesUpdated.connect(lambda: self._refresh())
        self.set_open(False)

    # ------------------------------------------------------------------ state
    def session(self):
        s = self.lan.session
        return s if s is not None and s.connected else None

    def is_open(self) -> bool:
        return self.toggle.isChecked()

    def playing(self) -> bool:
        return self.reader is not None

    def popped_out(self) -> bool:
        return self.popout is not None and self.popout.isVisible()

    def active_view(self) -> CameraView:
        """Where the picture goes: the camera window while it's open, else the panel."""
        return self.window_view if self.popped_out() else self.view

    def _on_screen(self) -> bool:
        """Whether any of the panel shows. A dock tab behind another stays "visible" to Qt (the
        current tab is drawn over it), so this asks what's actually exposed."""
        return self.isVisible() and not self.visibleRegion().isEmpty()

    def _can_decode(self) -> bool:
        return self.popped_out() or self._on_screen()

    def _check_shown(self) -> None:
        """Pause decoding while nothing shows the picture; resume when something does."""
        if not self.wanted:
            self._shown_timer.stop()
            return
        if self._can_decode():
            if self.reader is None and self.stream is not None and not self._retry_timer.isActive():
                self._open_stream()
        elif self.reader is not None:
            self._retry_timer.stop()
            self._close_reader()

    def set_open(self, on: bool) -> None:
        self.body.setVisible(on)
        self.toggle.setArrowType(Qt.DownArrow if on else Qt.RightArrow)
        if self.toggle.isChecked() != on:
            self.toggle.setChecked(on)
        if on:
            self.start()
        elif not self.popped_out():          # folding the panel leaves a popped-out feed running
            self.stop()

    # --------------------------------------------------------------- pop out
    def _popout_button(self) -> None:
        if self.popped_out():
            self.pop_in()
        else:
            self.pop_out()

    def pop_out(self) -> None:
        """Move the picture into its own window (and start the feed if it's off)."""
        if self.popout is None:
            from .panels import ToolWindow
            from PySide6.QtCore import QSettings
            self.popout = ToolWindow("Camera", self.window_view, self.settings or QSettings(),
                                     "cameraWindow", self.window())
            if not (self.settings and self.settings.value("cameraWindowGeometry")):
                self.popout.resize(800, 450)
            self.popout.closed.connect(self._popout_closed)
        self.window_view.set_image(self.view.image)
        self.window_view.set_message(self.view.message)
        self._fit_pending = True
        self._fit_window(shown=False)             # sized before it shows: Wayland keeps the first size
        self.popout.popup()
        self._fit_pending = self._fit_pending or not self.stream_size.isEmpty()
        self._fit_window()
        self.view.set_image(None)
        self.view.set_message("Shown in its own window")
        self.popout_btn.setText("Pop in")
        if self.reader is not None:
            self.reader.target = self.window_view.device_size()
        if not self.wanted:
            self.start()
        elif self.reader is None and self.stream is not None:
            self._open_stream()

    def _fit_window(self, shown: bool = True) -> None:
        """Size the camera window so the picture shows at the stream's full resolution, one
        stream pixel per screen pixel (scaled down only if the screen is smaller). On Wayland the
        compositor may answer a resize made just after the window appears with its previous size,
        so the size is checked again shortly after."""
        w = self.popout
        if w is None or (shown and not w.isVisible()) or self.stream_size.isEmpty():
            return
        if shown:
            self._fit_pending = False
        # the main window's monitor: the camera window opens over it (GNOME centres a child window
        # on its parent), while on Wayland a new window can't tell which monitor it will land on
        main = self.window()
        scr = main.screen() if main is not None and main is not w else w.screen()
        dpr = scr.devicePixelRatio() if scr is not None else (w.devicePixelRatioF() or 1.0)
        size = QSize(round(self.stream_size.width() / dpr), round(self.stream_size.height() / dpr))
        if scr is not None:
            avail = scr.availableGeometry()
            frame = (w.frameGeometry().size() - w.geometry().size()) if shown else QSize(0, 0)
            room = QSize(avail.width() - frame.width(), avail.height() - frame.height())
            if size.width() > room.width() or size.height() > room.height():
                size = size.scaled(room, Qt.KeepAspectRatio)
        w.resize(size)
        self._fit_size = size
        if not shown:
            return
        QTimer.singleShot(FIT_RECHECK_MS, self._refit)
        if scr is not None:                       # keep it on screen
            g, avail = w.frameGeometry(), scr.availableGeometry()
            x = min(max(g.x(), avail.left()), max(avail.left(), avail.right() - g.width() + 1))
            y = min(max(g.y(), avail.top()), max(avail.top(), avail.bottom() - g.height() + 1))
            if (x, y) != (g.x(), g.y()):
                w.move(x, y)

    def _refit(self) -> None:
        w = self.popout
        if w is not None and w.isVisible() and self._fit_size is not None and w.size() != self._fit_size:
            w.resize(self._fit_size)

    def pop_in(self) -> None:
        if self.popout is not None and self.popout.isVisible():
            self.popout.close()                   # → _popout_closed
        else:
            self._popout_closed()

    def _popout_closed(self) -> None:
        self.popout_btn.setText("Pop out")
        self.view.set_image(self.window_view.image)
        self.view.set_message(self.window_view.message if self.wanted else "Camera off")
        self.window_view.set_image(None)
        if not self.is_open():
            self.stop()                           # nowhere left to show it
        elif not self.isVisible():
            self._retry_timer.stop()
            self._close_reader()                  # resumes when the tab is shown
        elif self.reader is not None:
            self.reader.target = self.view.device_size()
        self._refresh()

    def _button(self) -> None:
        if self.wanted:
            self.stop()
        else:
            self.start()

    def _refresh(self, message: str | None = None) -> None:
        sess = self.session()
        cam = sess.has_camera() if sess else None
        self.button.setText("Stop camera" if self.wanted else "Start camera")
        self.button.setEnabled(self.wanted or (sess is not None and cam is not False))
        if message is not None:
            self.status.setText(message)
        elif not self.wanted:
            self.status.setText("Connect to a printer to see its camera." if sess is None else
                                "This printer has no camera." if cam is False else "")
        self.popout_btn.setEnabled(self.popped_out() or self.wanted or (sess is not None and cam is not False))
        if not self.wanted:
            v = self.active_view()
            v.set_image(None)
            v.set_message("Camera off" if sess is not None else "Not connected")

    # ------------------------------------------------------------ start/stop
    def start(self) -> None:
        sess = self.session()
        if sess is None or sess.has_camera() is False:
            self.wanted = False
            self._refresh()
            return
        if self.wanted:
            return
        self.wanted, self._retries = True, 0
        self._shown_timer.start()
        self.active_view().set_image(None)
        self.active_view().set_message("Starting the camera…")
        self._refresh("")
        self._request(sess)

    def _request(self, sess) -> None:
        from . import workers
        self._requesting = True
        self._session = sess

        def done(stream):
            self._requesting = False
            if not self.wanted or self.session() is not sess:
                sess.stop_video()
                return
            self.stream = stream
            if stream.note:
                self.log.emit(f"Camera: {stream.note}; trying the stream anyway")
            self._open_stream()

        def failed(msg):
            self._requesting = False
            if self.wanted:
                self.wanted = False
                text = msg.strip().splitlines()[-1] if msg.strip() else "Camera request failed"
                self.log.emit(f"Camera: {text}")
                self._refresh(text)
        workers.submit(sess.start_video, on_done=done, on_failed=failed)

    def _open_stream(self) -> None:
        if not self.wanted or self.stream is None or not self._can_decode():
            return
        self._close_reader()
        r = FrameReader(self.stream.url, self)
        r.target = self.active_view().device_size()
        r.frame.connect(self._frame)
        r.failed.connect(lambda msg, r=r: self._failed(r, msg))
        self.reader = r
        self.active_view().set_message("Connecting to the camera…")
        r.start()

    def _frame(self, img: QImage) -> None:
        r = self.reader
        if r is None or not self.wanted:
            return
        self._retries = 0
        if r.source[0] and QSize(*r.source) != self.stream_size:
            self.stream_size = QSize(*r.source)
            self._fit_pending = self._fit_pending or self.popped_out()
        if self._fit_pending:
            self._fit_window()
        v = self.active_view()
        v.set_image(img)
        if v.message:
            v.set_message("")
            self.status.setText(f"Live · {self.stream.url}")
        r.target = v.device_size()
        r.taken()

    def _failed(self, r: FrameReader, msg: str) -> None:
        if r is not self.reader:
            return
        self.reader = None
        if not self.wanted:
            return
        if self._retries < RETRIES and self.session() is not None:
            self._retries += 1
            self.active_view().set_message(f"{msg}. Retrying ({self._retries}/{RETRIES})…")
            self._retry_timer.start(RETRY_DELAY_MS)
            return
        self.log.emit(f"Camera: {msg}")
        self._stop_stream()
        self.wanted = False
        self._refresh(msg)
        self.active_view().set_message(msg)

    def _close_reader(self, wait: bool = False) -> None:
        r, self.reader = self.reader, None
        if r is not None:
            r.frame.disconnect()
            r.stop(wait=wait)

    def _stop_stream(self) -> None:
        sess, self._session = self._session, None
        if self.stream is not None and sess is not None and sess.connected:
            sess.stop_video()
        self.stream = None

    def stop(self, wait: bool = False) -> None:
        """Stop the feed and switch the printer's stream off."""
        was = self.wanted or self.reader is not None
        self.wanted = False
        self._retry_timer.stop()
        self._close_reader(wait=wait)
        if was:
            self._stop_stream()
        self._refresh()

    def shutdown(self) -> None:
        self.stop(wait=True)
        if self.popout is not None and self.popout.isVisible():
            self.popout.close()

    # ---------------------------------------------------------------- events
    def _connection_changed(self, connected: bool) -> None:
        if not connected and (self.wanted or self.reader):
            self._close_reader()
            self.stream, self._session = None, None
            self.wanted = False
        if connected and self.is_open() and not self.wanted:
            self.start()
        else:
            self._refresh()

    def hideEvent(self, e) -> None:
        # another tab is in front (or the window is minimised): stop decoding, keep the stream,
        # unless the picture is in its own window
        if not self.popped_out():
            self._retry_timer.stop()
            self._close_reader()
        super().hideEvent(e)

    def showEvent(self, e) -> None:
        super().showEvent(e)
        if self.wanted and self.reader is None and self.stream is not None:
            self._open_stream()
