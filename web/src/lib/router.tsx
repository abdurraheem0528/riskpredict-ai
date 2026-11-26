// A small History API router: the dashboard has eight routes and no nested layouts.
import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent } from 'react';

const EVT = 'rp:navigate';
const subscribe = (l: () => void) => {
  const pop = () => {
    replaceAlias();
    l();
  };
  window.addEventListener('popstate', pop);
  window.addEventListener(EVT, l);
  return () => {
    window.removeEventListener('popstate', pop);
    window.removeEventListener(EVT, l);
  };
};
const snapshot = () => window.location.pathname + window.location.search;

/**
 * Other addresses for a route. The nav calls the Connect screen "Sources", so `/sources`
 * opens it too (web/public/_redirects sends it there on Cloudflare Pages as well).
 */
export const ALIASES: Record<string, string> = { '/sources': '/connect' };

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
  if (to === snapshot()) return;
  if (opts.replace) window.history.replaceState(null, '', to);
  else window.history.pushState(null, '', to);
  window.dispatchEvent(new Event(EVT));
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
