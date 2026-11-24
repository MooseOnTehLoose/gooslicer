"""In-process fake SDCP printer for loopback tests (UDP discovery, WS, HTTP upload)."""
from __future__ import annotations

import email.parser
import email.policy
import json
import socket
import socketserver
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

from elegoo_lan import ws as wsmod

MAINBOARD = "abcdef0123456789"


def discovery_reply(ip="127.0.0.1", nested=True, version="V3.0.0") -> bytes:
    attrs = {"Name": "Saturn", "MachineName": "ELEGOO Saturn 4 Ultra", "ProtocolVersion": version,
             "FirmwareVersion": "V1.2.3", "Resolution": "11520x5120", "MainboardIP": ip,
             "MainboardID": MAINBOARD, "SDCPStatus": 0}
    data = {"Attributes": attrs, "Status": {"CurrentStatus": 0}} if nested else attrs
    return json.dumps({"Id": "conn-1", "Data": data}).encode()


class UdpResponder(threading.Thread):
    def __init__(self, nested=True, version="V3.0.0"):
        super().__init__(daemon=True)
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.port = self.sock.getsockname()[1]
        self.nested, self.version, self.probes = nested, version, []

    def run(self):
        while True:
            try:
                data, addr = self.sock.recvfrom(2048)
            except OSError:
                return
            self.probes.append(data)
            if data == b"M99999":
                self.sock.sendto(b"M99999", addr)  # echo, must be ignored
                self.sock.sendto(b"not json", addr)
                self.sock.sendto(discovery_reply(nested=self.nested, version=self.version), addr)


class _WsHandler(socketserver.BaseRequestHandler):
    def handle(self):
        srv = self.server
        req = b""
        while b"\r\n\r\n" not in req:
            req += self.request.recv(4096)
        hdrs = {k.strip().lower(): v.strip() for k, _, v in
                (l.partition(":") for l in req.decode().split("\r\n")[1:] if l)}
        srv.paths.append(req.split(b" ")[1].decode())
        self.request.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\n"
                              "Connection: Upgrade\r\nSec-WebSocket-Accept: "
                              f"{wsmod.accept_key(hdrs['sec-websocket-key'])}\r\n\r\n").encode())
        # unsolicited status push + server ping before any reply
        status = {"Topic": f"sdcp/status/{MAINBOARD}", "Status": {"CurrentStatus": [0]}}
        self.request.sendall(wsmod.encode_frame(wsmod.OP_TEXT, json.dumps(status).encode(), mask=False))
        self.request.sendall(wsmod.encode_frame(wsmod.OP_PING, b"hb", mask=False))
        while True:
            try:
                fin, op, data = wsmod.read_frame(self.request)
            except wsmod.WebSocketError:
                return
            if op == wsmod.OP_PONG:
                srv.pongs.append(data); continue
            if op == wsmod.OP_CLOSE:
                return
            msg = json.loads(data)
            srv.received.append(msg)
            d = msg["Data"]
            resp = {"Id": msg["Id"], "Topic": f"sdcp/response/{MAINBOARD}",
                    "Data": {"Cmd": d["Cmd"], "Data": {"Ack": 0}, "RequestID": d["RequestID"],
                             "MainboardID": MAINBOARD, "TimeStamp": d["TimeStamp"]}}
            raw = json.dumps(resp).encode()
            # send fragmented to exercise continuation handling
            half = len(raw) // 2
            self.request.sendall(bytes([0x01, half]) + raw[:half] if half < 126 else
                                 wsmod.encode_frame(wsmod.OP_TEXT, raw, mask=False))
            if half < 126:
                rest = raw[half:]
                self.request.sendall(bytes([0x80, len(rest)]) + rest)


class WsServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(self):
        super().__init__(("127.0.0.1", 0), _WsHandler)
        self.received, self.pongs, self.paths = [], [], []
        threading.Thread(target=self.serve_forever, daemon=True).start()


class UploadServer(HTTPServer):
    def __init__(self, fail_first=0):
        self.chunks, self.fail_left = [], fail_first
        srv = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers["Content-Length"]))
                if srv.fail_left > 0:
                    srv.fail_left -= 1
                    self.send_response(500); self.end_headers(); return
                msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
                    b"Content-Type: " + self.headers["Content-Type"].encode() + b"\r\n\r\n" + body)
                parts = []
                for p in msg.iter_parts():
                    parts.append((p.get_param("name", header="content-disposition"),
                                  p.get_filename(), p.get_content_type(), p.get_payload(decode=True)))
                srv.chunks.append({"path": self.path, "parts": parts})
                self.send_response(200); self.end_headers(); self.wfile.write(b'{"code":"000000"}')

        super().__init__(("127.0.0.1", 0), H)
        threading.Thread(target=self.serve_forever, daemon=True).start()
