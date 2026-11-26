// Feed signals that matched nothing of yours: quiet grey dots in the signal colour, fading
// with age (full when new, about 30% at seven days). After `feeds.updated` refetches the
// map events, the new ones fade in over 600 ms, at most 24 at a time; nothing else
// happens. They sit at the bottom of the dots band, under your entities.
import { useEffect, useMemo, useRef } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { useMap } from '../map/MapRoot';
import { useGeoJson } from '../map/layers';
import { slotId } from '../map/slots';
import { usePalette } from '../lib/theme';
import { D4, motionOff, tween } from '../lib/motion';
import { freshDots, recency } from './plan';

type DotProps = { _id: string; published_at?: string | null; severity?: number | null };

export function FeedDots({ data, id = 'feeddots' }: { data: FeatureCollection<Point, DotProps> | null; id?: string }) {
  const { map } = useMap();
  const P = usePalette();
  const layer = `${id}-dot`;

  const withAge = useMemo(() => {
    if (!data) return null;
    const now = Date.now();
    return {
      ...data,
      features: data.features.map((f) => {
        const t = f.properties.published_at ? new Date(f.properties.published_at).getTime() : now;
        return { ...f, properties: { ...f.properties, _o: recency(now - t), _t: t } };
      }),
    };
  }, [data]);

  useGeoJson(
    id,
    withAge,
    [
      {
        id: layer,
        type: 'circle',
        paint: {
          'circle-color': P.signal,
          'circle-radius': ['interpolate', ['linear'], ['coalesce', ['get', 'severity'], 0], 0, 2.4, 1, 3.2],
          'circle-opacity': ['*', ['get', '_o'], ['coalesce', ['feature-state', 'f'], 1]],
          'circle-opacity-transition': { duration: 0 },
        },
      },
    ],
    { promoteId: '_id' },
    undefined,
    'dots',
  );

  // Under the entities: the bottom of the dots band, whenever the layer is (re)added.
  useEffect(() => {
    if (!map) return;
    try {
      const ids = map.getStyle().layers.map((l) => l.id);
      const start = ids.indexOf(slotId('heat'));
      const end = ids.indexOf(slotId('dots'));
      const at = ids.indexOf(layer);
      if (start < 0 || end < 0 || at < 0) return;
      const first = ids[start + 1];
      if (first && first !== layer && start + 1 < end) map.moveLayer(layer, first);
    } catch {
      /* the map was removed */
    }
  }, [map, layer, P.signal]);

  // The dots that are new since the last data fade in.
  const known = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!map || !withAge) return;
    const fresh = freshDots(
      known.current,
      withAge.features.map((f) => ({ id: f.properties._id, at: f.properties._t })),
    );
    known.current = new Set(withAge.features.map((f) => f.properties._id));
    if (!fresh.size || motionOff()) return;
    const set = (v: number) => {
      try {
        for (const fid of fresh) map.setFeatureState({ source: id, id: fid }, { f: v });
      } catch {
        /* removed */
      }
    };
    set(0);
    const tw = tween(D4, set, { done: () => set(1) });
    return () => tw.finish();
  }, [map, id, withAge]);

  return null;
}
