// The one MapLibre map, full bleed under every screen. Screens add their layers as
// children (see layers.tsx). The base style is a globe (or flat, map controls) of Natural
// Earth countries served from /map/: sea, graticule, land, borders and coast, coloured
// from the theme's tokens and recoloured in place when the theme changes (no style
// reload). No tiles, keys or glyphs.
import { createContext, useContext, useEffect, useRef, useState, type ReactNode } from 'react';
import type { FeatureCollection, MultiPolygon, Polygon } from 'geojson';
import type { GeoJSONSource, Map as MlMap, StyleSpecification } from 'maplibre-gl';
import { D2, reducedMotion } from '../lib/motion';
import { onThemeChange, palette } from '../lib/theme';
import { loadGazetteer } from './geo';
import { addLabelImages, loadMapFonts } from './labels';
import { SLOTS, slotId } from './slots';
import { basePaint, graticule, skyFor, splitBorders } from './basemap';
import { getProjection, projectionSpec, subscribeProjection, type Projection } from './projection';
import { getRamps, markLoaded, noteInput, projectionApplied, setSwitching, subscribeRamps, updateRamps } from './camera';

export { reducedMotion } from '../lib/motion';
export { moveTo, fitTo, sheetPadding } from './camera';

type Lib = typeof import('maplibre-gl');
type Ctx = { map: MlMap | null; lib: Lib | null; status: 'loading' | 'ready' | 'failed' };
const MapContext = createContext<Ctx>({ map: null, lib: null, status: 'loading' });
export const useMap = () => useContext(MapContext);

const EMPTY: FeatureCollection = { type: 'FeatureCollection', features: [] };

function style(proj: Projection): StyleSpecification {
  const P = palette();
  return {
    version: 8,
    projection: projectionSpec(proj),
    sky: skyFor(P),
    sources: {
      countries: { type: 'geojson', data: EMPTY, attribution: 'Natural Earth' },
      grat: { type: 'geojson', data: graticule() },
      borders: { type: 'geojson', data: EMPTY },
      coast: { type: 'geojson', data: EMPTY },
    },
    layers: [
      { id: 'sea', type: 'background', paint: { 'background-color': P.sea } },
      {
        id: 'grat', type: 'line', source: 'grat',
        layout: { visibility: proj === 'globe' ? 'visible' : 'none' },
        paint: { 'line-color': P.grat, 'line-width': 0.6, 'line-opacity': ['interpolate', ['linear'], ['zoom'], 2.4, 1, 3.4, 0] },
      },
      { id: 'land', type: 'fill', source: 'countries', paint: { 'fill-color': P.land, 'fill-antialias': true } },
      { id: 'borders', type: 'line', source: 'borders', paint: { 'line-color': P.border, 'line-width': 0.5 } },
      { id: 'coast', type: 'line', source: 'coast', paint: { 'line-color': P.coast, 'line-width': ['interpolate', ['linear'], ['zoom'], 1, 0.6, 5, 1] } },
      // Empty marker layers that order everything added later (see SLOTS in ./slots).
      ...SLOTS.map((s) => ({ id: slotId(s), type: 'background' as const, layout: { visibility: 'none' as const } })),
    ],
  };
}

/** The view the overview's opening move starts from: the globe far out, turned a little west. */
export const WIDE_START = { center: [20, 22] as [number, number], zoom: 0.8 };
/** The inhabited world (the flat map's first view, and "fit" with nothing to fit). */
export const WORLD: [[number, number], [number, number]] = [[-125, -40], [150, 66]];

/** The overview (signed in, on / or /register) opens wide, for the opening move. */
function opensOnOverview() {
  try {
    const p = window.location.pathname.replace(/\/+$/, '') || '/';
    return (p === '/' || p === '/register') && !!localStorage.getItem('rp.refresh');
  } catch {
    return false;
  }
}

/** The globe as large as the whole viewport holds it (sign-in, empty and other first views). */
function fullDisc(el: HTMLElement) {
  const W = el.clientWidth || 1360, H = el.clientHeight || 900;
  const D = 1.5 * H, rho = (Math.min(W, H) / 2) * 0.86;
  const R = (rho * rho + rho * Math.sqrt(rho * rho + D * D)) / D;
  return { center: [20, 22] as [number, number], zoom: Math.max(0.6, Math.log2((R * 2 * Math.PI * Math.cos((22 * Math.PI) / 180)) / 512)) };
}

const wait = (ms: number) => new Promise<void>((r) => setTimeout(r, ms));

/** Paint the last frame of the map into the snapshot canvas (drawn right after a render). */
function snapshot(map: MlMap, snap: HTMLCanvasElement): Promise<boolean> {
  return new Promise((res) => {
    let done = false;
    const t = setTimeout(() => {
      if (!done) {
        done = true;
        res(false);
      }
    }, 300);
    map.once('render', () => {
      if (done) return;
      done = true;
      clearTimeout(t);
      try {
        const c = map.getCanvas();
        snap.width = c.width;
        snap.height = c.height;
        const g = snap.getContext('2d');
        if (!g) return res(false);
        g.fillStyle = palette().space;
        g.fillRect(0, 0, c.width, c.height);
        g.drawImage(c, 0, 0);
        snap.classList.remove('out');
        snap.classList.add('on');
        res(true);
      } catch {
        res(false);
      }
    });
    map.triggerRepaint();
  });
}

export function MapRoot({ children, veil = false }: { children?: ReactNode; veil?: boolean }) {
  const ref = useRef<HTMLDivElement>(null);
  const snapRef = useRef<HTMLCanvasElement>(null);
  const [ctx, setCtx] = useState<Ctx>({ map: null, lib: null, status: 'loading' });

  useEffect(() => {
    let map: MlMap | null = null;
    let cancelled = false;
    const offs: (() => void)[] = [];
    void loadGazetteer().catch(() => undefined);
    const countries = fetch('/map/countries.geojson').then((r) => (r.ok ? (r.json() as Promise<FeatureCollection<Polygon | MultiPolygon>>) : null)).catch(() => null);
    import('maplibre-gl')
      .then((lib) => {
        if (cancelled || !ref.current) return;
        const el = ref.current;
        const proj = getProjection();
        const start = opensOnOverview() ? WIDE_START : proj === 'globe' ? fullDisc(el) : null;
        try {
          map = new lib.Map({
            container: el,
            style: style(proj),
            ...(start ? { center: start.center, zoom: start.zoom } : { bounds: WORLD, fitBoundsOptions: { padding: 0 } }),
            minZoom: 0.4,
            maxZoom: 11,
            attributionControl: false,
            dragRotate: false,
            pitchWithRotate: false,
            touchPitch: false,
            renderWorldCopies: true,
            fadeDuration: 0,
            canvasContextAttributes: { antialias: true },
          });
        } catch {
          setCtx({ map: null, lib, status: 'failed' });
          return;
        }
        map.touchZoomRotate.disableRotation();
        map.keyboard.disableRotation();
        const m = map;

        // Base geometry: land, and borders and coastlines split out of the same polygons.
        m.once('load', () => {
          void countries.then((fc) => {
            if (cancelled || !fc) return;
            const { borders, coast } = splitBorders(fc);
            (m.getSource('countries') as GeoJSONSource | undefined)?.setData(fc);
            (m.getSource('borders') as GeoJSONSource | undefined)?.setData(borders);
            (m.getSource('coast') as GeoJSONSource | undefined)?.setData(coast);
          });
        });

        // Labels are drawn by MapLibre from the Inter faces: wait for them (≤ 2.5 s).
        Promise.all([new Promise<void>((r) => m.once('load', () => r())), loadMapFonts()]).then(() => {
          if (cancelled) return;
          markLoaded(m);
          addLabelImages(m);
          updateRamps(m);
          zoomClasses();
          setCtx({ map: m, lib, status: 'ready' });
        }, () => undefined);
        m.on('idle', () => el.setAttribute('data-map', 'idle'));
        m.on('movestart', () => el.setAttribute('data-map', 'moving'));

        // Zoom bands for the HTML markers (pills and site labels, chokepoints, city labels).
        let zkey = '';
        const zoomClasses = () => {
          const z = m.getZoom(), T = getRamps();
          const k = `${z >= T.pill}${z >= T.dia}${z > T.label}`;
          if (k === zkey) return;
          zkey = k;
          el.classList.toggle('zp', z >= T.pill);
          el.classList.toggle('zd', z >= T.dia);
          el.classList.toggle('zl', z > T.label);
        };
        m.on('zoom', zoomClasses);
        offs.push(subscribeRamps(() => {
          zkey = '';
          zoomClasses();
        }));
        const onResize = () => updateRamps(m);
        m.on('resize', onResize);

        // Theme: recolour the base layers and the sky in place.
        offs.push(onThemeChange(() => {
          const P = palette();
          for (const [layer, prop, value] of basePaint(P)) if (m.getLayer(layer)) m.setPaintProperty(layer, prop, value);
          m.setSky(skyFor(P));
          addLabelImages(m, true);
        }));

        // Any pointer, wheel, touch or key: stops idle rotation and cancels the opening move.
        const down = () => noteInput('down');
        const move = () => noteInput('move');
        for (const ev of ['pointerdown', 'wheel', 'touchstart'] as const) el.addEventListener(ev, down, { passive: true });
        el.addEventListener('pointermove', move, { passive: true });
        window.addEventListener('keydown', down);
        offs.push(() => {
          for (const ev of ['pointerdown', 'wheel', 'touchstart'] as const) el.removeEventListener(ev, down);
          el.removeEventListener('pointermove', move);
          window.removeEventListener('keydown', down);
        });

        // Globe ⇄ flat: a crossfade from a snapshot of the last frame.
        let busy = false;
        let want: Projection = proj;
        let have: Projection = proj;
        const apply = async () => {
          if (busy) return;
          busy = true;
          try {
            while (want !== have) {
              const next = want;
              setSwitching(true);
              const snap = snapRef.current;
              let shown = false;
              if (!reducedMotion() && !document.hidden && snap) {
                el.classList.add('zx'); // markers out first, so the snapshot has none
                await wait(60);
                shown = await snapshot(m, snap);
              }
              if (cancelled) return;
              m.setProjection(projectionSpec(next));
              if (m.getLayer('grat')) m.setLayoutProperty('grat', 'visibility', next === 'globe' ? 'visible' : 'none');
              have = next;
              updateRamps(m, next);
              projectionApplied(next);
              if (shown && snap) {
                await new Promise<void>((r) => {
                  const t = setTimeout(r, 1200);
                  m.once('idle', () => {
                    clearTimeout(t);
                    r();
                  });
                });
                el.classList.remove('zx');
                snap.classList.add('out');
                await wait(D2 + 40);
                snap.classList.remove('on', 'out');
              }
              el.classList.remove('zx');
              setSwitching(false);
            }
          } finally {
            busy = false;
          }
        };
        offs.push(subscribeProjection(() => {
          want = getProjection();
          void apply();
        }));
      })
      .catch(() => setCtx((c) => ({ ...c, status: 'failed' })));
    return () => {
      cancelled = true;
      offs.forEach((f) => f());
      map?.remove();
    };
  }, []);

  return (
    <MapContext.Provider value={ctx}>
      <div ref={ref} className="mapbox" data-map="loading" aria-hidden="true" />
      <canvas ref={snapRef} className="mapsnap" aria-hidden="true" />
      {veil && <div className="veil" aria-hidden="true" />}
      {children}
    </MapContext.Provider>
  );
}
