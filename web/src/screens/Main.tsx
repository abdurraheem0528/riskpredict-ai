// design/project/Main.dc.html (Overview) and the Register list (System-Nav, phone).
// Left sheet: headline figure, layer chips by category, top risks, and the selected
// risk. The map shows the business's entities, its routes and the places risks sit.
import { useEffect, useMemo, useState } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { prefetchRiskDetail, useDisruptions, useMapEntities, useRisk, useRiskSummary, useRisks, useFeedHealth, type Risk, type RiskSort } from '../api/queries';
import { useQueryClient } from '@tanstack/react-query';
import { useSession } from '../app/Session';
import { useWorkspaceState } from '../app/TopBar';
import { Link, navigate, useLocation } from '../lib/router';
import { CATEGORIES, CATEGORY_ORDER, KINDS, fmtInt, levelOf, outOf5, plural, type RiskCategory } from '../lib/format';
import { placeLabel } from '../map/geo';
import { useMap } from '../map/MapRoot';
import { fitTo, moveTo, sheetPadding, useIdleRotation, useOpeningMove, type LonLat } from '../map/camera';
import { EntityMap } from '../map/EntityLayers';
import { Chokepoints } from '../map/Chokepoints';
import { LanesLayer } from '../map/LanesLayer';
import { useLanes } from '../map/lanes';
import { MapControls } from '../map/MapControls';
import { Legend } from '../map/Legend';
import { clearFocus, setHovered, setSelected, useFocus } from '../map/focus';
import { usePlaced } from '../map/data';
import { Icon } from '../ui/Icon';
import { ErrorState, FeedStatus, Pct, SkeletonRows } from '../ui/parts';
import { CountPill, CountUp } from '../live/CountUp';
import { LiveDot } from '../live/LiveDot';
import { Sparkline } from '../live/Sparkline';
import { useListMotion } from '../live/presence';
import { Grab } from '../ui/Grab';

const LEGEND: Partial<Record<RiskCategory, string>> = {
  geopolitical: 'Conflict and tension near your entities and routes',
  logistics: 'Choke points and ports on your routes',
  natural_hazard: 'Storms, floods and quakes near your entities',
  sanctions: 'Entities that match a sanctions list',
  financial: 'Currency and financial stress where you pay or sell',
  cyber: 'Your software, checked against known exploited flaws',
};

export function RiskPlace(r: Pick<Risk, 'entity' | 'countries' | 'ports' | 'place'> & { story?: Risk['story'] }) {
  const where = placeLabel({ place: r.place, city: r.entity.city, ports: r.ports.slice(0, 1), countries: r.countries });
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
  const entities = useMapEntities(ws);
  const health = useFeedHealth();
  const { broken } = useWorkspaceState();

  const items = useMemo(() => risks.data?.pages.flatMap((p) => p.items) ?? [], [risks.data]);
  // After a live update: rows slide to their new place, enter, leave, and the changed one flashes.
  // A register row under the pointer loads its risk, so the title can morph into its header.
  const qc = useQueryClient();
  const motion = useListMotion(risks.data ? items : undefined, (r) => r.id, (r) => [r.id, r.story.key]);
  const selected = mode === 'overview' ? (items.find((r) => r.id === picked) ?? items[0] ?? null) : null;
  const detail = useRisk(ws, selected?.id ?? null);

  const placedEntities = usePlaced(entities.data);

  // The one entity the register is filtered to: its name, and its point on the map.
  const focusFeature = entityFilter ? (entities.data?.features.find((f) => f.id === entityFilter) ?? null) : null;
  const focusName = focusFeature?.properties.name ?? (entityFilter ? items.find((r) => r.entity.id === entityFilter)?.entity.name : null) ?? null;
  const focusPoint = useMemo(
    () => (entityFilter ? ((placedEntities?.fc.features.find((f) => f.properties._id === entityFilter)?.geometry.coordinates as LonLat | undefined) ?? null) : null),
    [entityFilter, placedEntities],
  );

  // Dimming: with a layer chosen, the entities its stories reach stay bright; on the register
  // for one entity, only that entity does.
  const dimmed = useMemo<Set<string> | null>(() => {
    if (entityFilter) return focusPoint ? new Set([entityFilter]) : null;
    if (!category || !risks.data) return null;
    return new Set(items.flatMap((r) => [r.entity.id, ...r.story.entities.map((e) => e.id)]));
  }, [entityFilter, focusPoint, category, risks.data, items]);

  // The camera: the opening move to the overview (globe: the densest group), or the one entity
  // the register is filtered to; idle rotation on the overview.
  const { map } = useMap();
  const points = useMemo(() => placedEntities?.fc.features.map((f) => f.geometry.coordinates as LonLat) ?? null, [placedEntities]);
  const fitBox = useMemo(() => boxOf(placedEntities?.fc), [placedEntities]);
  useOpeningMove(entityFilter ? null : map, points, 'overview');
  useEffect(() => {
    if (!map || !entityFilter || !placedEntities) return;
    // An entity with no place on the map keeps the whole business in view.
    if (focusPoint) moveTo(map, { center: focusPoint, zoom: Math.max(map.getZoom(), 3.2), padding: sheetPadding('left', 344) });
    else if (fitBox) fitTo(map, fitBox, sheetPadding('left', 344));
  }, [map, entityFilter, focusPoint, fitBox, placedEntities]);
  useIdleRotation(map, mode === 'overview' && !entityFilter);

  // Linked hover and selection: a row and its entity's dot, pill and lanes.
  const focus = useFocus();
  // Sea lanes of the declared routes, and how many pass each chokepoint.
  const disruptions = useDisruptions();
  const lanes = useLanes(entities.data, disruptions.data);
  // Only a pick selects on the map (the card shows the top story either way, and nothing
  // selected lets the globe turn); Esc or a click on open map lets go.
  const selectedEntity = picked ? (selected?.entity.id ?? null) : null;
  useEffect(() => {
    setSelected(mode === 'overview' ? selectedEntity : (entityFilter ?? null));
  }, [mode, selectedEntity, entityFilter]);
  useEffect(() => () => clearFocus(), []);
  useEffect(() => {
    if (!map || mode !== 'overview') return;
    const onKey = (e: KeyboardEvent) => e.key === 'Escape' && setPicked(null);
    const onClick = (e: { point: { x: number; y: number } }) => {
      const hit = map.getLayer('ents-hit') ? map.queryRenderedFeatures([e.point.x, e.point.y], { layers: ['ents-hit'] }) : [];
      if (!hit.length) setPicked(null);
    };
    window.addEventListener('keydown', onKey);
    map.on('click', onClick);
    return () => {
      window.removeEventListener('keydown', onKey);
      map.off('click', onClick);
    };
  }, [map, mode]);
  const flyToEntity = (entityId: string) => {
    const at = placedEntities?.fc.features.find((f) => f.properties._id === entityId)?.geometry.coordinates as LonLat | undefined;
    if (map && at) moveTo(map, { center: at, zoom: Math.max(map.getZoom(), 2.6), padding: sheetPadding('left', 344) });
  };
  const rowHover = (entityId: string) => ({
    onMouseEnter: () => setHovered(entityId, 'list'),
    onMouseLeave: () => setHovered(null, 'list'),
    onFocus: () => setHovered(entityId, 'list'),
    onBlur: () => setHovered(null, 'list'),
  });
  const hl = (entityId: string) => (focus.hovered === entityId && focus.source === 'map' ? ' hl' : '');

  // A dot or pill picked on the map: its top story on the overview, its register otherwise.
  const pickEntity = (entityId: string) => {
    const row = items.find((r) => r.entity.id === entityId || r.story.entities.some((e) => e.id === entityId));
    if (mode === 'overview' && row) {
      setPicked(row.id);
      flyToEntity(entityId);
    } else navigate(`/register?entity=${entityId}`);
  };

  const byCategory = summary.data?.stories_by_category ?? summary.data?.by_category;
  const chips = CATEGORY_ORDER.filter((c) => (byCategory?.[c] ?? 0) > 0);
  // A story is one feed item or event, however many entities it reaches; a risk is one
  // entity × story. The headline counts stories, with risks second.
  const open = summary.data?.story_count ?? summary.data?.open_count ?? 0;
  const riskRows = summary.data?.open_count ?? open;
  const storyWord = summary.data?.story_count != null ? 'open story' : 'open risk';
  const countText = (n: number) => plural(Math.round(n), storyWord, storyWord === 'open story' ? 'open stories' : 'open risks');
  const secondary = summary.data?.story_count != null && riskRows !== open ? plural(riskRows, 'risk') : null;
  const noRisks = summary.isSuccess && open === 0;

  return (
    <>
      <LanesLayer lanes={lanes} selected={focus.selected} />
      <EntityMap data={placedEntities?.fc ?? null} dimmed={dimmed} onPick={pickEntity} />
      <Chokepoints laneCounts={lanes?.counts} />

      <aside className="sheet w344 glass" aria-label={mode === 'overview' ? 'Risks' : 'Risk register'}>
        <Grab />
        <div className="sec" style={{ paddingTop: 18 }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
            <span className="lbl">{mode === 'overview' ? 'Highest open score' : 'Risk register'}</span>
            <LiveDot status={stream} scoredAt={summary.data ? summary.data.last_scored_at : undefined} />
          </div>
          {summary.isPending ? (
            <div className="sk" style={{ height: 52, width: 140, marginTop: 6 }} />
          ) : summary.isError ? (
            <div style={{ marginTop: 8 }}><ErrorState error={summary.error} retry={() => void summary.refetch()} /></div>
          ) : mode === 'overview' ? (
            <div style={{ display: 'flex', alignItems: 'flex-end', justifyContent: 'space-between', gap: 12, marginTop: 6 }}>
              <div style={{ display: 'flex', alignItems: secondary ? 'flex-end' : 'baseline', gap: 8, minWidth: 0 }}>
                <CountUp className="figxl" value={summary.data.max_score ?? 0} />
                <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0, paddingBottom: secondary ? 7 : 0 }}>
                  <CountUp className="t13" style={{ fontWeight: 600, color: 'var(--ink)', whiteSpace: 'nowrap' }} value={open} format={countText} />
                  {secondary && <CountUp className="t12" style={{ whiteSpace: 'nowrap' }} title="One risk for each entity a story reaches" value={riskRows} format={(n) => plural(Math.round(n), 'risk')} />}
                </span>
              </div>
            </div>
          ) : (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 10, marginTop: 6 }}>
              <span style={{ display: 'flex', alignItems: 'baseline', gap: 8, minWidth: 0 }}>
                <CountUp className="h2" style={{ whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }} value={open} format={countText} />
                {secondary && <CountUp className="t13" style={{ flex: 'none' }} title="One risk for each entity a story reaches" value={riskRows} format={(n) => plural(Math.round(n), 'risk')} />}
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
                <button key={c} type="button" className={`chip${category === c ? ' on' : ''}`} aria-pressed={category === c} title={LEGEND[c]} onClick={() => setCategory(c)}>
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
            <div role="group" aria-label="Risks in this layer" className="rp-rows" ref={motion.ref}>
              {items.map((r) =>
                mode === 'overview' ? (
                  <button key={r.id} type="button" className={`it${selected?.id === r.id ? ' on' : ''}${hl(r.entity.id)}`} aria-pressed={selected?.id === r.id} onClick={() => { setPicked(r.id); flyToEntity(r.entity.id); }} {...rowHover(r.entity.id)} {...motion.row(r.id)} data-lvl={levelOf(r.score)}>
                    <CountPill score={r.score} />
                    <span style={{ minWidth: 0 }}><b>{r.title}</b><span className="pl">{RiskPlace(r)}</span></span>
                    <Icon name="chev" size={14} stroke={2} className="chev" />
                  </button>
                ) : (
                  <Link key={r.id} to={`/risks/${r.id}`} className={`it${hl(r.entity.id)}`} {...rowHover(r.entity.id)} {...motion.row(r.id)} data-lvl={levelOf(r.score)} onPointerEnter={() => prefetchRiskDetail(qc, ws, r.id)} onFocusCapture={() => prefetchRiskDetail(qc, ws, r.id)}>
                    <CountPill score={r.score} />
                    <span style={{ minWidth: 0 }}><b data-vt-title={r.id}>{r.title}</b><span className="pl">{CATEGORIES[r.category].name} · {RiskPlace(r)}</span></span>
                    <Icon name="chev" size={14} stroke={2} className="chev" />
                  </Link>
                ),
              )}
              {/* rows that just left the list, fading out where they were */}
              {motion.ghosts.map((g) => (
                <div key={`gone:${g.key}`} className="it rp-leave" style={g.style} aria-hidden="true" inert>
                  <CountPill score={g.item.score} />
                  <span style={{ minWidth: 0 }}><b>{g.item.title}</b><span className="pl">{RiskPlace(g.item)}</span></span>
                  <Icon name="chev" size={14} stroke={2} className="chev" />
                </div>
              ))}
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
              <span className="h3" data-vt-title={selected.id}>{selected.title}</span>
              {detail.isPending ? (
                <span className="sk" style={{ height: 36 }} />
              ) : detail.isError ? (
                <span className="t13">The explanation did not load.</span>
              ) : (
                <span className="t13" style={{ display: '-webkit-box', WebkitLineClamp: 2, WebkitBoxOrient: 'vertical', overflow: 'hidden' }}>{detail.data.explanation}</span>
              )}
            </div>
            {detail.data && detail.data.id === selected.id && detail.data.history.length >= 3 && (
              <Sparkline
                id={selected.id}
                values={detail.data.history.map((h) => h.score)}
                label={`Recent scores, ${Math.round(detail.data.history[Math.max(0, detail.data.history.length - 16)].score)} to ${Math.round(selected.score)}`}
              />
            )}
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

      <Legend sheet={344}>
        {broken.length ? (
          <>
            <span className="k" style={{ color: 'var(--ink)' }}><span className="rdot" />{broken[0]} stopped syncing</span>
            <Link to="/connect" className="b plain" style={{ height: 44, margin: '0 -10px 0 -6px', padding: '0 12px', fontSize: 13 }}>Fix</Link>
          </>
        ) : (
          <FeedStatus health={health.data} pending={health.isPending} failed={health.isError} />
        )}
      </Legend>
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
