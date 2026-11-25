// Jump to · ⌘K (System-Nav): risks, entities and actions in one search. Dims the map,
// never replaces it. ↑↓ to move, ↵ to open, esc to close.
import { useEffect, useMemo, useRef, useState, type KeyboardEvent } from 'react';
import { Icon } from '../ui/Icon';
import { navigate } from '../lib/router';
import { useEntities, useRisks } from '../api/queries';
import { KINDS, levelOf, scoreText } from '../lib/format';
import { useSession } from './Session';
import { useWorkspaceState } from './TopBar';

type Row = { key: string; group: string; label: string; hint?: string; pill?: number; go: () => void };

export function JumpTo({ onClose }: { onClose: () => void }) {
  const { workspace } = useSession();
  const { ready } = useWorkspaceState();
  const [q, setQ] = useState('');
  const [debounced, setDebounced] = useState('');
  const [sel, setSel] = useState(0);
  const input = useRef<HTMLInputElement>(null);
  useEffect(() => {
    const t = setTimeout(() => setDebounced(q.trim()), 180);
    return () => clearTimeout(t);
  }, [q]);
  useEffect(() => input.current?.focus(), []);

  const risks = useRisks(workspace.id, { limit: 50 });
  const entities = useEntities(workspace.id, debounced, ready && debounced.length > 1);

  const rows = useMemo<Row[]>(() => {
    const needle = q.trim().toLowerCase();
    const out: Row[] = [];
    const rs = (risks.data?.pages.flatMap((p) => p.items) ?? [])
      .filter((r) => !needle || r.title.toLowerCase().includes(needle) || r.entity.name.toLowerCase().includes(needle))
      .slice(0, needle ? 6 : 4);
    for (const r of rs) out.push({ key: `r:${r.id}`, group: 'Risks', label: r.title, hint: r.entity.name, pill: r.score, go: () => navigate(`/risks/${r.id}`) });
    if (debounced.length > 1)
      for (const e of (entities.data?.items ?? []).slice(0, 5))
        out.push({
          key: `e:${e.id}`,
          group: 'Your entities',
          label: e.name,
          hint: `${KINDS[e.kind].one}${e.open_risk_count ? ` · ${e.open_risk_count} open ${e.open_risk_count === 1 ? 'risk' : 'risks'}` : ''}`,
          go: () => navigate(`/register?entity=${e.id}`),
        });
    const actions: Row[] = [
      { key: 'a:connect', group: 'Actions', label: 'Connect a source', go: () => navigate('/connect') },
      ...(ready
        ? [
            { key: 'a:whatif', group: 'Actions', label: 'Run a what-if', go: () => navigate('/scenarios') },
            { key: 'a:register', group: 'Actions', label: 'Open the register', go: () => navigate('/register') },
          ]
        : []),
    ];
    out.push(...actions.filter((a) => !needle || a.label.toLowerCase().includes(needle)));
    return out;
  }, [q, debounced, risks.data, entities.data, ready]);

  useEffect(() => setSel(0), [q]);

  const onKey = (e: KeyboardEvent) => {
    if (e.key === 'ArrowDown') {
      e.preventDefault();
      setSel((s) => Math.min(rows.length - 1, s + 1));
    } else if (e.key === 'ArrowUp') {
      e.preventDefault();
      setSel((s) => Math.max(0, s - 1));
    } else if (e.key === 'Enter') {
      e.preventDefault();
      const r = rows[sel];
      if (r) {
        onClose();
        r.go();
      }
    } else if (e.key === 'Escape') {
      e.preventDefault();
      onClose();
    }
  };

  let lastGroup = '';
  return (
    <>
      <div className="scrim" onClick={onClose} aria-hidden="true" />
      <div className="palette" role="dialog" aria-modal="true" aria-label="Jump to" onKeyDown={onKey}>
        <div className="ph">
          <Icon name="search" size={20} stroke={1.25} />
          <input
            ref={input}
            value={q}
            onChange={(e) => setQ(e.target.value)}
            placeholder="Risks, suppliers and actions"
            aria-label="Search"
            role="combobox"
            aria-expanded="true"
            aria-controls="jump-list"
            aria-activedescendant={rows[sel] ? `jump-${sel}` : undefined}
          />
          <span className="kbd">esc</span>
        </div>
        <div className="pl" id="jump-list" role="listbox" aria-label="Results">
          {rows.length === 0 && <div className="t13" style={{ padding: '14px 18px' }}>Nothing matches “{q}”.</div>}
          {rows.map((r, i) => {
            const head = r.group !== lastGroup ? <div className="mh" key={`h:${r.group}`}>{r.group}</div> : null;
            lastGroup = r.group;
            return [
              head,
              <button
                key={r.key}
                id={`jump-${i}`}
                type="button"
                role="option"
                aria-selected={i === sel}
                className="mi"
                onMouseMove={() => setSel(i)}
                onClick={() => {
                  onClose();
                  r.go();
                }}
              >
                <span style={{ display: 'flex', flexDirection: 'column', minWidth: 0 }}>
                  <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.label}</span>
                  {r.hint && <small style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.hint}</small>}
                </span>
                <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center' }}>
                  {r.pill != null && <span className={`pill sm l-${levelOf(r.pill)}`}>{scoreText(r.pill)}</span>}
                  {i === sel && <span className="kbd">↵</span>}
                </span>
              </button>,
            ];
          })}
        </div>
      </div>
    </>
  );
}
