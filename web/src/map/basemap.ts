// The base map's geometry and paint: the graticule, borders and coastlines split out of
// the country polygons, the sky, and the base layers' colours for a palette.
import type { Feature, FeatureCollection, MultiLineString, MultiPolygon, Polygon, Position } from 'geojson';
import type { SkySpecification } from 'maplibre-gl';
import type { Palette } from '../lib/theme';

/** 20° lines: meridians from 80°S to 80°N, parallels every 20° (2° steps, so they curve on the globe). */
export function graticule(step = 20): Feature<MultiLineString> {
  const lines: Position[][] = [];
  for (let lon = -180; lon < 180; lon += step) {
    const l: Position[] = [];
    for (let lat = -80; lat <= 80; lat += 2) l.push([lon, lat]);
    lines.push(l);
  }
  for (let lat = -80; lat <= 80; lat += step) {
    const l: Position[] = [];
    for (let lon = -180; lon <= 180; lon += 2) l.push([lon, lat]);
    lines.push(l);
  }
  return { type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates: lines } };
}

/**
 * Borders and coastlines from country polygons that share their edges exactly (the build
 * simplifies Natural Earth with topology kept, scripts/build-map-data.mjs): an edge two
 * polygons have is a border, an edge only one has is coast. Runs of one kind are joined
 * into lines.
 */
export function splitBorders(fc: FeatureCollection<Polygon | MultiPolygon>): { borders: Feature<MultiLineString>; coast: Feature<MultiLineString> } {
  const rings: Position[][] = [];
  for (const f of fc.features) {
    const g = f.geometry;
    if (!g) continue;
    const polys = g.type === 'Polygon' ? [g.coordinates] : g.type === 'MultiPolygon' ? g.coordinates : [];
    for (const p of polys) for (const r of p) rings.push(r);
  }
  const k = (a: Position, b: Position) => {
    const x = `${a[0]},${a[1]}`, y = `${b[0]},${b[1]}`;
    return x < y ? `${x}|${y}` : `${y}|${x}`;
  };
  const count = new Map<string, number>();
  for (const r of rings) for (let i = 0; i < r.length - 1; i++) {
    const key = k(r[i], r[i + 1]);
    count.set(key, (count.get(key) ?? 0) + 1);
  }
  const borders: Position[][] = [];
  const coast: Position[][] = [];
  const seenBorder = new Set<string>();
  for (const r of rings) {
    let run: Position[] | null = null;
    let runBorder = false;
    const flush = () => {
      if (run && run.length > 1) (runBorder ? borders : coast).push(run);
      run = null;
    };
    for (let i = 0; i < r.length - 1; i++) {
      const key = k(r[i], r[i + 1]);
      const isBorder = (count.get(key) ?? 0) > 1;
      // a shared edge is drawn once, from the first ring that has it
      if (isBorder && seenBorder.has(key)) {
        flush();
        continue;
      }
      if (isBorder) seenBorder.add(key);
      if (!run || runBorder !== isBorder) {
        flush();
        run = [r[i]];
        runBorder = isBorder;
      }
      run.push(r[i + 1]);
    }
    flush();
  }
  const mls = (coordinates: Position[][]): Feature<MultiLineString> => ({ type: 'Feature', properties: {}, geometry: { type: 'MultiLineString', coordinates } });
  return { borders: mls(borders), coast: mls(coast) };
}

/** The sky for a palette: weak atmosphere (a stronger blend washed the globe out). */
export function skyFor(P: Palette): SkySpecification {
  return {
    'sky-color': P.space,
    'horizon-color': P.sea,
    'fog-color': P.sea,
    'sky-horizon-blend': 0.6,
    'horizon-fog-blend': 1,
    'fog-ground-blend': 1,
    'atmosphere-blend': ['interpolate', ['linear'], ['zoom'], 0, P.atmo, 3, P.atmo * 0.8, 5, 0],
  };
}

/** Base layer paint that follows the theme: [layer, property, value]. */
export function basePaint(P: Palette): [string, string, unknown][] {
  return [
    ['sea', 'background-color', P.sea],
    ['grat', 'line-color', P.grat],
    ['land', 'fill-color', P.land],
    ['borders', 'line-color', P.border],
    ['coast', 'line-color', P.coast],
  ];
}
