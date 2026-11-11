"""Vector map tiles in the Mapbox Vector Tile format (version 2.1), written
and read by hand with protobuf.py.

A tile is a square of the map at one zoom level, holding named layers of
features: points, lines and polygons, in whole-number coordinates from 0 to
the tile's extent (4096) with y pointing down, each with a few properties
(class = "primary", name = "Mill Lane"). It is the format OpenMapTiles and
most vector .mbtiles files use, so maps Museion makes open in other map
programs, and the page draws vector maps made elsewhere too (ui/mvt.js
reads them). https://github.com/mapbox/vector-tile-spec/tree/master/2.1

    Tile    { repeated Layer layers = 3 }
    Layer   { uint32 version = 15; string name = 1; repeated Feature features = 2;
              repeated string keys = 3; repeated Value values = 4; uint32 extent = 5 }
    Feature { uint64 id = 1; packed uint32 tags = 2; GeomType type = 3;
              packed uint32 geometry = 4 }
    Value   { string = 1; float = 2; double = 3; int64 = 4; uint64 = 5;
              sint64 = 6; bool = 7 }

A geometry is a run of commands (MoveTo 1, LineTo 2, ClosePath 7), each a
command integer (id | count << 3) then count pairs of zigzag-encoded
moves from the last point. A polygon's outer rings turn clockwise on screen
(a positive area by the spec's formula) and its holes the other way.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field

from museion.protobuf import (decode, field_bytes, field_varint, packed, read_packed, unzigzag, varint,
                              zigzag)

EXTENT = 4096
POINT, LINESTRING, POLYGON = 1, 2, 3
MOVE_TO, LINE_TO, CLOSE_PATH = 1, 2, 7

Coords = list[tuple[int, int]]


@dataclass
class Feature:
    type: int  # POINT, LINESTRING or POLYGON
    # POINT: one list of points. LINESTRING: one list per line. POLYGON: its
    # rings, each outer ring followed by its holes, not repeating the first
    # point at the end.
    parts: list[Coords]
    properties: dict = field(default_factory=dict)


def ring_area(ring: Coords) -> float:
    """The spec's surveyor's formula: positive for a ring that turns
    clockwise on screen (y down), as an outer ring must."""
    total = 0
    for i, (x, y) in enumerate(ring):
        x2, y2 = ring[(i + 1) % len(ring)]
        total += x * y2 - x2 * y
    return total / 2


def command(cmd: int, count: int) -> int:
    return (cmd & 0x7) | (count << 3)


def encode_geometry(feature: Feature) -> list[int]:
    out: list[int] = []
    cx = cy = 0

    def moves(points: Coords) -> None:
        nonlocal cx, cy
        for x, y in points:
            out.append(zigzag(x - cx))
            out.append(zigzag(y - cy))
            cx, cy = x, y

    if feature.type == POINT:
        points = feature.parts[0]
        out.append(command(MOVE_TO, len(points)))
        moves(points)
        return out
    for part in feature.parts:
        out.append(command(MOVE_TO, 1))
        moves(part[:1])
        out.append(command(LINE_TO, len(part) - 1))
        moves(part[1:])
        if feature.type == POLYGON:
            out.append(command(CLOSE_PATH, 1))
    return out


def encode_value(value) -> bytes:
    if isinstance(value, bool):
        return field_varint(7, int(value))
    if isinstance(value, int):
        # sint64: the one that stays short for small negative numbers too.
        return field_varint(6, (value << 1) ^ (value >> 63))
    if isinstance(value, float):
        return varint((3 << 3) | 1) + struct.pack("<d", value)
    return field_bytes(1, str(value).encode("utf-8"))


def encode_layer(name: str, features: list[Feature], extent: int = EXTENT) -> bytes:
    keys: dict[str, int] = {}
    values: dict[tuple, int] = {}
    body = bytearray(field_bytes(1, name.encode("utf-8")))
    for f in features:
        tags = []
        for k, v in f.properties.items():
            if v is None or v == "":
                continue
            tags.append(keys.setdefault(k, len(keys)))
            tags.append(values.setdefault((type(v).__name__, v), len(values)))
        msg = bytearray()
        if tags:
            msg += field_bytes(2, packed(tags))
        msg += field_varint(3, f.type)
        msg += field_bytes(4, packed(encode_geometry(f)))
        body += field_bytes(2, bytes(msg))
    for k in keys:
        body += field_bytes(3, k.encode("utf-8"))
    for (_, v) in values:
        body += field_bytes(4, encode_value(v))
    body += field_varint(5, extent)
    body += field_varint(15, 2)
    return bytes(body)


def encode_tile(layers: dict[str, list[Feature]], extent: int = EXTENT) -> bytes:
    """A tile from its layers, in the order given (the order they are drawn
    in, bottom first). Empty layers are left out."""
    return b"".join(field_bytes(3, encode_layer(name, feats, extent)) for name, feats in layers.items() if feats)


# ── reading (for the tests, and for checking a map) ──────────────────────────

def decode_value(data: bytes):
    fields = decode(data)
    if 1 in fields:
        return fields[1][0].decode("utf-8")
    if 2 in fields:
        return struct.unpack("<f", struct.pack("<I", fields[2][0]))[0]
    if 3 in fields:
        return struct.unpack("<d", struct.pack("<Q", fields[3][0]))[0]
    if 4 in fields:
        n = fields[4][0]
        return n - (1 << 64) if n >= 1 << 63 else n
    if 5 in fields:
        return fields[5][0]
    if 6 in fields:
        return unzigzag(fields[6][0])
    if 7 in fields:
        return bool(fields[7][0])
    return None


def decode_geometry(kind: int, numbers: list[int]) -> list[Coords]:
    parts: list[Coords] = []
    x = y = i = 0
    while i < len(numbers):
        cmd, count = numbers[i] & 0x7, numbers[i] >> 3
        i += 1
        if cmd == CLOSE_PATH:
            continue
        for _ in range(count):
            x += unzigzag(numbers[i])
            y += unzigzag(numbers[i + 1])
            i += 2
            if cmd == MOVE_TO and (kind != POINT or not parts):
                parts.append([])
            parts[-1].append((x, y))
    return parts


def decode_tile(data: bytes) -> dict[str, list[Feature]]:
    out: dict[str, list[Feature]] = {}
    for raw in decode(data).get(3, []):
        layer = decode(raw)
        keys = [k.decode("utf-8") for k in layer.get(3, [])]
        values = [decode_value(v) for v in layer.get(4, [])]
        feats = []
        for rf in layer.get(2, []):
            f = decode(rf)
            kind = f.get(3, [0])[0]
            tags = read_packed(f[2][0]) if 2 in f else []
            props = {keys[tags[j]]: values[tags[j + 1]] for j in range(0, len(tags) - 1, 2)}
            feats.append(Feature(kind, decode_geometry(kind, read_packed(f[4][0]) if 4 in f else []), props))
        out[layer[1][0].decode("utf-8")] = feats
    return out
