// Light / Dark / System (src/lib/theme.ts) and the pre-paint script (public/theme-init.js).
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import initScript from '../../public/theme-init.js?raw';

type ThemeModule = typeof import('../../src/lib/theme');

/** A controllable prefers-color-scheme: dark (jsdom has no matchMedia). */
function mockSystem(dark: boolean) {
  const state = { dark };
  const listeners = new Set<(e: { matches: boolean }) => void>();
  const darkQuery = {
    media: '(prefers-color-scheme: dark)',
    get matches() {
      return state.dark;
    },
    addEventListener: (_: string, l: (e: { matches: boolean }) => void) => listeners.add(l),
    removeEventListener: (_: string, l: (e: { matches: boolean }) => void) => listeners.delete(l),
  };
  const other = { matches: false, addEventListener: () => undefined, removeEventListener: () => undefined };
  window.matchMedia = vi.fn((q: string) => (q.includes('prefers-color-scheme: dark') ? darkQuery : other)) as unknown as typeof window.matchMedia;
  return {
    set(next: boolean) {
      state.dark = next;
      for (const l of listeners) l({ matches: next });
    },
  };
}

/** A fresh copy of the module: its choice is read once, when it loads. */
async function load(): Promise<ThemeModule> {
  vi.resetModules();
  return import('../../src/lib/theme');
}

const root = () => document.documentElement;
const meta = () => document.querySelector('meta[name="theme-color"]')!.getAttribute('content');

beforeEach(() => {
  const m = document.createElement('meta');
  m.name = 'theme-color';
  m.content = '#f4f6f8';
  document.head.append(m);
});
afterEach(() => {
  document.head.querySelectorAll('meta[name="theme-color"]').forEach((m) => m.remove());
  root().removeAttribute('data-theme');
  // @ts-expect-error jsdom has none; each test installs its own
  delete window.matchMedia;
  vi.restoreAllMocks();
});

describe('theme choice', () => {
  it('reads the stored choice, and falls back to System for anything else', async () => {
    const t = await load();
    expect(t.readChoice()).toBe('system');
    localStorage.setItem(t.THEME_KEY, 'dark');
    expect(t.readChoice()).toBe('dark');
    localStorage.setItem(t.THEME_KEY, 'light');
    expect(t.readChoice()).toBe('light');
    localStorage.setItem(t.THEME_KEY, 'sepia');
    expect(t.readChoice()).toBe('system');
    expect(t.parseChoice(null)).toBe('system');
  });

  it('falls back to System when storage is blocked', async () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    const t = await load();
    expect(t.readChoice()).toBe('system');
    expect(t.getThemeChoice()).toBe('system');
  });

  it('System resolves to the system theme; Light and Dark ignore it', async () => {
    const t = await load();
    expect(t.resolveTheme('system', true)).toBe('dark');
    expect(t.resolveTheme('system', false)).toBe('light');
    expect(t.resolveTheme('light', true)).toBe('light');
    expect(t.resolveTheme('dark', false)).toBe('dark');
    // no matchMedia at all (an old browser): System is light
    expect(t.resolveTheme('system')).toBe('light');
  });

  it('keeps the choice, sets data-theme and the theme colour; System removes both overrides', async () => {
    mockSystem(false);
    const t = await load();
    t.setThemeChoice('dark');
    expect(localStorage.getItem('rp.theme')).toBe('dark');
    expect(root().getAttribute('data-theme')).toBe('dark');
    expect(meta()).toBe('#080c12');
    expect(t.getTheme()).toBe('dark');

    t.setThemeChoice('light');
    expect(localStorage.getItem('rp.theme')).toBe('light');
    expect(root().getAttribute('data-theme')).toBe('light');
    expect(meta()).toBe('#f4f6f8');

    t.setThemeChoice('system');
    expect(localStorage.getItem('rp.theme')).toBeNull();
    expect(root().hasAttribute('data-theme')).toBe(false);
    expect(t.getTheme()).toBe('light');
  });

  it('still applies a choice when storage refuses to save it', async () => {
    mockSystem(false);
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('full', 'QuotaExceededError');
    });
    const t = await load();
    expect(() => t.setThemeChoice('dark')).not.toThrow();
    expect(t.getThemeChoice()).toBe('dark');
    expect(root().getAttribute('data-theme')).toBe('dark');
  });

  it('applies the stored choice at start-up', async () => {
    mockSystem(false);
    localStorage.setItem('rp.theme', 'dark');
    const t = await load();
    t.initTheme();
    expect(t.getThemeChoice()).toBe('dark');
    expect(root().getAttribute('data-theme')).toBe('dark');
    expect(meta()).toBe('#080c12');
  });

  it('under System, follows the system theme as it changes and tells the map', async () => {
    const system = mockSystem(true);
    const t = await load();
    t.initTheme();
    expect(t.getTheme()).toBe('dark');
    expect(root().hasAttribute('data-theme')).toBe(false);
    expect(meta()).toBe('#080c12');

    const seen: string[] = [];
    const off = t.onThemeChange((x) => seen.push(x));
    system.set(false);
    expect(t.getTheme()).toBe('light');
    expect(meta()).toBe('#f4f6f8');
    expect(seen).toEqual(['light']);

    // an explicit choice no longer follows the system
    t.setThemeChoice('dark');
    expect(seen).toEqual(['light', 'dark']);
    system.set(true);
    system.set(false);
    expect(t.getTheme()).toBe('dark');
    expect(seen).toEqual(['light', 'dark']);

    off();
    t.setThemeChoice('light');
    expect(seen).toEqual(['light', 'dark']);
  });

  it('does not report a change when the resolved theme stays the same', async () => {
    mockSystem(false);
    const t = await load();
    t.initTheme();
    const seen: string[] = [];
    t.onThemeChange((x) => seen.push(x));
    t.setThemeChoice('light'); // System was light already
    t.setThemeChoice('system');
    expect(seen).toEqual([]);
  });
});

describe('theme-init.js (before first paint)', () => {
  const run = () => new Function(initScript)();

  it('sets data-theme and the theme colour from the stored choice', () => {
    mockSystem(false);
    localStorage.setItem('rp.theme', 'dark');
    run();
    expect(root().getAttribute('data-theme')).toBe('dark');
    expect(meta()).toBe('#080c12');
  });

  it('leaves System to the stylesheet, with the theme colour of the system theme', () => {
    mockSystem(true);
    root().setAttribute('data-theme', 'light');
    run();
    expect(root().hasAttribute('data-theme')).toBe(false);
    expect(meta()).toBe('#080c12');
  });

  it('survives blocked storage and a browser without matchMedia', () => {
    vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('blocked', 'SecurityError');
    });
    expect(run).not.toThrow();
    expect(root().hasAttribute('data-theme')).toBe(false);
    expect(meta()).toBe('#f4f6f8');
  });
});
