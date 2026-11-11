# Offline maps

The Map tab shows a map of your area with the places you mark on it, with no
internet. Museion can make the map itself while the internet works. The map is an **.mbtiles** file in the library's `maps` folder; the
places are `places.csv` in your notes (see [notes.md](notes.md)).

Make the map **before** you need it: making one takes map data you can only
download while the internet works.

## Make a map in Museion

In the Map tab, open **Make a map...**, type a town (or a latitude and
longitude such as `50.87, 0.01`) and press **Find**, pick the place it found,
pick how far across (5, 10, 25 or 50 km), and press **Make the map**. Museion
downloads what OpenStreetMap has for that area and makes a map that works with
no internet: roads and paths with their names, rail, rivers and lakes, woods
and fields, buildings, place names, and the signs that matter in an emergency
(hospitals, doctors, pharmacies, drinking water and springs, fuel, police,
fire stations, shelters, food shops). Click a sign to keep it as one of your
places.

From the command line:

    python -m museion make-map "Lewes, England" --km 10
    python -m museion make-map "50.87, 0.01" --km 25 --no-buildings
    python -m museion make-map --again lewes-10-km      # no internet needed

- **Where the data comes from.** Places are found with OpenStreetMap's
  Nominatim search (only when you press Find, at most once a second). The map
  data comes from the Overpass API (`overpass-api.de`, then
  `overpass.kumi.systems` if that one is busy), in pieces of about 10 by 10 km.
  Both are free services run by volunteers: make the maps you need, not a whole
  country. Set `MUSEION_OVERPASS` or `MUSEION_NOMINATIM` to use other servers.
- **The download is kept.** Each piece is saved in
  `<library>/maps/sources/<map>/` (gzip-compressed Overpass JSON, with
  `area.json` saying what it is). If the internet drops halfway, press Make the
  map again: the pieces already there are not downloaded again. The map can
  also be made again from them later with no internet (`--again`), for
  example after Museion learns to draw something new.
- **Buildings** make the download and the map several times bigger. They are
  on for 5 and 10 km, off for 25 and 50 km; change it with the tick box.
- **How long and how big.** Measured for Lewes (a small English town in
  farmland) on a home connection:

  | Size | Pieces | Time | Map | Download kept |
  | --- | --- | --- | --- | --- |
  | 5 km, buildings | 1 | about 1 min | 0.3 MB | |
  | 10 km, buildings | 2 | about 70 s | 0.7 MB | 2.2 MB |
  | 25 km, no buildings | 12 | about 7.5 min | 3.6 MB | 10.6 MB |

  Nearly all of the time is downloading: making a map again from the kept
  download took 2 seconds. A 50 km map is about 40 pieces, so expect half an
  hour or more. A city takes longer and makes a bigger file than this.
- **Not drawn yet:** the sea is not filled in (coastlines show as a line),
  there are no contour lines or hill shading, and house numbers are left out.

Museion makes the map itself (`museion/osm.py` downloads, `mapmaker.py` cuts
the data into tiles, `mvt.py` writes them) and the page draws it
(`ui/mvt.js` reads the tiles, `ui/mapdraw.js` draws them on a canvas). No map
library or map server is involved, so there is nothing more to break or update.

## What kind of map

An MBTiles file is a single SQLite database of map tiles, zoom level by zoom
level. Museion reads it with Python's own `sqlite3` (`museion/maps.py`) and the
page draws the tiles itself (`museion/ui/map.js`).

Two kinds work:

- **Vector** tiles (format `pbf`): the shapes and names themselves, drawn by
  the page. This is what Make a map writes: zoom 8 to 14, drawn up to zoom 19.
  Other vector MBTiles that use the OpenMapTiles layer names (OpenMapTiles,
  MapTiler, Planetiler) should draw with the same style, but so far only maps
  made by Museion have been tried.
- **Raster** tiles (PNG, JPEG or WebP pictures), as QGIS and most other tools
  make. Size grows four times with each zoom level: for one district, zoom 8
  to 16 is typically a few hundred MB; zoom 17 shows single houses and
  quadruples that.

## Making one with QGIS (free, works offline once installed)

For an area bigger than Make a map allows, a map styled your own way, or your
own scans and drone photos:

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
