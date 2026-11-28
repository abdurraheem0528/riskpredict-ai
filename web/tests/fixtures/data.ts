// Test-only fixtures (MSW). Invented rows for an invented company; they exist only under
// tests/ and never reach the built bundle (scripts/check-bundle.mjs checks the design's
// sample names are absent from dist/).
import type { components } from '../../src/api/schema';

type S = components['schemas'];
export const API = 'http://api.test';
export const WS = '7d1f3c2a-0000-4000-8000-000000000001';
// Fixture times are relative to the test run (to the minute), so "Checked 4 min ago" and
// "Up 3 since yesterday" read the same on any day.
const NOW = Math.floor(Date.now() / 60_000) * 60_000;
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
// Modelled on the owner's live test of 27 Nov 2026 (the fictional "Atlas" sheet: two sites,
// the suppliers of the 11-supplier test sheet, eight routes to Europe). Test fixtures only.
type E = S['Entity'];
const SRC = '7d1f3c2a-0000-4000-8000-0000000000b1';
const CONN = '7d1f3c2a-0000-4000-8000-0000000000c1';
let n = 0;
const ent = (kind: S['EntityKind'], name: string, extra: Partial<E> = {}, criticality: S['Criticality'] | null = 'medium'): E => {
  n++;
  return {
    id: `7d1f3c2a-0000-4000-8000-00000000e${String(n).padStart(3, '0')}`,
    kind, name, countries: [], ports: [], currencies: [], hs_codes: [], commodities: [], software: [],
    lat: null, lon: null, city: null, address: null,
    exposure: { annual_spend: null, spend_currency: null, revenue_share: null, criticality },
    attrs: {}, lineage: { source_id: SRC, source_name: 'Atlas sites and suppliers', row_key: `row:${n + 1}`, row_number: n + 1, mapping_version: 1, synced_at: iso(H) },
    open_risk_count: 0, max_score: null, ...extra,
  };
};
const supplier = (name: string, country: string, port: string, city: string, lat: number, lon: number, crit: S['Criticality'], ccy: string) =>
  ent('supplier', name, { countries: [country], ports: [port], city, lat, lon, currencies: [ccy] }, crit);
const VIA = ['malacca', 'bab_el_mandeb', 'suez'];
const route = (name: string, from: string, to: string, countries: string[], via = VIA) =>
  ent('route', name, { countries, ports: [from, to], attrs: { chokepoints: via } }, 'high');
export const entities: E[] = [
  ent('site', 'Atlas Eindhoven Assembly Plant', { countries: ['NLD'], city: 'Eindhoven', lat: 51.44, lon: 5.47 }, 'critical'),
  ent('site', 'Atlas Rotterdam Distribution Centre', { countries: ['NLD'], ports: ['NLRTM'], city: 'Rotterdam', lat: 51.92, lon: 4.48 }, null),
  supplier('Formosa Microcircuits', 'TWN', 'TWKHH', 'Hsinchu', 24.8, 120.97, 'high', 'TWD'),
  supplier('Pearl River PCB Works', 'CHN', 'CNYTN', 'Shenzhen', 22.54, 114.06, 'critical', 'CNY'),
  supplier('Penang Semicon Assembly', 'MYS', 'MYPEN', 'Penang', 5.3, 100.27, 'high', 'MYR'),
  supplier('Hanbit Display', 'KOR', 'KRPUS', 'Gumi', 36.12, 128.34, 'high', 'KRW'),
  supplier('Kansai Precision Capacitors', 'JPN', 'JPOSA', 'Osaka', 34.69, 135.5, 'medium', 'JPY'),
  supplier('Haiphong Cable & Harness', 'VNM', 'VNHPH', 'Haiphong', 20.86, 106.68, 'medium', 'VND'),
  supplier('Chennai Connector Works', 'IND', 'INMAA', 'Chennai', 13.08, 80.27, 'medium', 'INR'),
  supplier('Rhein Sensorik GmbH', 'DEU', 'DEHAM', 'Dortmund', 51.51, 7.47, 'high', 'EUR'),
  route('Busan to Rotterdam', 'KRPUS', 'NLRTM', ['KOR', 'NLD']),
  route('Kaohsiung to Rotterdam', 'TWKHH', 'NLRTM', ['TWN', 'NLD']),
  route('Penang to Rotterdam', 'MYPEN', 'NLRTM', ['MYS', 'NLD'], ['bab_el_mandeb', 'suez']),
  route('Yantian to Rotterdam', 'CNYTN', 'NLRTM', ['CHN', 'NLD']),
  route('Haiphong to Rotterdam', 'VNHPH', 'NLRTM', ['VNM', 'NLD']),
  route('Chennai to Rotterdam', 'INMAA', 'NLRTM', ['IND', 'NLD'], ['bab_el_mandeb', 'suez']),
  route('Osaka to Rotterdam', 'JPOSA', 'NLRTM', ['JPN', 'NLD']),
  route('Singapore to Antwerp', 'SGSIN', 'BEANR', ['SGP', 'BEL'], ['bab_el_mandeb', 'suez']),
];
const byName = (name: string) => entities.find((e) => e.name === name)!;
const ROUTES = entities.filter((e) => e.kind === 'route').map((e) => e.name);
const LANE_SUPPLIERS = ['Formosa Microcircuits', 'Pearl River PCB Works', 'Penang Semicon Assembly', 'Hanbit Display', 'Kansai Precision Capacitors', 'Haiphong Cable & Harness', 'Chennai Connector Works'];

// --- stories: one row per category and place, as GET /v1/risks?group=story lists them ----------
type R = S['Risk'];
type Story = {
  category: S['RiskCategory'];
  where: [S['Evidence']['match_type'], string, string];
  /** title, feed, hours ago, url, type; null for a market move */
  lead: [string, string, number, string | null, string] | null;
  /** the entities it reaches with each one's score, highest first; the first is the row's */
  reach: [string, number][];
  L: number;
  I: number;
  title: string;
};
const news = (title: string, source: string, h: number, slug: string): Story['lead'] => [title, source, h, `https://news.example.test/${slug}`, 'news'];
export const STORIES: Story[] = [
  { category: 'logistics', where: ['chokepoint', 'bab_el_mandeb', 'Bab el-Mandeb'], lead: news('Houthis Strike Saudi Ports on Red Sea', 'newsdata', 6, 'red-sea'),
    reach: [...ROUTES.map((r): [string, number] => [r, 34]), ...LANE_SUPPLIERS.map((r): [string, number] => [r, 22])], L: 0.68, I: 0.5, title: 'Shipping disruption news at Bab el-Mandeb on route Busan to Rotterdam' },
  { category: 'trade_policy', where: ['country', 'NLD', 'the Netherlands'], lead: news('Dutch Government Lobbies Against US Chip Export Bill', 'newsdata', 9, 'dutch-lobby'),
    reach: [['Atlas Eindhoven Assembly Plant', 30], ['Atlas Rotterdam Distribution Centre', 15]], L: 0.5, I: 0.6, title: 'Trade policy news in Netherlands affecting Atlas Eindhoven Assembly Plant' },
  { category: 'sanctions', where: ['country', 'MYS', 'Malaysia'], lead: news('No deals with US-blacklisted firms, Malaysian companies told after Cabinet briefing on chip curbs', 'newsdata', 14, 'my-curbs'),
    reach: [['Penang Semicon Assembly', 24]], L: 0.4, I: 0.6, title: 'Sanctions news in Malaysia affecting Penang Semicon Assembly' },
  { category: 'cyber', where: ['country', 'TWN', 'Taiwan'], lead: news("Taiwan weathers wave of cyberattacks ahead of Saturday's elections", 'gdelt', 20, 'tw-cyber'),
    reach: [['Formosa Microcircuits', 20]], L: 0.4, I: 0.5, title: 'Cyber incident news in Taiwan affecting Formosa Microcircuits' },
  { category: 'sanctions', where: ['country', 'CHN', 'China'], lead: news("Nvidia's Most Advanced AI Chips Reach Chinese Military Alumnus And Blacklisted Firm Via US Companies", 'newsdata', 22, 'cn-chips'),
    reach: [['Pearl River PCB Works', 18]], L: 0.3, I: 0.6, title: 'Sanctions news in China affecting Pearl River PCB Works' },
  { category: 'logistics', where: ['country', 'MYS', 'Malaysia'], lead: news('Nvidia Halts Server Shipments To Malaysia, Three Other Southeast Asian Markets', 'currents', 30, 'my-servers'),
    reach: [['Penang Semicon Assembly', 17]], L: 0.34, I: 0.5, title: 'Shipping disruption news in Malaysia affecting Penang Semicon Assembly' },
  { category: 'geopolitical', where: ['country', 'TWN', 'Taiwan'], lead: news('China flies record number of jets near Taiwan', 'gdelt', 26, 'tw-jets'),
    reach: [['Formosa Microcircuits', 15]], L: 0.3, I: 0.5, title: 'Conflict news in Taiwan affecting Formosa Microcircuits' },
  { category: 'trade_policy', where: ['country', 'CHN', 'China'], lead: news('Washington adds Chinese circuit board makers to export blacklist', 'gdelt', 33, 'cn-pcb'),
    reach: [['Pearl River PCB Works', 15]], L: 0.25, I: 0.6, title: 'Trade policy news in China affecting Pearl River PCB Works' },
  { category: 'logistics', where: ['port', 'VNHPH', 'Haiphong'], lead: news('Vietnam port congestion worsens at Haiphong', 'gdelt', 28, 'vn-port'),
    reach: [['Haiphong Cable & Harness', 14]], L: 0.35, I: 0.4, title: 'Shipping disruption news at the port of Haiphong affecting Haiphong Cable & Harness' },
  { category: 'natural_hazard', where: ['geo_radius', '84 km', 'Hsinchu'], lead: ['M 6.3 near Hualien City, Taiwan', 'usgs', 18, null, 'hazard.earthquake'],
    reach: [['Formosa Microcircuits', 13]], L: 0.26, I: 0.5, title: 'Earthquake near Formosa Microcircuits' },
  { category: 'labour', where: ['country', 'KOR', 'South Korea'], lead: news('Korean truckers vote to strike over freight rates', 'gdelt', 40, 'kr-strike'),
    reach: [['Hanbit Display', 13]], L: 0.26, I: 0.5, title: 'Labour action news in South Korea affecting Hanbit Display' },
  { category: 'trade_policy', where: ['country', 'KOR', 'South Korea'], lead: news('South Korea tightens export checks on display equipment', 'gdelt', 44, 'kr-export'),
    reach: [['Hanbit Display', 12]], L: 0.24, I: 0.5, title: 'Trade policy news in South Korea affecting Hanbit Display' },
  { category: 'geopolitical', where: ['country', 'CHN', 'China'], lead: news('China holds live-fire drills near disputed islands', 'gdelt', 36, 'cn-drills'),
    reach: [['Pearl River PCB Works', 12]], L: 0.2, I: 0.6, title: 'Conflict news in China affecting Pearl River PCB Works' },
  { category: 'natural_hazard', where: ['country', 'VNM', 'Vietnam'], lead: ['Flood alert for northern Vietnam', 'gdacs', 12, null, 'hazard.flood'],
    reach: [['Haiphong Cable & Harness', 11]], L: 0.275, I: 0.4, title: 'Flood in Vietnam affecting Haiphong Cable & Harness' },
  { category: 'cyber', where: ['country', 'JPN', 'Japan'], lead: news('Ransomware attack halts output at Japanese parts maker', 'gdelt', 50, 'jp-ransom'),
    reach: [['Kansai Precision Capacitors', 10]], L: 0.25, I: 0.4, title: 'Cyber incident news in Japan affecting Kansai Precision Capacitors' },
  { category: 'labour', where: ['country', 'IND', 'India'], lead: news('Chennai port workers threaten strike over wages', 'gdelt', 60, 'in-strike'),
    reach: [['Chennai Connector Works', 8]], L: 0.2, I: 0.4, title: 'Labour action news in India affecting Chennai Connector Works' },
  { category: 'financial', where: ['currency', 'EUR/TWD', 'EUR/TWD'], lead: null,
    reach: [['Formosa Microcircuits', 7]], L: 0.14, I: 0.5, title: 'TWD moved sharply against EUR (Formosa Microcircuits)' },
  { category: 'logistics', where: ['chokepoint', 'taiwan_strait', 'Taiwan Strait'], lead: news('Ships reroute around Taiwan Strait during drills', 'gdelt', 30, 'tw-strait'),
    reach: [['Formosa Microcircuits', 7]], L: 0.14, I: 0.5, title: 'Shipping disruption news at Taiwan Strait affecting Formosa Microcircuits' },
];

// Each entity's open risks and highest score, as the map's dots show them.
for (const st of STORIES)
  for (const [name, score] of st.reach) {
    const e = byName(name);
    e.open_risk_count += 1;
    e.max_score = Math.max(e.max_score ?? 0, score);
  }

export const mapEntities: S['EntityFeatureCollection'] = {
  type: 'FeatureCollection',
  truncated: false,
  features: entities.map((e) => ({
    type: 'Feature' as const,
    id: e.id,
    geometry: e.lat != null && e.lon != null ? { type: 'Point' as const, coordinates: [e.lon, e.lat] } : null,
    properties: {
      kind: e.kind, name: e.name, countries: e.countries, ports: e.ports, city: e.city, criticality: e.exposure.criticality, open_risk_count: e.open_risk_count, max_score: e.max_score,
      // routes carry their declared chokepoints, in order (the API resolves them to ids)
      ...(e.kind === 'route' ? { chokepoints: (e.attrs as { chokepoints?: string[] }).chokepoints ?? [] } : {}),
    },
  })),
};

const riskId = (i: number) => `7d1f3c2a-0000-4000-8000-0000000f${String(i).padStart(4, '0')}`;
const SCORED = iso(4 * 60_000); // the latest scoring run
export const risks: R[] = STORIES.map((st, i): R => {
  const e = byName(st.reach[0][0]);
  const members = st.reach.map(([name]) => byName(name));
  const kinds: Record<string, number> = {};
  for (const m of members) kinds[m.kind] = (kinds[m.kind] ?? 0) + 1;
  return {
    id: riskId(i + 1),
    entity: { id: e.id, kind: e.kind, name: e.name, city: e.city },
    category: st.category, title: st.title, likelihood: st.L, impact: st.I, score: Math.round(1000 * st.L * st.I) / 10, status: 'open',
    countries: st.where[0] === 'country' ? [st.where[1]] : e.countries, ports: st.where[0] === 'port' ? [st.where[1]] : e.ports,
    lat: e.lat, lon: e.lon, place: e.city ? `${e.city}` : null, evidence_count: st === STORIES[1] ? 4 : 2,
    first_seen_at: iso((3 + (i % 4)) * D), last_seen_at: SCORED, status_changed_at: iso((3 + (i % 4)) * D),
    story: {
      key: `${st.category}|${st.where[0]}:${st.where[1]}`, risk_count: st.reach.length, entity_count: members.length, kind_counts: kinds,
      entities: members.slice(0, 10).map((m) => ({ id: m.id, name: m.name, kind: m.kind, city: m.city, criticality: m.exposure.criticality })),
    },
    where: { match_type: st.where[0], match_value: st.where[1], label: st.where[2] },
    lead: st.lead ? { title: st.lead[0], source: st.lead[1], published_at: iso(st.lead[2] * H), url: st.lead[3], type: st.lead[4] } : null,
  };
});

const count = (f: (st: Story) => number) => {
  const out = { natural_hazard: 0, logistics: 0, geopolitical: 0, sanctions: 0, trade_policy: 0, cyber: 0, financial: 0, labour: 0, other: 0 };
  for (const st of STORIES) out[st.category] += f(st);
  return out;
};
const riskCount = STORIES.reduce((a, st) => a + st.reach.length, 0);
export const summary: S['RiskSummary'] = {
  open_count: riskCount,
  story_count: risks.length,
  by_category: count((st) => st.reach.length),
  stories_by_category: count(() => 1),
  story_scores: risks.map((r) => r.score),
  by_status: { open: riskCount, acknowledged: 0, mitigating: 0, resolved: 0, dismissed: 0 },
  max_score: risks[0].score,
  top: risks.slice(0, 5),
  last_scored_at: SCORED,
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
      properties: { place_type, place_key, label: point ? r.entity.name : null, risk_count: 1, story_count: 1, max_score: r.score, top_categories: [r.category], top_risk_ids: [r.id] },
    });
  }
  return { type: 'FeatureCollection', features: [...places.values()] };
})();

const CRIT_W = { low: 0.25, medium: 0.5, high: 0.75, critical: 1 } as const;
const MULT: Record<S['RiskCategory'], number> = { logistics: 1, natural_hazard: 1, geopolitical: 0.9, sanctions: 0.6, trade_policy: 0.6, cyber: 0.8, financial: 0.7, labour: 0.8, other: 0.5 };
const ADVICE: Record<string, string> = {
  // the Groq advice of the live Atlas Eindhoven risk, as stored (three steps)
  trade_policy: 'Ask the Eindhoven plant’s equipment suppliers which tools or spares would need a new export licence under the bill.\nCheck how many weeks of chip-tool spares the plant holds, and order ahead for any with a single source.\nName one person to follow the bill and the Dutch government’s response, and to brief the plant manager each week.',
  // Bab el-Mandeb: three steps of the length the advice prompt asks for (at most 16 words), with
  // the usual detour the prompt gives (risk-engine.md §8)
  logistics: 'Ask your carriers for transit times and surcharges on the detour around the Cape of Good Hope.\nOrder ahead the parts that would run short first if deliveries arrive weeks late.\nCheck which supplier and customer contracts put the cost of late delivery on you.',
};

/** Whether the feed cut a fixture headline off, as the API's `serialize.is_cut` says for these. */
const cutOff = (title: string) => /(?:…|\.\.\.)$/.test(title) || /: 'We Will$/.test(title);

/** The risk page's data for a row (GET /v1/risks/{id}), modelled on the live risks. Test fixture. */
export function riskDetail(r: R): S['RiskDetail'] {
  const i = risks.findIndex((x) => x.id === r.id);
  const st = STORIES[Math.max(0, i)];
  const e = byName(r.entity.name);
  const crit = e.exposure.criticality;
  const atlas = i === 1; // the live Atlas Eindhoven risk: history 27 → 30, four reports
  const hist = atlas
    ? [{ at: 3 * D, score: 27, reason: 'created' as const }, { at: 20 * H, score: 30, reason: 'rescored' as const }]
    : i === 0
      ? [{ at: 5 * D, score: 36, reason: 'created' as const }, { at: 2 * H, score: 34, reason: 'rescored' as const }]
      : [{ at: 10 * H, score: Math.max(1, r.score - 2), reason: 'created' as const }];
  const [mt, mv] = st.where;
  type Report = { title: string; source: string; h: number; url: string | null; type: string; p: number };
  const reports: Report[] = st.lead
    ? [{ title: st.lead[0], source: st.lead[1], h: st.lead[2], url: st.lead[3], type: st.lead[4], p: 0.45 }]
    : [];
  if (atlas) {
    // As on the live page: the strongest report's title was cut off by the feed (Currents), so the
    // lead is the next one, and the page shows the two strongest whole headlines first.
    reports.unshift({ title: "ASML Caught in US-China Chip Battle as Netherlands Vows to Resist Trump's Proposed Export Curbs: 'We Will", source: 'currents', h: 7, url: 'https://news.example.test/asml-curbs', type: 'news', p: 0.5 });
    reports.push(
      { title: 'Dutch cabinet weighs wider licence rules for chipmaking tool exports', source: 'gdelt', h: 15, url: 'https://news.example.test/nl-licence', type: 'news', p: 0.2 },
      { title: 'Netherlands and US at odds over curbs on chip equipment sales', source: 'gdelt', h: 26, url: 'https://news.example.test/nl-us', type: 'news', p: 0.12 },
    );
  } else if (i === 0)
    // As on the live page: a second headline long enough to wrap to three lines (clamped to two).
    // Its opening is the live headline's; the rest is test text of the same length.
    reports.push({ title: "Ethiopia: 'Stop Arming Ethiopia's Enemies' - Abiy Tells Eritrea, Warning of a Wider Crisis Along the Red Sea Coast and Its Shipping Lanes", source: 'newsdata', h: 14, url: 'https://news.example.test/et-er', type: 'news', p: 0.1 });
  else if (st.lead) reports.push({ title: `${st.lead[0].split(' ').slice(0, 4).join(' ')} prompts second warning`, source: 'gdelt', h: st.lead[2] + 8, url: 'https://news.example.test/second', type: 'news', p: 0.1 });
  const evidence: S['Evidence'][] = reports.map((x, k) => ({
    evidence_key: `event:${x.source}:${i}-${k}`, kind: 'event', event: { source: x.source, raw_id: `${i}-${k}` }, headline: x.title, cut: cutOff(x.title), match_type: mt, match_value: mv,
    match_confidence: 0.9, signal: 0.6, recency: 0.9, contribution: x.p,
    snapshot: { title: x.title, url: x.url, type: x.type, published_at: iso(x.h * H), severity: null, lat: null, lon: null, countries: mt === 'country' ? [mv] : [], ports: [] },
    still_in_feeds: true, last_linked_at: SCORED,
  }));
  if (!st.lead)
    evidence.push({
      evidence_key: 'anomaly:fx.rate|EUR/TWD', kind: 'anomaly', anomaly: { series: 'fx.rate', key: 'EUR/TWD', ts: iso(D), method: 'robust_z' }, match_type: 'currency', match_value: 'EUR/TWD',
      match_confidence: 0.9, signal: 0.4, recency: 0.9, contribution: 0.14, snapshot: { title: 'EUR/TWD moved 3.8 robust z from its 90-day norm', url: null, type: 'fx.rate', published_at: iso(D), severity: null, lat: null, lon: null, countries: [], ports: [] },
      still_in_feeds: true, last_linked_at: SCORED,
    });
  return {
    ...r,
    explanation: `Recent news reports about ${CATEGORIES_TEXT[st.category]} in ${st.where[2].replace(/^the /, '')} reach ${r.entity.name}.`,
    factors: {
      likelihood: { method: 'noisy_or', value: r.likelihood, decay: 0.5, items: evidence.map((x, k) => ({ evidence_key: x.evidence_key, signal: x.signal, recency: x.recency, match_confidence: x.match_confidence, contribution: x.contribution, weight: 0.5 ** k })) },
      impact: { value: r.impact, criticality: crit, criticality_weight: crit ? CRIT_W[crit] : 0.5, exposure_share: null, exposure_norm: null, category_multiplier: MULT[st.category] },
      engine_version: 'test',
      story: `${st.category}|${evidence[0]?.evidence_key ?? 'none'}`,
      explain: {
        matched: { match_type: mt, match_value: mv, label: `${mt}: ${st.where[2].replace(/^the /, '')}${mt === 'country' ? ` (${mv})` : ''}`, matched_text: null, inferred_from: null, supported_by: null },
        strongest: { evidence_key: evidence[0].evidence_key, title: evidence[0].snapshot.title, source: evidence[0].event?.source ?? 'fx.rate', published_at: evidence[0].snapshot.published_at ?? null },
        item_count: evidence.length,
        steps: [
          { label: 'Strongest signal', formula: `signal 0.600 × recency 0.900 × match 0.900 = ${evidence[0].contribution.toFixed(3)}`, value: evidence[0].contribution },
          { label: 'Likelihood', formula: `noisy-OR of ${evidence.length} items, each further item counting 0.5× the one before = ${r.likelihood.toFixed(3)}`, value: r.likelihood },
          { label: 'Impact', formula: `${MULT[st.category]} (${st.category.replace('_', ' ')}) × criticality ${crit ? CRIT_W[crit] : 0.5} (${crit ?? 'not set'}; exposure unknown) = ${r.impact.toFixed(3)}`, value: r.impact },
          { label: 'Score', formula: `100 × ${r.likelihood.toFixed(3)} × ${r.impact.toFixed(3)} = ${r.score.toFixed(1)}`, value: r.score },
        ],
      },
    },
    advice: ADVICE[st.category]
      ? { text: ADVICE[st.category], source: 'groq', model: 'llama-test', generated_at: iso(2 * H) }
      : { text: 'Ask the supplier how it is affected and when it expects to know more.\nCheck the stock you hold of its parts.', source: 'template', model: null, generated_at: SCORED },
    evidence,
    history: hist.map((h) => ({ recorded_at: iso(h.at), likelihood: (r.likelihood * h.score) / r.score, impact: r.impact, score: h.score, status: 'open' as const, reason: h.reason })),
    status_note: null,
    engine_version: 'test',
  };
}
// --- the worst case the risk page must fit at 1360 × 900 (e2e "risk at the limits") ---------------
// Every part at the longest real data makes it: a place title on two lines, a why-sentence on
// three entities with long names, each "a critical site" (it names the first and counts the
// other two), a trend line, two headlines of three lines (clamped to two) and three advice steps
// of 20 words (the most worker valid_advice lets through) with long words. It fits by holding
// steps back behind "2 more steps". Served only by GET /v1/risks/{id}: it is in no list, so the
// other screens are unchanged. Test fixture.
const WORST_SITES = [
  { id: '7d1f3c2a-0000-4000-8000-00000000e901', name: 'Kolwezi Northern Copper and Cobalt Refining Complex' },
  { id: '7d1f3c2a-0000-4000-8000-00000000e902', name: 'Lubumbashi Electrolytic Cathode Processing Works' },
  { id: '7d1f3c2a-0000-4000-8000-00000000e903', name: 'Likasi Hydrometallurgical Concentrate Storage Facility' },
];
export const WORST_ADVICE = [
  'Contact Kolwezi Northern Copper and Cobalt Refining Complex management regarding transportation interruptions, warehousing constraints and contractual compensation for delayed consignments.',
  'Reconfirm outstanding consignments, documentation requirements, insurance certificates and customs authorisations with international forwarders handling southbound overland transportation from Katanga Province.',
  'Establish contingency procurement arrangements with alternative concentrate suppliers, prioritising manufacturing components whose replenishment schedules depend entirely on uninterrupted overland deliveries.',
];
export const WORST_HEADLINES = [
  'Congolese Transport Unions Announce Indefinite Nationwide Strike Over Unpaid Wages, Paralysing Mining Corridors Between Kolwezi, Likasi and Lubumbashi',
  'Mining Companies Warn Prolonged Labour Disruption Across Haut-Katanga and Lualaba Provinces Threatens Copper and Cobalt Concentrate Shipments to Southern Ports',
];
export const worstCase: S['RiskDetail'] = (() => {
  const id = riskId(99);
  const L = 0.62, I = 0.9;
  const score = Math.round(1000 * L * I) / 10;
  const sites = WORST_SITES.map((x) => ({ ...x, kind: 'site' as const, city: null, criticality: 'critical' as const }));
  const evidence: S['Evidence'][] = [...WORST_HEADLINES, 'Strike talks in Lubumbashi end without agreement'].map((title, k) => ({
    evidence_key: `event:gdelt:worst-${k}`, kind: 'event', event: { source: 'gdelt', raw_id: `worst-${k}` }, headline: title, cut: false,
    match_type: 'country', match_value: 'COD', match_confidence: 0.9, signal: 0.6, recency: 0.9, contribution: [0.45, 0.3, 0.1][k],
    snapshot: { title, url: `https://news.example.test/worst-${k}`, type: 'news', published_at: iso((6 + 20 * k) * H), severity: null, lat: null, lon: null, countries: ['COD'], ports: [] },
    still_in_feeds: true, last_linked_at: SCORED,
  }));
  return {
    id,
    entity: { id: sites[0].id, kind: 'site', name: sites[0].name, city: null },
    category: 'labour', title: 'Labour action news in Democratic Republic of the Congo affecting Kolwezi Northern Copper and Cobalt Refining Complex',
    likelihood: L, impact: I, score, status: 'open', countries: ['COD'], ports: [], lat: null, lon: null, place: null, evidence_count: evidence.length,
    first_seen_at: iso(3 * D), last_seen_at: SCORED, status_changed_at: iso(3 * D),
    story: { key: 'labour|country:COD', risk_count: 3, entity_count: 3, kind_counts: { site: 3 }, entities: sites },
    where: { match_type: 'country', match_value: 'COD', label: 'the Democratic Republic of the Congo' },
    lead: { title: WORST_HEADLINES[0], source: 'gdelt', published_at: evidence[0].snapshot.published_at ?? null, url: evidence[0].snapshot.url ?? null, type: 'news' },
    explanation: `Recent news reports about labour action in Democratic Republic of the Congo reach ${sites[0].name}.`,
    factors: {
      likelihood: { method: 'noisy_or', value: L, decay: 0.5, items: evidence.map((x, k) => ({ evidence_key: x.evidence_key, signal: x.signal, recency: x.recency, match_confidence: x.match_confidence, contribution: x.contribution, weight: 0.5 ** k })) },
      impact: { value: I, criticality: 'critical', criticality_weight: 1, exposure_share: null, exposure_norm: null, category_multiplier: 0.9 },
      engine_version: 'test',
      story: `labour|${evidence[0].evidence_key}`,
      explain: {
        matched: { match_type: 'country', match_value: 'COD', label: 'country: Congo, The Democratic Republic of the (COD)', matched_text: null, inferred_from: null, supported_by: null },
        strongest: { evidence_key: evidence[0].evidence_key, title: WORST_HEADLINES[0], source: 'gdelt', published_at: evidence[0].snapshot.published_at ?? null },
        item_count: evidence.length,
        steps: [
          { label: 'Strongest signal', formula: 'signal 0.600 × recency 0.900 × match 0.900 = 0.450', value: 0.45 },
          { label: 'Likelihood', formula: `noisy-OR of ${evidence.length} items, each further item counting 0.5× the one before = ${L.toFixed(3)}`, value: L },
          { label: 'Impact', formula: `0.9 (labour) × criticality 1 (critical; exposure unknown) = ${I.toFixed(3)}`, value: I },
          { label: 'Score', formula: `100 × ${L.toFixed(3)} × ${I.toFixed(3)} = ${score.toFixed(1)}`, value: score },
        ],
      },
    },
    advice: { text: WORST_ADVICE.join('\n'), source: 'groq', model: 'llama-test', generated_at: iso(2 * H) },
    evidence,
    history: [
      { recorded_at: iso(3 * D), likelihood: 0.5, impact: I, score: 45, status: 'open', reason: 'created' },
      { recorded_at: iso(2 * H), likelihood: L, impact: I, score, status: 'open', reason: 'rescored' },
    ],
    status_note: null,
    engine_version: 'test',
  };
})();

const CATEGORIES_TEXT: Record<S['RiskCategory'], string> = {
  logistics: 'shipping disruption', trade_policy: 'trade policy', sanctions: 'sanctions', cyber: 'cyber incidents', geopolitical: 'conflict',
  natural_hazard: 'natural hazards', labour: 'labour action', financial: 'currency moves', other: 'events',
};

// --- connections, sources, mapping -------------------------------------------------------
export const connection: S['Connection'] = {
  id: CONN, provider: 'google_sheets', display_name: 'Google Sheets', status: 'active', status_reason: null,
  account_label: 'ops@kestrel.test', source_count: 2, watch: { expires_at: iso(-6 * D), renew_after: iso(-5 * D) }, created_at: iso(20 * D), updated_at: iso(H),
};
const col = (index: number, name: string, samples: string[]): S['SourceColumn'] => ({ index, name, samples });
export const sourceActive: S['Source'] = {
  id: SRC, connection_id: CONN, kind: 'google_sheet', external_id: 'sheet-1', external_table_id: '', name: 'Suppliers', url: null,
  status: 'active', status_reason: null, header_row: 1, columns: [], row_count: 19, entity_count: 18, last_synced_at: iso(H), last_change_at: iso(2 * H), created_at: iso(20 * D),
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
  { id: '7d1f3c2a-0000-4000-8000-0000000000a1', kind: 'sanctions_match', severity: 'critical', title: 'Sanctions news in Malaysia affecting Penang Semicon Assembly', body: '', risk_id: risks[2].id, connection_id: null, created_at: iso(3 * H), read_at: null, risk_status: 'open' },
  { id: '7d1f3c2a-0000-4000-8000-0000000000a2', kind: 'risk_escalated', severity: 'warning', title: 'Shipping through Bab el-Mandeb rose to 34', body: '', risk_id: risks[0].id, connection_id: null, created_at: iso(6 * H), read_at: null, risk_status: 'open' },
];

export function scenarioFor(name: string, inputs: S['ScenarioInputs']): S['Scenario'] {
  return {
    id: '7d1f3c2a-0000-4000-8000-0000000005c1', name, description: null, status: 'completed', created_at: iso(0), completed_at: iso(0), inputs, error: null,
    results: {
      baseline: { risk_count: 6, story_count: 4, max_score: 81, portfolio_score: 62.4, by_category: {}, most_exposed: { entity_id: byName('Yantian to Rotterdam').id, entity_name: 'Yantian to Rotterdam', exposure: 84.2, risk_count: 2 } },
      scenario: { risk_count: 8, story_count: 5, max_score: 86, portfolio_score: 73.1, by_category: {}, most_exposed: { entity_id: byName('Yantian to Rotterdam').id, entity_name: 'Yantian to Rotterdam', exposure: 90.3, risk_count: 2 } },
      changes: [
        { fingerprint: 'f1', risk_id: risks[0].id, entity_id: byName('Yantian to Rotterdam').id, entity_name: 'Yantian to Rotterdam', category: 'logistics', title: 'Route disrupted', baseline_score: 74, scenario_score: 86, delta: 12, lat: null, lon: null, countries: ['CHN', 'NLD'] },
        { fingerprint: 'f2', risk_id: null, entity_id: byName('Kaohsiung to Rotterdam').id, entity_name: 'Kaohsiung to Rotterdam', category: 'logistics', title: 'Route disrupted', baseline_score: null, scenario_score: 71, delta: 71, lat: null, lon: null, countries: ['TWN', 'NLD'] },
        { fingerprint: 'f3', risk_id: null, entity_id: byName('Atlas Rotterdam Distribution Centre').id, entity_name: 'Atlas Rotterdam Distribution Centre', category: 'logistics', title: 'Inbound delays', baseline_score: null, scenario_score: 44, delta: 44, lat: 51.92, lon: 4.48, countries: ['NLD'] },
      ],
      engine_version: 'test',
      as_of: iso(0),
    },
  };
}

// --- pages real data comes close to (e2e "risk near the limits") -------------------------------
// The Bab el-Mandeb page at another chokepoint, with three advice steps of three lines each (at
// most 20 words, as worker valid_advice allows) and a long second headline clamped to two lines.
// "Shipping through the Strait of Hormuz" is the page the ui-3 brief names; its place title sets
// on one line at 1360 × 900 here (379 of 392 px), so "Shipping through the Cape of Good Hope",
// which sets on two, is the same page with a two-line title. Neither chokepoint is in the
// disruptions fixture, so the other screens' maps are unchanged; the risk's own point is at the
// chokepoint. Served only by GET /v1/risks/{id}. Test fixtures.
type ChokePage = {
  n: number;
  at: [id: string, label: string, lat: number, lon: number, countries: string[]];
  routes: string[];
  suppliers: [name: string, city: string, criticality: S['Criticality']][];
  headlines: [string, string];
  advice: [string, string, string];
};
function chokepointPage(p: ChokePage): S['RiskDetail'] {
  const base = riskDetail(risks[0]);
  const [cid, label, lat, lon, countries] = p.at;
  const uuid = (k: number) => `7d1f3c2a-0000-4000-8000-0000000${String(p.n).padStart(2, '0')}e${String(k).padStart(2, '0')}`;
  const routes = p.routes.map((name, k) => ({ id: uuid(k), name, kind: 'route' as const, city: null, criticality: null }));
  const suppliers = p.suppliers.map(([name, city, criticality], k) => ({ id: uuid(10 + k), name, kind: 'supplier' as const, city, criticality }));
  const evidence: S['Evidence'][] = p.headlines.map((title, k) => ({
    ...base.evidence[k], evidence_key: `event:newsdata:${cid}-${k}`, event: { source: 'newsdata', raw_id: `${cid}-${k}` }, headline: title, cut: false, match_value: cid,
    snapshot: { ...base.evidence[k].snapshot, title, url: `https://news.example.test/${cid}-${k}` },
  }));
  return {
    ...base,
    id: riskId(p.n),
    entity: { id: routes[0].id, kind: 'route', name: routes[0].name, city: null },
    title: `Shipping disruption news at ${label} on route ${routes[0].name}`,
    countries, ports: [], lat, lon, place: null, evidence_count: evidence.length,
    story: {
      key: `logistics|chokepoint:${cid}`, risk_count: routes.length + suppliers.length, entity_count: routes.length + suppliers.length,
      kind_counts: { route: routes.length, supplier: suppliers.length }, entities: [...routes, ...suppliers],
    },
    where: { match_type: 'chokepoint', match_value: cid, label },
    lead: { title: p.headlines[0], source: 'newsdata', published_at: evidence[0].snapshot.published_at ?? null, url: evidence[0].snapshot.url ?? null, type: 'news' },
    explanation: `Recent news reports about shipping disruption at ${label} reach ${routes[0].name}.`,
    factors: {
      ...base.factors,
      story: `logistics|${evidence[0].evidence_key}`,
      explain: {
        ...base.factors.explain!,
        matched: { match_type: 'chokepoint', match_value: cid, label: `chokepoint: ${label}`, matched_text: null, inferred_from: null, supported_by: null },
        strongest: { evidence_key: evidence[0].evidence_key, title: p.headlines[0], source: 'newsdata', published_at: evidence[0].snapshot.published_at ?? null },
      },
    },
    advice: { text: p.advice.join('\n'), source: 'groq', model: 'llama-test', generated_at: iso(2 * H) },
    evidence,
  };
}
export const hormuzCase = chokepointPage({
  n: 98,
  at: ['hormuz', 'Strait of Hormuz', 26.57, 56.25, ['IRN', 'OMN']],
  routes: ['Jebel Ali to Rotterdam', 'Dammam to Antwerp', 'Hamad to Rotterdam'],
  suppliers: [['Jubail Polymer Industries', 'Jubail', 'high'], ['Ras Laffan Specialty Solvents', 'Ras Laffan', 'medium']],
  headlines: [
    'Tanker Traffic Through Hormuz Slows as Insurers Raise War-Risk Premiums',
    'Shipowners Hold Gulf Sailings as Navies Escort Tankers Through the Strait of Hormuz After a Week of Drone Attacks on Merchant Vessels',
  ],
  advice: [
    'Ask your carriers which Jebel Ali and Dammam sailings are waiting for naval escorts, and what war-risk surcharges they quote.',
    'Order ahead polymer resins and specialty solvents from Jubail and Ras Laffan that would run short first if tankers wait.',
    'Check which supplier and customer contracts put the extra freight, insurance and demurrage costs of a Hormuz closure on you.',
  ],
});
export const capeCase = chokepointPage({
  n: 97,
  at: ['cape_of_good_hope', 'Cape of Good Hope', -34.36, 18.47, ['ZAF']],
  routes: ['Shanghai to Rotterdam', 'Singapore to Hamburg', 'Kaohsiung to Antwerp'],
  suppliers: [['Penang Microsystems', 'Penang', 'high'], ['Kaohsiung Precision Optics', 'Kaohsiung', 'medium']],
  headlines: [
    'Winter Swells Off the Cape of Good Hope Delay Rerouted Container Ships',
    'Carriers Add Weeks to Asia to Europe Schedules as Rerouted Ships Queue for Fuel at Cape Town and Port Louis During Winter Storms',
  ],
  advice: [
    'Ask your carriers which Shanghai and Singapore sailings now detour around the Cape of Good Hope, and what surcharges apply.',
    'Order ahead the semiconductor components from Penang and Kaohsiung that would run short first if deliveries arrive weeks later.',
    'Check which supplier and customer contracts put the extra freight, bunker and insurance costs of the longer route on you.',
  ],
});
