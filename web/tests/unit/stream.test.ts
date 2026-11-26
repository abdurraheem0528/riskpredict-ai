import { afterEach, describe, expect, it, vi } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../setup';
import { LiveStream, POLL_MS, type StreamStatus } from '../../src/api/stream';
import { __resetAuth } from '../../src/api/auth';
import { API, WS } from '../fixtures/data';

/** A document whose visibility the test controls. */
class FakeDoc extends EventTarget {
  visibilityState: DocumentVisibilityState = 'visible';
  show(v: DocumentVisibilityState) {
    this.visibilityState = v;
    this.dispatchEvent(new Event('visibilitychange'));
  }
}

type Reply = { items: { id: number; event: string; data: unknown; created_at: string }[]; next: number; more: boolean; resync: boolean };
const ev = (id: number, event = 'risk.created') => ({ id, event, data: { risk: { id: `r${id}` } }, created_at: '2026-11-24T12:00:00Z' });

function serve(replies: (after: string | null) => Reply | Response) {
  const seen: (string | null)[] = [];
  server.use(
    http.get(`${API}/v1/events`, ({ request }) => {
      expect(request.headers.get('X-Workspace-Id')).toBe(WS);
      const after = new URL(request.url).searchParams.get('after');
      seen.push(after);
      const r = replies(after);
      return r instanceof Response ? r : HttpResponse.json(r);
    }),
  );
  return seen;
}

function make(doc: FakeDoc) {
  localStorage.setItem('rp.refresh', 'r');
  __resetAuth();
  const events: string[] = [];
  const statuses: StreamStatus[] = [];
  const s = new LiveStream(WS, (name) => events.push(name), (st) => statuses.push(st), doc as unknown as Document);
  return { s, events, statuses };
}

describe('LiveStream (polling)', () => {
  afterEach(() => vi.useRealTimers());

  it('starts from now, then polls after its cursor about every 15 s and dispatches events', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const seen = serve((after) =>
      after === null ? { items: [], next: 7, more: false, resync: false } : after === '7'
        ? { items: [ev(9), ev(12, 'alert.created')], next: 12, more: false, resync: false }
        : { items: [], next: Number(after), more: false, resync: false },
    );
    const doc = new FakeDoc();
    const { s, events, statuses } = make(doc);
    s.start();
    await vi.waitFor(() => expect(s.status).toBe('live'));
    expect(seen).toEqual([null]);
    expect(statuses).toEqual(['connecting', 'live']);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await vi.waitFor(() => expect(events).toEqual(['risk.created', 'alert.created']));
    expect(seen).toEqual([null, '7']);
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await vi.waitFor(() => expect(seen).toEqual([null, '7', '12']));
    s.stop();
    expect(s.status).toBe('off');
  });

  it('stops while the tab is hidden and catches up as soon as it is shown', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let n = 0;
    const seen = serve(() => ({ items: n++ === 1 ? [ev(3)] : [], next: 3, more: false, resync: false }));
    const doc = new FakeDoc();
    const { s, events } = make(doc);
    s.start();
    await vi.waitFor(() => expect(s.status).toBe('live'));
    doc.show('hidden');
    expect(s.status).toBe('off');
    await vi.advanceTimersByTimeAsync(POLL_MS * 10); // a long time hidden: not a single request
    expect(seen).toHaveLength(1);
    doc.show('visible'); // shown again: polls at once, without waiting for the interval
    await vi.waitFor(() => expect(events).toEqual(['risk.created']));
    expect(seen).toEqual([null, '3']);
    expect(s.status).toBe('live');
    s.stop();
  });

  it('a tab that starts hidden does not poll until it is shown', async () => {
    const seen = serve(() => ({ items: [], next: 0, more: false, resync: false }));
    const doc = new FakeDoc();
    doc.visibilityState = 'hidden';
    const { s } = make(doc);
    s.start();
    await new Promise((r) => setTimeout(r, 30));
    expect(seen).toEqual([]);
    doc.show('visible');
    await vi.waitFor(() => expect(seen).toEqual([null]));
    s.stop();
  });

  it('reads every page when the API says more, and passes resync on', async () => {
    const seen = serve((after) =>
      after === null ? { items: [], next: 1, more: false, resync: false }
        : after === '1' ? { items: [ev(2)], next: 2, more: true, resync: false }
        : { items: [], next: 50, more: false, resync: true },
    );
    vi.useFakeTimers({ shouldAdvanceTime: true });
    const { s, events } = make(new FakeDoc());
    s.start();
    await vi.waitFor(() => expect(s.status).toBe('live'));
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await vi.waitFor(() => expect(events).toEqual(['risk.created', 'resync']));
    expect(seen).toEqual([null, '1', '2']);
    s.stop();
  });

  it('backs off after an error and keeps its cursor', async () => {
    vi.useFakeTimers({ shouldAdvanceTime: true });
    let fail = false;
    const seen = serve((after) =>
      fail ? new HttpResponse(null, { status: 503 }) : { items: [], next: after === null ? 4 : Number(after), more: false, resync: false },
    );
    const { s, statuses } = make(new FakeDoc());
    s.start();
    await vi.waitFor(() => expect(s.status).toBe('live'));
    fail = true;
    await vi.advanceTimersByTimeAsync(POLL_MS);
    await vi.waitFor(() => expect(s.status).toBe('retrying'));
    fail = false;
    await vi.advanceTimersByTimeAsync(1100); // first retry within ~1 s, not a full interval
    await vi.waitFor(() => expect(s.status).toBe('live'));
    expect(seen.slice(-1)).toEqual(['4']);
    expect(statuses).toEqual(['connecting', 'live', 'retrying', 'live']);
    s.stop();
  });
});
