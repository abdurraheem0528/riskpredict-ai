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
