import { http, HttpResponse } from 'msw';
import type { Page } from '@playwright/test';
import { test, expect, mapSettled, noSheetScroll, sheetBodyHeight, shot } from './fixtures';
import { API, PENDING, WORST_HEADLINES, capeCase, entities, hormuzCase, risks, worstCase } from '../fixtures/data';

/** Lines of text in a box of the given line height. */
const lines = (box: { height: number } | null, lh: number) => Math.round((box?.height ?? 0) / lh);

/** Every advice step shows, and there is no toggle under them. */
async function allSteps(page: Page) {
  await expect(page.locator('.rsteps li')).toHaveCount(3);
  await expect(page.getByRole('button', { name: /^\d more steps?$/ })).toHaveCount(0);
}

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

  test('overview', async ({ page }, info) => {
    await page.goto('/');
    await expect(page.getByRole('heading', { name: '18 risks touch your business' })).toBeVisible();
    await expect(page.getByRole('button', { name: /^Shipping through Bab el-Mandeb/ })).toBeVisible();
    await expect(page.getByRole('link', { name: 'Open risk' })).toBeVisible();
    await mapSettled(page);
    // one pill for the story, at the chokepoint, with no plain label beside it
    if (!info.project.name.startsWith('phone')) {
      await expect(page.locator('.mpill.show', { hasText: 'Bab el-Mandeb' })).toHaveCount(1);
      await expect(page.locator('.mpill.show', { hasText: /Busan|Kaohsiung/ })).toHaveCount(0);
      await expect(page.locator('.clab', { hasText: 'Bab el-Mandeb' })).toHaveCount(0);
    }
    await shot(page, '4-overview', info.project.name);
    await noSheetScroll(page, info.project.name, 'aside.sheet');
  });

  test('overview is at /', async ({ page }) => {
    await page.goto('/overview');
    await expect(page.getByRole('heading', { name: '18 risks touch your business' })).toBeVisible();
    await expect(page).toHaveURL(/\/$/);
  });

  test('risks', async ({ page }, info) => {
    await page.goto('/risks');
    await expect(page.getByRole('heading', { name: 'Risks', exact: true })).toBeVisible();
    await expect(page.getByText('18 touch your business.', { exact: false })).toBeVisible();
    await expect(page.getByRole('link', { name: /^Trade policy in the Netherlands/ })).toBeVisible();
    await mapSettled(page);
    await shot(page, '5-risks', info.project.name);
  });

  test('risks for one entity', async ({ page }, info) => {
    const e = entities.find((x) => x.name === 'Formosa Microcircuits')!;
    await page.goto(`/register?entity=${e.id}`);
    await expect(page.getByLabel('Risks').getByTitle('Formosa Microcircuits')).toBeVisible();
    await expect(page.getByText('One entity')).toHaveCount(0);
    await mapSettled(page);
    await shot(page, '5-risks-entity', info.project.name);
  });

  test('risks to a risk and back', async ({ page }, info) => {
    // Going back while the risk screen's map work is still in flight removes its layers under
    // running tweens and pending writes: no console errors (the fixture fails the test on any).
    // The code is the same at every width, and three screens on SwiftShader are slow on CI.
    test.skip(info.project.name !== 'desktop', 'one width is enough');
    test.slow();
    await page.goto('/register');
    await expect(page.getByRole('link', { name: /^Trade policy in the Netherlands/ })).toBeVisible();
    await mapSettled(page);
    const row = page.getByRole('link', { name: /^Trade policy in the Netherlands/ });
    await row.hover();
    await row.click();
    await expect(page.getByText('What to do')).toBeVisible();
    await page.goBack();
    await expect(page.getByRole('link', { name: /^Trade policy in the Netherlands/ })).toBeVisible();
    await mapSettled(page);
  });

  test('risk', async ({ page }, info) => {
    const r = risks[1]; // the live Atlas Eindhoven risk: 2 news items and 3 steps shown
    await page.goto(`/risks/${r.id}`);
    await expect(page.getByRole('heading', { name: 'Trade policy in the Netherlands' })).toBeVisible();
    await expect(page.getByText('What to do')).toBeVisible();
    await expect(page.getByText('Up 3 since yesterday')).toBeVisible();
    await mapSettled(page);
    // the whole page in one 1360 × 900 view: the sheet body does not scroll, and nothing is held back
    await shot(page, '6-risk', info.project.name);
    await sheetBodyHeight(page, info.project.name);
    await noSheetScroll(page, info.project.name, '.sbody');
    await allSteps(page);
    // the numbers: one tap away, in place, above the action bar
    await page.getByRole('button', { name: 'More actions' }).click();
    await page.getByRole('menuitem', { name: 'Show the working' }).click();
    await expect(page.getByText(/noisy-OR of 4 items/)).toBeVisible();
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
    await shot(page, '6-risk-working', info.project.name);
  });

  test('risk at a chokepoint', async ({ page }, info) => {
    test.skip(info.project.name.startsWith('phone') || info.project.name === 'tablet', 'the desktop map shows the pill');
    await page.goto(`/risks/${risks[0].id}`);
    await expect(page.getByRole('heading', { name: 'Shipping through Bab el-Mandeb' })).toBeVisible();
    await mapSettled(page);
    await expect(page.locator('.mpill.show', { hasText: 'Bab el-Mandeb' })).toHaveCount(1);
    await shot(page, '6-risk-chokepoint', info.project.name);
    await sheetBodyHeight(page, info.project.name);
    await noSheetScroll(page, info.project.name, '.sbody');
    await allSteps(page);
  });

  test('risk at the limits', async ({ page }, info) => {
    // The worst case real data can reach (tests/fixtures/data.ts `worstCase`): a two-line place,
    // a why-sentence on three long-named critical sites, a trend line, two three-line headlines
    // clamped to two, and three advice steps of 20 long words. With all three steps it needs more
    // sheet body than 1360 × 900 has, so where the sheet has a fixed height the last steps wait
    // behind "2 more steps" and the page fits; the phone's bottom sheet scrolls and shows them all.
    await page.goto(`/risks/${worstCase.id}`);
    const title = page.getByRole('heading', { name: 'Labour unrest in the Democratic Republic of the Congo' });
    await expect(title).toBeVisible();
    await expect(page.getByText('Up 11 since yesterday')).toBeVisible();
    await expect(page.getByText('Kolwezi Northern Copper and Cobalt Refining Complex, a critical site, and 2 other sites are in the Democratic Republic of the Congo.')).toBeVisible();
    await mapSettled(page);
    if (info.project.name.startsWith('desktop')) {
      // what the fixture claims: the title on two lines, each headline clamped from three lines to two
      expect(lines(await title.boundingBox(), 28)).toBe(2);
      for (const text of WORST_HEADLINES) {
        const link = page.getByRole('link', { name: `${text} (opens the source)` });
        await expect(link).toHaveAttribute('title', text);
        const { shown, full } = await link.evaluate((el) => ({ shown: el.clientHeight, full: el.scrollHeight }));
        expect(Math.round(shown / 19), text).toBe(2);
        expect(Math.round(full / 19), text).toBeGreaterThanOrEqual(3);
      }
    }
    await shot(page, '6-risk-worst-case', info.project.name);
    await sheetBodyHeight(page, info.project.name);
    await noSheetScroll(page, info.project.name, '.sbody');
    if (info.project.name.startsWith('phone')) return allSteps(page);
    // desktop and tablet: steps held back, the toggle under the last one shown; pressed, all three show
    const shown = await page.locator('.rsteps li').count();
    const toggle = page.getByRole('button', { name: `${3 - shown} more ${3 - shown === 1 ? 'step' : 'steps'}` });
    if (info.project.name.startsWith('desktop')) expect(shown).toBeLessThan(3);
    if (shown === 3) return expect(page.locator('.rsteps + .more')).toHaveCount(0);
    await expect(toggle).toHaveAttribute('aria-expanded', 'false');
    test.info().annotations.push({ type: 'shown', description: `${info.project.name}: ${shown} of 3 steps, ${await page.locator('.rnews .ni').count()} news items` });
    await toggle.click();
    await expect(page.locator('.rsteps li')).toHaveCount(3);
    await sheetBodyHeight(page, info.project.name, '.sbody', 'sheet-body, all steps');
    const fewer = page.getByRole('button', { name: 'Fewer' }).last();
    await expect(fewer).toHaveAttribute('aria-expanded', 'true');
    // a 44 px hit area (the button and its ::after), and the keyboard's focus ring on it
    expect(await fewer.evaluate((el) => { const a = getComputedStyle(el, '::after'); return el.getBoundingClientRect().height - parseFloat(a.top) - parseFloat(a.bottom); })).toBeGreaterThanOrEqual(44);
    await shot(page, '6-risk-worst-case-steps', info.project.name);
    await page.keyboard.press('Tab');
    await page.keyboard.press('Shift+Tab');
    await expect(fewer).toBeFocused();
    expect(await fewer.evaluate((el) => [el.matches(':focus-visible'), getComputedStyle(el).outlineStyle, getComputedStyle(el).outlineWidth])).toEqual([true, 'solid', '3px']);
    await page.keyboard.press('Enter');
    await expect(page.locator('.rsteps li')).toHaveCount(shown);
    await expect(page.getByRole('button', { name: `${3 - shown} more ${3 - shown === 1 ? 'step' : 'steps'}` })).toBeFocused();
  });

  // Pages real data comes close to (tests/fixtures/data.ts `hormuzCase`, `capeCase`): three advice
  // steps of three lines each, and the place on one line (Hormuz, 379 of 392 px here) or two (the
  // Cape of Good Hope). Each must fit one 1360 × 900 view.
  for (const [name, d, place, titleLines] of [
    ['hormuz', hormuzCase, 'the Strait of Hormuz', 1],
    ['cape', capeCase, 'the Cape of Good Hope', 2],
  ] as const)
    test(`risk near the limits: ${place}`, async ({ page }, info) => {
      await page.goto(`/risks/${d.id}`);
      const title = page.getByRole('heading', { name: `Shipping through ${place}` });
      await expect(title).toBeVisible();
      await expect(page.getByText(`3 of your routes and 2 of your suppliers ship through ${place}.`)).toBeVisible();
      await mapSettled(page);
      await shot(page, `6-risk-${name}`, info.project.name);
      await sheetBodyHeight(page, info.project.name);
      await noSheetScroll(page, info.project.name, '.sbody');
      const shown = await page.locator('.rsteps li').count();
      test.info().annotations.push({ type: 'shown', description: `${info.project.name}: ${shown} of 3 steps, ${await page.locator('.rnews .ni').count()} news items` });
      if (info.project.name.startsWith('phone')) return allSteps(page);
      if (shown < 3) await page.getByRole('button', { name: `${3 - shown} more ${3 - shown === 1 ? 'step' : 'steps'}` }).click();
      await expect(page.locator('.rsteps li')).toHaveCount(3);
      await sheetBodyHeight(page, info.project.name, '.sbody', 'sheet-body, all steps');
      if (info.project.name.startsWith('desktop')) {
        // what the fixture claims: the title's lines, and each step on three
        expect(lines(await title.boundingBox(), 28)).toBe(titleLines);
        for (const [i, step] of d.advice!.text.split('\n').entries()) {
          const li = page.locator('.rsteps li').nth(i);
          await expect(li).toHaveText(step);
          expect(lines(await li.boundingBox(), 18), step).toBe(3);
        }
      }
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
