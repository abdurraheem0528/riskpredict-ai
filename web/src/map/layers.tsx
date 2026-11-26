// Map layers as React components. Each one owns a GeoJSON source and its layers, adds
// them when it mounts, updates the data when props change and removes them on unmount,
// so a screen only renders the layers it needs.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { FeatureCollection, Point } from 'geojson';
import type { ExpressionSpecification, GeoJSONSource, GeoJSONSourceSpecification, LayerSpecification, Marker, MapLayerMouseEvent } from 'maplibre-gl';
import { useMap } from './MapRoot';
import { levelOf } from '../lib/format';
import { FONT, FONT_SEMIBOLD, raiseSymbols, useViewportAnchors, type LabelSpec } from './labels';
import { slotId, type Slot } from './slots';
import { usePalette } from '../lib/theme';

/** #rgb / #rrggbb / rgb() → [r, g, b]. */
function rgb(c: string): [number, number, number] {
  const h = c.trim();
  if (h.startsWith('#')) {
    const x = h.length === 4 ? h.slice(1).split('').map((d) => d + d).join('') : h.slice(1, 7);
    const n = parseInt(x, 16);
    return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
  }
  const m = /rgba?\(([^)]+)\)/.exec(h);
  const [r = 0, g = 0, b = 0] = m ? m[1].split(',').map((v) => Number.parseFloat(v)) : [];
  return [r, g, b];
}

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

export type LayerMeta = { click?: boolean; slot?: Slot };

/**
 * A GeoJSON source and its layers, added on mount and removed on unmount. Data changes
 * call `setData`; paint changes (a theme switch, a zoom ramp) call `setPaintProperty`, so
 * layers are only re-added when their structure changes. Layers go into `slot`'s band, or
 * each into its own `metadata.slot` band (./slots).
 */
export function useGeoJson(
  id: string,
  data: FeatureCollection | null | undefined,
  layers: LayerDef[],
  opts: Partial<GeoJSONSourceSpecification> = {},
  onClick?: (e: MapLayerMouseEvent) => void,
  /** The band of the layer stack to add the layers in (./slots); on top when omitted. */
  slot?: Slot,
) {
  const { map } = useMap();
  const optsKey = JSON.stringify(opts);
  const structKey = JSON.stringify(layers.map((l) => ({ ...l, paint: undefined })));
  const paintKey = JSON.stringify(layers.map((l) => ('paint' in l ? (l.paint ?? null) : null)));
  const click = useRef(onClick);
  const latest = useRef(layers);
  const applied = useRef<string | null>(null);
  useEffect(() => {
    click.current = onClick;
    latest.current = layers;
  });

  useEffect(() => {
    if (!map) return;
    const spec = JSON.parse(optsKey) as Partial<GeoJSONSourceSpecification>;
    const defs = latest.current;
    const before = (l: LayerDef) => {
      const s = (l as { metadata?: LayerMeta }).metadata?.slot ?? slot;
      return s && map.getLayer(slotId(s)) ? slotId(s) : undefined;
    };
    try {
      map.addSource(id, { type: 'geojson', data: EMPTY, ...spec } as GeoJSONSourceSpecification);
      for (const l of defs) map.addLayer({ ...l, source: id } as LayerSpecification, before(l));
    } catch {
      return; // the style is being replaced
    }
    applied.current = JSON.stringify(defs.map((l) => ('paint' in l ? (l.paint ?? null) : null)));
    if (defs.some((l) => l.type === 'symbol')) raiseSymbols(map);
    const handlers: [string, (e: MapLayerMouseEvent) => void, () => void, () => void][] = [];
    for (const l of defs) {
      if (!(l as { metadata?: LayerMeta }).metadata?.click) continue;
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
        applied.current = null;
      });
  }, [map, id, optsKey, structKey, slot]);

  // Paint only: set it in place.
  useEffect(() => {
    if (!map || applied.current === null || applied.current === paintKey) return;
    const prev = JSON.parse(applied.current) as (Record<string, unknown> | null)[];
    applied.current = paintKey;
    safe(() => {
      latest.current.forEach((l, i) => {
        if (!map.getLayer(l.id)) return;
        const now = ('paint' in l ? (l.paint as Record<string, unknown> | undefined) : undefined) ?? {};
        const was = prev[i] ?? {};
        for (const k of new Set([...Object.keys(was), ...Object.keys(now)])) {
          if (JSON.stringify(was[k]) !== JSON.stringify(now[k])) map.setPaintProperty(l.id, k, now[k] as never);
        }
      });
    });
  }, [map, paintKey, structKey]);

  useEffect(() => {
    if (!map) return;
    safe(() => (map.getSource(id) as GeoJSONSource | undefined)?.setData(data ?? EMPTY));
  }, [map, id, data, structKey, optsKey]);
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
        // Hidden, not faded, when the globe turns it away (MapLibre owns the root's opacity:
        // state classes go on the child node).
        m = new lib.Marker({ element: el, anchor: it.anchor ?? 'center', offset: it.offset ?? [0, 0], opacityWhenCovered: '0', subpixelPositioning: true }).setLngLat(it.at).addTo(map);
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
  const P = usePalette();
  const ink = muted ? P.off : P.ink;
  const spec = labels === 'strong' ? STRONG_LABEL : NAME_LABEL;
  const anchored = useViewportAnchors(map, labels === 'none' ? null : data, spec);
  useGeoJson(
    id,
    labels === 'none' ? data : anchored,
    [
      { id: `${id}-cluster`, type: 'circle', filter: ['has', 'point_count'], paint: { 'circle-color': ink, 'circle-opacity': 0.12, 'circle-radius': ['step', ['get', 'point_count'], 12, 10, 15, 50, 18] } },
      ...(ring
        ? [{ id: `${id}-ring`, type: 'circle', filter: ['!', ['has', 'point_count']], paint: { 'circle-color': 'rgba(0,0,0,0)', 'circle-radius': 8, 'circle-stroke-color': P.accent, 'circle-stroke-width': 1.5 } } as LayerDef]
        : []),
      {
        id: `${id}-dot`,
        type: 'circle',
        filter: ['all', ['!', ['has', 'point_count']], ['!=', ['get', 'kind'], 'site']],
        paint: { 'circle-color': ink, 'circle-radius': 4, 'circle-stroke-color': P.dotStroke, 'circle-stroke-width': 1.5 },
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
              paint: { 'text-color': labels === 'strong' ? P.ink : P.tx2, 'text-halo-color': P.halo, 'text-halo-width': 1.5 },
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
        paint: { 'text-color': muted ? P.tx2 : P.ink },
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
  const P = usePalette();
  useEffect(() => {
    if (!map) return;
    const make = (fill: string, outline: string) => {
      const s = 2;
      const size = 14 * s;
      const data = new Uint8Array(size * size * 4);
      const inner = (x: number, y: number, pad: number, r: number) => {
        const lo = pad, hi = size - 1 - pad;
        const cx = Math.min(Math.max(x, lo + r), hi - r);
        const cy = Math.min(Math.max(y, lo + r), hi - r);
        return x >= lo && x <= hi && y >= lo && y <= hi && (x - cx) ** 2 + (y - cy) ** 2 <= r * r;
      };
      const [fr, fg, fb] = rgb(fill);
      const [lr, lg, lb] = rgb(outline);
      for (let y = 0; y < size; y++)
        for (let x = 0; x < size; x++) {
          const o = (y * size + x) * 4;
          if (inner(x, y, 4, 5)) [data[o], data[o + 1], data[o + 2], data[o + 3]] = [fr, fg, fb, 255];
          else if (inner(x, y, 0, 7)) [data[o], data[o + 1], data[o + 2], data[o + 3]] = [lr, lg, lb, 255];
        }
      return { width: size, height: size, data };
    };
    safe(() => {
      for (const [id, img] of [['rp-square', make(P.ink, P.land)], ['rp-square-muted', make(P.off, P.land)]] as const) {
        if (map.hasImage(id)) map.updateImage(id, img);
        else map.addImage(id, img, { pixelRatio: 2 });
      }
    });
  }, [map, P]);
}

type PlaceProps = {
  _id: string;
  place_type: 'country' | 'port' | 'point';
  place_key: string;
  max_score: number;
  risk_count: number;
  story_count?: number;
  label?: string | null;
  top_risk_ids?: string[];
};

/**
 * The place a risk sits (Risk): a soft halo in its level's fill, with a dot. On the
 * Overview the scored entity dots, heat and tint (EntityLayers) show risks instead.
 */
export function RiskPlacesLayer({ data, onPick }: { data: FeatureCollection<Point, PlaceProps> | null; onPick?: (riskId: string) => void }) {
  const P = usePalette();
  const colored = useMemo(() => {
    if (!data) return null;
    const fill = { low: P.low, med: P.med, high: P.high, crit: P.crit };
    return {
      ...data,
      features: data.features.map((f) => ({
        ...f,
        properties: { ...f.properties, _fill: fill[levelOf(f.properties.max_score)], _top: f.properties.top_risk_ids?.[0] ?? '' },
      })),
    };
  }, [data, P]);
  useGeoJson(
    'riskplaces',
    colored,
    [
      { id: 'riskplaces-halo', type: 'circle', paint: { 'circle-color': ['get', '_fill'], 'circle-opacity': 0.2, 'circle-radius': ['interpolate', ['linear'], ['get', 'max_score'], 0, 12, 100, 18] } },
      { id: 'riskplaces-dot', type: 'circle', paint: { 'circle-color': ['get', '_fill'], 'circle-radius': 4.5, 'circle-stroke-color': P.dotStroke, 'circle-stroke-width': 2 }, metadata: { click: true } },
    ],
    {},
    (e) => {
      const id = e.features?.[0]?.properties?._top;
      if (id && onPick) onPick(String(id));
    },
    'dots',
  );
  return null;
}

/** Plain points in one colour (evidence, affected entities, preview rows). */
export function DotsLayer({ id, data, color, radius = 4, stroke, slot = 'dots' }: { id: string; data: FeatureCollection<Point> | null; color: string; radius?: number; stroke?: string; slot?: Slot }) {
  const P = usePalette();
  useGeoJson(id, data, [{ id: `${id}-dot`, type: 'circle', paint: { 'circle-color': color, 'circle-radius': radius, 'circle-stroke-color': stroke ?? P.dotStroke, 'circle-stroke-width': 1.5 } }], {}, undefined, slot);
  return null;
}
