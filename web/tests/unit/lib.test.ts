import { readFileSync } from 'node:fs';
import { describe, expect, it } from 'vitest';
import { ago, initials, levelOf, outOf5, pct, signed } from '../../src/lib/format';
import { __setGazetteer, cleanPortName, placeAll, placeLabel, placeOf, portName, portPoint, unplacedPorts } from '../../src/map/geo';
import { backoffMs } from '../../src/api/stream';
import { adviceSteps, newestSignalAt } from '../../src/screens/RiskView';
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
  it("places every code of the World Shipping Council's Top 50 container ports (tests/fixtures/top50_container_ports.json)", () => {
    type Top50 = { ports: { rank: number; port: string; codes: string[] }[]; left_out: { code: string }[] };
    const top = JSON.parse(readFileSync('../tests/fixtures/top50_container_ports.json', 'utf8')) as Top50;
    const ports = JSON.parse(readFileSync('public/map/ports.json', 'utf8')) as Parameters<typeof __setGazetteer>[1];
    __setGazetteer({}, ports);
    const codes = top.ports.flatMap((p) => p.codes);
    expect(top.ports).toHaveLength(50);
    expect(unplacedPorts(codes)).toEqual([]);
    expect(codes.filter((c) => portName(c) === c)).toEqual([]);
    expect(unplacedPorts(top.left_out.map((l) => l.code))).toEqual(top.left_out.map((l) => l.code));
    // The standard codes the World Port Index files under its own codes, by their port's name.
    expect(['CNSHA', 'SGSIN', 'CNNGB', 'CNSZX', 'CNTAO', 'CNTSN', 'CNXMN', 'IDTPP'].map(portName))
      .toEqual(['Shanghai', 'Singapore', 'Ningbo', 'Shenzhen', 'Qingdao', 'Tianjin Xingang', 'Xiamen', 'Tanjung Priok']);
    expect(portPoint('CNSHA')).toEqual(portPoint('CNSGH'));
    expect(Object.values(ports).filter((p) => /\b(Apt|Airport)\b/.test(p[3] ?? p[2]))).toEqual([]);
  });
  it('lists the codes the table does not have, once each', () => {
    __setGazetteer({}, { NLRTM: [4.4, 51.9, 'Rotterdam'] });
    expect(unplacedPorts(['XXABC', 'NLRTM', 'YYDEF', 'XXABC'])).toEqual(['XXABC', 'YYDEF']);
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

describe('since the last signal', () => {
  // The engine sets last_seen_at (and each linked row's last_linked_at) on every run, every 30 min.
  const run = '2026-11-27T15:33:00Z';
  const row = (published_at: string | null, last_linked_at = run) =>
    ({ snapshot: { title: 't', published_at }, last_linked_at }) as Parameters<typeof newestSignalAt>[0]['evidence'][number];

  it('is the newest signal linked by the latest run, not the run itself', () => {
    const r = { last_seen_at: run, evidence: [row('2026-11-25T08:00:00Z'), row('2026-11-26T21:10:00Z'), row(null)] };
    expect(newestSignalAt(r)).toBe('2026-11-26T21:10:00Z');
    expect(ago(newestSignalAt(r), Date.parse(run))).toBe('18 h ago');
  });

  it('leaves out rows an earlier run linked, and is null without any', () => {
    const older = row('2026-11-27T09:00:00Z', '2026-11-20T10:00:00Z'); // feed row gone: kept, not counted
    expect(newestSignalAt({ last_seen_at: run, evidence: [older, row('2026-11-24T10:00:00Z')] })).toBe('2026-11-24T10:00:00Z');
    expect(newestSignalAt({ last_seen_at: run, evidence: [older] })).toBeNull();
    expect(newestSignalAt({ last_seen_at: run, evidence: [] })).toBeNull();
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
