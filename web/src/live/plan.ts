// What one poll of GET /v1/events plays, as plain functions (tests/unit/live.test.ts).
//
// Only real events animate, and only when they arrive while someone is watching: never on
// the first poll (the page was just loaded), after a resync, on the catch-up poll after the
// tab was hidden, or under reduced motion. A re-score can bring dozens of risks at once (the
// first worker run after a deploy does), so a poll plays the full sequence for at most
// three stories, highest score first, 400 ms apart; the rest are applied at once with only
// the count-up.
import type { Risk } from '../api/queries';
import type { StreamEventName } from '../api/stream';

export const MAX_SEQUENCES = 3;
export const SEQUENCE_GAP_MS = 400;
/** New feed dots that fade in at a time; any more appear at once. */
export const MAX_FRESH_DOTS = 24;

export type LiveEvent = { name: StreamEventName; data: unknown };
type RiskEventData = { risk: Risk; previous_score?: number | null };

export const isRiskEvent = (e: LiveEvent) => e.name === 'risk.created' || e.name === 'risk.updated' || e.name === 'risk.resolved';

/** A matched risk: a new one, or an update whose score rose. */
export function scoreRose(e: LiveEvent): boolean {
  if (e.name === 'risk.created') return true;
  if (e.name !== 'risk.updated') return false;
  const d = e.data as RiskEventData;
  const prev = d.previous_score;
  return prev != null && d.risk?.score != null && d.risk.score > prev;
}

/** One story played on the map: its strongest risk, and every event to apply when it lands. */
export type Sequence = { risk: Risk; events: LiveEvent[]; delay: number };
export type Plan = { sequences: Sequence[]; instant: LiveEvent[] };

/**
 * Split a poll's events: up to MAX_SEQUENCES stories to play (highest score first, one per
 * story however many entities it reached), and the rest to apply at once. Events keep their
 * order within each group. With `animate` false everything is instant.
 */
export function planBatch(events: readonly LiveEvent[], animate: boolean): Plan {
  if (!animate) return { sequences: [], instant: [...events] };
  type Group = { key: string; risk: Risk; score: number; events: LiveEvent[]; matched: boolean; first: number };
  const groups = new Map<string, Group>();
  const instant: LiveEvent[] = [];
  events.forEach((e, i) => {
    if (!isRiskEvent(e)) {
      instant.push(e);
      return;
    }
    const risk = (e.data as RiskEventData).risk;
    if (!risk?.id) {
      instant.push(e);
      return;
    }
    const key = risk.story?.key ?? `risk:${risk.id}`;
    const g = groups.get(key) ?? { key, risk, score: -1, events: [], matched: false, first: i };
    g.events.push(e);
    const matched = scoreRose(e) && risk.status !== 'resolved' && risk.status !== 'dismissed';
    if (matched && risk.score >= g.score) {
      g.risk = risk;
      g.score = risk.score;
    }
    g.matched ||= matched;
    groups.set(key, g);
  });
  const ranked = [...groups.values()].filter((g) => g.matched).sort((a, b) => b.score - a.score || a.first - b.first);
  const played = new Set(ranked.slice(0, MAX_SEQUENCES).map((g) => g.key));
  const sequences = ranked
    .slice(0, MAX_SEQUENCES)
    .map((g, i): Sequence => ({ risk: g.risk, events: g.events, delay: i * SEQUENCE_GAP_MS }));
  for (const g of groups.values()) if (!played.has(g.key)) instant.push(...g.events);
  // keep the original order of the instant events
  const order = new Map(events.map((e, i) => [e, i]));
  instant.sort((a, b) => (order.get(a) ?? 0) - (order.get(b) ?? 0));
  return { sequences, instant };
}

// --- feed dots -------------------------------------------------------------------------------

const DAY = 86_400_000;
/** A feed dot's opacity by age: full when new, about 30% at seven days and older. */
export function recency(ageMs: number): number {
  const a = Math.max(0, ageMs) / (7 * DAY);
  return Math.max(0.3, 1 - 0.7 * Math.min(1, a));
}

/**
 * The dots that are new since the last data (ids not in `known`), newest first, at most
 * MAX_FRESH_DOTS: these fade in. With no previous data (first load) nothing is new.
 */
export function freshDots<F extends { id: string; at: number }>(known: ReadonlySet<string> | null, features: readonly F[]): Set<string> {
  if (!known) return new Set();
  return new Set(
    features
      .filter((f) => !known.has(f.id))
      .sort((a, b) => b.at - a.at)
      .slice(0, MAX_FRESH_DOTS)
      .map((f) => f.id),
  );
}

// --- the live dot ------------------------------------------------------------------------------

/** The worker scores after every feeds run (every 30 min); two hours without one is stale. */
export const STALE_MS = 2 * 3600_000;

/** "Scored just now", "Scored 12 s ago", "Scored 3 min ago", "Scored 2 h ago", then days. */
export function scoredText(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'Not scored yet';
  const s = Math.max(0, Math.floor((now - new Date(iso).getTime()) / 1000));
  if (s < 5) return 'Scored just now';
  if (s < 60) return `Scored ${s} s ago`;
  if (s < 3600) return `Scored ${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `Scored ${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  return `Scored ${d} ${d === 1 ? 'day' : 'days'} ago`;
}
