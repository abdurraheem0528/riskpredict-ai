// design/project/Confirm.dc.html — check how the AI (or the header heuristics) read the
// sheet. Each column's suggestion and confidence is GET /v1/sources/{id}/mapping; the map
// and the corner card are POST …/mapping/preview, rerun as the user changes a column.
import { useEffect, useMemo, useState } from 'react';
import { useMutation, useQueryClient } from '@tanstack/react-query';
import { useMapping, useSource, useSources, type S } from '../api/queries';
import { api, unwrap } from '../api/client';
import { wsh } from '../api/workspace';
import { ApiError, errorText } from '../api/errors';
import { useSession, canEdit } from '../app/Session';
import { useToast } from '../app/Toast';
import { useWorkspaceState } from '../app/TopBar';
import { navigate } from '../lib/router';
import { KINDS, plural, fmtInt, type EntityKind } from '../lib/format';
import { EntitiesLayer } from '../map/layers';
import { usePlaced } from '../map/data';
import { useMap, fitTo, sheetPadding } from '../map/MapRoot';
import { boxOf } from './Main';
import { Icon } from '../ui/Icon';
import { ErrorState, SheetHead, SkeletonRows } from '../ui/parts';
import { Grab } from '../ui/Grab';

type Field = S['MappingField'];
// `currency`: an annual_spend column's spend currency, read from its header ("Annual spend (USD)").
type Choice = { field: Field | ''; split?: string; currency?: string; touched: boolean };
const KIND_LIST: EntityKind[] = ['supplier', 'site', 'customer', 'route', 'port', 'currency', 'commodity', 'software'];
const SURE = 0.6;

export function Confirm({ id }: { id: string }) {
  const { workspace, role } = useSession();
  const ws = workspace.id;
  const source = useSource(ws, id);
  const mapping = useMapping(ws, id);
  const all = useSources(ws);
  const { ready } = useWorkspaceState();
  const firstRun = all.isSuccess && !ready; // the step count only during the first connect flow
  const qc = useQueryClient();
  const toast = useToast();

  const base = mapping.data?.confirmed ?? mapping.data?.suggested ?? null;
  const [kind, setKind] = useState<EntityKind | null>(null);
  const [choices, setChoices] = useState<Record<string, Choice>>({});
  const [initFrom, setInitFrom] = useState<string | null>(null);

  // Start from the stored mapping once (and again if a newer suggestion arrives untouched).
  useEffect(() => {
    if (!base || !source.data || initFrom === base.id) return;
    if (initFrom && Object.values(choices).some((c) => c.touched)) return;
    const next: Record<string, Choice> = {};
    for (const col of source.data.columns) {
      const m = base.spec.columns.find((c) => c.column === col.name);
      next[col.name] = { field: m ? m.field : '', split: m?.split, currency: m?.currency, touched: false };
    }
    setChoices(next);
    setKind((base.spec.entity_kind as EntityKind | null) ?? 'supplier');
    setInitFrom(base.id);
  }, [base, source.data, initFrom, choices]);

  const columns = useMemo(() => source.data?.columns ?? [], [source.data]);
  const suggestion = mapping.data?.suggested ?? null;
  const conf = (name: string) => suggestion?.spec.columns.find((c) => c.column === name)?.confidence ?? null;
  const headerCurrency = (name: string) => base?.spec.columns.find((c) => c.column === name)?.currency ?? suggestion?.spec.columns.find((c) => c.column === name)?.currency;
  const needs = (name: string) => {
    const c = choices[name];
    if (!c || c.field === '') return true;
    if (c.touched || mapping.data?.confirmed) return false;
    const k = conf(name);
    return k != null && k < SURE;
  };
  const needCount = columns.filter((c) => needs(c.name)).length;
  const nameCols = columns.filter((c) => choices[c.name]?.field === 'name').length;
  const kindByColumn = columns.some((c) => choices[c.name]?.field === 'kind');

  const spec = useMemo<S['MappingSpec'] | null>(() => {
    const cols = columns
      .map((c) => ({ column: c.name, field: choices[c.name]?.field, split: choices[c.name]?.split, currency: choices[c.name]?.currency }))
      .filter((c): c is { column: string; field: Field; split: string | undefined; currency: string | undefined } => !!c.field)
      .map((c) => ({
        column: c.column,
        field: c.field,
        ...(c.split ? { split: c.split } : {}),
        ...(c.field === 'annual_spend' && c.currency ? { currency: c.currency } : {}),
      }));
    if (!cols.length) return null;
    return { entity_kind: kindByColumn ? null : kind, columns: cols };
  }, [columns, choices, kind, kindByColumn]);
  const valid = !!spec && nameCols === 1;

  // Preview, debounced while the user changes columns.
  const [debounced, setDebounced] = useState<string | null>(null);
  const specKey = valid ? JSON.stringify(spec) : null;
  useEffect(() => {
    const t = setTimeout(() => setDebounced(specKey), 500);
    return () => clearTimeout(t);
  }, [specKey]);
  const preview = useMutation({
    mutationFn: (s: S['MappingSpec']) =>
      unwrap(api.POST('/v1/sources/{source_id}/mapping/preview', { params: { header: wsh(ws), path: { source_id: id }, query: { limit: 200 } }, body: s })),
  });
  const { mutate: runPreview } = preview;
  useEffect(() => {
    if (debounced) runPreview(JSON.parse(debounced) as S['MappingSpec']);
  }, [debounced, runPreview]);

  const suggest = useMutation({
    mutationFn: () => unwrap(api.POST('/v1/sources/{source_id}/mapping/suggest', { params: { header: wsh(ws), path: { source_id: id } } })),
    onSuccess: () => {
      setInitFrom(null);
      setChoices({});
      void qc.invalidateQueries({ queryKey: [ws, 'mapping', id] });
    },
    onError: (e) => toast({ text: errorText(e) }),
  });

  const confirm = useMutation({
    mutationFn: () => unwrap(api.POST('/v1/sources/{source_id}/mapping/confirm', { params: { header: wsh(ws), path: { source_id: id } }, body: spec! })),
    onSuccess: () => {
      void qc.invalidateQueries({ queryKey: [ws] });
      toast({ text: 'Monitoring started. Risks appear as the feeds match.' });
      navigate('/');
    },
  });

  const placed = usePlaced(preview.data?.entities);
  const { map } = useMap();
  const box = useMemo(() => boxOf(placed?.fc), [placed]);
  useEffect(() => {
    if (map && box) fitTo(map, box, sheetPadding('left', 460), 5);
  }, [map, box]);

  const siblings = (all.data?.items ?? []).filter((s) => source.data && s.connection_id === source.data.connection_id && s.external_id === source.data.external_id);
  const fields = mapping.data?.fields ?? [];
  const offered = fields.filter((f) => f.field !== 'ignore' && (kindByColumn || !kind || f.kinds.length === 0 || f.kinds.includes(kind)));
  const editor = canEdit(role);
  const waiting = mapping.isSuccess && !base;

  const setField = (col: string, field: Field | '') =>
    setChoices((c) => ({ ...c, [col]: { ...c[col], field, currency: field === 'annual_spend' ? (c[col]?.currency ?? headerCurrency(col)) : undefined, touched: true } }));

  const summaryText = !base
    ? 'Reading your column headers and a few sample values.'
    : needCount
      ? `${columns.length - needCount} of ${columns.length} columns matched. ${needCount === 1 ? 'One needs' : `${needCount} need`} your choice.`
      : `All ${columns.length} columns are matched. Check anything that looks off, then start monitoring.`;

  const card = (
    <PreviewCard
      preview={preview.data ?? null}
      loading={preview.isPending || (valid && debounced !== specKey)}
      error={preview.error}
      valid={valid}
      nameCols={nameCols}
      kind={kindByColumn ? null : kind}
      confirming={confirm.isPending}
      confirmError={confirm.error}
      canConfirm={editor && valid}
      onConfirm={() => confirm.mutate()}
    />
  );

  return (
    <>
      <EntitiesLayer id="preview" data={placed?.fc ?? null} ring labels="names" />
      <aside className="sheet w460 tall split glass" aria-label="Check how we read your sheet">
        <Grab />
        <div className="sbody">
        <SheetHead back={{ to: '/connect', label: 'Sources', onClick: () => navigate('/connect') }} right={firstRun ? <span className="t12" style={{ paddingRight: 12 }}>Step 2 of 3</span> : undefined} />
        {source.isPending || mapping.isPending ? (
          <SkeletonRows n={6} h={48} />
        ) : source.isError || mapping.isError ? (
          <ErrorState error={source.error ?? mapping.error} retry={() => { void source.refetch(); void mapping.refetch(); }} />
        ) : (
          <>
            <div style={{ display: 'flex', flexDirection: 'column', gap: 6, marginTop: -6 }}>
              <h1 className="h1">Check how we read your sheet</h1>
              <span className="body15">{summaryText}</span>
            </div>
            {siblings.length > 1 && (
              <div className="segw" role="group" aria-label="Tabs in your sheet">
                {siblings.map((s) => (
                  <button key={s.id} type="button" className={`sg${s.id === id ? ' on' : ''}`} aria-pressed={s.id === id} onClick={() => navigate(`/sources/${s.id}/confirm`)}>
                    <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{s.name}</span>
                    {s.id === id && needCount > 0 && <span className="nb">{needCount}</span>}
                  </button>
                ))}
              </div>
            )}
            {waiting ? (
              <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
                <SkeletonRows n={4} h={48} />
                <span className="t12">{source.data.status === 'error' ? source.data.status_reason ?? 'Reading the sheet failed.' : 'This takes a few seconds. The page updates on its own.'}</span>
              </div>
            ) : (
              <>
                {!kindByColumn && (
                  <div className="mr">
                    <div style={{ minWidth: 0 }}>
                      <div className="cn">Every row is a</div>
                      <div className="sm">{source.data.row_count != null ? `${fmtInt(source.data.row_count)} rows in ${source.data.name}` : source.data.name}</div>
                    </div>
                    <select className="sel" aria-label="Every row is a" value={kind ?? 'supplier'} onChange={(e) => setKind(e.target.value as EntityKind)} disabled={!editor}>
                      {KIND_LIST.map((k) => <option key={k} value={k}>{KINDS[k].one.toLowerCase()}</option>)}
                    </select>
                  </div>
                )}
                <div style={{ display: 'flex', flexDirection: 'column' }}>
                  {columns.map((col) => {
                    const c = choices[col.name];
                    const need = needs(col.name);
                    const k = conf(col.name);
                    return (
                      <div key={col.index} className={`mr${need ? ' need' : ''}`}>
                        <div style={{ minWidth: 0 }}>
                          <div className="cn">{col.name || `Column ${col.index + 1}`}</div>
                          <div className="sm">{col.samples.length ? col.samples.slice(0, 3).join(', ') : 'No values in the first rows'}</div>
                        </div>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                          <select
                            className={`sel${need ? ' ask' : ''}`}
                            aria-label={`Read ${col.name} as`}
                            value={c?.field ?? ''}
                            disabled={!editor}
                            onChange={(e) => setField(col.name, e.target.value as Field | '')}
                          >
                            <option value="">Choose…</option>
                            {offered.map((f) => <option key={f.field} value={f.field}>{f.label}</option>)}
                            {c?.field && !offered.some((f) => f.field === c.field) && c.field !== 'ignore' && <option value={c.field}>{fields.find((f) => f.field === c.field)?.label ?? c.field}</option>}
                            <option value="ignore">Ignore it</option>
                          </select>
                          <span className="cf" style={need ? { color: '#0F55CF' } : undefined}>
                            {need ? 'Needs you' : c?.touched ? 'Your choice' : k != null ? `${Math.round(k * 100)}% sure` : mapping.data.confirmed ? 'Confirmed' : 'Your choice'}
                          </span>
                          {c?.field === 'annual_spend' && c.currency && <span className="cf">currency from the header: {c.currency}</span>}
                        </div>
                      </div>
                    );
                  })}
                </div>
              </>
            )}
          </>
        )}
        </div>
        {source.isSuccess && mapping.isSuccess && (
          <div className="sfoot">
            {!waiting && (
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 8 }}>
                  <span className="t12">
                    {suggestion?.model === 'heuristic' ? 'Matched from the headers.' : suggestion?.model ? 'Suggested by AI.' : ''} Ignored columns are never read again.
                  </span>
                  {editor && (
                    <button type="button" className="b plain" style={{ fontSize: 13, height: 44, padding: '0 10px', flex: 'none' }} disabled={suggest.isPending} onClick={() => suggest.mutate()}>
                      {suggest.isPending && <span className="spin" />}Suggest again
                    </button>
                  )}
                </div>
            )}
            <div className="fold">{card}</div>
          </div>
        )}
      </aside>
      {base && <section className="side glass" aria-label="What happens when you confirm">{card}</section>}
    </>
  );
}

function PreviewCard({
  preview,
  loading,
  error,
  valid,
  nameCols,
  kind,
  confirming,
  confirmError,
  canConfirm,
  onConfirm,
}: {
  preview: S['MappingPreview'] | null;
  loading: boolean;
  error: unknown;
  valid: boolean;
  nameCols: number;
  kind: EntityKind | null;
  confirming: boolean;
  confirmError: unknown;
  canConfirm: boolean;
  onConfirm: () => void;
}) {
  const feats = preview?.entities.features ?? [];
  const countries = new Set(feats.flatMap((f) => f.properties.countries));
  const ports = new Set(feats.flatMap((f) => f.properties.ports));
  const things = kind ? KINDS[kind].many : 'entities';
  return (
    <>
      <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
        <span className="lbl">When you confirm</span>
        {!valid ? (
          <span className="h3">{nameCols === 0 ? 'Pick the column with the names' : 'Only one column can be the name'}</span>
        ) : loading && !preview ? (
          <span className="sk" style={{ height: 22, width: 220 }} />
        ) : error ? (
          <span className="t13">{errorText(error)}</span>
        ) : preview ? (
          <>
            <span className="h3">{fmtInt(feats.length)} {things} from {plural(preview.rows_read, 'row')}</span>
            <span className="t13">
              {countries.size || ports.size
                ? `We will watch ${[countries.size ? plural(countries.size, 'country', 'countries') : '', ports.size ? plural(ports.size, 'port') : ''].filter(Boolean).join(' and ')}. Nothing else.`
                : 'None of these rows has a country, port or position yet.'}
            </span>
          </>
        ) : null}
      </div>
      {valid && preview && preview.problems.length > 0 && (
        <div style={{ display: 'flex', flexDirection: 'column' }}>
          {preview.problems.slice(0, 4).map((p, i) => (
            <div className="fr" key={i}>
              <span className="pill sm l-none">{p.row_number}</span>
              <span style={{ minWidth: 0, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                {p.column}: “{p.value}” {p.message}
              </span>
            </div>
          ))}
          {preview.problems.length > 4 && <span className="t12">{plural(preview.problems.length - 4, 'more value')} left out, never guessed.</span>}
        </div>
      )}
      {valid && preview && <span className="t12" style={{ marginTop: -4 }}>Blue rings on the map show where they go. Risks appear once the feeds are matched.</span>}
      {confirmError != null && (
        <div className="note" role="alert"><Icon name="warning" style={{ flex: 'none', marginTop: 1 }} />{confirmError instanceof ApiError && confirmError.errors.length ? confirmError.errors.map((e) => e.message).join(' ') : errorText(confirmError)}</div>
      )}
      <button type="button" className="b pri" disabled={!canConfirm || confirming} onClick={onConfirm}>
        {confirming && <span className="spin" />}Start monitoring
      </button>
    </>
  );
}
