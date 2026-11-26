// Plays one poll of live events (src/api/stream.ts → StreamBatch). For each played story:
//   1. a ripple where its signal is (the strongest evidence's position, else the risk's place),
//   2. a caption beside it (the title and short source), gone after about 3 s,
//   3. after 200 ms (+80 ms per further entity, nearest first) a line in the new level's
//      colour along the great circle to each entity the story reaches (600 ms, then fades),
//   4. a pulse at each entity as its line arrives,
//   5. then its state is applied (the queries invalidated) and the list animates: count-ups,
//      FLIP reorder, rows entering and leaving, and the changed row flashing (./bus).
// With no point for the signal the map steps are skipped. Everything else in the poll, and
// everything on the first poll, after a resync, on the catch-up after a hidden tab, in a poll
// cut short by a failed page (`partial`) or under
// reduced motion, is applied at once. A hidden tab mid-sequence lands every step at once
// (motion.ts `later` and `tween` do).
import type { QueryClient } from '@tanstack/react-query';
import { fetchRiskDetail, type EntityFC, type Risk, type RiskDetail } from '../api/queries';
import type { StreamBatch, StreamEventName, StreamEvents } from '../api/stream';
import { feedName, levelOf } from '../lib/format';
import { later, motionOff } from '../lib/motion';
import { palette } from '../lib/theme';
import { gazetteerReady, placeAll, placeOf } from '../map/geo';
import { noteLiveApply } from './bus';
import { angle, caption, pulse, ripple, travel, type FxMap, type LonLat } from './effects';
import { planBatch, type LiveEvent, type Sequence } from './plan';

export type PlayerDeps = {
  qc: QueryClient;
  ws: string;
  /** The map, once it is ready (null before, or when WebGL failed). */
  getMap: () => FxMap | null;
  /** applyStreamEvent for this workspace. */
  apply: <K extends StreamEventName>(name: K, data: StreamEvents[K]) => void;
};

const DETAIL_WAIT_MS = 1500;
const LINE_DELAY_MS = 200;
const LINE_STEP_MS = 80;
const LINE_MS = 600;
/** The longest a played story waits before its state is applied, whatever happens on the map. */
const SAFETY_MS = 4500;

export class LivePlayer {
  private cancels = new Set<() => void>();
  private pending = new Set<() => void>(); // applies not yet run
  private stopped = false;

  constructor(private deps: PlayerDeps) {}

  /** Apply what is pending now and drop the rest of any running sequence. */
  stop() {
    this.stopped = true;
    for (const c of this.cancels) c();
    this.cancels.clear();
    for (const p of [...this.pending]) p();
    this.pending.clear();
  }

  handle(batch: StreamBatch) {
    if (this.stopped) return;
    if (batch.resync) {
      this.deps.apply('resync', {});
      return;
    }
    // A partial poll (a later page failed) is applied at once, like a catch-up.
    const animate = !batch.initial && !batch.catchUp && !batch.partial && !motionOff();
    const { sequences, instant } = planBatch(batch.events as LiveEvent[], animate);
    const riskInstant = instant.filter((e) => e.name.startsWith('risk.'));
    const otherInstant = instant.filter((e) => !e.name.startsWith('risk.'));
    for (const e of otherInstant) this.applyEvent(e);
    if (!sequences.length) {
      for (const e of riskInstant) this.applyEvent(e);
      if (riskInstant.length && animate) noteLiveApply('count');
      return;
    }
    // The list is one query: the risks past the cap come in with the first played story's
    // update, so the played ones are not shown before their pulse lands.
    let carry: LiveEvent[] = riskInstant;
    for (const s of sequences) {
      const extra = carry;
      carry = [];
      void this.play(s, extra);
    }
  }

  private applyEvent(e: LiveEvent) {
    this.deps.apply(e.name, e.data as never);
  }

  private async play(s: Sequence, extra: LiveEvent[]) {
    let applied = false;
    const apply = () => {
      if (applied) return;
      applied = true;
      this.pending.delete(apply);
      for (const e of [...extra, ...s.events]) this.applyEvent(e);
      noteLiveApply('full', [s.risk.id, s.risk.story?.key ?? '']);
      if (extra.length) noteLiveApply('count');
    };
    this.pending.add(apply);
    const safety = later(apply, SAFETY_MS + s.delay);
    this.cancels.add(safety);

    const detail = await this.detail(s.risk.id);
    if (this.stopped || applied) return;
    let cancel: () => void = () => undefined;
    cancel = later(() => {
      this.cancels.delete(cancel);
      if (this.stopped || applied) return;
      if (motionOff()) return apply();
      try {
        this.run(s.risk, detail, apply);
      } catch {
        apply();
      }
    }, s.delay);
    if (!applied) this.cancels.add(cancel);
  }

  private detail(id: string): Promise<RiskDetail | null> {
    return Promise.race([
      fetchRiskDetail(this.deps.qc, this.deps.ws, id).catch(() => null),
      new Promise<null>((r) => setTimeout(() => r(null), DETAIL_WAIT_MS)),
    ]);
  }

  private run(risk: Risk, detail: RiskDetail | null, apply: () => void) {
    const fx = this.deps.getMap();
    const src = signalPoint(risk, detail);
    if (!fx || !src) return apply();
    const { title, source } = captionText(risk, detail);
    ripple(fx, src);
    caption(fx, src, title, source);
    const targets = this.entityPoints(risk)
      .map((p) => ({ p, d: angle(src, p) }))
      .sort((a, b) => a.d - b.d)
      .map((t) => t.p);
    if (!targets.length) {
      const c = later(apply, LINE_DELAY_MS + LINE_MS);
      this.cancels.add(c);
      return;
    }
    const P = palette();
    const lvl = levelOf(risk.score);
    const color = { low: P.low, med: P.med, high: P.high, crit: P.crit }[lvl];
    let arrived = 0;
    targets.forEach((to, k) => {
      const c = later(() => {
        if (this.stopped) return;
        travel(fx, src, to, color, () => {
          if (!motionOff()) pulse(fx, to, color);
          if (++arrived === targets.length) apply();
        });
      }, LINE_DELAY_MS + LINE_STEP_MS * k);
      this.cancels.add(c);
    });
  }

  /** Where the entities the story reaches are on the map (at least the risk's own entity). */
  private entityPoints(risk: Risk): LonLat[] {
    const fc = this.deps.qc.getQueryData<EntityFC>([this.deps.ws, 'mapEntities']);
    if (!fc || !gazetteerReady()) return [];
    const ids = new Set([risk.entity.id, ...(risk.story?.entities ?? []).map((e) => e.id)]);
    const placed = placeAll(fc.features.filter((f) => ids.has(String(f.id))) as unknown as Parameters<typeof placeAll>[0]);
    return placed.fc.features.map((f) => f.geometry.coordinates as LonLat);
  }
}

/** The strongest evidence item with a position, else the risk's own place. */
export function signalPoint(risk: Pick<Risk, 'lat' | 'lon' | 'ports' | 'countries'>, detail: Pick<RiskDetail, 'evidence'> | null): LonLat | null {
  const ev = [...(detail?.evidence ?? [])].sort((a, b) => (b.contribution ?? 0) - (a.contribution ?? 0));
  for (const e of ev) if (e.snapshot.lat != null && e.snapshot.lon != null) return [e.snapshot.lon, e.snapshot.lat];
  if (!gazetteerReady() && (risk.lat == null || risk.lon == null)) return null;
  return placeOf({ lat: risk.lat, lon: risk.lon, ports: risk.ports, countries: risk.countries });
}

/** The caption: the strongest evidence's title and its feed's short name, else the risk's title. */
export function captionText(risk: Pick<Risk, 'title'>, detail: Pick<RiskDetail, 'evidence'> | null): { title: string; source: string | null } {
  const top = [...(detail?.evidence ?? [])].sort((a, b) => (b.contribution ?? 0) - (a.contribution ?? 0))[0];
  if (!top) return { title: risk.title, source: null };
  const source = top.event ? feedName(top.event.source) : top.anomaly ? top.anomaly.series : null;
  return { title: top.snapshot.title || risk.title, source };
}
