// Map layers as React components. Each one owns a GeoJSON source and its layers, adds
// them when it mounts, updates the data when props change and removes them on unmount,
// so a screen only renders the layers it needs.
import { useEffect, useMemo, useRef, useState, type ReactNode } from 'react';
import { createPortal } from 'react-dom';
import type { FeatureCollection, Point } from 'geojson';
import type { GeoJSONSource, GeoJSONSourceSpecification, LayerSpecification, Marker, MapLayerMouseEvent } from 'maplibre-gl';
import { useMap } from './MapRoot';
import { LEVELS, levelOf } from '../lib/format';

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

// --- HTML markers (callouts, cluster counts, pulses) ---------------------------------

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

/** Cluster bubbles' counts, read back from a clustered source after each move. */
export function useClusters(sourceId: string, enabled: boolean) {
  const { map } = useMap();
  const [clusters, setClusters] = useState<{ id: number; at: [number, number]; count: number }[]>([]);
  useEffect(() => {
    if (!map || !enabled) return;
    const read = () =>
      safe(() => {
        if (!map.getSource(sourceId)) return;
        const seen = new Map<number, { id: number; at: [number, number]; count: number }>();
        for (const f of map.querySourceFeatures(sourceId, { filter: ['has', 'point_count'] })) {
          const id = f.properties?.cluster_id as number;
          if (seen.has(id)) continue;
          const c = (f.geometry as Point).coordinates as [number, number];
          seen.set(id, { id, at: c, count: f.properties?.point_count as number });
        }
        setClusters([...seen.values()]);
      });
    map.on('idle', read);
    return () => {
      map.off('idle', read);
    };
  }, [map, sourceId, enabled]);
  return enabled ? clusters : [];
}

export function zoomToCluster(map: ReturnType<typeof useMap>['map'], sourceId: string, id: number, at: [number, number]) {
  if (!map) return;
  const src = map.getSource(sourceId) as GeoJSONSource | undefined;
  void src?.getClusterExpansionZoom(id).then((z) => map.easeTo({ center: at, zoom: z + 0.5 }));
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

/** The business's own things: ink dots (sites as rounded squares), clustered. */
export function EntitiesLayer({
  data,
  id = 'entities',
  muted = false,
  ring = false,
  onPick,
}: {
  data: FeatureCollection<Point, EntityProps> | null;
  id?: string;
  muted?: boolean;
  ring?: boolean;
  onPick?: (entityId: string) => void;
}) {
  const ink = muted ? '#A3A7AE' : '#141518';
  useGeoJson(
    id,
    data,
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
    ],
    { cluster: true, clusterRadius: 36, clusterMaxZoom: 7 },
    (e) => {
      const f = e.features?.[0];
      if (f && onPick) onPick(String(f.properties?._id));
    },
  );
  useSquareImages();
  const clusters = useClusters(id, true);
  const { map } = useMap();
  const items = useMemo(
    () =>
      clusters.map((c) => ({
        key: `${id}-c${c.id}`,
        at: c.at,
        node: (
          <button type="button" className={muted ? 'clus grey' : 'clus'} aria-label={`${c.count} places, zoom in`} onClick={() => zoomToCluster(map, id, c.id, c.at)}>
            {c.count}
          </button>
        ),
      })),
    [clusters, id, map, muted],
  );
  return <Markers items={items} />;
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

type PlaceProps = { _id: string; place_key: string; max_score: number; risk_count: number; label?: string | null };

/** Risk hotspots: red halos whose strength follows the level (System-Color). */
export function RiskPlacesLayer({ data }: { data: FeatureCollection<Point, PlaceProps> | null }) {
  const colored = useMemo(() => {
    if (!data) return null;
    return {
      ...data,
      features: data.features.map((f) => ({ ...f, properties: { ...f.properties, _fill: LEVELS[levelOf(f.properties.max_score)].fill } })),
    };
  }, [data]);
  useGeoJson('riskplaces', colored, [
    { id: 'riskplaces-halo', type: 'circle', paint: { 'circle-color': ['get', '_fill'], 'circle-opacity': 0.2, 'circle-radius': ['interpolate', ['linear'], ['get', 'max_score'], 0, 12, 100, 18] } },
    { id: 'riskplaces-dot', type: 'circle', paint: { 'circle-color': ['get', '_fill'], 'circle-radius': 4.5, 'circle-stroke-color': '#FFFFFF', 'circle-stroke-width': 2 } },
  ]);
  return null;
}

/** Plain points in one colour (evidence, affected entities, preview rows). */
export function DotsLayer({ id, data, color, radius = 4, stroke = '#FFFFFF' }: { id: string; data: FeatureCollection<Point> | null; color: string; radius?: number; stroke?: string }) {
  useGeoJson(id, data, [{ id: `${id}-dot`, type: 'circle', paint: { 'circle-color': color, 'circle-radius': radius, 'circle-stroke-color': stroke, 'circle-stroke-width': 1.5 } }]);
  return null;
}
