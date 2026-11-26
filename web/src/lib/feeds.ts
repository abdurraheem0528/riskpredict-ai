import type { components } from '../api/schema';
import { feedName } from './format';

type SourceHealth = components['schemas']['SourceHealth'];
type Health = { sources: SourceHealth[] };

/** Live: the last run worked (ok, or ran and found nothing new). Anything else is down. */
export const isLive = (s: SourceHealth) => s.status === 'ok' || s.status === 'empty' || s.status === 'partial';

const STATUS_TEXT: Record<SourceHealth['status'], string> = {
  ok: 'live', empty: 'live', partial: 'partly live', throttled: 'paused by its rate limit', error: 'failing', skipped: 'skipped',
};

export function feedSummary(h: Health | undefined) {
  const sources = h?.sources ?? [];
  const live = sources.filter(isLive);
  const down = sources.filter((s) => !isLive(s));
  const names = [...new Set(sources.map((s) => feedName(s.source)))];
  return {
    total: sources.length,
    live: live.length,
    down,
    names,
    text: sources.length ? `${live.length} of ${sources.length} feeds live` : 'No feeds have run yet',
    /** The dot's state (FeedStatus): grey before any run, amber with a feed down, else green. */
    tone: (!sources.length ? 'none' : down.length ? 'warn' : 'live') as 'none' | 'warn' | 'live',
    /** "NewsData: paused by its rate limit (daily limit reached)" per down feed. */
    downText: down.map((s) => {
      const why = s.last_error ? ` (${s.last_error.length > 60 ? `${s.last_error.slice(0, 59)}…` : s.last_error})` : '';
      return `${feedName(s.source)}: ${STATUS_TEXT[s.status]}${why}`;
    }),
  };
}
