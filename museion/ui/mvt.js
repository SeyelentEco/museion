// Reading vector map tiles (the Mapbox Vector Tile format, version 2), as
// museion/mvt.py writes them and OpenMapTiles maps hold them. A tile is a
// protobuf message: layers of features, each with properties and a
// geometry of MoveTo / LineTo / ClosePath commands in tile units (0 to the
// layer's extent, usually 4096, y down). The format is described at the top
// of museion/mvt.py.

const utf8 = new TextDecoder();

class Reader {
  constructor(bytes, start = 0, end = bytes.length) {
    this.b = bytes;
    this.i = start;
    this.end = end;
  }

  more() {
    return this.i < this.end;
  }

  varint() {
    // Up to 2^53 exactly: enough for anything a tile holds.
    let n = 0;
    let mul = 1;
    for (;;) {
      const b = this.b[this.i++];
      n += (b & 0x7f) * mul;
      if (b < 0x80) return n;
      mul *= 128;
      if (this.i >= this.end) throw new Error('truncated tile');
    }
  }

  // The bounds of a length-delimited field, which is skipped over.
  sub() {
    const len = this.varint();
    const r = new Reader(this.b, this.i, this.i + len);
    this.i += len;
    return r;
  }

  skip(wire) {
    if (wire === 0) this.varint();
    else if (wire === 1) this.i += 8;
    else if (wire === 2) this.i += this.varint();
    else if (wire === 5) this.i += 4;
    else throw new Error(`wire type ${wire}`);
  }

  packed() {
    const r = this.sub();
    const out = [];
    while (r.more()) out.push(r.varint());
    return out;
  }

  string() {
    const r = this.sub();
    return utf8.decode(this.b.subarray(r.i, r.end));
  }
}

const unzigzag = (n) => (n % 2 ? -(n + 1) / 2 : n / 2);

function readValue(r) {
  let value = null;
  while (r.more()) {
    const key = r.varint();
    const field = Math.floor(key / 8);
    const view = new DataView(r.b.buffer, r.b.byteOffset);
    if (field === 1) value = r.string();
    else if (field === 2) {
      value = view.getFloat32(r.i, true);
      r.i += 4;
    } else if (field === 3) {
      value = view.getFloat64(r.i, true);
      r.i += 8;
    } else if (field === 4 || field === 5) value = r.varint();
    else if (field === 6) value = unzigzag(r.varint());
    else if (field === 7) value = Boolean(r.varint());
    else r.skip(key & 7);
  }
  return value;
}

/** A feature's geometry: for points one flat [x, y, x, y ...] list, for
 * lines and polygons one per line or ring. */
function readGeometry(numbers, type) {
  const parts = [];
  let x = 0;
  let y = 0;
  let current = null;
  for (let i = 0; i < numbers.length; ) {
    const cmd = numbers[i] & 7;
    const count = Math.floor(numbers[i] / 8);
    i++;
    if (cmd === 7) continue; // ClosePath: the ring ends where it began
    for (let k = 0; k < count; k++) {
      x += unzigzag(numbers[i++]);
      y += unzigzag(numbers[i++]);
      if (cmd === 1 && (type !== 1 || !current)) {
        current = [];
        parts.push(current);
      }
      current.push(x, y);
    }
  }
  return parts;
}

function readFeature(r, keys, values) {
  const f = { type: 0, props: {}, parts: [] };
  let geometry = [];
  while (r.more()) {
    const key = r.varint();
    const field = Math.floor(key / 8);
    if (field === 2) {
      const tags = r.packed();
      for (let j = 0; j + 1 < tags.length; j += 2) f.props[keys[tags[j]]] = values[tags[j + 1]];
    } else if (field === 3) f.type = r.varint();
    else if (field === 4) geometry = r.packed();
    else r.skip(key & 7);
  }
  f.parts = readGeometry(geometry, f.type);
  return f;
}

function readLayer(r) {
  const layer = { name: '', extent: 4096, features: [] };
  const keys = [];
  const values = [];
  const raw = [];
  while (r.more()) {
    const key = r.varint();
    const field = Math.floor(key / 8);
    if (field === 1) layer.name = r.string();
    else if (field === 2) raw.push(r.sub());
    else if (field === 3) keys.push(r.string());
    else if (field === 4) values.push(readValue(r.sub()));
    else if (field === 5) layer.extent = r.varint();
    else r.skip(key & 7);
  }
  // Keys and values may come after the features that use them.
  layer.features = raw.map((fr) => readFeature(fr, keys, values));
  return layer;
}

/** A tile's layers, by name: { extent, features: [{ type, props, parts }] }.
 * type is 1 point, 2 line, 3 polygon. */
export function decodeTile(buffer) {
  const r = new Reader(new Uint8Array(buffer));
  const layers = {};
  while (r.more()) {
    const key = r.varint();
    if (Math.floor(key / 8) === 3) {
      const layer = readLayer(r.sub());
      layers[layer.name] = layer;
    } else r.skip(key & 7);
  }
  return layers;
}
