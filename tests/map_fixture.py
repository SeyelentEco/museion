"""A made-up town as the Overpass API would describe it (out geom): streets,
a main road, a river and a lake, a wood with a clearing, buildings, a place
name and the points that matter in an emergency. For the map-making tests
and for trying the Map tab without the internet."""

import math

DLAT = 1 / 110570  # a metre north
LAT, LON = 50.8700, 0.0100


def m_to_lonlat(x: float, y: float, lat: float = LAT, lon: float = LON) -> dict:
    """x metres east and y metres north of the centre."""
    return {"lat": round(lat + y * DLAT, 7), "lon": round(lon + x / (111320 * math.cos(math.radians(lat))), 7)}


def way(i: int, points, tags: dict) -> dict:
    return {"type": "way", "id": i, "tags": tags, "geometry": [m_to_lonlat(x, y) for x, y in points]}


def node(i: int, x: float, y: float, tags: dict) -> dict:
    return {"type": "node", "id": i, **m_to_lonlat(x, y), "tags": tags}


def rect(x: float, y: float, w: float, h: float):
    return [(x, y), (x + w, y), (x + w, y + h), (x, y + h), (x, y)]


def town() -> dict:
    e = []
    n = 1000
    # A grid of streets every 200 m over 2 km, and a main road across it.
    for k in range(-5, 6):
        e.append(way(n := n + 1, [(k * 200, -1000), (k * 200, 1000)], {"highway": "residential", "name": f"{k + 6} Street"}))
        e.append(way(n := n + 1, [(-1000, k * 200), (1000, k * 200)], {"highway": "residential", "name": f"Lane {k + 6}"}))
    e.append(way(n := n + 1, [(-3000, -2500), (0, 0), (3000, 2600)], {"highway": "primary", "name": "London Road", "ref": "A26"}))
    e.append(way(n := n + 1, [(-3000, 900), (3000, 1100)], {"railway": "rail"}))
    # A river winding through, and a lake.
    e.append(way(n := n + 1, [(-3000 + i * 300, -300 + 150 * math.sin(i / 2)) for i in range(21)],
                 {"waterway": "river", "name": "River Ouse"}))
    lake = [(600 + 250 * math.cos(a / 10 * 2 * math.pi), -650 + 150 * math.sin(a / 10 * 2 * math.pi)) for a in range(10)]
    e.append(way(n := n + 1, lake + lake[:1], {"natural": "water", "name": "Mill Pond"}))
    # A wood with a clearing in it, as a multipolygon of two outer pieces and a hole.
    outer = [(-1800, 400), (-1200, 400), (-1200, 1500), (-1800, 1500)]
    e.append({"type": "relation", "id": 9001, "tags": {"type": "multipolygon", "landuse": "forest", "name": "Long Wood"},
              "members": [
                  {"type": "way", "ref": 1, "role": "outer", "geometry": [m_to_lonlat(*p) for p in outer[:3]]},
                  {"type": "way", "ref": 2, "role": "outer", "geometry": [m_to_lonlat(*p) for p in [outer[2], outer[3], outer[0]]]},
                  {"type": "way", "ref": 3, "role": "inner", "geometry": [m_to_lonlat(*p) for p in rect(-1600, 800, 200, 200)]},
              ]})
    e.append(way(n := n + 1, rect(-1000, -1000, 2000, 2000), {"landuse": "residential"}))
    e.append(way(n := n + 1, rect(200, 200, 160, 160), {"leisure": "park", "name": "Town Green"}))
    # Houses along the streets.
    for i in range(-4, 5):
        for j in range(-4, 5):
            e.append(way(n := n + 1, rect(i * 200 + 30, j * 200 + 30, 14, 10), {"building": "house"}))
            e.append(way(n := n + 1, rect(i * 200 + 120, j * 200 + 40, 12, 12), {"building": "yes"}))
    # A hospital (an area) and the points that matter.
    e.append(way(n := n + 1, rect(-700, 500, 120, 90), {"amenity": "hospital", "name": "Victoria Hospital", "building": "hospital"}))
    e.append(node(n := n + 1, 50, 50, {"amenity": "pharmacy", "name": "Boots"}))
    e.append(node(n := n + 1, -250, 160, {"amenity": "doctors", "name": "Lane Surgery"}))
    e.append(node(n := n + 1, 410, -410, {"amenity": "drinking_water"}))
    e.append(node(n := n + 1, 820, -480, {"natural": "spring", "name": "Mill Spring"}))
    e.append(node(n := n + 1, 600, 20, {"amenity": "fuel", "name": "Ouse Garage"}))
    e.append(node(n := n + 1, -60, 230, {"shop": "supermarket", "name": "Co-op"}))
    e.append(node(n := n + 1, 0, 30, {"place": "town", "name": "Millbrook"}))
    e.append(node(n := n + 1, -1500, 1200, {"place": "hamlet", "name": "Woodend"}))
    e.append(node(n := n + 1, 300, 300, {"amenity": "bench"}))  # nothing the map draws
    return {"version": 0.6, "elements": e}
