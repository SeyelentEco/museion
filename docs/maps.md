# Offline maps

The Map tab shows a map of your area with the places you mark on it, with no
internet. The map is an **.mbtiles** file in the library's `maps` folder; the
places are `places.csv` in your notes (see [notes.md](notes.md)).

Make the map **before** you need it: drawing one takes map data you can only
download while the internet works.

## What kind of map

An MBTiles file is a single SQLite database of map pictures ("tiles"),
zoom level by zoom level. Museion reads it with Python's own `sqlite3` and the
page draws the tiles itself (`museion/maps.py`, `museion/ui/map.js`), so there
is no map server or map library to break.

It must hold **raster** tiles (PNG, JPEG or WebP pictures). Vector MBTiles
(format `pbf`, as OpenMapTiles and Protomaps make) need a full map renderer;
Museion says so and does not show them.

Size grows four times with each zoom level. For one district, zoom 8 to 16 is
typically a few hundred MB; zoom 17 shows single houses and quadruples that.

## Making one with QGIS (free, works offline once installed)

1. Download OpenStreetMap data for your region while you still can: a
   `.osm.pbf` extract from <https://download.geofabrik.de/> (pick the smallest
   area that covers where you might walk or drive). Keep the file: it is the
   source for any map you make later.
2. In QGIS, add the extract (Layer > Add Layer > Add Vector Layer, choose the
   `.osm.pbf`; add its `lines`, `multipolygons` and `points` layers). Style them
   if you like: roads, water and buildings are what matter.
3. Zoom to the area to keep, then Processing > Toolbox > **Generate XYZ tiles
   (MBTiles)**. Set the extent to the map canvas, minimum zoom 8, maximum zoom
   15 or 16, tile format PNG, and the output file
   `<library>/maps/<your area>.mbtiles`.
4. In Museion, open the Map tab and press **Look again**.

QGIS can also turn your own scans or drone photos into tiles the same way
(add them as raster layers).

Any other tool that writes raster MBTiles works too (MOBAC, TileMill,
`gdal_translate -of MBTILES` for a single image). Respect the licence of the
map data: OpenStreetMap's needs the credit "© OpenStreetMap contributors",
which Museion shows from the file's `attribution`.

## Places

Press **Add a place**, click the map, name it and pick a kind (water, food,
shelter, medical, power, people, route, danger, other). It is added as a row of
`places.csv` in your notes, which you can also edit in the Notes tab or any
text editor (`name,kind,lat,lon,notes`, latitude and longitude in decimal
degrees). Because it is a note, the librarian and the agents find it when they
search: "where is the nearest clean water?" can cite your own spring.
