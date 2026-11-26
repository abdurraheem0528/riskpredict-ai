// Placing API features on the map. Entities, risks and events without a position are
// placed by their first port (World Port Index) or first country (Natural Earth label
// point), as openapi.yaml says for /v1/map/entities and /v1/map/risks.
import type { Feature, FeatureCollection, Point } from 'geojson';

export type LonLat = [number, number];
type Centroids = Record<string, [number, number, string]>;
// [lon, lat, World Port Index name] or [lon, lat, WPI name, UN/LOCODE name] (scripts/build-map-data.mjs).
type Ports = Record<string, [number, number, string] | [number, number, string, string]>;

let centroids: Centroids | null = null;
let ports: Ports | null = null;
let loading: Promise<void> | null = null;

async function getJson<T>(url: string): Promise<T> {
  const res = await fetch(url);
  if (!res.ok) throw new Error(`${url}: ${res.status}`);
  return (await res.json()) as T;
}

/**
 * Load the lookup tables once (13 KiB + 55 KiB gzipped). The URL carries a hash of the
 * files (vite.config.ts), so a new release is never read from an older cached copy
 * (`/map/*` is cached for a day).
 */
export function loadGazetteer(): Promise<void> {
  if (!loading) {
    const v = `?v=${__MAP_DATA_VERSION__}`;
    loading = Promise.all([getJson<Centroids>(`/map/centroids.json${v}`), getJson<Ports>(`/map/ports.json${v}`)]).then(
      ([c, p]) => {
        centroids = c;
        ports = p;
      },
      (e) => {
        loading = null;
        throw e;
      },
    );
  }
  return loading;
}
export const gazetteerReady = () => centroids !== null && ports !== null;

/** For tests. */
export function __setGazetteer(c: Centroids, p: Ports) {
  centroids = c;
  ports = p;
  loading = Promise.resolve();
}

export function countryPoint(iso3: string): LonLat | null {
  const c = centroids?.[iso3];
  return c ? [c[0], c[1]] : null;
}
export function countryName(iso3: string): string {
  return centroids?.[iso3]?.[2] ?? iso3;
}
export function portPoint(locode: string): LonLat | null {
  const p = ports?.[locode];
  return p ? [p[0], p[1]] : null;
}
/** The port's display name: "Kaohsiung", "Tokyo", "Penang", "Chennai" (not "Kao-Hsiung", "Tokyo Ko", …). */
export function portName(locode: string): string {
  const p = ports?.[locode];
  return p ? cleanPortName(p[3] ?? p[2]) : locode;
}
/**
 * A display name from a UN/LOCODE or World Port Index name: drops parenthesised former or
 * local names ("Chennai (ex Madras)"), a region after a comma ("Tegal, Java"), and the
 * WPI's harbour suffixes ("Tokyo Ko", "Hualien Hsien") and the UN/LOCODE's "Pt".
 */
export function cleanPortName(raw: string): string {
  const s = raw
    .replace(/\s*\([^)]*\)/g, '')
    .replace(/,.*$/, '')
    .replace(/[\s-]+(?:Ko|Kou|Hsien|Pt)$/i, '')
    .replace(/\s+/g, ' ')
    .trim();
  return s || raw.trim();
}

export function placeOf(p: {
  lat?: number | null;
  lon?: number | null;
  ports?: readonly string[];
  countries?: readonly string[];
}): LonLat | null {
  if (p.lat != null && p.lon != null) return [p.lon, p.lat];
  for (const code of p.ports ?? []) {
    const pt = portPoint(code);
    if (pt) return pt;
  }
  for (const code of p.countries ?? []) {
    const pt = countryPoint(code);
    if (pt) return pt;
  }
  return null;
}

/**
 * Human place line: the API's `place` when given; else "Hsinchu, Taiwan" from a city and
 * the first country; else "Kaohsiung, Taiwan" from ports/countries.
 */
export function placeLabel(p: {
  place?: string | null;
  city?: string | null;
  ports?: readonly string[];
  countries?: readonly string[];
}): string {
  if (p.place) return p.place;
  if (p.city) return p.countries?.length ? `${p.city}, ${countryName(p.countries[0])}` : p.city;
  const parts: string[] = [];
  if (p.ports?.length) parts.push(portName(p.ports[0]));
  if (p.countries?.length) {
    const names = p.countries.slice(0, 2).map(countryName);
    parts.push(names.join(' and ') + (p.countries.length > 2 ? ` +${p.countries.length - 2}` : ''));
  }
  return parts.join(', ');
}

type AnyProps = Record<string, unknown>;
type InFeature<P> = { id?: string | number; geometry: Point | { type: 'Point'; coordinates: number[] } | null; properties: P };

/**
 * Give every feature a point: its own geometry, else its first port, else first country.
 * Features that cannot be placed are dropped and counted.
 */
export function placeAll<P extends AnyProps>(
  features: readonly InFeature<P>[],
): { fc: FeatureCollection<Point, P & { _id: string }>; unplaced: number } {
  const out: Feature<Point, P & { _id: string }>[] = [];
  let unplaced = 0;
  features.forEach((f, i) => {
    const pr = f.properties as P & { ports?: string[]; countries?: string[]; place_type?: string; place_key?: string };
    // Risk places (/v1/map/risks) without geometry are keyed by place_type + place_key.
    const keyed =
      pr.place_type === 'country' && pr.place_key ? countryPoint(pr.place_key) : pr.place_type === 'port' && pr.place_key ? portPoint(pr.place_key) : null;
    const coords =
      f.geometry && f.geometry.coordinates.length >= 2
        ? ([f.geometry.coordinates[0], f.geometry.coordinates[1]] as LonLat)
        : (keyed ?? placeOf({ ports: pr.ports, countries: pr.countries }));
    if (!coords) {
      unplaced++;
      return;
    }
    const id = String(f.id ?? i);
    out.push({ type: 'Feature', id: i, geometry: { type: 'Point', coordinates: coords }, properties: { ...f.properties, _id: id } });
  });
  return { fc: { type: 'FeatureCollection', features: out }, unplaced };
}

export function bounds(points: readonly LonLat[]): [LonLat, LonLat] | null {
  if (!points.length) return null;
  let w = 180, s = 90, e = -180, n = -90;
  for (const [x, y] of points) {
    w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y);
  }
  return [[w, s], [e, n]];
}
