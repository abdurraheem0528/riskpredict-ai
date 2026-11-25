import { beforeEach, describe, expect, it } from 'vitest';
import { http, HttpResponse } from 'msw';
import { server } from '../setup';
import { __resetAuth, accessToken, getAuthStatus, refresh, signIn } from '../../src/api/auth';
import { api, unwrap } from '../../src/api/client';
import { API, tokens, user, WS } from '../fixtures/data';

const RT = 'rp.refresh';

describe('auth', () => {
  beforeEach(() => {
    localStorage.clear();
    __resetAuth();
  });

  it('keeps the access token in memory and only the refresh token in storage', async () => {
    await signIn('mira.okafor@kestrel.test', 'correct horse battery');
    expect(getAuthStatus()).toBe('signed-in');
    expect(localStorage.getItem(RT)).toBe(tokens().refresh_token);
    expect(Object.values({ ...localStorage }).join(' ')).not.toContain('test-access-token');
    expect(document.cookie).toBe('');
  });

  it('rotates the refresh token, once for concurrent callers', async () => {
    localStorage.setItem(RT, 'old');
    __resetAuth();
    let calls = 0;
    const seen: string[] = [];
    server.use(
      http.post(`${API}/v1/auth/refresh`, async ({ request }) => {
        calls++;
        seen.push(((await request.json()) as { refresh_token: string }).refresh_token);
        return HttpResponse.json({ ...tokens(), refresh_token: 'new' });
      }),
    );
    const [a, b] = await Promise.all([refresh(), refresh()]);
    expect(a).toBe('test-access-token');
    expect(b).toBe(a);
    expect(calls).toBe(1);
    expect(seen).toEqual(['old']);
    expect(localStorage.getItem(RT)).toBe('new');
  });

  it('signs out when the refresh token is rejected (reuse, expiry)', async () => {
    localStorage.setItem(RT, 'reused');
    __resetAuth();
    server.use(http.post(`${API}/v1/auth/refresh`, () => HttpResponse.json({ code: 'refresh_reused', status: 401, title: 'x', type: 'about:blank' }, { status: 401 })));
    expect(await accessToken()).toBeNull();
    expect(getAuthStatus()).toBe('signed-out');
    expect(localStorage.getItem(RT)).toBeNull();
  });

  it('keeps the session when refresh fails for a network reason', async () => {
    localStorage.setItem(RT, 'kept');
    __resetAuth();
    server.use(http.post(`${API}/v1/auth/refresh`, () => HttpResponse.json({}, { status: 503 })));
    expect(await refresh()).toBeNull();
    expect(localStorage.getItem(RT)).toBe('kept');
  });

  it('sends the bearer token and X-Workspace-Id, and retries once after a 401', async () => {
    await signIn('mira.okafor@kestrel.test', 'correct horse battery');
    let n = 0;
    const auth: (string | null)[] = [];
    server.use(
      http.get(`${API}/v1/connections`, ({ request }) => {
        n++;
        auth.push(request.headers.get('Authorization'));
        expect(request.headers.get('X-Workspace-Id')).toBe(WS);
        if (n === 1) return HttpResponse.json({ code: 'token_expired', status: 401, title: 'x', type: 'about:blank' }, { status: 401 });
        return HttpResponse.json({ items: [] });
      }),
    );
    const r = await unwrap(api.GET('/v1/connections', { params: { header: { 'X-Workspace-Id': WS } } }));
    expect(r.items).toEqual([]);
    expect(n).toBe(2);
    expect(auth.every((a) => a === 'Bearer test-access-token')).toBe(true);
    expect(user.id).toBeTruthy();
  });

  it('turns problems into ApiError with the stable code', async () => {
    await expect(signIn('a@b.c', 'wrong')).rejects.toMatchObject({ status: 401, code: 'invalid_credentials' });
  });
});
