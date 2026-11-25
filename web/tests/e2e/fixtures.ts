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
        // A stream that opens and stays open: page.route cannot hold an SSE response.
        class FakeEventSource extends EventTarget {
          readyState = 0;
          onopen: ((e: Event) => void) | null = null;
          onerror: ((e: Event) => void) | null = null;
          constructor(public url: string) {
            super();
            setTimeout(() => {
              this.readyState = 1;
              this.onopen?.(new Event('open'));
            }, 50);
          }
          close() {
            this.readyState = 2;
          }
        }
        (window as unknown as { EventSource: unknown }).EventSource = FakeEventSource;
      },
      [WS, signedIn],
    );
    await use(page);
  },
});

/** The map has drawn and settled; HTML markers have had a frame to land. */
export async function mapSettled(page: Page) {
  await expect(page.locator('.mapbox[data-map="idle"]')).toBeAttached({ timeout: 30_000 });
  await page.waitForTimeout(600);
  await expect(page.locator('.mapbox[data-map="idle"]')).toBeAttached({ timeout: 30_000 });
}

export async function shot(page: Page, name: string, project: string) {
  await page.screenshot({ path: `screenshots/${project}/${name}.png`, animations: 'disabled' });
}

export { expect };
