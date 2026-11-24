import os
import random

import pytest

from elegoo_goo import rle


def test_empty_black_image():
    enc = rle.encode(bytes(16))
    assert enc[0] == 0x55 and enc[1] == 0x00 | 1 << 4 | 0 and enc[2] == 1  # len 16 = 1<<4|0
    assert rle.decode(enc, 4, 4) == bytes(16)


def test_checksum_is_not_sum_of_body():
    enc = rle.encode(bytes([0, 0, 255, 7, 7]))
    body = enc[1:-1]
    assert enc[-1] == (~sum(body)) & 0xFF


@pytest.mark.parametrize("n", [1, 15, 16, 4095, 4096, 1 << 20, (1 << 20) + 3, 15120 * 6230])
def test_long_runs_roundtrip(n):
    data = bytes([255]) * n
    assert rle.decode(rle.encode(data), n, 1) == data


def test_gray_value_precedes_length_bytes():
    enc = rle.encode(bytes([0x80]) * 20)  # len 20 -> lb=1, nib=4, hi=1
    assert enc[1:4] == bytes([0b01_01_0100, 0x80, 0x01])


def test_random_roundtrip():
    rnd = random.Random(1)
    w, h = 97, 53
    px = bytearray()
    while len(px) < w * h:
        px += bytes([rnd.choice([0, 255, rnd.randrange(256)])]) * rnd.randrange(1, 300)
    px = bytes(px[: w * h])
    assert rle.decode(rle.encode(px), w, h) == px


def test_decoder_handles_delta_runs():
    # hand-built: gray 100 x3, then delta +5 (len 1), delta -2 with len byte 4
    body = bytes([0b01_00_0011, 100, 0b10_00_0101, 0b10_11_0010, 4])
    data = bytes([0x55]) + body + bytes([(~sum(body)) & 0xFF])
    assert rle.decode(data, 8, 1) == bytes([100, 100, 100, 105, 103, 103, 103, 103])


def test_bad_checksum_and_header():
    enc = bytearray(rle.encode(bytes(10)))
    enc[-1] ^= 1
    with pytest.raises(rle.RleError):
        rle.decode(bytes(enc), 10, 1)
    assert rle.decode(bytes(enc), 10, 1, strict=False) == bytes(10)
    with pytest.raises(rle.RleError):
        rle.decode(b"\x00\x00", 1, 1)


def test_short_stream_zero_fills():
    body = bytes([0b11_00_0011])  # 3 white
    data = bytes([0x55]) + body + bytes([(~sum(body)) & 0xFF])
    assert rle.decode(data, 5, 1) == bytes([255, 255, 255, 0, 0])
