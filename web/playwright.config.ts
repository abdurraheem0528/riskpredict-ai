// Screenshots of every screen at the three widths the designs define, against the MSW
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
  ],
  webServer: {
    command: 'npx vite preview --port 4173 --strictPort',
    url: 'http://localhost:4173',
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
