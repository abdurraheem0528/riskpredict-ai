// Live updates over SSE (openapi.yaml, /v1/stream). EventSource cannot send headers, so
// each connection starts with a one-time ticket (single use, 60 s). EventSource's own
// reconnect would replay the spent ticket, so on any error we close it and reconnect
// ourselves with a fresh ticket, backing off 1 s → 30 s with jitter. A new EventSource
// cannot send Last-Event-ID, so after a reconnect the caller refetches (as for `resync`).
import type { components } from './schema';
import { api, unwrap } from './client';
import { API_URL } from './config';
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

export function backoffMs(attempt: number, random = Math.random) {
  const base = Math.min(30_000, 1000 * 2 ** Math.max(0, attempt));
  return Math.round(base / 2 + (random() * base) / 2);
}

export class LiveStream {
  private es: EventSource | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private attempt = 0;
  private stopped = false;
  private everOpened = false;
  status: StreamStatus = 'off';

  constructor(
    private workspaceId: string,
    private onEvent: Handler,
    private onStatus: (s: StreamStatus) => void,
    private onReconnected: () => void,
  ) {}

  start() {
    this.stopped = false;
    void this.open();
  }

  stop() {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.es?.close();
    this.es = null;
    this.set('off');
  }

  private set(s: StreamStatus) {
    this.status = s;
    this.onStatus(s);
  }

  private async open() {
    if (this.stopped) return;
    this.set(this.attempt === 0 ? 'connecting' : 'retrying');
    let ticket: string;
    try {
      const t = await unwrap(api.POST('/v1/stream/ticket', { params: { header: wsh(this.workspaceId) } }));
      ticket = t.ticket;
    } catch {
      this.retry();
      return;
    }
    if (this.stopped) return;
    const es = new EventSource(`${API_URL}/v1/stream?ticket=${encodeURIComponent(ticket)}`);
    this.es = es;
    es.onopen = () => {
      const reconnect = this.everOpened;
      this.everOpened = true;
      this.attempt = 0;
      this.set('live');
      if (reconnect) this.onReconnected();
    };
    es.onerror = () => {
      es.close();
      if (this.es === es) this.es = null;
      this.retry();
    };
    for (const name of STREAM_EVENTS) {
      es.addEventListener(name, (e) => {
        let data: unknown = {};
        try {
          data = JSON.parse((e as MessageEvent<string>).data || '{}');
        } catch {
          return;
        }
        this.onEvent(name, data as StreamEvents[typeof name]);
      });
    }
  }

  private retry() {
    if (this.stopped) return;
    this.set('retrying');
    const wait = backoffMs(this.attempt++);
    this.timer = setTimeout(() => void this.open(), wait);
  }
}
