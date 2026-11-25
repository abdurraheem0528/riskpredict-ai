// The one MapLibre map, full bleed under every screen. Screens add their layers as
// children (see layers.tsx); the base style is Natural Earth countries served from
// /map/, coloured with the System-Color surface tokens. No tiles, keys or glyphs.
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import type { Map as MlMap, StyleSpecification } from 'maplibre-gl';
import { loadGazetteer } from './geo';
import { addLabelImages, loadMapFonts } from './labels';

type Lib = typeof import('maplibre-gl');
type Ctx = { map: MlMap | null; lib: Lib | null; status: 'loading' | 'ready' | 'failed' };
const MapContext = createContext<Ctx>({ map: null, lib: null, status: 'loading' });
export const useMap = () => useContext(MapContext);

export const SEA = '#E7ECF1';
const style: StyleSpecification = {
  version: 8,
  sources: { countries: { type: 'geojson', data: '/map/countries.geojson', attribution: 'Natural Earth' } },
  layers: [
    { id: 'sea', type: 'background', paint: { 'background-color': SEA } },
    { id: 'land', type: 'fill', source: 'countries', paint: { 'fill-color': '#FFFFFF', 'fill-antialias': true } },
    { id: 'coast', type: 'line', source: 'countries', paint: { 'line-color': '#CFD5DC', 'line-width': ['interpolate', ['linear'], ['zoom'], 1, 0.5, 5, 1] } },
  ],
};

export const reducedMotion = () =>
  typeof window !== 'undefined' && window.matchMedia?.('(prefers-reduced-motion: reduce)').matches;

/** The opening view: the inhabited world, as in the designs. */
export const WORLD: [[number, number], [number, number]] = [[-125, -40], [150, 66]];

export function MapRoot({ children, veil = false }: { children?: ReactNode; veil?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const [ctx, setCtx] = useState<Ctx>({ map: null, lib: null, status: 'loading' });

  useEffect(() => {
    let map: MlMap | null = null;
    let cancelled = false;
    void loadGazetteer().catch(() => undefined);
    import('maplibre-gl')
      .then((lib) => {
        if (cancelled || !ref.current) return;
        try {
          map = new lib.Map({
            container: ref.current,
            style,
            bounds: WORLD,
            fitBoundsOptions: { padding: 0 },
            minZoom: 0.6,
            maxZoom: 11,
            attributionControl: false,
            dragRotate: false,
            pitchWithRotate: false,
            touchPitch: false,
            renderWorldCopies: true,
            fadeDuration: 0,
          });
        } catch {
          setCtx({ map: null, lib, status: 'failed' });
          return;
        }
        map.touchZoomRotate.disableRotation();
        map.keyboard.disableRotation();
        const m = map;
        // Labels are drawn by MapLibre from the Inter faces: wait for them (≤ 2.5 s).
        Promise.all([new Promise<void>((r) => m.once('load', () => r())), loadMapFonts()]).then(() => {
          if (cancelled) return;
          addLabelImages(m);
          setCtx({ map: m, lib, status: 'ready' });
        }, () => undefined);
        m.on('idle', () => ref.current?.setAttribute('data-map', 'idle'));
        m.on('movestart', () => ref.current?.setAttribute('data-map', 'moving'));
      })
      .catch(() => setCtx((c) => ({ ...c, status: 'failed' })));
    return () => {
      cancelled = true;
      map?.remove();
    };
  }, []);

  return (
    <MapContext.Provider value={ctx}>
      <div ref={ref} className="mapbox" data-map="loading" aria-hidden="true" />
      {veil && <div className="veil" aria-hidden="true" />}
      {children}
    </MapContext.Provider>
  );
}

export function moveTo(map: MlMap, opts: { center: [number, number]; zoom?: number }) {
  if (reducedMotion()) map.jumpTo(opts);
  else map.flyTo({ ...opts, duration: 900, essential: false });
}

export function fitTo(map: MlMap, b: [[number, number], [number, number]], padding: { top: number; bottom: number; left: number; right: number }, maxZoom = 5) {
  const [[w, s], [e, n]] = b;
  // A single point (or a tiny box) would zoom in too far: pad it out.
  const pad = 2;
  const box: [[number, number], [number, number]] = [
    [Math.max(-180, Math.min(w, e - pad)), Math.max(-80, Math.min(s, n - pad))],
    [Math.min(180, Math.max(e, w + pad)), Math.min(84, Math.max(n, s + pad))],
  ];
  try {
    map.fitBounds(box, { padding, maxZoom, duration: reducedMotion() ? 0 : 800 });
  } catch {
    /* padding larger than the viewport: keep the current view */
  }
}

/** Padding that keeps fitted content clear of the floating sheet and top row. */
export function sheetPadding(side: 'left' | 'right' | 'none', width: number) {
  const w = typeof window !== 'undefined' ? window.innerWidth : 1360;
  const h = typeof window !== 'undefined' ? window.innerHeight : 900;
  if (w <= 760) return { top: 80, bottom: Math.round(h * 0.6) + 24, left: 24, right: 24 };
  const sheet = side === 'none' ? 40 : width + 60;
  // Callouts hang to the right of their point, and the map controls sit on the right edge.
  return { top: 110, bottom: 90, left: side === 'left' ? sheet : 80, right: side === 'right' ? sheet : 230 };
}
