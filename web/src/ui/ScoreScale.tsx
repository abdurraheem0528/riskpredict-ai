// The score scale: 0–100 in four bands sized to the level thresholds (levelOf): Low 0–39,
// Medium 40–59, High 60–79, Critical 80–100, so 4 : 2 : 2 : 2 with 2 px gaps. The bands are
// the level fills at reduced strength (Low neutral grey); a dot outlined in the sheet colour
// marks the score. Large on the risk page (with the level names), mini under a row's number,
// a strip of ticks on the Overview, and a 48 × 4 copy in the map legend.
import type { CSSProperties } from 'react';
import { LEVELS, levelOf, scoreText, type Level } from '../lib/format';

export const BANDS: readonly { level: Level; from: number; to: number }[] = [
  { level: 'low', from: 0, to: 40 },
  { level: 'med', from: 40, to: 60 },
  { level: 'high', from: 60, to: 80 },
  { level: 'crit', from: 80, to: 100 },
];
const GAP = 2;

/**
 * Where a score sits: its level, its band (0–3), and its x on the bar as a CSS length. The
 * bands share the bar's width less the three gaps in proportion to their span, so x is that
 * share of the score plus one gap per band boundary passed.
 */
export function scalePlace(score: number): { level: Level; band: number; at: number; left: string } {
  const s = Math.max(0, Math.min(100, score));
  const level = levelOf(s);
  const band = BANDS.findIndex((b) => b.level === level);
  const at = s / 100;
  return { level, band, at, left: `calc((100% - ${GAP * (BANDS.length - 1)}px) * ${at} + ${GAP * band}px)` };
}

/** The four bands alone (the legend's copy, the strip's base). */
export function ScoreBands({ width, height = 4, className = '' }: { width?: number; height?: number; className?: string }) {
  return (
    <span className={`sbar ${className}`} style={{ width, height }} aria-hidden="true">
      {BANDS.map((b) => (
        <i key={b.level} className={`bd ${b.level}`} style={{ flexGrow: b.to - b.from }} />
      ))}
    </span>
  );
}

function Names({ on }: { on: Level | null }) {
  return (
    <span className="snames" aria-hidden="true">
      {BANDS.map((b) => (
        <span key={b.level} className={b.level === on ? 'on' : undefined} style={{ flexGrow: b.to - b.from }}>
          {LEVELS[b.level].name}
        </span>
      ))}
    </span>
  );
}

/** One score on the scale. `large`: 6 px bar, 14 px dot, level names; `mini`: 3 px, 7 px dot. */
export function ScoreScale({ score, size = 'large', style }: { score: number; size?: 'large' | 'mini'; style?: CSSProperties }) {
  const p = scalePlace(score);
  const label = `Score ${scoreText(score)} out of 100: ${LEVELS[p.level].name}`;
  return (
    <span className={`scale ${size}`} style={style} role="img" aria-label={label}>
      <span className="strack">
        <ScoreBands height={size === 'large' ? 6 : 3} />
        <i className={`scdot ${p.level}`} style={{ left: p.left }} />
      </span>
      {size === 'large' && <Names on={p.level} />}
    </span>
  );
}

/** Every open story as a 2 × 14 tick on the scale, the highest written above its tick. */
export function ScoreStrip({ scores }: { scores: readonly number[] }) {
  const sorted = [...scores].sort((a, b) => b - a);
  const top = sorted[0];
  const label = sorted.length
    ? `${sorted.length} scores out of 100, from ${scoreText(sorted[sorted.length - 1])} to ${scoreText(top)}`
    : 'No open scores';
  return (
    <span className="sstrip" role="img" aria-label={label}>
      <span className="sticks">
        {top != null && (
          <b className="stop" style={{ left: scalePlace(top).left }}>
            {scoreText(top)}
          </b>
        )}
        {sorted.map((s, i) => (
          <i key={i} className={`stk ${levelOf(s)}`} style={{ left: scalePlace(s).left }} />
        ))}
      </span>
      <ScoreBands height={4} />
      <Names on={top != null ? levelOf(top) : null} />
    </span>
  );
}
