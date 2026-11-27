import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../setup';
import { App, makeQueryClient } from '../../src/App';
import { __resetAuth } from '../../src/api/auth';
import { handlers } from '../fixtures/handlers';
import { API, PENDING, WS, entities, risks, summary } from '../fixtures/data';

function boot(path: string, signedIn = true) {
  localStorage.clear();
  if (signedIn) {
    localStorage.setItem('rp.refresh', 'test-refresh-token');
    localStorage.setItem('rp.workspace', WS);
  }
  __resetAuth();
  window.history.replaceState(null, '', path);
  const client = makeQueryClient();
  client.setDefaultOptions({ queries: { ...client.getDefaultOptions().queries, retry: false } });
  return render(<App client={client} />);
}

describe('screens', () => {
  it('sends a signed-out visitor to sign in', async () => {
    boot('/', false);
    expect(await screen.findByRole('heading', { name: 'Sign in' })).toBeInTheDocument();
    expect(window.location.pathname).toBe('/sign-in');
  });

  it('shows a wrong password plainly', async () => {
    boot('/sign-in', false);
    await userEvent.type(await screen.findByLabelText('Email'), 'mira.okafor@kestrel.test');
    await userEvent.type(screen.getByLabelText('Password'), 'nope');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong email or password.');
  });

  it('Empty: a new workspace, with feed counts from the API', async () => {
    server.use(...handlers('new'));
    boot('/');
    expect(await screen.findByRole('heading', { name: 'Nothing on this map is yours yet' })).toBeInTheDocument();
    expect(await screen.findByText('9 of 10 feeds live')).toBeInTheDocument();
    // the down feed is named for hover and keyboard focus
    expect(screen.getByRole('tooltip', { hidden: true })).toHaveTextContent('NewsData: paused by its rate limit (daily limit reached)');
    expect(screen.getByText(/what 10 global feeds see/)).toBeInTheDocument();
    expect(screen.getByText('Google Sheets or Airtable')).toBeInTheDocument();
    // Register and Scenarios wait for a source; one blue button.
    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(within(nav).getByText('Risks')).toHaveAttribute('aria-disabled', 'true');
    expect(document.querySelectorAll('.b.pri')).toHaveLength(1);
  });

  it('Overview: how many risks and their levels, the score strip, and the top story open in place', async () => {
    boot('/');
    expect(await screen.findByRole('heading', { name: '18 risks touch your business' })).toBeInTheDocument();
    expect(screen.getByText(/^All are Low\. Checked 4 min ago\.$/)).toBeInTheDocument();
    expect(screen.getByRole('img', { name: '18 scores out of 100, from 7 to 34' })).toBeInTheDocument();
    // one row per story, listed once by place, with the real headline and what it reaches
    const first = await screen.findByRole('button', { name: /^Shipping through Bab el-Mandeb/ });
    expect(first).toHaveAttribute('aria-expanded', 'true');
    expect(within(first).getByText('Houthis Strike Saudi Ports on Red Sea')).toBeInTheDocument();
    expect(within(first).getByText('8 routes and 7 suppliers ship through it')).toBeInTheDocument();
    expect(within(first).getByText('34')).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: /^(Shipping|Trade policy|Sanctions) / })).toHaveLength(3);
    // the open row: the receipt (likely × impact = score) and its two buttons
    expect(await screen.findByText('68%')).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByText('Shipping, on a route you rated high')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open risk' })).toHaveAttribute('href', `/risks/${risks[0].id}`);
    expect(screen.getByRole('link', { name: 'What if' })).toHaveAttribute('href', `/scenarios?risk=${risks[0].id}`);
    expect(screen.getByRole('link', { name: /All 18 risks/ })).toHaveAttribute('href', '/register');
    expect(document.querySelectorAll('.b.pri')).toHaveLength(1);
    // no internal words
    expect(document.body.textContent).not.toMatch(/signals matched|open stories|same country|noisy-OR/);
    // another row opens in its place
    await userEvent.click(screen.getByRole('button', { name: /^Trade policy in the Netherlands/ }));
    expect(screen.getByRole('button', { name: /^Trade policy in the Netherlands/ })).toHaveAttribute('aria-expanded', 'true');
    expect(await screen.findByText('Trade policy, at a site you rated critical')).toBeInTheDocument();
  });

  it('Overview: names the levels when they differ; the Risks tab counts stories', async () => {
    server.use(http.get(`${API}/v1/risks/summary`, () => HttpResponse.json({ ...summary, story_count: 41, open_count: 66, story_scores: [72, 61, ...Array(39).fill(12)] })));
    boot('/');
    expect(await screen.findByRole('heading', { name: '41 risks touch your business' })).toBeInTheDocument();
    expect(screen.getByText(/^2 are High, 39 Low\./)).toBeInTheDocument();
    const nav = screen.getByRole('navigation', { name: 'Main' });
    expect(within(nav).getByTitle('41 risks')).toHaveTextContent('41');
    expect(within(nav).getByRole('link', { name: /^Risks/ })).toHaveAttribute('href', '/register');
  });

  it('Risks: one row per place, category chips with counts on one row, and the sort menu', async () => {
    boot('/risks');
    expect(await screen.findByRole('heading', { name: 'Risks' })).toBeInTheDocument();
    expect(screen.getByText(/^18 touch your business\. Checked 4 min ago\.$/)).toBeInTheDocument();
    await screen.findByText('Shipping through Bab el-Mandeb');
    const listed = [...document.querySelectorAll<HTMLElement>('a.rrow')];
    expect(listed).toHaveLength(18);
    expect(listed[0]).toHaveAttribute('href', `/risks/${risks[0].id}`);
    expect(within(listed[0]).getByText('Shipping through Bab el-Mandeb')).toBeInTheDocument();
    expect(within(listed[1]).getByText('Trade policy in the Netherlands')).toBeInTheDocument();
    expect(within(listed[1]).getByText('Dutch Government Lobbies Against US Chip Export Bill')).toBeInTheDocument();
    expect(within(listed[1]).getByText('2 sites, in Eindhoven and Rotterdam')).toBeInTheDocument();
    expect(within(listed[2]).getByText('Sanctions in Malaysia')).toBeInTheDocument();
    expect(within(listed[2]).getByText('Penang Semicon Assembly')).toBeInTheDocument();
    expect(within(listed[3]).getByText('Cyber threats in Taiwan')).toBeInTheDocument();
    expect(within(listed[4]).getByText('Sanctions in China')).toBeInTheDocument();
    expect(within(listed[5]).getByText('Shipping in Malaysia')).toBeInTheDocument();
    // chips: All, then the categories with their story counts
    const chips = screen.getByRole('group', { name: 'Category' });
    expect(within(chips).getByRole('button', { name: 'All 18' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(chips).getByRole('button', { name: 'Shipping 4' })).toBeInTheDocument();
    // the sort button opens the two choices
    await userEvent.click(screen.getByRole('button', { name: 'Highest first' }));
    const menu = screen.getByRole('menu', { name: 'Sort' });
    expect(within(menu).getAllByRole('menuitemradio').map((b) => b.textContent)).toEqual(['Highest first', 'Newest first']);
    await userEvent.click(within(menu).getByRole('menuitemradio', { name: 'Newest first' }));
    expect(screen.getByRole('button', { name: 'Newest first' })).toBeInTheDocument();
  });

  it('Risks: /register is the same list', async () => {
    boot('/register');
    expect(await screen.findByRole('heading', { name: 'Risks' })).toBeInTheDocument();
    expect(await screen.findByText('Shipping through Bab el-Mandeb')).toBeInTheDocument();
  });

  it('Overview: /overview opens the Overview', async () => {
    // (a typed or bookmarked /overview is replaced by / before the first render: tests/e2e)
    boot('/overview');
    expect(await screen.findByRole('heading', { name: '18 risks touch your business' })).toBeInTheDocument();
  });

  it('Risks: filtered to one entity, it names the entity', async () => {
    const e = entities.find((x) => x.name === 'Formosa Microcircuits')!;
    boot(`/register?entity=${e.id}`);
    expect(await screen.findByTitle('Formosa Microcircuits')).toHaveTextContent('Formosa Microcircuits');
    expect(screen.queryByText('One entity')).not.toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Show all' })).toHaveAttribute('href', '/register');
  });

  it('Main: an empty register says so', async () => {
    server.use(
      http.get(`${API}/v1/risks`, () => HttpResponse.json({ items: [], next_cursor: null })),
      http.get(`${API}/v1/risks/summary`, () => HttpResponse.json({ open_count: 0, by_category: {}, by_status: {}, max_score: null, top: [], last_scored_at: null, feeds_as_of: null })),
    );
    boot('/');
    expect(await screen.findByText('No risks touch you yet')).toBeInTheDocument();
  });

  it('Main: a failed load offers a retry', async () => {
    server.use(http.get(`${API}/v1/risks`, () => HttpResponse.json({ code: 'internal', status: 500, title: 'x', type: 'about:blank' }, { status: 500 })));
    boot('/');
    expect(await screen.findByRole('button', { name: 'Try again' })).toBeInTheDocument();
  });

  it('Risk: the place, why it reaches you, the score and its move, the receipt, the news and what to do', async () => {
    let patched = '';
    server.use(
      http.patch(`${API}/v1/risks/:id`, async ({ request }) => {
        patched = ((await request.json()) as { status: string }).status;
        return HttpResponse.json({ ...risks[1], status: patched });
      }),
    );
    const r = risks[1]; // the live Atlas Eindhoven risk
    boot(`/risks/${r.id}`);
    expect(await screen.findByRole('heading', { name: 'Trade policy in the Netherlands' })).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Risks' })).toHaveAttribute('href', '/register');
    expect(screen.getByText('Open')).toBeInTheDocument();
    expect(screen.getByText('Atlas Eindhoven Assembly Plant, a critical site, and Atlas Rotterdam Distribution Centre are in the Netherlands.')).toBeInTheDocument();
    expect(screen.getByText('30')).toHaveClass('figxl');
    expect(screen.getByText('Up 3 since yesterday')).toBeInTheDocument();
    expect(screen.getByRole('img', { name: 'Score 30 out of 100: Low' })).toBeInTheDocument();
    expect(screen.getByText('Likely')).toBeInTheDocument();
    expect(screen.getByText('50%')).toBeInTheDocument();
    expect(screen.getByText(/^4 reports (in \d days|on \d+ \w+) name the Netherlands$/)).toBeInTheDocument();
    expect(screen.getByText('60%')).toBeInTheDocument();
    expect(screen.getByText('Trade policy, at a site you rated critical')).toBeInTheDocument();
    // two news items, strongest first; "All 4" shows the rest in place
    const lead = screen.getByRole('link', { name: /Dutch Government Lobbies Against US Chip Export Bill/ });
    expect(lead).toHaveAttribute('href', 'https://news.example.test/dutch-lobby');
    expect(screen.queryByText(/Dutch exporters warn of delays/)).not.toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'All 4' }));
    expect(screen.getByText(/Dutch exporters warn of delays/)).toBeInTheDocument();
    expect(screen.getByText('Written by AI from these reports')).toBeInTheDocument();
    expect(screen.getByText(/which tools or spares would need a new export licence/)).toBeInTheDocument();
    expect(document.querySelectorAll('.rsteps li')).toHaveLength(3);
    // gone: the tiles, the formula and the footer line; the sentence names both sites
    expect(document.body.textContent).not.toMatch(/signals matched|since the last one|Sources:|noisy-OR|same country|The same story also reaches/);
    // the working is one tap away
    await userEvent.click(screen.getByRole('button', { name: 'More actions' }));
    await userEvent.click(screen.getByRole('menuitem', { name: 'Show the working' }));
    const how = screen.getByRole('region', { name: 'How this was scored' });
    expect(within(how).getByText(/noisy-OR of 4 items/)).toBeInTheDocument();
    expect(within(how).getByText('country: Netherlands (NLD)')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Acknowledge' }));
    await screen.findByText('Marked acknowledged');
    expect(patched).toBe('acknowledged');
  });

  it('Risk: a chokepoint story counts what ships through it; a market move says what moved', async () => {
    boot(`/risks/${risks[0].id}`);
    expect(await screen.findByRole('heading', { name: 'Shipping through Bab el-Mandeb' })).toBeInTheDocument();
    expect(screen.getByText('8 of your routes and 7 of your suppliers ship through Bab el-Mandeb.')).toBeInTheDocument();
    expect(screen.getByText('Down 2 since yesterday')).toBeInTheDocument();
    expect(screen.getByText(/^2 reports (in \d days|on \d+ \w+) mention Bab el-Mandeb$/)).toBeInTheDocument();
  });

  it('Risk: a market move says what moved, and has no news', async () => {
    const fx = risks.find((x) => x.category === 'financial')!;
    boot(`/risks/${fx.id}`);
    expect(await screen.findByRole('heading', { name: 'TWD swings' })).toBeInTheDocument();
    expect(screen.getByText('An unusual move in EUR/TWD')).toBeInTheDocument();
    expect(screen.queryByText('The news')).not.toBeInTheDocument();
    expect(screen.getByText('From the playbook')).toBeInTheDocument();
  });

  it('Risk: a missing risk says so', async () => {
    boot('/risks/7d1f3c2a-0000-4000-8000-000000000999');
    expect(await screen.findByText('This risk is not in this workspace')).toBeInTheDocument();
  });

  it('Confirm: flags the low-confidence column and confirms the mapping', async () => {
    const box: { body?: { columns: { column: string; field: string }[] } } = {};
    server.use(
      http.post(`${API}/v1/sources/:id/mapping/confirm`, async ({ request }) => {
        box.body = (await request.json()) as never;
        return HttpResponse.json({});
      }),
    );
    boot(`/sources/${PENDING}/confirm`);
    expect(await screen.findByText('6 of 7 columns matched. One needs your choice.')).toBeInTheDocument();
    expect(screen.getByText('Needs you')).toBeInTheDocument();
    expect(screen.getByText('currency from the header: USD')).toBeInTheDocument();
    await userEvent.selectOptions(screen.getByLabelText('Read Buyer as'), 'notes');
    expect(await screen.findByText(/All 7 columns are matched/)).toBeInTheDocument();
    const side = screen.getByRole('region', { name: 'What happens when you confirm' });
    expect(await within(side).findByText('8 suppliers from 42 rows')).toBeInTheDocument();
    await userEvent.click(within(side).getByRole('button', { name: 'Start monitoring' }));
    await screen.findByText(/Monitoring started/);
    expect(box.body?.columns).toContainEqual({ column: 'Buyer', field: 'notes' });
    expect(box.body?.columns).toContainEqual({ column: 'Annual spend (USD)', field: 'annual_spend', currency: 'USD' });
    expect(JSON.stringify(box.body)).not.toContain('confidence');
  });

  it('Confirm: names the port codes the map cannot place, once each', async () => {
    // The test gazetteer (tests/setup.ts) has CNYTN, NLRTM and TWKHH.
    const feature = (row: number, ports: string[]) => ({
      type: 'Feature' as const, id: `row:${row}`, geometry: null,
      properties: { kind: 'supplier' as const, name: `Supplier ${row}`, countries: ['CHN'], ports, city: null, place: null, criticality: null, open_risk_count: 0, max_score: null, row_number: row },
    });
    const previewOf = (rows: string[][]) => ({ rows_read: rows.length, problems: [], entities: { type: 'FeatureCollection', truncated: false, features: rows.map((p, i) => feature(i + 2, p)) } });
    server.use(http.post(`${API}/v1/sources/:id/mapping/preview`, () => HttpResponse.json(previewOf([['CNYTN'], ['XXABC', 'NLRTM'], ['YYDEF'], ['XXABC']]))));
    boot(`/sources/${PENDING}/confirm`);
    const side = await screen.findByRole('region', { name: 'What happens when you confirm' });
    expect(await within(side).findByText('2 port codes we cannot place: XXABC, YYDEF.')).toBeInTheDocument();
    expect(within(side).getByText('We will watch 1 country and 4 ports. Nothing else.')).toBeInTheDocument();
  });

  it('Confirm: says nothing about ports when the map has them all', async () => {
    server.use(http.post(`${API}/v1/sources/:id/mapping/preview`, () => HttpResponse.json({
      rows_read: 1, problems: [],
      entities: { type: 'FeatureCollection', truncated: false, features: [{ type: 'Feature', id: 'row:2', geometry: null, properties: { kind: 'supplier', name: 'Supplier 2', countries: ['TWN'], ports: ['TWKHH'], city: null, place: null, criticality: null, open_risk_count: 0, max_score: null, row_number: 2 } }] },
    })));
    boot(`/sources/${PENDING}/confirm`);
    const side = await screen.findByRole('region', { name: 'What happens when you confirm' });
    expect(await within(side).findByText('We will watch 1 country and 1 port. Nothing else.')).toBeInTheDocument();
    expect(within(side).queryByText(/we cannot place/)).not.toBeInTheDocument();
  });

  it('Scenario: runs the what-if and shows who feels it first', async () => {
    type Sent = { name: string; inputs: { shocks: { type: string; chokepoints?: string[] }[] } };
    const box: { sent?: Sent } = {};
    server.use(
      http.post(`${API}/v1/scenarios`, async ({ request }) => {
        const sent = (box.sent = (await request.json()) as Sent);
        const { scenarioFor } = await import('../fixtures/data');
        return HttpResponse.json(scenarioFor(sent.name, sent.inputs as never), { status: 201 });
      }),
    );
    boot('/scenarios');
    expect(await screen.findByRole('heading', { name: 'Bab el-Mandeb closes' })).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Run what-if' }));
    expect(await screen.findByText('Who feels it first')).toBeInTheDocument();
    expect(screen.getByText('Yantian to Rotterdam')).toBeInTheDocument();
    // the level names the index shown (73 → High), and the change is its own figure
    expect(document.querySelector('.lvl')).toHaveTextContent(/^High$/);
    expect(screen.getByText('+11 from today')).toBeInTheDocument();
    // headline counts are stories; the index is never above the most exposed entity, shown with it
    expect(screen.getByText('stories touch you, 4 today · 8 risks')).toBeInTheDocument();
    expect(screen.getByText('most exposed: Yantian to Rotterdam, 84 today')).toBeInTheDocument();
    expect(screen.getByText(/inferred from their port/)).toBeInTheDocument();
    expect(box.sent?.inputs.shocks[0]).toMatchObject({ type: 'event', chokepoints: ['bab_el_mandeb'] });
  });

  it('Sources: /sources opens Connect; no step count once a source is live', async () => {
    boot('/sources');
    expect(await screen.findByRole('heading', { name: 'Connect a source' })).toBeInTheDocument();
    expect(await screen.findByText('Connected · live')).toBeInTheDocument();
    expect(screen.queryByText('Step 1 of 3')).not.toBeInTheDocument();
  });

  it('Connect: the step count shows during the first connect flow', async () => {
    server.use(...handlers('new'));
    boot('/connect');
    expect(await screen.findByText('Step 1 of 3')).toBeInTheDocument();
  });

  it('Scenario: a change under one point is shown with a decimal, and old results fall back to risk counts', async () => {
    const { scenarioFor } = await import('../fixtures/data');
    // a result stored before story_count and most_exposed were added
    const s = scenarioFor('Bab el-Mandeb closes', { shocks: [] });
    const r = s.results!;
    const strip = ({ story_count: _s, most_exposed: _m, ...rest }: typeof r.baseline) => rest;
    const old = { ...s, results: { ...r, baseline: { ...strip(r.baseline), portfolio_score: 62.4 }, scenario: { ...strip(r.scenario), portfolio_score: 62.7 } } };
    server.use(
      http.post(`${API}/v1/scenarios`, () => HttpResponse.json(old, { status: 201 })),
      http.get(`${API}/v1/scenarios/:id`, () => HttpResponse.json(old)),
    );
    boot('/scenarios');
    await screen.findByRole('heading', { name: 'Bab el-Mandeb closes' });
    await userEvent.click(screen.getByRole('button', { name: 'Run what-if' }));
    expect(await screen.findByText('+0.3 from today')).toBeInTheDocument();
    expect(screen.getByText('62.7')).toBeInTheDocument();
    expect(screen.getByText('risks touch you, 6 today')).toBeInTheDocument();
    expect(screen.getByText('highest score, 81 today')).toBeInTheDocument();
  });

  it('Alerts: five at first, then all of them page by page; a resolved risk says so', async () => {
    const { alerts } = await import('../fixtures/data');
    const make = (i: number) => ({ ...alerts[1], id: `7d1f3c2a-0000-4000-8000-${String(100 + i).padStart(12, '0')}`, title: `Alert ${i}`, read_at: null });
    const first = [{ ...alerts[0], title: 'Sanctions news on chips', read_at: new Date().toISOString(), risk_status: 'resolved' as const }, ...Array.from({ length: 19 }, (_, i) => make(i + 1))];
    const second = Array.from({ length: 3 }, (_, i) => make(i + 20));
    server.use(
      http.get(`${API}/v1/alerts`, ({ request }) => {
        const cursor = new URL(request.url).searchParams.get('cursor');
        return HttpResponse.json(cursor === 'p2' ? { items: second, next_cursor: null, unread_count: 22 } : { items: first, next_cursor: 'p2', unread_count: 22 });
      }),
    );
    boot('/');
    await userEvent.click(await screen.findByRole('button', { name: 'Account, 22 unread alerts' }));
    const menu = screen.getByRole('menu', { name: 'Account' });
    expect(within(menu).getByText('Alerts · 22 unread')).toBeInTheDocument();
    expect(within(menu).getByText('Sanctions news on chips')).toBeInTheDocument();
    expect(within(menu).getByText(/^Resolved · /)).toBeInTheDocument();
    expect(within(menu).queryByText('Alert 5')).not.toBeInTheDocument();
    await userEvent.click(within(menu).getByRole('button', { name: 'Show all' }));
    expect(within(menu).getByText('Alert 19')).toBeInTheDocument();
    await userEvent.click(within(menu).getByRole('button', { name: 'Load more' }));
    expect(await within(menu).findByText('Alert 22')).toBeInTheDocument();
    expect(within(menu).queryByRole('button', { name: 'Load more' })).not.toBeInTheDocument();
  });

  it('Connect: tools come from the API', async () => {
    server.use(http.get(`${API}/v1/connections/providers`, () => HttpResponse.json({ items: [{ id: 'google_sheets', name: 'Google Sheets', kind: 'direct', available: false }] })));
    boot('/connect');
    expect(await screen.findByText('Not set up on this server')).toBeInTheDocument();
    expect(screen.queryByText('Airtable')).not.toBeInTheDocument();
  });
});
