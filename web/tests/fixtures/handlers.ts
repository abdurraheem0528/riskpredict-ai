// MSW handlers for the contract (docs/contracts/openapi.yaml), test-only. `state` picks the
// workspace: 'new' has no connections (Empty), 'active' has a connected sheet and risks.
import { http, HttpResponse, delay } from 'msw';
import * as F from './data';

export type FixtureState = 'new' | 'active';
const u = (p: string) => `${F.API}${p}`;
const page = <T,>(items: T[]) => ({ items, next_cursor: null });
const problem = (status: number, code: string, detail: string) =>
  HttpResponse.json({ type: 'about:blank', title: detail, status, code, detail }, { status, headers: { 'Content-Type': 'application/problem+json' } });

export function handlers(state: FixtureState = 'active', opts: { slow?: boolean } = {}) {
  const active = state === 'active';
  const wait = async () => {
    if (opts.slow) await delay(60_000);
  };
  return [
    http.post(u('/v1/auth/refresh'), () => HttpResponse.json(F.tokens())),
    http.post(u('/v1/auth/sign-in'), async ({ request }) => {
      const b = (await request.json()) as { email: string; password: string };
      if (b.password !== 'correct horse battery') return problem(401, 'invalid_credentials', 'Wrong email or password');
      return HttpResponse.json({ ...F.tokens(), user: F.user, workspace_id: F.WS });
    }),
    http.post(u('/v1/auth/sign-out'), () => new HttpResponse(null, { status: 204 })),
    http.get(u('/v1/me'), async () => {
      await wait();
      return HttpResponse.json({ user: F.user, memberships: [{ workspace: F.workspace, role: 'owner' }] });
    }),
    http.get(u('/v1/connections/providers'), () => HttpResponse.json({ items: F.providers })),
    http.get(u('/v1/connections'), () => HttpResponse.json({ items: active ? [F.connection] : [] })),
    http.get(u('/v1/connections/:id'), () => HttpResponse.json(F.connection)),
    http.get(u('/v1/sources'), () => HttpResponse.json({ items: active ? [F.sourceActive, F.sourcePending] : [] })),
    http.get(u('/v1/sources/:id'), ({ params }) => HttpResponse.json(params.id === F.PENDING ? F.sourcePending : F.sourceActive)),
    http.get(u('/v1/sources/:id/mapping'), () => HttpResponse.json({ suggested: F.suggestion, confirmed: null, fields: F.fields })),
    http.post(u('/v1/sources/:id/mapping/preview'), () => HttpResponse.json(F.preview)),
    http.post(u('/v1/sources/:id/mapping/confirm'), () => HttpResponse.json({ ...F.suggestion, status: 'confirmed', confirmed_at: F.iso(0) })),
    http.get(u('/v1/feeds/health'), () => HttpResponse.json(F.health)),
    http.get(u('/v1/map/events'), () => HttpResponse.json(F.events)),
    http.get(u('/v1/map/disruptions'), () => HttpResponse.json(F.disruptions)),
    http.get(u('/v1/map/entities'), () => HttpResponse.json(active ? F.mapEntities : { type: 'FeatureCollection', truncated: false, features: [] })),
    http.get(u('/v1/map/risks'), ({ request }) => {
      const cat = new URL(request.url).searchParams.get('category');
      const feats = active ? F.mapRisks.features.filter((f) => !cat || f.properties.top_categories.includes(cat as never)) : [];
      return HttpResponse.json({ type: 'FeatureCollection', features: feats });
    }),
    http.get(u('/v1/risks/summary'), () =>
      HttpResponse.json(
        active ? F.summary : { open_count: 0, by_category: {}, by_status: {}, max_score: null, top: [], last_scored_at: null, feeds_as_of: F.iso(0) },
      ),
    ),
    http.get(u('/v1/risks'), ({ request }) => {
      const q = new URL(request.url).searchParams;
      const cat = q.get('category');
      const limit = Number(q.get('limit') ?? 50);
      const items = (active ? F.risks : []).filter((r) => !cat || r.category === cat).slice(0, limit);
      return HttpResponse.json(page(items));
    }),
    http.get(u('/v1/risks/:id'), ({ params }) => {
      if (params.id === F.worstCase.id) return HttpResponse.json(F.worstCase); // in no list: the fit check's worst case
      const r = F.risks.find((x) => x.id === params.id);
      return r ? HttpResponse.json(F.riskDetail(r)) : problem(404, 'not_found', 'Not found');
    }),
    http.patch(u('/v1/risks/:id'), async ({ params, request }) => {
      const b = (await request.json()) as { status: 'open' };
      const r = F.risks.find((x) => x.id === params.id)!;
      return HttpResponse.json({ ...r, status: b.status });
    }),
    http.get(u('/v1/feeds/series'), () => HttpResponse.json({ series: 'fx.rate', key: 'EUR/TRY', unit: null, points: [], anomalies: [] })),
    http.get(u('/v1/alerts'), () => HttpResponse.json({ ...page(active ? F.alerts : []), unread_count: active ? F.alerts.length : 0 })),
    http.post(u('/v1/alerts/read-all'), () => new HttpResponse(null, { status: 204 })),
    http.get(u('/v1/entities'), () => HttpResponse.json(page(active ? F.entities : []))),
    http.get(u('/v1/scenarios'), () => HttpResponse.json(page([]))),
    http.post(u('/v1/scenarios'), async ({ request }) => {
      const b = (await request.json()) as { name: string; inputs: never };
      return HttpResponse.json(F.scenarioFor(b.name, b.inputs), { status: 201 });
    }),
    http.get(u('/v1/scenarios/:id'), () => HttpResponse.json(F.scenarioFor('Bab el-Mandeb closes', { shocks: [] }))),
    // Live updates are polled; nothing new happens in the fixtures.
    http.get(u('/v1/events'), ({ request }) => {
      const after = new URL(request.url).searchParams.get('after');
      return HttpResponse.json({ items: [], next: after === null ? 0 : Number(after), more: false, resync: false });
    }),
  ];
}
