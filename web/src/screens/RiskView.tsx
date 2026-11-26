// design/project/Risk.dc.html — one risk with its score history, evidence and advice.
// Everything comes from GET /v1/risks/{id}; status changes are PATCH /v1/risks/{id}.
import { useEffect, useMemo, useState } from 'react';
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
import { CATEGORIES, KINDS, LEVELS, levelOf, ago, fmtDate, feedName, outOf5, plural } from '../lib/format';
import { placeOf, countryName, portName } from '../map/geo';
import { useMap, reducedMotion } from '../map/MapRoot';
import { sheetPadding, useFit, boxOfPoints, type LonLat } from '../map/camera';
import { DotsLayer, RiskPlacesLayer } from '../map/layers';
import { EntityMap } from '../map/EntityLayers';
import { Chokepoints } from '../map/Chokepoints';
import { LanesLayer } from '../map/LanesLayer';
import { useLanes } from '../map/lanes';
import { MapControls } from '../map/MapControls';
import { Legend } from '../map/Legend';
import { clearFocus, setSelected } from '../map/focus';
import { usePalette } from '../lib/theme';
import { usePlaced, useGazetteer } from '../map/data';
import { Icon } from '../ui/Icon';
import { ErrorState, LevelChip, Pct, SheetHead, SkeletonRows, Spark } from '../ui/parts';
import { Grab } from '../ui/Grab';

type Status = 'open' | 'acknowledged' | 'mitigating' | 'dismissed';
const STATUS_TEXT: Record<S['RiskStatus'], string> = {
  open: 'Open', acknowledged: 'Acknowledged', mitigating: 'Mitigating', resolved: 'Resolved', dismissed: 'Dismissed',
};
const MATCH_TEXT: Record<S['Evidence']['match_type'], string> = {
  country: 'same country', geo_radius: 'nearby', port: 'same port', chokepoint: 'on your route', currency: 'your currency',
  commodity: 'what you buy', software: 'your software', sanctions_name: 'name on a sanctions list', company_name: 'named in the news',
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
  const where = useMemo(() => (r && ready ? placeOf(r) : null), [r, ready]);
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
  // pill, ring and lanes. The rest of the business is dimmed.
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

  return (
    <>
      <LanesLayer lanes={lanes} selected={entityId} />
      <EntityMap data={placedEntities?.fc ?? null} dimmed={bright} heat={false} pills={0} />
      <Chokepoints laneCounts={lanes?.counts} />
      <DotsLayer id="evidence" data={evidencePoints} color={P.signal} radius={3.5} />
      <RiskPlacesLayer data={hotspot} />

      <aside className="sheet w440 split glass" aria-label={r ? `Risk: ${r.title}` : 'Risk'}>
        <Grab />
        {risk.isPending ? (
          <div className="sbody">
            <SheetHead back={{ to: '/', label: 'Overview', onClick: () => navigate('/') }} />
            <SkeletonRows n={6} h={40} />
          </div>
        ) : risk.isError ? (
          <div className="sbody">
            <SheetHead back={{ to: '/', label: 'Overview', onClick: () => navigate('/') }} />
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
  const ref = useDismiss(menu, () => setMenu(false));

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

  const history = r.history.map((h) => h.score);
  const first = r.history[0];
  const anomaly = r.evidence.find((e) => e.kind === 'anomaly')?.anomaly ?? null;
  const series = useSeries(anomaly?.series ?? null, anomaly?.key ?? null);
  const steps = r.advice ? adviceSteps(r.advice.text) : [];
  const disruptions = useDisruptions();
  const chokeName = (id: string) =>
    disruptions.data?.features.find((f) => f.id === `chokepoint:${id}`)?.properties.name ?? id.replace(/_/g, ' ').replace(/^./, (c) => c.toUpperCase());
  const feeds = [...new Set(r.evidence.map((e) => (e.event ? feedName(e.event.source) : e.anomaly ? e.anomaly.series : '')).filter(Boolean))];
  const imp = r.factors.impact;
  const closed = r.status === 'resolved' || r.status === 'dismissed';
  const next: Status = r.status === 'open' ? 'acknowledged' : r.status === 'acknowledged' ? 'mitigating' : 'open';
  const nextLabel = { acknowledged: 'Acknowledge', mitigating: 'Mark mitigating', open: 'Reopen', dismissed: 'Dismiss' }[next];

  return (
    <>
      <div className="sbody">
      <SheetHead
        back={{ to: '/', label: 'Overview', onClick: () => (window.history.length > 1 ? window.history.back() : navigate('/')) }}
        right={
          <div style={{ display: 'flex', alignItems: 'center', gap: 4, position: 'relative' }} ref={ref}>
            <span className="t12">{STATUS_TEXT[r.status]}</span>
            {editor && r.status !== 'resolved' && (
              <>
                <button type="button" className="ib" aria-label="More actions" aria-expanded={menu} aria-haspopup="true" onClick={() => setMenu((m) => !m)}>
                  <Icon name="more" size={20} />
                </button>
                {menu && (
                  <div className="menu r" role="menu" style={{ top: 48, width: 220 }}>
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
                  </div>
                )}
              </>
            )}
          </div>
        }
      />

      <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: -6 }}>
        <span className="lbl">
          {CATEGORIES[r.category].name} · {KINDS[r.entity.kind].one} · opened {fmtDate(r.first_seen_at)}
        </span>
        <h1 className="h1" data-vt-title={r.id}>{r.title}</h1>
        <span className="body15">{r.explanation}</span>
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'auto minmax(0,1fr)', gap: 20, alignItems: 'center' }}>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <div style={{ display: 'flex', alignItems: 'baseline', gap: 10 }}>
            <span className="figxl">{Math.round(r.score)}</span>
            <LevelChip score={r.score} />
          </div>
          <span className="t12"><Pct value={r.likelihood} /> likely · impact {outOf5(r.impact)} of 5</span>
        </div>
        {history.length > 1 && first ? (
          <div style={{ display: 'flex', flexDirection: 'column', gap: 4, minWidth: 0 }}>
            <Spark values={history} area color={LEVELS[levelOf(r.score)].fill} label={`Score moved from ${Math.round(first.score)} to ${Math.round(r.score)} since ${fmtDate(first.recorded_at)}`} />
            <div style={{ display: 'flex', justifyContent: 'space-between' }} className="t12 tnum">
              <span>{Math.round(first.score)} on {fmtDate(first.recorded_at)}</span>
              <span>{Math.round(r.score)} now</span>
            </div>
          </div>
        ) : (
          <span className="t12">First scored {ago(r.first_seen_at)}. The trend appears after it is rescored.</span>
        )}
      </div>

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,minmax(0,1fr))', gap: 8 }}>
        <div className="tile">
          <span className="fig s">{imp.exposure_share != null ? <Pct value={imp.exposure_share} /> : imp.criticality ? cap(imp.criticality) : '–'}</span>
          <span className="t12">{imp.exposure_share != null ? 'of spend or revenue' : 'criticality'}</span>
        </div>
        <div className="tile"><span className="fig s">{r.evidence_count}</span><span className="t12">{r.evidence_count === 1 ? 'signal matched' : 'signals matched'}</span></div>
        <div className="tile"><span className="fig s">{ago(r.last_seen_at).replace(' ago', '')}</span><span className="t12">since the last one</span></div>
      </div>

      <div className="rule" />

      <div style={{ display: 'flex', flexDirection: 'column' }}>
        <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: 2 }}>
          <span className="lbl">What to do</span>
          {r.advice && <span className="t12">{r.advice.source === 'groq' ? 'Worded by AI from the evidence' : 'From the playbook'}</span>}
        </div>
        {steps.length ? (
          steps.map((s, i) => (
            <div className="act" key={i}>
              <span className="ck">{i + 1}</span>
              <span>{s}</span>
            </div>
          ))
        ) : (
          <span className="t13" style={{ padding: '8px 0' }}>Advice is written after the next scoring run.</span>
        )}
      </div>

      <div style={{ display: 'flex', flexDirection: 'column' }}>
        <span className="lbl" style={{ marginBottom: 2 }}>Why this reaches you</span>
        {r.evidence.length === 0 && <span className="t13" style={{ padding: '8px 0' }}>No evidence is linked any more.</span>}
        {r.evidence.slice(0, 6).map((e) => (
          <div className="evd" key={e.evidence_key}>
            <span style={{ minWidth: 0 }}>
              {e.snapshot.url ? (
                <a className="elink" href={e.snapshot.url} target="_blank" rel="noopener noreferrer">
                  {e.snapshot.title}
                  <Icon name="export" size={12} stroke={1.5} className="ext" aria-hidden="true" />
                  <span className="vh"> (opens the source)</span>
                </a>
              ) : (
                <b>{e.snapshot.title}</b>
              )}
            </span>
            <span className="t12" style={{ textAlign: 'right' }} title="Share of the likelihood"><Pct value={e.contribution} /></span>
            <span className="t12" style={{ gridColumn: '1 / -1' }}>
              {e.event ? feedName(e.event.source) : 'Anomaly'} · {matchText(e)}: {e.match_type === 'chokepoint' ? chokeName(e.match_value) : matchValue(e)}
              {e.snapshot.published_at ? ` · ${fmtDate(e.snapshot.published_at)}` : ''}
              {!e.still_in_feeds ? (e.match_type === 'sanctions_name' ? ' · no longer on the list' : ' · no longer in the feed') : ''}
            </span>
          </div>
        ))}
        {r.evidence.length > 6 && <span className="t12" style={{ paddingTop: 6 }}>{plural(r.evidence.length - 6, 'more signal')}</span>}
      </div>

      {r.story.risk_count > 1 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
          <span className="lbl">The same story also reaches</span>
          <span className="t13">
            {r.story.entities.filter((e) => e.id !== r.entity.id).map((e) => e.name).join(', ')}
            {r.story.risk_count > r.story.entities.length ? ` and ${plural(r.story.risk_count - r.story.entities.length, 'more')}` : ''}
          </span>
        </div>
      )}

      {r.factors.explain && <HowScored explain={r.factors.explain} />}

      {anomaly && series.data && series.data.points.length > 1 && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6 }}>
          <span className="lbl">{series.data.key} · {series.data.series}{series.data.unit ? ` (${series.data.unit})` : ''}</span>
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
      <span className="t12">
        Sources: {feeds.length ? feeds.join(', ') : 'none linked'} · updated {ago(r.last_seen_at)}
      </span>
      </div>
    </>
  );
}

/** The numbers behind the score, collapsed until asked for (factors.explain, engine 1.2.0). */
function HowScored({ explain }: { explain: NonNullable<RiskDetail['factors']['explain']> }) {
  const m = explain.matched;
  const via = m.inferred_from ? inferredText(m.inferred_from) : null;
  return (
    <details className="how" onToggle={(e) => e.currentTarget.open && revealInSheet(e.currentTarget)}>
      <summary>
        <span className="lbl">How this was scored</span>
        <Icon name="chev" size={14} stroke={2} className="chev" />
      </summary>
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
    </details>
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

function matchText(e: S['Evidence']) {
  const via = (e.snapshot as { inferred_from?: string | null }).inferred_from;
  if (e.match_type === 'chokepoint' && via) return via.startsWith('port ') ? 'inferred from the port' : 'inferred from the location';
  return MATCH_TEXT[e.match_type];
}

const cap = (s: string) => s[0].toUpperCase() + s.slice(1);
function minmax(v: number[]): [number, number] {
  const lo = Math.min(...v), hi = Math.max(...v);
  const pad = (hi - lo) * 0.1 || 1;
  return [lo - pad, hi + pad];
}
function matchValue(e: S['Evidence']) {
  if (e.match_type === 'country' && /^[A-Z]{3}$/.test(e.match_value)) return countryName(e.match_value);
  if (e.match_type === 'port' && /^[A-Z]{2}[A-Z2-9]{3}$/.test(e.match_value)) return portName(e.match_value);
  return e.match_value;
}
