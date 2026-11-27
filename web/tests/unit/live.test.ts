import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { QueryClient } from '@tanstack/react-query';
import { MAX_FRESH_DOTS, MAX_SEQUENCES, SEQUENCE_GAP_MS, freshDots, planBatch, recency, scoreRose, scoredText, type LiveEvent } from '../../src/live/plan';
import { LivePlayer, captionText, signalPoint } from '../../src/live/player';
import { __resetLive, liveRecent, takeFlashes } from '../../src/live/bus';
import { transitionFor } from '../../src/live/transitions';
import { sparkLayout, SPARK_H, SPARK_W } from '../../src/live/Sparkline';
import type { StreamBatch } from '../../src/api/stream';
import { riskDetail, risks, WS } from '../fixtures/data';

const risk = (i: number, score: number, story = `s${i}`) => ({ ...risks[0], id: `r${i}`, score, story: { key: story, risk_count: 1, entities: [] } });
const created = (i: number, score: number, story?: string): LiveEvent => ({ name: 'risk.created', data: { risk: risk(i, score, story), previous_score: null } });
const updated = (i: number, score: number, prev: number | null): LiveEvent => ({ name: 'risk.updated', data: { risk: risk(i, score), previous_score: prev } });

describe('live plan', () => {
  it('a matched risk is a new one, or an update whose score rose', () => {
    expect(scoreRose(created(1, 40))).toBe(true);
    expect(scoreRose(updated(1, 52, 47))).toBe(true);
    expect(scoreRose(updated(1, 47, 52))).toBe(false);
    expect(scoreRose(updated(1, 47, 47))).toBe(false); // a status change
    expect(scoreRose(updated(1, 47, null))).toBe(false);
    expect(scoreRose({ name: 'risk.resolved', data: { risk: risk(1, 10) } })).toBe(false);
  });

  it('plays at most 3 stories a poll, highest score first and 400 ms apart; the rest at once', () => {
    const events = [created(1, 30), created(2, 90), updated(3, 60, 50), created(4, 75), created(5, 20), { name: 'alert.created', data: {} } as LiveEvent, updated(6, 40, 45)];
    const { sequences, instant } = planBatch(events, true);
    expect(MAX_SEQUENCES).toBe(3);
    expect(sequences.map((s) => s.risk.id)).toEqual(['r2', 'r4', 'r3']);
    expect(sequences.map((s) => s.delay)).toEqual([0, SEQUENCE_GAP_MS, 2 * SEQUENCE_GAP_MS]);
    // the rest, in their order: the lower-scored matches, the alert, the update that fell
    expect(instant).toEqual([events[0], events[4], events[5], events[6]]);
  });

  it('a story that reaches several entities plays once, with every one of its events', () => {
    const events = [created(1, 50, 'one'), created(2, 70, 'one'), created(3, 60, 'two')];
    const { sequences, instant } = planBatch(events, true);
    expect(sequences.map((s) => s.risk.id)).toEqual(['r2', 'r3']);
    expect(sequences[0].events).toEqual([events[0], events[1]]);
    expect(instant).toEqual([]);
  });

  it('plays nothing when it must not animate', () => {
    const events = [created(1, 90), created(2, 80)];
    expect(planBatch(events, false)).toEqual({ sequences: [], instant: events });
  });

  it('feed dots fade with age, from full when new to about 30% at seven days', () => {
    const D = 86_400_000;
    expect(recency(0)).toBe(1);
    expect(recency(3.5 * D)).toBeCloseTo(0.65, 5);
    expect(recency(7 * D)).toBeCloseTo(0.3, 5);
    expect(recency(30 * D)).toBeCloseTo(0.3, 5);
  });

  it('at most 24 fresh dots fade in, the newest; nothing is fresh on first load', () => {
    const feats = Array.from({ length: 40 }, (_, i) => ({ id: `e${i}`, at: i }));
    expect(freshDots(null, feats).size).toBe(0);
    const known = new Set(feats.slice(0, 5).map((f) => f.id));
    const fresh = freshDots(known, feats);
    expect(fresh.size).toBe(MAX_FRESH_DOTS);
    expect(fresh.has('e39')).toBe(true);
    expect(fresh.has('e5')).toBe(false); // the oldest new ones appear at once
    expect(fresh.has('e0')).toBe(false); // known
  });

  it('says when the scores last changed', () => {
    const now = Date.parse('2026-11-26T12:00:00Z');
    const at = (s: number) => new Date(now - s * 1000).toISOString();
    expect(scoredText(at(1), now)).toBe('Scored just now');
    expect(scoredText(at(12), now)).toBe('Scored 12 s ago');
    expect(scoredText(at(185), now)).toBe('Scored 3 min ago');
    expect(scoredText(at(2 * 3600), now)).toBe('Scored 2 h ago');
    expect(scoredText(null, now)).toBe('Not scored yet');
  });
});

describe('live player', () => {
  beforeEach(() => __resetLive());
  afterEach(() => vi.useRealTimers());

  const batch = (events: LiveEvent[], extra: Partial<StreamBatch> = {}): StreamBatch => ({ events, initial: false, catchUp: false, resync: false, partial: false, ...extra });
  function make() {
    const applied: string[] = [];
    const player = new LivePlayer({
      qc: new QueryClient(),
      ws: WS,
      getMap: () => null,
      apply: (name, data) => applied.push(`${name}:${(data as { risk?: { id: string } }).risk?.id ?? ''}`),
    });
    return { player, applied };
  }

  it('applies everything at once on the first poll, on the catch-up after a hidden tab, for a poll cut short, and on resync', () => {
    for (const flags of [{ initial: true }, { catchUp: true }, { partial: true }]) {
      const { player, applied } = make();
      player.handle(batch([created(1, 90), created(2, 80)], flags));
      expect(applied).toEqual(['risk.created:r1', 'risk.created:r2']);
      expect(liveRecent('full')).toBe(false); // nothing animates
    }
    const { player, applied } = make();
    player.handle(batch([created(1, 90)], { resync: true }));
    expect(applied).toEqual(['resync:']);
  });

  it('applies at once while the tab is hidden', () => {
    const hidden = vi.spyOn(document, 'hidden', 'get').mockReturnValue(true);
    const { player, applied } = make();
    player.handle(batch([created(1, 90)]));
    expect(applied).toEqual(['risk.created:r1']);
    hidden.mockRestore();
  });

  it('a played risk is applied when its sequence lands (no map: its turn in the queue), then flashes', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { player, applied } = make();
    const events = [created(1, 30), created(2, 90), created(3, 70), created(4, 60), created(5, 50)];
    player.handle(batch(events));
    // nothing yet: the played ones wait for their sequence, and the rest come with the first
    expect(applied).toEqual([]);
    await vi.advanceTimersByTimeAsync(1600); // detail lookups give up after 1.5 s
    await vi.waitFor(() => expect(applied.length).toBeGreaterThanOrEqual(3));
    expect(applied.slice(0, 3)).toEqual(['risk.created:r1', 'risk.created:r5', 'risk.created:r2']);
    await vi.advanceTimersByTimeAsync(2 * SEQUENCE_GAP_MS + 10);
    expect(applied).toEqual(['risk.created:r1', 'risk.created:r5', 'risk.created:r2', 'risk.created:r3', 'risk.created:r4']);
    expect(liveRecent('full')).toBe(true);
    expect([...takeFlashes(['r2', 'r3', 'r4', 'r1'])].sort()).toEqual(['r2', 'r3', 'r4']);
    player.stop();
  });

  it('a stopped player applies what is pending at once', () => {
    const { player, applied } = make();
    player.handle(batch([created(2, 90)]));
    expect(applied).toEqual([]);
    player.stop();
    expect(applied).toEqual(['risk.created:r2']);
  });

  it('the ripple goes to the chokepoint of a chokepoint story, else where the strongest evidence is, else the risk’s place; the caption is the lead headline', () => {
    const r = risks.find((x) => x.where?.match_type === 'chokepoint')!;
    const d = riskDetail(r);
    const at = (id: string): [number, number] | null => (id === 'bab_el_mandeb' ? [43.33, 12.58] : null);
    expect(signalPoint(r, d, at)).toEqual([43.33, 12.58]);
    const quake = { ...d, evidence: [{ ...d.evidence[0], snapshot: { ...d.evidence[0].snapshot, lat: 23.9, lon: 121.6 } }] };
    expect(signalPoint({ ...r, where: null }, quake)).toEqual([121.6, 23.9]);
    expect(signalPoint({ ...r, where: null, lat: null, lon: null, ports: ['CNYTN'] }, null)).toEqual([114.27, 22.57]);
    expect(signalPoint({ ...r, where: null, lat: null, lon: null, ports: [], countries: [] }, null)).toBeNull();
    expect(captionText(r, d)).toEqual({ title: 'Houthis Strike Saudi Ports on Red Sea', source: 'NewsData' });
    expect(captionText({ ...r, lead: null }, d)).toEqual({ title: d.evidence[0].snapshot.title, source: 'NewsData' });
    expect(captionText({ ...r, lead: null }, null)).toEqual({ title: r.title, source: null });
  });
});

describe('view transitions and the sparkline', () => {
  it('morphs only between the list and a risk', () => {
    expect(transitionFor('/', '/risks/abc')).toEqual({ dir: 'fwd', id: 'abc' });
    expect(transitionFor('/register?entity=x', '/risks/abc')).toEqual({ dir: 'fwd', id: 'abc' });
    expect(transitionFor('/risks/abc', '/')).toEqual({ dir: 'back', id: 'abc' });
    expect(transitionFor('/risks/abc', '/scenarios?risk=abc')).toBeNull();
    expect(transitionFor('/', '/connect')).toBeNull();
  });

  it('lays the sparkline out in 160 × 44', () => {
    const pts = sparkLayout([40, 45, 50]);
    expect(pts[0][0]).toBe(0);
    expect(pts[2][0]).toBe(SPARK_W);
    for (const [, y] of pts) {
      expect(y).toBeGreaterThan(0);
      expect(y).toBeLessThan(SPARK_H);
    }
    expect(pts[2][1]).toBeLessThan(pts[0][1]); // higher score, higher on the chart
  });
});
