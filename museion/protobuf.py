"""Protocol buffers by hand: the few pieces of the wire format Museion
needs, so it needs no protobuf library. Used by the radio (Meshtastic's
client API, radio.py) and the maps it makes (vector tiles, mvt.py).

A message is a run of fields, each a key (field number << 3 | wire type)
then its value: a varint (0), eight bytes (1), a length and that many bytes
(2), or four bytes (5). https://protobuf.dev/programming-guides/encoding/
"""

from __future__ import annotations

import struct


def varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


def field_varint(number: int, value: int) -> bytes:
    return varint(number << 3) + varint(value)


def field_fixed32(number: int, value: int) -> bytes:
    return varint((number << 3) | 5) + struct.pack("<I", value & 0xFFFFFFFF)


def field_bytes(number: int, value: bytes) -> bytes:
    return varint((number << 3) | 2) + varint(len(value)) + value


def read_varint(data: bytes, i: int) -> tuple[int, int]:
    n = shift = 0
    while True:
        if i >= len(data):
            raise ValueError("truncated varint")
        b = data[i]
        i += 1
        n |= (b & 0x7F) << shift
        if not b & 0x80:
            return n, i
        shift += 7
        if shift > 63:
            raise ValueError("varint too long")


def decode(data: bytes) -> dict[int, list]:
    """A message's fields, by number: varints and fixed numbers as ints,
    length-delimited fields as bytes. Unknown wire types end the message."""
    out: dict[int, list] = {}
    i = 0
    while i < len(data):
        key, i = read_varint(data, i)
        number, kind = key >> 3, key & 7
        if kind == 0:
            value, i = read_varint(data, i)
        elif kind == 1:
            value = struct.unpack_from("<Q", data, i)[0]
            i += 8
        elif kind == 2:
            length, i = read_varint(data, i)
            value = data[i:i + length]
            if len(value) < length:
                raise ValueError("truncated field")
            i += length
        elif kind == 5:
            value = struct.unpack_from("<I", data, i)[0]
            i += 4
        else:
            raise ValueError(f"wire type {kind}")
        out.setdefault(number, []).append(value)
    return out


def first(fields: dict, number: int, default=None):
    values = fields.get(number)
    return values[0] if values else default


def packed(numbers) -> bytes:
    """A packed repeated field's contents: the varints one after another."""
    return b"".join(varint(n) for n in numbers)


def read_packed(data: bytes) -> list[int]:
    out, i = [], 0
    while i < len(data):
        n, i = read_varint(data, i)
        out.append(n)
    return out


def zigzag(n: int) -> int:
    """A signed number as protobuf's sint32 writes it: 0, -1, 1, -2 ... as
    0, 1, 2, 3 ..., so small negative numbers stay short."""
    return (n << 1) ^ (n >> 31)


def unzigzag(n: int) -> int:
    return (n >> 1) ^ -(n & 1)
