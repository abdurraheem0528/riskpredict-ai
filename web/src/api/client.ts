import createClient from 'openapi-fetch';
import type { paths } from './schema';
import { API_URL } from './config';
import { accessToken, refresh } from './auth';
import { ApiError, problemFrom } from './errors';

// Every request gets the in-memory access token. A 401 `token_expired` (or any 401 while
// we still hold a refresh token) rotates once and retries the request once.
async function authedFetch(input: Request): Promise<Response> {
  const retry = input.clone();
  const send = async (req: Request, token: string | null) => {
    if (token) req.headers.set('Authorization', `Bearer ${token}`);
    try {
      return await fetch(req);
    } catch {
      throw new ApiError(0, { code: 'network' });
    }
  };
  const res = await send(input, await accessToken());
  if (res.status !== 401) return res;
  const token = await refresh();
  return token ? send(retry, token) : res;
}

export const api = createClient<paths>({ baseUrl: API_URL, fetch: authedFetch });

type Result<T> = { data?: T; error?: unknown; response: Response };

/** Resolve an openapi-fetch call to its data, or throw an ApiError. */
export async function unwrap<T>(p: Promise<Result<T>>): Promise<T> {
  const { data, error, response } = await p;
  if (response.ok) return data as T;
  if (error && typeof error === 'object') {
    const ra = Number(response.headers.get('Retry-After'));
    return Promise.reject(new ApiError(response.status, error as object, Number.isFinite(ra) && ra > 0 ? ra : undefined));
  }
  return Promise.reject(await problemFrom(response));
}
