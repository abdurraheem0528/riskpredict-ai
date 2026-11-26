// Camera moves: the opening move, overview framing (flat and globe), flights to a selected
// entity, fits, idle rotation, and the padding that keeps content in the free area beside
// the sheet. Every move eases with the --ease curve and jumps under reduced motion.
import { useEffect, useRef } from 'react';
import type { Map as MlMap } from 'maplibre-gl';
import { EASE, clamp, later, onFrame, reducedMotion } from '../lib/motion';
import { getFocus } from './focus';
import { getProjection, type Projection } from './projection';

export type LonLat = [number, number];
export type Pad = { top: number; bottom: number; left: number; right: number };
type Box = [[number, number], [number, number]];

const isPhone = () => typeof window !== 'undefined' && window.innerWidth <= 760;

// --- padding ----------------------------------------------------------------------------------

/**
 * Padding that keeps fitted content clear of the floating sheet, the top row and the bottom
 * row (legend, map controls). On a phone the sheet is a bottom sheet: its measured top.
 */
export function sheetPadding(side: 'left' | 'right' | 'none' = 'left', width = 344): Pad {
  const w = typeof window !== 'undefined' ? window.innerWidth : 1360;
  const h = typeof window !== 'undefined' ? window.innerHeight : 900;
  if (w <= 760) {
    const sheet = typeof document !== 'undefined' ? document.querySelector('.sheet') : null;
    const top = sheet?.getBoundingClientRect().top;
    const bottom = top && top > 0 && top < h ? h - top + 16 : Math.round(h * 0.6) + 24;
    return { top: 80, bottom: Math.min(bottom, h - 200), left: 24, right: 24 };
  }
  const sheet = side === 'none' ? 40 : 20 + width + 40;
  const pad = { top: 100, bottom: 84, left: side === 'left' ? sheet : 40, right: side === 'right' ? sheet : 40 };
  // Never more padding than the map has room for (fitBounds throws).
  const spare = w - pad.left - pad.right - 160;
  if (spare < 0) {
    if (side === 'left') pad.left = Math.max(40, pad.left + spare);
    if (side === 'right') pad.right = Math.max(40, pad.right + spare);
  }
  return pad;
}

/** The inhabited world (the flat map's first view, and "fit" with nothing to fit). */
export const WORLD: Box = [[-125, -40], [150, 66]];
const NO_PAD: Pad = { top: 0, bottom: 0, left: 0, right: 0 };

/** The globe as large as the whole viewport holds it (sign-in, a new workspace). */
export function fullDisc(W: number, H: number) {
  W = W || 1360;
  H = H || 900;
  const D = 1.5 * H, rho = (Math.min(W, H) / 2) * 0.86;
  const R = (rho * rho + rho * Math.sqrt(rho * rho + D * D)) / D;
  return { center: [20, 22] as LonLat, zoom: Math.max(0.6, Math.log2((R * 2 * Math.PI * Math.cos((22 * Math.PI) / 180)) / 512)) };
}

/** The whole world behind a centred card (Empty): the full disc, or the flat world. */
export function useWorldView(map: MlMap | null) {
  useEffect(() => {
    if (!map) return;
    const go = (duration: number) => {
      if (getProjection() === 'flat') return fitTo(map, WORLD, { top: 80, bottom: 80, left: 40, right: 40 }, 3, duration);
      const c = map.getContainer();
      const cam = fullDisc(c.clientWidth, c.clientHeight);
      if (reducedMotion() || duration <= 0) map.jumpTo({ ...cam, padding: NO_PAD });
      else map.easeTo({ ...cam, padding: NO_PAD, duration, easing: EASE, essential: true });
    };
    go(800);
    return onProjectionApplied(() => go(0));
  }, [map]);
}

// --- shared camera state ------------------------------------------------------------------------

/**
 * Zoom thresholds that follow the overview zoom, so a smaller screen or the flat map still
 * shows dots, pills and chokepoints (MapRoot sets the classes `zp`, `zd`, `zl` from them).
 */
export type Ramps = { pill: number; dia: number; d0: number; d1: number; spin: number; label: number };
let ramps: Ramps = rampsFor(1.8);
const rampListeners = new Set<() => void>();
export function rampsFor(zo: number): Ramps {
  const o = clamp(zo - 2.3, -1.2, 0);
  return { pill: 1.8 + o, dia: 1.2 + o, d0: 1.5 + o, d1: 1.9 + o, spin: Math.max(3, zo + 0.4), label: 3.5 };
}
export const getRamps = () => ramps;
export function subscribeRamps(l: () => void) {
  rampListeners.add(l);
  return () => {
    rampListeners.delete(l);
  };
}
/** Recompute the ramps for this viewport and projection (entity independent). */
export function updateRamps(map: MlMap, proj: Projection = getProjection()) {
  const c = map.getContainer();
  const W = c.clientWidth || 1360, H = c.clientHeight || 900;
  const pad = sheetPadding('left', 344);
  const bw = Math.max(160, W - pad.left - pad.right), bh = Math.max(160, H - pad.top - pad.bottom);
  const zo = proj === 'globe' ? discZoom(bw, bh, H, 30, isPhone()) : Math.log2(bw / 400);
  const next = rampsFor(zo);
  if (JSON.stringify(next) === JSON.stringify(ramps)) return;
  ramps = next;
  rampListeners.forEach((l) => l());
}

/** The zoom of the last overview framing; idle rotation only runs near it. */
let overviewZoom: number | null = null;
export const getOverviewZoom = () => overviewZoom;

/** MapRoot calls `projectionApplied` once a new projection is set (under the crossfade snapshot):
 * screens jump their camera to the same framing in the new projection. */
const projListeners = new Set<(p: Projection) => void>();
export function onProjectionApplied(fn: (p: Projection) => void) {
  projListeners.add(fn);
  return () => {
    projListeners.delete(fn);
  };
}
export const projectionApplied = (p: Projection) => projListeners.forEach((l) => l(p));

/** True while the projection switches (no rotation, no opening move). */
let switching = false;
export const setSwitching = (v: boolean) => {
  switching = v;
};

// --- input ---------------------------------------------------------------------------------------

let lastInput = 0;
const inputListeners = new Set<(kind: 'down' | 'move') => void>();
/** MapRoot calls this on pointer, wheel, touch and key input (`move` for pointer moves). */
export function noteInput(kind: 'down' | 'move' = 'down') {
  lastInput = typeof performance !== 'undefined' ? performance.now() : Date.now();
  inputListeners.forEach((l) => l(kind));
}
export const sinceInput = () => (typeof performance !== 'undefined' ? performance.now() : Date.now()) - lastInput;
export const hadInput = () => lastInput > 0;
export function onInput(fn: (kind: 'down' | 'move') => void) {
  inputListeners.add(fn);
  return () => {
    inputListeners.delete(fn);
  };
}

// --- framing maths ---------------------------------------------------------------------------------

const RAD = Math.PI / 180;
export const wrap180 = (lon: number) => ((((lon + 180) % 360) + 360) % 360) - 180;

/**
 * The densest group of points one globe view can hold: the points within `radius` degrees
 * of the candidate centre that has the most of them (candidates: the points, sampled).
 */
export function densestGroup(pts: readonly LonLat[], radius = 70): LonLat[] {
  if (pts.length <= 1) return [...pts];
  const v = pts.map(([lo, la]) => [Math.cos(la * RAD) * Math.cos(lo * RAD), Math.cos(la * RAD) * Math.sin(lo * RAD), Math.sin(la * RAD)]);
  const cosR = Math.cos(radius * RAD);
  const stride = Math.max(1, Math.floor(pts.length / 400));
  let best = -1, bestN = -1;
  for (let i = 0; i < pts.length; i += stride) {
    const a = v[i];
    let n = 0;
    for (const b of v) if (a[0] * b[0] + a[1] * b[1] + a[2] * b[2] >= cosR) n++;
    if (n > bestN) {
      bestN = n;
      best = i;
    }
  }
  const a = v[best];
  return pts.filter((_, j) => a[0] * v[j][0] + a[1] * v[j][1] + a[2] * v[j][2] >= cosR);
}

/** The apparent globe radius (px) that fits a box, and the zoom that gives it at a latitude. */
function discZoom(bw: number, bh: number, H: number, lat: number, phone: boolean) {
  const D = 1.5 * H; // MapLibre's camera: 1.5 × height from the centre (fov 36.87°)
  const rho = (Math.min(bw, bh) / 2) * (phone ? 1.25 : 0.92);
  const R = (rho * rho + rho * Math.sqrt(rho * rho + D * D)) / D;
  return Math.log2((R * 2 * Math.PI * Math.cos(lat * RAD)) / 512);
}

/**
 * Globe overview: centre on the group's box and the highest zoom at which every point of
 * the group fits the free area and the whole disc stays inside it (the rest comes round
 * as the globe turns). MapLibre's globe radius is worldSize / (2π cos lat).
 */
export function globeFraming(group: readonly LonLat[], W: number, H: number, pad: Pad, phone = false, opts: { disc?: boolean; maxZoom?: number } = {}): { center: LonLat; zoom: number } {
  const bw = Math.max(160, W - pad.left - pad.right), bh = Math.max(160, H - pad.top - pad.bottom);
  if (!group.length) return { center: [20, 25], zoom: discZoom(bw, bh, H, 25, phone) };
  const ref = group[0][0];
  let w = Infinity, e = -Infinity, s = Infinity, n = -Infinity;
  for (const [x, y] of group) {
    const ux = ref + wrap180(x - ref);
    w = Math.min(w, ux); e = Math.max(e, ux); s = Math.min(s, y); n = Math.max(n, y);
  }
  const lon0 = wrap180((w + e) / 2), lat0 = clamp((s + n) / 2, -60, 60), p0 = lat0 * RAD;
  const D = 1.5 * H, hw = bw / 2 - 28, hh = bh / 2 - 28;
  let R = Infinity;
  for (const [lo, la] of group) {
    const dl = (lo - lon0) * RAD, q = la * RAD;
    const dx = Math.abs(Math.cos(q) * Math.sin(dl));
    const dy = Math.abs(Math.cos(p0) * Math.sin(q) - Math.sin(p0) * Math.cos(q) * Math.cos(dl));
    const k = 1 - (Math.sin(p0) * Math.sin(q) + Math.cos(p0) * Math.cos(q) * Math.cos(dl));
    for (const [d, h] of [[dx, hw], [dy, hh]]) {
      const den = D * d - h * k;
      if (den > 0) R = Math.min(R, (h * D) / den);
    }
  }
  if (opts.disc !== false) {
    const rho = (Math.min(bw, bh) / 2) * (phone ? 1.25 : 0.92);
    R = Math.min(R, (rho * rho + rho * Math.sqrt(rho * rho + D * D)) / D);
  }
  const zoom = clamp(Math.log2((R * 2 * Math.PI * Math.cos(p0)) / 512), 0.6, opts.maxZoom ?? 4);
  return { center: [lon0, lat0], zoom };
}

export function boxOfPoints(pts: readonly LonLat[]): Box | null {
  if (!pts.length) return null;
  let w = 180, s = 90, e = -180, n = -90;
  for (const [x, y] of pts) {
    w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y);
  }
  return [[w, s], [e, n]];
}

function safeBox(b: Box, minSpan = 2): Box {
  const [[w, s], [e, n]] = b;
  return [
    [Math.max(-180, Math.min(w, e - minSpan)), Math.max(-80, Math.min(s, n - minSpan))],
    [Math.min(180, Math.max(e, w + minSpan)), Math.min(84, Math.max(n, s + minSpan))],
  ];
}

/** The camera (centre, zoom, padding) for a box under a padding, without moving the map. */
function cameraFor(map: MlMap, b: Box, pad: Pad, maxZoom: number) {
  const cur = map.getPadding();
  try {
    map.setPadding(pad);
    const cam = map.cameraForBounds(safeBox(b), { padding: 24, maxZoom });
    return cam ? { center: cam.center as unknown as LonLat, zoom: cam.zoom } : null;
  } catch {
    return null; // padding larger than the viewport
  } finally {
    map.setPadding(cur);
  }
}

/** Where the overview sits: every placed entity (flat), or the densest group (globe). */
export function overviewCamera(map: MlMap, pts: readonly LonLat[], pad: Pad, proj: Projection = getProjection()) {
  if (!pts.length) return null;
  if (proj === 'flat') {
    const b = boxOfPoints(pts);
    return b ? cameraFor(map, b, pad, 5) : null;
  }
  const c = map.getContainer();
  return globeFraming(densestGroup(pts), c.clientWidth, c.clientHeight, pad, isPhone());
}

// --- moves ---------------------------------------------------------------------------------------

/** Fly to a point (a selected entity) so it lands in the free area; a jump under reduced motion. */
export function moveTo(map: MlMap, opts: { center: LonLat; zoom?: number; padding?: Pad; duration?: number }) {
  const { duration = 800, padding = sheetPadding(), ...cam } = opts;
  if (reducedMotion()) map.jumpTo({ ...cam, padding });
  else map.flyTo({ ...cam, padding, duration: clamp(duration, 600, 900), easing: EASE, essential: true });
}

/** The box's corners and edge midpoints, for framing it on the globe. */
function boxPoints(b: Box): LonLat[] {
  const [[w, s], [e, n]] = safeBox(b);
  const mx = (w + e) / 2, my = (s + n) / 2;
  return [[w, s], [mx, s], [e, s], [e, my], [e, n], [mx, n], [w, n], [w, my]];
}

/** Fit a box into the free area under `padding` (on the globe, with the perspective worked out exactly). */
export function fitTo(map: MlMap, b: Box, padding: Pad = sheetPadding(), maxZoom = 5, duration = 800) {
  const c = map.getContainer();
  // A box wider than a hemisphere view holds: the whole disc around its centre.
  const wide = b[1][0] - b[0][0] > 150 || b[1][1] - b[0][1] > 100;
  const cam = getProjection() === 'globe'
    ? globeFraming(boxPoints(b), c.clientWidth, c.clientHeight, padding, isPhone(), { disc: wide, maxZoom })
    : cameraFor(map, b, padding, maxZoom);
  if (!cam) return;
  if (reducedMotion() || duration <= 0) map.jumpTo({ ...cam, padding });
  else map.easeTo({ ...cam, padding, duration, easing: EASE, essential: true });
}

/** Ease to the overview of these points. */
export function toOverview(map: MlMap, pts: readonly LonLat[], pad: Pad = sheetPadding(), duration = 800) {
  const cam = overviewCamera(map, pts, pad);
  if (!cam) return;
  overviewZoom = cam.zoom ?? null;
  if (reducedMotion() || duration <= 0) map.jumpTo({ ...cam, padding: pad });
  else map.easeTo({ ...cam, padding: pad, duration, easing: EASE, essential: true });
}

let opened = false;
/** For tests. */
export const __resetOpening = () => {
  opened = false;
  lastInput = 0;
};

/**
 * The opening move, once per page load: from a wide start, after the map has loaded and
 * 400 ms more, ease over 1400 ms to the overview. Later calls (a new `key`, or coming back
 * to the screen) ease to the overview in 800 ms. Any input cancels it.
 */
export function useOpeningMove(map: MlMap | null, pts: readonly LonLat[] | null, key: string, pad: () => Pad = () => sheetPadding()) {
  const done = useRef<string | null>(null);
  const ptsRef = useRef(pts);
  const padRef = useRef(pad);
  useEffect(() => {
    ptsRef.current = pts;
    padRef.current = pad;
  });
  const ready = !!pts?.length;
  useEffect(() => {
    if (!map || !ready || done.current === key) return;
    done.current = key;
    const points = ptsRef.current ?? [];
    const p = padRef.current();
    let cancelTimer: (() => void) | null = null;
    let started = false;
    let moving = false;
    const off = onInput((kind) => {
      if (kind !== 'down') return;
      opened = true; // the visitor took over: no opening move later either
      cancelTimer?.();
      if (moving) map.stop();
    });
    const go = (duration: number) => {
      started = true;
      opened = true;
      if (switching) return;
      moving = duration > 0;
      toOverview(map, points, p, duration);
      if (moving) map.once('moveend', () => (moving = false));
    };
    if (opened || hadInput() || reducedMotion()) {
      go(reducedMotion() ? 0 : 800);
    } else {
      // MapRoot opened on a wide start (WIDE_START) when the page is the overview.
      const since = performance.now() - loadedAt(map);
      cancelTimer = later(() => go(1400), Math.max(0, 400 - since));
    }
    // The same framing in the other projection, at once (the crossfade covers the jump).
    const offProj = onProjectionApplied(() => {
      cancelTimer?.();
      if (!getFocus().selected) toOverview(map, ptsRef.current ?? points, padRef.current(), 0);
    });
    return () => {
      off();
      offProj();
      cancelTimer?.();
      if (!started) done.current = null; // unmounted (or StrictMode) before it ran: run again
    };
  }, [map, ready, key]);
}

/** Fit a box whenever it changes (easing), and again at once after a projection switch. */
export function useFit(map: MlMap | null, box: Box | null, pad: () => Pad, maxZoom = 4) {
  const padRef = useRef(pad);
  useEffect(() => {
    padRef.current = pad;
  });
  const key = box ? box.flat().map((v) => v.toFixed(3)).join(',') : '';
  useEffect(() => {
    if (!map || !box) return;
    fitTo(map, box, padRef.current(), maxZoom);
    return onProjectionApplied(() => fitTo(map, box, padRef.current(), maxZoom, 0));
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the key stands for the box
  }, [map, key, maxZoom]);
}

const loadTimes = new WeakMap<MlMap, number>();
/** MapRoot records when the map loaded. */
export const markLoaded = (map: MlMap) => loadTimes.set(map, performance.now());
const loadedAt = (map: MlMap) => loadTimes.get(map) ?? performance.now();

// --- idle rotation ---------------------------------------------------------------------------------

const IDLE_MS = 8000;
const SPIN_DEG_PER_S = 2;
const SPIN_UP_MS = 1500;

/**
 * Globe only: after 8 s with no input, nothing hovered or selected, at overview zoom, turn
 * at up to 2° of longitude a second, easing up over 1.5 s. Any input stops it at once.
 * Nothing under reduced motion. Moves carry `{ rpSpin: true }` so listeners can skip them.
 */
export function useIdleRotation(map: MlMap | null, enabled: boolean) {
  useEffect(() => {
    if (!map || !enabled) return;
    const m: MlMap = map;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let stopSpin: (() => void) | null = null;
    const allowed = () => {
      const f = getFocus();
      const maxZ = (overviewZoom ?? ramps.spin - 0.4) + 0.4;
      return !reducedMotion() && getProjection() === 'globe' && !switching && !f.hovered && !f.selected &&
        !document.hidden && m.getZoom() <= Math.min(maxZ, ramps.spin) && !m.isMoving();
    };
    const arm = (ms = Math.max(0, IDLE_MS - sinceInput())) => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(tryStart, ms);
    };
    const halt = () => {
      stopSpin?.();
      stopSpin = null;
    };
    function tryStart() {
      timer = null;
      if (sinceInput() < IDLE_MS) return arm();
      if (!allowed()) return arm(1000);
      let speed = 0;
      stopSpin = onFrame((_now, dt) => {
        if (!allowed() || sinceInput() < IDLE_MS) {
          stopSpin = null;
          arm(1000);
          return false;
        }
        speed = Math.min(1, speed + dt / SPIN_UP_MS);
        const v = SPIN_DEG_PER_S * speed * speed * (3 - 2 * speed);
        const c = m.getCenter();
        m.jumpTo({ center: [wrap180(c.lng - (v * dt) / 1000), c.lat] }, { rpSpin: true });
        return true;
      });
    }
    const off = onInput(() => {
      halt();
      arm(IDLE_MS);
    });
    arm();
    return () => {
      off();
      halt();
      if (timer) clearTimeout(timer);
    };
  }, [map, enabled]);
}
