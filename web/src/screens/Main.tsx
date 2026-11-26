// design/project/Main.dc.html (Overview) and the Register list (System-Nav, phone).
// Left sheet: headline figure, layer chips by category, top risks, and the selected
// risk. The map shows the business's entities, its routes and the places risks sit.
import { useEffect, useMemo, useRef, useState } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { useMapEntities, useMapRisks, useRisk, useRiskSummary, useRisks, useFeedHealth, type Risk, type RiskSort } from '../api/queries';
import { useSession } from '../app/Session';
import { useWorkspaceState } from '../app/TopBar';
import { Link, navigate, useLocation } from '../lib/router';
import { CATEGORIES, CATEGORY_ORDER, KINDS, fmtDay, fmtInt, ago, outOf5, plural, type RiskCategory } from '../lib/format';
import { placeLabel } from '../map/geo';
import { useMap, fitTo, sheetPadding } from '../map/MapRoot';
import { EntitiesLayer, RiskPlacesLayer } from '../map/layers';
import { usePlaced } from '../map/data';
import { Icon } from '../ui/Icon';
import { ErrorState, FeedStatus, LiveDot, MapControls, Pct, ScorePill, SkeletonRows } from '../ui/parts';
import { Grab } from '../ui/Grab';

const LEGEND: Partial<Record<RiskCategory, string>> = {
  geopolitical: 'Conflict and tension near your entities and routes',
  logistics: 'Choke points and ports on your routes',
  natural_hazard: 'Storms, floods and quakes near your entities',
  sanctions: 'Entities that match a sanctions list',
  financial: 'Currency and financial stress where you pay or sell',
  cyber: 'Your software, checked against known exploited flaws',
};

export function RiskPlace(r: Pick<Risk, 'entity' | 'countries' | 'ports'> & { story?: Risk['story'] }) {
  const where = placeLabel({ ports: r.ports.slice(0, 1), countries: r.countries });
  // One story that reaches several entities is listed once, with the others counted.
  const more = r.story && r.story.risk_count > 1 ? ` and ${r.story.risk_count - 1} more` : '';
  return where && !more ? `${r.entity.name} · ${where}` : `${r.entity.name}${more}`;
}

export function Main({ mode }: { mode: 'overview' | 'register' }) {
  const { workspace, stream } = useSession();
  const ws = workspace.id;
  const { query } = useLocation();
  const entityFilter = mode === 'register' ? query.get('entity') : null;
  const [category, setCategory] = useState<RiskCategory | null>(null);
  const [sort, setSort] = useState<RiskSort>('-score');
  const [picked, setPicked] = useState<string | null>(null);

  const summary = useRiskSummary(ws);
  // One row per story: a news report that reaches ten suppliers is one risk on the list.
  const risks = useRisks(ws, { category, sort, limit: mode === 'overview' ? 4 : 50, entityId: entityFilter, group: entityFilter ? null : 'story' });
  const mapRisks = useMapRisks(ws, category);
  const entities = useMapEntities(ws);
  const health = useFeedHealth();
  const { broken } = useWorkspaceState();

  const items = useMemo(() => risks.data?.pages.flatMap((p) => p.items) ?? [], [risks.data]);
  const selected = mode === 'overview' ? (items.find((r) => r.id === picked) ?? items[0] ?? null) : null;
  const detail = useRisk(ws, selected?.id ?? null);

  const placedEntities = usePlaced(entities.data);
  const placedRisks = usePlaced(mapRisks.data);

  // The one entity the register is filtered to: its name, and its point on the map.
  const focusFeature = entityFilter ? (entities.data?.features.find((f) => f.id === entityFilter) ?? null) : null;
  const focusName = focusFeature?.properties.name ?? (entityFilter ? items.find((r) => r.entity.id === entityFilter)?.entity.name : null) ?? null;
  const [focusFc, othersFc] = useMemo(() => {
    if (!entityFilter || !placedEntities) return [null, placedEntities?.fc ?? null];
    const fs = placedEntities.fc.features;
    const focus = fs.filter((f) => f.properties._id === entityFilter);
    if (!focus.length) return [null, placedEntities.fc];
    return [{ ...placedEntities.fc, features: focus }, { ...placedEntities.fc, features: fs.filter((f) => f.properties._id !== entityFilter) }];
  }, [entityFilter, placedEntities]);

  // Fit the business when its entities first arrive, or the one entity the register is
  // filtered to (again whenever the filter changes).
  const { map } = useMap();
  const fitted = useRef<string | null>(null);
  const fitBox = useMemo(() => boxOf(placedEntities?.fc), [placedEntities]);
  const focusBox = useMemo(() => boxOf(focusFc), [focusFc]);
  useEffect(() => {
    const key = entityFilter ?? '';
    if (!map || fitted.current === key) return;
    if (entityFilter) {
      if (!placedEntities) return; // not placed yet
      fitted.current = key;
      // An entity with no place on the map keeps the whole business in view.
      if (focusBox) fitTo(map, focusBox, sheetPadding('left', 344), 4);
      else if (fitBox) fitTo(map, fitBox, sheetPadding('left', 344));
      return;
    }
    if (!fitBox) return;
    fitted.current = key;
    fitTo(map, fitBox, sheetPadding('left', 344));
  }, [map, fitBox, focusBox, entityFilter, placedEntities]);

  const pickPlace = (id: string) => (mode === 'overview' ? setPicked(id) : navigate(`/risks/${id}`));

  const byCategory = summary.data?.stories_by_category ?? summary.data?.by_category;
  const chips = CATEGORY_ORDER.filter((c) => (byCategory?.[c] ?? 0) > 0);
  // A story is one feed item or event, however many entities it reaches; a risk is one
  // entity × story. The headline counts stories, with risks second.
  const open = summary.data?.story_count ?? summary.data?.open_count ?? 0;
  const riskRows = summary.data?.open_count ?? open;
  const storyWord = summary.data?.story_count != null ? 'open story' : 'open risk';
  const headline = plural(open, storyWord, storyWord === 'open story' ? 'open stories' : 'open risks');
  const secondary = summary.data?.story_count != null && riskRows !== open ? plural(riskRows, 'risk') : null;
  const noRisks = summary.isSuccess && open === 0;

  return (
    <>
      <EntitiesLayer data={othersFc} labels="names" muted={!!focusFc} />
      {focusFc && <EntitiesLayer id="focus" data={focusFc} ring labels="strong" />}
      <RiskPlacesLayer data={(placedRisks?.fc as FeatureCollection<Point, never>) ?? null} callouts={5} selected={selected?.id ?? null} onPick={pickPlace} />

      <aside className="sheet w344 glass" aria-label={mode === 'overview' ? 'Risks' : 'Risk register'}>
        <Grab />
        <div className="sec" style={{ paddingTop: 18 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span className="lbl">{mode === 'overview' ? `Highest open score · ${fmtDay(new Date())}` : 'Risk register'}</span>
            <LiveDot status={stream} />
          </div>
          {summary.isPending ? (
            <div className="sk" style={{ height: 52, width: 140, marginTop: 6 }} />
          ) : summary.isError ? (
            <div style={{ marginTop: 8 }}><ErrorState error={summary.error} retry={() => void summary.refetch()} /></div>
          ) : mode === 'overview' ? (
            <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12, marginTop: 6 }}>
              <div style={{ display: 'flex', alignItems: secondary ? 'flex-end' : 'baseline', gap: 8, minWidth: 0 }}>
                <span className="figxl">{summary.data.max_score == null ? '0' : Math.round(summary.data.max_score)}</span>
                <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0, paddingBottom: secondary ? 7 : 0 }}>
                  <span className="t13" style={{ fontWeight: 600, color: 'var(--ink)', whiteSpace: 'nowrap' }}>{headline}</span>
                  {secondary && <span className="t12" style={{ whiteSpace: 'nowrap' }} title="One risk for each entity a story reaches">{secondary}</span>}
                </span>
              </div>
              <span className="t12" style={{ textAlign: 'right' }}>Scored {ago(summary.data.last_scored_at)}</span>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 6 }}>
              <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, minWidth: 0 }}>
                <span className="h2" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>{headline}</span>
                {secondary && <span className="t13" style={{ flex: 'none' }} title="One risk for each entity a story reaches">{secondary}</span>}
              </span>
              <div className="segw" role="group" aria-label="Sort">
                <button type="button" className={`sg${sort === '-score' ? ' on' : ''}`} aria-pressed={sort === '-score'} onClick={() => setSort('-score')}>Highest first</button>
                <button type="button" className={`sg${sort === '-last_seen_at' ? ' on' : ''}`} aria-pressed={sort === '-last_seen_at'} onClick={() => setSort('-last_seen_at')}>Newest first</button>
              </div>
            </div>
          )}
        </div>

        {chips.length > 0 && (
          <div className="sec" style={{ paddingTop: 8, paddingBottom: 8 }}>
            <div className="chips" role="group" aria-label="Map layer">
              <button type="button" className={`chip${category === null ? ' on' : ''}`} aria-pressed={category === null} onClick={() => setCategory(null)}>
                All{mode === 'register' && <small>{fmtInt(open)}</small>}
              </button>
              {chips.map((c) => (
                <button key={c} type="button" className={`chip${category === c ? ' on' : ''}`} aria-pressed={category === c} onClick={() => setCategory(c)}>
                  {CATEGORIES[c].name}
                  {mode === 'register' && <small>{fmtInt(byCategory?.[c] ?? 0)}</small>}
                </button>
              ))}
            </div>
          </div>
        )}

        <div className="sec" style={{ paddingTop: 10, paddingBottom: 10 }}>
          {entityFilter && (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 6 }}>
              <span className="lbl" style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={focusName ?? undefined}>
                {focusName ?? 'One entity'}
              </span>
              <Link to="/register" className="b plain" style={{ height: 44, padding: '0 10px', fontSize: 13 }}>Show all</Link>
            </div>
          )}
          {risks.isPending ? (
            <SkeletonRows n={4} />
          ) : risks.isError ? (
            <ErrorState error={risks.error} retry={() => void risks.refetch()} />
          ) : items.length === 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '6px 0' }}>
              <span className="h3" style={{ fontSize: 15 }}>{noRisks ? 'No risks touch you yet' : 'Nothing in this layer'}</span>
              <span className="t13">
                {noRisks
                  ? `Every feed is checked against your ${plural(entities.data?.features.length ?? 0, 'entity', 'entities')}. A risk appears here the moment one matches.`
                  : 'Pick another layer, or All.'}
              </span>
            </div>
          ) : (
            <div role="group" aria-label="Risks in this layer">
              {items.map((r) =>
                mode === 'overview' ? (
                  <button key={r.id} type="button" className={`it${selected?.id === r.id ? ' on' : ''}`} aria-pressed={selected?.id === r.id} onClick={() => setPicked(r.id)}>
                    <ScorePill score={r.score} />
                    <span style={{ minWidth: 0 }}><b>{r.title}</b><span className="pl">{RiskPlace(r)}</span></span>
                    <Icon name="chev" size={14} stroke={2} className="chev" />
                  </button>
                ) : (
                  <Link key={r.id} to={`/risks/${r.id}`} className="it">
                    <ScorePill score={r.score} />
                    <span style={{ minWidth: 0 }}><b>{r.title}</b><span className="pl">{CATEGORIES[r.category].name} · {RiskPlace(r)}</span></span>
                    <Icon name="chev" size={14} stroke={2} className="chev" />
                  </Link>
                ),
              )}
              {mode === 'register' && risks.hasNextPage && (
                <button type="button" className="b sec2" style={{ width: '100%', marginTop: 8 }} onClick={() => void risks.fetchNextPage()} disabled={risks.isFetchingNextPage}>
                  {risks.isFetchingNextPage && <span className="spin" />}Show more
                </button>
              )}
            </div>
          )}
        </div>

        {mode === 'overview' && selected && (
          <div className="sec" style={{ flex: 1, display: 'flex', flexDirection: 'column', gap: 12 }}>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
              <span className="lbl">{CATEGORIES[selected.category].name} · {KINDS[selected.entity.kind].one}</span>
              <span className="h3">{selected.title}</span>
              {detail.isPending ? (
                <span className="sk" style={{ height: 36 }} />
              ) : detail.isError ? (
                <span className="t13">The explanation did not load.</span>
              ) : (
                <span className="t13" style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{detail.data.explanation}</span>
              )}
            </div>
            <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 12 }}>
              <div className="tile"><span className="fig"><Pct value={selected.likelihood} /></span><span className="t12">likely</span></div>
              <div className="tile"><span className="fig">{outOf5(selected.impact)}/5</span><span className="t12">impact on you</span></div>
            </div>
            <div style={{ display: 'flex', gap: 8, marginTop: 2 }}>
              <Link to={`/risks/${selected.id}`} className="b pri" style={{ flex: 1 }}>Open risk</Link>
              <Link to={`/scenarios?risk=${selected.id}`} className="b sec2">What if</Link>
            </div>
          </div>
        )}
      </aside>

      <MapControls fit={fitBox} fitLabel="Fit all your entities" />

      <div className="legend glass" style={{ paddingRight: broken.length ? 8 : 18 }}>
        <span className="fill">{category ? (LEGEND[category] ?? `${CATEGORIES[category].name} risks`) : noRisks ? 'Your entities' : 'Every risk that touches your entities'}</span>
        <span className="vr" />
        {broken.length ? (
          <>
            <span className="k" style={{ color: '#141518' }}><span className="rdot" />{broken[0]} stopped syncing</span>
            <Link to="/connect" className="b" style={{ height: 44, padding: '0 12px', background: 'transparent', color: '#1766F0', fontSize: 13 }}>Fix</Link>
          </>
        ) : (
          <FeedStatus health={health.data} pending={health.isPending} failed={health.isError} />
        )}
      </div>
    </>
  );
}

export function boxOf(fc: FeatureCollection<Point> | undefined | null): [[number, number], [number, number]] | null {
  if (!fc || !fc.features.length) return null;
  let w = 180, s = 90, e = -180, n = -90;
  for (const f of fc.features) {
    const [x, y] = f.geometry.coordinates;
    w = Math.min(w, x); e = Math.max(e, x); s = Math.min(s, y); n = Math.max(n, y);
  }
  return [[w, s], [e, n]];
}
