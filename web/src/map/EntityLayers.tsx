// The business on the map, in the globe look: countries with open stories tinted, a heat
// of entity max scores, dots by score (Low grey, Medium and up coloured), a shadow under a
// hovered dot, a selection ring, glass pills on the highest few, city labels close up, and
// sites as small ink squares. Hover, selection and dimming are feature-state tweens; the
// list and the map share hover and selection through ./focus.
import { useEffect, useMemo, useRef, useSyncExternalStore } from 'react';
import type { Feature, FeatureCollection, Point } from 'geojson';
import type { ExpressionSpecification, Map as MlMap, MapLayerMouseEvent, Marker } from 'maplibre-gl';
import { useMap } from './MapRoot';
import { useGeoJson } from './layers';
import { countryName, portName } from './geo';
import { FONT, safeArea, fits } from './labels';
import { slotId } from './slots';
import { getFocus, setHovered, setSelected, useFocus } from './focus';
import { forgetSource, setState, tweenState } from './fstate';
import { getRamps, subscribeRamps, type Ramps } from './camera';
import { usePalette, type Palette } from '../lib/theme';
import { D2, D3, later } from '../lib/motion';
import { levelOf, scoreText, type Level } from '../lib/format';

export type EntityProps = {
  _id: string;
  kind: string;
  name: string;
  countries?: string[];
  ports?: string[];
  city?: string | null;
  criticality?: string | null;
  max_score?: number | null;
  open_risk_count?: number;
};
type EntityFc = FeatureCollection<Point, EntityProps>;

const SRC = 'ents';
const useRamps = () => useSyncExternalStore(subscribeRamps, getRamps, getRamps);

/** Score an entity is shown at: its highest open score, 0 when it has no open risk. */
export const shownScore = (p: Pick<EntityProps, 'max_score' | 'open_risk_count'>) =>
  (p.open_risk_count ?? 0) > 0 && p.max_score != null ? p.max_score : 0;

/** Level an entity is drawn in (Low is neutral grey). */
export const shownLevel = (p: Pick<EntityProps, 'max_score' | 'open_risk_count'>): Level => levelOf(shownScore(p));

/** The short place on a pill: the city, else the first port or country, else the name. */
export function pillPlace(p: Pick<EntityProps, 'city' | 'ports' | 'countries' | 'name'>): string {
  if (p.city) return p.city;
  if (p.ports?.length) return portName(p.ports[0]);
  if (p.countries?.length) return countryName(p.countries[0]);
  return p.name;
}

/** Tint opacity for a country's highest score (before × --tint-k). */
export function tintOpacity(score: number): number {
  return levelOf(score) === 'low' ? 0.07 : 0.06 + 0.04 * Math.min(1, Math.max(0, (score - 25) / 24));
}

/** Countries with open stories → the highest entity score there. */
export function countryScores(fc: EntityFc | null): Map<string, number> {
  const out = new Map<string, number>();
  for (const f of fc?.features ?? []) {
    const s = shownScore(f.properties);
    if (s <= 0) continue;
    for (const c of f.properties.countries?.slice(0, 1) ?? []) out.set(c, Math.max(out.get(c) ?? 0, s));
  }
  return out;
}

/** The entities with a pill at overview zoom: the `n` highest scores with an open risk. */
export function topEntities(fc: EntityFc | null, n = 3, keep: Set<string> | null = null): string[] {
  return [...(fc?.features ?? [])]
    .filter((f) => shownScore(f.properties) > 0 && f.properties.kind !== 'site' && (!keep || keep.has(f.properties._id)))
    .sort((a, b) => shownScore(b.properties) - shownScore(a.properties) || a.properties._id.localeCompare(b.properties._id))
    .slice(0, n)
    .map((f) => f.properties._id);
}

const fill = (P: Palette): ExpressionSpecification => ['match', ['get', '_lvl'], 'crit', P.crit, 'high', P.high, 'med', P.med, P.low];
const st = (k: string): ExpressionSpecification => ['coalesce', ['feature-state', k], 0];
const R0: ExpressionSpecification = ['interpolate', ['linear'], ['get', '_score'], 10, 4, 50, 7];

/** Hex (#rgb / #rrggbb) or rgb() → rgba() with an alpha, for the heat ramp. */
export function withAlpha(c: string, a: number): string {
  const h = c.trim();
  if (h.startsWith('#')) {
    const x = h.length === 4 ? h.slice(1).split('').map((d) => d + d).join('') : h.slice(1, 7);
    const n = parseInt(x, 16);
    return `rgba(${(n >> 16) & 255},${(n >> 8) & 255},${n & 255},${a})`;
  }
  const m = /rgba?\(([^)]+)\)/.exec(h);
  if (m) {
    const [r, g, b] = m[1].split(',').map((v) => v.trim());
    return `rgba(${r},${g},${b},${a})`;
  }
  return h;
}

function layersFor(P: Palette, T: Ramps, heat: boolean) {
  const zr = (v: ExpressionSpecification | number): ExpressionSpecification => ['interpolate', ['linear'], ['zoom'], T.d0, 0, T.d1, v];
  const vis: ExpressionSpecification = ['-', 1, ['*', 0.75, st('dim')]];
  const notSite: ExpressionSpecification = ['!=', ['get', 'kind'], 'site'];
  return [
    ...(heat
      ? [{
          id: `${SRC}-heat`, type: 'heatmap' as const, filter: ['>', ['get', '_score'], 0] as ExpressionSpecification, metadata: { slot: 'heat' },
          paint: {
            'heatmap-weight': ['interpolate', ['linear'], ['get', '_score'], 0, 0, 20, 0.12, 50, 1] as ExpressionSpecification,
            'heatmap-intensity': ['interpolate', ['linear'], ['zoom'], 0, 0.9, 4, 1.6] as ExpressionSpecification,
            'heatmap-radius': ['interpolate', ['linear'], ['zoom'], 0, 14, 2, 24, 4, 40] as ExpressionSpecification,
            'heatmap-color': ['interpolate', ['linear'], ['heatmap-density'], 0, withAlpha(P.med, 0), 0.2, withAlpha(P.med, 0.3), 0.5, withAlpha(P.med, 0.6), 0.8, withAlpha(P.high, 0.72), 1, withAlpha(P.high, 0.85)] as ExpressionSpecification,
            'heatmap-opacity': ['interpolate', ['linear'], ['zoom'], 1, 0.8, 4, 0] as ExpressionSpecification,
          },
        }]
      : []),
    {
      id: `${SRC}-shadow`, type: 'circle' as const, filter: notSite, metadata: { slot: 'dots' },
      // A neutral shadow, not a themed colour: black at low opacity reads on both lands.
      paint: { 'circle-color': 'rgba(0,0,0,1)', 'circle-blur': 1, 'circle-translate': [0, 2] as [number, number], 'circle-radius': ['+', R0, 2, ['*', 4, st('h')]] as ExpressionSpecification, 'circle-opacity': zr(['*', 0.28, st('h')]) },
    },
    {
      id: `${SRC}-ring`, type: 'circle' as const, filter: notSite, metadata: { slot: 'dots' },
      paint: {
        'circle-color': P.accent, 'circle-opacity': zr(['*', 0.12, st('sel')]),
        'circle-radius': ['+', R0, 9, ['*', -3, st('sel')]] as ExpressionSpecification,
        'circle-stroke-color': P.accent, 'circle-stroke-width': 2, 'circle-stroke-opacity': zr(st('sel')),
      },
    },
    {
      id: `${SRC}-dot`, type: 'circle' as const, filter: notSite, metadata: { slot: 'dots' },
      layout: { 'circle-sort-key': ['get', '_score'] as ExpressionSpecification },
      paint: {
        'circle-color': fill(P),
        'circle-radius': ['+', R0, ['*', 2.5, st('h')], st('sel')] as ExpressionSpecification,
        'circle-stroke-color': P.dotStroke,
        'circle-stroke-width': ['+', 2, ['*', 0.5, st('h')]] as ExpressionSpecification,
        'circle-opacity': zr(vis), 'circle-stroke-opacity': zr(vis),
      },
    },
    {
      // Hit area for hover and click, live once the dots have faded in (paint, so a new ramp
      // does not re-add the layers and lose their feature-state).
      id: `${SRC}-hit`, type: 'circle' as const, metadata: { slot: 'dots' },
      paint: { 'circle-radius': ['step', ['zoom'], 0, Math.max(0, T.d0 + 0.2), 12] as ExpressionSpecification, 'circle-opacity': 0 },
    },
    {
      id: `${SRC}-label`, type: 'symbol' as const, minzoom: T.label, filter: notSite, metadata: { slot: 'labels' },
      layout: {
        'text-field': ['get', '_place'] as ExpressionSpecification,
        'text-font': [FONT],
        'text-size': 11,
        'text-variable-anchor': ['left', 'right', 'top', 'bottom'] as ('left' | 'right' | 'top' | 'bottom')[],
        'text-radial-offset': 0.9,
        'text-justify': 'auto' as const,
        'text-padding': 3,
        'symbol-sort-key': ['-', ['get', '_score']] as ExpressionSpecification,
      },
      paint: {
        'text-color': P.tx2, 'text-halo-color': P.halo, 'text-halo-width': 1.5,
        // hidden under a pill, faint when filtered out
        'text-opacity': ['*', ['-', 1, st('pill')], ['-', 1, ['*', 0.75, st('dim')]]] as ExpressionSpecification,
      },
    },
  ];
}

/** Countries with open stories, tinted in their level's fill (the base `countries` source). */
function CountryTint({ data }: { data: EntityFc | null }) {
  const { map } = useMap();
  const P = usePalette();
  const scores = useMemo(() => countryScores(data), [data]);
  useEffect(() => {
    if (!map) return;
    const id = 'country-tint';
    try {
      if (!map.getLayer(id)) map.addLayer({ id, type: 'fill', source: 'countries', filter: ['==', ['get', 'iso'], ''], paint: { 'fill-opacity': 0 } }, map.getLayer(slotId('tint')) ? slotId('tint') : undefined);
      const isos = [...scores.keys()];
      if (!isos.length) {
        map.setFilter(id, ['==', ['get', 'iso'], '']);
        return;
      }
      const color: unknown[] = ['match', ['get', 'iso']];
      const op: unknown[] = ['match', ['get', 'iso']];
      for (const [iso, s] of scores) {
        const lvl = levelOf(s);
        color.push(iso, lvl === 'crit' ? P.crit : lvl === 'high' ? P.high : lvl === 'med' ? P.med : P.low);
        op.push(iso, tintOpacity(s) * P.tintK);
      }
      color.push(P.low);
      op.push(0);
      map.setFilter(id, ['in', ['get', 'iso'], ['literal', isos]]);
      map.setPaintProperty(id, 'fill-color', color as ExpressionSpecification);
      map.setPaintProperty(id, 'fill-opacity', op as ExpressionSpecification);
    } catch {
      /* style not ready */
    }
  }, [map, scores, P]);
  useEffect(
    () => () => {
      try {
        if (map?.getLayer('country-tint')) map.removeLayer('country-tint');
      } catch {
        /* map removed */
      }
    },
    [map],
  );
  return null;
}

/**
 * The entity layers. `dimmed`: ids kept bright (the rest fade to 25%), or null for none.
 * `pills`: how many of the highest scores get a pill at overview zoom.
 */
export function EntityMap({
  data,
  dimmed = null,
  pills = 3,
  heat = true,
  tint = true,
  onPick,
}: {
  data: EntityFc | null;
  dimmed?: Set<string> | null;
  pills?: number;
  heat?: boolean;
  tint?: boolean;
  onPick?: (entityId: string) => void;
}) {
  const { map } = useMap();
  const P = usePalette();
  const T = useRamps();
  const fc = useMemo<EntityFc | null>(() => {
    if (!data) return null;
    return {
      ...data,
      features: data.features.map((f): Feature<Point, EntityProps & { _score: number; _lvl: Level; _place: string }> => ({
        ...f,
        properties: { ...f.properties, _score: shownScore(f.properties), _lvl: shownLevel(f.properties), _place: pillPlace(f.properties) },
      })),
    };
  }, [data]);
  const layers = useMemo(() => layersFor(P, T, heat), [P, T, heat]);
  const pick = useRef(onPick);
  useEffect(() => {
    pick.current = onPick;
  });
  useGeoJson(SRC, fc, layers, { promoteId: '_id' });

  // Hover and click on the dots.
  useEffect(() => {
    if (!map) return;
    const hit = `${SRC}-hit`;
    let cur: string | null = null;
    const move = (e: MapLayerMouseEvent) => {
      const f = e.features?.[0];
      const id = f ? String(f.properties?._id) : null;
      if (id === cur) return;
      cur = id;
      map.getCanvas().style.cursor = id ? 'pointer' : '';
      setHovered(id, 'map');
    };
    const leave = () => {
      cur = null;
      map.getCanvas().style.cursor = '';
      if (getFocus().source === 'map') setHovered(null, 'map');
    };
    const click = (e: MapLayerMouseEvent) => {
      const id = e.features?.[0]?.properties?._id;
      if (id == null) return;
      setSelected(String(id), 'map');
      pick.current?.(String(id));
    };
    map.on('mousemove', hit, move);
    map.on('mouseleave', hit, leave);
    map.on('click', hit, click);
    return () => {
      try {
        map.off('mousemove', hit, move);
        map.off('mouseleave', hit, leave);
        map.off('click', hit, click);
      } catch {
        /* removed */
      }
    };
  }, [map]);

  // Hover lift (200 ms) and selection ring (320 ms) follow ./focus.
  const focus = useFocus();
  const prev = useRef<{ h: string | null; s: string | null }>({ h: null, s: null });
  useEffect(() => {
    if (!map) return;
    const p = prev.current;
    if (p.h !== focus.hovered) {
      if (p.h) tweenState(map, SRC, p.h, 'h', 0, D2);
      if (focus.hovered) tweenState(map, SRC, focus.hovered, 'h', 1, D2);
    }
    if (p.s !== focus.selected) {
      if (p.s) tweenState(map, SRC, p.s, 'sel', 0, D3);
      if (focus.selected) tweenState(map, SRC, focus.selected, 'sel', 1, D3);
    }
    prev.current = { h: focus.hovered, s: focus.selected };
  }, [map, focus.hovered, focus.selected]);

  // Filter dimming (200 ms): tweened when few features change, set at once when many do.
  const dimKey = dimmed ? [...dimmed].sort().join(',') : '';
  const dimNow = useRef(new Map<string, number>());
  useEffect(() => {
    if (!map || !fc) return;
    const changes: [string, number][] = [];
    for (const f of fc.features) {
      const id = f.properties._id;
      const to = dimmed && !dimmed.has(id) ? 1 : 0;
      if ((dimNow.current.get(id) ?? 0) !== to) changes.push([id, to]);
      dimNow.current.set(id, to);
    }
    const tweenIt = changes.length <= 400;
    for (const [id, to] of changes) {
      if (tweenIt) tweenState(map, SRC, id, 'dim', to, D2);
      else setState(map, SRC, id, 'dim', to);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps -- dimKey stands for `dimmed`
  }, [map, fc, dimKey]);

  useEffect(
    () => () => {
      forgetSource(SRC);
    },
    [],
  );

  const top = useMemo(() => topEntities(fc, pills, dimmed), [fc, pills, dimmed]);
  return (
    <>
      {tint && <CountryTint data={fc} />}
      <Pills data={fc} top={top} onPick={(id) => pick.current?.(id)} />
      <Sites data={fc} />
    </>
  );
}

// --- pills (HTML markers) -------------------------------------------------------------------------

type PillRec = { m: Marker; root: HTMLDivElement; btn: HTMLButtonElement; at: [number, number]; gone: (() => void) | null; side: number };
const SIDES: [number, number][] = [[1, 0], [-1, 0], [0, -1], [0, 1]]; // right of the dot, left, above, below

function pillOffset(side: number, w: number, h: number): [number, number] {
  const [x, y] = SIDES[side];
  return [x * (12 + w / 2), y * (18 + h / 2)];
}

/** Glass pills: the score in its level pill, then the city. Not in the tab order (the list has the same action). */
function Pills({ data, top, onPick }: { data: EntityFc | null; top: string[]; onPick: (id: string) => void }) {
  const { map, lib } = useMap();
  const recs = useRef(new Map<string, PillRec>());
  const focus = useFocus();
  const byId = useMemo(() => new Map((data?.features ?? []).map((f) => [f.properties._id, f])), [data]);
  const pickRef = useRef(onPick);
  useEffect(() => {
    pickRef.current = onPick;
  });

  // Which pills exist, and their content.
  useEffect(() => {
    if (!map || !lib) return;
    const want = new Set(top);
    if (focus.hovered && byId.has(focus.hovered)) want.add(focus.hovered);
    if (focus.selected && byId.has(focus.selected)) want.add(focus.selected);
    for (const [id, r] of recs.current) {
      if (want.has(id) && byId.has(id)) continue;
      if (r.gone) continue;
      r.btn.classList.remove('show', 'force', 'on');
      setState(map, SRC, id, 'pill', 0);
      r.gone = later(() => {
        r.m.remove();
        recs.current.delete(id);
      }, D2);
    }
    for (const id of want) {
      const f = byId.get(id);
      if (!f) continue;
      let r = recs.current.get(id);
      const at = f.geometry.coordinates as [number, number];
      if (!r) {
        const root = document.createElement('div');
        root.className = 'mk';
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.tabIndex = -1;
        btn.className = 'mpill';
        btn.setAttribute('aria-hidden', 'true');
        const pn = document.createElement('span');
        const pc = document.createElement('span');
        pc.className = 'pc';
        btn.append(pn, pc);
        root.append(btn);
        // Keep MapLibre's own layer hover from firing a mouseleave under the pill.
        btn.addEventListener('mousemove', (e) => e.stopPropagation());
        btn.addEventListener('mouseenter', () => setHovered(id, 'map'));
        btn.addEventListener('mouseleave', () => {
          if (getFocus().hovered === id) setHovered(null, 'map');
        });
        btn.addEventListener('click', (e) => {
          e.stopPropagation();
          setSelected(id, 'map');
          pickRef.current(id);
        });
        const m = new lib.Marker({ element: root, anchor: 'center', offset: pillOffset(0, 120, 30), opacityWhenCovered: '0', subpixelPositioning: true }).setLngLat(at).addTo(map);
        r = { m, root, btn, at, gone: null, side: -1 };
        recs.current.set(id, r);
        // next frame, so the fade and scale-in runs
        const rec = r;
        requestAnimationFrame(() => {
          if (!rec.gone) rec.btn.classList.add('show');
        });
      } else if (r.gone) {
        r.gone();
        r.gone = null;
        r.btn.classList.add('show');
      }
      if (r.at[0] !== at[0] || r.at[1] !== at[1]) {
        r.m.setLngLat(at);
        r.at = at;
      }
      const p = f.properties;
      const score = shownScore(p);
      const [pn, pc] = r.btn.children as unknown as [HTMLSpanElement, HTMLSpanElement];
      pn.className = `pn l-${levelOf(score)}`;
      pn.textContent = scoreText(score);
      pc.textContent = pillPlace(p);
      const force = id === focus.hovered || id === focus.selected;
      r.btn.classList.toggle('force', force);
      r.btn.classList.toggle('on', id === focus.selected);
      if (r.btn.classList.contains('show') || !r.gone) r.btn.classList.add('show');
      r.root.style.zIndex = force ? '6' : '4';
      setState(map, SRC, id, 'pill', 1);
    }
    place(map, recs.current);
  }, [map, lib, top, byId, focus.hovered, focus.selected]);

  // Keep pills inside the open map and off each other: re-place after moves (not every rotation frame).
  useEffect(() => {
    if (!map) return;
    let t: ReturnType<typeof setTimeout> | null = null;
    const onEnd = (e: unknown) => {
      if (t) clearTimeout(t);
      t = setTimeout(() => place(map, recs.current), (e as { rpSpin?: boolean })?.rpSpin ? 500 : 0);
    };
    map.on('moveend', onEnd);
    map.on('resize', onEnd);
    return () => {
      if (t) clearTimeout(t);
      map.off('moveend', onEnd);
      map.off('resize', onEnd);
    };
  }, [map]);

  useEffect(() => {
    const rs = recs.current;
    return () => {
      for (const r of rs.values()) {
        r.gone?.();
        r.m.remove();
      }
      rs.clear();
    };
  }, []);

  return null;
}

/** Choose each pill's side: the first that stays in the open map and clear of the pills placed before it. */
function place(map: MlMap, recs: Map<string, PillRec>) {
  const area = safeArea(map);
  const boxes: [number, number, number, number][] = [];
  // selected and hovered first, so they get the best side
  const order = [...recs.entries()].filter(([, r]) => !r.gone).sort(([, a], [, b]) => Number(b.btn.classList.contains('force')) - Number(a.btn.classList.contains('force')));
  for (const [, r] of order) {
    const w = r.btn.offsetWidth || 110, h = r.btn.offsetHeight || 30;
    const p = map.project(r.at);
    // The first side that stays in the open map and clear of the pills placed before it;
    // else the one that overlaps least.
    let side = 0;
    let best = Infinity;
    for (let s = 0; s < SIDES.length; s++) {
      const [ox, oy] = pillOffset(s, w, h);
      const b: [number, number, number, number] = [p.x + ox - w / 2, p.y + oy - h / 2, p.x + ox + w / 2, p.y + oy + h / 2];
      let cost = fits(area, b) ? 0 : 1e6;
      for (const o of boxes) cost += Math.max(0, Math.min(b[2], o[2]) - Math.max(b[0], o[0])) * Math.max(0, Math.min(b[3], o[3]) - Math.max(b[1], o[1]));
      if (cost < best) {
        best = cost;
        side = s;
      }
      if (cost === 0) break;
    }
    const [ox, oy] = pillOffset(side, w, h);
    boxes.push([p.x + ox - w / 2, p.y + oy - h / 2, p.x + ox + w / 2, p.y + oy + h / 2]);
    if (side !== r.side) {
      r.side = side;
      r.m.setOffset([ox, oy]);
    }
  }
}

// --- sites -------------------------------------------------------------------------------------------

const MAX_SITE_MARKERS = 60;
const CRITICALITY_RANK: Record<string, number> = { critical: 0, high: 1, medium: 2, low: 3 };

/** Sites in the order their labels claim room: the more critical first, then by name. */
export function siteLabelOrder<T extends { properties: Pick<EntityProps, 'name' | 'criticality'> }>(sites: readonly T[]): T[] {
  const rank = (p: T['properties']) => CRITICALITY_RANK[p.criticality ?? ''] ?? 4;
  return [...sites].sort((a, b) => rank(a.properties) - rank(b.properties) || a.properties.name.localeCompare(b.properties.name));
}

/** A site label's box on screen, from the square's centre (map.css `.msite .slab`: 8 px below, 16 px tall). */
export const siteLabelBox = (x: number, y: number, w: number): [number, number, number, number] => [x - w / 2, y + 8, x + w / 2, y + 24];

/**
 * Which site labels to show, given in label order (siteLabelOrder): each one whose box (with
 * `pad` px round it) stays clear of the labels shown before it. The others hide until the map
 * zooms in far enough to part them; the squares always show.
 */
function placeLabels(map: MlMap, sites: readonly Feature<Point, EntityProps>[], recs: Map<string, { lab: HTMLSpanElement; at: [number, number] }>) {
  const items: { id: string; box: [number, number, number, number] }[] = [];
  for (const f of sites) {
    const r = recs.get(f.properties._id);
    if (!r) continue;
    const p = map.project(r.at);
    items.push({ id: f.properties._id, box: siteLabelBox(p.x, p.y, r.lab.offsetWidth || 7 * f.properties.name.length) });
  }
  const shown = placeSiteLabels(items);
  for (const { id } of items) recs.get(id)!.lab.classList.toggle('hid', !shown.has(id));
}

export function placeSiteLabels(items: readonly { id: string; box: [number, number, number, number] }[], pad = 3): Set<string> {
  const shown = new Set<string>();
  const taken: [number, number, number, number][] = [];
  for (const { id, box: [x0, y0, x1, y1] } of items) {
    if (taken.some(([a0, b0, a1, b1]) => x0 - pad < a1 && a0 < x1 + pad && y0 - pad < b1 && b0 < y1 + pad)) continue;
    taken.push([x0, y0, x1, y1]);
    shown.add(id);
  }
  return shown;
}

/** The business's sites: 10 px rounded ink squares with a land outline and a label from the pill zoom. */
function Sites({ data }: { data: EntityFc | null }) {
  const { map, lib } = useMap();
  const P = usePalette();
  const sites = useMemo(() => siteLabelOrder((data?.features ?? []).filter((f) => f.properties.kind === 'site')), [data]);
  const many = sites.length > MAX_SITE_MARKERS;
  const recs = useRef(new Map<string, { m: Marker; lab: HTMLSpanElement; at: [number, number] }>());

  useEffect(() => {
    if (!map || !lib) return;
    const live = new Set(many ? [] : sites.map((f) => f.properties._id));
    for (const [id, r] of recs.current) if (!live.has(id)) {
      r.m.remove();
      recs.current.delete(id);
    }
    if (many) return;
    for (const f of sites) {
      const id = f.properties._id;
      const at = f.geometry.coordinates as [number, number];
      let r = recs.current.get(id);
      if (!r) {
        const root = document.createElement('div');
        root.className = 'mk';
        root.setAttribute('aria-hidden', 'true');
        const sq = document.createElement('span');
        sq.className = 'msite';
        const i = document.createElement('i');
        const lab = document.createElement('span');
        lab.className = 'slab';
        sq.append(i, lab);
        root.append(sq);
        sq.addEventListener('mouseenter', () => setHovered(id, 'map'));
        sq.addEventListener('mouseleave', () => {
          if (getFocus().hovered === id) setHovered(null, 'map');
        });
        const m = new lib.Marker({ element: root, anchor: 'center', opacityWhenCovered: '0', subpixelPositioning: true }).setLngLat(at).addTo(map);
        root.style.zIndex = '2';
        r = { m, lab, at };
        recs.current.set(id, r);
      } else {
        r.m.setLngLat(at);
        r.at = at;
      }
      r.lab.textContent = f.properties.name;
    }
    placeLabels(map, sites, recs.current);
  }, [map, lib, sites, many]);

  // Labels that would land on each other: the more critical site keeps its label, re-checked
  // after every move (two sites 90 km apart share one spot at world zoom and part as it zooms in).
  useEffect(() => {
    if (!map || many) return;
    let t: ReturnType<typeof setTimeout> | null = null;
    // as the pills: not on every frame of the slow spin
    const onEnd = (e: unknown) => {
      if (t) clearTimeout(t);
      t = setTimeout(() => placeLabels(map, sites, recs.current), (e as { rpSpin?: boolean })?.rpSpin ? 500 : 0);
    };
    map.on('moveend', onEnd);
    map.on('resize', onEnd);
    return () => {
      if (t) clearTimeout(t);
      map.off('moveend', onEnd);
      map.off('resize', onEnd);
    };
  }, [map, sites, many]);

  useEffect(() => {
    const rs = recs.current;
    return () => {
      for (const r of rs.values()) r.m.remove();
      rs.clear();
    };
  }, []);

  // Many sites: a symbol layer instead (the rp-square image, MapLibre-drawn names, which make
  // way for each other in the same order).
  const T = useRamps();
  const fc = useMemo(
    () => (many ? { type: 'FeatureCollection' as const, features: sites.map((f, i) => ({ ...f, properties: { ...f.properties, _order: i } })) } : null),
    [many, sites],
  );
  useGeoJson(
    'sites',
    fc,
    many
      ? [{
          id: 'sites-sym', type: 'symbol', metadata: { slot: 'labels' },
          layout: {
            'icon-image': 'rp-square', 'icon-allow-overlap': true,
            'text-field': ['step', ['zoom'], '', T.pill, ['get', 'name']] as ExpressionSpecification,
            'text-font': [FONT], 'text-size': 12, 'text-anchor': 'top', 'text-offset': [0, 0.9] as [number, number], 'text-optional': true,
            'symbol-sort-key': ['get', '_order'],
          },
          paint: { 'text-color': P.ink, 'text-halo-color': P.halo, 'text-halo-width': 1.5 },
        }]
      : [],
  );
  return null;
}
