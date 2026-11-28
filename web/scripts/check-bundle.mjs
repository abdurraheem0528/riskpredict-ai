#!/usr/bin/env node
// Fails if the built site contains the designs' sample content or any test mock, and
// prints the size of every file a visitor can download (raw and gzip).
//
// The design files (design/project/*.dc.html) are illustration only: their company,
// supplier and people names, and numbers like "9 of 9 feeds live", must never ship.
import { readdirSync, readFileSync, statSync } from 'node:fs';
import { join, relative, extname } from 'node:path';
import { gzipSync } from 'node:zlib';
import { fileURLToPath } from 'node:url';

const dist = fileURLToPath(new URL('../dist', import.meta.url));

const SAMPLE = [
  // design sample content
  'Atlas Electronics', 'Atlas · Supply base', 'Gulf Components', 'Shenzhen Bright Parts', 'Penang Circuit Works',
  'Chennai Cable', 'Formosa Memory', 'Hsinchu Micro', 'Taipei Passive Parts', '9 of 9 feeds live', '8 of 9 feeds live',
  'J. Rivera', 'A. Chen', 'Red Sea transit', 'Hamburg · your warehouse', 'you pay in euros', 'R-011', 'R-014', 'R-016',
  'Excel stopped syncing', '31 of 38', '+€41k', 'Sun 23 Nov',
  // test fixtures and mock tooling (tests/ only)
  'Kestrel Components', 'kestrel.test', 'Anatolia Fasteners', 'Lumen Display Co', 'mswjs', 'setupWorker', 'FakeEventSource',
  'Atlas Eindhoven Assembly Plant', 'Formosa Microcircuits', 'Penang Semicon Assembly', 'Pearl River PCB Works', 'news.example.test',
  'Kolwezi Northern Copper', 'Jubail Polymer Industries',
];

function* files(dir) {
  for (const name of readdirSync(dir)) {
    const p = join(dir, name);
    if (statSync(p).isDirectory()) yield* files(p);
    else yield p;
  }
}

const TEXT = new Set(['.js', '.css', '.html', '.json', '.svg', '.txt', '.map', '.geojson', '.md']);
let failed = false;
const rows = [];
for (const f of files(dist)) {
  const buf = readFileSync(f);
  rows.push([relative(dist, f), buf.length, gzipSync(buf, { level: 9 }).length]);
  if (!TEXT.has(extname(f)) && !f.endsWith('_redirects') && !f.endsWith('_headers')) continue;
  const text = buf.toString('utf8').toLowerCase();
  for (const s of SAMPLE) {
    if (text.includes(s.toLowerCase())) {
      console.error(`✗ ${relative(dist, f)} contains sample text: "${s}"`);
      failed = true;
    }
  }
}

const kb = (n) => `${(n / 1024).toFixed(1)} KiB`;
rows.sort((a, b) => b[1] - a[1]);
console.log('file'.padEnd(56), 'raw'.padStart(12), 'gzip'.padStart(12));
for (const [name, raw, gz] of rows) console.log(name.padEnd(56), kb(raw).padStart(12), kb(gz).padStart(12));
const sum = (pred) => rows.filter(([n]) => pred(n)).reduce((a, [, r, g]) => [a[0] + r, a[1] + g], [0, 0]);
const [jsR, jsG] = sum((n) => n.endsWith('.js'));
const [appR, appG] = sum((n) => n.endsWith('.js') && !n.includes('maplibre'));
const [mapR, mapG] = sum((n) => n.startsWith('map/'));
console.log(`\nJavaScript: ${kb(jsR)} (${kb(jsG)} gzip), of which the app without MapLibre ${kb(appR)} (${kb(appG)} gzip)`);
console.log(`Map data (public/map): ${kb(mapR)} (${kb(mapG)} gzip)`);

if (failed) {
  console.error('\nThe bundle must not contain design sample content or test fixtures.');
  process.exit(1);
}
console.log(`\n✓ none of ${SAMPLE.length} sample or fixture strings found in dist/`);
