// Screenshots of every screen at the three widths the designs define (and at 1360 and 375 in
// the dark theme), against the MSW
// fixtures in tests/fixtures (routed with @msw/playwright, so no mock code is in the app).
// Run after `VITE_API_URL=http://api.test npm run build`.
import { defineConfig } from '@playwright/test';

const chromium = process.env.PW_CHROMIUM_PATH;

export default defineConfig({
  testDir: 'tests/e2e',
  outputDir: 'test-results',
  fullyParallel: true,
  workers: process.env.CI ? 2 : 3,
  retries: process.env.CI ? 1 : 0,
  timeout: 60_000,
  // The globe draws slowly on SwiftShader (no GPU on CI): the first frame of a screen can
  // take about 5 s at device scale 2.
  expect: { timeout: 15_000 },
  reporter: process.env.CI ? [['list'], ['html', { open: 'never' }]] : 'list',
  use: {
    baseURL: 'http://localhost:4173',
    trace: 'off',
    // Screenshots stay stable: no rotation, flow, ripples or count-ups. tests/e2e/live.spec.ts
    // turns motion back on to check the live sequence.
    reducedMotion: 'reduce',
    launchOptions: {
      ...(chromium ? { executablePath: chromium } : {}),
      // WebGL in headless Chromium without a GPU
      args: ['--use-angle=swiftshader', '--enable-unsafe-swiftshader', '--ignore-gpu-blocklist'],
    },
  },
  projects: [
    { name: 'desktop', use: { viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 } },
    { name: 'tablet', use: { viewport: { width: 900, height: 1000 }, deviceScaleFactor: 2 } },
    { name: 'phone', use: { viewport: { width: 375, height: 812 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true } },
    // The night map: every screen again in the dark theme (System, with the OS set to dark).
    ...(['desktop', 'phone'] as const).map((n) => ({
      name: `${n}-dark`,
      testMatch: 'screens.spec.ts',
      grepInvert: /security headers|sources is the connect screen|register to a risk and back/,
      use: {
        ...(n === 'desktop'
          ? { viewport: { width: 1360, height: 900 }, deviceScaleFactor: 2 }
          : { viewport: { width: 375, height: 812 }, deviceScaleFactor: 2, hasTouch: true, isMobile: true }),
        colorScheme: 'dark' as const,
      },
    })),
  ],
  webServer: {
    command: 'npx vite preview --port 4173 --strictPort',
    url: 'http://localhost:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
