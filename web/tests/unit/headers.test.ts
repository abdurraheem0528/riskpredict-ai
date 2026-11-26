// public/_headers (Cloudflare Pages) and public/_redirects, read as the build ships them.
import { describe, expect, it } from 'vitest';
import headersFile from '../../public/_headers?raw';
import redirectsFile from '../../public/_redirects?raw';
import { API_ORIGIN_TOKEN, apiOrigin, cspDirectives, headersFor, parseHeaders, renderHeaders } from '../../scripts/pages-headers';
import { canonical } from '../../src/lib/router';

const PROD = 'https://riskpredict-api.vercel.app';

describe('_headers', () => {
  const rules = parseHeaders(renderHeaders(headersFile, apiOrigin(`${PROD}/`)));
  const all = headersFor(rules, '/');

  it('sends HSTS for a year with subdomains, not preloaded', () => {
    expect(all['Strict-Transport-Security']).toBe('max-age=31536000; includeSubDomains');
  });

  it('sends a Content-Security-Policy on every page', () => {
    for (const p of ['/', '/connect', '/risks/abc', '/assets/index-x.js', '/map/countries.geojson']) {
      expect(headersFor(rules, p)['Content-Security-Policy']).toBe(all['Content-Security-Policy']);
    }
    const csp = cspDirectives(all['Content-Security-Policy']);
    expect(csp['default-src']).toEqual(["'self'"]);
    expect(csp['script-src']).toEqual(["'self'", 'https://apis.google.com']);
    expect(csp['connect-src']).toEqual(["'self'", PROD]);
    expect(csp['worker-src']).toContain('blob:');
    expect(csp['child-src']).toContain('blob:');
    expect(csp['frame-src']).toContain('https://docs.google.com');
    expect(csp['img-src']).toEqual(expect.arrayContaining(["'self'", 'data:', 'blob:']));
    expect(csp['frame-ancestors']).toEqual(["'none'"]);
    expect(csp['object-src']).toEqual(["'none'"]);
    expect(csp['base-uri']).toEqual(["'self'"]);
    expect(csp['form-action']).toEqual(["'self'"]);
    const text = all['Content-Security-Policy'];
    expect(text).not.toContain("'unsafe-eval'");
    expect(csp['script-src']).not.toContain("'unsafe-inline'");
    expect(text).not.toContain(API_ORIGIN_TOKEN);
  });

  it('keeps the other headers and the cache rules', () => {
    expect(all['X-Frame-Options']).toBe('DENY');
    expect(all['X-Content-Type-Options']).toBe('nosniff');
    expect(headersFor(rules, '/assets/index-x.js')['Cache-Control']).toContain('immutable');
    expect(headersFor(rules, '/connect')['Cache-Control']).toBeUndefined();
  });

  it('allows only the API of the build', () => {
    const e2e = cspDirectives(headersFor(parseHeaders(renderHeaders(headersFile, apiOrigin('http://api.test'))), '/')['Content-Security-Policy']);
    expect(e2e['connect-src']).toEqual(["'self'", 'http://api.test']);
    const none = cspDirectives(headersFor(parseHeaders(renderHeaders(headersFile, apiOrigin(''))), '/')['Content-Security-Policy']);
    expect(none['connect-src']).toEqual(["'self'"]);
    expect(apiOrigin('https://x.example/v1/')).toBe('https://x.example');
    expect(() => apiOrigin('ftp://x.example')).toThrow();
    expect(() => apiOrigin('not a url')).toThrow();
  });
});

describe('/sources', () => {
  it('is an address of the Connect screen, in the router and on Pages', () => {
    expect(canonical('/sources')).toBe('/connect');
    expect(canonical('/sources/?a=1')).toBe('/connect?a=1');
    expect(canonical('/sources/abc/confirm')).toBe('/sources/abc/confirm');
    expect(canonical('/register/')).toBe('/register');
    expect(canonical('/')).toBe('/');
    const lines = redirectsFile.split('\n').filter((l) => l.trim() && !l.startsWith('#')).map((l) => l.trim().split(/\s+/));
    // The first matching rule wins: the alias must come before the SPA catch-all.
    expect(lines[0]).toEqual(['/sources', '/connect', '302']);
    expect(lines.at(-1)).toEqual(['/*', '/index.html', '200']);
  });
});
