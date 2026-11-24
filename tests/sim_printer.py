"""Stateful SDCP v3 printer simulator (loopback UDP discovery + WebSocket + HTTP upload).

Message shapes copy the runtime captures in captures/ (Saturn 4 Ultra 16K); state transitions
follow the published SDCP v3 status codes. Used by tests and for demoing the GUI without hardware:

    python -m tests.sim_printer        # serves on 127.0.0.1, prints the ports
"""
from __future__ import annotations

import email.parser
import email.policy
import hashlib
import json
import socket
import socketserver
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

from elegoo_lan import ws as wsmod

MAINBOARD = "5151000000000001"
CONN_ID = "sim0000000000000000000000000000"


class SimPrinter:
    def __init__(self, name="Sim", model="Saturn 4 Ultra 16K", resolution="15120x6230",
                 version="V3.0.0", layer_period=0.05, host="127.0.0.1", mainboard=MAINBOARD):
        self.mainboard = mainboard
        self.name, self.model, self.resolution, self.version = name, model, resolution, version
        self.layer_period = layer_period
        self.host = host
        self.files: dict[str, bytes] = {}
        self.partial: dict[str, dict] = {}
        self.commands: list[dict] = []
        self.lock = threading.RLock()
        self.machine = [0]
        self.print = {"Status": 0, "CurrentLayer": 0, "TotalLayer": 0, "CurrentTicks": 0,
                      "TotalTicks": 0, "ErrorNumber": 0, "Filename": "", "TaskId": ""}
        self.clients: list[socket.socket] = []
        self._stop = threading.Event()
        # UDP discovery
        self.udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.udp.bind((host, 0))
        self.udp_port = self.udp.getsockname()[1]
        threading.Thread(target=self._udp_loop, daemon=True).start()
        # WebSocket + HTTP upload share one port on the real printer; here they are two servers
        sim = self

        class WsHandler(socketserver.BaseRequestHandler):
            def handle(self):
                sim._ws_session(self.request)

        class Tcp(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self.ws_server = Tcp((host, 0), WsHandler)
        self.ws_port = self.ws_server.server_address[1]
        threading.Thread(target=self.ws_server.serve_forever, daemon=True).start()

        class UpHandler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                fields, data, fname = {}, b"", ""
                for part in msg.iter_parts():
                    n = part.get_param("name", header="content-disposition")
                    if n == "File":
                        data, fname = part.get_payload(decode=True), part.get_filename()
                    else:
                        fields[n] = part.get_payload(decode=True).decode()
                sim._chunk(fields, fname, data)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b'{"code":"000000","messages":null,"data":null,"success":true}')

        self.http = HTTPServer((host, 0), UpHandler)
        self.http_port = self.http.server_address[1]
        threading.Thread(target=self.http.serve_forever, daemon=True).start()
        threading.Thread(target=self._tick_loop, daemon=True).start()

    # --------------------------------------------------------------- shapes
    def discovery(self) -> dict:
        return {"Id": CONN_ID, "Data": {"Name": self.name, "MachineName": self.model,
                                        "BrandName": "ELEGOO", "MainboardIP": self.host,
                                        "MainboardID": self.mainboard, "ProtocolVersion": self.version,
                                        "FirmwareVersion": "V1.5.6"}}

    def status_msg(self) -> dict:
        with self.lock:
            st = {"CurrentStatus": list(self.machine), "ReleaseFilm": 0, "TempOfUVLED": 24.5,
                  "TimeLapseStatus": 0, "HeatStatus": 1, "TempOfTank": 27, "TempTargetTank": 30,
                  "PrintInfo": dict(self.print)}
        return {"Status": st, "MainboardID": self.mainboard, "TimeStamp": int(time.time()),
                "Topic": f"sdcp/status/{self.mainboard}"}

    def attributes_msg(self) -> dict:
        return {"Attributes": {"Name": self.name, "MachineName": self.model, "BrandName": "ELEGOO",
                               "ProtocolVersion": self.version, "FirmwareVersion": "V1.5.6",
                               "Resolution": self.resolution, "XYZsize": "218.88x128.88x220",
                               "MainboardIP": self.host, "MainboardID": self.mainboard,
                               "Capabilities": ["FILE_TRANSFER", "PRINT_CONTROL", "VIDEO_STREAM"],
                               "SupportFileType": ["CTB", "GOO"], "RemainingMemory": 5_000_000_000},
                "MainboardID": self.mainboard, "TimeStamp": int(time.time()),
                "Topic": f"sdcp/attributes/{self.mainboard}"}

    # ------------------------------------------------------------------ io
    def _udp_loop(self):
        while not self._stop.is_set():
            try:
                data, addr = self.udp.recvfrom(2048)
            except OSError:
                return
            if data == b"M99999":
                self.udp.sendto(json.dumps(self.discovery()).encode(), addr)

    def _push(self, msg: dict):
        raw = wsmod.encode_frame(wsmod.OP_TEXT, json.dumps(msg).encode(), mask=False)
        for c in list(self.clients):
            try:
                c.sendall(raw)
            except OSError:
                self.clients.remove(c)

    def _ws_session(self, sock):
        req = b""
        while b"\r\n\r\n" not in req:
            chunk = sock.recv(4096)
            if not chunk:
                return
            req += chunk
        hdrs = {k.strip().lower(): v.strip() for k, _, v in
                (l.partition(":") for l in req.decode().split("\r\n")[1:] if l)}
        sock.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                      f"Sec-WebSocket-Accept: {wsmod.accept_key(hdrs['sec-websocket-key'])}\r\n\r\n").encode())
        self.clients.append(sock)
        while True:
            try:
                fin, op, data = wsmod.read_frame(sock)
            except (wsmod.WebSocketError, OSError):
                break
            if op == wsmod.OP_CLOSE:
                break
            if op != wsmod.OP_TEXT:
                continue
            msg = json.loads(data)
            self.commands.append(msg)
            d = msg["Data"]
            payload = self._handle(d["Cmd"], d.get("Data") or {})
            resp = {"Id": msg["Id"], "Data": {"Cmd": d["Cmd"], "Data": payload,
                                               "RequestID": d["RequestID"], "MainboardID": self.mainboard,
                                               "TimeStamp": int(time.time())},
                    "Topic": f"sdcp/response/{self.mainboard}"}
            sock.sendall(wsmod.encode_frame(wsmod.OP_TEXT, json.dumps(resp).encode(), mask=False))
            if d["Cmd"] == 0 or d["Cmd"] >= 128:
                self._push(self.status_msg())
            if d["Cmd"] == 1:
                self._push(self.attributes_msg())
        if sock in self.clients:
            self.clients.remove(sock)

    # --------------------------------------------------------------- logic
    def _handle(self, cmd: int, a: dict) -> dict:
        with self.lock:
            if cmd in (0, 1):
                return {"Ack": 0}
            if cmd == 128:
                if self.machine != [0]:
                    return {"Ack": 1}
                name = a.get("Filename", "")
                if name not in self.files:
                    return {"Ack": 2}
                layers = _goo_layers(self.files[name])
                self.machine = [1]
                self.print.update(Status=1, CurrentLayer=int(a.get("StartLayer", 0)), TotalLayer=layers,
                                  CurrentTicks=0, TotalTicks=int(layers * self.layer_period * 1000),
                                  Filename=name, TaskId=hashlib.md5(name.encode()).hexdigest())
                return {"Ack": 0}
            if cmd == 129:
                if self.machine != [1]:
                    return {"Ack": 1}
                self.print["Status"] = 6
                return {"Ack": 0}
            if cmd == 131:
                if self.print["Status"] != 6:
                    return {"Ack": 1}
                self.print["Status"] = 3
                return {"Ack": 0}
            if cmd == 130:
                if self.machine != [1]:
                    return {"Ack": 1}
                self.print["Status"] = 8
                self.machine = [0]
                return {"Ack": 0}
            if cmd == 258:
                # the 16K (V1.5.6) lists only name + type (1 = file) [RUNTIME]
                return {"Ack": 0, "FileList": [{"name": f"/local/{n}", "type": 1}
                                               for n in sorted(self.files)]}
            if cmd == 259:
                for f in a.get("FileList", []):
                    self.files.pop(f.rsplit("/", 1)[-1], None)
                return {"Ack": 0}
            if cmd == 255:
                self.partial.pop(a.get("Uuid", ""), None)
                return {"Ack": 0}
            return {"Ack": 0}

    def _chunk(self, f: dict, fname: str, data: bytes):
        with self.lock:
            p = self.partial.setdefault(f["Uuid"], {"buf": bytearray(int(f["TotalSize"])), "got": 0})
            off = int(f["Offset"])
            p["buf"][off:off + len(data)] = data
            p["got"] += len(data)
            if p["got"] >= int(f["TotalSize"]):
                buf = bytes(p.pop("buf"))
                self.partial.pop(f["Uuid"], None)
                if hashlib.md5(buf).hexdigest() == f["S-File-MD5"]:
                    self.files[fname] = buf

    def _tick_loop(self):
        while not self._stop.wait(self.layer_period):
            changed = False
            with self.lock:
                if self.machine == [1] and self.print["Status"] in (1, 2, 3, 4):
                    self.print["Status"] = 3
                    self.print["CurrentLayer"] += 1
                    self.print["CurrentTicks"] += int(self.layer_period * 1000)
                    if self.print["CurrentLayer"] >= self.print["TotalLayer"]:
                        self.print["Status"] = 9
                        self.machine = [0]
                    changed = True
            if changed:
                self._push(self.status_msg())

    def close(self):
        self._stop.set()
        for s in (self.ws_server, self.http):
            s.shutdown()
            s.server_close()
        self.udp.close()


def _goo_layers(data: bytes) -> int:
    try:
        from elegoo_goo import goo
        return goo.Header.unpack(data[:goo.HEADER_STRUCT.size]).layer_count
    except Exception:
        return 100


if __name__ == "__main__":
    s = SimPrinter(layer_period=0.5)
    print(f"Simulated printer: UDP discovery {s.udp_port}, WebSocket {s.ws_port}, HTTP upload {s.http_port}")
    print("Ctrl+C to stop")
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        s.close()
