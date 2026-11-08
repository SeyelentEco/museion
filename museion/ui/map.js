// The Map tab: an offline map (an .mbtiles file in the library's maps
// folder, served tile by tile by museion/app.py) with the person's places
// from places.csv drawn on it. Drawn here with plain page elements: no map
// library, so there is nothing to fetch and nothing to break offline.

import { invoke } from './api.js';
import { $, el } from './dom.js';
import { onTab } from './tabs.js';

const TILE = 256;
const KINDS = ['water', 'food', 'shelter', 'medical', 'power', 'people', 'route', 'danger', 'other'];

let maps = [];
let places = [];
// What is on screen: the map (or null for a blank sheet), the zoom, and the
// centre in pixels of the whole world at that zoom.
const view = { map: null, z: 3, cx: 0, cy: 0 };
let adding = false;
let started = false;

// ── projection (Web Mercator, as map tiles use) ──

const world = (z) => TILE * 2 ** z;
const lonToX = (lon, z) => ((lon + 180) / 360) * world(z);
const latToY = (lat, z) => {
  const r = (Math.max(Math.min(lat, 85.05), -85.05) * Math.PI) / 180;
  return ((1 - Math.log(Math.tan(r) + 1 / Math.cos(r)) / Math.PI) / 2) * world(z);
};
const xToLon = (x, z) => (x / world(z)) * 360 - 180;
const yToLat = (y, z) => (Math.atan(Math.sinh(Math.PI * (1 - (2 * y) / world(z)))) * 180) / Math.PI;

function zoomRange() {
  if (!view.map) return [2, 18];
  // Three levels past the map's own, drawn from its closest tiles enlarged.
  return [Math.max(view.map.minzoom - 2, 0), view.map.maxzoom + 3];
}

function setCentre(lat, lon, z) {
  const [lo, hi] = zoomRange();
  view.z = Math.max(lo, Math.min(hi, Math.round(z)));
  view.cx = lonToX(lon, view.z);
  view.cy = latToY(lat, view.z);
}

/** The closest zoom at which the whole map fits on screen. */
function fitZoom(m) {
  const box = $('#map-view');
  if (!m.bounds) return m.center?.[2] ?? m.minzoom;
  const [w, south, e, north] = m.bounds;
  for (let z = m.maxzoom; z > m.minzoom; z--) {
    if (lonToX(e, z) - lonToX(w, z) <= box.clientWidth && latToY(south, z) - latToY(north, z) <= box.clientHeight) return z;
  }
  return m.minzoom;
}

function centre() {
  return { lat: yToLat(view.cy, view.z), lon: xToLon(view.cx, view.z) };
}

// ── drawing ──

function draw() {
  const box = $('#map-view');
  const w = box.clientWidth;
  const h = box.clientHeight;
  if (!w || !h) return;
  const left = view.cx - w / 2;
  const top = view.cy - h / 2;
  drawTiles(left, top, w, h);
  drawPlaces(left, top);
  const c = centre();
  $('#map-where').textContent = `${c.lat.toFixed(4)}, ${c.lon.toFixed(4)} · zoom ${view.z}`;
}

function drawTiles(left, top, w, h) {
  const layer = $('#map-tiles');
  const keep = new Set();
  if (view.map && !view.map.problem) {
    const tz = Math.max(view.map.minzoom, Math.min(view.z, view.map.maxzoom));
    const size = TILE * 2 ** (view.z - tz);
    const n = 2 ** tz;
    const x0 = Math.max(Math.floor(left / size), 0);
    const x1 = Math.min(Math.floor((left + w) / size), n - 1);
    const y0 = Math.max(Math.floor(top / size), 0);
    const y1 = Math.min(Math.floor((top + h) / size), n - 1);
    for (let x = x0; x <= x1; x++) {
      for (let y = y0; y <= y1; y++) {
        const key = `${view.map.id}/${tz}/${x}/${y}`;
        keep.add(key);
        let img = layer.querySelector(`img[data-key="${key}"]`);
        if (!img) {
          img = el('img');
          img.dataset.key = key;
          img.alt = '';
          img.draggable = false;
          // No tile there (the edge of the map) is not an error to show.
          img.addEventListener('error', () => (img.style.visibility = 'hidden'));
          img.src = `/tiles/${key}`;
          layer.append(img);
        }
        img.style.left = `${Math.round(x * size - left)}px`;
        img.style.top = `${Math.round(y * size - top)}px`;
        img.style.width = img.style.height = `${Math.ceil(size)}px`;
      }
    }
  }
  for (const img of [...layer.children]) if (!keep.has(img.dataset.key)) img.remove();
}

function drawPlaces(left, top) {
  const layer = $('#map-marks');
  layer.textContent = '';
  for (const p of places) {
    const m = el('div', `place-mark kind-${KINDS.includes(p.kind) ? p.kind : 'other'}`);
    m.style.left = `${Math.round(lonToX(p.lon, view.z) - left)}px`;
    m.style.top = `${Math.round(latToY(p.lat, view.z) - top)}px`;
    m.title = [p.name, p.kind, p.notes].filter(Boolean).join(' — ');
    m.append(el('span', 'pin'));
    m.append(el('span', 'label', p.name));
    layer.append(m);
  }
}

// ── the side panel ──

function listPlaces() {
  const list = $('#place-items');
  list.textContent = '';
  if (!places.length) list.append(el('li', 'empty-note', 'No places yet. Press Add a place, then click the map.'));
  for (const p of places) {
    const li = el('li');
    li.append(el('div', 't', p.name));
    li.append(el('div', 'meta', [p.kind, `${p.lat.toFixed(4)}, ${p.lon.toFixed(4)}`].filter(Boolean).join(' · ')));
    li.addEventListener('click', () => {
      setCentre(p.lat, p.lon, Math.max(view.z, view.map ? Math.min(view.map.maxzoom, 15) : 14));
      draw();
    });
    list.append(li);
  }
}

function chooseMap(id) {
  const c = view.map ? centre() : null;
  view.map = maps.find((m) => m.id === id) ?? null;
  $('#map-tiles').textContent = '';
  const note = $('#map-note');
  if (!view.map) {
    note.hidden = false;
    note.textContent = maps.length
      ? 'Showing your places on a blank sheet.'
      : 'There is no map of the area yet, so your places are shown on a blank sheet. Put an .mbtiles map in the maps folder (see docs/maps.md) and press Look again.';
  } else if (view.map.problem) {
    note.hidden = false;
    note.textContent = `${view.map.file} cannot be shown: ${view.map.problem}.`;
  } else {
    note.hidden = true;
  }
  $('#map-credit').textContent = view.map?.attribution ?? '';
  if (view.map?.center && !c) {
    setCentre(view.map.center[1], view.map.center[0], fitZoom(view.map));
  } else if (c) {
    setCentre(c.lat, c.lon, view.z);
  }
  draw();
}

async function refresh() {
  let data;
  try {
    [data, places] = await Promise.all([invoke('list_maps'), invoke('list_places')]);
  } catch (e) {
    $('#map-note').hidden = false;
    $('#map-note').textContent = String(e);
    return;
  }
  maps = data.maps;
  const pick = $('#map-pick');
  pick.textContent = '';
  for (const m of maps) {
    const o = el('option', undefined, m.name);
    o.value = m.id;
    pick.append(o);
  }
  const blank = el('option', undefined, 'Blank sheet');
  blank.value = '';
  pick.append(blank);
  pick.value = view.map?.id ?? maps[0]?.id ?? '';
  listPlaces();
  if (!started) {
    started = true;
    // First look: the map's own centre, else the places, else the world.
    if (places.length && !maps.length) setCentre(places[0].lat, places[0].lon, 12);
    else setCentre(20, 0, 2);
  }
  chooseMap(pick.value);
}

// ── moving about ──

function zoomAt(delta, px, py) {
  const box = $('#map-view');
  const [lo, hi] = zoomRange();
  const z = Math.max(lo, Math.min(hi, view.z + delta));
  if (z === view.z) return;
  const ox = px ?? box.clientWidth / 2;
  const oy = py ?? box.clientHeight / 2;
  // Keep the point under the pointer where it is.
  const wx = view.cx - box.clientWidth / 2 + ox;
  const wy = view.cy - box.clientHeight / 2 + oy;
  const f = 2 ** (z - view.z);
  view.cx = wx * f - ox + box.clientWidth / 2;
  view.cy = wy * f - oy + box.clientHeight / 2;
  view.z = z;
  draw();
}

function setAdding(on) {
  adding = on;
  $('#map-view').classList.toggle('adding', on);
  $('#map-add').textContent = on ? 'Click the map…' : 'Add a place';
}

function showForm(lat, lon) {
  const form = $('#place-form');
  form.hidden = false;
  form.dataset.lat = String(lat);
  form.dataset.lon = String(lon);
  $('#place-at').textContent = `${lat.toFixed(5)}, ${lon.toFixed(5)}`;
  $('#place-name').value = '';
  $('#place-notes').value = '';
  $('#place-name').focus();
}

function wire() {
  const box = $('#map-view');
  let drag = null;
  box.addEventListener('pointerdown', (e) => {
    drag = { x: e.clientX, y: e.clientY, cx: view.cx, cy: view.cy, moved: false };
    box.setPointerCapture(e.pointerId);
  });
  box.addEventListener('pointermove', (e) => {
    if (!drag) return;
    const dx = e.clientX - drag.x;
    const dy = e.clientY - drag.y;
    if (Math.abs(dx) + Math.abs(dy) > 3) drag.moved = true;
    view.cx = drag.cx - dx;
    view.cy = drag.cy - dy;
    draw();
  });
  box.addEventListener('pointerup', (e) => {
    const was = drag;
    drag = null;
    if (!was || was.moved || !adding) return;
    const r = box.getBoundingClientRect();
    const x = view.cx - r.width / 2 + (e.clientX - r.left);
    const y = view.cy - r.height / 2 + (e.clientY - r.top);
    setAdding(false);
    showForm(yToLat(y, view.z), xToLon(x, view.z));
  });
  box.addEventListener('wheel', (e) => {
    e.preventDefault();
    const r = box.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1 : -1, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  box.addEventListener('dblclick', (e) => {
    const r = box.getBoundingClientRect();
    zoomAt(1, e.clientX - r.left, e.clientY - r.top);
  });
  $('#map-in').addEventListener('click', () => zoomAt(1));
  $('#map-out').addEventListener('click', () => zoomAt(-1));
  $('#map-pick').addEventListener('change', (e) => chooseMap(e.target.value));
  $('#map-add').addEventListener('click', () => setAdding(!adding));
  $('#map-reload').addEventListener('click', () => void refresh());
  $('#map-folder').addEventListener('click', () => invoke('open_maps_folder').catch((e) => alert(String(e))));
  const kind = $('#place-kind');
  for (const k of KINDS) {
    const o = el('option', undefined, k);
    o.value = k;
    kind.append(o);
  }
  $('#place-cancel').addEventListener('click', () => ($('#place-form').hidden = true));
  $('#place-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const form = $('#place-form');
    try {
      places = await invoke('add_place', {
        name: $('#place-name').value, kind: kind.value, notes_text: $('#place-notes').value,
        lat: Number(form.dataset.lat), lon: Number(form.dataset.lon),
      });
      form.hidden = true;
      listPlaces();
      draw();
    } catch (err) {
      alert(String(err));
    }
  });
  new ResizeObserver(() => draw()).observe(box);
}

wire();
onTab('map', refresh);
