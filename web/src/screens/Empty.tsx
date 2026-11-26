// design/project/Empty.dc.html — a new workspace. The grey dots are real feed events
// (GET /v1/map/events), and the legend's feed count comes from GET /v1/feeds/health.
import { useFeedHealth, useMapEvents, useProviders, type Source } from '../api/queries';
import { useSession, canEdit } from '../app/Session';
import { navigate } from '../lib/router';
import { feedSummary } from '../lib/feeds';
import { fmtInt } from '../lib/format';
import { FeedDots } from '../live/FeedDots';
import { FeedStatus } from '../ui/parts';
import { usePlaced } from '../map/data';
import { useMap } from '../map/MapRoot';
import { useWorldView } from '../map/camera';

export function Empty({ pending, hasConnection }: { pending: Source | null; hasConnection: boolean }) {
  const { workspace, role } = useSession();
  const events = useMapEvents();
  const health = useFeedHealth();
  const providers = useProviders();
  const placed = usePlaced(events.data);
  const feeds = feedSummary(health.data);
  const tools = (providers.data?.items ?? []).filter((p) => p.available).map((p) => p.name);
  const step = pending ? 2 : 1;
  const editor = canEdit(role);
  // the whole globe behind the card
  useWorldView(useMap().map);

  const intro = health.isSuccess && feeds.total > 0
    ? `The grey dots are what ${fmtInt(feeds.total)} global feeds see right now.`
    : 'The grey dots are what the global feeds see right now.';
  const signalNote =
    events.isSuccess && events.data.features.length === 0 ? ' No signals in the last 7 days yet.' : events.isError ? ' The signals did not load.' : '';

  return (
    <>
      <FeedDots data={placed?.fc ?? null} />
      <section className="card glass" aria-label="Set up your workspace">
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          <span className="lbl">{workspace.name} · new workspace</span>
          <h1 className="h1">Nothing on this map is yours yet</h1>
          <span className="body15">
            {intro}
            {signalNote} Connect where you keep suppliers, shipments or invoices, and the ones that touch you turn red.
          </span>
        </div>
        <div>
          <div className="st">
            <span className={`stn${step === 1 ? ' on' : ''}`}>1</span>
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              <span className={step === 1 ? undefined : 'muted'} style={{ fontSize: 15, lineHeight: '20px', fontWeight: step === 1 ? 600 : 500, letterSpacing: '-0.006em' }}>
                {hasConnection ? 'Source connected' : 'Connect a source'}
              </span>
              <span className="t13">{tools.length ? tools.join(' or ') : providers.isPending ? ' ' : 'No tools are set up on this server yet'}</span>
            </div>
          </div>
          <div className="st">
            <span className={`stn${step === 2 ? ' on' : ''}`}>2</span>
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              <span className={step === 2 ? undefined : 'muted'} style={{ fontSize: 15, lineHeight: '20px', fontWeight: step === 2 ? 600 : 500 }}>Check how we read it</span>
              <span className="t13">{pending ? pending.name : 'About two minutes'}</span>
            </div>
          </div>
          <div className="st">
            <span className="stn">3</span>
            <div style={{ display: 'flex', flexDirection: 'column' }}>
              <span className="muted" style={{ fontSize: 15, lineHeight: '20px', fontWeight: 500 }}>See your map</span>
              <span className="t13">Risks appear the moment something matches</span>
            </div>
          </div>
        </div>
        {editor ? (
          <button type="button" className="b pri" onClick={() => navigate(pending ? `/sources/${pending.id}/confirm` : '/connect')}>
            {pending ? 'Check how we read it' : 'Connect a source'}
          </button>
        ) : (
          <div className="note info">You can view this workspace. An editor or admin connects its sources.</div>
        )}
        <span className="t12" style={{ textAlign: 'center', marginTop: -8 }}>Read-only access. No row becomes part of your map until you confirm how we read it.</span>
      </section>
      <div className="legend left glass">
        <span className="k"><span className="sw signal" />Global signals</span>
        <span className="vr" />
        <FeedStatus health={health.data} pending={health.isPending} failed={health.isError} />
        {health.isSuccess && feeds.names.length > 0 && <span className="fill">{feeds.names.join(' · ')}</span>}
      </div>
    </>
  );
}
