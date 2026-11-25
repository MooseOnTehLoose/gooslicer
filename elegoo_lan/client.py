"""Persistent SDCP v3 printer session (Qt-free; thread-based).

Message shapes are from runtime captures of a Saturn 4 Ultra 16K (02-lan-sdcp-spec.md §6) and
the WebSocket envelope from EleNetManager.dll (§2a). Status/ack code meanings are not named in the
binary; they follow the published SDCP v3 documentation [PUBLIC-SPEC] and unknown codes are shown
numerically rather than guessed.
"""
from __future__ import annotations

import os
import select
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass, field
from typing import Callable

from . import protocol, upload as uploadmod
from .protocol import Cmd, Printer
from .ws import SdcpWebSocket, WebSocketError

# Meanings from the published SDCP v3 docs [PUBLIC-SPEC]. Runtime-confirmed on the 16K
# (02-lan-sdcp-spec.md §6): CurrentStatus 0/1/2 and PrintInfo.Status 0–8 [RUNTIME].
MACHINE_STATUS = {0: "Idle", 1: "Printing", 2: "Transferring file", 3: "Exposure test",
                  4: "Device self-test"}
PRINT_STATUS = {0: "Idle", 1: "Homing", 2: "Dropping", 3: "Exposing", 4: "Lifting",
                5: "Pausing", 6: "Paused", 7: "Stopping", 8: "Stopped", 9: "Complete",
                10: "Checking file"}
ACK = {0: "OK", 1: "Printer busy", 2: "File not found", 3: "MD5 check failed",
       4: "File read error", 5: "Resolution mismatch", 6: "Unknown file format",
       7: "Machine model mismatch"}                                             # [PUBLIC-SPEC]
# Cmd 386 (video stream on/off) acks [PUBLIC-SPEC]. Seen on the 16K [RUNTIME]: Ack 1 while the
# printer counted 2 of 2 viewers, and the stream still played (02-lan-sdcp-spec.md §6).
VIDEO_ACK = {0: "OK", 1: "The printer's camera viewer limit is reached", 2: "The printer has no camera",
             3: "Camera error"}
RTSP_PORT = 554


def default_video_url(host: str) -> str:
    """Where the 16K serves its camera when it doesn't say [RUNTIME: its VideoUrl reply]."""
    return f"rtsp://{host}:{RTSP_PORT}/video"


@dataclass
class VideoStream:
    url: str
    ack: int = 0            # the printer's answer to switching the stream on (VIDEO_ACK)

    @property
    def note(self) -> str:
        return "" if not self.ack else describe(self.ack, VIDEO_ACK)


PRINT_ERROR = {0: "", 1: "File MD5 check failed", 2: "File read failed",
               3: "Resolution mismatch", 4: "Format mismatch", 5: "Machine model mismatch"}


def describe(code: int | None, table: dict[int, str]) -> str:
    if code is None:
        return "—"
    return table.get(code, f"status {code}")


@dataclass
class PrinterState:
    """Latest pushed status/attributes, decoded for display."""
    status: dict = field(default_factory=dict)
    attributes: dict = field(default_factory=dict)
    updated: float = 0.0

    @property
    def machine_status(self) -> list[int]:
        cs = self.status.get("CurrentStatus", [])
        return list(cs) if isinstance(cs, list) else [cs]

    @property
    def print_info(self) -> dict:
        return self.status.get("PrintInfo") or {}

    @property
    def print_status(self) -> int | None:
        s = self.print_info.get("Status")
        return int(s) if s is not None else None

    @property
    def is_printing(self) -> bool:
        return 1 in self.machine_status

    @property
    def is_paused(self) -> bool:
        return self.print_status in (5, 6)

    @property
    def is_busy(self) -> bool:
        return any(s != 0 for s in self.machine_status)

    @property
    def progress(self) -> float:
        pi = self.print_info
        total = pi.get("TotalLayer") or 0
        return min(1.0, (pi.get("CurrentLayer") or 0) / total) if total else 0.0

    @property
    def remaining_s(self) -> float | None:
        pi = self.print_info
        cur, tot = pi.get("CurrentTicks"), pi.get("TotalTicks")
        if not tot:
            return None
        return max(0.0, (tot - (cur or 0)) / 1000.0)    # ticks are ms [PUBLIC-SPEC]

    def summary(self) -> str:
        ms = ", ".join(describe(s, MACHINE_STATUS) for s in self.machine_status) or "—"
        if self.is_printing or self.print_status not in (None, 0):
            return f"{ms} — {describe(self.print_status, PRINT_STATUS)}"
        return ms


class SessionError(RuntimeError):
    pass


class PrinterSession:
    """One WebSocket connection to one printer, with a reader thread.

    Callbacks run on the reader thread; GUI code must marshal them (Qt queued signals do).
    """

    def __init__(self, printer: Printer, *, port: int = protocol.WS_PORT,
                 http_port: int | None = None, refresh_s: float = 5.0,
                 connect: Callable[..., SdcpWebSocket] = SdcpWebSocket):
        self.printer = printer
        self.host = printer.ip or printer.source_ip
        self.port, self.http_port = port, http_port or port
        self.refresh_s = refresh_s
        self.state = PrinterState()
        self.on_status: Callable[[PrinterState], None] | None = None
        self.on_attributes: Callable[[PrinterState], None] | None = None
        self.on_connection: Callable[[bool, str], None] | None = None
        self._connect = connect
        self._ws: SdcpWebSocket | None = None
        self._pending: dict[str, Future] = {}
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.connected = False

    # ------------------------------------------------------------ lifecycle
    def start(self) -> None:
        if protocol.compatibility_warning(self.printer):
            raise protocol.IncompatiblePrinterError(protocol.compatibility_warning(self.printer))
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name=f"sdcp-{self.host}", daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._stop.set()
        ws = self._ws
        if ws:
            try:
                ws.close()
            except OSError:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=3)
        self._fail_pending("session closed")

    def _set_connected(self, ok: bool, msg: str = "") -> None:
        self.connected = ok
        if self.on_connection:
            self.on_connection(ok, msg)

    def _fail_pending(self, msg: str) -> None:
        with self._lock:
            pend, self._pending = self._pending, {}
        for f in pend.values():
            if not f.done():
                f.set_exception(SessionError(msg))

    def _run(self) -> None:
        backoff = 1.0
        while not self._stop.is_set():
            try:
                ws = self._connect(self.host, self.printer.mainboard_id, self.printer.id,
                                   port=self.port, timeout=10.0)
            except (OSError, WebSocketError) as e:
                self._set_connected(False, f"cannot connect: {e}")
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)
                continue
            backoff = 1.0
            self._ws = ws
            ws.sock.settimeout(15.0)        # bound for reading one frame once data is available
            self._set_connected(True, "connected")
            try:
                self._send(Cmd.ATTRIBUTES)
                self._send(Cmd.STATUS)
                last = time.monotonic()
                while not self._stop.is_set():
                    wait = max(0.05, min(0.5, self.refresh_s - (time.monotonic() - last)))
                    readable = getattr(ws, "buffered", 0) > 0 or \
                        select.select([ws.sock], [], [], wait)[0]
                    if readable:            # only start a frame read when bytes are waiting
                        msg = ws.recv_message()
                        if msg is None:
                            raise WebSocketError("closed by printer")
                        self._dispatch(msg)
                    if time.monotonic() - last >= self.refresh_s:
                        self._send(Cmd.STATUS)
                        last = time.monotonic()
            except (OSError, WebSocketError) as e:
                if not self._stop.is_set():
                    self._set_connected(False, f"connection lost: {e}")
                self._fail_pending("connection lost")
            finally:
                self._ws = None
                try:
                    ws.sock.close()
                except OSError:
                    pass
            if self._stop.wait(1.0):
                return
        self._set_connected(False, "disconnected")

    def _dispatch(self, msg) -> None:
        if not isinstance(msg, dict):
            return
        kind = protocol.classify_topic(msg.get("Topic", ""))
        if kind == "status":
            st = msg.get("Status") or (msg.get("Data") or {}).get("Status") or {}
            self.state.status = st
            self.state.updated = time.time()
            if self.on_status:
                self.on_status(self.state)
        elif kind == "attributes":
            at = msg.get("Attributes") or (msg.get("Data") or {}).get("Attributes") or {}
            self.state.attributes = at
            if self.on_attributes:
                self.on_attributes(self.state)
        elif kind == "response":
            d = msg.get("Data") or {}
            with self._lock:
                fut = self._pending.pop(d.get("RequestID", ""), None)
            if fut and not fut.done():
                fut.set_result(d.get("Data") or {})

    # ------------------------------------------------------------- requests
    def _send(self, cmd: int, args: dict | None = None) -> Future:
        ws = self._ws
        fut: Future = Future()
        if ws is None:
            fut.set_exception(SessionError("not connected"))
            return fut
        msg = protocol.build_ws_request(cmd, self.printer.mainboard_id, self.printer.id, args)
        with self._lock:
            self._pending[msg["Data"]["RequestID"]] = fut
        try:
            ws.send_text(protocol.encode(msg))
        except OSError as e:
            with self._lock:
                self._pending.pop(msg["Data"]["RequestID"], None)
            fut.set_exception(SessionError(str(e)))
        return fut

    def request(self, cmd: int, args: dict | None = None, timeout: float = 10.0) -> dict:
        """Send and wait for the matching sdcp/response; raise SessionError on a non-zero Ack."""
        try:
            data = self._send(cmd, args).result(timeout)
        except TimeoutError as e:
            raise SessionError(f"no response to command {cmd}") from e
        ack = data.get("Ack", 0)
        if ack:
            raise SessionError(describe(ack, ACK))
        return data

    def refresh(self) -> None:
        self._send(Cmd.STATUS)

    def has_camera(self) -> bool | None:
        """From the attributes: True/False, or None before they arrive."""
        a = self.state.attributes
        if not a:
            return None
        caps = a.get("Capabilities")
        if isinstance(caps, list) and "VIDEO_STREAM" not in caps:
            return False
        return a.get("CameraStatus", 1) != 0

    def start_video(self, timeout: float = 10.0) -> VideoStream:
        """Switch the camera stream on (Cmd 386) and return its URL (RTSP, H.264 over UDP on the
        16K). A full viewer count (Ack 1) is passed back rather than raised: the 16K kept
        serving the stream regardless [RUNTIME]. No camera (Ack 2) raises SessionError."""
        try:
            data = self._send(Cmd.VIDEO_STREAM, {"Enable": 1}).result(timeout)
        except TimeoutError as e:
            raise SessionError("no response to the camera request") from e
        ack = int(data.get("Ack", 0) or 0)
        if ack == 2:
            raise SessionError(describe(ack, VIDEO_ACK))
        url = str(data.get("VideoUrl") or "") or default_video_url(self.host)
        if "://" not in url:
            url = "rtsp://" + url
        return VideoStream(url, ack)

    def stop_video(self) -> None:
        """Switch the camera stream off; best effort (never raises)."""
        try:
            if self._ws is not None:
                self._send(Cmd.VIDEO_STREAM, {"Enable": 0})
        except Exception:  # noqa: BLE001
            pass

    def pause(self) -> dict:
        return self.request(Cmd.PAUSE)

    def resume(self) -> dict:
        return self.request(Cmd.RESUME)

    def stop_print(self) -> dict:
        return self.request(Cmd.STOP)

    def start_print(self, filename: str, start_layer: int = 0) -> dict:
        return self.request(Cmd.START_PRINT, {"Filename": filename, "StartLayer": start_layer})

    def list_files(self, url: str = "/local") -> list[dict]:
        data = self.request(Cmd.FILE_LIST, {"Url": url})
        files = data.get("FileList") or []
        return [f for f in files if isinstance(f, dict)]

    def delete_files(self, files: list[str], folders: list[str] | None = None) -> dict:
        return self.request(Cmd.DELETE_FILES, {"FileList": files, "FolderList": folders or []})

    def upload(self, path: str, progress: Callable[[int, int], None] | None = None,
               cancelled: Callable[[], bool] | None = None) -> str:
        """Upload over HTTP; on cancel, tell the printer to drop the partial file (Cmd 255)."""
        uid_box: dict = {}
        try:
            return uploadmod.upload(self.host, path, port=self.http_port, progress=progress,
                                    cancelled=cancelled, uuid_out=uid_box)
        except uploadmod.UploadCancelled:
            if uid_box.get("uuid") and self.connected:
                try:
                    self.request(Cmd.CANCEL_TRANSFER, {"Uuid": uid_box["uuid"],
                                                       "FileName": os.path.basename(path)}, timeout=5)
                except SessionError:
                    pass
            raise


def check_file_for_printer(header, state: PrinterState, printer: Printer) -> list[str]:
    """Problems that would make the printer reject (or misprint) a .goo file."""
    problems = []
    res = state.attributes.get("Resolution") or (
        f"{printer.resolution[0]}x{printer.resolution[1]}" if printer.resolution else "")
    if res and res != f"{header.resolution_x}x{header.resolution_y}":
        problems.append(f"file is {header.resolution_x}×{header.resolution_y} px but the printer is "
                        f"{res.replace('x', '×')} px")
    model = (state.attributes.get("MachineName") or printer.machine_name or "").strip().lower()
    hm = (header.machine_type or header.machine_name or "").strip().lower()
    if model and hm and not hm.endswith(model) and not model.endswith(hm.replace("elegoo ", "")):
        problems.append(f"file was sliced for “{header.machine_type}”, printer is "
                        f"“{state.attributes.get('MachineName') or printer.machine_name}”")
    types = state.attributes.get("SupportFileType")
    if isinstance(types, list) and types and "GOO" not in [str(t).upper() for t in types]:
        problems.append("printer does not accept .goo files")
    return problems
