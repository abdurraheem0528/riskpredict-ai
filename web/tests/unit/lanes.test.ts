// Sea lanes (src/map/lanes.ts): the flow's dash steps, a route's path through the network
// passing its declared chokepoints in order, shared stretches drawn once, the antimeridian.
import { readFileSync } from 'node:fs';
import { resolve } from 'node:path';
import { describe, expect, it } from 'vitest';
import {
  DASH, buildLanes, decodeNetwork, km, routeInputs, routePath, unwrap,
  type LanesFile, type Network,
} from '../../src/map/lanes';
import { __setGazetteer, type LonLat } from '../../src/map/geo';

// Vitest runs from web/ (jsdom: import.meta.url is not a file URL).
const WEB = process.cwd();
const real: Network = decodeNetwork(JSON.parse(readFileSync(resolve(WEB, 'public/map/lanes.json'), 'utf8')) as LanesFile);

const YANTIAN: LonLat = [114.27, 22.57];
const KAOHSIUNG: LonLat = [120.27, 22.6];
const ROTTERDAM: LonLat = [4.4, 51.9];
const SHANGHAI: LonLat = [121.5, 31.23];
const LOS_ANGELES: LonLat = [-118.27, 33.73];
const PENANG: LonLat = [100.35, 5.417];
const SINGAPORE: LonLat = [103.85, 1.283]; // SGSIN's UN/LOCODE position (1°17'N 103°51'E)
const lineKm = (c: LonLat[]) => c.reduce((s, p, i) => (i ? s + km(c[i - 1], p) : 0), 0);

/** A LanesFile from plain nodes ([lon, lat] in degrees), edges and gates, as the build writes it. */
function file(nodes: LonLat[], edges: [number, number][], gates: Record<string, [number, number]> = {}): LanesFile {
  const q = nodes.map(([x, y]) => [Math.round(x * 100), Math.round(y * 100)]);
  const flat: number[] = [];
  q.forEach(([x, y], i) => flat.push(x - (i ? q[i - 1][0] : 0), y - (i ? q[i - 1][1] : 0)));
  const adj: number[] = [];
  nodes.forEach((_, i) => {
    const fwd = edges.map(([a, b]) => [Math.min(a, b), Math.max(a, b)]).filter(([a]) => a === i).map(([, b]) => b - i).sort((a, b) => a - b);
    adj.push(fwd.length, ...fwd);
  });
  const g: LanesFile['gates'] = {};
  for (const [id, [node, r]] of Object.entries(gates)) g[id] = [node, q[node][0], q[node][1], r];
  return { scale: 100, nodes: flat, adj, via: {}, gates: g };
}

const segKeys = (fc: { features: { geometry: { coordinates: number[][] } }[] }) =>
  fc.features.flatMap((f) => f.geometry.coordinates.slice(1).map((c, i) => {
    const p = f.geometry.coordinates[i];
    return [p, c].map((x) => x.map((v) => v.toFixed(2)).join(',')).sort().join('|');
  }));

describe('flow', () => {
  it('steps a 2-unit dash along a 10-unit period in 20 steps of 0.5', () => {
    expect(DASH).toHaveLength(20);
    expect(DASH[0]).toEqual([0, 0.5, 2, 7.5]);
    expect(DASH[15]).toEqual([0, 8, 2, 0]); // s = 8
    expect(DASH[16]).toEqual([0.5, 8, 1.5, 0]); // the dash wraps round to the start
    expect(DASH[19]).toEqual([2, 8, 0, 0]); // s = 10
    for (const d of DASH) expect(d.reduce((a, b) => a + b, 0)).toBeCloseTo(10);
    for (const d of DASH) expect(d[0] + d[2]).toBeCloseTo(2);
  });
});

describe('a route through a small network', () => {
  //   0 ── 1 ── 2      the direct way 0 → 2 is along the top; a route that declares
  //   │         │      "south" (node 4) goes down and round.
  //   3 ── 4 ── 5
  const net = decodeNetwork(file([[0, 1], [1, 1], [2, 1], [0, 0], [1, 0], [2, 0]], [[0, 1], [1, 2], [0, 3], [3, 4], [4, 5], [5, 2]], { south: [4, 50], north: [1, 50] }));

  it('takes the shortest path when nothing is declared', () => {
    const p = routePath(net, [0, 1], [], [2, 1])!;
    expect(p.steps.map((s) => s.to)).toEqual([1, 2]);
    expect(p.passes).toEqual(['north']);
  });

  it('goes through each declared chokepoint, in the declared order', () => {
    const p = routePath(net, [0, 1], ['south'], [2, 1])!;
    expect(p.steps.map((s) => s.to)).toEqual([3, 4, 5, 2]);
    expect(p.via).toEqual(['south']);
    expect(p.passes).toEqual(['south']);
    const q = routePath(net, [0, 1], ['north', 'south'], [2, 0])!;
    const order = q.steps.map((s) => s.to);
    expect(order.indexOf(1)).toBeLessThan(order.indexOf(4));
    expect(q.passes).toEqual(['north', 'south']);
  });

  it('does not pass a chokepoint whose antimeridian the lane crosses', () => {
    // 170°E → 170°W crosses 180°, the antimeridian of a gate at 0°: the line is half a world away.
    const pacific = decodeNetwork(file([[0, 0], [10, 0], [170, 0], [-170, 0]], [[0, 1], [2, 3]], { zero: [0, 100] }));
    const p = routePath(pacific, [170, 0], [], [-170, 0])!;
    expect(p.coords.at(-1)).toEqual([190, 0]);
    expect(p.passes).toEqual([]);
  });

  it('skips a chokepoint it has no place for, and uses one from the disruptions layer', () => {
    expect(routePath(net, [0, 1], ['atlantis'], [2, 1])!.via).toEqual([]);
    const p = routePath(net, [0, 1], ['east'], [0, 0], { east: { at: [2, 0.5], radiusKm: 80 } })!;
    expect(p.via).toEqual(['east']);
    expect(p.steps.map((s) => s.to)).toContain(2);
  });
});

describe('the real network (public/map/lanes.json)', () => {
  it('has a gate for every chokepoint the engine knows', () => {
    const engine = JSON.parse(readFileSync(resolve(WEB, '../engine/data/chokepoints.json'), 'utf8')) as { chokepoints: { id: string; lat: number; lon: number }[] };
    for (const c of engine.chokepoints) {
      const g = real.gates[c.id];
      expect(g, c.id).toBeDefined();
      expect(g.at).toEqual([c.lon, c.lat]);
    }
  });

  it('has no edge the wrong way round the world', () => {
    // The longest real edge is MARNET's 3,611 km Southern Ocean lane toward Cape Horn; an edge
    // across ±180° read as flat lon/lat is 6,000 km or more (scripts/build-map-data.mjs).
    let longest = 0;
    for (const w of real.weight) longest = Math.max(longest, w);
    expect(longest).toBeLessThan(4000);
  });

  it('puts each gate inside its chokepoint, on a lane through it', () => {
    for (const [id, g] of Object.entries(real.gates)) {
      expect(km(g.at, [real.x[g.node], real.y[g.node]]), id).toBeLessThanOrEqual(g.radiusKm);
      const edges = Array.from(real.weight.subarray(real.start[g.node], real.start[g.node + 1]));
      expect(edges.length, id).toBeGreaterThanOrEqual(2);
      for (const w of edges) expect(w, id).toBeLessThan(1000);
    }
  });

  // Published sea distances (Port of Rotterdam, Suez Canal Authority): Singapore → Rotterdam
  // 8,288 nmi (15,349 km) by Suez and 11,755 nmi (21,770 km) by the Cape. Kaohsiung is about
  // 1,600 nmi (3,000 km) before Singapore, Penang about 350 nmi (650 km) after it. MARNET at
  // 100 km runs a few per cent long.
  it.each([
    ['Kaohsiung', KAOHSIUNG, ['singapore', 'malacca', 'bab_el_mandeb', 'suez', 'gibraltar', 'dover'], 17000, 20500],
    ['Penang', PENANG, ['malacca', 'bab_el_mandeb', 'suez', 'gibraltar', 'dover'], 14500, 16500],
    ['Singapore', SINGAPORE, ['cape_of_good_hope', 'dover'], 20500, 24000],
  ] as const)('routes %s → Rotterdam about as far as ships sail it', (_, origin, cps, lo, hi) => {
    const p = routePath(real, origin, cps, ROTTERDAM)!;
    expect(p.via).toEqual(cps);
    const length = lineKm(p.coords);
    expect(length).toBeGreaterThan(lo);
    expect(length).toBeLessThan(hi);
    // no spur out into the Pacific (it used to reach 175°E)
    for (const [lon] of p.coords) expect(lon).toBeLessThan(125);
  });

  it('routes Yantian → Bab el-Mandeb → Suez → Rotterdam through them in that order, by sea', () => {
    const p = routePath(real, YANTIAN, ['bab_el_mandeb', 'suez'], ROTTERDAM)!;
    expect(p.via).toEqual(['bab_el_mandeb', 'suez']);
    const nodes = p.steps.map((s) => s.to);
    expect(nodes).toContain(real.gates.bab_el_mandeb.node);
    expect(nodes.indexOf(real.gates.bab_el_mandeb.node)).toBeLessThan(nodes.indexOf(real.gates.suez.node));
    // and, on the way, the straits a ship takes (not declared, but passed)
    const at = (id: string) => p.passes.indexOf(id);
    for (const id of ['malacca', 'bab_el_mandeb', 'suez', 'gibraltar', 'dover']) expect(at(id), id).toBeGreaterThanOrEqual(0);
    expect(at('malacca')).toBeLessThan(at('bab_el_mandeb'));
    expect(at('suez')).toBeLessThan(at('gibraltar'));
    expect(at('gibraltar')).toBeLessThan(at('dover'));
    expect(p.passes).not.toContain('cape_of_good_hope');
    expect(p.passes).not.toContain('panama');
    // no shortcut over land: every step is short, and the line stays near the trunk's latitudes
    for (let i = 1; i < p.coords.length; i++) expect(Math.abs(p.coords[i][0] - p.coords[i - 1][0])).toBeLessThan(25);
  });

  it('follows the declared order backwards too, and a Cape route avoids Suez', () => {
    const back = routePath(real, ROTTERDAM, ['suez', 'bab_el_mandeb'], YANTIAN)!;
    expect(back.passes.indexOf('suez')).toBeLessThan(back.passes.indexOf('bab_el_mandeb'));
    const cape = routePath(real, YANTIAN, ['cape_of_good_hope'], ROTTERDAM)!;
    expect(cape.passes).toContain('cape_of_good_hope');
    expect(cape.passes).not.toContain('suez');
  });

  it('passes a chokepoint it crosses, not one whose circle it only starts in', () => {
    // Kaohsiung lies within the Taiwan Strait's 200 km circle, but its lane heads south.
    expect(routePath(real, KAOHSIUNG, ['bab_el_mandeb', 'suez'], ROTTERDAM)!.passes).not.toContain('taiwan_strait');
    expect(routePath(real, SHANGHAI, [], [114.17, 22.3])!.passes).toContain('taiwan_strait');
  });

  it('crosses the Pacific without wrapping round the map', () => {
    const p = routePath(real, SHANGHAI, [], LOS_ANGELES)!;
    for (let i = 1; i < p.coords.length; i++) expect(Math.abs(p.coords[i][0] - p.coords[i - 1][0])).toBeLessThan(30);
    const lons = p.coords.map((c) => c[0]);
    expect(Math.max(...lons) - Math.min(...lons)).toBeLessThan(180); // the short way, across 180°
    expect(p.passes).not.toContain('panama');
  });

  it('draws shared stretches once and counts the routes at each chokepoint', () => {
    const lanes = buildLanes(real, [
      { id: 'a', origin: YANTIAN, dest: ROTTERDAM, chokepoints: ['bab_el_mandeb', 'suez'] },
      { id: 'b', origin: KAOHSIUNG, dest: ROTTERDAM, chokepoints: ['bab_el_mandeb', 'suez'] },
    ]);
    expect([...lanes.routes.keys()]).toEqual(['a', 'b']);
    const keys = segKeys(lanes.network);
    expect(new Set(keys).size).toBe(keys.length);
    const routeSegs = [...lanes.routes.values()].flatMap((f) => segKeys({ features: [f] }));
    expect(keys.length).toBeLessThan(routeSegs.length * 0.75); // most of the way is shared
    expect(lanes.counts.bab_el_mandeb).toBe(2);
    expect(lanes.counts.suez).toBe(2);
    expect(lanes.conflicts).toBe(0);
    // A route the other way shares the edges but draws nothing twice.
    const both = buildLanes(real, [
      { id: 'a', origin: YANTIAN, dest: ROTTERDAM, chokepoints: ['suez'] },
      { id: 'c', origin: ROTTERDAM, dest: YANTIAN, chokepoints: ['suez'] },
    ]);
    const k2 = segKeys(both.network);
    expect(new Set(k2).size).toBe(k2.length);
    expect(both.conflicts).toBeGreaterThan(0);
    expect(both.counts.suez).toBe(2);
  });
});

describe('route inputs', () => {
  it('reads only declared routes with a first and a last port on the map', () => {
    const fs = [
      { id: 'r1', properties: { kind: 'route', ports: ['CNYTN', 'NLRTM'], chokepoints: ['bab_el_mandeb', 'suez'] } },
      { id: 'r2', properties: { kind: 'route', ports: ['XXZZZ', 'TWKHH', 'NLRTM'] } },
      { id: 'r3', properties: { kind: 'route', ports: ['CNYTN'] } },
      { id: 's1', properties: { kind: 'supplier', ports: ['CNYTN', 'NLRTM'] } },
    ];
    const ins = routeInputs(fs);
    expect(ins.map((r) => r.id)).toEqual(['r1', 'r2']);
    expect(ins[0].chokepoints).toEqual(['bab_el_mandeb', 'suez']);
    expect(ins[1].origin).toEqual([120.27, 22.6]);
    expect(ins[1].chokepoints).toEqual([]);
  });

  it('draws routes declared with the standard codes of the busiest ports (public/map/ports.json)', () => {
    const ports = JSON.parse(readFileSync(resolve(WEB, 'public/map/ports.json'), 'utf8')) as Parameters<typeof __setGazetteer>[1];
    __setGazetteer({}, ports);
    try {
      const fs = [
        { id: 'sha', properties: { kind: 'route', ports: ['CNSHA', 'NLRTM'], chokepoints: ['malacca', 'bab_el_mandeb', 'suez'] } },
        { id: 'sin', properties: { kind: 'route', ports: ['SGSIN', 'NLRTM'] } },
        { id: 'szx', properties: { kind: 'route', ports: ['CNSZX', 'USLAX'] } },
      ];
      const ins = routeInputs(fs);
      expect(ins.map((r) => r.id)).toEqual(['sha', 'sin', 'szx']);
      for (const r of ins) expect(routePath(real, r.origin, r.chokepoints, r.dest), r.id).not.toBeNull();
    } finally {
      __setGazetteer( // back to tests/setup.ts's tables
        { TUR: [35.2, 39.1, 'Turkey'], CHN: [104.2, 35.9, 'China'], TWN: [121.0, 23.7, 'Taiwan'], NLD: [5.3, 52.1, 'Netherlands'], YEM: [47.6, 15.9, 'Yemen'] },
        { CNYTN: [114.27, 22.57, 'Yantian'], NLRTM: [4.4, 51.9, 'Rotterdam'], TWKHH: [120.27, 22.6, 'Kao-Hsiung'] },
      );
    }
  });

  it('unwraps longitudes so a line never jumps across the map', () => {
    expect(unwrap([[170, 0], [-175, 0], [-160, 5]])).toEqual([[170, 0], [185, 0], [200, 5]]);
    expect(unwrap([[190, 0], [-170, 1]])).toEqual([[-170, 0], [-170, 1]]);
  });
});
