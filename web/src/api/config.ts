/**
 * Base URL of the API without `/v1`, from `VITE_API_URL` at build time. The root
 * .env.example's older name, `VITE_API_BASE_URL`, is accepted too.
 */
export const API_URL: string = (import.meta.env.VITE_API_URL || import.meta.env.VITE_API_BASE_URL || '').replace(/\/+$/, '');
export const API_CONFIGURED = API_URL.length > 0;
