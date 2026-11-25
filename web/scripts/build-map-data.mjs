#!/usr/bin/env node
// Builds the static map data in public/map/ from two public-domain sources.
// The output is committed, so CI and the site never fetch these at run time.
//
//   Natural Earth 1:50m admin-0 countries (public domain) → countries.geojson, centroids.json ([lon, lat, name])
//   NGA World Port Index, Pub 150 (US Government work, public domain) → ports.json
//
// Usage: node scripts/build-map-data.mjs [--wpi path/to/UpdatedPub150.csv] [--ne path/to/ne_50m_admin_0_countries.geojson]
// Without the flags it downloads both from the URLs below. Needs `npx` (mapshaper 0.6).
import { execFileSync } from 'node:child_process';
import { mkdtempSync, readFileSync, writeFileSync, statSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

const NE_URL =
  'https://raw.githubusercontent.com/nvkelso/natural-earth-vector/master/geojson/ne_50m_admin_0_countries.geojson';
// Mirror of msi.nga.mil's UpdatedPub150.csv (the NGA link has no stable machine URL).
const WPI_URL =
  'https://raw.githubusercontent.com/CienciaDeDatosEspacial/dataSets/main/WORLD/UpdatedPub150.csv';

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
writeFileSync(join(out, 'ports.json'), JSON.stringify(ports));

for (const f of ['countries.geojson', 'centroids.json', 'ports.json']) {
  console.log(`${f}: ${(statSync(join(out, f)).size / 1024).toFixed(0)} KiB`);
}
console.log(`${Object.keys(centroids).length} country codes, ${Object.keys(ports).length} ports (${skipped} WPI rows without a valid UN/LOCODE or position)`);
