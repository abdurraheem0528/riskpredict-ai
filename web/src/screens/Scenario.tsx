// design/project/Scenario.dc.html — a what-if. The four kinds of event are the shocks the
// API accepts (ScenarioInputs): a choke point closing, an event in a country, a currency
// move and a newly sanctioned entity. Results are POST /v1/scenarios, run by the engine.
import { useEffect, useMemo, useState } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useDisruptions, useEntities, useMapEntities, useRisk, useScenario, useScenarios, type S, type Scenario as ScenarioT } from '../api/queries';
import { api, unwrap } from '../api/client';
import { wsh } from '../api/workspace';
import { ApiError, errorText } from '../api/errors';
import { useSession, canEdit } from '../app/Session';
import { navigate, useLocation } from '../lib/router';
import { CATEGORIES, LEVELS, levelOf, ago, plural, signed, scoreText, type RiskCategory } from '../lib/format';
import { countryName, placeOf } from '../map/geo';
import { useMap, fitTo, sheetPadding } from '../map/MapRoot';
import { DotsLayer, EntitiesLayer } from '../map/layers';
import { usePlaced, useGazetteer } from '../map/data';
import { Icon } from '../ui/Icon';
import { ErrorState, SkeletonRows } from '../ui/parts';
import { Grab } from '../ui/Grab';

type Kind = 'route' | 'country' | 'fx' | 'sanction';
const OPTIONS: { kind: Kind; name: string; hint: string }[] = [
  { kind: 'route', name: 'Choke point closes', hint: 'Shipping' },
  { kind: 'country', name: 'Event in a country', hint: 'Geopolitical or hazard' },
  { kind: 'fx', name: 'Currency moves', hint: 'Financial' },
  { kind: 'sanction', name: 'Entity sanctioned', hint: 'Sanctions' },
];

const curName = (() => {
  try {
    const d = new Intl.DisplayNames(['en'], { type: 'currency' });
    return (c: string) => d.of(c) ?? c;
  } catch {
    return (c: string) => c;
  }
})();

export function Scenario() {
  const { workspace, role } = useSession();
  const ws = workspace.id;
  const { query } = useLocation();
  const savedId = query.get('id');
  const fromRisk = query.get('risk');
  const qc = useQueryClient();

  const disruptions = useDisruptions();
  const entities = useEntities(ws);
  const mapEntities = useMapEntities(ws);
  const saved = useScenarios(ws);
  const savedOne = useScenario(ws, savedId);
  const seed = useRisk(ws, fromRisk);

  const chokepoints = useMemo(
    () => (disruptions.data?.features ?? []).filter((f) => f.properties.kind === 'chokepoint').map((f) => ({ id: f.id.replace(/^chokepoint:/, ''), name: f.properties.name, at: f.geometry.coordinates as [number, number], radius: f.properties.radius_km ?? null, status: f.properties.status })),
    [disruptions.data],
  );
  const items = useMemo(() => entities.data?.items ?? [], [entities.data]);
  const countries = useMemo(() => [...new Set(items.flatMap((e) => e.countries))].sort((a, b) => countryName(a).localeCompare(countryName(b))), [items]);
  const currencies = useMemo(
    () => [...new Set(items.flatMap((e) => [...e.currencies, ...(e.exposure.spend_currency ? [e.exposure.spend_currency] : [])]))].filter((c) => c !== workspace.base_currency).sort(),
    [items, workspace.base_currency],
  );
  const sanctionable = useMemo(() => [...items].sort((a, b) => (a.kind === 'supplier' ? -1 : 0) - (b.kind === 'supplier' ? -1 : 0) || a.name.localeCompare(b.name)), [items]);

  const [kind, setKind] = useState<Kind>('route');
  const [choke, setChoke] = useState('');
  const [country, setCountry] = useState('');
  const [eventCat, setEventCat] = useState<RiskCategory>('geopolitical');
  const [currency, setCurrency] = useState('');
  const [entityId, setEntityId] = useState('');
  const [severity, setSeverity] = useState(80);
  const [move, setMove] = useState(10);
  const [result, setResult] = useState<ScenarioT | null>(null);
  const [seeded, setSeeded] = useState(false);

  // Defaults once the lists arrive.
  useEffect(() => { if (!choke && chokepoints[0]) setChoke(chokepoints[0].id); }, [chokepoints, choke]);
  useEffect(() => { if (!country && countries[0]) setCountry(countries[0]); }, [countries, country]);
  useEffect(() => { if (!currency && currencies[0]) setCurrency(currencies[0]); }, [currencies, currency]);
  useEffect(() => { if (!entityId && sanctionable[0]) setEntityId(sanctionable[0].id); }, [sanctionable, entityId]);

  // Coming from a risk: start from its kind of event, place and entity.
  useEffect(() => {
    const r = seed.data;
    if (!r || seeded) return;
    setSeeded(true);
    if (r.category === 'sanctions') {
      setKind('sanction');
      setEntityId(r.entity.id);
    } else if (r.category === 'financial') {
      setKind('fx');
    } else if (r.category === 'logistics' && r.evidence.some((e) => e.match_type === 'chokepoint')) {
      setKind('route');
      const ev = r.evidence.find((e) => e.match_type === 'chokepoint');
      const cp = chokepoints.find((c) => c.id === ev?.match_value || c.name.toLowerCase() === ev?.match_value.toLowerCase());
      if (cp) setChoke(cp.id);
    } else if (r.countries[0]) {
      setKind('country');
      setCountry(r.countries[0]);
      setEventCat(r.category === 'logistics' || r.category === 'cyber' ? 'geopolitical' : r.category);
    }
  }, [seed.data, seeded, chokepoints]);

  // A saved scenario opened from the list.
  useEffect(() => {
    if (savedOne.data) setResult(savedOne.data);
  }, [savedOne.data]);

  const cp = chokepoints.find((c) => c.id === choke);
  const ent = items.find((e) => e.id === entityId);
  const base = workspace.base_currency;
  const spec = (() => {
    switch (kind) {
      case 'route':
        return cp
          ? { title: `${cp.name} closes`, sub: 'Every route you mapped through it is scored as disrupted, at the severity you set.', shock: { type: 'event' as const, category: 'logistics' as const, severity: severity / 100, chokepoints: [cp.id], label: `${cp.name} closes` } }
          : null;
      case 'country':
        return country
          ? { title: `${CATEGORIES[eventCat].name === 'Hazards' ? 'Natural disaster' : CATEGORIES[eventCat].name + ' crisis'} in ${countryName(country)}`, sub: 'Treated as a new event there, matched to your entities by the normal rules.', shock: { type: 'event' as const, category: eventCat, severity: severity / 100, countries: [country], label: `${CATEGORIES[eventCat].name} event in ${countryName(country)}` } }
          : null;
      case 'fx':
        return currency
          ? { title: `${curName(currency)} ${move >= 0 ? 'rises' : 'falls'} ${Math.abs(move)}%`, sub: `Against ${base ? curName(base).toLowerCase() : 'your base currency'}. What you pay or earn in ${currency} changes with it.`, shock: { type: 'fx' as const, currency, pct_change: move, ...(base ? { base_currency: base } : {}) } }
          : null;
      case 'sanction':
        return ent ? { title: `${ent.name} is sanctioned`, sub: 'Treated as newly designated. You would have to stop trading with it.', shock: { type: 'sanction' as const, entity_id: ent.id } } : null;
    }
  })();

  const run = useMutation({
    mutationFn: () =>
      unwrap(api.POST('/v1/scenarios', { params: { header: wsh(ws) }, body: { name: spec!.title, inputs: { shocks: [spec!.shock] } } })),
    onSuccess: (s) => {
      setResult(s);
      void qc.invalidateQueries({ queryKey: [ws, 'scenarios'] });
      navigate(`/scenarios?id=${s.id}`, { replace: true });
    },
  });

  // --- map: the shock's place, then who it reaches --------------------------------
  const ready = useGazetteer();
  const placed = usePlaced(mapEntities.data);
  const res = result?.results ?? null;
  const affectedIds = useMemo(() => new Set(res?.changes.map((c) => c.entity_id) ?? []), [res]);
  const affected = useMemo(() => (placed ? { ...placed.fc, features: placed.fc.features.filter((f) => affectedIds.has(f.properties._id)) } : null), [placed, affectedIds]);
  const others = useMemo(() => (placed ? { ...placed.fc, features: placed.fc.features.filter((f) => !affectedIds.has(f.properties._id)) } : null), [placed, affectedIds]);
  const shockAt = useMemo<[number, number] | null>(() => {
    if (!ready) return null;
    if (kind === 'route') return cp?.at ?? null;
    if (kind === 'country') return placeOf({ countries: [country] });
    if (kind === 'sanction') return placed?.fc.features.find((f) => f.properties._id === entityId)?.geometry.coordinates as [number, number] ?? null;
    return null;
  }, [ready, kind, cp, country, entityId, placed]);
  const shockFc = useMemo<FeatureCollection<Point> | null>(
    () => (shockAt ? { type: 'FeatureCollection', features: [{ type: 'Feature', properties: {}, geometry: { type: 'Point', coordinates: shockAt } }] } : null),
    [shockAt],
  );
  const { map } = useMap();
  useEffect(() => {
    if (!map) return;
    const pts = [...(shockAt ? [shockAt] : []), ...((affected?.features ?? []).map((f) => f.geometry.coordinates as [number, number]))];
    if (!pts.length) return;
    let w = 180, s = 90, e = -180, n = -90;
    for (const [x, y] of pts) { w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y); }
    fitTo(map, [[w, s], [e, n]], sheetPadding('right', 400), 4);
  }, [map, shockAt, affected]);

  const editor = canEdit(role);
  const loadingLists = entities.isPending || disruptions.isPending;
  const disabled: Record<Kind, boolean> = { route: chokepoints.length === 0, country: countries.length === 0, fx: currencies.length === 0, sanction: sanctionable.length === 0 };
  const pctVar = kind === 'fx' ? `${((move + 50) / 100) * 100}%` : `${((severity - 10) / 90) * 100}%`;

  return (
    <>
      <CountryShade iso={kind === 'country' ? country : null} />
      <EntitiesLayer id="sc-others" data={others} muted />
      <EntitiesLayer id="sc-affected" data={affected} labels="names" />
      <DotsLayer id="sc-shock" data={shockFc} color="#DD6248" radius={6} />

      <aside className="sheet right split glass" aria-label="What if">
        <Grab />
        <div className="sbody">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span className="lbl">What if</span>
          <h1 className="h2">{spec?.title ?? 'Pick an event'}</h1>
          <span style={{ fontSize: 14, lineHeight: '20px', color: '#5F646C' }}>{spec?.sub ?? 'Choose what happens, then run it against your entities.'}</span>
        </div>

        <div className="opts" role="radiogroup" aria-label="Event">
          {OPTIONS.map((o) => (
            <button
              key={o.kind}
              type="button"
              role="radio"
              aria-checked={kind === o.kind}
              className={`op${kind === o.kind ? ' on' : ''}`}
              disabled={!loadingLists && disabled[o.kind]}
              onClick={() => { setKind(o.kind); setResult(null); }}
            >
              <b>{o.name}</b>
              <span>{!loadingLists && disabled[o.kind] ? 'Nothing to pick yet' : o.hint}</span>
            </button>
          ))}
        </div>

        {loadingLists ? (
          <SkeletonRows n={2} />
        ) : entities.isError ? (
          <ErrorState error={entities.error} retry={() => void entities.refetch()} />
        ) : (
          <>
            <div className="field">
              {kind === 'route' && (
                <>
                  <label htmlFor="sc-pick">Choke point</label>
                  <select id="sc-pick" className="sel" value={choke} onChange={(e) => { setChoke(e.target.value); setResult(null); }}>
                    {chokepoints.map((c) => <option key={c.id} value={c.id}>{c.name}{c.status !== 'normal' ? ` · ${c.status}` : ''}</option>)}
                  </select>
                </>
              )}
              {kind === 'country' && (
                <>
                  <label htmlFor="sc-pick">Country, from your entities</label>
                  <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 140px', gap: 8 }}>
                    <select id="sc-pick" className="sel" value={country} onChange={(e) => { setCountry(e.target.value); setResult(null); }}>
                      {countries.map((c) => <option key={c} value={c}>{countryName(c)}</option>)}
                    </select>
                    <select className="sel" aria-label="Kind of event" value={eventCat} onChange={(e) => { setEventCat(e.target.value as RiskCategory); setResult(null); }}>
                      {(['geopolitical', 'natural_hazard', 'trade_policy', 'labour'] as RiskCategory[]).map((c) => <option key={c} value={c}>{CATEGORIES[c].name}</option>)}
                    </select>
                  </div>
                </>
              )}
              {kind === 'fx' && (
                <>
                  <label htmlFor="sc-pick">Currency you pay or sell in</label>
                  <select id="sc-pick" className="sel" value={currency} onChange={(e) => { setCurrency(e.target.value); setResult(null); }}>
                    {currencies.map((c) => <option key={c} value={c}>{c} · {curName(c)}</option>)}
                  </select>
                </>
              )}
              {kind === 'sanction' && (
                <>
                  <label htmlFor="sc-pick">Entity</label>
                  <select id="sc-pick" className="sel" value={entityId} onChange={(e) => { setEntityId(e.target.value); setResult(null); }}>
                    {sanctionable.map((e) => <option key={e.id} value={e.id}>{e.name}</option>)}
                  </select>
                </>
              )}
            </div>
            {kind !== 'sanction' && (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
                  <label htmlFor="sc-rng" className="lbl">{kind === 'fx' ? 'How far it moves' : 'How severe'}</label>
                  <span style={{ fontSize: 15, lineHeight: '20px', fontWeight: 600 }} className="tnum">{kind === 'fx' ? `${signed(move)}%` : `${severity}%`}</span>
                </div>
                <input
                  id="sc-rng"
                  className="rng"
                  type="range"
                  min={kind === 'fx' ? -50 : 10}
                  max={kind === 'fx' ? 50 : 100}
                  step={kind === 'fx' ? 5 : 10}
                  value={kind === 'fx' ? move : severity}
                  onChange={(e) => { (kind === 'fx' ? setMove : setSeverity)(Number(e.target.value)); setResult(null); }}
                  style={{ ['--p' as string]: pctVar }}
                />
              </div>
            )}
          </>
        )}

        <Results result={result} running={run.isPending} />

        {run.isError && (
          <div className="note" role="alert">
            <Icon name="warning" style={{ flex: 'none', marginTop: 1 }} />
            {run.error instanceof ApiError && run.error.errors.length ? run.error.errors.map((e) => e.message).join(' ') : errorText(run.error)}
          </div>
        )}

        <SavedList items={saved.data?.items ?? []} current={result?.id ?? null} />

        </div>
        <div className="sfoot">
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" className="b pri" style={{ flex: 1 }} disabled={!editor || !spec || run.isPending} onClick={() => run.mutate()}>
            {run.isPending && <span className="spin" />}
            {run.isPending ? 'Running' : result ? 'Run again' : 'Run what-if'}
          </button>
        </div>
        <span className="t12">
          {editor ? 'An estimate from your current entities and today’s feeds, not a forecast. Saved for everyone in this workspace.' : 'Viewers can open saved what-ifs. An editor runs new ones.'}
        </span>
        </div>
      </aside>

      <div className="legend left glass">
        <span className="k"><span className="sw" style={{ background: '#DD6248' }} />Where it happens</span>
        <span className="k"><span className="sw" style={{ background: '#141518' }} />Affected</span>
        <span className="k"><span className="sw" style={{ background: '#A3A7AE' }} />Not affected</span>
      </div>
    </>
  );
}

function Results({ result, running }: { result: ScenarioT | null; running: boolean }) {
  if (running) return <SkeletonRows n={3} h={40} />;
  if (!result) return null;
  if (result.status === 'failed' || !result.results) {
    return (
      <div className="note" role="alert">
        <Icon name="warning" style={{ flex: 'none', marginTop: 1 }} />
        {result.error ?? 'The engine could not run this what-if.'}
      </div>
    );
  }
  const { baseline, scenario, changes } = result.results;
  const delta = scenario.portfolio_score - baseline.portfolio_score;
  const lvl = levelOf(scenario.portfolio_score);
  return (
    <>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 10, paddingTop: 14, borderTop: '1px solid rgba(20,21,24,0.07)' }}>
        <span className="lbl">Risk index</span>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 12, flexWrap: 'wrap' }}>
          <span className="tnum" style={{ fontSize: 28, lineHeight: '32px', fontWeight: 600, letterSpacing: '-0.02em', color: '#A3A7AE' }}>{Math.round(baseline.portfolio_score)}</span>
          <svg width="20" height="14" viewBox="0 0 20 14" fill="none" stroke="#5F646C" strokeWidth="1.75" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true"><path d="M2 7h15M12 2l5 5-5 5" /></svg>
          <span className="tnum" style={{ fontSize: 40, lineHeight: '44px', fontWeight: 600, letterSpacing: '-0.03em' }}>{Math.round(scenario.portfolio_score)}</span>
          <span className={`lvl l-${lvl}`}>{signed(delta)} · {LEVELS[lvl].name}</span>
        </div>
        <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 8 }}>
          <div className="tile"><span className="fig s">{scenario.risk_count}</span><span className="t12">risks touch you, {baseline.risk_count} today</span></div>
          <div className="tile"><span className="fig s">{scoreText(scenario.max_score)}</span><span className="t12">highest score, {scoreText(baseline.max_score)} today</span></div>
        </div>
      </div>
      <div style={{ display: 'flex', flexDirection: 'column' }}>
        <span className="lbl" style={{ marginBottom: 4 }}>Who feels it first</span>
        {changes.length === 0 && <span className="t13" style={{ padding: '8px 0' }}>No score changes. Nothing you mapped is exposed to this.</span>}
        {changes.slice(0, 3).map((c) => (
          <div className="rw" key={c.fingerprint}>
            <span style={{ fontSize: 14, lineHeight: '20px', fontWeight: 500, minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{c.entity_name}</span>
            <span className="tnum" style={{ fontSize: 13, lineHeight: '18px', fontWeight: 500, color: c.delta > 0 ? LEVELS[levelOf(c.scenario_score ?? 0)].text : '#5F646C', whiteSpace: 'nowrap' }}>
              {c.baseline_score == null ? `new, ${scoreText(c.scenario_score)}` : `${scoreText(c.baseline_score)} → ${scoreText(c.scenario_score)}`}
            </span>
          </div>
        ))}
        {changes.length > 3 && <span className="t12" style={{ paddingTop: 4 }}>{plural(changes.length - 3, 'more risk')} change{changes.length - 3 === 1 ? 's' : ''}</span>}
        <span className="t12" style={{ paddingTop: 6 }}>As of {ago(result.results.as_of)}</span>
      </div>
    </>
  );
}

function SavedList({ items, current }: { items: S['ScenarioSummary'][]; current: string | null }) {
  const rest = items.filter((s) => s.id !== current).slice(0, 3);
  if (!rest.length) return null;
  return (
    <div style={{ display: 'flex', flexDirection: 'column' }}>
      <span className="lbl" style={{ marginBottom: 2 }}>Saved what-ifs</span>
      {rest.map((s) => (
        <button key={s.id} type="button" className="mi" style={{ margin: '0 -12px' }} onClick={() => navigate(`/scenarios?id=${s.id}`)}>
          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.name}</span>
          <small>{s.status === 'failed' ? 'failed' : ago(s.created_at)}</small>
        </button>
      ))}
    </div>
  );
}

/** Tint one country on the base map (uses the style's own `countries` source). */
function CountryShade({ iso }: { iso: string | null }) {
  const { map } = useMap();
  useEffect(() => {
    if (!map) return;
    const id = 'shock-country';
    try {
      if (!map.getLayer(id)) map.addLayer({ id, type: 'fill', source: 'countries', paint: { 'fill-color': '#DD6248', 'fill-opacity': 0.12 }, filter: ['==', ['get', 'iso'], ''] }, 'coast');
      map.setFilter(id, ['==', ['get', 'iso'], iso ?? '']);
    } catch {
      /* style not ready */
    }
  }, [map, iso]);
  useEffect(() => () => {
    try {
      if (map?.getLayer('shock-country')) map.removeLayer('shock-country');
    } catch {
      /* removed */
    }
  }, [map]);
  return null;
}
