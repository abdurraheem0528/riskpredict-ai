// design/project/Risk.dc.html, as redesigned for plain reading: one story at one place, why it
// reaches you, the score on the 0–100 scale with its move, the two numbers it is made of, the
// news behind it and what to do. Everything comes from GET /v1/risks/{id}; status changes are
// PATCH /v1/risks/{id}; the formula is one tap away ("···" → Show the working).
import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState, useSyncExternalStore, type RefObject } from 'react';
import { flushSync } from 'react-dom';
import type { FeatureCollection, Point } from 'geojson';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useDisruptions, useMapEntities, useRisk, useSeries, type RiskDetail, type S } from '../api/queries';
import { api, unwrap } from '../api/client';
import { wsh } from '../api/workspace';
import { errorText } from '../api/errors';
import { useSession, canEdit } from '../app/Session';
import { useToast } from '../app/Toast';
import { useDismiss } from '../app/TopBar';
import { navigate } from '../lib/router';
import { LEVELS, levelOf, fmtDate, fmtInt, feedName, scoreText } from '../lib/format';
import { headlineText, latestReports, receipt, shownReports, trendText, whySentence } from '../lib/story';
import { fitCounts, measureSheet, type Fit } from '../lib/fit';
import { placeOf, countryName, portName } from '../map/geo';
import { useMap, reducedMotion } from '../map/MapRoot';
import { sheetPadding, useFit, boxOfPoints, type LonLat } from '../map/camera';
import { DotsLayer, RiskPlacesLayer } from '../map/layers';
import { EntityMap } from '../map/EntityLayers';
import { Chokepoints, useChokepoints } from '../map/Chokepoints';
import { StoryPills, storyPills } from '../map/StoryPills';
import { LanesLayer } from '../map/LanesLayer';
import { useLanes } from '../map/lanes';
import { MapControls } from '../map/MapControls';
import { Legend } from '../map/Legend';
import { clearFocus, setSelected } from '../map/focus';
import { usePalette } from '../lib/theme';
import { usePlaced, useGazetteer } from '../map/data';
import { Icon } from '../ui/Icon';
import { ErrorState, SheetHead, SkeletonRows, Spark } from '../ui/parts';
import { ScoreScale } from '../ui/ScoreScale';
import { Grab } from '../ui/Grab';
import { Receipt, rowPlace } from '../ui/rows';

type Status = 'open' | 'acknowledged' | 'mitigating' | 'dismissed';
const STATUS_TEXT: Record<S['RiskStatus'], string> = {
  open: 'Open', acknowledged: 'Acknowledged', mitigating: 'Mitigating', resolved: 'Resolved', dismissed: 'Dismissed',
};

/** Advice text → steps: one per line, or per sentence when it is one paragraph. */
export function adviceSteps(text: string): string[] {
  const lines = text.split(/\n+/).map((l) => l.replace(/^\s*(?:[-*•]|\d+[.)])\s*/, '').trim()).filter(Boolean);
  if (lines.length > 1) return lines;
  return (text.match(/[^.!?]+[.!?]+(?=\s|$)|[^.!?]+$/g) ?? [text]).map((s) => s.trim()).filter(Boolean);
}

export function RiskView({ id }: { id: string }) {
  const { workspace, role } = useSession();
  const ws = workspace.id;
  const risk = useRisk(ws, id);
  const entities = useMapEntities(ws);
  const placedEntities = usePlaced(entities.data);
  const ready = useGazetteer();
  const { map } = useMap();

  const r = risk.data;
  // Where the trouble is: the chokepoint for a chokepoint match, else the risk's own place.
  const chokepoints = useChokepoints();
  const choke = r?.where?.match_type === 'chokepoint' ? (chokepoints.find((c) => c.id === r.where!.match_value) ?? null) : null;
  const where = useMemo(() => (r && ready ? (choke?.at ?? placeOf(r)) : null), [r, ready, choke]);
  const evidencePoints = useMemo<FeatureCollection<Point> | null>(() => {
    if (!r || !ready) return null;
    const features = r.evidence
      .map((e) => placeOf({ lat: e.snapshot.lat, lon: e.snapshot.lon, ports: e.snapshot.ports, countries: e.snapshot.countries }))
      .filter((p): p is [number, number] => !!p)
      .map((c) => ({ type: 'Feature' as const, properties: {}, geometry: { type: 'Point' as const, coordinates: c } }));
    return { type: 'FeatureCollection', features };
  }, [r, ready]);
  const hotspot = useMemo<FeatureCollection<Point, never> | null>(() => {
    if (!where || !r) return null;
    return { type: 'FeatureCollection', features: [{ type: 'Feature', properties: { _id: r.id, max_score: r.score } as never, geometry: { type: 'Point', coordinates: where } }] };
  }, [where, r]);

  // The affected entity (and the others the same story reaches) stay bright, selected: its
  // ring and lanes, and the story's pill at the place the trouble is. The rest is dimmed.
  const P = usePalette();
  const entityId = r?.entity.id ?? null;
  const disruptionsFc = useDisruptions();
  const lanes = useLanes(entities.data, disruptionsFc.data);
  const bright = useMemo(() => (r ? new Set([r.entity.id, ...r.story.entities.map((e) => e.id)]) : null), [r]);
  useEffect(() => {
    setSelected(entityId);
  }, [entityId]);
  useEffect(() => () => clearFocus(), []);

  const entityAt = (placedEntities?.fc.features.find((f) => f.properties._id === entityId)?.geometry.coordinates as LonLat | undefined) ?? null;
  const box = useMemo(() => {
    if (!where) return null;
    return boxOfPoints([where, ...(entityAt ? [entityAt] : []), ...(evidencePoints?.features.map((f) => f.geometry.coordinates as LonLat) ?? [])]);
  }, [where, entityAt, evidencePoints]);
  useFit(map, box, () => sheetPadding('left', 440), 4);
  const pills = useMemo(() => {
    if (!r) return [];
    const placed = new Map((placedEntities?.fc.features ?? []).map((f) => [f.properties._id, { at: f.geometry.coordinates as LonLat, props: f.properties }]));
    return storyPills([r], chokepoints, placed);
  }, [r, chokepoints, placedEntities]);
  const unlabelled = useMemo(() => new Set(pills.flatMap((p) => (p.chokepoint ? [p.chokepoint] : []))), [pills]);

  return (
    <>
      <LanesLayer lanes={lanes} selected={entityId} />
      <EntityMap data={placedEntities?.fc ?? null} dimmed={bright} heat={false} pills={false} />
      <Chokepoints laneCounts={lanes?.counts} unlabelled={unlabelled} />
      <StoryPills pills={pills} />
      <DotsLayer id="evidence" data={evidencePoints} color={P.signal} radius={3.5} />
      <RiskPlacesLayer data={hotspot} />

      <aside className="sheet w440 split glass" aria-label={r ? `Risk: ${rowPlace(r)}` : 'Risk'}>
        <Grab />
        {risk.isPending ? (
          <div className="sbody">
            <SheetHead back={{ to: '/register', label: 'Risks', onClick: () => navigate('/register') }} />
            <SkeletonRows n={6} h={40} />
          </div>
        ) : risk.isError ? (
          <div className="sbody">
            <SheetHead back={{ to: '/register', label: 'Risks', onClick: () => navigate('/register') }} />
            <ErrorState
              error={risk.error}
              title={(risk.error as { status?: number }).status === 404 ? 'This risk is not in this workspace' : 'The risk did not load'}
              retry={() => void risk.refetch()}
            />
          </div>
        ) : (
          <RiskBody r={risk.data} editor={canEdit(role)} />
        )}
      </aside>

      <MapControls fit={box} fitLabel="Fit this risk" sheet={440} />
      <Legend sheet={440} marks={['signal', 'lane', 'choke']} />
    </>
  );
}

function RiskBody({ r, editor }: { r: RiskDetail; editor: boolean }) {
  const { workspace } = useSession();
  const qc = useQueryClient();
  const toast = useToast();
  const [menu, setMenu] = useState(false);
  const [working, setWorking] = useState(false);
  const [allNews, setAllNews] = useState(false);
  const [allSteps, setAllSteps] = useState(false);
  const ref = useDismiss(menu, () => setMenu(false));
  useGazetteer(); // country names in the place phrase and the sentences

  const setStatus = useMutation({
    mutationFn: (status: Status) =>
      unwrap(api.PATCH('/v1/risks/{risk_id}', { params: { header: wsh(workspace.id), path: { risk_id: r.id } }, body: { status } })),
    onSuccess: (_d, status) => {
      const prev = r.status;
      void qc.invalidateQueries({ queryKey: [workspace.id] });
      toast({
        text: `Marked ${STATUS_TEXT[status].toLowerCase()}`,
        action: prev !== 'resolved' ? { label: 'Undo', run: () => setStatus.mutate(prev as Status) } : undefined,
      });
    },
    onError: (e) => toast({ text: errorText(e) }),
  });

  const anomaly = r.evidence.find((e) => e.kind === 'anomaly')?.anomaly ?? null;
  const series = useSeries(anomaly?.series ?? null, anomaly?.key ?? null);
  const steps = r.advice ? adviceSteps(r.advice.text) : [];
  const closed = r.status === 'resolved' || r.status === 'dismissed';
  const next: Status = r.status === 'open' ? 'acknowledged' : r.status === 'acknowledged' ? 'mitigating' : 'open';
  const nextLabel = { acknowledged: 'Acknowledge', mitigating: 'Mark mitigating', open: 'Reopen', dismissed: 'Dismiss' }[next];

  const why = whySentence(r, countryName);
  const trend = trendText(r.history, r.score);
  const lvl = levelOf(r.score);
  const news = latestReports(r);
  const others = r.story.entities.filter((e) => e.id !== r.entity.id);
  const workingRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (working && workingRef.current) revealInSheet(workingRef.current);
  }, [working]);

  // Where the sheet has a fixed height, the page shows fewer steps (then fewer news items) rather
  // than scroll; the rest are a tap away ("2 more steps", "All 3"). In the narrow layout, all show.
  const bodyRef = useRef<HTMLDivElement>(null);
  const stepsRef = useRef<HTMLUListElement>(null);
  const stepsId = useId();
  const candidates = shownReports(news); // the two news items shown by default
  const stepKeys = steps.map((_, i) => `step:${i}`);
  const newsKeys = candidates.map((e) => `news:${e.evidence_key}`);
  const content = [r.id, ...steps, ...candidates.map((e) => `${e.evidence_key} ${headlineText(e)}`)].join('\n');
  const fit = useSheetFit(bodyRef, content, stepKeys, newsKeys);
  const held = fit ? steps.length - fit.steps : 0;
  const shownSteps = allSteps || !held ? steps : steps.slice(0, fit!.steps);
  const fitNews = shownReports(news, fit ? fit.news : 2);
  const shownNews = allNews ? news : fitNews;
  useEffect(() => {
    // the whole "What to do" block, so "Fewer" is in view too
    const block = stepsRef.current?.parentElement;
    if (allSteps && block) revealInSheet(block);
  }, [allSteps]);

  return (
    <>
      <div className="sbody rbody" ref={bodyRef}>
      <SheetHead
        back={{ to: '/register', label: 'Risks', onClick: () => (window.history.length > 1 ? window.history.back() : navigate('/register')) }}
        right={
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, position: 'relative' }} ref={ref}>
            <span className="t13">{STATUS_TEXT[r.status]}</span>
            {(r.factors.explain || (editor && r.status !== 'resolved')) && (
              <button type="button" className="ib" aria-label="More actions" aria-expanded={menu} aria-haspopup="true" onClick={() => setMenu((m) => !m)}>
                <Icon name="more" size={20} />
              </button>
            )}
            {menu && (
              <div className="menu r" role="menu" style={{ top: 48, width: 220 }}>
                {r.factors.explain && (
                  <button type="button" role="menuitem" className="mi" onClick={() => { setMenu(false); setWorking((w) => !w); }}>
                    {working ? 'Hide the working' : 'Show the working'}
                  </button>
                )}
                {editor && r.status !== 'resolved' && (
                  <>
                    {r.factors.explain && <span className="mdiv" />}
                    {(['open', 'acknowledged', 'mitigating'] as Status[]).filter((s) => s !== r.status).map((s) => (
                      <button key={s} type="button" role="menuitem" className="mi" onClick={() => { setMenu(false); setStatus.mutate(s); }}>
                        {s === 'open' ? 'Reopen' : s === 'acknowledged' ? 'Acknowledge' : 'Mark mitigating'}
                      </button>
                    ))}
                    {r.status !== 'dismissed' && (
                      <>
                        <span className="mdiv" />
                        <button type="button" role="menuitem" className="mi red" onClick={() => { setMenu(false); setStatus.mutate('dismissed'); }}>Dismiss</button>
                      </>
                    )}
                  </>
                )}
              </div>
            )}
          </div>
        }
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: -8 }}>
        <h1 className="h2" data-vt-title={r.id}>{rowPlace(r)}</h1>
        <span className="t14">{why.text}</span>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
          <span className="figxl">{scoreText(r.score)}</span>
          <span className={`tx-${lvl}`} style={{ fontSize: 17, lineHeight: '22px', fontWeight: 600 }}>{LEVELS[lvl].name}</span>
          {trend && (
            <span className="trend" style={{ marginLeft: 'auto' }}>
              <Icon name={trend.dir} size={14} stroke={2} aria-hidden="true" />
              {trend.text}
            </span>
          )}
        </div>
        <ScoreScale score={r.score} />
      </div>

      <Receipt rows={receipt(r, countryName)} large />

      {working && r.factors.explain && (
        <div ref={workingRef} data-fit="skip">
          <HowScored explain={r.factors.explain} />
        </div>
      )}

      {news.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
          <div className="shd">
            <h2 style={{ margin: 0, font: 'inherit', fontSize: 15, lineHeight: '20px', fontWeight: 600 }}>The news</h2>
            {news.length > fitNews.length && (
              <button type="button" className="more" aria-expanded={allNews} onClick={() => setAllNews((a) => !a)}>
                {allNews ? 'Fewer' : `All ${news.length}`}
              </button>
            )}
          </div>
          <div className="rnews" data-fit="list" data-fit-list="news">
            {shownNews.map((e) => {
              // At most two lines, cut with an ellipsis; the whole headline is the link's name and its tooltip.
              const text = headlineText(e);
              return (
                <div className="ni" key={e.evidence_key} data-fit-key={`news:${e.evidence_key}`}>
                  <span className="src">{[e.event ? feedName(e.event.source) : null, e.snapshot.published_at ? fmtDate(e.snapshot.published_at) : null].filter(Boolean).join(', ')}</span>
                  {e.snapshot.url ? (
                    <a className="clamp2" href={e.snapshot.url} target="_blank" rel="noopener noreferrer" title={text}>
                      {text}
                      <span className="vh"> (opens the source)</span>
                    </a>
                  ) : (
                    <b className="clamp2" title={text}>{text}</b>
                  )}
                </div>
              );
            })}
          </div>
        </div>
      )}

      <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
        <div className="shd">
          <h2 style={{ margin: 0, font: 'inherit', fontSize: 15, lineHeight: '20px', fontWeight: 600 }}>What to do</h2>
          {r.advice && <span className="t12">{r.advice.source === 'groq' ? 'Written by AI from these reports' : 'From the playbook'}</span>}
        </div>
        {steps.length ? (
          <ul className="rsteps" id={stepsId} ref={stepsRef} data-fit="list" data-fit-list="steps">
            {shownSteps.map((s, i) => (
              <li key={i} data-fit-key={`step:${i}`}>{s}</li>
            ))}
          </ul>
        ) : (
          <span className="t13">Advice is written after the next scoring run.</span>
        )}
        {held > 0 && (
          <button type="button" className="more under" data-fit="toggle" aria-expanded={allSteps} aria-controls={stepsId} onClick={() => setAllSteps((a) => !a)}>
            {allSteps ? 'Fewer' : `${held} more ${held === 1 ? 'step' : 'steps'}`}
          </button>
        )}
      </div>

      {!why.named && others.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span className="lbl">The same story also reaches</span>
          <span className="t13">
            {others.map((e) => e.name).join(', ')}
            {(r.story.entity_count ?? r.story.risk_count) > r.story.entities.length ? ` and ${fmtInt((r.story.entity_count ?? r.story.risk_count) - r.story.entities.length)} more` : ''}
          </span>
        </div>
      )}

      {anomaly && series.data && series.data.points.length > 1 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span className="lbl">{series.data.key}, {series.data.series}{series.data.unit ? ` (${series.data.unit})` : ''}</span>
          <Spark
            values={series.data.points.map((p) => p.value)}
            domain={minmax(series.data.points.map((p) => p.value))}
            ref60={false}
            label={`${series.data.key} over ${series.data.points.length} points`}
          />
        </div>
      )}

      </div>
      <div className="sfoot">
      <div style={{ display: 'flex', gap: 8 }}>
        <button type="button" className="b pri" style={{ flex: 1 }} onClick={() => navigate(`/scenarios?risk=${r.id}`)}>Run what-if</button>
        {editor && !closed && (
          <button type="button" className="b sec2" onClick={() => setStatus.mutate(next)} disabled={setStatus.isPending}>
            {setStatus.isPending && <span className="spin" />}
            {nextLabel}
          </button>
        )}
        {editor && r.status === 'dismissed' && (
          <button type="button" className="b sec2" onClick={() => setStatus.mutate('open')} disabled={setStatus.isPending}>Reopen</button>
        )}
      </div>
      </div>
    </>
  );
}

// The narrow layout's sheet is a bottom sheet that scrolls by design (styles.css, ≤ 760 px).
const NARROW = '(max-width: 760px)';
function useNarrow(): boolean {
  return useSyncExternalStore(
    (l) => {
      const q = window.matchMedia?.(NARROW);
      q?.addEventListener?.('change', l);
      return () => q?.removeEventListener?.('change', l);
    },
    () => !!window.matchMedia?.(NARROW).matches,
    () => false,
  );
}

/** The toggle's space before it has been shown once: the gap above it (8) and the button (32, less its two −6 margins). */
const TOGGLE_SPACE = 28;
/** Fewer is lower: steps go first, then news items. */
const rank = (f: Fit) => f.news * 1000 + f.steps;

/**
 * How many advice steps and news items the risk page shows (lib/fit.ts): null shows them all, in
 * the narrow layout and while every one is measured. `content` names what the page shows (a new
 * one is measured again); `stepKeys` and `newsKeys` are the `data-fit-key`s of the steps and of
 * the news items shown by default. It measures after each render, before paint, and again when
 * the sheet body is resized or fonts finish loading (flushSync: no frame shows the in-between).
 * Each height it measures is kept, so held-back items need not be shown again to be counted.
 */
function useSheetFit(body: RefObject<HTMLDivElement | null>, content: string, stepKeys: readonly string[], newsKeys: readonly string[]): Fit | null {
  const narrow = useNarrow();
  const [fit, setFit] = useState<Fit | null>(null);
  const memo = useRef({ epoch: '', heights: new Map<string, number>(), toggle: null as number | null, settled: '' });
  const fonts = useRef(0);
  const run = useRef<() => void>(() => {});
  // after each render, before paint; `run` is also what a resize or the fonts call
  useLayoutEffect(() => {
    run.current = measure;
    measure();
  });
  function measure() {
    const m = !narrow && body.current ? measureSheet(body.current) : null;
    if (!m) return setFit(null);
    const c = memo.current;
    const epoch = `${content}|${m.width}|${fonts.current}`;
    if (c.epoch !== epoch) Object.assign(c, { epoch, heights: new Map(), settled: '' });
    for (const it of m.items) c.heights.set(it.key, it.height);
    if (m.toggle != null) c.toggle = m.toggle;
    const steps = stepKeys.map((k) => c.heights.get(k));
    const news = newsKeys.map((k) => c.heights.get(k));
    // one not measured yet: show every one, and measure again after that render
    if (steps.includes(undefined) || news.includes(undefined)) return setFit(null);
    let next = fitCounts({
      available: m.available,
      fixed: m.content - m.lists - (m.toggle ?? 0) - m.skip,
      steps: steps as number[],
      stepGap: m.gaps.steps ?? 0,
      news: news as number[],
      newsGap: m.gaps.news ?? 0,
      toggle: c.toggle ?? TOGGLE_SPACE,
    });
    // With the same heights to go on, the counts only go down: they never swing back and forth.
    const settled = `${epoch}|${m.available}|${c.toggle}`;
    if (fit && c.settled === settled && rank(next) > rank(fit)) next = fit;
    c.settled = settled;
    if (!fit || fit.steps !== next.steps || fit.news !== next.news || fit.fits !== next.fits) setFit(next);
  }
  useEffect(() => {
    const el = body.current;
    if (!el) return;
    const again = () => flushSync(() => run.current());
    // the border box: a scrollbar coming or going does not count as a resize
    const ro = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(again);
    ro?.observe(el, { box: 'border-box' });
    const f = typeof document !== 'undefined' ? document.fonts : undefined;
    let live = true;
    const refont = () => {
      if (!live) return;
      fonts.current++;
      again();
    };
    f?.addEventListener?.('loadingdone', refont);
    void f?.ready?.then(refont);
    return () => {
      live = false;
      ro?.disconnect();
      f?.removeEventListener?.('loadingdone', refont);
    };
  }, [body]);
  return fit;
}

/** The numbers behind the score (factors.explain, engine 1.2.0), opened in place from the "···" menu. */
function HowScored({ explain }: { explain: NonNullable<RiskDetail['factors']['explain']> }) {
  const m = explain.matched;
  const via = m.inferred_from ? inferredText(m.inferred_from) : null;
  return (
    <section className="how" aria-label="How this was scored">
      <h2 className="lbl" style={{ margin: 0, padding: '12px 0 4px' }}>How this was scored</h2>
      <div className="step">
        <span className="t12">Matched on</span>
        <span>
          {m.label}
          {m.matched_text ? ` (“${m.matched_text}”)` : ''}
          {via ? `, ${via}` : ''}
          {m.supported_by ? `, with ${m.supported_by.matched_text ?? m.supported_by.match_value} (${m.supported_by.match_value})` : ''}
        </span>
      </div>
      {explain.steps.map((s) => (
        <div className="step" key={s.label}>
          <span className="t12">{s.label}</span>
          <span className="tnum">{s.formula}</span>
        </div>
      ))}
    </section>
  );
}

/**
 * Scrolls the sheet body so an opened section is in view above the sticky action bar:
 * all of it when it fits, else its top. Only the sheet scrolls, never the page.
 */
export function revealInSheet(el: HTMLElement) {
  const box = el.closest('.sbody');
  if (!box) return;
  // After layout, so the opened content has its height.
  requestAnimationFrame(() => {
    const b = box.getBoundingClientRect();
    const r = el.getBoundingClientRect();
    const fade = 28; // the action bar's fade (.sfoot::before) plus a little air
    let dy = 0;
    if (r.bottom > b.bottom - fade) dy = r.bottom - (b.bottom - fade);
    if (r.top - dy < b.top + 8) dy = r.top - (b.top + 8);
    if (dy > 0) box.scrollBy({ top: dy, behavior: reducedMotion() ? 'auto' : 'smooth' });
  });
}

function inferredText(via: string) {
  if (via.startsWith('port ')) return `inferred from the port ${portName(via.slice(5))}`;
  if (via.startsWith('country ')) return `inferred from ${countryName(via.slice(8))}`;
  return 'inferred from the location';
}

function minmax(v: number[]): [number, number] {
  const lo = Math.min(...v), hi = Math.max(...v);
  const pad = (hi - lo) * 0.1 || 1;
  return [lo - pad, hi + pad];
}
