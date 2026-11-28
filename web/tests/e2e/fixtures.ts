import { test as base, expect, type Page } from '@playwright/test';
import { defineNetworkFixture, type NetworkFixture } from '@msw/playwright';
import { handlers, type FixtureState } from '../fixtures/handlers';
import { WS } from '../fixtures/data';

type Fx = { state: FixtureState; signedIn: boolean; network: NetworkFixture };

export const test = base.extend<Fx>({
  state: ['active', { option: true }],
  signedIn: [true, { option: true }],
  network: [
    async ({ context, state }, use) => {
      const network = defineNetworkFixture({ context, handlers: handlers(state), onUnhandledRequest: 'bypass' });
      await network.enable();
      await use(network);
      await network.disable();
    },
    { auto: true },
  ],
  page: async ({ page, signedIn }, use) => {
    await page.addInitScript(
      ([ws, signed]) => {
        if (signed) {
          localStorage.setItem('rp.refresh', 'test-refresh-token-0000000000000000000000000');
          localStorage.setItem('rp.workspace', ws as string);
        }
      },
      [WS, signedIn],
    );
    // Every screen runs under the Content-Security-Policy of dist/_headers (vite preview
    // serves those headers, see vite.config.ts): any violation, console error or uncaught
    // error fails the test.
    const problems: string[] = [];
    await page.addInitScript(() => {
      document.addEventListener('securitypolicyviolation', (e) => {
        console.error(`CSP violation: ${e.violatedDirective} blocked ${e.blockedURI || '(inline)'}`);
      });
    });
    page.on('console', (m) => {
      if (m.type() === 'error') problems.push(`console: ${m.text()}`);
    });
    page.on('pageerror', (e) => problems.push(`page error: ${e.message}`));
    await use(page);
    expect(problems, 'console errors, CSP violations or uncaught errors').toEqual([]);
  },
});

/** The map has drawn and settled; HTML markers have had a frame to land. */
export async function mapSettled(page: Page) {
  // Generous: on CI the globe renders in software (SwiftShader).
  await expect(page.locator('.mapbox[data-map="idle"]')).toBeAttached({ timeout: 45_000 });
  await page.waitForTimeout(600);
  await expect(page.locator('.mapbox[data-map="idle"]')).toBeAttached({ timeout: 45_000 });
}

/**
 * At 1360 × 900 the sheet (or its scrolling body) shows everything at once: no scrolling.
 * Other widths keep today's behaviour (the phone sheet scrolls), so they are not checked.
 */
export async function noSheetScroll(page: Page, project: string, selector: string) {
  if (!project.startsWith('desktop')) return;
  const el = page.locator(selector).first();
  const { scroll, client } = await el.evaluate((e) => ({ scroll: e.scrollHeight, client: e.clientHeight }));
  expect(scroll, `${selector} scrolls: ${scroll} px of content in ${client} px`).toBeLessThanOrEqual(client);
}

/**
 * How tall the sheet body's content is, and the body itself, in px: the content from the body's
 * top to its last child's bottom plus the bottom padding (scrollHeight cannot say it when the
 * content fits), and how many advice steps and news items it shows. Recorded on the test as a
 * `sheet-body` annotation (or `type`) and in the log, for the PR's numbers.
 */
export async function sheetBodyHeight(page: Page, project: string, selector = '.sbody', type = 'sheet-body') {
  const m = await page.locator(selector).first().evaluate((e) => {
    const box = e.getBoundingClientRect();
    const last = e.lastElementChild?.getBoundingClientRect();
    const pad = parseFloat(getComputedStyle(e).paddingBottom) || 0;
    return {
      content: Math.ceil((last ? last.bottom - box.top + e.scrollTop : 0) + pad),
      body: Math.round(e.clientHeight),
      steps: e.querySelectorAll('.rsteps li').length,
      news: e.querySelectorAll('.rnews .ni').length,
    };
  });
  const description = `${project}: ${m.content} px of content in ${m.body} px; ${m.steps} ${m.steps === 1 ? 'step' : 'steps'} and ${m.news} news ${m.news === 1 ? 'item' : 'items'} shown`;
  test.info().annotations.push({ type, description });
  // also in the run's log, where CI's numbers can be read without the report
  console.log(`${type} · ${test.info().title} · ${description}`);
  return m;
}

export async function shot(page: Page, name: string, project: string) {
  // SHOT_DIR lets a private run (another port and outDir) keep its screenshots apart.
  await page.screenshot({ path: `${process.env.SHOT_DIR ?? 'screenshots'}/${project}/${name}.png`, animations: 'disabled' });
}

export { expect };
