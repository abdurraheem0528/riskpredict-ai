// Sea lanes for declared routes. A route entity's `ports` is ordered origin → destination and
// its `chokepoints` (on /v1/map/entities) lists the chokepoints it declares, in order. Its lane
// is the shortest path through the sea-lane network in public/map/lanes.json (Eurostat MARNET,
// see ATTRIBUTION.md): origin port → each declared chokepoint in turn → destination port.
// Nothing is invented: an entity without a declared route has no lane (its inferred
// chokepoints stay on its risks). Shared stretches are drawn once, each pointing toward the
// destination of the first route that uses it, so the lanes never stack into a solid trunk.
import { useEffect, useMemo, useState } from 'react';
import type { Feature, FeatureCollection, LineString } from 'geojson';
import { portPoint, type LonLat } from './geo';
import { useGazetteer } from './data';

// --- flow -----------------------------------------------------------------------------------

/**
 * The dash steps of the flow layer: a 2-unit dash moves 0.5 units along an 8-unit gap each
 * step (one step every 80 ms), toward the end of the line, i.e. the destination.
 */
export const DASH: [number, number, number, number][] = [];
for (let s = 0.5; s <= 10; s += 0.5) DASH.push(s <= 8 ? [0, s, 2, 8 - s] : [s - 8, 8, 2 - (s - 8), 0]);
export const DASH_STEP_MS = 80;

// --- the network ----------------------------------------------------------------------------

/** public/map/lanes.json (scripts/build-map-data.mjs documents the format). */
export type LanesFile = {
  scale: number;
  nodes: number[];
  adj: number[];
  via: Record<string, number[]>;
  gates: Record<string, [node: number, x: number, y: number, radiusKm: number]>;
};

export type Gate = { node: number; at: LonLat; radiusKm: number };
export type Network = {
  /** Node positions (degrees). */
  x: Float64Array;
  y: Float64Array;
  /** Edge k joins edgeA[k] < edgeB[k]; via[k] holds its inner vertices from A to B. */
  edgeA: Int32Array;
  edgeB: Int32Array;
  via: (LonLat[] | undefined)[];
  /** Adjacency (CSR): the neighbours of node i are to[start[i] .. start[i + 1]). */
  start: Int32Array;
  to: Int32Array;
  edgeOf: Int32Array;
  weight: Float64Array;
  /** The chokepoints' nodes on the network, with their centre and radius. */
  gates: Record<string, Gate>;
  /** Shortest-path trees by source node (a few kept), and paths by leg. */
  trees: Map<number, { dist: Float64Array; prev: Int32Array; prevEdge: Int32Array }>;
  legs: Map<string, Step[] | null>;
};
/** One edge of a path, walked from `from` to `to`. */
export type Step = { edge: number; from: number; to: number };

const R = Math.PI / 180;
export function km(a: LonLat, b: LonLat): number {
  const p1 = a[1] * R, p2 = b[1] * R;
  const h = Math.sin((p2 - p1) / 2) ** 2 + Math.cos(p1) * Math.cos(p2) * Math.sin(((b[0] - a[0]) * R) / 2) ** 2;
  return 12742 * Math.asin(Math.min(1, Math.sqrt(h)));
}

export function decodeNetwork(f: LanesFile): Network {
  const s = f.scale || 100;
  const n = f.nodes.length / 2;
  const x = new Float64Array(n), y = new Float64Array(n);
  let px = 0, py = 0;
  for (let i = 0; i < n; i++) {
    px += f.nodes[2 * i];
    py += f.nodes[2 * i + 1];
    x[i] = px / s;
    y[i] = py / s;
  }
  const A: number[] = [], B: number[] = [];
  for (let i = 0, p = 0; i < n; i++) {
    const c = f.adj[p++];
    for (let t = 0; t < c; t++) {
      A.push(i);
      B.push(i + f.adj[p++]);
    }
  }
  const m = A.length;
  const via: (LonLat[] | undefined)[] = new Array(m);
  for (const [k, flat] of Object.entries(f.via)) {
    const pts: LonLat[] = [];
    for (let i = 0; i < flat.length; i += 2) pts.push([flat[i] / s, flat[i + 1] / s]);
    via[Number(k)] = pts;
  }
  const deg = new Int32Array(n + 1);
  for (let k = 0; k < m; k++) {
    deg[A[k] + 1]++;
    deg[B[k] + 1]++;
  }
  const start = new Int32Array(n + 1);
  for (let i = 0; i < n; i++) start[i + 1] = start[i] + deg[i + 1];
  const fill = start.slice(0, n);
  const to = new Int32Array(2 * m), edgeOf = new Int32Array(2 * m), weight = new Float64Array(2 * m);
  for (let k = 0; k < m; k++) {
    const len = lineKm([[x[A[k]], y[A[k]]], ...(via[k] ?? []), [x[B[k]], y[B[k]]]]);
    for (const [u, v] of [[A[k], B[k]], [B[k], A[k]]]) {
      const at = fill[u]++;
      to[at] = v;
      edgeOf[at] = k;
      weight[at] = len;
    }
  }
  const gates: Record<string, Gate> = {};
  for (const [id, [node, gx, gy, r]] of Object.entries(f.gates)) gates[id] = { node, at: [gx / s, gy / s], radiusKm: r };
  return { x, y, edgeA: Int32Array.from(A), edgeB: Int32Array.from(B), via, start, to, edgeOf, weight, gates, trees: new Map(), legs: new Map() };
}

const lineKm = (pts: LonLat[]) => pts.reduce((sum, p, i) => (i ? sum + km(pts[i - 1], p) : 0), 0);

/** An edge's vertices, from A to B (`forward`) or from B to A. */
function edgeCoords(net: Pick<Network, 'x' | 'y' | 'edgeA' | 'edgeB' | 'via'>, k: number, forward: boolean): LonLat[] {
  const a = net.edgeA[k], b = net.edgeB[k];
  const pts: LonLat[] = [[net.x[a], net.y[a]], ...(net.via[k] ?? []), [net.x[b], net.y[b]]];
  return forward ? pts : pts.reverse();
}

export function nodePoint(net: Network, i: number): LonLat {
  return [net.x[i], net.y[i]];
}

/** The node nearest a point, and how far it is (km). */
export function nearestNode(net: Network, p: LonLat): { node: number; km: number } {
  let best = -1, bd = Infinity;
  const cos = Math.cos(p[1] * R);
  // A cheap equirectangular pass picks a few candidates; the haversine decides.
  const cand: [number, number][] = [];
  for (let i = 0; i < net.x.length; i++) {
    let dx = Math.abs(net.x[i] - p[0]);
    if (dx > 180) dx = 360 - dx;
    const dy = net.y[i] - p[1];
    const d = (dx * cos) ** 2 + dy * dy;
    if (cand.length < 8 || d < cand[cand.length - 1][1]) {
      cand.push([i, d]);
      cand.sort((a, b) => a[1] - b[1]);
      if (cand.length > 8) cand.pop();
    }
  }
  for (const [i] of cand) {
    const d = km(p, [net.x[i], net.y[i]]);
    if (d < bd) [best, bd] = [i, d];
  }
  return { node: best, km: bd };
}

const MAX_TREES = 48;
function tree(net: Network, src: number) {
  const hit = net.trees.get(src);
  if (hit) return hit;
  const n = net.x.length;
  const dist = new Float64Array(n).fill(Infinity);
  const prev = new Int32Array(n).fill(-1), prevEdge = new Int32Array(n).fill(-1);
  dist[src] = 0;
  // binary heap of [dist, node]
  const hd: number[] = [0], hn: number[] = [src];
  const swap = (i: number, j: number) => {
    [hd[i], hd[j]] = [hd[j], hd[i]];
    [hn[i], hn[j]] = [hn[j], hn[i]];
  };
  while (hd.length) {
    const d = hd[0], u = hn[0];
    const ld = hd.pop()!, ln = hn.pop()!;
    if (hd.length) {
      hd[0] = ld;
      hn[0] = ln;
      for (let i = 0; ; ) {
        const l = 2 * i + 1, r = l + 1;
        let m = i;
        if (l < hd.length && hd[l] < hd[m]) m = l;
        if (r < hd.length && hd[r] < hd[m]) m = r;
        if (m === i) break;
        swap(m, i);
        i = m;
      }
    }
    if (d > dist[u]) continue;
    for (let a = net.start[u]; a < net.start[u + 1]; a++) {
      const v = net.to[a], nd = d + net.weight[a];
      if (nd < dist[v]) {
        dist[v] = nd;
        prev[v] = u;
        prevEdge[v] = net.edgeOf[a];
        hd.push(nd);
        hn.push(v);
        for (let i = hd.length - 1; i; ) {
          const p = (i - 1) >> 1;
          if (hd[p] <= hd[i]) break;
          swap(p, i);
          i = p;
        }
      }
    }
  }
  const t = { dist, prev, prevEdge };
  if (net.trees.size >= MAX_TREES) net.trees.delete(net.trees.keys().next().value!);
  net.trees.set(src, t);
  return t;
}

/** The edges of the shortest path from node `a` to node `b` (empty when a = b, null when none). */
export function shortestPath(net: Network, a: number, b: number): Step[] | null {
  const key = `${a}>${b}`;
  if (net.legs.has(key)) return net.legs.get(key)!;
  let out: Step[] | null = [];
  if (a !== b) {
    const t = tree(net, a);
    if (!Number.isFinite(t.dist[b])) out = null;
    else for (let v = b; v !== a; v = t.prev[v]) out.unshift({ edge: t.prevEdge[v], from: t.prev[v], to: v });
  }
  net.legs.set(key, out);
  return out;
}

// --- routes -----------------------------------------------------------------------------------

/** A port this close to its snapped node is joined to the lane by a short straight leg. */
export const PORT_LEG_KM = 60;

export type ChokepointPlaces = Record<string, { at: LonLat; radiusKm?: number | null }>;
export type RoutePath = {
  /** Network edges, in order, from the origin's node to the destination's. */
  steps: Step[];
  /** The line, origin → destination, its longitudes unwrapped (no jump at ±180°). */
  coords: LonLat[];
  /** The declared chokepoints the lane was routed through (unknown ones are skipped), in order. */
  via: string[];
  /** Every chokepoint the lane passes (declared or not), in the order it passes them. */
  passes: string[];
  originLeg: LonLat[] | null;
  destLeg: LonLat[] | null;
};

/** Longitudes made continuous along a line, so a Pacific crossing does not wrap round the map. */
export function unwrap(coords: LonLat[]): LonLat[] {
  const out: LonLat[] = [];
  for (const [lon, lat] of coords) {
    let x = lon;
    if (out.length) {
      const px = out[out.length - 1][0];
      while (x - px > 180) x -= 360;
      while (x - px < -180) x += 360;
    } else {
      while (x > 180) x -= 360;
      while (x < -180) x += 360;
    }
    out.push([x, lat]);
  }
  return out;
}

function gateFor(net: Network, id: string, extra?: ChokepointPlaces): Gate | null {
  const g = net.gates[id];
  if (g) return g;
  const e = extra?.[id];
  if (!e) return null;
  return { node: nearestNode(net, e.at).node, at: e.at, radiusKm: e.radiusKm ?? 100 };
}

/** Shortest distance (km) from a point to a line, on a local flat projection around the point. */
function kmToLine(p: LonLat, line: LonLat[]): number {
  const k = Math.cos(p[1] * R) * 111.195, ky = 111.195;
  let best = Infinity;
  const rel = (q: LonLat): [number, number] => {
    let dx = q[0] - p[0];
    dx -= 360 * Math.round(dx / 360);
    return [dx * k, (q[1] - p[1]) * ky];
  };
  for (let i = 0; i < line.length - 1; i++) {
    // The far end relative to the near one: wrapping each end on its own would read a segment
    // across the point's antimeridian as one round the world, through the point.
    const [ax, ay] = rel(line[i]);
    let step = line[i + 1][0] - line[i][0];
    step -= 360 * Math.round(step / 360);
    const bx = ax + step * k, by = (line[i + 1][1] - p[1]) * ky;
    const dx = bx - ax, dy = by - ay, L = dx * dx + dy * dy;
    const t = L ? Math.max(0, Math.min(1, -(ax * dx + ay * dy) / L)) : 0;
    best = Math.min(best, Math.hypot(ax + t * dx, ay + t * dy));
  }
  if (line.length === 1) best = Math.hypot(...rel(line[0]));
  return best;
}

/**
 * The lane of one route: the shortest path from the origin port's nearest node, through the
 * gate of each declared chokepoint in order, to the destination port's nearest node. Null when
 * the network cannot join them (or both ends snap to one node).
 */
export function routePath(net: Network, origin: LonLat, chokepoints: readonly string[], dest: LonLat, extra?: ChokepointPlaces): RoutePath | null {
  const o = nearestNode(net, origin), d = nearestNode(net, dest);
  const via: string[] = [];
  const stops = [o.node];
  for (const id of chokepoints) {
    const g = gateFor(net, id, extra);
    if (!g) continue;
    via.push(id);
    if (stops[stops.length - 1] !== g.node) stops.push(g.node);
  }
  if (stops[stops.length - 1] !== d.node) stops.push(d.node);
  if (stops.length < 2) return null;
  const steps: Step[] = [];
  for (let i = 1; i < stops.length; i++) {
    const leg = shortestPath(net, stops[i - 1], stops[i]);
    if (!leg) return null;
    steps.push(...leg);
  }
  const raw: LonLat[] = [];
  const originLeg = o.km <= PORT_LEG_KM ? [origin, nodePoint(net, o.node)] : null;
  const destLeg = d.km <= PORT_LEG_KM ? [nodePoint(net, d.node), dest] : null;
  if (originLeg) raw.push(origin);
  raw.push(nodePoint(net, o.node));
  for (const s of steps) raw.push(...edgeCoords(net, s.edge, s.from === net.edgeA[s.edge]).slice(1));
  if (destLeg) raw.push(dest);
  const coords = unwrap(raw);
  // Which chokepoints it passes: through its gate, or across its circle (centre and radius as
  // the engine matches them). A lane that only starts or ends inside a circle (Kaohsiung is
  // within the Taiwan Strait's) passes it only through its gate.
  const onPath = new Set(steps.flatMap((s) => [s.from, s.to]));
  const ids = new Set([...Object.keys(net.gates), ...Object.keys(extra ?? {})]);
  const passes: [string, number][] = [];
  for (const id of ids) {
    const g = gateFor(net, id, extra);
    if (!g) continue;
    const inside = (p: LonLat) => km(g.at, p) <= g.radiusKm;
    const crosses = !inside(origin) && !inside(dest) && kmToLine(g.at, coords) <= g.radiusKm;
    if (!onPath.has(g.node) && !crosses) continue;
    // Its place along the line: the vertex nearest its centre.
    let at = 0, bd = Infinity;
    coords.forEach((c, i) => {
      const dd = km(g.at, c);
      if (dd < bd) [bd, at] = [dd, i];
    });
    passes.push([id, at]);
  }
  passes.sort((a, b) => a[1] - b[1]);
  return { steps, coords, via, passes: passes.map(([id]) => id), originLeg, destLeg };
}

// --- all the lanes of a workspace -----------------------------------------------------------

export type RouteInput = { id: string; origin: LonLat; dest: LonLat; chokepoints: readonly string[] };
export type Lanes = {
  /** Every drawn stretch once, pointing toward the destination of the first route using it. */
  network: FeatureCollection<LineString, { routes: number }>;
  /** Each route's own line (entity id → line), for the selected entity. */
  routes: Map<string, Feature<LineString, { id: string }>>;
  /** Chokepoint id → the number of drawn routes passing it ("Bab el-Mandeb · 8 lanes"). */
  counts: Record<string, number>;
  /** Shared edges walked in the opposite direction by a later route (drawn once, the first way). */
  conflicts: number;
};

export function buildLanes(net: Network, inputs: readonly RouteInput[], extra?: ChokepointPlaces): Lanes {
  const routes = new Map<string, Feature<LineString, { id: string }>>();
  const counts: Record<string, number> = {};
  const forward = new Map<number, boolean>(); // edge → walked A → B
  const users = new Map<number, number>();
  const legs = new Map<string, LonLat[]>();
  let conflicts = 0;
  for (const r of inputs) {
    const p = routePath(net, r.origin, r.chokepoints, r.dest, extra);
    if (!p || p.coords.length < 2) continue;
    routes.set(r.id, { type: 'Feature', properties: { id: r.id }, geometry: { type: 'LineString', coordinates: p.coords } });
    for (const id of p.passes) counts[id] = (counts[id] ?? 0) + 1;
    for (const s of p.steps) {
      const fwd = s.from === net.edgeA[s.edge];
      if (!forward.has(s.edge)) forward.set(s.edge, fwd);
      else if (forward.get(s.edge) !== fwd) conflicts++;
      users.set(s.edge, (users.get(s.edge) ?? 0) + 1);
    }
    // A port's short leg to the network, once whichever way it is walked (the first way).
    for (const leg of [p.originLeg, p.destLeg]) {
      const key = leg?.map((c) => c.join(',')).sort().join(';');
      if (leg && key && !legs.has(key)) legs.set(key, leg);
    }
  }
  return { network: chains(net, forward, users, [...legs.values()]), routes, counts, conflicts };
}

/** The chosen directed edges merged into as few lines as possible (each edge once). */
function chains(net: Network, forward: Map<number, boolean>, users: Map<number, number>, legs: LonLat[][]): Lanes['network'] {
  const out = new Map<number, [number, number][]>(); // node → [edge, next node]
  const indeg = new Map<number, number>();
  for (const [e, fwd] of forward) {
    const [a, b] = fwd ? [net.edgeA[e], net.edgeB[e]] : [net.edgeB[e], net.edgeA[e]];
    if (!out.has(a)) out.set(a, []);
    out.get(a)!.push([e, b]);
    indeg.set(b, (indeg.get(b) ?? 0) + 1);
  }
  const through = (v: number) => (indeg.get(v) ?? 0) === 1 && (out.get(v)?.length ?? 0) === 1;
  const done = new Set<number>();
  const features: Lanes['network']['features'] = [];
  const walk = (u: number, first: [number, number]) => {
    const pts: LonLat[] = [nodePoint(net, u)];
    let n = 0;
    for (let step: [number, number] | undefined = first; step && !done.has(step[0]); ) {
      const [e, v]: [number, number] = step;
      done.add(e);
      n = Math.max(n, users.get(e) ?? 1);
      pts.push(...edgeCoords(net, e, forward.get(e)!).slice(1));
      step = through(v) ? out.get(v)![0] : undefined;
    }
    features.push({ type: 'Feature', properties: { routes: n }, geometry: { type: 'LineString', coordinates: unwrap(pts) } });
  };
  for (const [u, list] of out) if (!through(u)) for (const s of list) if (!done.has(s[0])) walk(u, s);
  // What is left are closed loops.
  for (const [u, list] of out) for (const s of list) if (!done.has(s[0])) walk(u, s);
  for (const leg of legs) features.push({ type: 'Feature', properties: { routes: 1 }, geometry: { type: 'LineString', coordinates: unwrap(leg) } });
  return { type: 'FeatureCollection', features };
}

// --- loading and the hook -------------------------------------------------------------------

let loading: Promise<Network> | null = null;
let loaded: Network | null = null;

/** Load the network once (≈ 22 KiB gzipped); the URL carries the map data's hash (vite.config.ts). */
export function loadLanes(): Promise<Network> {
  if (!loading) {
    loading = fetch(`/map/lanes.json?v=${__MAP_DATA_VERSION__}`)
      .then((r) => {
        if (!r.ok) throw new Error(`/map/lanes.json: ${r.status}`);
        return r.json() as Promise<LanesFile>;
      })
      .then((f) => (loaded = decodeNetwork(f)))
      .catch((e) => {
        loading = null;
        throw e;
      });
  }
  return loading;
}
/** For tests. */
export function __setLanes(net: Network | null) {
  loaded = net;
  loading = net ? Promise.resolve(net) : null;
}

export function useNetwork(): Network | null {
  const [net, setNet] = useState<Network | null>(loaded);
  useEffect(() => {
    if (net) return;
    let live = true;
    loadLanes().then((n) => live && setNet(n), () => undefined);
    return () => {
      live = false;
    };
  }, [net]);
  return net;
}

type RouteFeature = { id?: string | number | null; properties: { kind: string; ports: readonly string[]; chokepoints?: readonly string[] | null } };
type ChokepointFeature = { id?: string | number | null; geometry: { coordinates: number[] } | null; properties: { kind?: string; radius_km?: number | null } };

/** The lanes to draw from route entities: those with a first and a last port on the map. */
export function routeInputs(features: readonly RouteFeature[]): RouteInput[] {
  const out: RouteInput[] = [];
  for (const f of features) {
    if (f.properties.kind !== 'route' || f.id == null) continue;
    const pts = f.properties.ports.map(portPoint);
    const first = pts.findIndex(Boolean);
    const last = pts.length - 1 - [...pts].reverse().findIndex(Boolean);
    if (first < 0 || last <= first) continue;
    out.push({ id: String(f.id), origin: pts[first]!, dest: pts[last]!, chokepoints: f.properties.chokepoints ?? [] });
  }
  return out;
}

/** Chokepoint positions from /v1/map/disruptions (`chokepoint:<id>`), for ids the network has no gate for. */
export function chokepointPlaces(features: readonly ChokepointFeature[] | undefined): ChokepointPlaces {
  const out: ChokepointPlaces = {};
  for (const f of features ?? []) {
    const id = String(f.id ?? '');
    if (!id.startsWith('chokepoint:') || !f.geometry) continue;
    const [lon, lat] = f.geometry.coordinates;
    out[id.slice('chokepoint:'.length)] = { at: [lon, lat], radiusKm: f.properties.radius_km ?? null };
  }
  return out;
}

/**
 * The workspace's lanes: `entities` is /v1/map/entities as the API sends it (only route
 * features are read), `disruptions` the /v1/map/disruptions layer (optional). Null until the
 * network and the port gazetteer are loaded.
 */
export function useLanes(
  entities: { features: readonly RouteFeature[] } | null | undefined,
  disruptions?: { features: readonly ChokepointFeature[] } | null,
): Lanes | null {
  const net = useNetwork();
  const gazetteer = useGazetteer();
  return useMemo(() => {
    if (!net || !gazetteer || !entities) return null;
    return buildLanes(net, routeInputs(entities.features), chokepointPlaces(disruptions?.features));
  }, [net, gazetteer, entities, disruptions]);
}
