/// <reference types="vitest/config" />
import { createHash } from 'node:crypto';
import { readFileSync, writeFileSync, existsSync } from 'node:fs';
import { resolve } from 'node:path';
import { defineConfig, loadEnv, type Plugin } from 'vite';
import react from '@vitejs/plugin-react';
import { API_ORIGIN_TOKEN, apiOrigin, headersFor, parseHeaders, renderHeaders } from './scripts/pages-headers';

/**
 * dist/_headers with the API origin of this build in its Content-Security-Policy, and the
 * same headers on `vite preview` (the server the Playwright run uses), so the e2e runs
 * under the policy Cloudflare Pages serves.
 */
function pagesHeaders(): Plugin {
  let outDir = 'dist';
  let origin = '';
  return {
    name: 'rp-pages-headers',
    configResolved(c) {
      outDir = resolve(c.root, c.build.outDir);
      const env = loadEnv(c.mode, c.envDir || c.root, 'VITE_');
      origin = apiOrigin(process.env.VITE_API_URL || env.VITE_API_URL || process.env.VITE_API_BASE_URL || env.VITE_API_BASE_URL);
    },
    closeBundle() {
      const file = resolve(outDir, '_headers');
      if (!existsSync(file)) return;
      const out = renderHeaders(readFileSync(file, 'utf8'), origin);
      if (out.includes(API_ORIGIN_TOKEN)) throw new Error('dist/_headers still has the API origin placeholder');
      writeFileSync(file, out);
    },
    configurePreviewServer(server) {
      const file = resolve(server.config.root, server.config.build.outDir, '_headers');
      if (!existsSync(file)) return;
      const rules = parseHeaders(readFileSync(file, 'utf8'));
      server.middlewares.use((req, res, next) => {
        const path = (req.url ?? '/').split('?')[0];
        for (const [k, v] of Object.entries(headersFor(rules, path))) res.setHeader(k, v);
        next();
      });
    },
  };
}

// The gazetteer's URL carries a hash of its files (src/map/geo.ts), so a release with new
// port names or positions is never masked by a copy cached under the same URL.
const mapDataVersion = createHash('sha256')
  .update(['centroids.json', 'ports.json'].map((f) => readFileSync(new URL(`./public/map/${f}`, import.meta.url))).join('\n'))
  .digest('hex')
  .slice(0, 12);

export default defineConfig({
  plugins: [react(), pagesHeaders()],
  define: { __MAP_DATA_VERSION__: JSON.stringify(mapDataVersion) },
  build: {
    target: 'es2022',
    sourcemap: false,
    chunkSizeWarningLimit: 1100,
    rollupOptions: {
      output: {
        // MapLibre is most of the bytes and changes rarely: keep it in its own chunk so
        // app releases don't invalidate it in the browser cache.
        manualChunks: (id) => (id.includes('node_modules/maplibre-gl') ? 'maplibre' : undefined),
      },
    },
  },
  test: {
    environment: 'jsdom',
    setupFiles: ['./tests/setup.ts'],
    include: ['tests/unit/**/*.test.{ts,tsx}'],
    css: false,
    env: { VITE_API_URL: 'http://api.test' },
  },
});
