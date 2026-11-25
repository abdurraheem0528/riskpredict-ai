// Map layers as React components. Each one owns a GeoJSON source and its layers, adds
// them when it mounts, updates the data when props change and removes them on unmount,
// so a screen only renders the layers it needs.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { FeatureCollection, Point } from 'geojson';
import type { ExpressionSpecification, GeoJSONSource, GeoJSONSourceSpecification, LayerSpecification, Marker, MapLayerMouseEvent } from 'maplibre-gl';
import { useMap } from './MapRoot';
import { LEVELS, levelOf } from '../lib/format';
import { countryName, portName } from './geo';
import { FONT, FONT_SEMIBOLD, raiseSymbols, useViewportAnchors, type LabelSpec } from './labels';

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] };
type DistOmit<T, K extends PropertyKey> = T extends unknown ? Omit<T, K> : never;
type LayerDef = DistOmit<Exclude<LayerSpecification, { type: 'background' }>, 'source'>;

function safe(fn: () => void) {
  try {
    fn();
  } catch {
    /* the map was removed first */
  }
}

export function useGeoJson(
  id: string,
  data: FeatureCollection | null | undefined,
  layers: LayerDef[],
  opts: Partial<GeoJSONSourceSpecification> = {},
  onClick?: (e: MapLayerMouseEvent) => void,
) {
  const { map } = useMap();
  const optsKey = JSON.stringify(opts);
  const layersKey = JSON.stringify(layers);
  const click = useRef(onClick);
  useEffect(() => {
    click.current = onClick;
  });

  useEffect(() => {
    if (!map) return;
    const spec = JSON.parse(optsKey) as Partial<GeoJSONSourceSpecification>;
    const defs = JSON.parse(layersKey) as LayerDef[];
    map.addSource(id, { type: 'geojson', data: EMPTY, ...spec } as GeoJSONSourceSpecification);
    for (const l of defs) map.addLayer({ ...l, source: id } as LayerSpecification);
    if (defs.some((l) => l.type === 'symbol')) raiseSymbols(map);
    const handlers: [string, (e: MapLayerMouseEvent) => void, () => void, () => void][] = [];
    for (const l of defs) {
      if (!(l as { metadata?: { click?: boolean } }).metadata?.click) continue;
      const h = (e: MapLayerMouseEvent) => click.current?.(e);
      const enter = () => (map.getCanvas().style.cursor = 'pointer');
      const leave = () => (map.getCanvas().style.cursor = '');
      map.on('click', l.id, h);
      map.on('mouseenter', l.id, enter);
      map.on('mouseleave', l.id, leave);
      handlers.push([l.id, h, enter, leave]);
    }
    return () =>
      safe(() => {
        for (const [lid, h, enter, leave] of handlers) {
          map.off('click', lid, h);
          map.off('mouseenter', lid, enter);
          map.off('mouseleave', lid, leave);
        }
        for (const l of [...defs].reverse()) if (map.getLayer(l.id)) map.removeLayer(l.id);
        if (map.getSource(id)) map.removeSource(id);
      });
  }, [map, id, optsKey, layersKey]);

  useEffect(() => {
    if (!map) return;
    safe(() => (map.getSource(id) as GeoJSONSource | undefined)?.setData(data ?? EMPTY));
  }, [map, id, data, layersKey, optsKey]);
}

// --- HTML markers (decorative pulses only; all text is drawn by MapLibre) -------------

export type MarkerItem = { key: string; at: [number, number]; node: ReactNode; anchor?: 'center' | 'left' | 'bottom' | 'bottom-left' | 'top-left'; offset?: [number, number]; z?: number };

export function Markers({ items }: { items: MarkerItem[] }) {
  const { map, lib } = useMap();
  const els = useRef(new Map<string, HTMLDivElement>());
  const markers = useRef(new Map<string, Marker>());
  const [, force] = useState(0);

  const elFor = (key: string) => {
    let el = els.current.get(key);
    if (!el) {
      el = document.createElement('div');
      els.current.set(key, el);
    }
    return el;
  };

  useEffect(() => {
    if (!map || !lib) return;
    const live = new Set(items.map((i) => i.key));
    for (const [k, m] of markers.current) {
      if (!live.has(k)) {
        m.remove();
        markers.current.delete(k);
        els.current.delete(k);
      }
    }
    for (const it of items) {
      const el = elFor(it.key);
      el.style.zIndex = String(it.z ?? 0);
      let m = markers.current.get(it.key);
      if (!m) {
        m = new lib.Marker({ element: el, anchor: it.anchor ?? 'center', offset: it.offset ?? [0, 0] }).setLngLat(it.at).addTo(map);
        markers.current.set(it.key, m);
      } else {
        m.setLngLat(it.at);
        if (it.offset) m.setOffset(it.offset);
      }
    }
  }, [map, lib, items]);

  useEffect(() => {
    const ms = markers.current;
    return () => {
      for (const m of ms.values()) m.remove();
      ms.clear();
    };
  }, []);

  useEffect(() => force((n) => n + 1), [map]);
  if (!map) return null;
  return <>{items.map((it) => createPortal(it.node, elFor(it.key), it.key))}</>;
}

// --- the layers the screens use -------------------------------------------------------

/** Global feed signals: quiet grey dots, clustered where dense (Empty, Main). */
export function SignalsLayer({ data, faint = false }: { data: FeatureCollection<Point> | null; faint?: boolean }) {
  const op = faint ? 0.55 : 1;
  useGeoJson(
    'signals',
    data,
    [
      { id: 'signals-cluster', type: 'circle', filter: ['has', 'point_count'], paint: { 'circle-color': '#7D838B', 'circle-opacity': 0.22 * op, 'circle-radius': ['step', ['get', 'point_count'], 6, 10, 9, 50, 12, 200, 15] } },
      { id: 'signals-cluster-core', type: 'circle', filter: ['has', 'point_count'], paint: { 'circle-color': '#7D838B', 'circle-opacity': op, 'circle-radius': 3.5 } },
      { id: 'signals-dot', type: 'circle', filter: ['!', ['has', 'point_count']], paint: { 'circle-color': '#7D838B', 'circle-opacity': op, 'circle-radius': ['interpolate', ['linear'], ['coalesce', ['get', 'severity'], 0], 0, 2.5, 1, 3.5] } },
    ],
    { cluster: true, clusterRadius: 28, clusterMaxZoom: 6 },
  );
  return null;
}

/** Soft pulsing rings on the strongest signals (CSS; still under reduced motion). */
export function Pulses({ points, color = '#7D838B' }: { points: { key: string; at: [number, number] }[]; color?: string }) {
  const items = useMemo(
    () =>
      points.map((p, i) => ({
        key: `pulse:${p.key}`,
        at: p.at,
        z: -1,
        node: <span className={`halo d${(i % 3) + 1}`} style={{ background: color }} aria-hidden="true" />,
      })),
    [points, color],
  );
  return <Markers items={items} />;
}

type EntityProps = { kind: string; name: string; _id: string; max_score?: number | null; open_risk_count?: number };

/** Entity names: small grey text with a white halo (the designs' `.lab`). */
const NAME_LABEL: LabelSpec = {
  font: `400 11px "${FONT}"`,
  text: (p) => (typeof p.name === 'string' && p.name ? p.name : null),
  extra: 0,
  anchors: [
    ['left', [0.9, 0]],
    ['right', [-0.9, 0]],
    ['top', [0, 0.9]],
    ['bottom', [0, -0.9]],
  ],
  box: (a, w) => (a === 'left' ? [10, -8, 10 + w, 8] : a === 'right' ? [-10 - w, -8, -10, 8] : a === 'top' ? [-w / 2, 10, w / 2, 24] : [-w / 2, -24, w / 2, -10]),
};
/** The entity a risk is about: ink, semibold, a size up. */
const STRONG_LABEL: LabelSpec = {
  ...NAME_LABEL,
  font: `600 12px "${FONT_SEMIBOLD}"`,
  box: (a, w) => (a === 'left' ? [11, -9, 11 + w, 9] : a === 'right' ? [-11 - w, -9, -11, 9] : a === 'top' ? [-w / 2, 11, w / 2, 26] : [-w / 2, -26, w / 2, -11]),
};

const visible: ExpressionSpecification = ['==', ['get', '_hide'], false];

/**
 * The business's own things: ink dots (sites as rounded squares), clustered. Cluster
 * counts and names are symbols, so names make way for counts and for each other, and are
 * hidden rather than drawn over something or past the edge of the map.
 */
export function EntitiesLayer({
  data,
  id = 'entities',
  muted = false,
  ring = false,
  labels = 'none',
  onPick,
}: {
  data: FeatureCollection<Point, EntityProps> | null;
  id?: string;
  muted?: boolean;
  ring?: boolean;
  labels?: 'none' | 'names' | 'strong';
  onPick?: (entityId: string) => void;
}) {
  const { map } = useMap();
  const ink = muted ? '#A3A7AE' : '#141518';
  const spec = labels === 'strong' ? STRONG_LABEL : NAME_LABEL;
  const anchored = useViewportAnchors(map, labels === 'none' ? null : data, spec);
  useGeoJson(
    id,
    labels === 'none' ? data : anchored,
    [
      { id: `${id}-cluster`, type: 'circle', filter: ['has', 'point_count'], paint: { 'circle-color': ink, 'circle-opacity': 0.12, 'circle-radius': ['step', ['get', 'point_count'], 12, 10, 15, 50, 18] } },
      ...(ring
        ? [{ id: `${id}-ring`, type: 'circle', filter: ['!', ['has', 'point_count']], paint: { 'circle-color': 'rgba(0,0,0,0)', 'circle-radius': 8, 'circle-stroke-color': '#1766F0', 'circle-stroke-width': 1.5 } } as LayerDef]
        : []),
      {
        id: `${id}-dot`,
        type: 'circle',
        filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'kind'], 'site']],
        paint: { 'circle-color': ink, 'circle-radius': 4, 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 1.5 },
        metadata: { click: true },
      },
      {
        id: `${id}-site`,
        type: 'symbol',
        filter: ['all', ['!', ['has', 'point_count']], ['==', ['get', 'kind'], 'site']],
        layout: { 'icon-image': muted ? 'rp-square-muted' : 'rp-square', 'icon-allow-overlap': true, 'icon-ignore-placement': true },
        metadata: { click: true },
      },
      ...(labels === 'none'
        ? []
        : [
            {
              id: `${id}-label`,
              type: 'symbol',
              filter: ['all', ['!', ['has', 'point_count']], visible],
              layout: {
                'text-field': ['get', 'name'],
                'text-font': [labels === 'strong' ? FONT_SEMIBOLD : FONT],
                'text-size': labels === 'strong' ? 12 : 11,
                'text-max-width': 30,
                'text-variable-anchor-offset': ['get', '_vao'],
                'text-justify': 'auto',
                'text-padding': 3,
                'symbol-sort-key': ['-', ['coalesce', ['get', 'max_score'], 0]],
              },
              paint: { 'text-color': labels === 'strong' ? '#141518' : '#5F646C', 'text-halo-color': '#FFFFFF', 'text-halo-width': 1.5 },
            } as LayerDef,
          ]),
      {
        id: `${id}-count`,
        type: 'symbol',
        filter: ['has', 'point_count'],
        layout: {
          // one digit gets thin spaces so its bubble stays round
          'text-field': ['case', ['<', ['get', 'point_count'], 10], ['concat', '\u2009', ['to-string', ['get', 'point_count']], '\u2009'], ['get', 'point_count_abbreviated']],
          'text-font': [FONT_SEMIBOLD],
          'text-size': 12,
          'icon-image': 'rp-bubble',
          'icon-text-fit': 'both',
          'text-allow-overlap': true,
          'icon-allow-overlap': true,
        },
        paint: { 'text-color': muted ? '#5F646C' : '#141518' },
        metadata: { click: true },
      },
    ],
    { cluster: true, clusterRadius: 36, clusterMaxZoom: 7 },
    (e) => {
      const f = e.features?.[0];
      if (!f) return;
      const cluster = f.properties?.cluster_id as number | undefined;
      if (cluster != null && map) {
        const at = (f.geometry as Point).coordinates as [number, number];
        const src = map.getSource(id) as GeoJSONSource | undefined;
        void src?.getClusterExpansionZoom(cluster).then((z) => map.easeTo({ center: at, zoom: z + 0.5 }));
        return;
      }
      if (onPick) onPick(String(f.properties?._id));
    },
  );
  useSquareImages();
  return null;
}

function useSquareImages() {
  const { map } = useMap();
  useEffect(() => {
    if (!map) return;
    const make = (fill: string) => {
      const s = 2;
      const size = 14 * s;
      const data = new Uint8Array(size * size * 4);
      const inner = (x: number, y: number, pad: number, r: number) => {
        const lo = pad, hi = size - 1 - pad;
        const cx = Math.min(Math.max(x, lo + r), hi - r);
        const cy = Math.min(Math.max(y, lo + r), hi - r);
        return x >= lo && x <= hi && y >= lo && y <= hi && (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
      };
      const [fr, fg, fb] = [1, 3, 5].map((i) => parseInt(fill.slice(i, i + 2), 16));
      for (let y = 0; y < size; y++)
        for (let x = 0; x < size; x++) {
          const o = (y * size + x) * 4;
          if (inner(x, y, 4, 5)) [data[o], data[o + 1], data[o + 2], data[o + 3]] = [fr, fg, fb, 255];
          else if (inner(x, y, 0, 7)) [data[o], data[o + 1], data[o + 2], data[o + 3]] = [255, 255, 255, 255];
        }
      return { width: size, height: size, data };
    };
    safe(() => {
      if (!map.hasImage('rp-square')) map.addImage('rp-square', make('#141518'), { pixelRatio: 2 });
      if (!map.hasImage('rp-square-muted')) map.addImage('rp-square-muted', make('#A3A7AE'), { pixelRatio: 2 });
    });
  }, [map]);
}

type PlaceProps = {
  _id: string;
  place_type: 'country' | 'port' | 'point';
  place_key: string;
  max_score: number;
  risk_count: number;
  label?: string | null;
  top_risk_ids?: string[];
};

/** Score pill + place name in a white capsule (Main.dc.html), drawn as one symbol. */
const CALLOUT_LABEL: LabelSpec = {
  font: `600 13px "${FONT_SEMIBOLD}"`,
  text: (p) => (p._co ? `${p._score}\u2002\u2002\u2002${p._label}` : null),
  extra: 26,
  anchors: [
    ['left', [2, 0]],
    ['right', [-1.85, 0]],
    ['bottom', [0, -1.77]],
    ['top', [0, 1.77]],
  ],
  box: (a, w) => (a === 'left' ? [12, -17, 12 + w, 17] : a === 'right' ? [-12 - w, -17, -12, 17] : a === 'bottom' ? [-w / 2, -48, w / 2, -14] : [-w / 2, 14, w / 2, 48]),
};

const placeName = (p: PlaceProps) =>
  p.label ?? (p.place_type === 'country' ? countryName(p.place_key) : p.place_type === 'port' ? portName(p.place_key) : `${p.risk_count} ${p.risk_count === 1 ? 'risk' : 'risks'}`);

/**
 * Risk hotspots: red halos whose strength follows the level (System-Color), with a callout
 * on the strongest few. Callouts are symbols: they give way to cluster counts and to each
 * other, flip side to stay on the map, and hide when there is no room.
 */
export function RiskPlacesLayer({
  data,
  callouts = 0,
  selected = null,
  onPick,
}: {
  data: FeatureCollection<Point, PlaceProps> | null;
  callouts?: number;
  selected?: string | null;
  onPick?: (riskId: string) => void;
}) {
  const { map } = useMap();
  const colored = useMemo(() => {
    if (!data) return null;
    const top = new Set([...data.features].sort((a, b) => b.properties.max_score - a.properties.max_score).slice(0, callouts).map((f) => f.properties._id));
    return {
      ...data,
      features: data.features.map((f) => {
        const p = f.properties;
        const ids = p.top_risk_ids ?? [];
        const lvl = levelOf(p.max_score);
        const score = String(Math.round(p.max_score));
        return {
          ...f,
          properties: {
            ...p,
            _fill: LEVELS[lvl].fill,
            _co: top.has(p._id),
            _lvl: lvl,
            _tc: LEVELS[lvl].text,
            _score: score.length === 1 ? `\u2009${score}\u2009` : score,
            _label: placeName(p),
            _on: !!selected && ids.includes(selected),
            _top: ids[0] ?? '',
          },
        };
      }),
    };
  }, [data, callouts, selected]);
  const anchored = useViewportAnchors(map, callouts ? colored : null, CALLOUT_LABEL);
  useGeoJson(
    'riskplaces',
    callouts ? anchored : colored,
    [
      { id: 'riskplaces-halo', type: 'circle', paint: { 'circle-color': ['get', '_fill'], 'circle-opacity': 0.2, 'circle-radius': ['interpolate', ['linear'], ['get', 'max_score'], 0, 12, 100, 18] } },
      { id: 'riskplaces-dot', type: 'circle', paint: { 'circle-color': ['get', '_fill'], 'circle-radius': 4.5, 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 2 }, metadata: { click: true } },
      ...(callouts
        ? [
            {
              id: 'riskplaces-callout',
              type: 'symbol',
              filter: ['all', ['==', ['get', '_co'], true], visible],
              layout: {
                'icon-image': ['concat', 'rp-callout-', ['get', '_lvl'], ['case', ['get', '_on'], '-on', '']],
                'icon-text-fit': 'both',
                'text-field': ['format', ['get', '_score'], { 'font-scale': 12 / 13, 'text-color': ['get', '_tc'] }, '\u2002\u2002\u2002', {}, ['get', '_label'], {}],
                'text-font': [FONT_SEMIBOLD],
                'text-size': 13,
                'text-max-width': 100,
                'text-variable-anchor-offset': ['get', '_vao'],
                'text-padding': 4,
                'symbol-sort-key': ['case', ['get', '_on'], -1000, ['-', ['get', 'max_score']]],
              },
              paint: { 'text-color': '#141518' },
              metadata: { click: true },
            } as LayerDef,
          ]
        : []),
    ],
    {},
    (e) => {
      const id = e.features?.[0]?.properties?._top;
      if (id && onPick) onPick(String(id));
    },
  );
  return null;
}

/** Plain points in one colour (evidence, affected entities, preview rows). */
export function DotsLayer({ id, data, color, radius = 4, stroke = '#FFFFFF' }: { id: string; data: FeatureCollection<Point> | null; color: string; radius?: number; stroke?: string }) {
  useGeoJson(id, data, [{ id: `${id}-dot`, type: 'circle', paint: { 'circle-color': color, 'circle-radius': radius, 'circle-stroke-color': stroke, 'circle-stroke-width': 1.5 } }]);
  return null;
}
