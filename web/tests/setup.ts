import '@testing-library/jest-dom/vitest';
import { afterAll, afterEach, beforeAll, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { setupServer } from 'msw/node';
import { handlers } from './fixtures/handlers';
import { __setGazetteer } from '../src/map/geo';

export const server = setupServer(...handlers('active'));

// jsdom has no WebGL: the map reports "failed" and every layer renders nothing.
vi.mock('maplibre-gl', () => ({
  Map: class {
    constructor() {
      throw new Error('WebGL is not available');
    }
  },
}));

class StubEventSource extends EventTarget {
  onopen: (() => void) | null = null;
  onerror: (() => void) | null = null;
  constructor(public url: string) {
    super();
  }
  close() {}
}
(globalThis as unknown as { EventSource: unknown }).EventSource = StubEventSource;

__setGazetteer(
  { TUR: [35.2, 39.1, 'Turkey'], CHN: [104.2, 35.9, 'China'], TWN: [121.0, 23.7, 'Taiwan'], NLD: [5.3, 52.1, 'Netherlands'], YEM: [47.6, 15.9, 'Yemen'] },
  { CNYTN: [114.27, 22.57, 'Yantian'], NLRTM: [4.4, 51.9, 'Rotterdam'], TWKHH: [120.27, 22.6, 'Kao-Hsiung'] },
);

beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => {
  cleanup();
  server.resetHandlers();
  localStorage.clear();
  window.history.replaceState(null, '', '/');
});
afterAll(() => server.close());
