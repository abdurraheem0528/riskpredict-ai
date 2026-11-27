// design/project/Main.dc.html (Overview) and the Risks list (System-Nav, phone), as redesigned
// for plain reading: each row is one story, listed once by place ("Shipping through Bab
// el-Mandeb"), with the real headline of its strongest report, what it reaches, and its score
// on the 0–100 scale. The map marks each story on screen at the place the trouble is.
import { useEffect, useMemo, useState, type ReactNode } from 'react';
import type { FeatureCollection, Point } from 'geojson';
import { prefetchRiskDetail, useDisruptions, useMapEntities, useMapEvents, useRisk, useRiskSummary, useRisks, useFeedHealth, type Risk, type RiskSort } from '../api/queries';
import { useQueryClient } from '@tanstack/react-query';
import { useSession } from '../app/Session';
import { useDismiss, useWorkspaceState } from '../app/TopBar';
import { Link, navigate, useLocation } from '../lib/router';
import { CATEGORIES, CATEGORY_ORDER, LEVELS, fmtInt, levelOf, plural, scoreText, type RiskCategory } from '../lib/format';
import { Receipt, rowHeadline, rowPlace } from '../ui/rows';
import { levelsLine, reachLine, receipt } from '../lib/story';
import { countryName } from '../map/geo';
import { useMap } from '../map/MapRoot';
import { fitTo, moveTo, sheetPadding, useIdleRotation, useOpeningMove, type LonLat } from '../map/camera';
import { EntityMap } from '../map/EntityLayers';
import { Chokepoints, useChokepoints } from '../map/Chokepoints';
import { StoryPills, storyPills } from '../map/StoryPills';
import { LanesLayer } from '../map/LanesLayer';
import { useLanes } from '../map/lanes';
import { MapControls } from '../map/MapControls';
import { Legend } from '../map/Legend';
import { clearFocus, setHovered, setSelected, useFocus } from '../map/focus';
import { useGazetteer, usePlaced } from '../map/data';
import { Icon } from '../ui/Icon';
import { ErrorState, FeedStatus, SkeletonRows } from '../ui/parts';
import { ScoreScale, ScoreStrip } from '../ui/ScoreScale';
import { CountUp } from '../live/CountUp';
import { LiveLine } from '../live/LiveDot';
import { useListMotion } from '../live/presence';
import { FeedDots } from '../live/FeedDots';
import { Grab } from '../ui/Grab';

const LEGEND: Partial<Record<RiskCategory, string>> = {
  geopolitical: 'Conflict and tension near your entities and routes',
  logistics: 'Choke points and ports on your routes',
  natural_hazard: 'Storms, floods and quakes near your entities',
  sanctions: 'Entities that match a sanctions list',
  financial: 'Currency and financial stress where you pay or sell',
  cyber: 'Your software, checked against known exploited flaws',
};

/** One story: the place, the lead headline (two lines at most), what it reaches; the score and its mini scale. */
function RowBody({ r, vt }: { r: Risk; vt?: boolean }) {
  return (
    <>
      <span className="rt">
        <span className="rp" data-vt-title={vt ? r.id : undefined}>{rowPlace(r)}</span>
        <span className="rh">{rowHeadline(r)}</span>
        <span className="rr">{reachLine(r)}</span>
      </span>
      <span className="rs">
        <CountUp value={r.score} format={(n) => scoreText(n)} className="tnum" style={{ fontSize: 17, lineHeight: '20px', fontWeight: 600 }} />
        <ScoreScale score={r.score} size="mini" />
      </span>
    </>
  );
}

/** The sort control: "Highest first" and a chevron, opening the two choices. */
function SortMenu({ sort, setSort }: { sort: RiskSort; setSort: (s: RiskSort) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useDismiss(open, () => setOpen(false));
  const label = (s: RiskSort) => (s === '-score' ? 'Highest first' : 'Newest first');
  return (
    <div ref={ref} style={{ position: 'relative', marginRight: -10 }}>
      <button type="button" className="b plain" style={{ height: 44, padding: '0 10px', fontSize: 13, gap: 4 }} aria-haspopup="true" aria-expanded={open} onClick={() => setOpen((o) => !o)}>
        {label(sort)}
        <Icon name="expand" size={16} stroke={2} />
      </button>
      {open && (
        <div className="menu r" role="menu" aria-label="Sort" style={{ top: 46, width: 200 }}>
          {(['-score', '-last_seen_at'] as RiskSort[]).map((s) => (
            <button key={s} type="button" role="menuitemradio" aria-checked={sort === s} className={`mi${sort === s ? ' on' : ''}`} onClick={() => { setSort(s); setOpen(false); }}>
              {label(s)}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

export function Main({ mode }: { mode: 'overview' | 'register' }) {
  const { workspace, stream } = useSession();
  const ws = workspace.id;
  const { query } = useLocation();
  const entityFilter = mode === 'register' ? query.get('entity') : null;
  const [category, setCategory] = useState<RiskCategory | null>(null);
  const [sort, setSort] = useState<RiskSort>('-score');
  const [picked, setPicked] = useState<string | null>(null);
  useGazetteer(); // country names in the place phrases

  const summary = useRiskSummary(ws);
  // One row per story (a category at a place): a report that reaches ten suppliers is one row.
  const risks = useRisks(ws, { category: mode === 'overview' ? null : category, sort: mode === 'overview' ? '-score' : sort, limit: mode === 'overview' ? 3 : 50, entityId: entityFilter, group: entityFilter ? null : 'story' });
  const entities = useMapEntities(ws);
  const health = useFeedHealth();
  const { broken } = useWorkspaceState();

  const items = useMemo(() => risks.data?.pages.flatMap((p) => p.items) ?? [], [risks.data]);
  // After a live update: rows slide to their new place, enter, leave, and the changed one flashes.
  // A row under the pointer loads its risk, so the place can morph into the risk page's title.
  const qc = useQueryClient();
  const motion = useListMotion(risks.data ? items : undefined, (r) => r.id, (r) => [r.id, r.story.key]);
  const selected = mode === 'overview' ? (items.find((r) => r.id === picked) ?? items[0] ?? null) : null;
  const detail = useRisk(ws, selected?.id ?? null);

  const placedEntities = usePlaced(entities.data);
  // Feed signals that matched nothing of yours: quiet grey dots that fade with age.
  const events = useMapEvents();
  const placedEvents = usePlaced(events.data);

  // The one entity the list is filtered to: its name, and its point on the map.
  const focusFeature = entityFilter ? (entities.data?.features.find((f) => f.id === entityFilter) ?? null) : null;
  const focusName = focusFeature?.properties.name ?? (entityFilter ? items.find((r) => r.entity.id === entityFilter)?.entity.name : null) ?? null;
  const focusPoint = useMemo(
    () => (entityFilter ? ((placedEntities?.fc.features.find((f) => f.properties._id === entityFilter)?.geometry.coordinates as LonLat | undefined) ?? null) : null),
    [entityFilter, placedEntities],
  );

  // Dimming: with a category chosen, the entities its stories reach stay bright; on the list
  // for one entity, only that entity does.
  const dimmed = useMemo<Set<string> | null>(() => {
    if (entityFilter) return focusPoint ? new Set([entityFilter]) : null;
    if (!category || !risks.data || mode === 'overview') return null;
    return new Set(items.flatMap((r) => [r.entity.id, ...r.story.entities.map((e) => e.id)]));
  }, [entityFilter, focusPoint, category, risks.data, items, mode]);

  // The camera: the opening move to the overview (globe: the densest group), or the one entity
  // the list is filtered to; idle rotation on the overview.
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

  // Linked hover and selection: a row, its story's pill, and its entity's dot and lanes.
  const focus = useFocus();
  // Sea lanes of the declared routes, and how many pass each chokepoint.
  const disruptions = useDisruptions();
  const lanes = useLanes(entities.data, disruptions.data);
  // Only a pick selects on the map (the open row shows the top story either way, and nothing
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

  // A pill per story on screen (the Overview's three rows, the list's first five, and a hovered
  // or selected one further down), at the chokepoint or at the reached entity.
  const chokepoints = useChokepoints();
  const placedById = useMemo(
    () => new Map((placedEntities?.fc.features ?? []).map((f) => [f.properties._id, { at: f.geometry.coordinates as LonLat, props: f.properties }])),
    [placedEntities],
  );
  const reaches = (r: Risk, id: string | null) => !!id && (r.entity.id === id || r.story.entities.some((e) => e.id === id));
  const pilled = useMemo(() => {
    const top = items.slice(0, mode === 'overview' ? 3 : 5);
    const more = items.filter((r) => !top.includes(r) && (reaches(r, focus.hovered) || reaches(r, focus.selected))).slice(0, 2);
    return storyPills([...top, ...more], chokepoints, placedById);
  }, [items, mode, chokepoints, placedById, focus.hovered, focus.selected]);
  const unlabelled = useMemo(() => new Set(pilled.flatMap((p) => (p.chokepoint ? [p.chokepoint] : []))), [pilled]);

  const flyTo = (r: Risk) => {
    const at = pilled.find((p) => p.id === r.id)?.at ?? placedById.get(r.entity.id)?.at;
    if (map && at) moveTo(map, { center: at, zoom: Math.max(map.getZoom(), 2.6), padding: sheetPadding('left', 344) });
  };
  const rowHover = (entityId: string) => ({
    onMouseEnter: () => setHovered(entityId, 'list'),
    onMouseLeave: () => setHovered(null, 'list'),
    onFocus: () => setHovered(entityId, 'list'),
    onBlur: () => setHovered(null, 'list'),
  });
  const hl = (r: Risk) => (focus.source === 'map' && reaches(r, focus.hovered) ? ' hl' : '');

  // A dot or pill picked on the map: its story on the overview, the list for that entity otherwise.
  const pickEntity = (entityId: string) => {
    const row = items.find((r) => reaches(r, entityId));
    if (mode === 'overview' && row) {
      setPicked(row.id);
      flyTo(row);
    } else navigate(`/register?entity=${entityId}`);
  };

  const byCategory = summary.data?.stories_by_category ?? summary.data?.by_category;
  const chips = CATEGORY_ORDER.filter((c) => (byCategory?.[c] ?? 0) > 0);
  // A story is one category at one place, however many entities it reaches; the headline counts them.
  const open = summary.data?.story_count ?? summary.data?.open_count ?? 0;
  const noRisks = summary.isSuccess && open === 0;
  const scores = summary.data?.story_scores ?? (mode === 'overview' ? summary.data?.top.map((r) => r.score) : items.map((r) => r.score)) ?? [];
  const levels = levelsLine(scores, (s) => LEVELS[levelOf(s)].name);

  let header: ReactNode;
  if (summary.isPending) header = <div className="sk" style={{ height: 52, width: 220 }} />;
  else if (summary.isError) header = <ErrorState error={summary.error} retry={() => void summary.refetch()} />;
  else if (mode === 'overview')
    header = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
        <h1 className="h2">
          <CountUp value={open} format={(n) => (Math.round(n) === 0 ? 'No risks touch your business' : `${plural(Math.round(n), 'risk')} ${Math.round(n) === 1 ? 'touches' : 'touch'} your business`)} />
        </h1>
        <LiveLine status={stream} scoredAt={summary.data.last_scored_at} lead={levels ? `${levels}.` : ''} />
      </div>
    );
  else
    header = (
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', gap: 12 }}>
          <h1 className="h1">Risks</h1>
          {!entityFilter && <SortMenu sort={sort} setSort={setSort} />}
        </div>
        <LiveLine status={stream} scoredAt={summary.data.last_scored_at} lead={open ? `${fmtInt(open)} ${open === 1 ? 'touches' : 'touch'} your business.` : 'Nothing touches your business yet.'} />
      </div>
    );

  return (
    <>
      <FeedDots data={placedEvents?.fc ?? null} />
      <LanesLayer lanes={lanes} selected={focus.selected} />
      <EntityMap data={placedEntities?.fc ?? null} dimmed={dimmed} pills={false} onPick={pickEntity} />
      <Chokepoints laneCounts={lanes?.counts} unlabelled={unlabelled} />
      <StoryPills pills={pilled} onPick={pickEntity} />

      <aside className="sheet w344 glass" aria-label="Risks">
        <Grab />
        <div className="sec" style={{ paddingTop: 18, paddingBottom: mode === 'register' && chips.length && !entityFilter ? 6 : 14 }}>
          {header}
          {mode === 'register' && !entityFilter && chips.length > 0 && (
            <div className="chiprow" role="group" aria-label="Category" style={{ marginTop: 10 }}>
              <button type="button" className={`chip${category === null ? ' on' : ''}`} aria-pressed={category === null} onClick={() => setCategory(null)}>
                All<small>{fmtInt(open)}</small>
              </button>
              {chips.map((c) => (
                <button key={c} type="button" className={`chip${category === c ? ' on' : ''}`} aria-pressed={category === c} title={LEGEND[c]} onClick={() => setCategory(c)}>
                  {CATEGORIES[c].name}
                  <small>{fmtInt(byCategory?.[c] ?? 0)}</small>
                </button>
              ))}
            </div>
          )}
        </div>

        {mode === 'overview' && scores.length > 0 && (
          <div className="sec" style={{ paddingTop: 10, paddingBottom: 14 }}>
            <ScoreStrip scores={scores} />
          </div>
        )}

        <div className="sec" style={{ paddingTop: 8, paddingBottom: 8 }}>
          {entityFilter && (
            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 2 }}>
              <span className="lbl" style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={focusName ?? undefined}>
                {focusName ?? 'One entity'}
              </span>
              <Link to="/register" className="b plain" style={{ height: 44, padding: '0 10px', fontSize: 13 }}>Show all</Link>
            </div>
          )}
          {risks.isPending ? (
            <SkeletonRows n={3} h={72} />
          ) : risks.isError ? (
            <ErrorState error={risks.error} retry={() => void risks.refetch()} />
          ) : items.length === 0 ? (
            <div style={{ display: 'flex', flexDirection: 'column', gap: 4, padding: '6px 0' }}>
              <span className="h3" style={{ fontSize: 15 }}>{noRisks ? 'No risks touch you yet' : 'Nothing in this category'}</span>
              <span className="t13">
                {noRisks
                  ? `Every feed is checked against your ${plural(entities.data?.features.length ?? 0, 'entity', 'entities')}. A risk appears here the moment one matches.`
                  : 'Pick another category, or All.'}
              </span>
            </div>
          ) : (
            <div role="group" aria-label={mode === 'overview' ? 'Top risks' : 'Risks in this category'} className="rp-rows" ref={motion.ref}>
              {items.map((r) =>
                mode === 'overview' ? (
                  <div key={r.id} {...motion.row(r.id)}>
                    <button
                      type="button"
                      className={`rrow${selected?.id === r.id ? ' on' : ''}${hl(r)}`}
                      aria-expanded={selected?.id === r.id}
                      onClick={() => { setPicked(r.id); flyTo(r); }}
                      {...rowHover(r.entity.id)}
                      data-lvl={levelOf(r.score)}
                    >
                      <RowBody r={r} vt={selected?.id === r.id} />
                    </button>
                    {selected?.id === r.id && <OpenRow r={r} detail={detail} />}
                  </div>
                ) : (
                  <Link key={r.id} to={`/risks/${r.id}`} className={`rrow${hl(r)}`} {...rowHover(r.entity.id)} {...motion.row(r.id)} data-lvl={levelOf(r.score)} onPointerEnter={() => prefetchRiskDetail(qc, ws, r.id)} onFocusCapture={() => prefetchRiskDetail(qc, ws, r.id)}>
                    <RowBody r={r} vt />
                  </Link>
                ),
              )}
              {/* rows that just left the list, fading out where they were */}
              {motion.ghosts.map((g) => (
                <div key={`gone:${g.key}`} className="rrow rp-leave" style={g.style} aria-hidden="true" inert>
                  <RowBody r={g.item} />
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

        {mode === 'overview' && open > 0 && (
          <div className="sec" style={{ paddingTop: 4, paddingBottom: 12 }}>
            <Link to="/register" className="b plain" style={{ height: 44, padding: '0 10px', margin: '0 -10px', fontSize: 14 }}>
              All {fmtInt(open)} {open === 1 ? 'risk' : 'risks'}
              <Icon name="chev" size={14} stroke={2} />
            </Link>
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

/** The Overview's open row: the two-row receipt, then Open risk and What if. */
function OpenRow({ r, detail }: { r: Risk; detail: ReturnType<typeof useRisk> }) {
  const d = detail.data && detail.data.id === r.id ? detail.data : null;
  return (
    <div className="ropen">
      {d ? (
        <Receipt rows={receipt(d, countryName)} />
      ) : detail.isError ? (
        <span className="t13">The numbers did not load.</span>
      ) : (
        <span className="sk" style={{ height: 76 }} />
      )}
      <div style={{ display: 'flex', gap: 8 }}>
        <Link to={`/risks/${r.id}`} className="b pri" style={{ flex: 1 }}>Open risk</Link>
        <Link to={`/scenarios?risk=${r.id}`} className="b sec2">What if</Link>
      </div>
    </div>
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
