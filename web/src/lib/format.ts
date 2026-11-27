// Wording and number rules from System-Type: sentence case, tabular figures, units after
// the value, signed changes, 24-hour clock. Levels and their colours from System-Color.
import type { IconName } from '../ui/Icon';
import type { components } from '../api/schema';

export type RiskCategory = components['schemas']['RiskCategory'];
export type EntityKind = components['schemas']['EntityKind'];

export type Level = 'low' | 'med' | 'high' | 'crit';
/**
 * Level names and their light-theme colours (styles.css has both themes as `--<lvl>-fill`,
 * `-tint`, `-text`; UI should use the `l-<lvl>` / `tx-<lvl>` classes, and the map reads the
 * fills from the theme's palette). Low is neutral grey: only Medium and up are coloured.
 */
export const LEVELS: Record<Level, { name: string; fill: string; tint: string; text: string }> = {
  low: { name: 'Low', fill: '#9AA1A9', tint: '#EEF0F2', text: '#5F646C' },
  med: { name: 'Medium', fill: '#E9A190', tint: '#FBEAE6', text: '#9C3522' },
  high: { name: 'High', fill: '#DD6248', tint: '#FCE5E0', text: '#A42D16' },
  crit: { name: 'Critical', fill: '#B3200F', tint: '#FADBD5', text: '#8F1A0B' },
};

/** 60 is where risk becomes elevated (System-Controls); 80 and up is critical. */
export function levelOf(score: number): Level {
  if (score >= 80) return 'crit';
  if (score >= 60) return 'high';
  if (score >= 40) return 'med';
  return 'low';
}

export const scoreText = (score: number | null | undefined) =>
  score == null ? '–' : String(Math.round(score));

export const CATEGORIES: Record<RiskCategory, { name: string; icon: IconName }> = {
  geopolitical: { name: 'Geopolitical', icon: 'geopolitical' },
  logistics: { name: 'Shipping', icon: 'supply' },
  natural_hazard: { name: 'Hazards', icon: 'hazard' },
  sanctions: { name: 'Sanctions', icon: 'sanctions' },
  financial: { name: 'Financial', icon: 'financial' },
  cyber: { name: 'Cyber', icon: 'cyber' },
  trade_policy: { name: 'Trade policy', icon: 'geopolitical' },
  labour: { name: 'Labour', icon: 'supply' },
  other: { name: 'Other', icon: 'info' },
};
/** The order chips appear in on Main (design order first). */
export const CATEGORY_ORDER: RiskCategory[] = [
  'geopolitical', 'logistics', 'natural_hazard', 'sanctions', 'financial', 'cyber', 'trade_policy', 'labour', 'other',
];

export const KINDS: Record<EntityKind, { one: string; many: string }> = {
  supplier: { one: 'Supplier', many: 'suppliers' },
  site: { one: 'Site', many: 'sites' },
  customer: { one: 'Customer', many: 'customers' },
  route: { one: 'Route', many: 'routes' },
  port: { one: 'Port', many: 'ports' },
  currency: { one: 'Currency', many: 'currencies' },
  commodity: { one: 'Commodity', many: 'commodities' },
  software: { one: 'Software', many: 'software' },
};

export function plural(n: number, one: string, many = `${one}s`) {
  return `${fmtInt(n)} ${n === 1 ? one : many}`;
}

const intFmt = new Intl.NumberFormat('en-GB', { maximumFractionDigits: 0 });
export const fmtInt = (n: number) => intFmt.format(n);
/** 0.72 → "72%" (no space before the sign). */
export const pct = (x: number | null | undefined, digits = 0) =>
  x == null ? '–' : `${(x * 100).toFixed(digits)}%`;
/** Always signed: +4, −2, 0. */
export const signed = (n: number, digits = 0) => {
  const v = Number(n.toFixed(digits));
  if (v === 0) return '0';
  return v > 0 ? `+${v.toFixed(digits)}` : `−${Math.abs(v).toFixed(digits)}`;
};
/** Impact 0–1 shown on the design's 5-point scale: "4/5". */
export const outOf5 = (x: number) => Math.max(1, Math.min(5, Math.round(x * 5)));

const dayFmt = new Intl.DateTimeFormat('en-GB', { weekday: 'short', day: 'numeric', month: 'short' });
const dateFmt = new Intl.DateTimeFormat('en-GB', { day: 'numeric', month: 'short' });
const timeFmt = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false });
const hmFmt = new Intl.DateTimeFormat('en-GB', { hour: '2-digit', minute: '2-digit', hour12: false });

/** "Sun 23 Nov" */
export const fmtDay = (iso: string | Date) => dayFmt.format(new Date(iso)).replace(',', '');
/** "9 Nov" */
export const fmtDate = (iso: string | Date) => dateFmt.format(new Date(iso));
/** "14:00:12" */
export const fmtTime = (iso: string | Date) => timeFmt.format(new Date(iso));
export const fmtHm = (iso: string | Date) => hmFmt.format(new Date(iso));

/** "2 min ago", "1 h ago", "3 days ago", then a date. */
export function ago(iso: string | null | undefined, now = Date.now()): string {
  if (!iso) return 'never';
  const s = Math.max(0, (now - new Date(iso).getTime()) / 1000);
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  const d = Math.floor(s / 86400);
  if (d < 14) return `${d} ${d === 1 ? 'day' : 'days'} ago`;
  return fmtDate(iso);
}

export function initials(name: string | null | undefined, email: string): string {
  const src = (name ?? '').trim();
  if (src) {
    const parts = src.split(/\s+/).filter(Boolean);
    return ((parts[0]?.[0] ?? '') + (parts.length > 1 ? parts[parts.length - 1][0] : '')).toUpperCase();
  }
  return email.slice(0, 2).toUpperCase();
}

/** Feed source ids as the designs name them; unknown ids are shown as they are. */
const FEED_NAMES: Record<string, string> = {
  gdelt: 'GDELT', newsdata: 'NewsData', cisa_kev: 'CISA KEV', nvd: 'NVD', portwatch: 'PortWatch',
  gdacs: 'GDACS', usgs: 'USGS', fred: 'FRED', ofac: 'OFAC', osv: 'OSV', otx: 'OTX', ecb: 'ECB',
  worldbank: 'World Bank', frankfurter: 'Frankfurter', eonet: 'EONET', comtrade: 'UN Comtrade',
  currents: 'Currents', eia: 'EIA', epss: 'EPSS', tiingo: 'Tiingo', ofac_sdn: 'OFAC',
};
export const feedName = (id: string) => FEED_NAMES[id] ?? FEED_NAMES[id.split(/[._:]/)[0]] ?? id;

// --- places in running text (the Risks list, the risk page) ------------------------------------

/**
 * Countries English names with "the", and Natural Earth's short forms ("Dem. Rep. Congo",
 * "Marshall Is.") written out. Everything else is the gazetteer's name.
 */
const PROSE_COUNTRIES: Record<string, string> = {
  NLD: 'the Netherlands', USA: 'the United States', GBR: 'the United Kingdom', PHL: 'the Philippines',
  ARE: 'the United Arab Emirates', CZE: 'the Czech Republic', DOM: 'the Dominican Republic', BHS: 'the Bahamas',
  GMB: 'the Gambia', MDV: 'the Maldives', MHL: 'the Marshall Islands', SLB: 'the Solomon Islands',
  CAF: 'the Central African Republic', COM: 'the Comoros', COD: 'the Democratic Republic of the Congo',
  SSD: 'South Sudan', BIH: 'Bosnia and Herzegovina', GNQ: 'Equatorial Guinea', ESH: 'Western Sahara',
  ATG: 'Antigua and Barbuda', VCT: 'Saint Vincent and the Grenadines', KNA: 'Saint Kitts and Nevis',
};
/** "NLD" → "the Netherlands", "TWN" → "Taiwan". `name` is the gazetteer's name for the code. */
export function countryProse(iso3: string, name?: string): string {
  return PROSE_COUNTRIES[iso3] ?? (name ?? iso3).replace(/\bIs\.$/, 'Islands');
}

/** Chokepoints that take "the" in a sentence ("the Suez Canal"); Bab el-Mandeb does not. */
const THE_CHOKEPOINTS = new Set(['suez', 'panama', 'hormuz', 'malacca', 'singapore', 'bosporus', 'gibraltar', 'dover', 'taiwan_strait', 'cape_of_good_hope']);
export const chokeProse = (id: string, name: string) => (THE_CHOKEPOINTS.has(id) ? `the ${name}` : name);

export type Where = { match_type: string; match_value: string; label: string };

/**
 * The words for where a match is: a country as running text names it, a chokepoint with
 * "the" where it takes one, else the API's label. `country` is the gazetteer lookup.
 */
export function placeWords(w: Where, country: (iso3: string) => string = (c) => c): string {
  if (w.match_type === 'country' && /^[A-Z]{3}$/.test(w.match_value)) {
    // the API's label already reads as prose; the gazetteer is used only when it has the code
    const name = country(w.match_value);
    return countryProse(w.match_value, name !== w.match_value ? name : w.label.replace(/^the /, ''));
  }
  if (w.match_type === 'chokepoint') return chokeProse(w.match_value, w.label);
  return w.label;
}

const HAZARDS: Record<string, string> = {
  earthquake: 'Earthquake', tropical_cyclone: 'Tropical cyclone', flood: 'Flood', volcano: 'Volcanic activity',
  drought: 'Drought', wildfire: 'Wildfire', tsunami: 'Tsunami', severe_storm: 'Severe storm', landslide: 'Landslide',
  temperature_extreme: 'Extreme temperature',
};
/** "hazard.tropical_cyclone" → "Tropical cyclone"; anything else → "Natural hazard". */
export function hazardName(type: string | null | undefined): string {
  const h = (type ?? '').replace(/^hazard\./, '');
  return HAZARDS[h] ?? (type?.startsWith('hazard.') && h ? h.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase()) : 'Natural hazard');
}

/**
 * The row's first line: what is happening, and where. "Shipping through Bab el-Mandeb",
 * "Trade policy in the Netherlands", "Cyber threats in Taiwan", "Earthquake near Hsinchu".
 * `ctx.city` is the reached entity's city (hazards), `ctx.leadType` the lead report's type.
 */
export function placePhrase(
  category: RiskCategory,
  w: Where | null | undefined,
  ctx: { city?: string | null; leadType?: string | null; country?: (iso3: string) => string; fallback?: string } = {},
): string {
  if (!w) return ctx.fallback ?? CATEGORIES[category].name;
  const at = placeWords(w, ctx.country);
  const mt = w.match_type;
  const near = mt === 'chokepoint' || mt === 'geo_radius';
  switch (category) {
    case 'logistics':
      if (mt === 'chokepoint') return `Shipping through ${at}`;
      if (mt === 'port') return `Shipping at ${at}`;
      if (mt === 'country') return `Shipping in ${at}`;
      break;
    case 'geopolitical':
      if (mt === 'chokepoint') return `Conflict near ${at}`;
      if (mt === 'country') return `Conflict in ${at}`;
      break;
    case 'trade_policy':
      if (mt === 'country') return `Trade policy in ${at}`;
      break;
    case 'sanctions':
      if (mt === 'sanctions_name') return `${w.label} on a sanctions list`;
      if (mt === 'country') return `Sanctions in ${at}`;
      break;
    case 'labour':
      if (mt === 'country') return `Labour unrest in ${at}`;
      break;
    case 'cyber':
      if (mt === 'software') return `A flaw in ${w.label}`;
      if (mt === 'country') return `Cyber threats in ${at}`;
      break;
    case 'natural_hazard':
      if (mt === 'geo_radius') return `${hazardName(ctx.leadType)} near ${ctx.city || w.label}`;
      return `${hazardName(ctx.leadType)} ${near ? 'near' : 'in'} ${at}`;
    case 'financial':
      if (mt === 'currency') return `${w.match_value.split('/').pop()} swings`;
      break;
  }
  if (mt === 'company_name') return `${CATEGORIES[category].name} news about ${w.label}`;
  if (mt === 'commodity') return `${CATEGORIES[category].name} news on ${w.label}`;
  return `${CATEGORIES[category].name} ${near ? 'near' : 'in'} ${at}`;
}

/** Lower-case kind words: "site" / "sites". */
export const kindWord = (kind: EntityKind, n: number) => (n === 1 ? KINDS[kind].one.toLowerCase() : KINDS[kind].many);

/** "A", "A and B", "A, B and C". */
export function listWords(items: readonly string[]): string {
  if (items.length <= 1) return items[0] ?? '';
  return `${items.slice(0, -1).join(', ')} and ${items[items.length - 1]}`;
}
