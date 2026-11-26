// Cloudflare Pages' `_headers` file (public/_headers), shared by the build and the preview
// server that the Playwright run uses. Pure functions only: vite.config.ts does the I/O.
//
// A static `_headers` file cannot read the environment, so its Content-Security-Policy names
// the API as API_ORIGIN_TOKEN and the build writes dist/_headers with the origin of
// VITE_API_URL (the Vercel URL in the deploy workflow, http://api.test for the e2e build).
// Each build allows exactly its own API, and nothing else.

export const API_ORIGIN_TOKEN = '%API_ORIGIN%';

/** `https://x.vercel.app/` → `https://x.vercel.app`; empty or unset → `''`. Throws on anything else. */
export function apiOrigin(url: string | undefined): string {
  const raw = (url ?? '').trim();
  if (!raw) return '';
  let u: URL;
  try {
    u = new URL(raw);
  } catch {
    throw new Error(`VITE_API_URL is not a URL: ${raw}`);
  }
  if (u.protocol !== 'https:' && u.protocol !== 'http:') throw new Error(`VITE_API_URL must be http(s): ${raw}`);
  return u.origin;
}

/** public/_headers → dist/_headers: the API origin in place of its token. */
export function renderHeaders(text: string, origin: string): string {
  if (/[\s;,']/.test(origin)) throw new Error(`Not an origin: ${origin}`);
  // With no API configured the dashboard only says so: connect-src stays 'self'.
  return text.split(` ${API_ORIGIN_TOKEN}`).join(origin ? ` ${origin}` : '').split(API_ORIGIN_TOKEN).join(origin);
}

export type HeaderRule = { pattern: string; headers: [string, string][] };

/** The rules of a `_headers` file: an unindented URL pattern, then indented `Name: value` lines. */
export function parseHeaders(text: string): HeaderRule[] {
  const rules: HeaderRule[] = [];
  for (const line of text.split(/\r?\n/)) {
    if (!line.trim() || line.trim().startsWith('#')) continue;
    if (!/^\s/.test(line)) {
      rules.push({ pattern: line.trim(), headers: [] });
      continue;
    }
    const i = line.indexOf(':');
    if (i < 0 || !rules.length) continue;
    rules[rules.length - 1].headers.push([line.slice(0, i).trim(), line.slice(i + 1).trim()]);
  }
  return rules;
}

/** Every header the rules give a path (`*` is a splat, `:name` one segment), as Pages merges them. */
export function headersFor(rules: HeaderRule[], path: string): Record<string, string> {
  const out: Record<string, string> = {};
  for (const r of rules) {
    const re = new RegExp(
      '^' + r.pattern.split('*').map((part) => part.replace(/[.+?^${}()|[\]\\]/g, '\\$&').replace(/:[A-Za-z]\w*/g, '[^/]+')).join('.*') + '$',
    );
    if (!re.test(path)) continue;
    for (const [k, v] of r.headers) out[k] = out[k] ? `${out[k]}, ${v}` : v;
  }
  return out;
}

/** One CSP header value → its directives, e.g. `{ 'connect-src': ["'self'", 'https://…'] }`. */
export function cspDirectives(value: string): Record<string, string[]> {
  const out: Record<string, string[]> = {};
  for (const d of value.split(';')) {
    const [name, ...src] = d.trim().split(/\s+/);
    if (name) out[name.toLowerCase()] = src;
  }
  return out;
}
