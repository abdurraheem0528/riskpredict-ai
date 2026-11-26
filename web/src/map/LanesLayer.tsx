// The sea lanes of declared routes (./lanes.ts), in the map's 'lanes' band: the shared network
// drawn once, a dashed flow over it moving toward each lane's destination, and the selected
// entity's own lanes on top. Colours follow the theme (usePalette → paint, set in place).
import { useEffect, useMemo, useSyncExternalStore } from 'react';
import type { ExpressionSpecification } from 'maplibre-gl';
import type { FeatureCollection, LineString } from 'geojson';
import { useMap } from './MapRoot';
import { useGeoJson } from './layers';
import { useFocus } from './focus';
import { usePalette } from '../lib/theme';
import { D3, onFrame, tween } from '../lib/motion';
import { DASH, DASH_STEP_MS, type Lanes } from './lanes';

const WIDTH: ExpressionSpecification = ['interpolate', ['linear'], ['zoom'], 1, 1, 5, 1.5];
const SEL_WIDTH: ExpressionSpecification = ['interpolate', ['linear'], ['zoom'], 1, 1.6, 5, 2.4];
const ROUND = { 'line-join': 'round', 'line-cap': 'round' } as const;
const NO_TRANSITION = { duration: 0, delay: 0 };

const RM = '(prefers-reduced-motion: reduce)';
function useReducedMotion(): boolean {
  return useSyncExternalStore(
    (l) => {
      const q = window.matchMedia?.(RM);
      q?.addEventListener?.('change', l);
      return () => q?.removeEventListener?.('change', l);
    },
    () => !!window.matchMedia?.(RM).matches,
    () => false,
  );
}

/**
 * `lanes` comes from `useLanes(entities, disruptions)`. The selected entity is the linked
 * selection (./focus) unless `selected` is given (Risk: the risk's entity).
 */
export function LanesLayer({ lanes, selected }: { lanes: Lanes | null; selected?: string | null }) {
  const { map } = useMap();
  const focus = useFocus();
  const sel = selected !== undefined ? selected : focus.selected;
  const P = usePalette();
  const rm = useReducedMotion();
  const network = lanes?.network ?? null;
  const hasLanes = !!network?.features.length;

  useGeoJson(
    'lanes',
    network as FeatureCollection | null,
    [
      { id: 'lanes-base', type: 'line', layout: ROUND, paint: { 'line-color': P.lane, 'line-opacity': 0.35, 'line-width': WIDTH } },
      {
        id: 'lanes-flow',
        type: 'line',
        layout: { 'line-join': 'round' },
        paint: { 'line-color': P.lane, 'line-opacity': 0.55, 'line-width': WIDTH, 'line-dasharray': DASH[0], 'line-dasharray-transition': NO_TRANSITION },
      },
    ],
    {},
    undefined,
    'lanes',
  );

  const route = sel ? lanes?.routes.get(sel) : undefined;
  const selFc = useMemo<FeatureCollection | null>(() => (route ? { type: 'FeatureCollection', features: [route] } : null), [route]);
  useGeoJson(
    'lanes-sel',
    selFc,
    [{ id: 'lanes-sel', type: 'line', layout: ROUND, paint: { 'line-color': P.lane, 'line-width': SEL_WIDTH, 'line-opacity': 0, 'line-opacity-transition': NO_TRANSITION } }],
    {},
    undefined,
    'lanes',
  );

  // The flow: one dash step every 80 ms on the shared frame loop; none under reduced motion.
  useEffect(() => {
    if (!map || !hasLanes) return;
    try {
      map.setLayoutProperty('lanes-flow', 'visibility', rm ? 'none' : 'visible');
    } catch {
      return;
    }
    if (rm) return;
    let acc = 0, i = 0;
    return onFrame((_now, dt) => {
      acc += dt;
      if (acc < DASH_STEP_MS) return true;
      const n = Math.floor(acc / DASH_STEP_MS);
      acc -= n * DASH_STEP_MS;
      i = (i + n) % DASH.length;
      try {
        map.setPaintProperty('lanes-flow', 'line-dasharray', DASH[i]);
      } catch {
        return false; // the map was removed
      }
      return true;
    });
  }, [map, hasLanes, rm]);

  // The selected entity's lanes fade in over 320 ms (again only when it is another line).
  const routeKey = route ? `${sel}:${(route.geometry as LineString).coordinates.length}:${(route.geometry as LineString).coordinates[0]?.join(',')}` : '';
  useEffect(() => {
    if (!map) return;
    const set = (o: number) => {
      try {
        map.setPaintProperty('lanes-sel', 'line-opacity', o);
      } catch {
        /* not added yet, or the map was removed */
      }
    };
    if (!routeKey) {
      set(0);
      return;
    }
    set(0);
    const t = tween(D3, (p) => set(0.9 * p));
    return () => t.cancel();
  }, [map, routeKey]);

  return null;
}
