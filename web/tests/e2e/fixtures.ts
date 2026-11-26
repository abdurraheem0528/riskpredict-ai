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
  await expect(page.locator('.mapbox[data-map="idle"]')).toBeAttached({ timeout: 30_000 });
  await page.waitForTimeout(600);
  await expect(page.locator('.mapbox[data-map="idle"]')).toBeAttached({ timeout: 30_000 });
}

export async function shot(page: Page, name: string, project: string) {
  // SHOT_DIR lets a private run (another port and outDir) keep its screenshots apart.
  await page.screenshot({ path: `${process.env.SHOT_DIR ?? 'screenshots'}/${project}/${name}.png`, animations: 'disabled' });
}

export { expect };
