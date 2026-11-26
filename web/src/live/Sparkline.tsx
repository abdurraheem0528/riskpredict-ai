// The top story's recent scores (its `history`, last 16 points) on the Overview card:
// 160 × 44, a line over a faint area, a dot at the end. When a new point arrives the old
// points glide to their new places over 320 ms while the new end grows out of the old one.
// Hidden with fewer than 3 points.
import { useLayoutEffect, useRef } from 'react';
import { D3, tween } from '../lib/motion';
import { levelOf } from '../lib/format';

export const SPARK_W = 160;
export const SPARK_H = 44;
export const SPARK_POINTS = 16;

type Pt = [number, number];

/** x, y of every value for a window of values (exported for tests). */
export function sparkLayout(values: readonly number[]): Pt[] {
  const lo = Math.min(...values) - 2, hi = Math.max(...values) + 2;
  const dx = SPARK_W / Math.max(1, values.length - 1);
  return values.map((v, i) => [i * dx, SPARK_H - 3 - ((v - lo) / (hi - lo || 1)) * (SPARK_H - 10)]);
}

function paths(pts: Pt[]) {
  const d = pts.map(([x, y], i) => `${i ? 'L' : 'M'}${x.toFixed(1)} ${y.toFixed(1)}`).join('');
  const [ex, ey] = pts[pts.length - 1];
  return { line: d, area: `${d}L${ex.toFixed(1)} ${SPARK_H}L${pts[0][0].toFixed(1)} ${SPARK_H}Z`, end: [ex, ey] as Pt };
}

export function Sparkline({ id, values, label }: { id: string; values: readonly number[]; label: string }) {
  const win = values.slice(-SPARK_POINTS);
  const line = useRef<SVGPathElement>(null);
  const area = useRef<SVGPathElement>(null);
  const dot = useRef<SVGCircleElement>(null);
  const prev = useRef<{ id: string; values: readonly number[] } | null>(null);
  const key = win.join(',');

  useLayoutEffect(() => {
    const draw = (pts: Pt[]) => {
      const p = paths(pts);
      line.current?.setAttribute('d', p.line);
      area.current?.setAttribute('d', p.area);
      dot.current?.setAttribute('cx', p.end[0].toFixed(1));
      dot.current?.setAttribute('cy', p.end[1].toFixed(1));
    };
    const all = values;
    const before = prev.current;
    prev.current = { id, values: all };
    if (win.length < 3) return;
    const to = sparkLayout(win);
    // One more point for the same risk: glide from the old window to the new one.
    const grew = before && before.id === id && all.length === before.values.length + 1 && before.values.every((v, i) => v === all[i]);
    if (!grew || before.values.length < 2) {
      draw(to);
      return;
    }
    const oldWin = before.values.slice(-SPARK_POINTS);
    const from = sparkLayout(oldWin);
    // Index i of the new window was index i + shift of the old one (shift 1 once the window is full).
    const shift = oldWin.length + 1 - win.length;
    const fromOf = (i: number): Pt => {
      const j = i + shift;
      if (j < from.length) return from[Math.max(0, j)];
      return from[from.length - 1]; // the new end grows out of the old end
    };
    draw(to.map((_, i) => fromOf(i))); // before paint: React has already drawn the end state
    const tw = tween(D3, (p) => draw(to.map(([x1, y1], i) => {
      const [x0, y0] = fromOf(i);
      return [x0 + (x1 - x0) * p, y0 + (y1 - y0) * p];
    })), { done: () => draw(to) });
    return () => tw.cancel();
    // `key` stands for `win`; `values` and `id` are read through `prev`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [id, key]);

  if (win.length < 3) return null;
  const init = paths(sparkLayout(win));
  const last = win[win.length - 1];
  return (
    <svg className="rp-spark" data-lvl={levelOf(last)} width={SPARK_W} height={SPARK_H} viewBox={`0 0 ${SPARK_W} ${SPARK_H}`} role="img" aria-label={label}>
      <line className="bl" x1="0" x2={SPARK_W} y1={SPARK_H - 0.5} y2={SPARK_H - 0.5} />
      <path ref={area} className="ar" d={init.area} />
      <path ref={line} className="ln" d={init.line} />
      <circle ref={dot} className="dt" r="3" cx={init.end[0].toFixed(1)} cy={init.end[1].toFixed(1)} />
    </svg>
  );
}
