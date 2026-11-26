/// <reference types="vitest/config" />
import { createHash } from 'node:crypto';
import { readFileSync } from 'node:fs';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

// The gazetteer's URL carries a hash of its files (src/map/geo.ts), so a release with new
// port names or positions is never masked by a copy cached under the same URL.
const mapDataVersion = createHash('sha256')
  .update(['centroids.json', 'ports.json'].map((f) => readFileSync(new URL(`./public/map/${f}`, import.meta.url))).join('\n'))
  .digest('hex')
  .slice(0, 12);

export default defineConfig({
  plugins: [react()],
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
