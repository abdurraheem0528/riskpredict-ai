// A small History API router: the dashboard has eight routes and no nested layouts.
import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent } from 'react';
import { withTransition } from '../live/transitions';

const EVT = 'rp:navigate';
const snapshot = () => window.location.pathname + window.location.search;

// One set of listeners and one popstate handler, so Back and Forward can run the screen
// change inside a single view transition (src/live/transitions.ts).
const listeners = new Set<() => void>();
let shown = typeof window === 'undefined' ? '' : snapshot();
const notify = () => {
  shown = snapshot();
  for (const l of [...listeners]) l();
};
let wired = false;
function wire() {
  if (wired) return;
  wired = true;
  window.addEventListener('popstate', () => {
    replaceAlias();
    withTransition(shown, snapshot(), notify);
  });
  window.addEventListener(EVT, notify);
}
const subscribe = (l: () => void) => {
  wire();
  listeners.add(l);
  return () => {
    listeners.delete(l);
  };
};

/**
 * Other addresses for a route. The nav calls the Connect screen "Sources", so `/sources`
 * opens it too, and `/overview` is the Overview at `/` (web/public/_redirects sends both there
 * on Cloudflare Pages as well). `/risks` and `/register` are both the Risks list.
 */
export const ALIASES: Record<string, string> = { '/sources': '/connect', '/overview': '/' };

/** `/sources?x=1` → `/connect?x=1`, `/register/` → `/register`; anything else unchanged. */
export function canonical(href: string): string {
  const m = /^([^?#]*)(.*)$/.exec(href)!;
  const path = m[1].length > 1 ? m[1].replace(/\/+$/, '') || '/' : m[1];
  return (ALIASES[path] ?? path) + m[2];
}

function replaceAlias() {
  if (typeof window === 'undefined') return;
  const { pathname, search, hash } = window.location;
  const to = canonical(pathname);
  if (to !== pathname) window.history.replaceState(window.history.state, '', to + search + hash);
}
// An alias typed or bookmarked is replaced before the first render.
replaceAlias();

export function navigate(href: string, opts: { replace?: boolean } = {}) {
  const to = canonical(href);
  const from = snapshot();
  if (to === from) return;
  // List ↔ risk morphs the title and slides the sheet; anything else swaps at once.
  withTransition(from, to, () => {
    if (opts.replace) window.history.replaceState(null, '', to);
    else window.history.pushState(null, '', to);
    window.dispatchEvent(new Event(EVT));
  });
}

export function useLocation() {
  const href = useSyncExternalStore(subscribe, snapshot, snapshot);
  const url = new URL(href, 'http://x');
  return { path: canonical(url.pathname), query: url.searchParams, href };
}

/** Match `/risks/:id` style patterns. */
export function match(pattern: string, path: string): Record<string, string> | null {
  const p = pattern.split('/').filter(Boolean);
  const s = path.split('/').filter(Boolean);
  if (p.length !== s.length) return null;
  const out: Record<string, string> = {};
  for (let i = 0; i < p.length; i++) {
    if (p[i].startsWith(':')) out[p[i].slice(1)] = decodeURIComponent(s[i]);
    else if (p[i] !== s[i]) return null;
  }
  return out;
}

export function Link({ to, onClick, ...rest }: { to: string } & AnchorHTMLAttributes<HTMLAnchorElement>) {
  const go = (e: MouseEvent<HTMLAnchorElement>) => {
    onClick?.(e);
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey || e.altKey) return;
    e.preventDefault();
    navigate(to);
  };
  return <a href={canonical(to)} onClick={go} {...rest} />;
}
