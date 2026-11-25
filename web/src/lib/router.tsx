// A small History API router: the dashboard has eight routes and no nested layouts.
import { useSyncExternalStore, type AnchorHTMLAttributes, type MouseEvent } from 'react';

const EVT = 'rp:navigate';
const subscribe = (l: () => void) => {
  window.addEventListener('popstate', l);
  window.addEventListener(EVT, l);
  return () => {
    window.removeEventListener('popstate', l);
    window.removeEventListener(EVT, l);
  };
};
const snapshot = () => window.location.pathname + window.location.search;

export function navigate(to: string, opts: { replace?: boolean } = {}) {
  if (to === snapshot()) return;
  if (opts.replace) window.history.replaceState(null, '', to);
  else window.history.pushState(null, '', to);
  window.dispatchEvent(new Event(EVT));
}

export function useLocation() {
  const href = useSyncExternalStore(subscribe, snapshot, snapshot);
  const url = new URL(href, 'http://x');
  return { path: url.pathname, query: url.searchParams, href };
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
  return <a href={to} onClick={go} {...rest} />;
}
