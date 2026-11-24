"""GOO layer-image RLE (clean-room, from 03-goo-format-spec.md §5)."""
from __future__ import annotations

import re
from typing import Iterable, Iterator

START = 0x55


def _run_bytes(kind: int, value: int, length: int) -> bytes:
    """Encode one run of `length` pixels (kind 0=black, 1=gray, 3=white)."""
    out = bytearray()
    while length > 0:
        n = min(length, (1 << 28) - 1)
        length -= n
        nib = n & 0xF
        hi = n >> 4
        if hi == 0:
            lb, extra = 0, b""
        elif hi < 1 << 8:
            lb, extra = 1, bytes([hi])
        elif hi < 1 << 16:
            lb, extra = 2, hi.to_bytes(2, "big")
        else:
            lb, extra = 3, hi.to_bytes(3, "big")
        out.append((kind << 6) | (lb << 4) | nib)
        if kind == 1:
            out.append(value)
        out += extra
    return bytes(out)


_RUN = re.compile(rb"\x00+|\xff+|([\x01-\xfe])\1*", re.S)


def iter_runs(pixels) -> Iterator[tuple[int, int]]:
    """Yield (value, length) runs of an 8-bit bitmap at C speed (regex scan)."""
    for m in _RUN.finditer(pixels):
        a, b = m.span()
        yield pixels[a], b - a


def encode_runs(runs: Iterable[tuple[int, int]]) -> bytes:
    """Encode (value, length) runs; adjacent equal values are merged first."""
    body = bytearray()
    pv, pl = None, 0
    for v, n in runs:
        if n <= 0:
            continue
        if v == pv:
            pl += n
            continue
        if pv is not None:
            body += _run_bytes(0 if pv == 0 else 3 if pv == 0xFF else 1, pv, pl)
        pv, pl = v, n
    if pv is not None:
        body += _run_bytes(0 if pv == 0 else 3 if pv == 0xFF else 1, pv, pl)
    return bytes([START]) + bytes(body) + bytes([(~sum(body)) & 0xFF])


def encode(pixels: bytes | bytearray | memoryview) -> bytes:
    """Encode an 8-bit grayscale bitmap (row-major) to `0x55 | chunks | checksum`.

    Uses only types 0/1/3 with minimal length bytes (no delta runs), which
    reproduces SatelLite's output byte-for-byte for the reference sample.
    """
    return encode_runs(iter_runs(bytes(pixels)))


class RleError(ValueError):
    pass


def decode(data: bytes, width: int, height: int, *, strict: bool = True) -> bytearray:
    """Decode to a width*height grayscale bytearray, following the app's decoder."""
    if len(data) < 2 or data[0] != START:
        raise RleError("missing 0x55 start byte")
    body_end = len(data) - 1
    total = width * height
    out = bytearray(total)
    pos, i, prev, csum = 0, 1, 0, 0

    def take() -> int:
        nonlocal i, csum
        if i >= body_end:
            raise RleError("truncated chunk")
        b = data[i]
        i += 1
        csum += b
        return b

    while i < body_end and pos < total:
        h = take()
        kind, nib = h >> 6, h & 0xF
        if kind == 2:
            d = -nib if h & 0x20 else nib
            value = (prev + d) & 0xFF
            length = take() if h & 0x10 else 1
        else:
            value = {0: 0, 3: 0xFF}.get(kind)
            if kind == 1:
                value = take()
            lb = (h >> 4) & 3
            hi = 0
            for _ in range(lb):
                hi = (hi << 8) | take()
            length = (hi << 4) | nib if lb else nib
        if pos + length > total + 1:
            raise RleError("run exceeds image size")
        length = min(length, total - pos)
        out[pos:pos + length] = bytes([value]) * length
        pos += length
        prev = value
    if strict and ((~csum) & 0xFF) != data[-1]:
        raise RleError(f"checksum mismatch: sum={csum & 0xFF:#x} stored={data[-1]:#x}")
    return out


def decode_runs(data: bytes) -> list[tuple[int, int]]:
    """Parse an encoded layer into (value, length) runs without building pixels."""
    if len(data) < 2 or data[0] != START:
        raise RleError("missing 0x55 start byte")
    body = memoryview(data)[1:-1]
    if ((~sum(body)) & 0xFF) != data[-1]:
        raise RleError("checksum mismatch")
    runs, k, prev = [], 0, 0
    while k < len(body):
        h = body[k]
        k += 1
        kind, nib = h >> 6, h & 0xF
        if kind == 2:
            v = (prev + (-nib if h & 0x20 else nib)) & 0xFF
            if h & 0x10:
                n = body[k]
                k += 1
            else:
                n = 1
        else:
            v = 0 if kind == 0 else 0xFF if kind == 3 else body[k]
            k += kind == 1
            lb = (h >> 4) & 3
            n = (int.from_bytes(body[k:k + lb], "big") << 4 | nib) if lb else nib
            k += lb
        runs.append((v, n))
        prev = v
    return runs
