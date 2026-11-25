import type { components } from './schema';

export type Problem = components['schemas']['Problem'];
export type ProblemCode = Problem['code'];

/** An RFC 9457 problem from the API. `detail` is safe to show (contract decision 12). */
export class ApiError extends Error {
  readonly status: number;
  readonly code: ProblemCode | 'network';
  readonly detail?: string;
  readonly errors: { field: string; message: string }[];
  readonly retryAfter?: number;
  constructor(status: number, p: Omit<Partial<Problem>, 'code'> & { code?: ProblemCode | 'network' }, retryAfter?: number) {
    super(p.detail ?? p.title ?? `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.code = p.code ?? (status === 0 ? 'network' : 'internal');
    this.detail = p.detail;
    this.errors = p.errors ?? [];
    this.retryAfter = retryAfter;
  }
}

export async function problemFrom(res: Response): Promise<ApiError> {
  let body: Partial<Problem> = {};
  try {
    body = (await res.json()) as Partial<Problem>;
  } catch {
    /* not JSON */
  }
  const ra = Number(res.headers.get('Retry-After'));
  return new ApiError(res.status, body, Number.isFinite(ra) && ra > 0 ? ra : undefined);
}

/** Short, factual text for an error, in the product's voice. */
export function errorText(e: unknown): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case 'network':
        return 'Cannot reach RiskPredict. Check your connection.';
      case 'rate_limited':
        return e.retryAfter ? `Too many requests. Try again in ${e.retryAfter} s.` : 'Too many requests. Try again shortly.';
      case 'forbidden':
        return e.detail ?? 'Your role cannot do this. Ask an admin.';
      case 'invalid_credentials':
        return 'Wrong email or password.';
      case 'account_locked':
        return 'Too many failed attempts. The account is locked for 15 minutes.';
      case 'upstream_unavailable':
      case 'ai_unavailable':
        return e.detail ?? 'A connected service is not answering. Try again in a minute.';
      default:
        return e.detail ?? (e.status >= 500 ? 'Something went wrong on our side. Try again.' : e.message);
    }
  }
  return 'Something went wrong. Try again.';
}
