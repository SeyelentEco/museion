"""Vector tiles, written and read by hand (museion/mvt.py).

OFFICIAL was made with the reference library, mapbox-vector-tile 2.2.0
(pip install mapbox-vector-tile in a scratch venv; not a dependency):

    mapbox_vector_tile.encode([
      {"name": "poi", "features": [{"geometry": "POINT (100 200)",
        "properties": {"class": "pharmacy", "name": "Boots", "rank": 3}}]},
      {"name": "transportation", "features": [{"geometry": "LINESTRING (0 0, 1000 0, 1000 1000)",
        "properties": {"class": "primary", "ref": "A26"}}]},
      {"name": "water", "features": [{"geometry": "POLYGON ((0 0, 0 2000, 2000 2000, 2000 0, 0 0),
        (500 500, 1500 500, 1500 1500, 500 1500, 500 500))", "properties": {"class": "lake", "deep": True, "depth": -2}}]},
    ], default_options={"y_coord_down": True, "quantize_bounds": None})

The same library read the tiles of a map made by mapmaker.py back
correctly (layers, classes, names, polygons with their holes); that check
is not repeated here, as it needs the library."""

import unittest

from museion import mvt

OFFICIAL = (
    "1a490a03706f69121112060000010102021801220509c80190031a05636c6173731a046e616d651a0472616e6b220a0a08706861726d6163"
    "7922070a05426f6f74732202200328802078021a490a0e7472616e73706f72746174696f6e12141204000001011802220a09000012d00f00"
    "00d00f1a05636c6173731a0372656622090a077072696d61727922050a0341323628802078021a650a057761746572122a12060000010102"
    "021803221e0900001aa01f0000a01f9f1f000f09e807b7171a00d00fd00f0000cf0f0f1a05636c6173731a04646565701a05646570746822"
    "060a046c616b6522023801220b20feffffffffffffffff012880207802"
)
EXPECTED = {
    "poi": [mvt.Feature(mvt.POINT, [[(100, 200)]], {"class": "pharmacy", "name": "Boots", "rank": 3})],
    "transportation": [mvt.Feature(mvt.LINESTRING, [[(0, 0), (1000, 0), (1000, 1000)]],
                                   {"class": "primary", "ref": "A26"})],
    "water": [mvt.Feature(mvt.POLYGON, [[(0, 0), (2000, 0), (2000, 2000), (0, 2000)],
                                        [(500, 500), (500, 1500), (1500, 1500), (1500, 500)]],
                          {"class": "lake", "deep": True, "depth": -2})],
}


class MvtTest(unittest.TestCase):
    def test_reads_a_tile_the_reference_library_wrote(self):
        self.assertEqual(mvt.decode_tile(bytes.fromhex(OFFICIAL)), EXPECTED)

    def test_writes_what_it_reads(self):
        tile = mvt.encode_tile(EXPECTED)
        self.assertEqual(mvt.decode_tile(tile), EXPECTED)

    def test_rings_turn_the_right_way(self):
        outer, hole = EXPECTED["water"][0].parts
        self.assertGreater(mvt.ring_area(outer), 0)  # clockwise on screen, y down
        self.assertLess(mvt.ring_area(hole), 0)

    def test_empty_layers_and_properties_are_left_out(self):
        tile = mvt.encode_tile({"poi": [], "place": [mvt.Feature(mvt.POINT, [[(1, 2), (3, 4)]], {"name": "", "x": 1.5})]})
        self.assertEqual(mvt.decode_tile(tile), {"place": [mvt.Feature(mvt.POINT, [[(1, 2), (3, 4)]], {"x": 1.5})]})


if __name__ == "__main__":
    unittest.main()
