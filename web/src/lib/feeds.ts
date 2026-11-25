import type { components } from '../api/schema';
import { feedName } from './format';

type Health = { sources: components['schemas']['SourceHealth'][] };

/** A feed is live when its last run worked (ok, or ran and found nothing new). */
export const isLive = (s: components['schemas']['SourceHealth']) => s.status === 'ok' || s.status === 'empty' || s.status === 'partial';

export function feedSummary(h: Health | undefined) {
  const sources = h?.sources ?? [];
  const live = sources.filter(isLive);
  const failing = sources.filter((s) => s.status === 'error');
  const names = [...new Set(sources.map((s) => feedName(s.source)))];
  return {
    total: sources.length,
    live: live.length,
    failing,
    names,
    text: sources.length ? `${live.length} of ${sources.length} feeds live` : 'No feeds have run yet',
    color: sources.length && live.length === sources.length ? '#1A9E5A' : failing.length ? '#B3200F' : '#A3A7AE',
  };
}
