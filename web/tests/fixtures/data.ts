// Test-only fixtures (MSW). Invented rows for an invented company; they exist only under
// tests/ and never reach the built bundle (scripts/check-bundle.mjs checks the design's
// sample names are absent from dist/).
import type { components } from '../../src/api/schema';

type S = components['schemas'];
export const API = 'http://api.test';
export const WS = '7d1f3c2a-0000-4000-8000-000000000001';
const NOW = new Date('2026-11-24T13:12:00Z').getTime();
export const iso = (msAgo: number) => new Date(NOW - msAgo).toISOString();
const H = 3600_000;
const D = 24 * H;

export const user: S['User'] = { id: '7d1f3c2a-0000-4000-8000-0000000000aa', email: 'mira.okafor@kestrel.test', display_name: 'Mira Okafor', created_at: iso(30 * D) };
export const workspace: S['Workspace'] = { id: WS, name: 'Kestrel Components', base_currency: 'EUR', alert_min_score: 60, share_watch_terms: false, created_at: iso(30 * D) };

export const tokens = (): S['Tokens'] => ({
  token_type: 'Bearer',
  access_token: 'test-access-token',
  access_token_expires_at: new Date(Date.now() + 15 * 60_000).toISOString(),
  refresh_token: 'test-refresh-token-0000000000000000000000000',
  refresh_token_expires_at: new Date(Date.now() + 30 * D).toISOString(),
});

export const providers: S['Provider'][] = [
  { id: 'google_sheets', name: 'Google Sheets', kind: 'direct', available: true },
  { id: 'composio:airtable', name: 'Airtable', kind: 'composio', available: true },
];

const FEEDS = ['gdelt', 'newsdata', 'cisa_kev', 'nvd', 'portwatch', 'gdacs', 'usgs', 'fred', 'ofac', 'osv'];
export const health = {
  sources: FEEDS.map((s, i): S['SourceHealth'] => ({
    source: s,
    status: s === 'newsdata' ? 'throttled' : i % 4 === 3 ? 'empty' : 'ok',
    last_run_at: iso((i + 1) * 7 * 60_000),
    last_success_at: s === 'newsdata' ? iso(20 * H) : iso((i + 1) * 7 * 60_000),
    last_error: s === 'newsdata' ? 'daily limit reached' : null,
    consecutive_failures: 0,
    records_last_run: 10 + i * 3,
  })),
  analyses: [
    { analysis: 'tagging' as const, status: 'ok', last_run_at: iso(40 * 60_000), last_success_at: iso(40 * 60_000) },
    { analysis: 'anomalies' as const, status: 'ok', last_run_at: iso(3 * H), last_success_at: iso(3 * H) },
  ],
};

// Feed events: a deterministic scatter over places where hazards and news really happen.
const PLACES: [number, number, string, string][] = [
  [139.7, 35.7, 'JPN', 'hazard.earthquake'], [121.5, 25.0, 'TWN', 'news'], [120.3, 22.6, 'TWN', 'news'], [114.1, 22.5, 'CHN', 'hazard.tropical_cyclone'],
  [113.3, 23.1, 'CHN', 'port.disruption'], [121.5, 31.2, 'CHN', 'news'], [106.8, -6.2, 'IDN', 'hazard.flood'], [100.5, 13.7, 'THA', 'news'],
  [103.8, 1.3, 'SGP', 'port.disruption'], [100.3, 5.4, 'MYS', 'news'], [106.7, 10.8, 'VNM', 'hazard.flood'], [80.3, 13.1, 'IND', 'hazard.flood'],
  [72.9, 19.1, 'IND', 'news'], [77.2, 28.6, 'IND', 'news'], [67.0, 24.9, 'PAK', 'hazard.flood'], [55.3, 25.3, 'ARE', 'sanctions'],
  [51.4, 35.7, 'IRN', 'sanctions'], [43.3, 12.6, 'YEM', 'news'], [32.5, 30.0, 'EGY', 'port.disruption'], [29.0, 41.0, 'TUR', 'news'],
  [37.6, 55.8, 'RUS', 'sanctions'], [30.5, 50.4, 'UKR', 'news'], [21.0, 52.2, 'POL', 'news'], [13.4, 52.5, 'DEU', 'news'],
  [4.5, 51.9, 'NLD', 'port.disruption'], [2.35, 48.9, 'FRA', 'news'], [-0.1, 51.5, 'GBR', 'cyber'], [-3.7, 40.4, 'ESP', 'hazard.wildfire'],
  [12.5, 41.9, 'ITA', 'hazard.earthquake'], [23.7, 38.0, 'GRC', 'hazard.wildfire'], [36.8, -1.3, 'KEN', 'news'], [3.4, 6.5, 'NGA', 'news'],
  [18.4, -33.9, 'ZAF', 'news'], [-46.6, -23.5, 'BRA', 'news'], [-58.4, -34.6, 'ARG', 'news'], [-70.6, -33.4, 'CHL', 'hazard.earthquake'],
  [-77.0, -12.0, 'PER', 'hazard.earthquake'], [-99.1, 19.4, 'MEX', 'news'], [-118.2, 34.0, 'USA', 'hazard.wildfire'], [-122.4, 37.8, 'USA', 'cyber'],
  [-74.0, 40.7, 'USA', 'news'], [-87.6, 41.9, 'USA', 'news'], [-95.4, 29.8, 'USA', 'hazard.tropical_cyclone'], [-79.4, 43.7, 'CAN', 'news'],
  [151.2, -33.9, 'AUS', 'hazard.wildfire'], [174.8, -41.3, 'NZL', 'hazard.earthquake'], [126.9, 37.6, 'KOR', 'news'], [125.6, 7.1, 'PHL', 'hazard.tropical_cyclone'],
];
export const events: S['FeedEventFeatureCollection'] = {
  type: 'FeatureCollection',
  truncated: false,
  features: Array.from({ length: 140 }, (_, i) => {
    const [lon, lat, c, type] = PLACES[i % PLACES.length];
    const j = Math.floor(i / PLACES.length);
    const dx = ((i * 37) % 17) / 6 - 1.4, dy = ((i * 53) % 13) / 5 - 1.3;
    const severity = Math.round((((i * 29) % 100) / 100) * 100) / 100;
    return {
      type: 'Feature' as const,
      id: `gdelt:evt-${i}`,
      geometry: { type: 'Point' as const, coordinates: [lon + dx * j, lat + dy * j] },
      properties: {
        source: type.startsWith('hazard') ? 'gdacs' : type === 'cyber' ? 'cisa_kev' : 'gdelt',
        raw_id: `evt-${i}`, type, title: `Test signal ${i}`, url: null,
        published_at: iso(i * 37 * 60_000), severity, countries: [c], ports: [], event_type: null,
      },
    };
  }),
};

export const disruptions: S['DisruptionFeatureCollection'] = {
  type: 'FeatureCollection',
  features: [
    { type: 'Feature', id: 'chokepoint:bab_el_mandeb', geometry: { type: 'Point', coordinates: [43.33, 12.58] }, properties: { kind: 'chokepoint', name: 'Bab el-Mandeb', status: 'disrupted', severity: 0.78, radius_km: 150, ports: [], countries: ['YEM', 'DJI'], event_ids: ['portwatch:1'], since: iso(9 * D) } },
    { type: 'Feature', id: 'chokepoint:suez', geometry: { type: 'Point', coordinates: [32.35, 30.6] }, properties: { kind: 'chokepoint', name: 'Suez Canal', status: 'watch', severity: 0.4, radius_km: 120, ports: [], countries: ['EGY'], event_ids: [], since: iso(5 * D) } },
    { type: 'Feature', id: 'chokepoint:malacca', geometry: { type: 'Point', coordinates: [101.0, 2.5] }, properties: { kind: 'chokepoint', name: 'Strait of Malacca', status: 'normal', severity: null, radius_km: 200, ports: [], countries: ['MYS', 'IDN', 'SGP'], event_ids: [], since: null } },
    { type: 'Feature', id: 'chokepoint:taiwan_strait', geometry: { type: 'Point', coordinates: [119.5, 24.0] }, properties: { kind: 'chokepoint', name: 'Taiwan Strait', status: 'watch', severity: 0.5, radius_km: 180, ports: [], countries: ['TWN', 'CHN'], event_ids: [], since: iso(3 * D) } },
  ],
};

// --- the active workspace's entities ----------------------------------------------------
type E = S['Entity'];
const SRC = '7d1f3c2a-0000-4000-8000-0000000000b1';
const CONN = '7d1f3c2a-0000-4000-8000-0000000000c1';
let n = 0;
const ent = (kind: S['EntityKind'], name: string, extra: Partial<E> = {}): E => {
  n++;
  return {
    id: `7d1f3c2a-0000-4000-8000-00000000e${String(n).padStart(3, '0')}`,
    kind, name, countries: [], ports: [], currencies: [], hs_codes: [], commodities: [], software: [],
    lat: null, lon: null, city: null, address: null,
    exposure: { annual_spend: null, spend_currency: null, revenue_share: null, criticality: 'medium' },
    attrs: {}, lineage: { source_id: SRC, source_name: 'Suppliers', row_key: `row:${n + 1}`, row_number: n + 1, mapping_version: 1, synced_at: iso(H) },
    open_risk_count: 0, max_score: null, ...extra,
  };
};
export const entities: E[] = [
  ent('supplier', 'Lumen Display Co', { countries: ['TWN'], ports: ['TWKHH'], currencies: ['TWD'], open_risk_count: 1, max_score: 66, exposure: { annual_spend: '1840000.00', spend_currency: 'TWD', revenue_share: 0.14, criticality: 'high' } }),
  ent('supplier', 'Pearl River Plastics', { countries: ['CHN'], ports: ['CNYTN'], currencies: ['CNY'], open_risk_count: 1, max_score: 47 }),
  ent('supplier', 'Anatolia Fasteners', { countries: ['TUR'], currencies: ['TRY'], open_risk_count: 1, max_score: 81, exposure: { annual_spend: '420000.00', spend_currency: 'EUR', revenue_share: 0.06, criticality: 'high' } }),
  ent('supplier', 'Kinabalu Wire Works', { countries: ['MYS'], ports: ['MYPEN'], currencies: ['MYR'] }),
  ent('supplier', 'Mekong Precision', { countries: ['VNM'], ports: ['VNSGN'], currencies: ['VND'] }),
  ent('supplier', 'Coromandel Cables', { countries: ['IND'], ports: ['INMAA'], currencies: ['INR'] }),
  ent('supplier', 'Baltic Steelworks', { countries: ['POL'], currencies: ['PLN'] }),
  ent('supplier', 'Osaka Sensor Lab', { countries: ['JPN'], ports: ['JPOSA'], currencies: ['JPY'] }),
  ent('site', 'Rotterdam warehouse', { countries: ['NLD'], lat: 51.92, lon: 4.48, city: 'Rotterdam' }),
  ent('route', 'Yantian to Rotterdam', { countries: ['CHN', 'NLD'], ports: ['CNYTN', 'NLRTM'], attrs: { chokepoints: ['bab_el_mandeb', 'suez'] }, open_risk_count: 1, max_score: 74 }),
  ent('route', 'Kaohsiung to Rotterdam', { countries: ['TWN', 'NLD'], ports: ['TWKHH', 'NLRTM'], attrs: { chokepoints: ['bab_el_mandeb', 'suez'] } }),
  ent('currency', 'Turkish lira payables', { currencies: ['TRY'], open_risk_count: 1, max_score: 52 }),
  ent('software', 'Edge gateway', { software: ['cpe:2.3:o:vendor:edge_os:7.2'], open_risk_count: 1, max_score: 61 }),
];
const byName = (name: string) => entities.find((e) => e.name === name)!;

export const mapEntities: S['EntityFeatureCollection'] = {
  type: 'FeatureCollection',
  truncated: false,
  features: entities
    .filter((e) => e.kind !== 'currency' && e.kind !== 'software')
    .map((e) => ({
      type: 'Feature' as const,
      id: e.id,
      geometry: e.lat != null && e.lon != null ? { type: 'Point' as const, coordinates: [e.lon, e.lat] } : null,
      properties: {
        kind: e.kind, name: e.name, countries: e.countries, ports: e.ports, criticality: e.exposure.criticality, open_risk_count: e.open_risk_count, max_score: e.max_score,
        // routes carry their declared chokepoints, in order (the API resolves them to ids)
        ...(e.kind === 'route' ? { chokepoints: (e.attrs as { chokepoints?: string[] }).chokepoints ?? [] } : {}),
      },
    })),
};

// --- risks -----------------------------------------------------------------------------
type R = S['Risk'];
const risk = (i: number, e: E, category: S['RiskCategory'], title: string, L: number, I: number, extra: Partial<R> = {}): R => ({
  id: `7d1f3c2a-0000-4000-8000-00000000f00${i}`,
  entity: { id: e.id, kind: e.kind, name: e.name },
  story: { key: `risk:7d1f3c2a-0000-4000-8000-00000000f00${i}`, risk_count: 1, entities: [{ id: e.id, name: e.name }] },
  category, title, likelihood: L, impact: I, score: Math.round(1000 * L * I) / 10, status: 'open',
  countries: e.countries, ports: e.ports, lat: null, lon: null, evidence_count: 3,
  first_seen_at: iso((10 - i) * D), last_seen_at: iso(i * H), status_changed_at: iso((10 - i) * D), ...extra,
});
export const risks: R[] = [
  risk(1, byName('Anatolia Fasteners'), 'sanctions', 'Name matches a sanctions entry', 0.9, 0.9, { evidence_count: 1 }),
  risk(2, byName('Yantian to Rotterdam'), 'logistics', 'Bab el-Mandeb transits down', 0.82, 0.9, {
    lat: 12.58, lon: 43.33,
    // One story that reaches two entities: the route, and a supplier whose lane is inferred from its port.
    story: { key: 'logistics|event:portwatch:pw-1', risk_count: 2, entities: [{ id: byName('Yantian to Rotterdam').id, name: 'Yantian to Rotterdam' }, { id: byName('Lumen Display Co').id, name: 'Lumen Display Co' }] },
  }),
  risk(3, byName('Lumen Display Co'), 'geopolitical', 'Military exercises near Kaohsiung', 0.66, 1.0),
  risk(4, byName('Edge gateway'), 'cyber', 'Exploited flaw in your edge gateway', 0.76, 0.8, { countries: [], ports: [] }),
  risk(5, byName('Turkish lira payables'), 'financial', 'Turkish lira down 6% in 30 days', 0.65, 0.8, { countries: ['TUR'] }),
  risk(6, byName('Pearl River Plastics'), 'natural_hazard', 'Typhoon warning for Guangdong', 0.59, 0.8),
].sort((a, b) => b.score - a.score);

export const summary: S['RiskSummary'] = {
  open_count: risks.length,
  story_count: risks.length,
  by_category: { natural_hazard: 1, logistics: 1, geopolitical: 1, sanctions: 1, trade_policy: 0, cyber: 1, financial: 1, labour: 0, other: 0 },
  stories_by_category: { natural_hazard: 1, logistics: 1, geopolitical: 1, sanctions: 1, trade_policy: 0, cyber: 1, financial: 1, labour: 0, other: 0 },
  by_status: { open: risks.length, acknowledged: 0, mitigating: 0, resolved: 0, dismissed: 0 },
  max_score: risks[0].score,
  top: risks.slice(0, 5),
  last_scored_at: iso(4 * 60_000),
  feeds_as_of: iso(7 * 60_000),
};

export const mapRisks: S['RiskPlaceFeatureCollection'] = (() => {
  // One feature per place, as the API groups them.
  const places = new Map<string, S['RiskPlaceFeatureCollection']['features'][number]>();
  for (const r of risks) {
    const point = r.lat != null && r.lon != null;
    if (!point && !r.ports.length && !r.countries.length) continue;
    const place_type = point ? ('point' as const) : r.ports.length ? ('port' as const) : ('country' as const);
    const place_key = point ? r.entity.id : (r.ports[0] ?? r.countries[0]);
    const id = `${place_type}:${place_key}`;
    const f = places.get(id);
    if (f) {
      f.properties.risk_count++;
      f.properties.story_count++;
      f.properties.max_score = Math.max(f.properties.max_score, r.score);
      f.properties.top_risk_ids.push(r.id);
      f.properties.top_categories.push(r.category);
      continue;
    }
    places.set(id, {
      type: 'Feature',
      id,
      geometry: point ? { type: 'Point', coordinates: [r.lon!, r.lat!] } : null,
      properties: { place_type, place_key, label: point ? 'Bab el-Mandeb' : null, risk_count: 1, story_count: 1, max_score: r.score, top_categories: [r.category], top_risk_ids: [r.id] },
    });
  }
  return { type: 'FeatureCollection', features: [...places.values()] };
})();

export function riskDetail(r: R): S['RiskDetail'] {
  const hist = [0.62, 0.64, 0.66, 0.7, 0.72, 0.75, 0.78, 1].map((f, i, a) => ({
    recorded_at: iso((a.length - i) * D),
    likelihood: r.likelihood * f,
    impact: r.impact,
    score: Math.round(r.score * f * 10) / 10,
    status: 'open' as const,
    reason: i === 0 ? ('created' as const) : ('rescored' as const),
  }));
  return {
    ...r,
    explanation:
      r.category === 'logistics'
        ? 'Fewer ships are passing Bab el-Mandeb this week, and this route runs through it to Suez.'
        : `${r.entity.name} matches signals from the public feeds. The score combines how likely it is and how much it touches you.`,
    factors: {
      likelihood: { method: 'noisy_or', value: r.likelihood, items: [{ evidence_key: 'event:portwatch:1', signal: 0.8, recency: 0.95, match_confidence: 0.9, contribution: 0.68 }] },
      impact: { value: r.impact, criticality: 'high', criticality_weight: 0.85, exposure_share: 0.22, exposure_norm: 0.9, category_multiplier: 1 },
      engine_version: 'test',
      story: r.story.key,
      explain: {
        matched: { match_type: 'chokepoint', match_value: 'bab_el_mandeb', label: 'chokepoint: Bab el-Mandeb', matched_text: null, inferred_from: null, supported_by: null },
        strongest: { evidence_key: 'event:portwatch:1', title: 'Transit calls at Bab el-Mandeb down week on week', source: 'portwatch', published_at: iso(D) },
        item_count: 2,
        steps: [
          { label: 'Strongest signal', formula: 'signal 0.800 × recency 0.950 × match 0.900 = 0.684', value: 0.684 },
          { label: 'Likelihood', formula: 'noisy-OR of 2 items: 1 − (1 − 0.684) × (1 − 0.210) = 0.750', value: 0.75 },
          { label: 'Score', formula: `100 × ${r.likelihood.toFixed(3)} × ${r.impact.toFixed(3)} = ${r.score.toFixed(1)}`, value: r.score },
        ],
      },
    },
    advice: {
      text: 'Ask the carriers on this route for a quote around the Cape of Good Hope.\nOrder four weeks of the parts that run out first.\nCheck which contracts put the cost of late delivery on you.',
      source: 'groq', model: 'llama-test', generated_at: iso(2 * H),
    },
    evidence: [
      { evidence_key: 'event:portwatch:1', kind: 'event', event: { source: 'portwatch', raw_id: '1' }, match_type: 'chokepoint', match_value: 'bab_el_mandeb', match_confidence: 0.9, signal: 0.8, recency: 0.95, contribution: 0.68, snapshot: { title: 'Transit calls at Bab el-Mandeb down week on week', url: null, type: 'port.disruption', published_at: iso(D), severity: 0.8, lat: 12.58, lon: 43.33, countries: ['YEM'], ports: [] }, still_in_feeds: true, last_linked_at: iso(H) },
      { evidence_key: 'event:gdelt:2', kind: 'event', event: { source: 'gdelt', raw_id: '2' }, match_type: 'country', match_value: r.countries[0] ?? 'EGY', match_confidence: 0.6, signal: 0.5, recency: 0.8, contribution: 0.21, snapshot: { title: 'Shipping lines extend diversions for another month', url: 'https://example.test/news/2', type: 'news', published_at: iso(2 * D), severity: null, lat: null, lon: null, countries: ['EGY'], ports: [] }, still_in_feeds: true, last_linked_at: iso(2 * H) },
    ],
    history: hist,
    status_note: null,
    engine_version: 'test',
  };
}

// --- connections, sources, mapping -------------------------------------------------------
export const connection: S['Connection'] = {
  id: CONN, provider: 'google_sheets', display_name: 'Google Sheets', status: 'active', status_reason: null,
  account_label: 'ops@kestrel.test', source_count: 2, watch: { expires_at: iso(-6 * D), renew_after: iso(-5 * D) }, created_at: iso(20 * D), updated_at: iso(H),
};
const col = (index: number, name: string, samples: string[]): S['SourceColumn'] => ({ index, name, samples });
export const sourceActive: S['Source'] = {
  id: SRC, connection_id: CONN, kind: 'google_sheet', external_id: 'sheet-1', external_table_id: '', name: 'Suppliers', url: null,
  status: 'active', status_reason: null, header_row: 1, columns: [], row_count: 14, entity_count: 13, last_synced_at: iso(H), last_change_at: iso(2 * H), created_at: iso(20 * D),
};
export const PENDING = '7d1f3c2a-0000-4000-8000-0000000000b2';
export const sourcePending: S['Source'] = {
  ...sourceActive, id: PENDING, external_table_id: '1520', name: 'Shipments', status: 'mapping_suggested', row_count: 42, entity_count: 0, last_synced_at: null,
  columns: [
    col(0, 'Vendor', ['Lumen Display Co', 'Pearl River Plastics', 'Mekong Precision']),
    col(1, 'Origin', ['Taiwan', 'China', 'Vietnam']),
    col(2, 'Port of loading', ['TW KHH', 'CN YTN', 'VN SGN']),
    col(3, 'Item', ['LCD panels', 'ABS housings', 'machined shafts']),
    col(4, 'Annual spend (USD)', ['420000', '85000', '121000']),
    col(5, 'Pay ccy', ['TWD', 'CNY', 'VND']),
    col(6, 'Buyer', ['M. Okafor', 'D. Lindqvist', 'S. Haddad']),
  ],
};
export const fields: S['MappingFieldInfo'][] = [
  { field: 'name', label: 'Name', kinds: [], multi: false },
  { field: 'country', label: 'Country', kinds: [], multi: false },
  { field: 'city', label: 'City', kinds: ['supplier', 'site', 'customer'], multi: false },
  { field: 'port', label: 'Port', kinds: ['supplier', 'site', 'port'], multi: false },
  { field: 'commodity', label: 'Commodity', kinds: ['supplier', 'commodity'], multi: true },
  { field: 'annual_spend', label: 'Annual spend', kinds: ['supplier'], multi: false },
  { field: 'currency', label: 'Currency', kinds: [], multi: false },
  { field: 'notes', label: 'Notes', kinds: [], multi: false },
  { field: 'row_key', label: 'Row key', kinds: [], multi: false },
  { field: 'ignore', label: 'Ignore it', kinds: [], multi: false },
];
export const suggestion: S['ColumnMapping'] = {
  id: '7d1f3c2a-0000-4000-8000-0000000000d1', source_id: PENDING, version: 1, status: 'suggested', model: 'groq:llama-test', created_at: iso(60_000), confirmed_at: null,
  spec: {
    entity_kind: 'supplier',
    columns: [
      { column: 'Vendor', field: 'name', confidence: 0.99 },
      { column: 'Origin', field: 'country', confidence: 0.98 },
      { column: 'Port of loading', field: 'port', confidence: 0.96 },
      { column: 'Item', field: 'commodity', confidence: 0.93 },
      { column: 'Annual spend (USD)', field: 'annual_spend', currency: 'USD', confidence: 0.71 },
      { column: 'Pay ccy', field: 'currency', confidence: 0.99 },
      { column: 'Buyer', field: 'ignore', confidence: 0.34 },
    ],
  },
};
export const preview: S['MappingPreview'] = {
  rows_read: 42,
  entities: {
    type: 'FeatureCollection',
    truncated: false,
    features: mapEntities.features.filter((f) => f.properties.kind === 'supplier').map((f, i) => ({ ...f, id: `row:${i + 2}`, properties: { ...f.properties, row_number: i + 2 } })),
  },
  problems: [
    { row_number: 17, column: 'Origin', value: 'Formosa', message: 'not a known country' },
    { row_number: 31, column: 'Port of loading', value: 'Haiphong?', message: 'not a UN/LOCODE' },
  ],
};

export const alerts: S['Alert'][] = [
  { id: '7d1f3c2a-0000-4000-8000-0000000000a1', kind: 'sanctions_match', severity: 'critical', title: 'Anatolia Fasteners matches a sanctions entry', body: '', risk_id: risks[0].id, connection_id: null, created_at: iso(3 * H), read_at: null, risk_status: 'open' },
  { id: '7d1f3c2a-0000-4000-8000-0000000000a2', kind: 'risk_escalated', severity: 'warning', title: 'Bab el-Mandeb transits down rose to 74', body: '', risk_id: risks[1].id, connection_id: null, created_at: iso(6 * H), read_at: null, risk_status: 'open' },
];

export function scenarioFor(name: string, inputs: S['ScenarioInputs']): S['Scenario'] {
  return {
    id: '7d1f3c2a-0000-4000-8000-0000000005c1', name, description: null, status: 'completed', created_at: iso(0), completed_at: iso(0), inputs, error: null,
    results: {
      baseline: { risk_count: 6, story_count: 4, max_score: 81, portfolio_score: 62.4, by_category: {}, most_exposed: { entity_id: byName('Yantian to Rotterdam').id, entity_name: 'Yantian to Rotterdam', exposure: 84.2, risk_count: 2 } },
      scenario: { risk_count: 8, story_count: 5, max_score: 86, portfolio_score: 73.1, by_category: {}, most_exposed: { entity_id: byName('Yantian to Rotterdam').id, entity_name: 'Yantian to Rotterdam', exposure: 90.3, risk_count: 2 } },
      changes: [
        { fingerprint: 'f1', risk_id: risks[1].id, entity_id: byName('Yantian to Rotterdam').id, entity_name: 'Yantian to Rotterdam', category: 'logistics', title: 'Route disrupted', baseline_score: 74, scenario_score: 86, delta: 12, lat: null, lon: null, countries: ['CHN', 'NLD'] },
        { fingerprint: 'f2', risk_id: null, entity_id: byName('Kaohsiung to Rotterdam').id, entity_name: 'Kaohsiung to Rotterdam', category: 'logistics', title: 'Route disrupted', baseline_score: null, scenario_score: 71, delta: 71, lat: null, lon: null, countries: ['TWN', 'NLD'] },
        { fingerprint: 'f3', risk_id: null, entity_id: byName('Rotterdam warehouse').id, entity_name: 'Rotterdam warehouse', category: 'logistics', title: 'Inbound delays', baseline_score: null, scenario_score: 44, delta: 44, lat: 51.92, lon: 4.48, countries: ['NLD'] },
      ],
      engine_version: 'test',
      as_of: iso(0),
    },
  };
}
