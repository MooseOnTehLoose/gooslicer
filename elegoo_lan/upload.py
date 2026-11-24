"""Chunked multipart upload to http://<ip>:3030/uploadFile/upload (spec §2a)."""
from __future__ import annotations

import hashlib
import http.client
import os
import uuid
from typing import Callable

from . import protocol


def file_md5_hex(path: str) -> str:
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()  # lowercase, as QByteArray::toHex


def build_multipart(fields: list[tuple[str, str]], filename: str, chunk: bytes,
                    boundary: str) -> bytes:
    out = bytearray()
    for name, value in fields:
        out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n"
                f"{value}\r\n").encode("utf-8")
    out += (f"--{boundary}\r\nContent-Disposition: form-data; name=\"File\"; "
            f"filename=\"{filename}\"\r\nContent-Type: application/octet-stream\r\n\r\n").encode("utf-8")
    out += chunk + b"\r\n" + f"--{boundary}--\r\n".encode()
    return bytes(out)


def chunk_ranges(total: int, size: int = protocol.UPLOAD_CHUNK) -> list[tuple[int, int]]:
    return [(off, min(size, total - off)) for off in range(0, total, size)]


class UploadCancelled(Exception):
    pass


def upload(host: str, path: str, *, port: int = protocol.WS_PORT,
           chunk_size: int = protocol.UPLOAD_CHUNK, retries: int = protocol.UPLOAD_RETRIES,
           progress: Callable[[int, int], None] | None = None, timeout: float = 60.0,
           cancelled: Callable[[], bool] | None = None, uuid_out: dict | None = None) -> str:
    """Upload `path`. Returns the upload Uuid. Raises on a chunk failing `retries` times.

    `cancelled` is polled before each chunk (the app checks a cancel flag the same way, spec §2a);
    `uuid_out["uuid"]` receives the transfer Uuid before the first chunk is sent.
    """
    total = os.path.getsize(path)
    if total == 0:
        raise ValueError("empty file")
    md5 = file_md5_hex(path)
    up_uuid = uuid.uuid4().hex
    if uuid_out is not None:
        uuid_out["uuid"] = up_uuid
    name = os.path.basename(path)
    with open(path, "rb") as f:
        for off, n in chunk_ranges(total, chunk_size):
            if cancelled and cancelled():
                raise UploadCancelled()
            f.seek(off)
            data = f.read(n)
            fields = [("TotalSize", str(total)), ("Uuid", up_uuid), ("Offset", str(off)),
                      ("Check", "0"), ("S-File-MD5", md5)]
            last_err: Exception | None = None
            for _ in range(retries):
                boundary = "boundary_" + uuid.uuid4().hex
                body = build_multipart(fields, name, data, boundary)
                try:
                    conn = http.client.HTTPConnection(host, port, timeout=timeout)
                    conn.request("POST", protocol.UPLOAD_PATH, body=body, headers={
                        "Content-Type": f"multipart/form-data; boundary={boundary}",
                        "Content-Length": str(len(body))})
                    resp = conn.getresponse()
                    resp.read()
                    conn.close()
                    if 200 <= resp.status < 300:
                        last_err = None
                        break
                    last_err = RuntimeError(f"HTTP {resp.status} at offset {off}")
                except OSError as e:
                    last_err = e
            if last_err:
                raise last_err
            if progress:
                progress(off + n, total)
    return up_uuid
