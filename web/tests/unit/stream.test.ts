import { afterEach, describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../setup';
import { LiveStream, type StreamStatus } from '../../src/api/stream';
import { __resetAuth } from '../../src/api/auth';
import { API, WS } from '../fixtures/data';

type Fake = { url: string; onopen: (() => void) | null; onerror: (() => void) | null; closed: boolean; listeners: Record<string, (e: MessageEvent) => void> };
const opened: Fake[] = [];
class FakeES {
  url: string;
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  listeners: Record<string, (e: MessageEvent) => void> = {};
  constructor(url: string) {
    this.url = url;
    opened.push(this as unknown as Fake);
  }
  addEventListener(name: string, fn: (e: MessageEvent) => void) {
    this.listeners[name] = fn;
  }
  close() {
    this.closed = true;
  }
}

describe('LiveStream', () => {
  const orig = globalThis.EventSource;
  afterEach(() => {
    globalThis.EventSource = orig;
    opened.length = 0;
    vi.useRealTimers();
  });

  it('opens with a one-time ticket, dispatches events, and reconnects with a new ticket after an error', async () => {
    localStorage.setItem('rp.refresh', 'r');
    __resetAuth();
    globalThis.EventSource = FakeES as unknown as typeof EventSource;
    let n = 0;
    server.use(
      http.post(`${API}/v1/stream/ticket`, ({ request }) => {
        expect(request.headers.get('X-Workspace-Id')).toBe(WS);
        return HttpResponse.json({ ticket: `t${++n}`, expires_at: new Date(Date.now() + 60_000).toISOString() }, { status: 201 });
      }),
    );
    const events: string[] = [];
    const statuses: StreamStatus[] = [];
    const reconnected = vi.fn();
    const s = new LiveStream(WS, (name) => events.push(name), (st) => statuses.push(st), reconnected);
    s.start();
    await vi.waitFor(() => expect(opened).toHaveLength(1));
    expect(opened[0].url).toBe(`${API}/v1/stream?ticket=t1`);
    opened[0].onopen?.();
    expect(s.status).toBe('live');
    opened[0].listeners['risk.created']({ data: JSON.stringify({ risk: { id: 'x' } }) } as MessageEvent);
    expect(events).toEqual(['risk.created']);

    vi.useFakeTimers();
    opened[0].onerror?.();
    expect(opened[0].closed).toBe(true);
    expect(s.status).toBe('retrying');
    await vi.advanceTimersByTimeAsync(1100);
    vi.useRealTimers();
    await vi.waitFor(() => expect(opened).toHaveLength(2));
    expect(opened[1].url).toBe(`${API}/v1/stream?ticket=t2`);
    opened[1].onopen?.();
    expect(reconnected).toHaveBeenCalledTimes(1);
    s.stop();
    expect(statuses.at(-1)).toBe('off');
  });
});
