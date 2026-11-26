// The green dot in the sheet's top row and when the scores last changed: "Scored just now",
// "12 s ago", "3 min ago". The dot breathes while the feed is live; it holds still when the
// tab is hidden (html.rp-hidden pauses it), when the scores are stale (polling not live, or
// no scoring for STALE_MS), and under reduced motion (live.css).
import { useEffect, useState } from 'react';
import type { StreamStatus } from '../api/stream';
import { STALE_MS, scoredText } from './plan';

const STATUS: Record<StreamStatus, string> = { live: 'Live', connecting: 'Connecting', retrying: 'Reconnecting', off: 'Offline' };

/** Re-render often enough for the text: every second under a minute, then every 30 s; never while hidden. */
function useNow(iso: string | null | undefined) {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const tick = () => {
      if (timer) clearTimeout(timer);
      timer = null;
      if (document.hidden) return;
      const t = Date.now();
      setNow(t);
      const age = iso ? t - new Date(iso).getTime() : Infinity;
      timer = setTimeout(tick, age < 60_000 ? 1000 - (t % 1000) + 5 : 30_000);
    };
    tick();
    document.addEventListener('visibilitychange', tick);
    return () => {
      if (timer) clearTimeout(timer);
      document.removeEventListener('visibilitychange', tick);
    };
  }, [iso]);
  return now;
}

export function LiveDot({ status, scoredAt }: { status: StreamStatus; scoredAt?: string | null }) {
  const now = useNow(scoredAt);
  const live = status === 'live';
  const stale = !scoredAt || now - new Date(scoredAt).getTime() > STALE_MS;
  const text = live && scoredAt !== undefined ? scoredText(scoredAt, now) : STATUS[status];
  return (
    <span className={`rp-live${live ? (stale ? ' still' : '') : ' off'}`} role="status" aria-live="off" title={live ? STATUS.live : undefined}>
      <i aria-hidden="true" />
      {text}
    </span>
  );
}
