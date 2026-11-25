// Auth per docs/contracts (decision 6): a 15-minute access JWT kept only in memory, and a
// 30-day opaque refresh token that is rotated on every use. No cookies (the API is on
// another site). The refresh token has to outlive a reload, so it is the one thing kept in
// localStorage. Rotation is single-use with reuse detection, so two tabs must never
// refresh with the same token: refreshes are serialised across tabs with the Web Locks
// API, and each one re-reads the newest token from storage first.
import type { components } from './schema';
import { API_URL } from './config';
import { ApiError, problemFrom } from './errors';

type Tokens = components['schemas']['Tokens'];
export type AuthResult = components['schemas']['AuthResult'];

const RT_KEY = 'rp.refresh';
export type AuthStatus = 'unknown' | 'signed-in' | 'signed-out';

let access: { token: string; exp: number } | null = null;
let status: AuthStatus = readRefresh() ? 'unknown' : 'signed-out';
const listeners = new Set<() => void>();

function readRefresh(): string | null {
  try {
    return localStorage.getItem(RT_KEY);
  } catch {
    return null;
  }
}
function writeRefresh(token: string | null) {
  try {
    if (token) localStorage.setItem(RT_KEY, token);
    else localStorage.removeItem(RT_KEY);
  } catch {
    /* storage blocked: the session lasts until the tab closes */
  }
}

function setStatus(s: AuthStatus) {
  if (s === status) return;
  status = s;
  listeners.forEach((l) => l());
}
export const getAuthStatus = () => status;
export function subscribeAuth(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

function store(t: Tokens) {
  access = { token: t.access_token, exp: new Date(t.access_token_expires_at).getTime() };
  writeRefresh(t.refresh_token);
  setStatus('signed-in');
}

export function clearSession() {
  access = null;
  writeRefresh(null);
  setStatus('signed-out');
}

async function post<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) throw await problemFrom(res);
  return (res.status === 204 ? undefined : await res.json()) as T;
}

export async function signIn(email: string, password: string): Promise<AuthResult> {
  const r = await post<AuthResult>('/v1/auth/sign-in', { email, password });
  store(r);
  return r;
}

export async function signUp(body: { email: string; password: string; display_name?: string; workspace_name: string }) {
  const r = await post<AuthResult>('/v1/auth/sign-up', body);
  store(r);
  return r;
}

export async function signOut() {
  const rt = readRefresh();
  clearSession();
  if (rt) await post('/v1/auth/sign-out', { refresh_token: rt }).catch(() => undefined);
}

let inflight: Promise<string | null> | null = null;

async function doRefresh(): Promise<string | null> {
  const rt = readRefresh();
  if (!rt) {
    clearSession();
    return null;
  }
  try {
    const t = await post<Tokens>('/v1/auth/refresh', { refresh_token: rt });
    store(t);
    return t.access_token;
  } catch (e) {
    // 401 (expired, revoked or reused): the session is over. Anything else (offline, 5xx)
    // keeps the refresh token so the next attempt can succeed.
    if (e instanceof ApiError && e.status === 401) clearSession();
    else if (status === 'unknown') setStatus('signed-out');
    return null;
  }
}

/** Rotate the refresh token; concurrent callers in this tab share one request. */
export function refresh(): Promise<string | null> {
  if (!inflight) {
    const locks = typeof navigator !== 'undefined' ? navigator.locks : undefined;
    const run = locks ? locks.request('rp-refresh', doRefresh) : doRefresh();
    inflight = Promise.resolve(run).finally(() => {
      inflight = null;
    });
  }
  return inflight;
}

/** A valid access token, refreshing if it is missing or about to expire. */
export async function accessToken(): Promise<string | null> {
  if (access && access.exp - Date.now() > 30_000) return access.token;
  return refresh();
}

/** For tests only. */
export function __resetAuth() {
  access = null;
  inflight = null;
  status = readRefresh() ? 'unknown' : 'signed-out';
}
