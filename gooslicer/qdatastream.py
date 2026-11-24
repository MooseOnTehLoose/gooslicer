"""Minimal Qt 5 QDataStream (big-endian) reader for QVariantMap files such as SatelLite's *.satsup.

Clean-room from the Qt serialization format (QVariant = quint32 type, quint8 isNull, payload).
"""
from __future__ import annotations

import struct
import sys


class Reader:
    def __init__(self, data: bytes):
        self.d, self.o = data, 0

    def take(self, n):
        if self.o + n > len(self.d):
            raise EOFError(f"need {n} at {self.o}")
        b = self.d[self.o:self.o + n]
        self.o += n
        return b

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def qstring(self):
        n = self.u32()
        if n == 0xFFFFFFFF:
            return None
        return self.take(n).decode("utf-16-be")

    def variant(self):
        t = self.u32()
        self.take(1)                                    # isNull flag
        return self.value(t)

    def value(self, t):
        if t == 1:  return bool(self.take(1)[0])        # bool
        if t == 2:  return struct.unpack(">i", self.take(4))[0]
        if t == 3:  return self.u32()
        if t == 4:  return struct.unpack(">q", self.take(8))[0]
        if t == 5:  return struct.unpack(">Q", self.take(8))[0]
        if t == 6:  return struct.unpack(">d", self.take(8))[0]
        if t == 135: return struct.unpack(">f", self.take(4))[0]   # Qt4 Float (QSettings INI)
        if t == 38: return struct.unpack(">d", self.take(8))[0]    # Qt5 Float, double precision on the wire
        if t == 10: return self.qstring()
        if t == 11: return [self.qstring() for _ in range(self.u32())]
        if t == 12:
            n = self.u32()
            return None if n == 0xFFFFFFFF else self.take(n)
        if t == 9:  return [self.variant() for _ in range(self.u32())]
        if t == 8:  return {self.qstring(): self.variant() for _ in range(self.u32())}
        if t == 0:  return None
        raise ValueError(f"unsupported QVariant type {t} at {self.o}")

    def variant_map(self):
        return {self.qstring(): self.variant() for _ in range(self.u32())}


def read_variant_map(path: str) -> dict:
    r = Reader(open(path, "rb").read())
    m = r.variant_map()
    if r.o != len(r.d):
        raise ValueError(f"{len(r.d) - r.o} trailing bytes")
    return m


# ------------------------------------------------------------------ writer
BOOL, INT, UINT, LONG, ULONG, DOUBLE, STRING, FLOAT = 1, 2, 3, 4, 5, 6, 10, 38


def _qstring(s: str) -> bytes:
    b = s.encode("utf-16-be")
    return struct.pack(">I", len(b)) + b


def _variant(t: int, v) -> bytes:
    head = struct.pack(">IB", t, 0)
    if t == BOOL:   return head + struct.pack(">?", bool(v))
    if t == INT:    return head + struct.pack(">i", int(v))
    if t == UINT:   return head + struct.pack(">I", int(v))
    if t == LONG:   return head + struct.pack(">q", int(v))
    if t == ULONG:  return head + struct.pack(">Q", int(v))
    if t in (DOUBLE, FLOAT): return head + struct.pack(">d", float(v))   # Qt5 Float: a double on the wire
    if t == STRING: return head + _qstring(str(v))
    raise ValueError(f"cannot write QVariant type {t}")


def write_variant_map(path, typed: dict) -> None:
    """Write {key: (qvariant type, value)} as a Qt 5 QDataStream QVariantMap. Keys go in reverse
    UTF-16 order, as Qt 5 serialises a QMap (seen in SatelLite's .satres)."""
    keys = sorted(typed, key=lambda k: k.encode("utf-16-be"), reverse=True)
    data = struct.pack(">I", len(keys)) + b"".join(_qstring(k) + _variant(*typed[k]) for k in keys)
    tmp = f"{path}.tmp"
    with open(tmp, "wb") as fh:
        fh.write(data)
    import os
    os.replace(tmp, path)


if __name__ == "__main__":  # python -m gooslicer.qdatastream FILE…
    import json
    for p in sys.argv[1:]:
        m = read_variant_map(p)
        print(json.dumps(m, indent=1, ensure_ascii=False, default=repr))
