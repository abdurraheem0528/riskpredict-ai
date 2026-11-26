// The four capsules of the top row (System-Nav): workspace, sections, Jump to, account.
// ≤900 the sections fold into one menu and Jump to keeps only its icon; ≤760 Jump to
// moves into that menu. Register and Scenarios stay greyed out until a source is live.
import { useEffect, useRef, useState, type FormEvent, type ReactNode } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { Icon } from '../ui/Icon';
import { Link, navigate, useLocation } from '../lib/router';
import { useAlerts, useConnections, useRiskSummary, useSources } from '../api/queries';
import { api, unwrap } from '../api/client';
import { signOut } from '../api/auth';
import { wsh, setWorkspaceId } from '../api/workspace';
import { errorText } from '../api/errors';
import { ago, fmtInt, initials } from '../lib/format';
import { useSession } from './Session';

export type Section = 'overview' | 'register' | 'scenarios' | 'sources';

export function sectionOf(path: string): Section {
  if (path.startsWith('/register') || path.startsWith('/risks')) return 'register';
  if (path.startsWith('/scenarios')) return 'scenarios';
  if (path.startsWith('/connect') || path.startsWith('/sources')) return 'sources';
  return 'overview';
}

/** Close on a click outside or Esc (System-Nav: menus). */
export function useDismiss(open: boolean, close: () => void) {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open) return;
    const onDown = (e: PointerEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) close();
    };
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') close();
    };
    document.addEventListener('pointerdown', onDown);
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('pointerdown', onDown);
      document.removeEventListener('keydown', onKey);
    };
  }, [open, close]);
  return ref;
}

export function useWorkspaceState() {
  const { workspace } = useSession();
  const sources = useSources(workspace.id);
  const connections = useConnections(workspace.id);
  const items = sources.data?.items ?? [];
  const ready = items.some((s) => s.status === 'active' || s.status === 'paused' || (s.status === 'error' && s.entity_count > 0));
  const broken =
    (connections.data?.items ?? []).filter((c) => c.status === 'error' || c.status === 'needs_reauth').map((c) => c.display_name)
      .concat(items.filter((s) => s.status === 'error').map((s) => s.name));
  return { ready, broken, sources, connections };
}

/** The Register count is of stories (RiskSummary.story_count), not entity × story risks. */
const storiesText = (n: number) => `${fmtInt(n)} open ${n === 1 ? 'story' : 'stories'}`;

function NavLinks({ section, ready, count, broken, item, onPick }: { section: Section; ready: boolean; count: number | null; broken: boolean; item: 'navl' | 'mi'; onPick?: () => void }) {
  const links: { id: Section; label: string; to: string; extra?: ReactNode; off?: boolean }[] = [
    { id: 'overview', label: 'Overview', to: '/' },
    { id: 'register', label: 'Register', to: '/register', off: !ready, extra: ready && count != null && count > 0 ? <small title={storiesText(count)}>{fmtInt(count)}</small> : null },
    { id: 'scenarios', label: 'Scenarios', to: '/scenarios', off: !ready },
    { id: 'sources', label: 'Sources', to: '/connect', extra: broken ? <span className="rdot" role="img" aria-label="needs attention" /> : null },
  ];
  return (
    <>
      {links.map((l) =>
        l.off ? (
          <span key={l.id} className={`${item} off`} aria-disabled="true" title="Connect a source first">
            {l.label}
          </span>
        ) : (
          <Link key={l.id} to={l.to} className={`${item}${section === l.id ? ' on' : ''}`} aria-current={section === l.id ? 'page' : undefined} onClick={onPick}>
            {l.label}
            {l.extra}
          </Link>
        ),
      )}
    </>
  );
}

export function TopBar({ onJump }: { onJump: () => void }) {
  const { workspace, user } = useSession();
  const { path } = useLocation();
  const section = sectionOf(path);
  const summary = useRiskSummary(workspace.id);
  const { ready, broken } = useWorkspaceState();
  const [open, setOpen] = useState<null | 'ws' | 'nav' | 'acct'>(null);
  const close = () => setOpen(null);
  const count = summary.data?.story_count ?? null;
  const toggle = (m: 'ws' | 'nav' | 'acct') => setOpen((o) => (o === m ? null : m));
  const sectionName = { overview: 'Overview', register: 'Register', scenarios: 'Scenarios', sources: 'Sources' }[section];

  return (
    <div className="top">
      <WorkspaceMenu open={open === 'ws'} onToggle={() => toggle('ws')} onClose={close} />
      <div className="navc">
        <nav aria-label="Main" className="cap glass">
          <NavLinks section={section} ready={ready} count={count} broken={broken.length > 0} item="navl" />
        </nav>
      </div>
      <NavMenu open={open === 'nav'} onToggle={() => toggle('nav')} onClose={close} label={sectionName} count={section === 'register' ? count : null}>
        <NavLinks section={section} ready={ready} count={count} broken={broken.length > 0} item="mi" onPick={close} />
        <span className="mdiv mjump" />
        <button type="button" className="mi mjump" style={{ justifyContent: 'flex-start', gap: 10 }} onClick={() => { close(); onJump(); }}>
          <Icon name="search" />
          Jump to
        </button>
      </NavMenu>
      <button type="button" className="cap glass jump" aria-label="Jump to" aria-keyshortcuts="Meta+K Control+K" onClick={onJump}>
        <Icon name="search" />
        <span className="jtl">Jump to</span>
        <span className="kbd jtl">⌘K</span>
      </button>
      <AccountMenu open={open === 'acct'} onToggle={() => toggle('acct')} onClose={close} label={initials(user.display_name, user.email)} />
    </div>
  );
}

function NavMenu({ open, onToggle, onClose, label, count, children }: { open: boolean; onToggle: () => void; onClose: () => void; label: string; count: number | null; children: ReactNode }) {
  const ref = useDismiss(open, onClose);
  return (
    <div className="navs">
      <div className="navm" ref={ref}>
        <button type="button" className="cap glass navsum" aria-expanded={open} aria-haspopup="true" onClick={onToggle}>
          {label}
          {count != null && count > 0 && <small className="t12 tnum" style={{ fontWeight: 500 }} title={storiesText(count)}>{fmtInt(count)}</small>}
          <Icon name="expand" size={14} stroke={1.75} style={{ color: '#5F646C' }} />
        </button>
        {open && (
          <nav aria-label="Sections" className="menu c">
            {children}
          </nav>
        )}
      </div>
    </div>
  );
}

function WorkspaceMenu({ open, onToggle, onClose }: { open: boolean; onToggle: () => void; onClose: () => void }) {
  const { workspace, memberships, switchWorkspace, role } = useSession();
  const ref = useDismiss(open, onClose);
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState('');
  const qc = useQueryClient();
  const create = useMutation({
    mutationFn: (n: string) => unwrap(api.POST('/v1/workspaces', { body: { name: n } })),
    onSuccess: async (w) => {
      await qc.invalidateQueries({ queryKey: ['me'] });
      setWorkspaceId(w.id);
      setAdding(false);
      setName('');
      onClose();
      navigate('/');
    },
  });
  const submit = (e: FormEvent) => {
    e.preventDefault();
    if (name.trim()) create.mutate(name.trim());
  };
  return (
    <div style={{ position: 'relative' }} ref={ref}>
      <button type="button" className="cap glass capbtn" aria-expanded={open} aria-haspopup="true" aria-label={`Workspace: ${workspace.name}`} onClick={onToggle}>
        <span className="wsmark"><Icon name="signals" size={18} stroke={1.75} /></span>
        <span className="wsn">{workspace.name}</span>
        <Icon name="expand" size={14} stroke={1.75} style={{ color: '#5F646C' }} />
      </button>
      {open && (
        <div className="menu l" role="menu" aria-label="Workspaces">
          <span className="mh">Workspaces</span>
          {memberships.map((m) => (
            <button
              key={m.workspace.id}
              type="button"
              role="menuitemradio"
              aria-checked={m.workspace.id === workspace.id}
              className={`mi${m.workspace.id === workspace.id ? ' on' : ''}`}
              onClick={() => {
                switchWorkspace(m.workspace.id);
                onClose();
                navigate('/');
              }}
            >
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{m.workspace.name}</span>
              <small>{m.role}</small>
            </button>
          ))}
          <span className="mdiv" />
          {adding ? (
            <form onSubmit={submit} style={{ display: 'flex', flexDirection: 'column', gap: 8, padding: '4px 12px 8px' }}>
              <label className="vh" htmlFor="ws-new">Workspace name</label>
              <input id="ws-new" className="inp" autoFocus placeholder="Company name" value={name} maxLength={120} onChange={(e) => setName(e.target.value)} />
              {create.isError && <span className="err">{errorText(create.error)}</span>}
              <div style={{ display: 'flex', gap: 8 }}>
                <button type="button" className="b sec2" style={{ flex: 1 }} onClick={() => setAdding(false)}>Cancel</button>
                <button type="submit" className="b sec2" style={{ flex: 1 }} disabled={!name.trim() || create.isPending}>
                  {create.isPending && <span className="spin" />}Create
                </button>
              </div>
            </form>
          ) : (
            <button type="button" role="menuitem" className="mi" style={{ justifyContent: 'flex-start', gap: 10 }} onClick={() => setAdding(true)}>
              <Icon name="add" />
              New workspace
            </button>
          )}
          <span className="mh" style={{ paddingBottom: 8 }}>Your role here: {role}</span>
        </div>
      )}
    </div>
  );
}

function AccountMenu({ open, onToggle, onClose, label }: { open: boolean; onToggle: () => void; onClose: () => void; label: string }) {
  const { user, workspace } = useSession();
  const ref = useDismiss(open, onClose);
  const alerts = useAlerts(workspace.id);
  const qc = useQueryClient();
  const unread = alerts.data?.pages[0]?.unread_count ?? 0;
  const readAll = useMutation({
    mutationFn: () => unwrap(api.POST('/v1/alerts/read-all', { params: { header: wsh(workspace.id) } })),
    onSuccess: () => qc.invalidateQueries({ queryKey: [workspace.id, 'alerts'] }),
  });
  // Five at first; "Show all" lists every alert in a scrolling list, loading further pages.
  const [all, setAll] = useState(false);
  useEffect(() => { if (!open) setAll(false); }, [open]);
  const loaded = alerts.data?.pages.flatMap((p) => p.items) ?? [];
  const items = all ? loaded : loaded.slice(0, 5);
  const more = !all && (loaded.length > 5 || alerts.hasNextPage);
  return (
    <div style={{ position: 'relative' }} ref={ref}>
      <button type="button" className="cap glass acct" aria-label={unread ? `Account, ${unread} unread alerts` : 'Account'} aria-expanded={open} aria-haspopup="true" onClick={onToggle} style={{ position: 'relative' }}>
        {label}
        {unread > 0 && <span className="rdot" style={{ position: 'absolute', top: 9, right: 10 }} aria-hidden="true" />}
      </button>
      {open && (
        <div className="menu r" role="menu" aria-label="Account">
          <div style={{ display: 'flex', flexDirection: 'column', padding: '8px 18px 8px' }}>
            <span style={{ fontWeight: 600, overflow: 'hidden', textOverflow: 'ellipsis' }}>{user.display_name ?? user.email}</span>
            {user.display_name && <span className="t12" style={{ overflow: 'hidden', textOverflow: 'ellipsis' }}>{user.email}</span>}
          </div>
          <span className="mdiv" />
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '0 6px 0 18px' }}>
            <span className="lbl">Alerts{unread ? ` · ${fmtInt(unread)} unread` : ''}</span>
            {unread > 0 && (
              <button type="button" className="b plain" style={{ height: 44, padding: '0 12px', fontSize: 13 }} onClick={() => readAll.mutate()}>
                Mark all read
              </button>
            )}
          </div>
          {alerts.isPending && <div className="sk" style={{ height: 36, margin: '6px 18px' }} />}
          {alerts.isError && <span className="t12" style={{ padding: '6px 18px' }}>{errorText(alerts.error)}</span>}
          {alerts.isSuccess && items.length === 0 && <span className="t12" style={{ padding: '6px 18px 10px' }}>No alerts yet.</span>}
          <div style={all ? { maxHeight: 'min(360px, 50vh)', overflowY: 'auto', display: 'flex', flexDirection: 'column' } : { display: 'contents' }}>
            {items.map((a) => {
              const done = a.risk_status === 'resolved' || a.risk_status === 'dismissed';
              return (
                <button
                  key={a.id}
                  type="button"
                  role="menuitem"
                  className="mi"
                  style={{ alignItems: 'flex-start', padding: '8px 12px', flexDirection: 'column', gap: 2, justifyContent: 'center', flex: 'none' }}
                  onClick={() => {
                    onClose();
                    if (a.risk_id) navigate(`/risks/${a.risk_id}`);
                    else if (a.connection_id) navigate('/connect');
                  }}
                >
                  <span style={{ display: 'flex', gap: 8, alignItems: 'center', width: '100%' }}>
                    {!a.read_at && !done && <span className={a.severity === 'critical' ? 'rdot' : 'gdot'} style={a.severity === 'info' ? { background: '#A3A7AE' } : a.severity === 'warning' ? { background: '#DD6248' } : undefined} aria-hidden="true" />}
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', flex: 1, ...(done ? { color: '#5F646C' } : {}) }}>{a.title}</span>
                  </span>
                  <small>{done ? `${a.risk_status === 'resolved' ? 'Resolved' : 'Dismissed'} · ` : ''}{ago(a.created_at)}</small>
                </button>
              );
            })}
            {all && alerts.hasNextPage && (
              <button type="button" className="b plain" style={{ height: 40, margin: '2px 6px', fontSize: 13, flex: 'none' }} disabled={alerts.isFetchingNextPage} onClick={() => void alerts.fetchNextPage()}>
                {alerts.isFetchingNextPage && <span className="spin" />}
                Load more
              </button>
            )}
          </div>
          {more && (
            <button type="button" className="b plain" style={{ height: 40, margin: '2px 6px', fontSize: 13 }} onClick={() => setAll(true)}>
              Show all
            </button>
          )}
          <span className="mdiv" />
          <button type="button" role="menuitem" className="mi" onClick={() => void signOut().then(() => { qc.clear(); navigate('/sign-in'); })}>
            Sign out
          </button>
        </div>
      )}
    </div>
  );
}
