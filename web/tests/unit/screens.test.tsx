import { describe, expect, it } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { server } from '../setup';
import { App, makeQueryClient } from '../../src/App';
import { __resetAuth } from '../../src/api/auth';
import { handlers } from '../fixtures/handlers';
import { API, PENDING, WS, risks } from '../fixtures/data';

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
    expect(within(nav).getByText('Register')).toHaveAttribute('aria-disabled', 'true');
    expect(document.querySelectorAll('.b.pri')).toHaveLength(1);
  });

  it('Main: top risks from the API, the first one selected', async () => {
    boot('/');
    const first = await screen.findByRole('button', { name: /Name matches a sanctions entry/ });
    expect(first).toHaveAttribute('aria-pressed', 'true');
    expect(document.querySelector('.figxl')).toHaveTextContent('81');
    expect(screen.getByText('6 open risks')).toBeInTheDocument();
    // one story that reaches two entities is one row, naming the first and counting the rest
    expect(screen.getByText('Yantian to Rotterdam and 1 more')).toBeInTheDocument();
    expect(screen.getByRole('link', { name: 'Open risk' })).toHaveAttribute('href', `/risks/${risks[0].id}`);
    expect(document.querySelectorAll('.b.pri')).toHaveLength(1);
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

  it('Risk: evidence and advice; acknowledging calls PATCH', async () => {
    let patched = '';
    server.use(
      http.patch(`${API}/v1/risks/:id`, async ({ request }) => {
        patched = ((await request.json()) as { status: string }).status;
        return HttpResponse.json({ ...risks[1], status: patched });
      }),
    );
    const r = risks.find((x) => x.category === 'logistics')!;
    boot(`/risks/${r.id}`);
    expect(await screen.findByRole('heading', { name: r.title })).toBeInTheDocument();
    expect(screen.getByText(/Order four weeks/)).toBeInTheDocument();
    expect(screen.getByText('Transit calls at Bab el-Mandeb down week on week')).toBeInTheDocument();
    expect(screen.getByText('The same story also reaches')).toBeInTheDocument();
    expect(screen.getByText('Lumen Display Co')).toBeInTheDocument();
    // the numbers sit in a collapsed section, not in the explanation
    const how = screen.getByText('How this was scored').closest('details')!;
    expect(how).not.toHaveAttribute('open');
    await userEvent.click(screen.getByText('How this was scored'));
    expect(how).toHaveAttribute('open');
    expect(within(how).getByText(/noisy-OR of 2 items/)).toBeInTheDocument();
    expect(within(how).getByText('chokepoint: Bab el-Mandeb')).toBeInTheDocument();
    await userEvent.click(screen.getByRole('button', { name: 'Acknowledge' }));
    await screen.findByText('Marked acknowledged');
    expect(patched).toBe('acknowledged');
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
    expect(box.sent?.inputs.shocks[0]).toMatchObject({ type: 'event', chokepoints: ['bab_el_mandeb'] });
  });

  it('Connect: tools come from the API', async () => {
    server.use(http.get(`${API}/v1/connections/providers`, () => HttpResponse.json({ items: [{ id: 'google_sheets', name: 'Google Sheets', kind: 'direct', available: false }] })));
    boot('/connect');
    expect(await screen.findByText('Not set up on this server')).toBeInTheDocument();
    expect(screen.queryByText('Airtable')).not.toBeInTheDocument();
  });
});
