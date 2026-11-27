// The map look's pure parts: base geometry, framing, pills, tint, projection choice.
import { describe, expect, it, beforeEach } from 'vitest';
import type { FeatureCollection, Point, Polygon } from 'geojson';
import { graticule, splitBorders, skyFor, basePaint } from '../../src/map/basemap';
import { densestGroup, globeFraming, rampsFor, sheetPadding, wrap180, boxOfPoints, type LonLat } from '../../src/map/camera';
import {
  countryScores, pillPlace, placeSiteLabels, shownLevel, shownScore, siteLabelBox, siteLabelOrder, tintOpacity, topEntities, withAlpha,
  type EntityProps,
} from '../../src/map/EntityLayers';
import { chokeLabel } from '../../src/map/Chokepoints';
import { PROJECTION_KEY, __reloadProjection, getProjection, parseProjection, setProjection } from '../../src/map/projection';
import { palette } from '../../src/lib/theme';

const square = (x: number, y: number): Polygon => ({ type: 'Polygon', coordinates: [[[x, y], [x + 1, y], [x + 1, y + 1], [x, y + 1], [x, y]]] });

describe('base map', () => {
  it('splits shared edges into borders, drawn once, and the rest into coast', () => {
    const fc: FeatureCollection<Polygon> = {
      type: 'FeatureCollection',
      features: [
        { type: 'Feature', properties: {}, geometry: square(0, 0) },
        { type: 'Feature', properties: {}, geometry: square(1, 0) },
      ],
    };
    const { borders, coast } = splitBorders(fc);
    const segs = (lines: number[][][]) => lines.reduce((n, l) => n + l.length - 1, 0);
    expect(segs(borders.geometry.coordinates)).toBe(1); // x = 1 from y 0 to 1, once
    expect(segs(coast.geometry.coordinates)).toBe(6); // the outline of the two
  });

  it('draws 20° graticule lines', () => {
    const g = graticule();
    expect(g.geometry.coordinates).toHaveLength(18 + 9); // 18 meridians, parallels -80…80
  });

  it('keeps the atmosphere weak and follows the palette', () => {
    const P = { ...palette(), atmo: 0.12, space: '#000001', sea: '#000002' };
    const sky = skyFor(P);
    expect(sky['sky-color']).toBe('#000001');
    expect(sky['atmosphere-blend']).toEqual(['interpolate', ['linear'], ['zoom'], 0, 0.12, 3, 0.12 * 0.8, 5, 0]);
    expect(basePaint(P).map(([l]) => l)).toEqual(['sea', 'grat', 'land', 'borders', 'coast']);
  });
});

const ent = (id: string, score: number | null, extra: Partial<EntityProps> = {}) => ({
  type: 'Feature' as const,
  geometry: { type: 'Point' as const, coordinates: [0, 0] },
  properties: { _id: id, kind: 'supplier', name: id, countries: ['CHN'], ports: [], city: null, max_score: score, open_risk_count: score == null ? 0 : 1, ...extra } as EntityProps,
});
const fcOf = (...fs: ReturnType<typeof ent>[]): FeatureCollection<Point, EntityProps> => ({ type: 'FeatureCollection', features: fs });

describe('entities on the map', () => {
  it('shows Low as grey and only open risks as scores', () => {
    expect(shownScore({ max_score: 70, open_risk_count: 0 })).toBe(0);
    expect(shownLevel({ max_score: 12, open_risk_count: 1 })).toBe('low');
    expect(shownLevel({ max_score: 74, open_risk_count: 2 })).toBe('high');
  });

  it('tints by the formula: Low 0.07, then 0.06 + 0.04 × min(1, (score − 25) / 24)', () => {
    expect(tintOpacity(12)).toBe(0.07);
    expect(tintOpacity(45)).toBeCloseTo(0.06 + 0.04 * Math.min(1, 20 / 24));
    expect(tintOpacity(90)).toBeCloseTo(0.1);
  });

  it('tints a country by its highest entity score', () => {
    const s = countryScores(fcOf(ent('a', 40), ent('b', 66), ent('c', 90, { countries: ['TWN'] }), ent('d', null, { countries: ['NLD'] })));
    expect(Object.fromEntries(s)).toEqual({ CHN: 66, TWN: 90 });
  });

  it('gives pills to the three highest scores with an open risk, never a site', () => {
    const fc = fcOf(ent('a', 40), ent('b', 81), ent('site', 95, { kind: 'site' }), ent('c', 66), ent('d', 74), ent('none', null));
    expect(topEntities(fc)).toEqual(['b', 'd', 'c']);
    expect(topEntities(fc, 3, new Set(['a', 'c']))).toEqual(['c', 'a']);
  });

  it('names a pill by its city, else its port or country', () => {
    expect(pillPlace({ name: 'X', city: 'Hsinchu', ports: [], countries: [] })).toBe('Hsinchu');
    expect(pillPlace({ name: 'X', city: null, ports: [], countries: [] })).toBe('X');
  });

  it('labels an active chokepoint with the drawn lanes through it', () => {
    expect(chokeLabel('Bab el-Mandeb', 8)).toBe('Bab el-Mandeb · 8 lanes');
    expect(chokeLabel('Suez Canal', 1)).toBe('Suez Canal · 1 lane');
    expect(chokeLabel('Suez Canal', 0)).toBe('Suez Canal');
  });

  it('adds alpha to token colours for the heat ramp', () => {
    expect(withAlpha('#e9a190', 0.3)).toBe('rgba(233,161,144,0.3)');
    expect(withAlpha('rgb(1, 2, 3)', 0)).toBe('rgba(1,2,3,0)');
  });
});

describe('site labels', () => {
  // Two sites about 90 km apart (Rotterdam and Eindhoven): one spot at world zoom.
  const site = (name: string, criticality: string | null, at: [number, number]) => ({ properties: { name, criticality }, at });
  const rotterdam = site('Rotterdam Distribution Centre', 'high', [4.48, 51.92]);
  const eindhoven = site('Eindhoven Assembly Plant', 'critical', [5.47, 51.44]);
  // Web Mercator at zoom z, in px (512 px world at z = 0, as MapLibre)
  const project = ([lon, lat]: [number, number], z: number) => {
    const w = 512 * 2 ** z;
    const y = Math.log(Math.tan(Math.PI / 4 + (lat * Math.PI) / 360));
    return [((lon + 180) / 360) * w, (0.5 - y / (2 * Math.PI)) * w] as const;
  };
  const shownAt = (z: number, sites = [rotterdam, eindhoven]) =>
    placeSiteLabels(siteLabelOrder(sites).map((s) => {
      const [x, y] = project(s.at, z);
      return { id: s.properties.name, box: siteLabelBox(x, y, 7 * s.properties.name.length) };
    }));

  it('orders labels by criticality, then by name', () => {
    const order = siteLabelOrder([site('B', null, [0, 0]), site('C', 'low', [0, 0]), site('A', null, [0, 0]), site('D', 'critical', [0, 0]), site('E', 'medium', [0, 0])]);
    expect(order.map((s) => s.properties.name)).toEqual(['D', 'E', 'C', 'A', 'B']);
  });

  it('keeps the more critical label where two would overlap, and shows both once zoomed in', () => {
    expect([...shownAt(1.8)]).toEqual(['Eindhoven Assembly Plant']);
    expect([...shownAt(3)]).toEqual(['Eindhoven Assembly Plant']);
    expect(shownAt(6).size).toBe(2);
    // the same criticality: the first by name
    const level = [site('Rotterdam Distribution Centre', 'high', [4.48, 51.92]), site('Eindhoven Assembly Plant', 'high', [5.47, 51.44])];
    expect([...shownAt(1.8, level)]).toEqual(['Eindhoven Assembly Plant']);
  });

  it('shows labels that do not touch', () => {
    expect(placeSiteLabels([{ id: 'a', box: [0, 0, 100, 16] }, { id: 'b', box: [0, 30, 100, 46] }, { id: 'c', box: [50, 10, 150, 26] }])).toEqual(new Set(['a', 'b']));
  });
});

describe('camera', () => {
  const EUROPE_ASIA: LonLat[] = [[7.5, 51.5], [114, 22.5], [121, 24.8], [128.3, 36.1], [139.7, 35.5], [80.3, 13.1], [100.3, 5.3], [106.7, 20.9], [55.3, 25.2]];
  const AMERICAS: LonLat[] = [[-70.7, -33.5], [-100.4, 20.6]];

  it('frames the densest group a globe view can hold', () => {
    const g = densestGroup([...EUROPE_ASIA, ...AMERICAS]);
    expect(g).toHaveLength(EUROPE_ASIA.length);
    expect(g).not.toContainEqual(AMERICAS[0]);
  });

  it('keeps the whole disc inside the free area beside the sheet', () => {
    const pad = { top: 100, bottom: 84, left: 404, right: 40 };
    const { center, zoom } = globeFraming(EUROPE_ASIA, 1360, 900, pad);
    expect(center[0]).toBeGreaterThan(60);
    expect(center[0]).toBeLessThan(80);
    // the apparent radius at that zoom is no larger than the free box allows
    const R = (512 * 2 ** zoom) / (2 * Math.PI * Math.cos((center[1] * Math.PI) / 180));
    const D = 1.5 * 900;
    const apparent = (R * D) / Math.sqrt(D * D + 2 * R * D); // tangent radius seen from the camera
    expect(apparent).toBeLessThanOrEqual(Math.min(1360 - 444, 900 - 184) / 2 + 1);
  });

  it('wraps longitudes and boxes points', () => {
    expect(wrap180(190)).toBe(-170);
    expect(boxOfPoints([[10, 5], [-20, 40]])).toEqual([[-20, 5], [10, 40]]);
  });

  it('ramps pills and dots with the overview zoom', () => {
    expect(rampsFor(2.3)).toMatchObject({ pill: 1.8, dia: 1.2, d0: 1.5, d1: 1.9 });
    expect(rampsFor(0.5).pill).toBeCloseTo(0.6);
  });

  it('pads beside the sheet at desktop and above it on a phone', () => {
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1360 });
    Object.defineProperty(window, 'innerHeight', { configurable: true, value: 900 });
    expect(sheetPadding('left', 344)).toEqual({ top: 100, bottom: 84, left: 404, right: 40 });
    expect(sheetPadding('right', 400).right).toBe(460);
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 375 });
    const p = sheetPadding('left', 344);
    expect(p.left).toBe(24);
    expect(p.bottom).toBeGreaterThan(300);
    Object.defineProperty(window, 'innerWidth', { configurable: true, value: 1024 });
  });
});

describe('projection choice', () => {
  beforeEach(() => {
    localStorage.clear();
    __reloadProjection();
  });
  it('defaults to the globe and remembers Flat', () => {
    expect(getProjection()).toBe('globe');
    setProjection('flat');
    expect(localStorage.getItem(PROJECTION_KEY)).toBe('flat');
    __reloadProjection();
    expect(getProjection()).toBe('flat');
    expect(parseProjection('nonsense')).toBe('globe');
  });
});
