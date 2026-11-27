// The words around a story (one category at one place, `Risk.story`): what it reaches, why it
// reaches you, how the score moved, and the two-row "receipt" behind the score. Pure functions
// of API rows, so they are tested without the screens (tests/unit/story.test.ts).
import type { components } from '../api/schema';
import { CATEGORIES, KINDS, chokeProse, fmtDate, kindWord, listWords, pct, placeWords, type EntityKind, type Where } from './format';

type S = components['schemas'];
type Risk = S['Risk'];
type Detail = S['RiskDetail'];
type StoryEntity = Risk['story']['entities'][number];
export type CountryName = (iso3: string) => string;

const KIND_ORDER: EntityKind[] = ['route', 'supplier', 'site', 'customer', 'port', 'software', 'currency', 'commodity'];

/** Distinct entities by kind, for every entity in the story (not only the ten listed). */
export function kindCounts(story: Risk['story'], own?: Risk['entity']): [EntityKind, number][] {
  const from = story.kind_counts as Partial<Record<EntityKind, number>> | undefined;
  const counts = new Map<EntityKind, number>();
  if (from && Object.keys(from).length) for (const [k, n] of Object.entries(from)) counts.set(k as EntityKind, n ?? 0);
  else for (const e of story.entities.length ? story.entities : own ? [own] : []) if (e.kind) counts.set(e.kind, (counts.get(e.kind) ?? 0) + 1);
  return [...counts].filter(([, n]) => n > 0).sort((a, b) => b[1] - a[1] || KIND_ORDER.indexOf(a[0]) - KIND_ORDER.indexOf(b[0]));
}

const entityTotal = (story: Risk['story']) => story.entity_count ?? story.entities.length;
/** Every entity of the story is in `story.entities` (it lists at most ten). */
const allListed = (story: Risk['story']) => entityTotal(story) <= story.entities.length;

/** "8 routes and 7 suppliers", "2 sites". */
export function countsText(counts: [EntityKind, number][]): string {
  return listWords(counts.map(([k, n]) => `${n} ${kindWord(k, n)}`));
}

/**
 * The row's third line: what the story reaches. One entity: its name. Several: counts by
 * kind and their cities ("2 sites, in Eindhoven and Rotterdam"), or for a chokepoint what
 * they do ("8 routes and 7 suppliers ship through it").
 */
export function reachLine(r: Pick<Risk, 'entity' | 'story' | 'where'>): string {
  const total = entityTotal(r.story);
  if (total <= 1) return r.story.entities[0]?.name ?? r.entity.name;
  const counts = countsText(kindCounts(r.story, r.entity));
  if (r.where?.match_type === 'chokepoint') return `${counts} ship through it`;
  const cities = [...new Set(r.story.entities.map((e) => e.city?.trim()))];
  if (!allListed(r.story) || cities.some((c) => !c)) return counts;
  const named = cities as string[];
  return `${counts}, in ${named.length > 3 ? `${named.slice(0, 2).join(', ')} and ${named.length - 2} more places` : listWords(named)}`;
}

/** "Atlas Eindhoven Assembly Plant, a critical site" (only a critical entity is called that). */
function named(e: StoryEntity): { text: string; aside: boolean } {
  return e.criticality === 'critical' && e.kind ? { text: `${e.name}, a critical ${kindWord(e.kind, 1)}`, aside: true } : { text: e.name, aside: false };
}
/** Names joined as a sentence subject; an aside before "and" keeps its closing comma. */
function subject(list: StoryEntity[]): string {
  const parts = list.map(named);
  if (parts.length === 1) return parts[0].text;
  const head = parts.slice(0, -1).map((p, i, a) => (i === a.length - 1 && p.aside ? `${p.text},` : p.text));
  return `${head.join(', ')} and ${parts[parts.length - 1].text}`;
}

/**
 * One sentence on why the story reaches you, from its entities, their kind and criticality
 * and the place: "Atlas Eindhoven Assembly Plant, a critical site, and Atlas Rotterdam
 * Distribution Centre are in the Netherlands." `named` says whether it accounts for every
 * entity the story reaches, by name or by count ("8 of your routes and 7 of your suppliers");
 * when it does not, the page lists the others. Falls back to the engine's explanation when it
 * cannot be built.
 */
export function whySentence(r: Pick<Detail, 'entity' | 'story' | 'where' | 'explanation'>, country?: CountryName): { text: string; named: boolean } {
  const w = r.where;
  const fallback = { text: r.explanation, named: false };
  if (!w) return fallback;
  const ents = r.story.entities.length ? r.story.entities : [{ id: r.entity.id, name: r.entity.name, kind: r.entity.kind, city: r.entity.city ?? null }];
  const listed = allListed(r.story) && ents.length <= 3;
  const one = entityTotal(r.story) <= 1;
  const counts = kindCounts(r.story, r.entity);
  const at = placeWords(w as Where, country);
  if (w.match_type === 'chokepoint') {
    const who = listed ? subject(ents) : listWords(counts.map(([k, n]) => `${n} of your ${kindWord(k, n)}`));
    return { text: `${who} ${one ? 'ships' : 'ship'} through ${chokeProse(w.match_value, w.label)}.`, named: true };
  }
  if (!listed) return fallback;
  if (w.match_type === 'country') {
    // a route runs through a country; the others are in it
    if (ents.every((e) => e.kind === 'route')) return { text: `${subject(ents)} ${one ? 'runs' : 'run'} through ${at}.`, named: true };
    if (ents.some((e) => e.kind === 'route')) return fallback;
    return { text: `${subject(ents)} ${one ? 'is' : 'are'} in ${at}.`, named: true };
  }
  if (w.match_type === 'port') return { text: `${subject(ents)} ${one ? 'uses' : 'use'} the port of ${at}.`, named: true };
  return fallback;
}

/**
 * The score's move: against the latest history point at least 24 h old ("since yesterday"),
 * or, for a risk younger than that, its first point ("since it opened"). Null under 1 point.
 */
export function trendText(history: readonly { recorded_at: string; score: number }[], score: number, now = Date.now()): { dir: 'up' | 'down'; text: string } | null {
  if (!history.length) return null;
  const day = now - 24 * 3600_000;
  let ref: (typeof history)[number] | null = null;
  for (const h of history) if (new Date(h.recorded_at).getTime() <= day) ref = h;
  const since = ref ? 'yesterday' : 'it opened';
  const base = ref ?? history[0];
  const delta = score - base.score;
  if (Math.abs(delta) < 1) return null;
  const dir = delta > 0 ? 'up' : 'down';
  return { dir, text: `${dir === 'up' ? 'Up' : 'Down'} ${Math.round(Math.abs(delta))} since ${since}` };
}

/** The evidence rows the latest scoring linked (the engine stamps them with that run's time). */
export function latestRun(r: Pick<Detail, 'evidence' | 'last_seen_at'>) {
  const at = new Date(r.last_seen_at).getTime();
  return r.evidence.filter((e) => new Date(e.last_linked_at).getTime() >= at);
}

/** The reports the latest scoring linked, strongest first (the evidence list's own order). */
export const latestReports = (r: Pick<Detail, 'evidence' | 'last_seen_at'>) => latestRun(r).filter((e) => e.kind === 'event');

/** The calendar day of a time, as the dates on screen show it (local, like fmtDate). */
const dayOf = (iso: string) => {
  const d = new Date(iso);
  return new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime();
};

const NOUNS: Record<string, [string, string]> = {
  geo_radius: ['event', 'events'], sanctions_name: ['listing', 'listings'], software: ['advisory', 'advisories'],
};

export type ReceiptRow = { label: string; value: string; reason: string };

/**
 * The two rows behind the score, so that likely × impact = score (50% × 60% = 30).
 * Likely: what the reports the latest run linked have in common ("4 reports in 2 days name
 * the Netherlands"), or for a market move what moved. Impact: the category, and the entity's
 * share of spend or revenue when known, else its criticality.
 */
export function receipt(r: Pick<Detail, 'category' | 'likelihood' | 'impact' | 'entity' | 'factors' | 'evidence' | 'last_seen_at' | 'where'>, country?: CountryName): [ReceiptRow, ReceiptRow] {
  return [
    { label: 'Likely', value: pct(r.likelihood), reason: likelyReason(r, country) },
    { label: 'Impact', value: pct(r.impact), reason: impactReason(r) },
  ];
}

export function likelyReason(r: Pick<Detail, 'evidence' | 'last_seen_at' | 'where'>, country?: CountryName): string {
  const run = latestRun(r);
  const reports = run.filter((e) => e.kind === 'event');
  if (!reports.length) {
    const a = run.find((e) => e.kind === 'anomaly')?.anomaly ?? r.evidence.find((e) => e.kind === 'anomaly')?.anomaly;
    if (!a) return 'No report is linked any more';
    if (/^[A-Z]{3}$/.test(a.key)) return `An unusual reading for ${placeWords({ match_type: 'country', match_value: a.key, label: a.key }, country)}`;
    return `An unusual move in ${a.key}`;
  }
  const w = r.where;
  const n = reports.length;
  const [one, many] = NOUNS[w?.match_type ?? ''] ?? ['report', 'reports'];
  const times = reports.map((e) => e.snapshot.published_at).filter((t): t is string => !!t && !Number.isNaN(new Date(t).getTime()));
  let when = '';
  if (times.length) {
    // calendar days from the oldest report to the newest (rounded: a day can be 23 or 25 hours)
    const days = Math.round((Math.max(...times.map(dayOf)) - Math.min(...times.map(dayOf))) / 86400_000) + 1;
    when = days > 1 ? ` in ${days} days` : ` on ${fmtDate(times[0])}`;
  }
  const head = `${n} ${n === 1 ? one : many}${when}`;
  const s = n === 1 ? 's' : '';
  if (!w) return head;
  const at = placeWords(w as Where, country);
  switch (w.match_type) {
    case 'country': case 'sanctions_name': case 'software': case 'company_name':
      return `${head} name${s} ${at}`;
    case 'chokepoint': case 'port': case 'commodity': case 'currency':
      return `${head} mention${s} ${w.match_type === 'port' ? `the port of ${at}` : at}`;
    case 'geo_radius':
      return `${head} within ${w.match_value} of ${w.label}`;
  }
  return head;
}

const AT_KIND: Record<EntityKind, string> = {
  site: 'at a site', route: 'on a route', supplier: 'with a supplier', customer: 'with a customer', port: 'at a port',
  software: 'in software', currency: 'in a currency', commodity: 'in a commodity',
};

export function impactReason(r: Pick<Detail, 'category' | 'entity' | 'factors'>): string {
  const cat = CATEGORIES[r.category].name;
  const f = r.factors.impact;
  if (f.exposure_share != null) {
    const of = r.entity.kind === 'customer' ? 'revenue' : r.entity.kind === 'supplier' ? 'spend' : 'spend and revenue';
    return `${cat}, on ${pct(f.exposure_share)} of your ${of}`;
  }
  const at = AT_KIND[r.entity.kind] ?? `with this ${KINDS[r.entity.kind]?.one.toLowerCase() ?? 'entity'}`;
  return f.criticality ? `${cat}, ${at} you rated ${f.criticality}` : `${cat}, ${at} you have not rated`;
}

/** "All are Low", "2 are High, 16 Low", "It is Medium": the levels of the open stories. */
export function levelsLine(scores: readonly number[], name: (s: number) => string): string {
  if (!scores.length) return '';
  const order: string[] = [];
  const counts = new Map<string, number>();
  for (const s of [...scores].sort((a, b) => b - a)) {
    const l = name(s);
    if (!counts.has(l)) order.push(l);
    counts.set(l, (counts.get(l) ?? 0) + 1);
  }
  if (order.length === 1) return scores.length === 1 ? `It is ${order[0]}` : `All are ${order[0]}`;
  return order.map((l, i) => (i === 0 ? `${counts.get(l)} ${counts.get(l) === 1 ? 'is' : 'are'} ${l}` : `${counts.get(l)} ${l}`)).join(', ');
}
