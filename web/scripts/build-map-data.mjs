#!/usr/bin/env node
// Builds the static map data in public/map/ from free, open sources.
// The output is committed, so CI and the site never fetch these at run time.
//
//   Natural Earth 1:50m admin-0 countries (public domain) → countries.geojson, centroids.json ([lon, lat, name])
//   NGA World Port Index, Pub 150 (US Government work, public domain) → ports.json
//   UNECE UN/LOCODE code list (free to use) → the ports' display names in ports.json
//   Eurostat SeaRoute's MARNET maritime network, 100 km resolution (EUPL-1.2) → lanes.json
//
// Usage: node scripts/build-map-data.mjs [--wpi path/to/UpdatedPub150.csv] [--ne path/to/ne_50m_admin_0_countries.geojson]
//                                        [--locode path/to/code-list.csv] [--marnet path/to/marnet_plus_100km.gpkg]
//        node scripts/build-map-data.mjs --port-names-only [--locode path/to/code-list.csv]
//        node scripts/build-map-data.mjs --lanes-only [--marnet path/to/marnet_plus_100km.gpkg]
// Without the flags it downloads them from the URLs below. Needs `npx` (mapshaper 0.6), and
// Node 22.13 or later for the lanes (it reads MARNET's GeoPackage with node:sqlite).
// --port-names-only rewrites just the UN/LOCODE names in the committed ports.json.
// --lanes-only rebuilds just lanes.json (also after a change to engine/data/chokepoints.json).
// After the full build or --port-names-only, run `python engine/data/build_places.py` so the
// API's port names (engine/data/ports.tsv) follow (tests/test_places.py checks that they agree).
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { gzipSync } from 'node:zlib';

const NE_URL =
  'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson';
// Mirror of msi.nga.mil's UpdatedPub150.csv (the NGA link has no stable machine URL).
const WPI_URL =
  'https://raw.githubusercontent.com/CienciaDeDatosEspacial/dataSets/main/WORLD/UpdatedPub150.csv';

// The UN/LOCODE code list as CSV (UNECE's release, republished unchanged by the Frictionless
// Data "datasets" project).
const LOCODE_URL = 'https://raw.githubusercontent.com/datasets/un-locode/main/data/code-list.csv';

const out = join(dirname(fileURLToPath(import.meta.url)), '..', 'public', 'map');
const tmp = mkdtempSync(join(tmpdir(), 'rp-map-'));
const arg = (name) => {
  const i = process.argv.indexOf(name);
  return i > 0 ? process.argv[i + 1] : undefined;
};

async function fetchTo(url, path) {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: HTTP ${res.status}`);
  writeFileSync(path, Buffer.from(await res.arrayBuffer()));
  return path;
}

// Country code as the API uses it (ISO 3166-1 alpha-3). Natural Earth's ISO_A3 is -99 for a
// few places (France, Norway, Kosovo...); ISO_A3_EH fills most of those in.
function iso3(p) {
  for (const k of ['ISO_A3', 'ISO_A3_EH', 'ADM0_A3']) {
    const v = p[k];
    if (typeof v === 'string' && /^[A-Z]{3}$/.test(v)) return v;
  }
  return null;
}

// Minimal RFC 4180 CSV parser (quoted fields, doubled quotes, newlines in quotes).
function parseCsv(text) {
  const rows = [];
  let row = [];
  let field = '';
  let quoted = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (quoted) {
      if (c === '"' && text[i + 1] === '"') { field += '"'; i++; }
      else if (c === '"') quoted = false;
      else field += c;
    } else if (c === '"') quoted = true;
    else if (c === ',') { row.push(field); field = ''; }
    else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(field); rows.push(row); row = []; field = '';
    } else field += c;
  }
  if (field || row.length) { row.push(field); rows.push(row); }
  return rows;
}

const round = (n, d) => Math.round(n * 10 ** d) / 10 ** d;

// --- port display names ------------------------------------------------------------------
// ports.json is UN/LOCODE → [lon, lat, WPI name] or [lon, lat, WPI name, UN/LOCODE name]: the
// fourth element only when the UN/LOCODE name differs. The raw WPI name stays for matching;
// the dashboard shows cleanPortName(UN/LOCODE name ?? WPI name) (src/map/geo.ts).
async function addPortNames(ports) {
  const path = arg('--locode') ?? (await fetchTo(LOCODE_URL, join(tmp, 'code-list.csv')));
  const [head, ...lines] = parseCsv(readFileSync(path, 'utf8').replace(/^\uFEFF/, ''));
  const [iChange, iCountry, iLocation, iName] = ['Change', 'Country', 'Location', 'Name'].map((c) => {
    const i = head.indexOf(c);
    if (i < 0) throw new Error(`UN/LOCODE column missing: ${c}`);
    return i;
  });
  const names = {};
  for (const r of lines) {
    if ((r[iChange] ?? '').trim() === 'X') continue; // marked for deletion
    const code = `${r[iCountry] ?? ''}${r[iLocation] ?? ''}`.trim();
    if (/^[A-Z]{2}[A-Z2-9]{3}$/.test(code) && r[iName]?.trim() && !names[code]) names[code] = r[iName].trim();
  }
  let named = 0;
  for (const [code, p] of Object.entries(ports)) {
    p.length = 3;
    if (names[code] && names[code] !== p[2]) {
      p.push(names[code]);
      named++;
    }
  }
  console.log(`ports: ${named} UN/LOCODE names that differ from the WPI name`);
}

// --- sea lanes ---------------------------------------------------------------------------
// lanes.json is the network the dashboard draws declared routes on (src/map/lanes.ts): a
// route is the shortest path through it from its origin port, through each declared
// chokepoint in order, to its destination port. Source: Eurostat's MARNET maritime network
// at 100 km resolution, from the SeaRoute project (EUPL-1.2), itself derived from the Oak
// Ridge National Laboratory CTA Global Shipping Lane Network (2000) and AIS data.
//
// Kept: the largest connected part, with the ±180° meridian joined, the Arctic passages
// (MARNET `pass` northwest / northeast) and lines reaching south of 60°S left out; one
// node is added on the network at each chokepoint of engine/data/chokepoints.json (a "gate",
// so a path through the gate goes through the strait or canal); then only the edges on a
// shortest path between two of the network's port ends or gates (the main lanes, not the
// open-ocean lattice between them); coordinates in 1/100°, the vertices inside an edge
// simplified (Douglas–Peucker, 0.05°).
//
// Format (all coordinates integers in 1/100 degree):
//   nodes   [x0, y0, dx1, dy1, …]  node i = the running sum of pairs 0..i (nodes in Morton order)
//   adj     for each node i in order: n, then n deltas j − i to its neighbours j > i (edge k is
//           the k-th pair listed)
//   via     { "<edge k>": [x, y, …] }  the vertices between the edge's two nodes, from the lower index
//   gates   { "<chokepoint id>": [node, x, y, radius_km] }  centre and radius as in chokepoints.json
const MARNET_URL =
  'https://raw.githubusercontent.com/eurostat/searoute/master/modules/core/src/main/resources/marnet/marnet_plus_100km.gpkg';
const CHOKEPOINTS_PATH = join(dirname(fileURLToPath(import.meta.url)), '..', '..', 'engine', 'data', 'chokepoints.json');
const LANE_DROP_PASS = new Set(['northwest', 'northeast']);
const LANE_MIN_LAT = -60;
const GATE_SNAP_KM = 25; // a node this close to a chokepoint's centre is its gate; else one is added
const VIA_EPS = 5; // Douglas–Peucker tolerance, 1/100°

const kmBetween = (a, b) => {
  const r = Math.PI / 18000;
  const p1 = a[1] * r, p2 = b[1] * r;
  const h = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(((b[0] - a[0]) * r) / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
};
const lengthOf = (pts) => pts.reduce((s, p, i) => (i ? s + kmBetween(pts[i - 1], p) : 0), 0);

async function readMarnet(path) {
  let sqlite;
  try {
    sqlite = await import('node:sqlite');
  } catch {
    throw new Error('the lanes step reads a GeoPackage with node:sqlite: use Node 22.13 or later');
  }
  const db = new sqlite.DatabaseSync(path, { readOnly: true });
  const rows = db.prepare('SELECT geometry, pass FROM type').all();
  db.close();
  // GeoPackage geometry: a header ('GP', version, flags, srs id, envelope), then WKB.
  return rows.map((r) => {
    const b = Buffer.from(r.geometry);
    if (b[0] !== 0x47 || b[1] !== 0x50) throw new Error('MARNET: not a GeoPackage geometry');
    const w = b.subarray(8 + [0, 32, 48, 48, 64][(b[3] >> 1) & 7]);
    const le = w[0] === 1;
    const dv = new DataView(w.buffer, w.byteOffset, w.byteLength);
    const type = dv.getUint32(1, le);
    if (type % 1000 !== 2) throw new Error(`MARNET: geometry type ${type}, expected a LineString`);
    const dims = type >= 3000 ? 4 : type >= 1000 ? 3 : 2;
    const n = dv.getUint32(5, le);
    const pts = [];
    for (let i = 0; i < n; i++) pts.push([dv.getFloat64(9 + i * 8 * dims, le), dv.getFloat64(17 + i * 8 * dims, le)]);
    return { pass: r.pass ?? null, pts };
  });
}

function simplify(pts, eps) {
  if (pts.length < 3) return pts;
  const [a, b] = [pts[0], pts[pts.length - 1]];
  const dx = b[0] - a[0], dy = b[1] - a[1], L = Math.hypot(dx, dy) || 1;
  let worst = 0, at = 0;
  for (let i = 1; i < pts.length - 1; i++) {
    const d = Math.abs(dy * (pts[i][0] - a[0]) - dx * (pts[i][1] - a[1])) / L;
    if (d > worst) [worst, at] = [d, i];
  }
  if (worst <= eps) return [a, b];
  return [...simplify(pts.slice(0, at + 1), eps).slice(0, -1), ...simplify(pts.slice(at), eps)];
}

function buildLanes(lines, chokepoints) {
  const nodes = [];
  const index = new Map();
  const nodeAt = (p) => {
    const k = `${p[0]},${p[1]}`;
    let i = index.get(k);
    if (i === undefined) index.set(k, (i = nodes.push(p) - 1));
    return i;
  };
  // 1/100° integers, with +180° folded onto −180° so the two sides of the Pacific join.
  const q = ([x, y]) => [Math.round(x * 100) === 18000 ? -18000 : Math.round(x * 100), Math.round(y * 100)];
  const byPair = new Map();
  // Where MARNET's own lanes end (degree 1 in the whole network): ports and coasts. Ends made by
  // leaving lines out below are not counted.
  const ends = new Map();
  for (const l of lines) {
    for (const p of [l.pts[0], l.pts[l.pts.length - 1]].map(q)) ends.set(`${p[0]},${p[1]}`, (ends.get(`${p[0]},${p[1]}`) ?? 0) + 1);
  }
  let dropped = 0;
  for (const l of lines) {
    if (l.pass && LANE_DROP_PASS.has(l.pass)) { dropped++; continue; }
    const pts = l.pts.map(q).filter((p, i, a) => !i || p[0] !== a[i - 1][0] || p[1] !== a[i - 1][1]);
    if (pts.length < 2 || pts.some((p) => p[1] < LANE_MIN_LAT * 100)) { dropped++; continue; }
    const a = nodeAt(pts[0]), b = nodeAt(pts[pts.length - 1]);
    if (a === b) continue;
    const e = { a, b, pts, len: lengthOf(pts) };
    const k = a < b ? `${a}-${b}` : `${b}-${a}`;
    if (!byPair.has(k) || e.len < byPair.get(k).len) byPair.set(k, e); // parallel edges: the shorter
  }
  let edges = [...byPair.values()];

  // The largest connected part (MARNET has a few small separate pieces, such as lakes).
  const parent = nodes.map((_, i) => i);
  const find = (i) => { while (parent[i] !== i) i = parent[i] = parent[parent[i]]; return i; };
  for (const e of edges) parent[find(e.a)] = find(e.b);
  const size = new Map();
  for (const e of edges) size.set(find(e.a), (size.get(find(e.a)) ?? 0) + 1);
  const main = [...size].sort((x, y) => y[1] - x[1])[0][0];
  edges = edges.filter((e) => find(e.a) === main);

  // Gates: the network point nearest each chokepoint's centre, made a node.
  const gates = {};
  for (const cp of chokepoints) {
    const c = [Math.round(cp.lon * 100), Math.round(cp.lat * 100)];
    const k = Math.cos((cp.lat * Math.PI) / 180);
    let best = null;
    edges.forEach((e, ei) => {
      for (let s = 0; s < e.pts.length - 1; s++) {
        const [p, r] = [e.pts[s], e.pts[s + 1]];
        const ax = (p[0] - c[0]) * k, ay = p[1] - c[1], dx = (r[0] - p[0]) * k, dy = r[1] - p[1];
        const L = dx * dx + dy * dy;
        const t = L ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / L)) : 0;
        const at = [Math.round(p[0] + t * (r[0] - p[0])), Math.round(p[1] + t * (r[1] - p[1]))];
        const d = kmBetween(c, at);
        if (!best || d < best.d) best = { d, ei, s, at };
      }
    });
    if (!best) continue;
    const e = edges[best.ei];
    const ends = [e.pts[0], e.pts[e.pts.length - 1]];
    const near = ends.map((p, i) => [kmBetween(c, p), i === 0 ? e.a : e.b]).sort((x, y) => x[0] - y[0])[0];
    let node;
    if (near[0] <= GATE_SNAP_KM && near[0] <= best.d + GATE_SNAP_KM) {
      node = near[1];
    } else {
      node = nodeAt(best.at);
      const dedupe = (pts) => pts.filter((p, i, a) => !i || p[0] !== a[i - 1][0] || p[1] !== a[i - 1][1]);
      const left = dedupe([...e.pts.slice(0, best.s + 1), best.at]);
      const right = dedupe([best.at, ...e.pts.slice(best.s + 1)]);
      edges.splice(best.ei, 1);
      if (left.length > 1 && node !== e.a) edges.push({ a: e.a, b: node, pts: left, len: lengthOf(left) });
      if (right.length > 1 && node !== e.b) edges.push({ a: node, b: e.b, pts: right, len: lengthOf(right) });
    }
    gates[cp.id] = { node, centre: c, radius: cp.radius_km, km: kmBetween(c, nodes[node]) };
  }

  // The main lanes: every edge on a shortest path between two terminals (the network's port
  // ends, where a lane stops at a coast, and the gates).
  const adj = new Map();
  edges.forEach((e, i) => {
    for (const [u, v] of [[e.a, e.b], [e.b, e.a]]) {
      if (!adj.has(u)) adj.set(u, []);
      adj.get(u).push([v, e.len, i]);
    }
  });
  const terminals = new Set([...adj].filter(([u, l]) => l.length === 1 && ends.get(`${nodes[u][0]},${nodes[u][1]}`) === 1).map(([u]) => u));
  for (const g of Object.values(gates)) terminals.add(g.node);
  const keep = new Set();
  for (const s of terminals) {
    const dist = new Map([[s, 0]]);
    const via = new Map();
    const heap = [[0, s]];
    while (heap.length) {
      // binary heap
      const top = heap[0];
      const last = heap.pop();
      if (heap.length) {
        heap[0] = last;
        for (let i = 0; ; ) {
          const l = 2 * i + 1, r = l + 1;
          let m = i;
          if (l < heap.length && heap[l][0] < heap[m][0]) m = l;
          if (r < heap.length && heap[r][0] < heap[m][0]) m = r;
          if (m === i) break;
          [heap[m], heap[i]] = [heap[i], heap[m]];
          i = m;
        }
      }
      const [d, u] = top;
      if (d > dist.get(u)) continue;
      for (const [v, w, ei] of adj.get(u) ?? []) {
        if (d + w < (dist.get(v) ?? Infinity)) {
          dist.set(v, d + w);
          via.set(v, [u, ei]);
          heap.push([d + w, v]);
          for (let i = heap.length - 1; i; ) {
            const p = (i - 1) >> 1;
            if (heap[p][0] <= heap[i][0]) break;
            [heap[p], heap[i]] = [heap[i], heap[p]];
            i = p;
          }
        }
      }
    }
    for (const t of terminals) for (let v = t; via.has(v); v = via.get(v)[0]) keep.add(via.get(v)[1]);
  }
  edges = edges.filter((_, i) => keep.has(i));

  // Encode: Morton order keeps neighbours' indices and positions close, so deltas are small.
  const used = [...new Set(edges.flatMap((e) => [e.a, e.b]))];
  const morton = ([x, y]) => {
    const X = (x + 18000) >> 2, Y = (y + 9000) >> 2;
    let m = 0;
    for (let b = 0; b < 14; b++) m += (((X >> b) & 1) * 2 ** (2 * b)) + (((Y >> b) & 1) * 2 ** (2 * b + 1));
    return m;
  };
  used.sort((a, b) => morton(nodes[a]) - morton(nodes[b]) || a - b);
  const order = new Map(used.map((n, i) => [n, i]));
  const outNodes = [];
  let px = 0, py = 0;
  for (const n of used) {
    outNodes.push(nodes[n][0] - px, nodes[n][1] - py);
    [px, py] = nodes[n];
  }
  const fwd = used.map(() => []);
  for (const e of edges) {
    const [a, b] = [order.get(e.a), order.get(e.b)];
    const pts = a < b ? e.pts : [...e.pts].reverse();
    fwd[Math.min(a, b)].push([Math.max(a, b), simplify(pts, VIA_EPS).slice(1, -1)]);
  }
  const adjOut = [];
  const viaOut = {};
  let k = 0;
  fwd.forEach((list, i) => {
    list.sort((x, y) => x[0] - y[0]);
    adjOut.push(list.length);
    for (const [j, mid] of list) {
      adjOut.push(j - i);
      if (mid.length) viaOut[k] = mid.flat();
      k++;
    }
  });
  const gatesOut = {};
  for (const [id, g] of Object.entries(gates)) gatesOut[id] = [order.get(g.node), g.centre[0], g.centre[1], g.radius];
  const missing = Object.entries(gates).filter(([, g]) => !order.has(g.node)).map(([id]) => id);
  if (missing.length) throw new Error(`lanes: gates left off the network: ${missing.join(', ')}`);
  return {
    json: {
      about: 'Sea-lane network for drawing declared routes (web/src/map/lanes.ts). Derived from Eurostat SeaRoute MARNET, 100 km resolution, licensed under the EUPL-1.2 (text in EUPL-1.2.txt beside this file; see ATTRIBUTION.md); built by web/scripts/build-map-data.mjs. Approximate: for drawing, not navigation.',
      licence: 'EUPL-1.2',
      scale: 100,
      nodes: outNodes,
      adj: adjOut,
      via: viaOut,
      gates: gatesOut,
    },
    stats: { nodes: used.length, edges: k, via: Object.values(viaOut).reduce((s, v) => s + v.length / 2, 0), dropped, gates },
  };
}

async function writeLanes() {
  const path = arg('--marnet') ?? (await fetchTo(MARNET_URL, join(tmp, 'marnet_plus_100km.gpkg')));
  const { chokepoints } = JSON.parse(readFileSync(CHOKEPOINTS_PATH, 'utf8'));
  const { json, stats } = buildLanes(await readMarnet(path), chokepoints);
  const text = JSON.stringify(json);
  writeFileSync(join(out, 'lanes.json'), text);
  console.log(`lanes.json: ${stats.nodes} nodes, ${stats.edges} edges, ${stats.via} inner vertices; ${(text.length / 1024).toFixed(1)} KiB (${(gzipSync(text, { level: 9 }).length / 1024).toFixed(1)} KiB gzip)`);
  for (const [id, g] of Object.entries(stats.gates)) console.log(`  gate ${id}: ${g.km.toFixed(0)} km from the centre`);
}

if (process.argv.includes('--lanes-only')) {
  await writeLanes();
  process.exit(0);
}

if (process.argv.includes('--port-names-only')) {
  const ports = JSON.parse(readFileSync(join(out, 'ports.json'), 'utf8'));
  await addPortNames(ports);
  writeFileSync(join(out, 'ports.json'), JSON.stringify(ports));
  process.exit(0);
}

// --- countries -----------------------------------------------------------------------
const nePath = arg('--ne') ?? (await fetchTo(NE_URL, join(tmp, 'ne.geojson')));
const ne = JSON.parse(readFileSync(nePath, 'utf8'));
const centroids = {};
for (const f of ne.features) {
  const p = f.properties;
  const code = iso3(p);
  f.properties = { iso: code ?? '' };
  if (!code) continue;
  const pt = [round(p.LABEL_X, 2), round(p.LABEL_Y, 2), p.NAME];
  centroids[code] = pt;
  // The contract joins on Natural Earth ADM0_A3 too; keep that alias where it differs.
  if (typeof p.ADM0_A3 === 'string' && p.ADM0_A3 !== code) centroids[p.ADM0_A3] = pt;
}
const slim = join(tmp, 'slim.geojson');
writeFileSync(slim, JSON.stringify(ne));
execFileSync(
  'npx',
  [
    '--yes', 'mapshaper@0.6.121', slim,
    '-simplify', 'visvalingam', 'weighted', '12%', 'keep-shapes',
    '-clean',
    '-o', join(out, 'countries.geojson'), 'precision=0.01', 'format=geojson',
  ],
  { stdio: 'inherit' },
);
writeFileSync(join(out, 'centroids.json'), JSON.stringify(centroids));

// --- ports ---------------------------------------------------------------------------
const wpiPath = arg('--wpi') ?? (await fetchTo(WPI_URL, join(tmp, 'wpi.csv')));
const [header, ...rows] = parseCsv(readFileSync(wpiPath, 'utf8').replace(/^\uFEFF/, ''));
const col = (name) => {
  const i = header.indexOf(name);
  if (i < 0) throw new Error(`WPI column missing: ${name}`);
  return i;
};
const [iCode, iName, iLat, iLon] = ['UN/LOCODE', 'Main Port Name', 'Latitude', 'Longitude'].map(col);
const ports = {};
let skipped = 0;
for (const r of rows) {
  const code = (r[iCode] ?? '').replace(/\s+/g, '').toUpperCase();
  const lat = Number(r[iLat]);
  const lon = Number(r[iLon]);
  if (!/^[A-Z]{2}[A-Z2-9]{3}$/.test(code) || !Number.isFinite(lat) || !Number.isFinite(lon)) {
    skipped++;
    continue;
  }
  if (!ports[code]) ports[code] = [round(lon, 3), round(lat, 3), r[iName].trim()];
}
await addPortNames(ports);
writeFileSync(join(out, 'ports.json'), JSON.stringify(ports));

// --- sea lanes -------------------------------------------------------------------------
await writeLanes();

for (const f of ['countries.geojson', 'centroids.json', 'ports.json']) {
  console.log(`${f}: ${(statSync(join(out, f)).size / 1024).toFixed(0)} KiB`);
}
console.log(`${Object.keys(centroids).length} country codes, ${Object.keys(ports).length} ports (${skipped} WPI rows without a valid UN/LOCODE or position)`);
