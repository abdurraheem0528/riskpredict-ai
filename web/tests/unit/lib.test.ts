import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { ago, initials, levelOf, outOf5, pct, signed } from '../../src/lib/format';
import { __setGazetteer, cleanPortName, placeAll, placeLabel, placeOf, portName } from '../../src/map/geo';
import { backoffMs } from '../../src/api/stream';
import { adviceSteps } from '../../src/screens/RiskView';
import { feedSummary } from '../../src/lib/feeds';

describe('format', () => {
  it('maps scores to the four levels', () => {
    expect([24, 40, 59.9, 60, 79, 80, 100].map(levelOf)).toEqual(['low', 'med', 'med', 'high', 'high', 'crit', 'crit']);
  });
  it('signs changes and writes percents without a space', () => {
    expect(signed(4)).toBe('+4');
    expect(signed(-2)).toBe('−2');
    expect(signed(0)).toBe('0');
    expect(pct(0.72)).toBe('72%');
    expect(outOf5(0.8)).toBe(4);
  });
  it('says how long ago', () => {
    const now = Date.parse('2026-11-24T12:00:00Z');
    expect(ago('2026-11-24T11:58:00Z', now)).toBe('2 min ago');
    expect(ago('2026-11-24T09:00:00Z', now)).toBe('3 h ago');
    expect(ago(null, now)).toBe('never');
  });
  it('makes initials', () => {
    expect(initials('Mira Okafor', 'x@y.z')).toBe('MO');
    expect(initials(null, 'ops@kestrel.test')).toBe('OP');
  });
});

describe('placing features', () => {
  it('uses the position, then the first port, then the first country', () => {
    expect(placeOf({ lat: 1, lon: 2, ports: ['CNYTN'] })).toEqual([2, 1]);
    expect(placeOf({ ports: ['XXXXX', 'CNYTN'], countries: ['TUR'] })).toEqual([114.27, 22.57]);
    expect(placeOf({ countries: ['TUR'] })).toEqual([35.2, 39.1]);
    expect(placeOf({ countries: ['ZZZ'] })).toBeNull();
  });
  it('places risk places by place_type and place_key, and counts what it cannot place', () => {
    const { fc, unplaced } = placeAll([
      { id: 'country:TUR', geometry: null, properties: { place_type: 'country', place_key: 'TUR' } },
      { id: 'port:NLRTM', geometry: null, properties: { place_type: 'port', place_key: 'NLRTM' } },
      { id: 'country:ZZZ', geometry: null, properties: { place_type: 'country', place_key: 'ZZZ' } },
    ]);
    expect(fc.features.map((f) => f.geometry.coordinates)).toEqual([[35.2, 39.1], [4.4, 51.9]]);
    expect(unplaced).toBe(1);
  });
  it('names places', () => {
    expect(placeLabel({ ports: ['CNYTN'], countries: ['CHN'] })).toBe('Yantian, China');
  });
  it('names an entity with a city by its city and country, and prefers the API line', () => {
    expect(placeLabel({ city: 'Shenzhen', ports: ['CNYTN'], countries: ['CHN'] })).toBe('Shenzhen, China');
    expect(placeLabel({ place: 'Hsinchu, Taiwan', city: 'Hsinchu', ports: ['TWKHH'], countries: ['TWN'] })).toBe('Hsinchu, Taiwan');
    expect(placeLabel({ place: null, city: null, ports: ['CNYTN'], countries: ['CHN'] })).toBe('Yantian, China');
  });
});

describe('port names', () => {
  it('cleans World Port Index and UN/LOCODE names for display', () => {
    expect(['Tokyo Ko', 'Kao-Hsiung', 'Chennai (Madras)', 'Chennai (ex Madras)', 'Penang (Georgetown)', 'Yantian Pt', 'Tegal, Java', 'Hualien Hsien', 'Busan'].map(cleanPortName))
      .toEqual(['Tokyo', 'Kao-Hsiung', 'Chennai', 'Chennai', 'Penang', 'Yantian', 'Tegal', 'Hualien', 'Busan']);
  });
  it('prefers the UN/LOCODE name and keeps the raw WPI name in the table', () => {
    __setGazetteer({}, { TWKHH: [120.25, 22.617, 'Kao-Hsiung', 'Kaohsiung'], MYPEN: [100.35, 5.417, 'Pulau Pinang', 'Penang (Georgetown)'], JPXXX: [139, 35, 'Somewhere Ko'] });
    expect([portName('TWKHH'), portName('MYPEN'), portName('JPXXX'), portName('NLRTM')]).toEqual(['Kaohsiung', 'Penang', 'Somewhere', 'NLRTM']);
  });
  it("names the live test's ports as the API does (engine/places.py, tests/test_places.py)", () => {
    const ports = JSON.parse(readFileSync('public/map/ports.json', 'utf8')) as Record<string, [number, number, string, string?]>;
    const names = ['TWKHH', 'MYPEN', 'AEJEA', 'CLSAI', 'VNHPH'].map((c) => cleanPortName(ports[c][3] ?? ports[c][2]));
    expect(names).toEqual(['Kaohsiung', 'Penang', 'Jebel Ali', 'San Antonio', 'Haiphong']);
  });
});

describe('stream backoff', () => {
  it('grows from about a second to at most 30 s', () => {
    expect(backoffMs(0, () => 0)).toBe(500);
    expect(backoffMs(0, () => 1)).toBe(1000);
    expect(backoffMs(3, () => 1)).toBe(8000);
    expect(backoffMs(20, () => 1)).toBe(30000);
  });
});

describe('advice', () => {
  it('splits lines, or sentences when it is one paragraph', () => {
    expect(adviceSteps('- Do this.\n- Then that.')).toEqual(['Do this.', 'Then that.']);
    expect(adviceSteps('Call them. Order early! Check terms')).toEqual(['Call them.', 'Order early!', 'Check terms']);
  });
});

describe('feed health', () => {
  it('counts live feeds from the API, never a fixed number', () => {
    const at = '2026-11-24T12:00:00Z';
    const s = feedSummary({
      sources: [
        { source: 'usgs', status: 'ok', last_run_at: at, last_success_at: at, consecutive_failures: 0, records_last_run: 3 },
        { source: 'newsdata', status: 'throttled', last_run_at: at, last_success_at: at, consecutive_failures: 0, records_last_run: 0 },
      ],
    });
    expect(s.text).toBe('1 of 2 feeds live');
    expect(s.names).toEqual(['USGS', 'NewsData']);
    expect(s.tone).toBe('warn');
    expect(s.downText).toEqual(['NewsData: paused by its rate limit']);
    expect(feedSummary({ sources: [s.down[0]].map((d) => ({ ...d, status: 'ok' as const })) }).tone).toBe('live');
    expect(feedSummary(undefined).text).toBe('No feeds have run yet');
    expect(feedSummary(undefined).tone).toBe('none');
  });
});
