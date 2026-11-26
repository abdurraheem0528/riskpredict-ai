// Light / Dark / System. The choice is kept in localStorage (`rp.theme`) and set as
// `data-theme` on <html> ("system" removes it, so the prefers-color-scheme block in
// styles.css applies). public/theme-init.js applies it before first paint; this module
// keeps it in step afterwards and tells the map when the resolved theme changes.
import { useSyncExternalStore } from 'react';

export type ThemeChoice = 'light' | 'dark' | 'system';
export type Theme = 'light' | 'dark';

export const THEME_KEY = 'rp.theme';
const CHOICES: readonly ThemeChoice[] = ['light', 'dark', 'system'];
const DARK_QUERY = '(prefers-color-scheme: dark)';
/** <meta name="theme-color"> per resolved theme: the space behind the globe. */
export const THEME_COLOR: Record<Theme, string> = { light: '#f4f6f8', dark: '#080c12' };

export function parseChoice(v: unknown): ThemeChoice {
  return CHOICES.includes(v as ThemeChoice) ? (v as ThemeChoice) : 'system';
}

export function readChoice(): ThemeChoice {
  try {
    return parseChoice(localStorage.getItem(THEME_KEY));
  } catch {
    return 'system'; // storage blocked
  }
}

function systemDark(): boolean {
  return typeof window !== 'undefined' && !!window.matchMedia?.(DARK_QUERY).matches;
}

/** The theme a choice resolves to; "system" follows prefers-color-scheme. */
export function resolveTheme(choice: ThemeChoice, prefersDark = systemDark()): Theme {
  return choice === 'system' ? (prefersDark ? 'dark' : 'light') : choice;
}

let choice: ThemeChoice = typeof window === 'undefined' ? 'system' : readChoice();
let resolved: Theme = resolveTheme(choice);
const listeners = new Set<() => void>();
const themeListeners = new Set<(t: Theme) => void>();

function apply() {
  if (typeof document === 'undefined') return;
  const root = document.documentElement;
  if (choice === 'system') root.removeAttribute('data-theme');
  else root.setAttribute('data-theme', choice);
  const next = resolveTheme(choice);
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.setAttribute('content', THEME_COLOR[next]);
  if (next !== resolved) {
    resolved = next;
    for (const l of themeListeners) l(next);
  }
}

let wired = false;
function wire() {
  if (wired || typeof window === 'undefined') return;
  wired = true;
  window.matchMedia?.(DARK_QUERY).addEventListener?.('change', () => {
    if (choice === 'system') apply();
  });
  // Another tab changed it.
  window.addEventListener('storage', (e) => {
    if (e.key === THEME_KEY) setThemeChoice(parseChoice(e.newValue), false);
  });
}

export function getThemeChoice(): ThemeChoice {
  return choice;
}
export function getTheme(): Theme {
  return resolved;
}

export function setThemeChoice(next: ThemeChoice, persist = true) {
  wire();
  choice = parseChoice(next);
  if (persist) {
    try {
      if (choice === 'system') localStorage.removeItem(THEME_KEY);
      else localStorage.setItem(THEME_KEY, choice);
    } catch {
      /* storage blocked: the choice lasts for this page */
    }
  }
  apply();
  for (const l of listeners) l();
}

/** Call once at start-up: applies the stored choice (theme-init.js has normally done so). */
export function initTheme() {
  wire();
  choice = readChoice();
  resolved = resolveTheme(choice);
  apply();
}

/** Runs when the resolved theme changes (a choice, or the system theme under "system"). */
export function onThemeChange(fn: (t: Theme) => void): () => void {
  wire();
  themeListeners.add(fn);
  return () => {
    themeListeners.delete(fn);
  };
}

const subscribe = (l: () => void) => {
  wire();
  listeners.add(l);
  const off = onThemeChange(l);
  return () => {
    listeners.delete(l);
    off();
  };
};

export function useThemeChoice(): ThemeChoice {
  return useSyncExternalStore(subscribe, getThemeChoice, () => 'system' as ThemeChoice);
}
export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, () => 'light' as Theme);
}

// --- map palette ------------------------------------------------------------------------------

/** The map's colours, read from the CSS tokens on :root (styles.css), so they follow the theme. */
export type Palette = {
  ink: string; tx2: string; off: string;
  sea: string; land: string; coast: string; border: string; space: string; grat: string;
  accent: string; lane: string; signal: string; dotStroke: string; halo: string;
  low: string; med: string; high: string; crit: string;
  lowText: string; medText: string; highText: string; critText: string;
  tintK: number; atmo: number;
};

const FALLBACK: Palette = {
  ink: '#141518', tx2: '#5f646c', off: '#a3a7ae',
  sea: '#e7ecf1', land: '#ffffff', coast: '#cfd5dc', border: '#dbe0e6', space: '#f4f6f8', grat: 'rgba(30,60,100,.08)',
  accent: '#1766f0', lane: '#1766f0', signal: '#8a9098', dotStroke: '#ffffff', halo: '#ffffff',
  low: '#9aa1a9', med: '#e9a190', high: '#dd6248', crit: '#b3200f',
  lowText: '#5f646c', medText: '#9c3522', highText: '#a42d16', critText: '#8f1a0b',
  tintK: 1, atmo: 0.12,
};

export function palette(): Palette {
  if (typeof document === 'undefined' || typeof getComputedStyle === 'undefined') return FALLBACK;
  const cs = getComputedStyle(document.documentElement);
  const g = (n: string, fb: string) => cs.getPropertyValue(n).trim() || fb;
  const num = (n: string, fb: number) => {
    const v = parseFloat(cs.getPropertyValue(n));
    return Number.isFinite(v) ? v : fb;
  };
  const F = FALLBACK;
  return {
    ink: g('--ink', F.ink), tx2: g('--tx2', F.tx2), off: g('--off', F.off),
    sea: g('--sea', F.sea), land: g('--land', F.land), coast: g('--coast', F.coast), border: g('--border', F.border),
    space: g('--space', F.space), grat: g('--grat', F.grat),
    accent: g('--accent', F.accent), lane: g('--lane', F.lane), signal: g('--signal', F.signal),
    dotStroke: g('--dot-stroke', F.dotStroke), halo: g('--halo', F.halo),
    low: g('--low-fill', F.low), med: g('--med-fill', F.med), high: g('--high-fill', F.high), crit: g('--crit-fill', F.crit),
    lowText: g('--low-text', F.lowText), medText: g('--med-text', F.medText), highText: g('--high-text', F.highText), critText: g('--crit-text', F.critText),
    tintK: num('--tint-k', F.tintK), atmo: num('--atmo', F.atmo),
  };
}

/** The palette, re-read whenever the resolved theme changes. */
export function usePalette(): Palette {
  const t = useTheme();
  return useSyncExternalStore(
    subscribe,
    () => cachedPalette(t),
    () => FALLBACK,
  );
}
let cache: { t: Theme; p: Palette } | null = null;
function cachedPalette(t: Theme): Palette {
  if (!cache || cache.t !== t) cache = { t, p: palette() };
  return cache.p;
}
