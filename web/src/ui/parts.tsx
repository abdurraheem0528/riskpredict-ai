import type { ReactNode } from 'react';
import { Icon } from './Icon';
import { errorText } from '../api/errors';
import { LEVELS, levelOf, scoreText } from '../lib/format';
import { useMap, fitTo, WORLD, sheetPadding } from '../map/MapRoot';
import type { StreamStatus } from '../api/stream';

export function ScorePill({ score, small = false }: { score: number; small?: boolean }) {
  return <span className={`pill${small ? ' sm' : ''} l-${levelOf(score)}`}>{scoreText(score)}</span>;
}

export function LevelChip({ score, children }: { score: number; children?: ReactNode }) {
  return <span className={`lvl l-${levelOf(score)}`}>{children ?? LEVELS[levelOf(score)].name}</span>;
}

export function SkeletonRows({ n = 4, h = 44 }: { n?: number; h?: number }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }} aria-busy="true" aria-label="Loading">
      {Array.from({ length: n }, (_, i) => (
        <div key={i} className="sk" style={{ height: h, opacity: 1 - i * 0.15 }} />
      ))}
    </div>
  );
}

/** A failed load: what happened, and one way to try again. */
export function ErrorState({ error, retry, title = 'This did not load' }: { error: unknown; retry?: () => void; title?: string }) {
  return (
    <div role="alert" style={{ display: 'flex', flexDirection: 'column', gap: 10, alignItems: 'flex-start' }}>
      <span style={{ display: 'inline-flex', gap: 8, alignItems: 'center', fontWeight: 600 }}>
        <Icon name="warning" style={{ color: '#B3200F' }} />
        {title}
      </span>
      <span className="t13">{errorText(error)}</span>
      {retry && (
        <button type="button" className="b sec2" onClick={retry}>
          <Icon name="refresh" />
          Try again
        </button>
      )}
    </div>
  );
}

export function LiveDot({ status }: { status: StreamStatus }) {
  const map = { live: ['Live', '#1A9E5A'], connecting: ['Connecting', '#A3A7AE'], retrying: ['Reconnecting', '#A3A7AE'], off: ['Offline', '#A3A7AE'] } as const;
  const [label, color] = map[status];
  return (
    <span className="t12" style={{ display: 'inline-flex', alignItems: 'center', gap: 6 }}>
      <span style={{ width: 6, height: 6, borderRadius: 3, background: color }} aria-hidden="true" />
      {label}
    </span>
  );
}

/** A small line chart with an optional reference line (System-Data: no grid on small charts). */
export function Spark({
  values,
  width = 200,
  height = 56,
  color = '#141518',
  area = false,
  ref60 = true,
  label,
  domain = [0, 100],
}: {
  values: number[];
  width?: number;
  height?: number;
  color?: string;
  area?: boolean;
  ref60?: boolean;
  label: string;
  domain?: [number, number];
}) {
  if (values.length === 0) return null;
  const [lo, hi] = domain;
  const span = hi - lo || 1;
  const y = (v: number) => height - 4 - ((v - lo) / span) * (height - 8);
  const pts = values.length === 1 ? [values[0], values[0]] : values;
  const step = width / (pts.length - 1);
  const line = pts.map((v, i) => `${(i * step).toFixed(1)},${y(v).toFixed(1)}`).join(' ');
  return (
    <svg viewBox={`0 0 ${width} ${height}`} width="100%" height={height} preserveAspectRatio="none" role="img" aria-label={label}>
      {ref60 && lo <= 60 && hi >= 60 && <line x1="0" y1={y(60)} x2={width} y2={y(60)} stroke="#C9CCD1" strokeDasharray="2 3" vectorEffect="non-scaling-stroke" />}
      {area && <polygon points={`${line} ${width},${height} 0,${height}`} fill={color} fillOpacity={0.1} />}
      <polyline points={line} fill="none" stroke={color} strokeWidth={1.75} vectorEffect="non-scaling-stroke" strokeLinejoin="round" />
    </svg>
  );
}

/** Zoom in, zoom out, fit (System-Nav: map controls, 44 per button, hidden on phones). */
export function MapControls({ fit, fitLabel = 'Fit everything', low = false }: { fit?: [[number, number], [number, number]] | null; fitLabel?: string; low?: boolean }) {
  const { map } = useMap();
  if (!map) return null;
  return (
    <div className={`ctrl glass${low ? ' low' : ''}`}>
      <button type="button" className="mb" aria-label="Zoom in" title="Zoom in" onClick={() => map.zoomIn()}>
        <Icon name="add" size={18} stroke={1.75} />
      </button>
      <hr />
      <button type="button" className="mb" aria-label="Zoom out" title="Zoom out" onClick={() => map.zoomOut()}>
        <Icon name="minus" size={18} stroke={1.75} />
      </button>
      <hr />
      <button type="button" className="mb" aria-label={fitLabel} title={fitLabel} onClick={() => fitTo(map, fit ?? WORLD, sheetPadding('left', 344), fit ? 5 : 3)}>
        <Icon name="fit" size={18} />
      </button>
    </div>
  );
}

export function SheetHead({ back, right }: { back: { to: string; label: string; onClick: () => void }; right?: ReactNode }) {
  return (
    <div className="sheethead">
      <a className="b back" href={back.to} onClick={(e) => { e.preventDefault(); back.onClick(); }}>
        <Icon name="back" stroke={2} />
        {back.label}
      </a>
      {right}
    </div>
  );
}
