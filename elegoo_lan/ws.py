"""Minimal RFC 6455 WebSocket client (stdlib only) replacing QWebSocket for SDCP."""
from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import struct

from . import protocol

_GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
OP_CONT, OP_TEXT, OP_BIN, OP_CLOSE, OP_PING, OP_PONG = 0x0, 0x1, 0x2, 0x8, 0x9, 0xA


class WebSocketError(Exception):
    pass


def encode_frame(opcode: int, payload: bytes, mask: bool = True, mask_key: bytes | None = None) -> bytes:
    head = bytes([0x80 | opcode])
    n = len(payload)
    mbit = 0x80 if mask else 0
    if n < 126:
        head += bytes([mbit | n])
    elif n < 1 << 16:
        head += bytes([mbit | 126]) + struct.pack(">H", n)
    else:
        head += bytes([mbit | 127]) + struct.pack(">Q", n)
    if not mask:
        return head + payload
    key = mask_key if mask_key is not None else os.urandom(4)
    return head + key + bytes(b ^ key[i & 3] for i, b in enumerate(payload))


def _recv_exact(sock: socket.socket, n: int) -> bytes:
    buf = bytearray()
    while len(buf) < n:
        chunk = sock.recv(n - len(buf))
        if not chunk:
            raise WebSocketError("connection closed")
        buf += chunk
    return bytes(buf)


def read_frame(sock: socket.socket) -> tuple[bool, int, bytes]:
    b0, b1 = _recv_exact(sock, 2)
    fin, opcode, masked, n = bool(b0 & 0x80), b0 & 0x0F, bool(b1 & 0x80), b1 & 0x7F
    if n == 126:
        n = struct.unpack(">H", _recv_exact(sock, 2))[0]
    elif n == 127:
        n = struct.unpack(">Q", _recv_exact(sock, 8))[0]
    key = _recv_exact(sock, 4) if masked else None
    data = _recv_exact(sock, n)
    if key:
        data = bytes(b ^ key[i & 3] for i, b in enumerate(data))
    return fin, opcode, data


def accept_key(client_key: str) -> str:
    return base64.b64encode(hashlib.sha1(client_key.encode() + _GUID).digest()).decode()


class _PrefixedSocket:
    """A socket whose first reads come from bytes already received with the handshake (a
    server may send a frame in the same TCP segment as its 101 response)."""

    def __init__(self, sock: socket.socket, prefix: bytes = b""):
        self._sock, self._buf = sock, bytearray(prefix)

    @property
    def buffered(self) -> int:
        return len(self._buf)

    def recv(self, n: int, *flags) -> bytes:
        if self._buf:
            out = bytes(self._buf[:n])
            del self._buf[:n]
            return out
        return self._sock.recv(n, *flags)

    def __getattr__(self, name):
        return getattr(self._sock, name)


class SdcpWebSocket:
    """SDCP v3-style connection: ws://<ip>:3030/websocket."""

    def __init__(self, host: str, mainboard_id: str, conn_id: str = "",
                 port: int = protocol.WS_PORT, timeout: float = 10.0):
        self.host, self.port = host, port
        self.mainboard_id, self.conn_id = mainboard_id, conn_id
        self.sock = socket.create_connection((host, port), timeout=timeout)
        key = base64.b64encode(os.urandom(16)).decode()
        req = (f"GET {protocol.WS_PATH} HTTP/1.1\r\nHost: {host}:{port}\r\n"
               "Upgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n\r\n")
        self.sock.sendall(req.encode())
        resp = b""
        while b"\r\n\r\n" not in resp:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise WebSocketError("handshake: connection closed")
            resp += chunk
        head, _, self._pending = resp.partition(b"\r\n\r\n")
        lines = head.decode("latin-1").split("\r\n")
        if " 101 " not in lines[0] + " ":
            raise WebSocketError(f"handshake failed: {lines[0]}")
        hdrs = {k.strip().lower(): v.strip() for k, _, v in (l.partition(":") for l in lines[1:])}
        if hdrs.get("sec-websocket-accept") != accept_key(key):
            raise WebSocketError("bad Sec-WebSocket-Accept")
        self.sock = _PrefixedSocket(self.sock, self._pending)

    @property
    def buffered(self) -> int:
        """Bytes already read from the socket but not yet consumed as frames."""
        return self.sock.buffered

    def send_text(self, text: str) -> None:
        self.sock.sendall(encode_frame(OP_TEXT, text.encode("utf-8")))

    def request(self, cmd: int, args: dict | None = None) -> str:
        msg = protocol.build_ws_request(cmd, self.mainboard_id, self.conn_id, args)
        self.send_text(protocol.encode(msg))
        return msg["Data"]["RequestID"]

    def recv_message(self) -> dict | str | None:
        """Next text message (JSON-decoded if possible). Handles ping/close/fragments."""
        parts, op0 = [], None
        while True:
            fin, op, data = read_frame(self.sock)
            if op == OP_PING:
                self.sock.sendall(encode_frame(OP_PONG, data))
                continue
            if op == OP_PONG:
                continue
            if op == OP_CLOSE:
                try:
                    self.sock.sendall(encode_frame(OP_CLOSE, data[:2]))
                except OSError:
                    pass
                return None
            if op != OP_CONT:
                op0 = op
            parts.append(data)
            if fin:
                break
        raw = b"".join(parts)
        if op0 != OP_TEXT:
            return None
        text = raw.decode("utf-8")
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def wait_response(self, request_id: str, max_messages: int = 200) -> dict | None:
        """Read until a sdcp/response/ message with matching RequestID arrives."""
        for _ in range(max_messages):
            m = self.recv_message()
            if m is None:
                return None
            if isinstance(m, dict) and protocol.classify_topic(m.get("Topic", "")) == "response":
                if (m.get("Data") or {}).get("RequestID") == request_id:
                    return m
        return None

    def close(self) -> None:
        try:
            self.sock.sendall(encode_frame(OP_CLOSE, struct.pack(">H", 1000)))
        except OSError:
            pass
        self.sock.close()

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
