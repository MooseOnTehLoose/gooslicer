"""Printer control panel: discovery, live status, print control, files, upload (SDCP v3 LAN)."""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from PySide6.QtCore import QObject, Qt, Signal
from PySide6.QtWidgets import (QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout,
                               QInputDialog, QLabel, QListWidget, QListWidgetItem, QMessageBox,
                               QProgressBar, QPushButton, QVBoxLayout, QWidget)

from elegoo_goo import goo
from elegoo_lan import client as C, discovery, protocol
from elegoo_lan.protocol import Printer

from . import workers

APP = "GooSlicer"


def _first_line(msg: str) -> str:
    return (msg or "").strip().splitlines()[0] if msg else "unknown error"


def _fmt_secs(s: float | None) -> str:
    if s is None:
        return "—"
    s = int(s)
    h, rem = divmod(s, 3600)
    return f"{h}h {rem // 60:02d}m" if h else f"{rem // 60}m {rem % 60:02d}s"


def read_goo_header(path: str) -> goo.Header:
    with open(path, "rb") as f:
        return goo.Header.unpack(f.read(goo.HEADER_STRUCT.size))


class SessionBridge(QObject):
    """Moves PrinterSession callbacks (reader thread) onto the GUI thread."""
    statusChanged = Signal(object)
    attributesChanged = Signal(object)
    connectionChanged = Signal(bool, str)

    def __init__(self, session: C.PrinterSession):
        super().__init__()
        self.session = session
        session.on_status = self.statusChanged.emit
        session.on_attributes = self.attributesChanged.emit
        session.on_connection = self.connectionChanged.emit


class PrinterControlPanel(QWidget):
    log = Signal(str)
    uploadProgress = Signal(int, int)
    uploadFinished = Signal(str, bool, str)     # filename, ok, message

    def __init__(self, settings, parent=None, ports: dict | None = None):
        super().__init__(parent)
        self.settings = settings
        self.ports = ports or {}                 # test hook: {"discovery", "ws", "http", "targets"}
        self.known: list[Printer] = self._load_known()
        self.bridge: SessionBridge | None = None
        self._upload_cancel = threading.Event()
        self._uploading = False
        self._we_paused = False

        # --- printer selection
        self.combo = QComboBox()
        self.combo.setMinimumContentsLength(22)
        self.find_btn = QPushButton("Find printers")
        self.add_btn = QPushButton("Add by IP…")
        self.connect_btn = QPushButton("Connect")
        self.conn_label = QLabel("Not connected")
        self.warn_label = QLabel()
        self.warn_label.setWordWrap(True)
        self.warn_label.setStyleSheet("color:#c0392b;")
        self.warn_label.hide()
        top = QHBoxLayout()
        top.addWidget(self.combo, 1)
        top.addWidget(self.find_btn)
        top.addWidget(self.add_btn)
        top.addWidget(self.connect_btn)

        # --- status
        self.state_label = QLabel("—")
        self.file_label = QLabel("—")
        self.layer_label = QLabel("—")
        self.time_label = QLabel("—")
        self.temp_label = QLabel("—")
        self.progress = QProgressBar()
        self.progress.setRange(0, 1000)
        self.progress.setTextVisible(True)
        self.progress.setFormat("%p%")
        status_box = QGroupBox("Status")
        sf = QFormLayout(status_box)
        sf.addRow("State", self.state_label)
        sf.addRow("File", self.file_label)
        sf.addRow("Layer", self.layer_label)
        sf.addRow("Progress", self.progress)
        sf.addRow("Remaining", self.time_label)
        sf.addRow("Temperatures", self.temp_label)
        self.pause_btn = QPushButton("Pause")
        self.stop_btn = QPushButton("Stop")
        ctl = QHBoxLayout()
        ctl.addWidget(self.pause_btn)
        ctl.addWidget(self.stop_btn)
        ctl.addStretch(1)
        sf.addRow(ctl)

        # --- files
        files_box = QGroupBox("Files on printer")
        self.files = QListWidget()
        self.refresh_files_btn = QPushButton("Refresh")
        self.print_file_btn = QPushButton("Print")
        self.delete_file_btn = QPushButton("Delete")
        self.send_btn = QPushButton("Send .goo…")
        self.upload_bar = QProgressBar()
        self.upload_bar.hide()
        self.cancel_upload_btn = QPushButton("Cancel upload")
        self.cancel_upload_btn.hide()
        fb = QHBoxLayout()
        for b in (self.refresh_files_btn, self.print_file_btn, self.delete_file_btn, self.send_btn):
            fb.addWidget(b)
        fb.addStretch(1)
        ub = QHBoxLayout()
        ub.addWidget(self.upload_bar, 1)
        ub.addWidget(self.cancel_upload_btn)
        fl = QVBoxLayout(files_box)
        fl.addWidget(self.files, 1)
        fl.addLayout(fb)
        fl.addLayout(ub)

        body = QHBoxLayout()
        body.addWidget(status_box, 1)
        body.addWidget(files_box, 1)
        lay = QVBoxLayout(self)
        lay.addLayout(top)
        lay.addWidget(self.conn_label)
        lay.addWidget(self.warn_label)
        lay.addLayout(body, 1)

        self.find_btn.clicked.connect(self.find_printers)
        self.add_btn.clicked.connect(self.add_by_ip_dialog)
        self.connect_btn.clicked.connect(self.toggle_connection)
        self.combo.currentIndexChanged.connect(self._selection_changed)
        self.pause_btn.clicked.connect(self.pause_resume)
        self.stop_btn.clicked.connect(self.stop_print)
        self.refresh_files_btn.clicked.connect(self.refresh_files)
        self.print_file_btn.clicked.connect(self.print_selected)
        self.delete_file_btn.clicked.connect(self.delete_selected)
        self.send_btn.clicked.connect(self.send_dialog)
        self.cancel_upload_btn.clicked.connect(self._upload_cancel.set)
        self.files.itemSelectionChanged.connect(self._update_buttons)
        self.uploadProgress.connect(self._upload_progress)
        self.uploadFinished.connect(self._upload_finished)
        self._fill_combo()
        self._update_buttons()

    # ----------------------------------------------------------- persistence
    def _load_known(self) -> list[Printer]:
        try:
            rows = json.loads(self.settings.value("lan/printers", "[]") or "[]")
        except ValueError:
            rows = []
        out = []
        for r in rows:
            if isinstance(r, dict):
                out.append(Printer(**{k: v for k, v in r.items() if k in Printer.__dataclass_fields__
                                      and k not in ("status", "resolution")}))
        return out

    def _save_known(self):
        keep = ("id", "name", "machine_name", "protocol_version", "firmware_version", "ip",
                "mainboard_id")
        self.settings.setValue("lan/printers", json.dumps(
            [{k: getattr(p, k) for k in keep} for p in self.known]))

    def _fill_combo(self, select: str | None = None):
        cur = select or (self.current().mainboard_id if self.current() else
                         self.settings.value("lan/last", ""))
        self.combo.blockSignals(True)
        self.combo.clear()
        for p in self.known:
            label = f"{p.name or 'Printer'} — {p.machine_name or '?'} ({p.ip})"
            if protocol.compatibility_warning(p):
                label += "  [not compatible]"
            self.combo.addItem(label, p.mainboard_id or p.ip)
        idx = max(0, self.combo.findData(cur)) if cur else 0
        self.combo.setCurrentIndex(idx if self.known else -1)
        self.combo.blockSignals(False)
        self._selection_changed()

    def current(self) -> Printer | None:
        i = self.combo.currentIndex()
        return self.known[i] if 0 <= i < len(self.known) else None

    def _remember(self, found: list[Printer]):
        for p in found:
            key = p.mainboard_id or p.ip
            self.known = [k for k in self.known if (k.mainboard_id or k.ip) != key] + [p]
        self._save_known()
        self._fill_combo(select=(found[0].mainboard_id or found[0].ip) if found else None)

    # ------------------------------------------------------------- discovery
    def find_printers(self):
        self.find_btn.setEnabled(False)
        self.conn_label.setText("Searching the local network…")
        port = self.ports.get("discovery", protocol.DISCOVERY_PORT)
        targets = self.ports.get("targets")
        workers.submit(self._discover, port, targets, on_done=self._found, on_failed=self._find_failed)

    @staticmethod
    def _discover(port, targets):
        import warnings
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            return discovery.discover(2.0, targets=targets, port=port)

    def _found(self, printers):
        self.find_btn.setEnabled(True)
        if not printers:
            self.conn_label.setText("No printers answered. Check the printer is on the same network, "
                                    "or add it by IP.")
            return
        self.log.emit(f"Found {len(printers)} printer(s): " +
                      ", ".join(f"{p.name} ({p.machine_name}, {p.ip})" for p in printers))
        for p in printers:
            if (w := protocol.compatibility_warning(p)):
                self.log.emit("WARNING: " + w)
        self._remember(printers)
        self.conn_label.setText(f"Found {len(printers)} printer(s)")

    def _find_failed(self, msg):
        self.find_btn.setEnabled(True)
        self.conn_label.setText("Search failed: " + _first_line(msg))

    def add_by_ip_dialog(self):
        ip, ok = QInputDialog.getText(self, "Add printer", "Printer IP address:")
        if ok and ip.strip():
            self.add_by_ip(ip.strip())

    def add_by_ip(self, ip: str):
        port = self.ports.get("discovery", protocol.DISCOVERY_PORT)
        self.conn_label.setText(f"Asking {ip}…")
        workers.submit(discovery.probe, ip, 2.0, port,
                       on_done=lambda p: self._found([p] if p else []) if p else
                       self.conn_label.setText(f"No reply from {ip}"),
                       on_failed=self._find_failed)

    # ------------------------------------------------------------ connection
    def _selection_changed(self, *_):
        p = self.current()
        w = protocol.compatibility_warning(p) if p else None
        self.warn_label.setText(w or "")
        self.warn_label.setVisible(bool(w))
        self._update_buttons()

    def toggle_connection(self):
        if self.bridge:
            self.disconnect_printer()
        else:
            self.connect_printer()

    def connect_printer(self):
        p = self.current()
        if p is None:
            return
        if (w := protocol.compatibility_warning(p)):
            QMessageBox.warning(self, APP, w)
            return
        self.disconnect_printer()
        sess = C.PrinterSession(p, port=self.ports.get("ws", protocol.WS_PORT),
                                http_port=self.ports.get("http"))
        self.bridge = SessionBridge(sess)
        self.bridge.statusChanged.connect(self._status)
        self.bridge.attributesChanged.connect(self._attributes)
        self.bridge.connectionChanged.connect(self._connection)
        self.settings.setValue("lan/last", p.mainboard_id or p.ip)
        self.conn_label.setText(f"Connecting to {p.name or p.ip}…")
        sess.start()
        self.connect_btn.setText("Disconnect")
        self._update_buttons()

    def disconnect_printer(self):
        if self.bridge:
            b, self.bridge = self.bridge, None
            b.session.close()
            b.deleteLater()
        self.connect_btn.setText("Connect")
        self.conn_label.setText("Not connected")
        for l in (self.state_label, self.file_label, self.layer_label, self.time_label, self.temp_label):
            l.setText("—")
        self.progress.setValue(0)
        self.progress.setFormat("%p%")
        self.files.clear()
        self._update_buttons()

    @property
    def session(self) -> C.PrinterSession | None:
        return self.bridge.session if self.bridge else None

    @property
    def state(self) -> C.PrinterState | None:
        return self.session.state if self.session else None

    def _connection(self, ok: bool, msg: str):
        p = self.current()
        self.conn_label.setText(f"Connected to {p.name if p else 'printer'}" if ok else msg.capitalize())
        self.log.emit(f"Printer {p.name if p else ''}: {msg}")
        if ok:
            self.refresh_files()
        self._update_buttons()

    def _attributes(self, st: C.PrinterState):
        a = st.attributes
        p = self.current()
        if p and a.get("MachineName") and not p.machine_name:
            p.machine_name = a["MachineName"]
        self._update_buttons()

    def _status(self, st: C.PrinterState):
        pi = st.print_info
        if not st.is_paused:
            self._we_paused = False
        new_task = pi.get("TaskId") != getattr(self, "_task", None)
        self._task = pi.get("TaskId")
        if new_task:
            self._we_paused = False
        if st.is_paused and not self._we_paused and not pi.get("CurrentLayer"):
            # printer paused itself before layer 1: its resin check found an empty vat and
            # asked for confirmation (seen in the ELEGOO app) [USER-REPORTED, 02 §6]
            self.state_label.setText("Paused by printer — check resin level, then Resume or Stop")
            self.state_label.setToolTip("The printer paused before the first layer without a request "
                                        "from GooSlicer — on the Saturn 4 Ultra 16K this is its “no resin "
                                        "detected” check. The printer does not report the reason over the "
                                        "network. Check the vat, then press Resume here (or answer on the "
                                        "printer / ELEGOO app), or Stop. Resuming here does not clear "
                                        "the prompt in the ELEGOO app; dismiss it there.")
        else:
            self.state_label.setText(st.summary())
            self.state_label.setToolTip("")
        self.file_label.setText(pi.get("Filename") or "—")
        tot = pi.get("TotalLayer") or 0
        self.layer_label.setText(f"{pi.get('CurrentLayer', 0)} / {tot}" if tot else "—")
        self.progress.setValue(int(st.progress * 1000))
        self.progress.setFormat(f"{st.progress * 100:.1f}%")
        self.time_label.setText(_fmt_secs(st.remaining_s) if st.is_printing else "—")
        s = st.status
        temps = []
        if s.get("TempOfUVLED") is not None:
            temps.append(f"UV LED {s['TempOfUVLED']:.1f} °C")
        if s.get("TempOfTank") is not None:
            t = f"vat {s['TempOfTank']:.0f} °C"
            if s.get("TempTargetTank"):
                t += f" (target {s['TempTargetTank']:.0f})"
            temps.append(t)
        self.temp_label.setText(", ".join(temps) or "—")
        err = pi.get("ErrorNumber")
        if err:
            self.state_label.setText(st.summary() + " — " + C.describe(err, C.PRINT_ERROR))
        self._update_buttons()

    def _update_buttons(self):
        connected = bool(self.session and self.session.connected)
        st = self.state
        printing = bool(st and st.is_printing)
        self.connect_btn.setEnabled(self.current() is not None)
        ps = st.print_status if st else None
        # Pausing (5) lasts until the current layer finishes (~60 s on the 16K) [RUNTIME]
        self.pause_btn.setEnabled(connected and printing and ps not in (5, 7))
        self.pause_btn.setText("Resume" if st and st.is_paused else "Pause")
        self.stop_btn.setEnabled(connected and printing and ps != 7)
        self.refresh_files_btn.setEnabled(connected)
        sel = bool(self.files.selectedItems())
        self.print_file_btn.setEnabled(connected and sel and not printing and not self._uploading)
        self.delete_file_btn.setEnabled(connected and sel and not self._uploading)
        self.send_btn.setEnabled(connected and not self._uploading)

    # -------------------------------------------------------------- confirm
    def confirm(self, title: str, text: str) -> bool:
        """Every state-changing action goes through here (tests replace it)."""
        return QMessageBox.question(self, title, text, QMessageBox.Yes | QMessageBox.No,
                                    QMessageBox.No) == QMessageBox.Yes

    def _run(self, fn, *args, done_msg: str = "", after=None):
        def ok(_):
            if done_msg:
                self.log.emit(done_msg)
            if after:
                after()

        def bad(msg):
            self.log.emit("Printer: " + _first_line(msg))
            QMessageBox.warning(self, APP, _first_line(msg))
        workers.submit(fn, *args, on_done=ok, on_failed=bad)

    # ------------------------------------------------------------- controls
    def pause_resume(self):
        if not self.session:
            return
        if self.state.is_paused:
            self._run(self.session.resume, done_msg="Print resumed")
        elif self.confirm("Pause print", "Pause the current print?\n\n"
                                           "The printer finishes the current layer first (about a minute)."):
            self._we_paused = True
            self._run(self.session.pause, done_msg="Pause requested")

    def stop_print(self):
        if self.session and self.confirm("Stop print", "Stop the current print? It cannot be resumed."):
            self._run(self.session.stop_print, done_msg="Print stopped")

    def refresh_files(self):
        if not self.session:
            return
        workers.submit(self.session.list_files, on_done=self._files,
                       on_failed=lambda m: self.log.emit("File list: " + _first_line(m)))

    def _files(self, files):
        sel = {i.data(Qt.UserRole) for i in self.files.selectedItems()}
        self.files.clear()
        for f in files:
            name = str(f.get("name", ""))
            if f.get("type", 1) == 0:
                continue                       # folders [PUBLIC-SPEC type 0]
            size = f.get("usedSize") or f.get("totalSize")      # absent on the 16K (V1.5.6) [RUNTIME]
            label = name.rsplit("/", 1)[-1]
            it = QListWidgetItem(f"{label}   ({size / 1e6:.1f} MB)" if size else label)
            it.setData(Qt.UserRole, name)
            self.files.addItem(it)
            it.setSelected(name in sel)
        self._update_buttons()

    def _selected_files(self) -> list[str]:
        return [i.data(Qt.UserRole) for i in self.files.selectedItems()]

    def print_selected(self):
        names = self._selected_files()
        if names:
            self.start_print(names[0].rsplit("/", 1)[-1])

    def start_print(self, filename: str):
        p = self.current()
        if not self.session or not self.confirm(
                "Start print", f"Start printing “{filename}” on {p.name if p else 'the printer'}?\n\n"
                               "Make sure the build plate is fitted and the vat has enough resin."):
            return
        self._run(self.session.start_print, filename, done_msg=f"Print started: {filename}")

    def delete_selected(self):
        names = self._selected_files()
        if names and self.session and self.confirm(
                "Delete files", "Delete from the printer?\n\n" + "\n".join(n.rsplit('/', 1)[-1] for n in names)):
            self._run(self.session.delete_files, names, done_msg=f"Deleted {len(names)} file(s)",
                      after=self.refresh_files)

    # --------------------------------------------------------------- upload
    def send_dialog(self):
        path, _ = QFileDialog.getOpenFileName(self, "Send to printer", self.settings.value(
            "lastGooDir", str(Path.home())), "ELEGOO GOO (*.goo)")
        if path:
            self.send_file(path)

    def send_file(self, path: str, start: bool | None = None) -> bool:
        """Check, upload and (after confirmation) start a .goo. Returns False if not started."""
        p = self.current()
        if not self.session or not self.session.connected or p is None:
            QMessageBox.warning(self, APP, "Connect to a printer first (Printer control panel).")
            return False
        if self._uploading:
            return False
        try:
            header = read_goo_header(path)
        except (OSError, goo.GooError, Exception) as e:  # noqa: B014 - any unreadable file
            QMessageBox.warning(self, APP, f"Cannot read {Path(path).name}: {e}")
            return False
        problems = C.check_file_for_printer(header, self.state, p)
        if problems:
            QMessageBox.warning(self, APP, f"{Path(path).name} cannot be sent to {p.name}:\n\n• " +
                                "\n• ".join(problems))
            return False
        name = os.path.basename(path)
        if not self.confirm("Send to printer", f"Upload “{name}” ({os.path.getsize(path) / 1e6:.1f} MB) "
                                               f"to {p.name}?"):
            return False
        self._start_after = start
        self._uploading = True
        self._upload_cancel.clear()
        self.upload_bar.setRange(0, 1000)
        self.upload_bar.setValue(0)
        self.upload_bar.show()
        self.cancel_upload_btn.show()
        self._update_buttons()
        self.log.emit(f"Uploading {name} to {p.name}…")
        sess = self.session

        def work():
            try:
                sess.upload(path, progress=self.uploadProgress.emit, cancelled=self._upload_cancel.is_set)
            except Exception as e:  # report every failure in the panel
                self.uploadFinished.emit(name, False, "cancelled" if self._upload_cancel.is_set()
                                         else str(e) or type(e).__name__)
            else:
                self.uploadFinished.emit(name, True, "")
        threading.Thread(target=work, name="goo-upload", daemon=True).start()
        return True

    def _upload_progress(self, done, total):
        self.upload_bar.setValue(int(1000 * done / max(1, total)))
        self.upload_bar.setFormat(f"{done / 1e6:.1f} / {total / 1e6:.1f} MB")

    def _upload_finished(self, name, ok, msg):
        self._uploading = False
        self.upload_bar.hide()
        self.cancel_upload_btn.hide()
        self._update_buttons()
        if not ok:
            self.log.emit(f"Upload of {name} {'cancelled' if msg == 'cancelled' else 'failed: ' + msg}")
            if msg != "cancelled":
                QMessageBox.warning(self, APP, f"Upload of {name} failed: {msg}")
            return
        self.log.emit(f"Uploaded {name}")
        self.refresh_files()
        if self._start_after is not False:
            self.start_print(name)

    def shutdown(self):
        self._upload_cancel.set()
        self.disconnect_printer()
