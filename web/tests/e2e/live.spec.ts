// The live sequence with motion on (the other specs run with reduced motion): a matched risk
// from GET /v1/events ripples where its signal is, then the list updates and its rows move.
// The fixture in ./fixtures.ts fails the test on any console error or CSP violation.
import { http, HttpResponse } from 'msw';
import { test, expect } from './fixtures';
import { API, entities, iso, riskDetail, risks, summary } from '../fixtures/data';

// Motion on keeps the map drawing every frame (lane flow, rotation, the travelling line); in
// CI that is software WebGL, so this runs at 1× pixel density to keep frames affordable.
test.use({ reducedMotion: 'no-preference', viewport: { width: 1200, height: 800 }, deviceScaleFactor: 1 });

test('a new risk ripples on the map, then the row moves', async ({ page, network }, info) => {
  test.skip(info.project.name !== 'desktop', 'one width is enough for the sequence');
  test.setTimeout(120_000);
  const formosa = entities.find((e) => e.name === 'Formosa Microcircuits')!;
  const base = risks.find((r) => r.entity.id === formosa.id && r.category === 'geopolitical')!;
  const fresh = {
    ...base,
    id: '7d1f3c2a-0000-4000-8000-00000000f0a1',
    category: 'labour' as const,
    title: 'Labour action news in Taiwan affecting Formosa Microcircuits',
    likelihood: 0.88,
    impact: 1,
    score: 88,
    first_seen_at: iso(0),
    last_seen_at: iso(0),
    story: { key: 'labour|country:TWN', risk_count: 1, entity_count: 1, kind_counts: { supplier: 1 }, entities: [{ id: formosa.id, name: formosa.name, kind: formosa.kind, city: formosa.city }] },
    where: { match_type: 'country' as const, match_value: 'TWN', label: 'Taiwan' },
    lead: { title: 'Port strike at Kaohsiung', source: 'gdelt', published_at: iso(0), url: null, type: 'news' },
  };
  let fire = false;
  let sent = false;
  network.use(
    http.get(`${API}/v1/events`, ({ request }) => {
      const after = new URL(request.url).searchParams.get('after');
      if (after !== null && fire && !sent) {
        sent = true;
        return HttpResponse.json({
          items: [{ id: 1, event: 'risk.created', data: { risk: fresh, previous_score: null }, created_at: iso(0) }],
          next: 1, more: false, resync: false,
        });
      }
      return HttpResponse.json({ items: [], next: after === null ? 0 : Number(after), more: false, resync: false });
    }),
    http.get(`${API}/v1/risks`, ({ request }) => {
      const limit = Number(new URL(request.url).searchParams.get('limit') ?? 50);
      const all = sent ? [fresh, ...risks] : risks;
      return HttpResponse.json({ items: all.slice(0, limit), next_cursor: null });
    }),
    http.get(`${API}/v1/risks/summary`, () =>
      HttpResponse.json(sent ? { ...summary, max_score: 88, open_count: summary.open_count + 1, story_count: (summary.story_count ?? 0) + 1, story_scores: [88, ...(summary.story_scores ?? [])] } : summary),
    ),
    http.get(`${API}/v1/risks/:id`, ({ params }) => {
      const r = params.id === fresh.id ? fresh : risks.find((x) => x.id === params.id);
      return r ? HttpResponse.json(riskDetail(r)) : HttpResponse.json({ status: 404 }, { status: 404 });
    }),
  );

  await page.goto('/');
  const top = page.getByRole('button', { name: /^Shipping through Bab el-Mandeb/ });
  await expect(top).toBeVisible({ timeout: 20_000 });
  // the map has drawn (with motion on it may already be turning, so it never settles)
  await expect(page.locator('.mapbox[data-map]:not([data-map="loading"])')).toBeAttached({ timeout: 30_000 });
  // polling is live (the first poll is done, and it plays nothing)
  await expect(page.getByText(/Checked /)).toBeVisible();
  await expect(page.locator('.rp-ripple')).toHaveCount(0);
  const before = (await top.boundingBox())!.y;

  // the next poll brings the risk: ask for it now (the stream polls at once on visibilitychange).
  // The ripple lives 1.6 s: an observer notes when it and the new row first appear.
  fire = true;
  await page.evaluate((id) => {
    const w = window as unknown as { rpSeen: Record<string, number> };
    w.rpSeen = {};
    const note = () => {
      if (!w.rpSeen.ripple && document.querySelector('.rp-ripple')) w.rpSeen.ripple = performance.now();
      if (!w.rpSeen.row && document.querySelector(`[data-motion-key="${id}"]`)) w.rpSeen.row = performance.now();
    };
    new MutationObserver(note).observe(document.body, { subtree: true, childList: true });
  }, fresh.id);
  // (a poll already in flight ignores the nudge: nudge until the event has been served)
  await expect
    .poll(async () => {
      if (!sent) await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
      return sent;
    }, { timeout: 20_000, intervals: [100, 250, 500] })
    .toBe(true);
  const seen = () => page.evaluate(() => (window as unknown as { rpSeen: Record<string, number> }).rpSeen);
  await expect.poll(async () => (await seen()).ripple ?? 0, { timeout: 10_000 }).toBeGreaterThan(0);

  // then the list updates: the new risk leads, the old first row moves down a place
  await expect(page.getByRole('button', { name: /^Labour unrest in Taiwan.*Port strike at Kaohsiung/ })).toBeVisible({ timeout: 20_000 });
  const t = await seen();
  expect(t.row).toBeGreaterThan(t.ripple);
  await expect.poll(async () => (await top.boundingBox())!.y, { timeout: 10_000 }).toBeGreaterThan(before + 20);
  await expect(page.getByRole('heading', { name: '19 risks touch your business' })).toBeVisible({ timeout: 10_000 });
  expect(sent).toBe(true);
});
