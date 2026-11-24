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


if __name__ == "__main__":  # python -m gooslicer.qdatastream FILE…
    import json
    for p in sys.argv[1:]:
        m = read_variant_map(p)
        print(json.dumps(m, indent=1, ensure_ascii=False, default=repr))
