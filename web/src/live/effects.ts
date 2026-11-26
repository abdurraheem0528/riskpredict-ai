// The map half of the live sequence: a ripple where the signal is, a caption beside it, a
// thin line that runs along the great circle to each entity it reaches, and a pulse there.
// Short-lived HTML markers (opacityWhenCovered '0', so nothing shows through the globe) and
// one line source on top of the map, driven by the shared frame loop (src/lib/motion.ts).
import type { Map as MlMap, GeoJSONSource } from 'maplibre-gl';
import type { Feature, FeatureCollection, LineString } from 'geojson';
import { D3, LINEAR, clamp, onFrame, tween } from '../lib/motion';
import { fits, safeArea } from '../map/labels';

type Lib = typeof import('maplibre-gl');
export type FxMap = { map: MlMap; lib: Lib };
export type LonLat = [number, number];

const RIPPLE_MS = 1600; // 1.2 s rings, the second 250 ms later
const PULSE_MS = 1000; // a 900 ms ring
const LINE_MS = 600;
const LINE_FADE_MS = 500;
const CAPTION_MS = 3000;

function marker(fx: FxMap, at: LonLat, html: string, opts: { anchor?: 'center' | 'left' | 'right' | 'top' | 'bottom'; offset?: [number, number] } = {}) {
  const el = document.createElement('div');
  el.className = 'rp-fx';
  el.setAttribute('aria-hidden', 'true');
  el.innerHTML = html;
  // MapLibre owns the root's opacity (globe occlusion): animation classes go on the child.
  const m = new fx.lib.Marker({ element: el, anchor: opts.anchor ?? 'center', offset: opts.offset ?? [0, 0], opacityWhenCovered: '0', subpixelPositioning: true })
    .setLngLat(at)
    .addTo(fx.map);
  return { el, remove: () => m.remove() };
}

/** Two 1 px rings, 4 → 44 px over 1.2 s, the second 250 ms later. */
export function ripple(fx: FxMap, at: LonLat) {
  const m = marker(fx, at, '<span class="rp-ripple"><i></i><i></i></span>');
  setTimeout(m.remove, RIPPLE_MS);
}

/** A ring 10 → 36 px over 900 ms in the level colour. */
export function pulse(fx: FxMap, at: LonLat, color: string) {
  const m = marker(fx, at, '<span class="rp-pulse"><i></i></span>');
  (m.el.firstElementChild as HTMLElement).style.setProperty('--c', color);
  setTimeout(m.remove, PULSE_MS);
}

// --- caption ---------------------------------------------------------------------------------

type Box = [number, number, number, number];
const captions = new Set<Box>();
const SIDES: ['left' | 'right' | 'top' | 'bottom', [number, number]][] = [
  ['left', [16, 0]],
  ['right', [-16, 0]],
  ['bottom', [0, -16]],
  ['top', [0, 16]],
];

/**
 * A glass caption (28 px) beside the signal: the title, then the short source. It goes on
 * the first side clear of other captions and of the panels over the map, and leaves after
 * about 3 s.
 */
export function caption(fx: FxMap, at: LonLat, title: string, source: string | null) {
  const card = document.createElement('div');
  card.className = 'rp-cap';
  const b = document.createElement('b');
  b.textContent = title;
  card.append(b);
  if (source) {
    const s = document.createElement('span');
    s.textContent = source;
    card.append(s);
  }
  // measure it off screen
  card.style.cssText = 'position:absolute;left:-9999px;top:0;visibility:hidden';
  document.body.append(card);
  const w = card.offsetWidth, h = card.offsetHeight || 28;
  card.remove();
  card.style.cssText = '';

  const p = fx.map.project(at);
  const area = safeArea(fx.map);
  const box = ([a, [ox, oy]]: (typeof SIDES)[number]): Box => {
    const x = p.x + ox, y = p.y + oy;
    return a === 'left' ? [x, y - h / 2, x + w, y + h / 2] : a === 'right' ? [x - w, y - h / 2, x, y + h / 2] : a === 'bottom' ? [x - w / 2, y - h, x + w / 2, y] : [x - w / 2, y, x + w / 2, y + h];
  };
  // Clear of other captions, and of the pills, sites and chokepoints drawn as HTML markers.
  const origin = fx.map.getContainer().getBoundingClientRect();
  const marks: Box[] = [];
  for (const m of fx.map.getContainer().querySelectorAll<HTMLElement>('.maplibregl-marker:not(.rp-fx)')) {
    const child = (m.firstElementChild as HTMLElement | null) ?? m;
    if (m.style.opacity === '0' || getComputedStyle(child).opacity === '0') continue; // turned away, or not shown
    // the marker with what hangs off it (a chokepoint's label sits outside its diamond)
    for (const el of [child, ...child.querySelectorAll<HTMLElement>('*')]) {
      const r = el.getBoundingClientRect();
      if (r.width && r.height) marks.push([r.left - origin.left, r.top - origin.top, r.right - origin.left, r.bottom - origin.top]);
    }
  }
  const hit = (bx: Box, o: Box) => bx[0] < o[2] && bx[2] > o[0] && bx[1] < o[3] && bx[3] > o[1];
  const clear = (bx: Box) => fits(area, bx) && ![...captions, ...marks].some((o) => hit(bx, o));
  const side = SIDES.find((s) => clear(box(s))) ?? SIDES[0];
  const taken = box(side);
  captions.add(taken);

  const el = document.createElement('div');
  el.className = 'rp-fx';
  el.setAttribute('aria-hidden', 'true');
  el.style.zIndex = '10'; // over the pills
  el.append(card);
  const m = new fx.lib.Marker({ element: el, anchor: side[0], offset: side[1], opacityWhenCovered: '0', subpixelPositioning: true }).setLngLat(at).addTo(fx.map);
  setTimeout(() => {
    card.classList.add('out');
    setTimeout(() => {
      m.remove();
      captions.delete(taken);
    }, D3 + 20);
  }, CAPTION_MS - D3);
}

// --- travelling lines ----------------------------------------------------------------------

const RAD = Math.PI / 180;
type V3 = [number, number, number];
const vec = ([lon, lat]: LonLat): V3 => [Math.cos(lat * RAD) * Math.cos(lon * RAD), Math.cos(lat * RAD) * Math.sin(lon * RAD), Math.sin(lat * RAD)];
const toLonLat = ([x, y, z]: V3): LonLat => [Math.atan2(y, x) / RAD, Math.asin(clamp(z, -1, 1)) / RAD];
/** The angle between two points on the sphere, in radians. */
export const angle = (a: LonLat, b: LonLat) => {
  const A = vec(a), B = vec(b);
  return Math.acos(clamp(A[0] * B[0] + A[1] * B[1] + A[2] * B[2], -1, 1));
};
function slerp(A: V3, B: V3, t: number, om: number): V3 {
  if (om < 1e-9) return A;
  const s = Math.sin(om), k = Math.sin((1 - t) * om) / s, j = Math.sin(t * om) / s;
  return [A[0] * k + B[0] * j, A[1] * k + B[1] * j, A[2] * k + B[2] * j];
}
const near = (lon: number, ref: number) => lon + 360 * Math.round((ref - lon) / 360);

/** Points along the great circle from a to b (about one every 1.5°), longitudes unwrapped. */
export function greatCircle(a: LonLat, b: LonLat): { at: (t: number) => LonLat; points: LonLat[] } {
  const A = vec(a), B = vec(b), om = angle(a, b);
  const n = Math.max(2, Math.ceil(om / RAD / 1.5));
  const at = (t: number) => toLonLat(slerp(A, B, t, om));
  const points: LonLat[] = [];
  for (let i = 0; i <= n; i++) points.push(at(i / n));
  for (let i = 1; i < points.length; i++) points[i][0] = near(points[i][0], points[i - 1][0]);
  return { at, points };
}

type Trail = { xy: LonLat[]; c: string; o: number };
const SRC = 'rp-trails';
const trails = new Map<MlMap, Set<Trail>>();
const dirty = new Set<MlMap>();
let flushing: (() => void) | null = null;

function ensureLayer(map: MlMap): boolean {
  try {
    if (!map.getSource(SRC)) {
      map.addSource(SRC, { type: 'geojson', data: { type: 'FeatureCollection', features: [] } });
      // on top of everything (no band): the line runs over lanes, heat and dots
      map.addLayer({
        id: SRC,
        type: 'line',
        source: SRC,
        layout: { 'line-join': 'round', 'line-cap': 'round' },
        paint: { 'line-color': ['get', 'c'], 'line-width': 1.5, 'line-opacity': ['get', 'o'] },
      });
    }
    return true;
  } catch {
    return false; // the style is not ready, or the map was removed
  }
}

function flushTrails() {
  for (const map of dirty) {
    const set = trails.get(map);
    const features: Feature<LineString, { c: string; o: number }>[] = [...(set ?? [])].map((t) => ({
      type: 'Feature',
      properties: { c: t.c, o: t.o },
      geometry: { type: 'LineString', coordinates: t.xy },
    }));
    try {
      (map.getSource(SRC) as GeoJSONSource | undefined)?.setData({ type: 'FeatureCollection', features } as FeatureCollection);
    } catch {
      /* removed */
    }
  }
  dirty.clear();
}

function markDirty(map: MlMap) {
  dirty.add(map);
  if (flushing) return;
  // one setData per frame, after every tween of that frame has stepped
  flushing = onFrame(() => {
    flushTrails();
    flushing = null;
    return false;
  });
}

/**
 * A thin line from `a` to `b` along the great circle, drawn over 600 ms; `done` runs when it
 * arrives, then it fades over 500 ms. Without a line layer it just calls `done`.
 */
export function travel(fx: FxMap, a: LonLat, b: LonLat, color: string, done: () => void) {
  if (!ensureLayer(fx.map)) {
    done();
    return;
  }
  const { at, points } = greatCircle(a, b);
  const n = points.length - 1;
  const tr: Trail = { xy: [points[0], points[0]], c: color, o: 0.9 };
  let set = trails.get(fx.map);
  if (!set) trails.set(fx.map, (set = new Set()));
  set.add(tr);
  const s = set;
  tween(
    LINE_MS,
    (p) => {
      const k = Math.min(n - 1, Math.floor(p * n));
      const head = at(p);
      head[0] = near(head[0], points[k][0]);
      tr.xy = points.slice(0, k + 1).concat([head]);
      markDirty(fx.map);
    },
    {
      done: () => {
        tr.xy = points;
        done();
        tween(
          LINE_FADE_MS,
          (p) => {
            tr.o = 0.9 * (1 - p);
            markDirty(fx.map);
          },
          {
            ease: LINEAR,
            done: () => {
              s.delete(tr);
              markDirty(fx.map);
            },
          },
        );
      },
    },
  );
}
