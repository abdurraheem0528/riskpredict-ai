// API GeoJSON → placed features for the layers, once the gazetteer is loaded.
import { useEffect, useMemo, useState } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { gazetteerReady, loadGazetteer, placeAll } from './geo';
import type { EntityFC, EventFC, RiskPlaceFC } from '../api/queries';

export function useGazetteer() {
  const [ready, setReady] = useState(gazetteerReady());
  useEffect(() => {
    if (ready) return;
    let live = true;
    loadGazetteer().then(() => live && setReady(true), () => undefined);
    return () => {
      live = false;
    };
  }, [ready]);
  return ready;
}

export function usePlaced<T extends EventFC | EntityFC | RiskPlaceFC>(fc: T | undefined) {
  const ready = useGazetteer();
  return useMemo(() => {
    if (!fc || !ready) return null;
    return placeAll(fc.features as unknown as Parameters<typeof placeAll>[0]);
  }, [fc, ready]) as { fc: FeatureCollection<Point, T['features'][number]['properties'] & { _id: string }>; unplaced: number } | null;
}

/** The strongest few signals, for the pulsing rings on Empty. */
export function topSignals(fc: FeatureCollection<Point, { severity?: number | null; _id: string }> | null, n = 6) {
  if (!fc) return [];
  return [...fc.features]
    .sort((a, b) => (b.properties.severity ?? 0) - (a.properties.severity ?? 0))
    .slice(0, n)
    .map((f) => ({ key: f.properties._id, at: f.geometry.coordinates as [number, number] }));
}
