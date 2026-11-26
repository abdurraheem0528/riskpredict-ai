// Numbers that count up (or down) to a new value over 600 ms, ease-out, after a live update.
// The first value, and any change that is not live (a filter, a refetch on focus), is shown
// at once; reduced motion and a hidden tab land at once too (motion.ts).
import { useLayoutEffect, useRef, type CSSProperties } from 'react';
import { D4, EASE_OUT, tween } from '../lib/motion';
import { levelOf, scoreText } from '../lib/format';
import { liveRecent } from './bus';

/** Animate `from → to`, calling `show` with each value; returns a cancel. */
function count(from: number, to: number, show: (v: number) => void, done: () => void) {
  return tween(D4, (p) => show(from + (to - from) * p), { ease: EASE_OUT, done });
}

export function CountUp({
  value,
  format = (n) => String(Math.round(n)),
  className,
  style,
  title,
}: {
  value: number;
  format?: (n: number) => string;
  className?: string;
  style?: CSSProperties;
  title?: string;
}) {
  const ref = useRef<HTMLSpanElement>(null);
  const shown = useRef(value);
  const fmt = useRef(format);
  useLayoutEffect(() => {
    fmt.current = format;
  });
  useLayoutEffect(() => {
    const from = shown.current;
    const el = ref.current;
    const set = (v: number) => {
      shown.current = v;
      if (el) el.textContent = fmt.current(v);
    };
    if (from === value) return;
    if (!liveRecent('count')) {
      set(value);
      return;
    }
    const tw = count(from, value, set, () => set(value));
    return () => tw.cancel();
  }, [value]);
  return (
    <span ref={ref} className={className} style={style} title={title}>
      {format(shown.current)}
    </span>
  );
}

/** A score in its level pill (as ScorePill), counting to a new score; the level follows the count. */
export function CountPill({ score, small = false }: { score: number; small?: boolean }) {
  const ref = useRef<HTMLSpanElement>(null);
  const shown = useRef(score);
  useLayoutEffect(() => {
    const from = shown.current;
    const el = ref.current;
    const set = (v: number) => {
      shown.current = v;
      if (!el) return;
      el.textContent = scoreText(v);
      el.className = `pill${small ? ' sm' : ''} l-${levelOf(v)}`;
    };
    if (from === score) return;
    if (!liveRecent('count')) {
      set(score);
      return;
    }
    const tw = count(from, score, set, () => set(score));
    return () => tw.cancel();
  }, [score, small]);
  return (
    <span ref={ref} className={`pill${small ? ' sm' : ''} l-${levelOf(shown.current)}`}>
      {scoreText(shown.current)}
    </span>
  );
}
