import { http, HttpResponse } from 'msw';
import { test, expect, mapSettled, shot } from './fixtures';
import { API, PENDING, entities, risks } from '../fixtures/data';

test.describe('signed out', () => {
  test.use({ signedIn: false });
  test('sign in', async ({ page }, info) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Sign in' })).toBeVisible();
    await mapSettled(page);
    await shot(page, '0-sign-in', info.project.name);
  });

  test('security headers', async ({ page }) => {
    const res = await page.goto('/');
    const h = res!.headers();
    expect(h['strict-transport-security']).toBe('max-age=31536000; includeSubDomains');
    const csp = h['content-security-policy'];
    // the build's own API (http://api.test for this build), nothing looser
    expect(csp).toContain("connect-src 'self' http://api.test;");
    expect(csp).toContain("frame-ancestors 'none'");
    expect(csp).not.toContain('unsafe-eval');
    // the static privacy page (Google's OAuth consent links to it) runs under it too
    await page.goto('/privacy.html');
    await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
  });
});

test.describe('new workspace', () => {
  test.use({ state: 'new' });
  test('empty', async ({ page }, info) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: 'Nothing on this map is yours yet' })).toBeVisible();
    await expect(page.getByText('9 of 10 feeds live')).toBeAttached();
    await mapSettled(page);
    await shot(page, '1-empty', info.project.name);
  });
});

test.describe('active workspace', () => {
  test('connect', async ({ page }, info) => {
    await page.goto('/connect');
    await expect(page.getByRole('heading', { name: 'Connect a source' })).toBeVisible();
    await expect(page.getByText('Connected · live')).toBeVisible();
    // a source is live: this is where tools are managed, not step 1 of the first flow
    await expect(page.getByText('Step 1 of 3')).toHaveCount(0);
    await mapSettled(page);
    await shot(page, '2-connect', info.project.name);
  });

  test('sources is the connect screen', async ({ page }) => {
    await page.goto('/sources');
    await expect(page.getByRole('heading', { name: 'Connect a source' })).toBeVisible();
    await expect(page).toHaveURL(/\/connect$/);
  });

  test('confirm', async ({ page }, info) => {
    await page.goto(`/sources/${PENDING}/confirm`);
    await expect(page.getByRole('heading', { name: 'Check how we read your sheet' })).toBeVisible();
    await expect(page.getByText('6 of 7 columns matched. One needs your choice.')).toBeVisible();
    await expect(page.getByText(/suppliers from 42 rows/).first()).toBeAttached();
    await mapSettled(page);
    await shot(page, '3-confirm', info.project.name);
  });

  test('main', async ({ page }, info) => {
    await page.goto('/');
    await expect(page.getByRole('button', { name: /Name matches a sanctions entry/ })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open risk' })).toBeVisible();
    await mapSettled(page);
    await shot(page, '4-main', info.project.name);
  });

  test('register', async ({ page }, info) => {
    await page.goto('/register');
    await expect(page.getByText('6 open stories')).toBeVisible();
    await mapSettled(page);
    await shot(page, '5-register', info.project.name);
  });

  test('register for one entity', async ({ page }, info) => {
    const e = entities.find((x) => x.name === 'Lumen Display Co')!;
    await page.goto(`/register?entity=${e.id}`);
    await expect(page.getByLabel('Risk register').getByTitle('Lumen Display Co')).toBeVisible();
    await expect(page.getByText('One entity')).toHaveCount(0);
    await mapSettled(page);
    await shot(page, '5-register-entity', info.project.name);
  });

  test('register to a risk and back', async ({ page }, info) => {
    // Going back while the risk screen's map work is still in flight removes its layers under
    // running tweens and pending writes: no console errors (the fixture fails the test on any).
    // The code is the same at every width, and three screens on SwiftShader are slow on CI.
    test.skip(info.project.name !== 'desktop', 'one width is enough');
    test.slow();
    await page.goto('/register');
    await expect(page.getByText('6 open stories')).toBeVisible();
    await mapSettled(page);
    const row = page.getByRole('link', { name: /Name matches a sanctions entry/ });
    await row.hover();
    await row.click();
    await expect(page.getByText('What to do')).toBeVisible();
    await page.goBack();
    await expect(page.getByText('6 open stories')).toBeVisible();
    await mapSettled(page);
  });

  test('risk', async ({ page }, info) => {
    const r = risks.find((x) => x.category === 'logistics')!;
    await page.goto(`/risks/${r.id}`);
    await expect(page.getByRole('heading', { name: r.title })).toBeVisible();
    await expect(page.getByText('What to do')).toBeVisible();
    await mapSettled(page);
    await shot(page, '6-risk', info.project.name);
    // the numbers: a collapsed section under the evidence
    const how = page.getByText('How this was scored');
    await how.scrollIntoViewIfNeeded();
    await how.click();
    await expect(page.getByText(/noisy-OR of 2 items/)).toBeVisible();
    // opening it scrolls the sheet so the numbers sit above the action bar, not behind it
    const inView = async () => {
      const [body, foot, steps] = await Promise.all([
        page.locator('.sbody').boundingBox(),
        page.locator('.sfoot').boundingBox(),
        page.locator('.how .step').evaluateAll((els) => els.map((el) => { const r = el.getBoundingClientRect(); return { top: r.top, bottom: r.bottom }; })),
      ]);
      const first = steps[0];
      const last = steps[steps.length - 1];
      const whole = last.bottom - first.top <= foot!.y - body!.y - 40;
      return first.top >= body!.y - 1 && (whole ? last.bottom : first.bottom) <= foot!.y + 1;
    };
    await expect.poll(inView, { timeout: 5_000 }).toBe(true);
    await shot(page, '6-risk-how-scored', info.project.name);
  });

  test('scenario', async ({ page }, info) => {
    await page.goto('/scenarios');
    await expect(page.getByRole('heading', { name: 'Bab el-Mandeb closes' })).toBeVisible();
    await page.getByRole('button', { name: 'Run what-if' }).click();
    await expect(page.getByText('Who feels it first')).toBeVisible();
    await mapSettled(page);
    await shot(page, '7-scenario', info.project.name);
  });

  test('jump to', async ({ page, network }, info) => {
    test.skip(info.project.name.startsWith('phone'), 'Jump to opens from the sections menu on phones');
    // a long title stays on one line, cut with an ellipsis, with no sideways scroll
    const long = `${risks[0].title}: a long headline from the feed that runs well past the width of the palette`;
    network.use(http.get(`${API}/v1/risks`, () => HttpResponse.json({ items: [{ ...risks[0], title: long }, ...risks.slice(1)], next_cursor: null })));
    await page.goto('/');
    await expect(page.getByRole('link', { name: 'Open risk' })).toBeVisible();
    await page.keyboard.press('ControlOrMeta+k');
    const dialog = page.getByRole('dialog', { name: 'Jump to' });
    await expect(dialog).toBeVisible();
    const label = dialog.getByTitle(long);
    await expect(label).toBeVisible();
    expect(await label.evaluate((el) => el.scrollWidth > el.clientWidth && getComputedStyle(el).textOverflow === 'ellipsis')).toBe(true);
    expect(await dialog.locator('.pl').evaluate((el) => el.scrollWidth <= el.clientWidth)).toBe(true);
    await mapSettled(page);
    await shot(page, '8-jump-to', info.project.name);
  });
});
