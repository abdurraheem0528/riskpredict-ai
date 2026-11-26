// Live updates by polling (openapi.yaml, GET /v1/events). The API is serverless, so there is
// no stream to hold open: about every 15 s the dashboard asks for the events after the last
// id it saw. Only while the tab is visible (Page Visibility API): a hidden tab stops polling
// at once, so an unwatched dashboard never keeps the database awake, and polls right away
// when it is shown again, catching up from its cursor. `resync` (the cursor fell out of the
// 24 h retention) makes the caller refetch everything. Errors back off 1 s → 30 s with jitter.
import type { components } from './schema';
import { api, unwrap } from './client';
import { wsh } from './workspace';

type S = components['schemas'];
export type StreamEvents = {
  'risk.created': S['RiskEvent'];
  'risk.updated': S['RiskEvent'];
  'risk.resolved': S['RiskEvent'];
  'alert.created': S['Alert'];
  'source.sync_started': S['SourceEvent'];
  'source.synced': S['SourceEvent'];
  'source.sync_failed': S['SourceEvent'];
  'mapping.suggested': S['MappingEvent'];
  'connection.updated': S['ConnectionEvent'];
  'feeds.updated': S['FeedsUpdatedEvent'];
  resync: Record<string, never>;
};
export type StreamEventName = keyof StreamEvents;
export const STREAM_EVENTS = [
  'risk.created', 'risk.updated', 'risk.resolved', 'alert.created', 'source.sync_started', 'source.synced',
  'source.sync_failed', 'mapping.suggested', 'connection.updated', 'feeds.updated', 'resync',
] as const satisfies readonly StreamEventName[];

export type StreamStatus = 'connecting' | 'live' | 'retrying' | 'off';
type Handler = <K extends StreamEventName>(name: K, data: StreamEvents[K]) => void;

/**
 * Everything one poll brought, for a caller that plays live changes (src/live/player.ts).
 * `initial`: the first poll after start (the page was just loaded). `catchUp`: the first
 * poll after the tab was hidden. `resync`: the cursor fell out of retention (refetch all).
 */
export type StreamBatch = {
  events: { name: StreamEventName; data: unknown }[];
  initial: boolean;
  catchUp: boolean;
  resync: boolean;
};

export const POLL_MS = 15_000;
const MAX_PAGES = 10; // per poll, when the API says `more`

export function backoffMs(attempt: number, random = Math.random) {
  const base = Math.min(30_000, 1000 * 2 ** Math.max(0, attempt));
  return Math.round(base / 2 + (random() * base) / 2);
}

const KNOWN = new Set<string>(STREAM_EVENTS);

export class LiveStream {
  private timer: ReturnType<typeof setTimeout> | null = null;
  private cursor: number | null = null;
  private attempt = 0;
  private stopped = true;
  private polling = false;
  private wasHidden = false;
  status: StreamStatus = 'off';

  /**
   * `onEvent` gets each event as it is read. With `onBatch`, each poll's events are passed
   * to it instead, once the poll has read every page.
   */
  constructor(
    private workspaceId: string,
    private onEvent: Handler,
    private onStatus: (s: StreamStatus) => void,
    private doc: Pick<Document, 'visibilityState' | 'addEventListener' | 'removeEventListener'> = document,
    private onBatch?: (batch: StreamBatch) => void,
  ) {}

  start() {
    if (!this.stopped) return;
    this.stopped = false;
    this.doc.addEventListener('visibilitychange', this.onVisibility);
    if (this.visible()) void this.poll();
    else this.set('off');
  }

  stop() {
    this.stopped = true;
    this.doc.removeEventListener('visibilitychange', this.onVisibility);
    this.clear();
    this.set('off');
  }

  private visible() {
    return this.doc.visibilityState !== 'hidden';
  }

  private onVisibility = () => {
    if (this.stopped) return;
    if (this.visible()) {
      void this.poll(); // catch up at once
    } else {
      this.wasHidden = true;
      this.clear();
      this.set('off');
    }
  };

  private clear() {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
  }

  private set(s: StreamStatus) {
    if (this.status === s) return;
    this.status = s;
    this.onStatus(s);
  }

  private schedule(ms: number) {
    this.clear();
    if (this.stopped || !this.visible()) return;
    this.timer = setTimeout(() => void this.poll(), ms);
  }

  private async poll() {
    if (this.stopped || !this.visible() || this.polling) return;
    this.polling = true;
    this.clear();
    if (this.status !== 'live') this.set(this.attempt === 0 ? 'connecting' : 'retrying');
    // A tab hidden while this poll runs makes the next one a catch-up too.
    const batch: StreamBatch = { events: [], initial: this.cursor === null, catchUp: this.wasHidden, resync: false };
    this.wasHidden = false;
    try {
      for (let page = 0; page < MAX_PAGES; page++) {
        const query = this.cursor === null ? {} : { after: this.cursor };
        const res = await unwrap(api.GET('/v1/events', { params: { header: wsh(this.workspaceId), query } }));
        if (this.stopped) return;
        if (res.resync) {
          if (this.onBatch) batch.resync = true;
          else this.onEvent('resync', {});
        } else {
          for (const e of res.items) {
            if (!KNOWN.has(e.event)) continue;
            if (this.onBatch) batch.events.push({ name: e.event, data: e.data });
            else this.onEvent(e.event, e.data as StreamEvents[typeof e.event]);
          }
        }
        this.cursor = res.next;
        if (!res.more) break;
      }
      if (this.onBatch && (batch.events.length || batch.resync)) this.onBatch(batch);
      this.attempt = 0;
      this.set(this.visible() ? 'live' : 'off');
      this.schedule(POLL_MS);
    } catch {
      if (this.stopped) return;
      this.wasHidden ||= batch.catchUp; // the catch-up has not happened yet
      this.set('retrying');
      this.schedule(backoffMs(this.attempt++));
    } finally {
      this.polling = false;
    }
  }
}
