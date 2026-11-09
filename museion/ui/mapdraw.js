// Drawing vector map tiles (decoded by mvt.js) on a canvas: land cover,
// water, buildings, roads and rail, then the names and the signs for the
// places that matter (hospital, pharmacy, drinking water ...). The layers
// and classes are those of the OpenMapTiles schema, which the maps Museion
// makes use too (museion/mapmaker.py), so maps made elsewhere draw the same.
//
// A style rule says which features it draws (layer, classes), how (fill or
// line), its colour, and its width in pixels at a few zoom levels. Rules are
// drawn in order, bottom first; each tile's shapes for a rule are made once
// (a Path2D in tile units) and drawn scaled at every zoom.

const LAND = '#f2efe9';
const WATER = '#aad3df';

/** A width in pixels at zoom z, from stops [[z, px], ...]: geometric
 * between stops, and doubling per zoom past the last (as the ground does). */
function widthAt(stops, z) {
  if (z <= stops[0][0]) return stops[0][1] * 2 ** (z - stops[0][0]);
  for (let i = 1; i < stops.length; i++) {
    const [z1, w1] = stops[i - 1];
    const [z2, w2] = stops[i];
    if (z <= z2) return w1 * (w2 / w1) ** ((z - z1) / (z2 - z1));
  }
  const [zl, wl] = stops[stops.length - 1];
  return wl * 2 ** (z - zl);
}

const is = (...classes) => (p) => classes.includes(p.class);
const fill = (layer, color, when = () => true, minz = 0) => ({ layer, kind: 'fill', color, when, minz });
const line = (layer, color, stops, when = () => true, opts = {}) => ({ layer, kind: 'line', color, stops, when, minz: 0, ...opts });

const ROADS = [
  // class, fill, casing, width stops (the fill), first zoom
  ['service', '#ffffff', '#c8c0b6', [[13, 0.8], [14, 1.5], [16, 5], [18, 14]], 13],
  ['minor', '#ffffff', '#c8c0b6', [[12, 1], [13, 1.6], [14, 3.2], [16, 9], [18, 26]], 12],
  ['tertiary', '#ffffff', '#b5aca1', [[11, 1], [12, 2], [14, 5], [16, 11], [18, 30]], 11],
  ['secondary', '#f7fabf', '#8d9a26', [[10, 1], [12, 2.6], [14, 6], [16, 12], [18, 32]], 10],
  ['primary', '#fcd6a4', '#b77d1a', [[8, 0.8], [10, 1.6], [12, 3.2], [14, 7], [16, 14], [18, 36]], 8],
  ['trunk', '#f9b29c', '#c8603d', [[6, 0.8], [8, 1.2], [10, 2], [12, 3.6], [14, 8], [16, 16], [18, 40]], 6],
  ['motorway', '#e892a2', '#c24e6b', [[6, 0.8], [8, 1.4], [10, 2.2], [12, 4], [14, 8], [16, 16], [18, 40]], 6],
];
const ROAD_CLASS = (name) => (p) => p.class === name || p.class === `${name}_link` || (name === 'minor' && p.class === 'residential');

export const STYLE = [
  fill('landcover', '#eef0d5', is('farmland')),
  fill('landcover', '#d8eacb', is('grass')),
  fill('landcover', '#d5e3c0', is('scrub')),
  fill('landcover', '#d6e6d8', is('wetland')),
  fill('landcover', '#f2e8c4', is('sand')),
  fill('landcover', '#e4dfd8', is('rock')),
  fill('landcover', '#f3f8fc', is('ice', 'glacier')),
  fill('landuse', '#e8e3dc', is('residential', 'suburb', 'neighbourhood')),
  fill('landuse', '#e9e0ec', is('industrial', 'railway', 'quarry', 'construction')),
  fill('landuse', '#f1dfda', is('commercial', 'retail')),
  fill('landuse', '#cfdcc8', is('cemetery')),
  fill('landuse', '#f2dada', is('military')),
  fill('landuse', '#f6e6e6', is('hospital')),
  fill('landuse', '#f5efcd', is('school', 'university', 'college', 'kindergarten')),
  fill('landcover', '#c5dcae', is('wood', 'forest')),
  fill('park', '#cdebb0', (p) => p.class !== 'pitch'),
  fill('park', '#b9e2a5', is('pitch')),
  fill('water', WATER),
  line('waterway', '#7fb0cc', [[8, 0.8], [14, 1.5], [18, 2.5]], is('coastline')),
  line('waterway', WATER, [[12, 0.6], [14, 1.2], [16, 2], [18, 4]], is('stream', 'ditch', 'drain', 'brook')),
  line('waterway', WATER, [[10, 0.8], [14, 3], [18, 12]], is('canal')),
  line('waterway', WATER, [[8, 0.8], [10, 1.2], [12, 2], [14, 4], [16, 8], [18, 16]], is('river')),
  fill('building', '#d9d0c9', () => true, 13),
  line('building', '#c4b6ab', [[15, 0.6], [18, 1]], () => true, { minz: 15 }),
  line('transportation', '#a87f4a', [[13, 0.8], [16, 1.6], [18, 2.5]], is('track'), { minz: 13, dash: [5, 3] }),
  line('transportation', '#e27c6c', [[14, 0.8], [16, 1.4], [18, 2]], is('path'), { minz: 14, dash: [3, 2.5] }),
  ...ROADS.map(([cls, , casing, stops, minz]) =>
    line('transportation', casing, stops, ROAD_CLASS(cls), { minz, casing: true, cap: 'round' })),
  ...ROADS.map(([cls, color, , stops, minz]) =>
    line('transportation', color, stops, ROAD_CLASS(cls), { minz, cap: 'round' })),
  line('transportation', '#999999', [[12, 0.8], [16, 1.5], [18, 2.5]], is('transit'), { minz: 12 }),
  line('transportation', '#8a8a8a', [[9, 0.7], [12, 1.4], [14, 2.4], [18, 4]], is('rail'), { minz: 9 }),
  line('transportation', '#ffffff', [[13, 0.8], [14, 1.2], [18, 2]], is('rail'), { minz: 13, dash: [6, 6] }),
];

// Signs for points of interest: a coloured disc with a letter, the first
// zoom it shows at, and the word for one that has no name.
export const POI = {
  hospital: ['#d8483d', 'H', 12, 'Hospital'],
  clinic: ['#d8483d', '+', 14, 'Clinic'],
  doctors: ['#d8483d', '+', 14, 'Doctor'],
  pharmacy: ['#2f9a4b', '+', 14, 'Pharmacy'],
  drinking_water: ['#2f7fd0', 'W', 14, 'Drinking water'],
  water_tank: ['#2f7fd0', 'W', 15, 'Water tank'],
  water_tower: ['#2f7fd0', 'T', 15, 'Water tower'],
  fuel: ['#7a5c45', 'F', 14, 'Fuel'],
  police: ['#3a5a9b', 'P', 14, 'Police'],
  fire_station: ['#c0392b', 'F', 14, 'Fire station'],
  shelter: ['#8e6c3a', 'S', 15, 'Shelter'],
  campsite: ['#5a7d3a', 'C', 15, 'Campsite'],
  food: ['#b26b00', '$', 15, 'Food shop'],
  hardware: ['#6b6b6b', 'T', 16, 'Hardware'],
  school: ['#8b6914', 'S', 15, 'School'],
  town_hall: ['#555555', 'T', 15, 'Town hall'],
  community_centre: ['#555555', 'C', 16, 'Community centre'],
  place_of_worship: ['#555555', '†', 16, 'Place of worship'],
  defibrillator: ['#2f9a4b', 'D', 16, 'Defibrillator'],
  emergency_phone: ['#c0392b', 'T', 16, 'Emergency phone'],
  peak: ['#7a5c45', '▲', 13, 'Peak'],
};
const POI_OTHER = ['#8a8a8a', '•', 16, ''];

const PLACE = {
  // size, bold, shown from zoom, until zoom
  city: [17, true, 6, 15], town: [15, true, 9, 16], village: [13, true, 11, 18], suburb: [13, false, 12, 16],
  quarter: [12, false, 13, 17], hamlet: [12, false, 12, 19], neighbourhood: [11, false, 14, 19],
  locality: [11, false, 14, 19], isolated_dwelling: [11, false, 15, 19], farm: [11, false, 15, 19],
};

function addShape(p, feature) {
  for (const part of feature.parts) {
    p.moveTo(part[0], part[1]);
    for (let i = 2; i < part.length; i += 2) p.lineTo(part[i], part[i + 1]);
    if (feature.type === 3) p.closePath();
  }
}

/** The shapes of one tile for one rule, made the first time they are drawn. */
function shapes(tile, index, rule) {
  tile.paths ??= [];
  if (tile.paths[index] !== undefined) return tile.paths[index];
  const layer = tile.layers[rule.layer];
  let out = null;
  if (layer) {
    const wanted = rule.kind === 'fill' ? 3 : null;
    const p = new Path2D();
    let any = false;
    for (const f of layer.features) {
      if (wanted ? f.type !== 3 : f.type === 1) continue;
      if (!rule.when(f.props)) continue;
      addShape(p, f);
      any = true;
    }
    out = any ? p : null;
  }
  tile.paths[index] = out;
  return out;
}

/**
 * Draw the tiles: each { layers, x, y, size } with its place and size on
 * screen in CSS pixels. z is the zoom on screen (it may be past the tiles').
 */
export function drawMap(ctx, tiles, z, dpr) {
  const { width, height } = ctx.canvas;
  ctx.setTransform(1, 0, 0, 1, 0, 0);
  ctx.fillStyle = LAND;
  ctx.fillRect(0, 0, width, height);
  for (const t of tiles) {
    const extent = Object.values(t.layers)[0]?.extent ?? 4096;
    const scale = t.size / extent;
    ctx.save();
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.beginPath();
    ctx.rect(t.x, t.y, t.size, t.size);
    ctx.clip();
    ctx.setTransform(dpr * scale, 0, 0, dpr * scale, dpr * t.x, dpr * t.y);
    ctx.lineJoin = 'round';
    STYLE.forEach((rule, i) => {
      if (z < rule.minz) return;
      const path = shapes(t, i, rule);
      if (!path) return;
      if (rule.kind === 'fill') {
        ctx.fillStyle = rule.color;
        ctx.fill(path, 'evenodd');
        return;
      }
      let w = widthAt(rule.stops, z);
      if (rule.casing) w += z >= 13 ? 2 : z >= 11 ? 1 : 0.5;
      ctx.strokeStyle = rule.color;
      ctx.lineWidth = w / scale;
      ctx.lineCap = rule.cap ?? 'butt';
      ctx.setLineDash(rule.dash ? rule.dash.map((d) => (d * Math.max(w, 1)) / scale) : []);
      ctx.stroke(path);
    });
    ctx.restore();
  }
  ctx.setLineDash([]);
}

// ── names and signs ──

function fits(box, placed) {
  for (const b of placed) {
    if (box[0] < b[2] && box[2] > b[0] && box[1] < b[3] && box[3] > b[1]) return false;
  }
  return true;
}

function halo(ctx, text, x, y) {
  ctx.strokeText(text, x, y);
  ctx.fillText(text, x, y);
}

function label(f) {
  return f.props.name ?? f.props['name:latin'] ?? f.props.name_en ?? '';
}

/** Where along a line its name goes: the middle of its longest stretch on
 * screen, if that is long enough for the name. */
function alongLine(f, t, scale, wide) {
  let best = null;
  for (const part of f.parts) {
    for (let i = 2; i < part.length; i += 2) {
      const x1 = t.x + part[i - 2] * scale;
      const y1 = t.y + part[i - 1] * scale;
      const x2 = t.x + part[i] * scale;
      const y2 = t.y + part[i + 1] * scale;
      const len = Math.hypot(x2 - x1, y2 - y1);
      if (len > wide && (!best || len > best.len)) best = { len, x: (x1 + x2) / 2, y: (y1 + y2) / 2, a: Math.atan2(y2 - y1, x2 - x1) };
    }
  }
  if (best && (best.a > Math.PI / 2 || best.a < -Math.PI / 2)) best.a += Math.PI;
  return best;
}

/** Names and signs over the whole view, the most important first, none on
 * top of another. Returns the signs drawn, for clicking on. */
export function drawLabels(ctx, tiles, z, dpr, viewW, viewH) {
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.textAlign = 'center';
  ctx.textBaseline = 'middle';
  ctx.lineJoin = 'round';
  const placed = [];
  const signs = [];
  const seen = new Map(); // a road's name: where it was written
  const inView = (x, y) => x > -50 && y > -50 && x < viewW + 50 && y < viewH + 50;
  const items = [];
  for (const t of tiles) {
    const extent = Object.values(t.layers)[0]?.extent ?? 4096;
    const scale = t.size / extent;
    const at = (f) => [t.x + f.parts[0][0] * scale, t.y + f.parts[0][1] * scale];
    for (const f of t.layers.place?.features ?? []) {
      const style = PLACE[f.props.class];
      const name = label(f);
      if (!style || !name || z < style[2] || z > style[3]) continue;
      // Close in, the signs (a pharmacy, a spring) matter more than the
      // name of the town the person is already looking at.
      const rank = 10 - (f.props.rank ?? 5) - (z >= 15 ? 4 : 0);
      items.push({ kind: 'place', rank, name, style, xy: at(f) });
    }
    for (const f of t.layers.poi?.features ?? []) {
      const sign = POI[f.props.class] ?? POI_OTHER;
      if (z < sign[2]) continue;
      items.push({ kind: 'poi', rank: f.props.class === 'hospital' ? 8 : POI[f.props.class] ? 6 : 1, name: label(f), sign, xy: at(f), props: f.props });
    }
    for (const lname of ['water_name', 'water']) {
      for (const f of t.layers[lname]?.features ?? []) {
        const name = label(f);
        if (!name || z < 13) continue;
        if (f.type === 1) items.push({ kind: 'water', rank: 3, name, xy: at(f) });
      }
    }
    for (const lname of ['transportation_name', 'transportation', 'waterway']) {
      for (const f of t.layers[lname]?.features ?? []) {
        const name = label(f);
        if (!name || f.type !== 2) continue;
        const major = ['motorway', 'trunk', 'primary', 'secondary', 'river'].includes(f.props.class);
        if (z < (major ? 13 : 15)) continue;
        items.push({ kind: lname === 'waterway' ? 'river' : 'road', rank: major ? 4 : 2, name, f, t, scale });
      }
    }
  }
  items.sort((a, b) => b.rank - a.rank);
  for (const it of items) {
    if (it.kind === 'place' || it.kind === 'water') {
      const [x, y] = it.xy;
      if (!inView(x, y)) continue;
      const size = it.kind === 'place' ? it.style[0] : 12;
      ctx.font = `${it.kind === 'place' && it.style[1] ? '600 ' : it.kind === 'water' ? 'italic ' : ''}${size}px system-ui, sans-serif`;
      const w = ctx.measureText(it.name).width;
      const box = [x - w / 2 - 3, y - size / 2 - 2, x + w / 2 + 3, y + size / 2 + 2];
      if (!fits(box, placed)) continue;
      placed.push(box);
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(255,255,255,0.9)';
      ctx.fillStyle = it.kind === 'water' ? '#3f6f8f' : '#2b2b2b';
      halo(ctx, it.name, x, y);
    } else if (it.kind === 'poi') {
      const [x, y] = it.xy;
      if (!inView(x, y)) continue;
      const r = 7;
      const box = [x - r - 1, y - r - 1, x + r + 1, y + r + 1];
      if (!fits(box, placed)) continue;
      placed.push(box);
      const [color, letter, , word] = it.sign;
      ctx.beginPath();
      ctx.arc(x, y, r, 0, Math.PI * 2);
      ctx.fillStyle = color;
      ctx.fill();
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = '#ffffff';
      ctx.stroke();
      ctx.font = '700 9px system-ui, sans-serif';
      ctx.fillStyle = '#ffffff';
      ctx.fillText(letter, x, y + 0.5);
      signs.push({ x, y, r, name: it.name || word, kind: word || it.props.class, props: it.props });
      const text = it.name || (z >= 17 ? word : '');
      if (text && z >= (it.rank >= 6 ? 15 : 16)) {
        ctx.font = '11px system-ui, sans-serif';
        const w = ctx.measureText(text).width;
        const tbox = [x - w / 2 - 2, y + r + 1, x + w / 2 + 2, y + r + 14];
        if (fits(tbox, placed)) {
          placed.push(tbox);
          ctx.lineWidth = 3;
          ctx.strokeStyle = 'rgba(255,255,255,0.9)';
          ctx.fillStyle = color;
          halo(ctx, text, x, y + r + 8);
        }
      }
    } else {
      ctx.font = `${it.kind === 'river' ? 'italic ' : ''}${it.rank >= 4 ? 12 : 11}px system-ui, sans-serif`;
      const w = ctx.measureText(it.name).width;
      const spot = alongLine(it.f, it.t, it.scale, w + 16);
      if (!spot || !inView(spot.x, spot.y)) continue;
      const near = seen.get(it.name) ?? [];
      if (near.some(([x, y]) => Math.hypot(x - spot.x, y - spot.y) < 350)) continue;
      const c = Math.abs(Math.cos(spot.a));
      const s = Math.abs(Math.sin(spot.a));
      const hw = (w / 2) * c + 7 * s;
      const hh = (w / 2) * s + 7 * c;
      const box = [spot.x - hw, spot.y - hh, spot.x + hw, spot.y + hh];
      if (!fits(box, placed)) continue;
      placed.push(box);
      near.push([spot.x, spot.y]);
      seen.set(it.name, near);
      ctx.save();
      ctx.translate(spot.x, spot.y);
      ctx.rotate(spot.a);
      ctx.lineWidth = 3;
      ctx.strokeStyle = 'rgba(255,255,255,0.9)';
      ctx.fillStyle = it.kind === 'river' ? '#3f6f8f' : '#444444';
      halo(ctx, it.name, 0, 0);
      ctx.restore();
    }
  }
  return signs;
}
