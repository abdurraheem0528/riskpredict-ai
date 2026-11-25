// design/project/Connect.dc.html — Sources. The tiles are GET /v1/connections/providers
// (Google Sheets directly, others through Composio), never a fixed list. Google uses OAuth
// then the Google Picker; Composio uses its hosted sign-in, then browsing bases and tables.
import { useEffect, useMemo, useRef, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useBrowse, useConnection, useConnections, useMapEntities, useProviders, useSources, type Connection, type S } from '../api/queries';
import { api, unwrap } from '../api/client';
import { wsh } from '../api/workspace';
import { errorText } from '../api/errors';
import { useSession, canAdmin, canEdit } from '../app/Session';
import { useToast } from '../app/Toast';
import { navigate, useLocation } from '../lib/router';
import { ago, fmtInt, fmtTime, plural } from '../lib/format';
import { pickSpreadsheet } from '../lib/picker';
import { EntitiesLayer } from '../map/layers';
import { usePlaced } from '../map/data';
import { Icon } from '../ui/Icon';
import { ErrorState, SheetHead, SkeletonRows } from '../ui/parts';
import { Grab } from '../ui/Grab';

const needsConfirm = (s: S['Source']) => s.status === 'new' || s.status === 'mapping_suggested';
const monogram = (name: string) => {
  const words = name.split(/\s+/).filter(Boolean);
  return (words.length > 1 ? words.map((w) => w[0]).join('') : name).slice(0, 2).toUpperCase();
};

export function Connect() {
  const { workspace, role } = useSession();
  const ws = workspace.id;
  const { query } = useLocation();
  const returned = query.get('connection_id');
  const returnStatus = query.get('status');
  const returnCode = query.get('code');
  const providers = useProviders();
  const connections = useConnections(ws);
  const sources = useSources(ws);
  const entities = useMapEntities(ws);
  const placed = usePlaced(entities.data);
  const toast = useToast();
  const qc = useQueryClient();
  const [q, setQ] = useState('');
  const [selected, setSelected] = useState<string | null>(returned);

  // Back from Composio's hosted sign-in: wait for the connection to turn active.
  const polled = useConnection(ws, returned, true);
  const announced = useRef(false);
  useEffect(() => {
    if (!polled.data || announced.current) return;
    if (polled.data.status === 'active') {
      announced.current = true;
      toast({ text: `${polled.data.display_name} connected` });
      void qc.invalidateQueries({ queryKey: [ws, 'connections'] });
    }
  }, [polled.data, toast, qc, ws]);

  const byProvider = useMemo(() => {
    const m = new Map<string, Connection>();
    for (const c of connections.data?.items ?? []) {
      if (c.status === 'revoked') continue;
      const prev = m.get(c.provider);
      if (!prev || (c.status === 'active' && prev.status !== 'active')) m.set(c.provider, c);
    }
    return m;
  }, [connections.data]);

  const items = (providers.data?.items ?? []).filter((p) => !q.trim() || p.name.toLowerCase().includes(q.trim().toLowerCase()));
  const selConn = (connections.data?.items ?? []).find((c) => c.id === selected) ?? [...byProvider.values()][0] ?? null;
  const admin = canAdmin(role);

  const start = useMutation({
    mutationFn: async (p: S['Provider']) => {
      if (p.kind === 'direct') {
        const r = await unwrap(api.POST('/v1/connections/google/start', { params: { header: wsh(ws) }, body: { return_path: '/connect' } }));
        return r.authorization_url;
      }
      const app = p.id.replace(/^composio:/, '');
      const r = await unwrap(api.POST('/v1/connections/composio', { params: { header: wsh(ws) }, body: { app } }));
      return r.redirect_url;
    },
    onSuccess: (url) => window.location.assign(url),
    onError: (e) => toast({ text: errorText(e) }),
  });

  const onTile = (p: S['Provider']) => {
    const c = byProvider.get(p.id);
    if (c && c.status !== 'pending') {
      setSelected(c.id);
      return;
    }
    if (!admin) {
      toast({ text: 'Only an admin can connect tools. Ask one to add it.' });
      return;
    }
    start.mutate(p);
  };

  return (
    <>
      <EntitiesLayer data={placed?.fc ?? null} />
      <aside className="sheet glass" aria-label="Connect a source">
        <Grab />
        <SheetHead back={{ to: '/', label: 'Overview', onClick: () => navigate('/') }} right={<span className="t12" style={{ paddingRight: 12 }}>Step 1 of 3</span>} />
        <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: -6 }}>
          <h1 className="h1">Connect a source</h1>
          <span className="body15">Pick wherever you already keep suppliers, shipments or invoices. Access is read-only, and changes sync on their own.</span>
        </div>
        {returnStatus === 'error' && (
          <div className="note" role="alert">
            <Icon name="warning" style={{ flex: 'none', marginTop: 1 }} />
            The connection did not finish{returnCode ? ` (${returnCode.replace(/_/g, ' ')})` : ''}. Try again.
          </div>
        )}
        <label style={{ position: 'relative', display: 'flex', alignItems: 'center' }}>
          <span style={{ position: 'absolute', left: 14, display: 'inline-flex', color: '#5F646C' }}><Icon name="search" /></span>
          <span className="vh">Search tools</span>
          <input className="search" type="text" placeholder="Search tools" value={q} onChange={(e) => setQ(e.target.value)} />
        </label>
        {providers.isPending ? (
          <SkeletonRows n={2} h={60} />
        ) : providers.isError ? (
          <ErrorState error={providers.error} retry={() => void providers.refetch()} />
        ) : items.length === 0 ? (
          <span className="t13">{q ? `No tool matches “${q}”.` : 'No tools are set up on this server yet. An administrator adds Google or Composio credentials to the API.'}</span>
        ) : (
          <div className="tiles">
            {items.map((p) => {
              const c = byProvider.get(p.id);
              const on = !!c && selConn?.id === c.id;
              let sub: { text: string; color?: string } = { text: p.kind === 'direct' ? 'Spreadsheets' : 'Bases and tables' };
              if (!p.available) sub = { text: 'Not set up on this server' };
              else if (c?.status === 'active') sub = { text: 'Connected · live', color: '#0D7A47' };
              else if (c?.status === 'needs_reauth' || c?.status === 'error') sub = { text: 'Needs reconnecting', color: '#8F1A0B' };
              else if (c?.status === 'pending') sub = { text: 'Waiting for sign-in' };
              return (
                <button key={p.id} type="button" className={`ctile${on ? ' on' : ''}`} aria-pressed={on} disabled={!p.available || (start.isPending && start.variables?.id === p.id)} onClick={() => onTile(p)}>
                  <span className="mg" style={c?.status === 'active' ? { color: '#141518' } : undefined}>{monogram(p.name)}</span>
                  <span style={{ minWidth: 0 }}>
                    <b>{p.name}</b>
                    <span className="s" style={sub.color ? { color: sub.color } : undefined}>{start.isPending && start.variables?.id === p.id ? 'Opening sign-in…' : sub.text}</span>
                  </span>
                </button>
              );
            })}
          </div>
        )}
        <span className="t12" style={{ marginTop: 'auto' }}>
          {admin ? 'More tools connect through Composio as the server adds them.' : 'Only admins connect tools. Editors pick sheets and confirm how they are read.'}
        </span>
        {selConn && (
          <div className="fold">
            <ConnectionPanel conn={selConn} sources={sources.data?.items ?? []} compact />
          </div>
        )}
      </aside>

      {selConn && (
        <section className="side above glass" aria-label={`${selConn.display_name} connection`}>
          <ConnectionPanel conn={selConn} sources={sources.data?.items ?? []} />
        </section>
      )}

      <div className="legend glass">
        <span className="k"><span className="sw" style={{ background: '#141518' }} />From your sources</span>
        <span className="k"><span className="sw" style={{ background: '#141518', borderRadius: 2 }} />Your sites</span>
        <span>{entities.data ? plural(entities.data.features.length, 'entity', 'entities') + ' on the map' : ''}</span>
      </div>
    </>
  );
}

function ConnectionPanel({ conn, sources, compact = false }: { conn: Connection; sources: S['Source'][]; compact?: boolean }) {
  const { workspace, role } = useSession();
  const ws = workspace.id;
  const toast = useToast();
  const qc = useQueryClient();
  const mine = sources.filter((s) => s.connection_id === conn.id);
  const pending = mine.find(needsConfirm) ?? null;
  const google = conn.provider === 'google_sheets';
  const [browsing, setBrowsing] = useState(false);
  const [confirmDisconnect, setConfirmDisconnect] = useState(false);
  const editor = canEdit(role);

  const addSource = useMutation({
    mutationFn: (body: { external_id: string; external_table_id?: string }) =>
      unwrap(api.POST('/v1/sources', { params: { header: wsh(ws) }, body: { connection_id: conn.id, ...body } })),
    onSuccess: (s) => {
      void qc.invalidateQueries({ queryKey: [ws, 'sources'] });
      navigate(`/sources/${s.id}/confirm`);
    },
    onError: (e) => toast({ text: errorText(e) }),
  });

  const pick = useMutation({
    mutationFn: async () => {
      const creds = await unwrap(api.POST('/v1/connections/{connection_id}/google/picker-token', { params: { header: wsh(ws), path: { connection_id: conn.id } } }));
      return pickSpreadsheet(creds);
    },
    onSuccess: (doc) => {
      if (doc) addSource.mutate({ external_id: doc.id });
    },
    onError: (e) => toast({ text: e instanceof Error && !('status' in e) ? e.message : errorText(e) }),
  });

  const sync = useMutation({
    mutationFn: (id: string) => unwrap(api.POST('/v1/sources/{source_id}/sync', { params: { header: wsh(ws), path: { source_id: id } } })),
    onSuccess: () => toast({ text: 'Reading it again now' }),
    onError: (e) => toast({ text: errorText(e) }),
  });

  const disconnect = useMutation({
    mutationFn: () => unwrap(api.DELETE('/v1/connections/{connection_id}', { params: { header: wsh(ws), path: { connection_id: conn.id } } })),
    onSuccess: () => {
      setConfirmDisconnect(false);
      void qc.invalidateQueries({ queryKey: [ws] });
      toast({ text: `${conn.display_name} disconnected` });
    },
    onError: (e) => toast({ text: errorText(e) }),
  });

  const reconnect = useMutation({
    mutationFn: async () => {
      if (google) {
        const r = await unwrap(api.POST('/v1/connections/google/start', { params: { header: wsh(ws) }, body: { return_path: '/connect' } }));
        return r.authorization_url;
      }
      const r = await unwrap(api.POST('/v1/connections/composio', { params: { header: wsh(ws) }, body: { app: conn.provider.replace(/^composio:/, '') } }));
      return r.redirect_url;
    },
    onSuccess: (url) => window.location.assign(url),
    onError: (e) => toast({ text: errorText(e) }),
  });

  const live = conn.status === 'active';
  const busy = pick.isPending || addSource.isPending;
  const read = mine.filter((s) => !needsConfirm(s));
  const totalRows = read.reduce((n, s) => n + (s.row_count ?? 0), 0);
  const placedN = read.reduce((n, s) => n + s.entity_count, 0);
  const lastChange = mine.map((s) => s.last_change_at).filter(Boolean).sort().pop() ?? null;
  const lastSync = mine.map((s) => s.last_synced_at).filter(Boolean).sort().pop() ?? null;
  const failing = mine.find((s) => s.status === 'error');

  const primary = pending ? (
    <button type="button" className="b pri" style={{ flex: 1 }} onClick={() => navigate(`/sources/${pending.id}/confirm`)}>Review what we found</button>
  ) : null;
  const addBtn = editor && live && (google || !browsing) ? (
    <button type="button" className={`b ${primary ? 'sec2' : 'pri'}`} style={{ flex: primary ? undefined : 1 }} disabled={busy} onClick={() => (google ? pick.mutate() : setBrowsing(true))}>
      {busy && <span className="spin" />}
      {google ? (mine.length ? 'Add a sheet' : 'Pick a sheet') : 'Choose a table'}
    </button>
  ) : null;

  return (
    <>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span style={{ fontSize: 15, lineHeight: '20px', fontWeight: 600 }}>{conn.display_name}</span>
        <span className="t12" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
          <span style={{ width: 6, height: 6, borderRadius: 3, background: live && !failing ? '#1A9E5A' : conn.status === 'pending' ? '#A3A7AE' : '#B3200F' }} />
          {live ? (failing ? 'Sync failed' : 'Live') : conn.status === 'pending' ? 'Waiting' : 'Needs reconnecting'}
        </span>
      </div>
      <span className="t12" style={{ marginTop: -8 }}>
        {[conn.account_label, plural(conn.source_count, google ? 'sheet' : 'table'), google && conn.watch ? 'syncs on every edit' : 'syncs on a schedule', 'read-only'].filter(Boolean).join(' · ')}
      </span>
      {conn.status_reason && !live && <span className="t12" style={{ color: '#8F1A0B' }}>{conn.status_reason}</span>}

      {mine.length > 0 && !compact && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, paddingTop: 14, borderTop: '1px solid rgba(20,21,24,0.07)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline' }}>
            <span style={{ fontSize: 13, lineHeight: '18px', fontWeight: 600 }}>Placing rows on the map</span>
            <span className="t12 tnum">{totalRows ? `${fmtInt(placedN)} of ${fmtInt(totalRows)}` : fmtInt(placedN)}</span>
          </div>
          <div className="prog" role="progressbar" aria-valuenow={placedN} aria-valuemin={0} aria-valuemax={totalRows || placedN || 1} aria-label="Rows placed">
            <i style={{ width: `${totalRows ? Math.min(100, (placedN / totalRows) * 100) : placedN ? 100 : 0}%` }} />
          </div>
          {mine.map((s) => (
            <div key={s.id} className="kv">
              <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.name}</span>
              <span>
                {s.status === 'error' ? (
                  <button type="button" className="b plain" style={{ height: 32, padding: '0 8px', fontSize: 13 }} onClick={() => sync.mutate(s.id)}>Retry</button>
                ) : needsConfirm(s) ? 'to review' : s.status === 'paused' ? 'paused' : plural(s.entity_count, 'entity', 'entities')}
              </span>
            </div>
          ))}
        </div>
      )}

      {mine.length > 0 && !compact && (lastChange || lastSync) && (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, paddingTop: 14, borderTop: '1px solid rgba(20,21,24,0.07)' }}>
          <span className="lbl">Changes picked up</span>
          {lastChange && <div className="ev"><span className="t12 tnum">{fmtTime(lastChange)}</span><span>Last edit in the tool · {ago(lastChange)}</span></div>}
          {lastSync && <div className="ev"><span className="t12 tnum">{fmtTime(lastSync)}</span><span>Last read · {ago(lastSync)}</span></div>}
        </div>
      )}

      {browsing && !google && <Browse conn={conn} onPick={(base, table) => { setBrowsing(false); addSource.mutate({ external_id: base, external_table_id: table }); }} onCancel={() => setBrowsing(false)} />}

      <div style={{ display: 'flex', gap: 8, marginTop: 4 }}>
        {primary}
        {addBtn}
        {!live && conn.status !== 'pending' && canAdmin(role) && (
          <button type="button" className="b pri" style={{ flex: 1 }} disabled={reconnect.isPending} onClick={() => reconnect.mutate()}>
            {reconnect.isPending && <span className="spin" />}Reconnect
          </button>
        )}
      </div>
      {canAdmin(role) && !compact && (
        <button type="button" className="b plain" style={{ alignSelf: 'flex-start', marginTop: -8, marginLeft: -12, height: 44, fontSize: 13, color: '#5F646C' }} onClick={() => setConfirmDisconnect(true)}>
          Disconnect…
        </button>
      )}

      {confirmDisconnect && (
        <>
          <div className="scrim" style={{ position: 'fixed' }} onClick={() => setConfirmDisconnect(false)} aria-hidden="true" />
          <div role="alertdialog" aria-modal="true" aria-labelledby="dc-t" className="card" style={{ position: 'fixed', background: '#fff', width: 400, gap: 16, zIndex: 21 }}>
            <span id="dc-t" className="h3">Disconnect {conn.display_name}?</span>
            <span className="t13">Its {plural(mine.length, 'source')}, the entities read from them and their risks are deleted. Alerts from it stop until you connect it again.</span>
            <div style={{ display: 'flex', gap: 8, justifyContent: 'flex-end' }}>
              <button type="button" className="b sec2" autoFocus onClick={() => setConfirmDisconnect(false)}>Cancel</button>
              <button type="button" className="b dest" disabled={disconnect.isPending} onClick={() => disconnect.mutate()}>
                {disconnect.isPending && <span className="spin" />}Disconnect
              </button>
            </div>
          </div>
        </>
      )}
    </>
  );
}

function Browse({ conn, onPick, onCancel }: { conn: Connection; onPick: (base: string, table: string) => void; onCancel: () => void }) {
  const { workspace } = useSession();
  const [base, setBase] = useState<{ id: string; name: string } | null>(null);
  const list = useBrowse(workspace.id, conn.id, base?.id);
  return (
    <div style={{ display: 'flex', flexDirection: 'column', paddingTop: 14, borderTop: '1px solid rgba(20,21,24,0.07)', maxHeight: 260, overflow: 'auto' }}>
      <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center' }}>
        <span className="lbl">{base ? base.name : 'Bases'}</span>
        <button type="button" className="b plain" style={{ height: 32, padding: '0 8px', fontSize: 13 }} onClick={() => (base ? setBase(null) : onCancel())}>
          {base ? 'All bases' : 'Cancel'}
        </button>
      </div>
      {list.isPending && <SkeletonRows n={3} h={36} />}
      {list.isError && <ErrorState error={list.error} retry={() => void list.refetch()} />}
      {list.isSuccess && list.data.items.length === 0 && <span className="t13" style={{ padding: '8px 0' }}>Nothing here to read.</span>}
      {list.data?.items.map((it) => (
        <button
          key={it.id}
          type="button"
          className="mi"
          style={{ margin: '0 -12px' }}
          disabled={it.kind === 'table' && !it.selectable}
          onClick={() => (it.kind === 'base' ? setBase({ id: it.id, name: it.name }) : base && onPick(base.id, it.id))}
        >
          <span>{it.name}</span>
          <Icon name="chev" size={14} stroke={2} />
        </button>
      ))}
    </div>
  );
}
